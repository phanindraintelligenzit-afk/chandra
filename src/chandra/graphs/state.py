"""LangGraph ``ChandraState`` — single source of truth threaded through every node.

Reducers (``Annotated[T, fn]``) are how LangGraph merges concurrent updates emitted
by parallel observer branches. Without them, the second branch to return would
clobber the first.
"""

from __future__ import annotations

from operator import add
from typing import Annotated, Any, TypedDict

from src.chandra.briefing.schemas import (
    ActionResult,
    AnalyzedFinding,
    ApprovalDecision,
    Finding,
    Observation,
    ProposedWrite,
)


def merge_raw_findings(
    left: dict[str, list[Finding]] | None,
    right: dict[str, list[Finding]] | None,
) -> dict[str, list[Finding]]:
    """Reducer: deep-merge per-KRA finding lists from parallel observers."""
    out: dict[str, list[Finding]] = {k: list(v) for k, v in (left or {}).items()}
    for kra, items in (right or {}).items():
        out.setdefault(kra, []).extend(items)
    return out


def merge_inventory(
    left: dict[str, list[dict[str, Any]]] | None,
    right: dict[str, list[dict[str, Any]]] | None,
) -> dict[str, list[dict[str, Any]]]:
    """Reducer: deep-merge resource_type -> [resource] inventory dicts."""
    out: dict[str, list[dict[str, Any]]] = {k: list(v) for k, v in (left or {}).items()}
    for rtype, items in (right or {}).items():
        out.setdefault(rtype, []).extend(items)
    return out


class ChandraState(TypedDict, total=False):
    """Full graph state. ``total=False`` so partial node returns are legal."""

    assume_role_arn: str | None = None

    run_id: str
    account_id: str
    regions: list[str]
    # Seeded by ``onboard_account`` from ``Settings.sns_topic_arn`` (env
    # ``SNS_TOPIC_ARN``). The escalation node reads this to publish to
    # the right topic in every entry point (CLI, FastAPI, harness).
    sns_topic_arn: str | None
    # When False, ``action_executor_node`` actually invokes the boto3
    # mutating API for any detector with a registered handler. Defaults
    # to True; read by action_executor at runtime.
    dry_run: bool
    inventory: Annotated[dict[str, list[dict[str, Any]]], merge_inventory]
    raw_findings: Annotated[dict[str, list[Finding]], merge_raw_findings]
    observations: Annotated[list[Observation], add]
    analyzed_findings: list[AnalyzedFinding]
    scorecard: dict[str, int]
    pending_writes: Annotated[list[ProposedWrite], add]
    auto_fixed: Annotated[list[ProposedWrite], add]
    action_results: Annotated[list[ActionResult], add]
    approvals: list[ApprovalDecision]
    briefing_md: str
    briefing_json: dict[str, Any]
    escalation_result: dict[str, Any]
    errors: Annotated[list[dict[str, Any]], add]
    bedrock_input_tokens: int
    bedrock_output_tokens: int
    bedrock_cost_usd: float
