"""LangGraph ``ChandraState`` — single source of truth threaded through every node.

Reducers (``Annotated[T, fn]``) are how LangGraph merges concurrent updates emitted
by parallel observer branches. Without them, the second branch to return would
clobber the first.
"""

from __future__ import annotations

from operator import add
from typing import Annotated, Any, TypedDict

from chandra.briefing.schemas import AnalyzedFinding, Finding


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
    out: dict[str, list[dict[str, Any]]] = {
        k: list(v) for k, v in (left or {}).items()
    }
    for rtype, items in (right or {}).items():
        out.setdefault(rtype, []).extend(items)
    return out


class ChandraState(TypedDict, total=False):
    """Full graph state. ``total=False`` so partial node returns are legal."""

    run_id: str
    account_id: str
    regions: list[str]
    inventory: Annotated[dict[str, list[dict[str, Any]]], merge_inventory]
    raw_findings: Annotated[dict[str, list[Finding]], merge_raw_findings]
    analyzed_findings: list[AnalyzedFinding]
    scorecard: dict[str, int]
    briefing_md: str
    briefing_json: dict[str, Any]
    errors: Annotated[list[dict[str, Any]], add]
    bedrock_input_tokens: int
    bedrock_output_tokens: int
    bedrock_cost_usd: float