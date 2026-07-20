"""LangGraph node implementations for the Chandra observation pipeline.

Topology:

::

    START
      └─► onboard_account
            └─► kra_supervisor (returns Send(...) per KRA — slim projection only)
                  ├─► observe_cost
                  ├─► observe_security
                  ├─► observe_compliance
                  ├─► observe_performance
                  └─► observe_reliability
                        └─► analyze
                              └─► compose_briefing
                                    └─► persist
                                          └─► END

LG-07: _route_kra_workers now passes a slim projection
       {run_id, account_id, regions} instead of the full ChandraState.
       Observers only need these three fields; reducers handle the merge.
       Checkpoint row size shrinks ~4-5x for 1k-resource accounts.
"""

from __future__ import annotations

import os

from datetime import UTC, datetime
from typing import Any, Literal

from langgraph.types import Send, interrupt
from src.chandra.aws.client_factory import get_default_factory
from src.chandra.aws.regions import active_regions
from src.chandra.briefing.composer import (
    compose_executive_summary,
    deterministic_rank,
    render_markdown,
    score_findings,
)
from src.chandra.briefing.schemas import (
    AnalyzedFinding,
    ApprovalDecision,
    Finding,
    Observation,
    ProposedWrite,
)
from src.chandra.db.models import Briefing, Run
from src.chandra.db.models import Finding as FindingRow
from src.chandra.db.session import session_scope
from src.chandra.escalation.publisher import SNSPublisher
from src.chandra.escalation.schemas import EscalationPayload
from src.chandra.graphs.action_nodes.action_executor import action_executor_node  # noqa: F401
from src.chandra.graphs.state import ChandraState
from src.chandra.logging import get_logger
from src.chandra.observability import traced_node
from src.chandra.tools import compliance, cost, performance, reliability, security
from src.chandra.tools.base import DetectorContext, detector_guard, paginate

logger = get_logger(__name__)


KRA_RUNNERS = {
    "cost": cost.run_all,
    "security": security.run_all,
    "compliance": compliance.run_all,
    "performance": performance.run_all,
    "reliability": reliability.run_all,
}

ESCALATE_SEVERITIES: frozenset[str] = frozenset({"critical", "high"})

KRAS_TO_RUN: tuple[str, ...] = (
    "cost",
    "security",
    "compliance",
    "performance",
    "reliability",
)


# ---------------------------------------------------------------------------
# Node: onboard_account
# ---------------------------------------------------------------------------


@traced_node
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
# KRA Supervisor + Slim Send Router  ← LG-07
# ---------------------------------------------------------------------------


@traced_node
def kra_supervisor(state: ChandraState) -> dict[str, Any]:
    """Supervisor node: dispatches to all KRA worker nodes in parallel.

    Routing is handled by _route_kra_workers conditional edge.
    """
    selected = state.get("selected_kras", [])
    active_kras = [k for k in selected if k in KRAS_TO_RUN]

    logger.info(
        "graph.kra_supervisor",
        run_id=state["run_id"],
        kras=active_kras,
    )
    return {}


def _route_kra_workers(state: ChandraState) -> list[Send]:
    """LG-07: Slim projection — only run_id, account_id, regions on the wire.

    Full state passed to 5 parallel branches = dead weight in checkpoints.
    Reducers handle merge back into ChandraState.
    Expect ~4-5x smaller checkpoint rows for 1k-resource accounts.
    """
    projection = {
        "run_id": state["run_id"],
        "account_id": state["account_id"],
        "regions": list(state.get("regions", [])),
    }

    selected = state.get("selected_kras", [])
    active_kras = [k for k in selected if k in KRAS_TO_RUN]

    return [Send(f"observe_{kra}", projection) for kra in active_kras]


# ---------------------------------------------------------------------------
# Observer nodes — one per KRA
# Signatures type as ChandraState; total=False allows partial dict from Send
# ---------------------------------------------------------------------------


def _run_observer(kra: str, state: ChandraState) -> dict[str, Any]:
    """Shared runner for all KRA observers.

    Reads ONLY: state["run_id"], state["account_id"], state.get("regions").
    LG-07 guarantees the projection contains exactly these three keys.
    """
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


@traced_node
def observe_cost(state: ChandraState) -> dict[str, Any]:
    return _run_observer("cost", state)


@traced_node
def observe_security(state: ChandraState) -> dict[str, Any]:
    return _run_observer("security", state)


@traced_node
def observe_compliance(state: ChandraState) -> dict[str, Any]:
    return _run_observer("compliance", state)


@traced_node
def observe_performance(state: ChandraState) -> dict[str, Any]:
    return _run_observer("performance", state)


@traced_node
def observe_reliability(state: ChandraState) -> dict[str, Any]:
    return _run_observer("reliability", state)


