"""LangGraph node implementations for the Chandra observation pipeline.

Topology:

::

    START
      └─► onboard_account
            └─► fanout_observers (returns Send(...) per KRA)
                  ├─► observe_cost
                  ├─► observe_security
                  ├─► observe_compliance
                  ├─► observe_performance
                  └─► observe_reliability
                        └─► analyze
                              └─► compose_briefing
                                    └─► persist
                                          └─► END
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from langgraph.types import Send, interrupt

from chandra.aws.client_factory import get_default_factory
from chandra.aws.regions import active_regions
from chandra.briefing.composer import (
    compose_executive_summary,
    llm_rank,
    render_markdown,
    score_findings,
)
from chandra.briefing.schemas import (
    AnalyzedFinding,
    ApprovalDecision,
    Finding,
    Observation,
    ProposedWrite,
)
from chandra.db.models import Briefing, Finding as FindingRow, Run
from chandra.db.session import session_scope
from chandra.graphs.state import ChandraState
from chandra.logging import get_logger
from chandra.tools import compliance, cost, performance, reliability, security
from chandra.tools.base import DetectorContext, detector_guard, paginate

logger = get_logger(__name__)


KRA_RUNNERS = {
    "cost": cost.run_all,
    "security": security.run_all,
    "compliance": compliance.run_all,
    "performance": performance.run_all,
    "reliability": reliability.run_all,
}

ESCALATE_SEVERITIES: frozenset[str] = frozenset({"critical", "high"})


# ---------------------------------------------------------------------------
# Decision Router (low-risk auto-fix vs high-risk escalate)
# ---------------------------------------------------------------------------


def decision_router(state: ChandraState) -> dict[str, Any]:
    """Classify each AnalyzedFinding as low-risk (auto-fix) or high-risk (escalate).

    High-risk findings (critical/high severity) are added to pending_writes,
    triggering the approval_node interrupt for human review.
    Low-risk findings (medium/low/info severity) are added to auto_fixed —
    flagged for a future executor node, no human interrupt.
    """
    analyzed = state.get("analyzed_findings", []) or []
    pending: list[ProposedWrite] = []
    auto_fixed: list[ProposedWrite] = []

    for af in analyzed:
        f = af.finding
        risk: Literal["low", "high"] = (
            "high" if f.severity in ESCALATE_SEVERITIES else "low"
        )
        write = ProposedWrite(
            action=f"remediate_{f.detector_id}",
            target_arn=f.resource_arn,
            payload={"recommendation": f.recommendation},
            requested_by="decision_router",
            justification=af.rationale or f.recommendation,
            risk_level=risk,
        )
        if risk == "high":
            pending.append(write)
        else:
            auto_fixed.append(write)

    logger.info(
        "graph.decision_router",
        run_id=state["run_id"],
        escalated=len(pending),
        auto_fixed=len(auto_fixed),
    )
    return {"pending_writes": pending, "auto_fixed": auto_fixed}


# ---------------------------------------------------------------------------
# Node: onboard_account
# ---------------------------------------------------------------------------


def onboard_account(state: ChandraState) -> dict[str, Any]:
    """Validate the run, resolve regions, and seed the inventory."""
    factory = get_default_factory()
    account_id = state.get("account_id") or factory.account_id()
    regions = state.get("regions") or active_regions(account_id, factory=factory)
    logger.info(
        "graph.onboard",
        run_id=state.get("run_id"),
        account_id=account_id,
        regions=regions,
    )
    return {
        "account_id": account_id,
        "regions": regions,
        "raw_findings": {},
        "errors": [],
    }


# ---------------------------------------------------------------------------
# KRA Supervisor + Workers
# ---------------------------------------------------------------------------

KRAS_TO_RUN: tuple[str, ...] = (
    "cost",
    "security",
    "compliance",
    "performance",
    "reliability",
)


def kra_supervisor(state: ChandraState) -> dict[str, Any]:
    """Supervisor node: dispatches to all KRA worker nodes.

    Routes to the 5 KRA worker nodes (observe_cost, observe_security, etc.)
    in parallel. Routing logic lives here so future iterations can skip or
    re-order KRAs without touching the graph topology.
    """
    logger.info(
        "graph.kra_supervisor",
        run_id=state["run_id"],
        kras=list(KRAS_TO_RUN),
    )
    return {}


def _route_kra_workers(state: ChandraState) -> list[Send]:
    """Route to all KRA worker nodes in parallel."""
    return [Send(f"observe_{kra}", state) for kra in KRAS_TO_RUN]


# ---------------------------------------------------------------------------
# Observer nodes — one per KRA
# ---------------------------------------------------------------------------


def _run_observer(kra: str, state: ChandraState) -> dict[str, Any]:
    runner = KRA_RUNNERS[kra]
    ctx = DetectorContext(
        run_id=state["run_id"],
        account_id=state["account_id"],
        regions=list(state.get("regions", [])),
    )
    findings: list[Finding] = runner(ctx)
    logger.info(
        "graph.observe",
        kra=kra,
        run_id=state["run_id"],
        count=len(findings),
        errors=len(ctx.errors),
    )
    return {
        "raw_findings": {kra: findings},
        "errors": ctx.errors,
    }


def observe_cost(state: ChandraState) -> dict[str, Any]:
    return _run_observer("cost", state)


def observe_security(state: ChandraState) -> dict[str, Any]:
    return _run_observer("security", state)


def observe_compliance(state: ChandraState) -> dict[str, Any]:
    return _run_observer("compliance", state)


def observe_performance(state: ChandraState) -> dict[str, Any]:
    return _run_observer("performance", state)


def observe_reliability(state: ChandraState) -> dict[str, Any]:
    return _run_observer("reliability", state)


# ---------------------------------------------------------------------------
# Ingest observations (CloudWatch + EventBridge)
# ---------------------------------------------------------------------------


def ingest_observations(state: ChandraState) -> dict[str, Any]:
    """Ingest CloudWatch alarms and EventBridge rules into state.observations."""
    ctx = DetectorContext(
        run_id=state["run_id"],
        account_id=state["account_id"],
        regions=list(state.get("regions", [])),
    )
    observations: list[Observation] = []

    for region in ctx.regions:
        # CloudWatch alarms
        with detector_guard(ctx, detector_id="OBS-cloudwatch-alarms", region=region):
            cw = ctx.factory.client("cloudwatch", region=region)
            for page in paginate(cw, "describe_alarms"):
                for alarm in page.get("MetricAlarms", []):
                    observations.append(
                        Observation(
                            source="cloudwatch_alarm",
                            resource_arn=alarm["AlarmArn"],
                            region=region,
                            name=alarm["AlarmName"],
                            state=alarm["StateValue"],
                            observed_at=alarm.get(
                                "StateUpdatedTimestamp", datetime.now(timezone.utc)
                            ),
                            raw={
                                "namespace": alarm.get("Namespace", ""),
                                "metric_name": alarm.get("MetricName", ""),
                                "threshold": alarm.get("Threshold"),
                                "comparison_operator": alarm.get("ComparisonOperator", ""),
                            },
                        )
                    )

        # EventBridge rules
        with detector_guard(ctx, detector_id="OBS-eventbridge-rules", region=region):
            eb = ctx.factory.client("events", region=region)
            for page in paginate(eb, "list_rules"):
                for rule in page.get("Rules", []):
                    observations.append(
                        Observation(
                            source="eventbridge_rule",
                            resource_arn=rule["Arn"],
                            region=region,
                            name=rule["Name"],
                            state=rule["State"],
                            observed_at=datetime.now(timezone.utc),
                            raw={
                                "event_bus_name": rule.get("EventBusName", "default"),
                                "schedule_expression": rule.get("ScheduleExpression", ""),
                                "event_pattern": rule.get("EventPattern", ""),
                                "description": rule.get("Description", ""),
                            },
                        )
                    )

    logger.info(
        "graph.ingest_observations",
        run_id=state["run_id"],
        count=len(observations),
        errors=len(ctx.errors),
    )
    return {"observations": observations, "errors": ctx.errors}


# ---------------------------------------------------------------------------
# Analyze (LLM rank + dedup, deterministic fallback)
# ---------------------------------------------------------------------------


def analyze(state: ChandraState) -> dict[str, Any]:
    """Rank + dedup findings and compute the per-KRA scorecard.

    Calls the LLM via :mod:`chandra.briefing.composer` for narrative ranking,
    but falls back to deterministic severity-weight ordering on any failure.
    """
    raw = state.get("raw_findings", {}) or {}
    flat: list[Finding] = []
    for kra_findings in raw.values():
        flat.extend(kra_findings)

    analyzed: list[AnalyzedFinding] = llm_rank(flat)
    scorecard = score_findings(raw)

    logger.info(
        "graph.analyze",
        run_id=state["run_id"],
        total=len(flat),
        scorecard=scorecard.as_dict(),
    )
    return {
        "analyzed_findings": analyzed,
        "scorecard": scorecard.as_dict(),
    }


# ---------------------------------------------------------------------------
# Compose briefing (LLM narrative)
# ---------------------------------------------------------------------------


def compose_briefing(state: ChandraState) -> dict[str, Any]:
    """Render the markdown + JSON briefing for the run."""
    analyzed = state.get("analyzed_findings", []) or []
    scorecard = state.get("scorecard", {}) or {}
    raw = state.get("raw_findings", {}) or {}

    flat: list[Finding] = []
    for kra_findings in raw.values():
        flat.extend(kra_findings)

    executive = compose_executive_summary(analyzed, scorecard)
    metadata = {
        "regions": state.get("regions", []),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "errors": state.get("errors", []),
    }
    briefing_md, briefing_json = render_markdown(
        run_id=state["run_id"],
        account_id=state["account_id"],
        scorecard=scorecard,
        executive_summary=executive,
        top_findings=analyzed[:10],
        all_findings=flat,
        metadata=metadata,
    )
    return {"briefing_md": briefing_md, "briefing_json": briefing_json}


# ---------------------------------------------------------------------------
# Approval (human-in-the-loop checkpoint)
# ---------------------------------------------------------------------------


def approval_node(state: ChandraState) -> dict[str, Any]:
    """Interrupt on pending writes for human approval.

    If no pending writes exist, returns empty dict (no change).
    Otherwise, emits an interrupt with pending writes; on resume,
    creates ApprovalDecision records from the payload.
    """
    pending = state.get("pending_writes", []) or []
    if not pending:
        return {}

    payload = interrupt({"pending_writes": [p.model_dump() for p in pending]})
    return {"approvals": [ApprovalDecision(**d) for d in payload]}


# ---------------------------------------------------------------------------
# Persist (only node allowed to write to Postgres outside migrations)
# ---------------------------------------------------------------------------


def persist(state: ChandraState) -> dict[str, Any]:
    """Write run, findings and briefing rows. Idempotent on (run_id)."""
    run_id = state["run_id"]
    account_id = state["account_id"]
    raw = state.get("raw_findings", {}) or {}
    scorecard = state.get("scorecard", {}) or {}
    briefing_md = state.get("briefing_md", "") or ""
    errors = state.get("errors", []) or []

    flat: list[Finding] = []
    for kra_findings in raw.values():
        flat.extend(kra_findings)

    with session_scope() as sess:
        run = sess.get(Run, run_id)
        if run is None:
            run = Run(
                id=run_id,
                account_id=account_id,
                status="completed",
                finished_at=datetime.now(timezone.utc),
                errors_json=errors,
            )
            sess.add(run)
        else:
            run.account_id = account_id
            run.status = "completed"
            run.finished_at = datetime.now(timezone.utc)
            run.errors_json = errors

        # Replace findings for this run to keep persist idempotent.
        sess.query(FindingRow).filter(FindingRow.run_id == run_id).delete()
        for f in flat:
            sess.add(
                FindingRow(
                    run_id=run_id,
                    kra=f.kra,
                    severity=f.severity,
                    detector_id=f.detector_id,
                    resource_arn=f.resource_arn,
                    resource_type=f.resource_type,
                    region=f.region,
                    title=f.title,
                    evidence_jsonb=f.evidence,
                    recommendation=f.recommendation,
                )
            )

        existing_briefing = (
            sess.query(Briefing).filter(Briefing.run_id == run_id).one_or_none()
        )
        if existing_briefing is None:
            sess.add(
                Briefing(
                    run_id=run_id,
                    scorecard_jsonb=scorecard,
                    markdown_text=briefing_md,
                    findings_count=len(flat),
                )
            )
        else:
            existing_briefing.scorecard_jsonb = scorecard
            existing_briefing.markdown_text = briefing_md
            existing_briefing.findings_count = len(flat)

    logger.info(
        "graph.persist",
        run_id=run_id,
        findings=len(flat),
        errors=len(errors),
    )
    return {}
