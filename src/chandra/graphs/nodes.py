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
from typing import Any

from langgraph.types import Send, interrupt

from chandra.aws.client_factory import get_default_factory
from chandra.aws.regions import active_regions
from chandra.briefing.composer import (
    compose_executive_summary,
    deterministic_rank,
    render_markdown,
    score_findings,
)
from chandra.briefing.schemas import AnalyzedFinding, ApprovalDecision, Finding, ProposedWrite
from chandra.db.models import Briefing, Finding as FindingRow, Run
from chandra.db.session import session_scope
from chandra.graphs.state import ChandraState
from chandra.logging import get_logger
from chandra.tools import compliance, cost, performance, reliability, security
from chandra.tools.base import DetectorContext

logger = get_logger(__name__)


KRA_RUNNERS = {
    "cost": cost.run_all,
    "security": security.run_all,
    "compliance": compliance.run_all,
    "performance": performance.run_all,
    "reliability": reliability.run_all,
}


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
# Fanout router
# ---------------------------------------------------------------------------


def fanout_observers(state: ChandraState) -> list[Send]:
    """Emit one ``Send(...)`` per KRA so observers run concurrently.

    LangGraph's parallel-branch primitive. Each observer receives the same
    state snapshot; their partial returns are merged by the reducers defined
    in :mod:`chandra.graphs.state`.
    """
    return [
        Send(f"observe_{kra}", state)
        for kra in ("cost", "security", "compliance", "performance", "reliability")
    ]


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