# ---------------------------------------------------------------------------
# Ingest observations (CloudWatch + EventBridge)
# ---------------------------------------------------------------------------


@traced_node
def ingest_observations(state: ChandraState) -> dict[str, Any]:
    """Ingest CloudWatch alarms and EventBridge rules into state.observations."""
    ctx = DetectorContext(
        run_id=state["run_id"],
        account_id=state["account_id"],
        regions=list(state.get("regions", [])),
    )
    observations: list[Observation] = []

    for region in ctx.regions:
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
                            observed_at=alarm.get("StateUpdatedTimestamp", datetime.now(UTC)),
                            raw={
                                "namespace": alarm.get("Namespace", ""),
                                "metric_name": alarm.get("MetricName", ""),
                                "threshold": alarm.get("Threshold"),
                                "comparison_operator": alarm.get("ComparisonOperator", ""),
                            },
                        )
                    )

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
                            observed_at=datetime.now(UTC),
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
# Decision Router
# ---------------------------------------------------------------------------


@traced_node
def decision_router(state: ChandraState) -> dict[str, Any]:
    """Classify each AnalyzedFinding as low-risk (auto-fix) or high-risk (escalate).

    critical/high  → pending_writes → approval_node interrupt
    medium/low/info → auto_fixed    → action_executor_node, no interrupt
    """
    analyzed = state.get("analyzed_findings", []) or []
    pending: list[ProposedWrite] = []
    auto_fixed: list[ProposedWrite] = []

    for af in analyzed:
        f = af.finding
        risk: Literal["low", "high"] = "high" if f.severity in ESCALATE_SEVERITIES else "low"
        write = ProposedWrite(
            action=f"remediate_{f.detector_id}",
            target_arn=f.resource_arn,
            region=f.region,
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
# Analyze
# ---------------------------------------------------------------------------


@traced_node
def analyze(state: ChandraState) -> dict[str, Any]:
    """Rank + dedup findings; compute per-KRA scorecard.

    LLM ranking via chandra.briefing.composer; deterministic fallback on failure.
    """
    raw = state.get("raw_findings", {}) or {}
    flat: list[Finding] = []
    for kra_findings in raw.values():
        flat.extend(kra_findings)

    analyzed: list[AnalyzedFinding] = deterministic_rank(flat)
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
# Compose briefing
# ---------------------------------------------------------------------------


@traced_node
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
        "generated_at": datetime.now(UTC).isoformat(),
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
# Approval (human-in-the-loop)
# ---------------------------------------------------------------------------


@traced_node
def approval_node(state: ChandraState) -> dict[str, Any]:
    """Interrupt on pending writes for human approval.

    Pass-through when no pending writes.
    On resume, creates ApprovalDecision records from returned payload.
    """
    pending = state.get("pending_writes", []) or []
    if not pending:
        return {}

    payload = interrupt({"pending_writes": [p.model_dump() for p in pending]})
    return {"approvals": [ApprovalDecision(**d) for d in payload]}


# ---------------------------------------------------------------------------
# Escalation node
# ---------------------------------------------------------------------------


@traced_node
def escalation_node(state: ChandraState) -> dict[str, Any]:
    """Publish escalation alerts to SNS for critical/high findings."""
    publisher = SNSPublisher(
        topic_arn=state.get("sns_topic_arn")
        or "arn:aws:sns:us-east-1:123456789012:chandra-escalations"
    )
    payload = EscalationPayload(
        finding_id=state.get("finding_id", "unknown"),
        resource_id=state.get("resource_id", "unknown"),
        severity=state.get("severity", "medium"),
        service=state.get("service", "aws"),
        region=state.get("region", os.getenv("AWS_DEFAULT_REGION", "us-east-1")),
        summary=state.get("summary", "Security finding"),
        recommended_action=state.get("recommended_action", "Review and remediate"),
    )
    result = publisher.publish(payload)
    if result.status == "skipped":
        logger.warning(f"Escalation skipped: {result.error}")
    elif result.status == "failed":
        logger.error(f"Escalation failed: {result.error}")
    else:
        logger.info(f"Escalation succeeded: {result.message_id}")
    return {"escalation_result": result.model_dump()}


# ---------------------------------------------------------------------------
# Persist (only node allowed to write to Postgres outside migrations)
# ---------------------------------------------------------------------------


@traced_node
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
                finished_at=datetime.now(UTC),
                errors_json=errors,
                bedrock_cost_usd=state.get("bedrock_cost_usd", 0.0),
            )
            sess.add(run)
        else:
            run.account_id = account_id
            run.status = "completed"
            run.finished_at = datetime.now(UTC)
            run.errors_json = errors
            run.bedrock_cost_usd = state.get("bedrock_cost_usd", 0.0)

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

        existing_briefing = sess.query(Briefing).filter(Briefing.run_id == run_id).one_or_none()
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
