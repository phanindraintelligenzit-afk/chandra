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

from datetime import UTC, datetime
from typing import Any, Literal

from langgraph.types import Send, interrupt
from src.chandra.aws.client_factory import get_default_factory
from src.chandra.aws.regions import active_regions
from src.chandra.briefing.composer import (
    compose_executive_summary,
    llm_rank,
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
from src.chandra.config import settings
from src.chandra.db.models import Briefing, Run, serialize_finding_evidence
from src.chandra.db.models import Finding as FindingRow
from src.chandra.db.session import session_scope
from src.chandra.escalation.publisher import SNSPublisher
from src.chandra.escalation.schemas import EscalationPayload, EscalationResult
from src.chandra.graphs.action_nodes.action_executor import action_executor_node
from src.chandra.graphs.state import ChandraState
from src.chandra.logging import get_logger
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


# ---------------------------------------------------------------------------
# Decision Router (low-risk auto-fix vs high-risk escalate)
# ---------------------------------------------------------------------------


def decision_router(state: ChandraState) -> dict[str, Any]:
    """Classify each AnalyzedFinding as low-risk (auto-fix) or high-risk (escalate).

    High-risk findings (critical/high severity) are added to pending_writes,
    triggering the approval_node interrupt for human review.
    Low-risk findings (medium/low/info severity) are added to auto_fixed —
    consumed downstream by action_executor_node, no human interrupt.
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
            severity=f.severity,
            summary=f.title,
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
    # Caller-supplied ``sns_topic_arn`` wins; otherwise seed from settings
    # so the escalation node publishes to the right topic in every entry
    # point (run.py, FastAPI, the harness). Falls through to the
    # escalation node's own placeholder if neither is set, so omitting
    # ``SNS_TOPIC_ARN`` still runs end-to-end (just without notifying).
    sns_topic_arn = state.get("sns_topic_arn") or settings.sns_topic_arn
    logger.info(
        "graph.onboard",
        run_id=state.get("run_id"),
        account_id=account_id,
        regions=regions,
        sns_topic_arn_set=bool(sns_topic_arn),
    )
    return {
        "account_id": account_id,
        "regions": regions,
        "sns_topic_arn": sns_topic_arn,
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


def fanout_observers(state: ChandraState) -> list[Send]:
    """Dispatch to all KRA observer nodes in parallel."""
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
                            observed_at=alarm.get("StateUpdatedTimestamp", datetime.now(UTC)),
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
    logger.info(
        "graph.compose_briefing",
        run_id=state["run_id"],
        analyzed_findings=len(analyzed),
        briefing_length=len(briefing_md),
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
    pending_raw = state.get("pending_writes", []) or []
    if not pending_raw:
        logger.info(
            "graph.approval",
            run_id=state.get("run_id"),
            pending_writes=0,
        )
        return {}

    # When this node runs after a checkpoint round-trip (i.e. on resume
    # from the interrupt_before pause), the LangGraph serde rehydrates
    # ``pending_writes`` items as plain dicts rather than ProposedWrite
    # instances — even with the module registered for msgpack. Normalise
    # back to ProposedWrite so the rest of the function is uniform.
    pending: list[ProposedWrite] = [
        p if isinstance(p, ProposedWrite) else ProposedWrite.model_validate(p) for p in pending_raw
    ]

    logger.info(
        "graph.approval",
        run_id=state.get("run_id"),
        pending_writes=len(pending),
    )
    payload = interrupt({"pending_writes": [p.model_dump() for p in pending]})
    return {"approvals": [ApprovalDecision(**d) for d in payload]}


# ---------------------------------------------------------------------------
# Persist (only node allowed to write to Postgres outside migrations)
# ---------------------------------------------------------------------------


def persist(state: ChandraState) -> dict[str, Any]:
    """Write run, findings and briefing rows. Idempotent on (run_id).

    Converts all datetime objects in evidence_jsonb to ISO strings before saving
    to prevent 'datetime is not JSON serializable' errors.
    """
    run_id = state["run_id"]
    account_id = state["account_id"]
    raw = state.get("raw_findings", {}) or {}
    scorecard = state.get("scorecard", {}) or {}
    briefing_md = state.get("briefing_md", "") or ""

    # Scorecard might have complex Pydantic types; simplify it
    def simplify_for_json(obj: Any) -> Any:
        """Recursively convert complex objects to JSON-serializable types."""
        if hasattr(obj, "model_dump"):
            return simplify_for_json(obj.model_dump())
        elif isinstance(obj, datetime):
            return obj.isoformat()
        elif isinstance(obj, dict):
            return {k: simplify_for_json(v) for k, v in obj.items()}
        elif isinstance(obj, (list, tuple)):
            return [simplify_for_json(item) for item in obj]
        else:
            return obj

    scorecard = simplify_for_json(scorecard)

    # Errors are skipped for now
    errors: list[dict[str, Any]] = []

    flat: list[Finding] = []
    for kra_findings in raw.values():
        flat.extend(kra_findings)

    findings_list = flat

    with session_scope() as sess:
        run = sess.get(Run, run_id)
        if run is None:
            run = Run(
                id=run_id,
                account_id=account_id,
                status="completed",
                finished_at=datetime.now(UTC),
                errors_json=errors,
            )
            sess.add(run)
        else:
            run.account_id = account_id
            run.status = "completed"
            run.finished_at = datetime.now(UTC)
            run.errors_json = errors

        # Replace findings for this run to keep persist idempotent.
        sess.query(FindingRow).filter(FindingRow.run_id == run_id).delete()
        for f in findings_list:
            # Serialize evidence_jsonb to remove datetime objects
            evidence_clean = serialize_finding_evidence(f.evidence)

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
                    evidence_jsonb=evidence_clean,
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
                    findings_count=len(findings_list),
                )
            )
        else:
            existing_briefing.scorecard_jsonb = scorecard
            existing_briefing.markdown_text = briefing_md
            existing_briefing.findings_count = len(findings_list)

        sess.commit()

    logger.info(
        "graph.persist",
        run_id=run_id,
        findings=len(findings_list),
        errors=len(errors),
    )
    return {}


def escalation_node(state: ChandraState) -> dict[str, Any]:
    """Publish one SNS message per critical/high pending write.

    The ``decision_router`` already routes critical/high findings to
    ``pending_writes`` (and medium/low to ``auto_fixed``). We iterate the
    list, build a per-finding :class:`EscalationPayload`, and publish.
    Each write carries its own ``severity``, ``summary``, and
    ``recommendation`` so the resulting SNS message identifies the
    specific resource — not a generic "unknown / medium" placeholder.
    """
    topic_arn = state.get("sns_topic_arn")
    if not topic_arn:
        logger.warning(
            "graph.escalation.skipped_no_topic",
            run_id=state.get("run_id"),
        )
        return {
            "escalation_result": EscalationResult(
                status="skipped",
                error="sns_topic_arn not set in state",
            ).model_dump()
        }

    region = str(state.get("region") or "us-east-1")
    publisher = SNSPublisher(topic_arn=topic_arn, region=region)
    pending: list[ProposedWrite] = state.get("pending_writes", []) or []

    if not pending:
        logger.info(
            "graph.escalation.no_pending",
            run_id=state.get("run_id"),
        )
        return {
            "escalation_result": EscalationResult(
                status="skipped",
                error="no pending writes to escalate",
            ).model_dump()
        }

    published: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    for write in pending:
        # Defensive filter: decision_router routes non-critical/high to
        # ``auto_fixed`` already, but persisted rows or fixtures may
        # contain lower-severity writes. Skip them here.
        sev = write.severity or "high"
        if sev not in ESCALATE_SEVERITIES:
            continue
        # Derive service from the ARN's third colon-separated component
        # (arn:aws:<service>:...). Falls back to ``aws`` for malformed
        # ARNs so we never block the run on a parse error.
        arn_parts = write.target_arn.split(":", 2)
        service = arn_parts[2] if len(arn_parts) >= 3 else "aws"
        # ``action`` is ``remediate_<detector_id>``; strip the prefix
        # to recover the canonical finding_id.
        finding_id = write.action.removeprefix("remediate_")
        payload = EscalationPayload(
            finding_id=finding_id,
            resource_id=write.target_arn,
            severity=sev,
            service=service,
            region=write.region or region,
            summary=write.summary or write.justification,
            recommended_action=write.payload.get("recommendation", "Review and remediate"),
        )
        result = publisher.publish(payload)
        if result.status == "success":
            published.append({"finding_id": finding_id, "message_id": result.message_id or ""})
        else:
            failed.append({"finding_id": finding_id, "error": result.error or "unknown"})

    if published and not failed:
        status = "success"
    elif published and failed:
        status = "partial"
    else:
        status = "failed"

    logger.info(
        "graph.escalation",
        run_id=state.get("run_id"),
        published=len(published),
        failed=len(failed),
        status=status,
    )
    return {
        "escalation_result": {
            "status": status,
            "published": published,
            "failed": failed,
        }
    }


__all__ = [
    "_route_kra_workers",
    "action_executor_node",
    "analyze",
    "approval_node",
    "compose_briefing",
    "decision_router",
    "escalation_node",
    "fanout_observers",
    "ingest_observations",
    "kra_supervisor",
    "observe_compliance",
    "observe_cost",
    "observe_performance",
    "observe_reliability",
    "observe_security",
    "onboard_account",
    "persist",
]
