"""Organization-level summary aggregation."""

from __future__ import annotations

from typing import Any

from src.chandra.briefing.schemas import Scorecard
from src.chandra.logging import get_logger

logger = get_logger(__name__)


def aggregate_org_scorecard(
    account_scorecards: dict[str, dict[str, int]],
) -> dict[str, int]:
    """
    Aggregate per-account scorecards into org-level scorecard.

    Simple average across all accounts.

    Args:
        account_scorecards: {account_id: {kra: score, ...}}

    Returns:
        Aggregated scorecard dict
    """
    if not account_scorecards:
        return {
            "cost": 0,
            "security": 0,
            "compliance": 0,
            "performance": 0,
            "reliability": 0,
            "overall": 0,
        }

    kras = [
        "cost",
        "security",
        "compliance",
        "performance",
        "reliability",
    ]

    org_scorecard: dict[str, int] = {}

    for kra in kras:
        scores = [
            sc.get(kra, 0)
            for sc in account_scorecards.values()
        ]
        org_scorecard[kra] = (
            sum(scores) // len(scores)
            if scores
            else 0
        )

    overall = (
        sum(org_scorecard.values())
        // len(kras)
    )
    org_scorecard["overall"] = overall

    logger.info(
        "org_summary.aggregate_scorecard",
        org_scorecard=org_scorecard,
        account_count=len(
            account_scorecards
        ),
    )

    return org_scorecard


def build_org_briefing(
    account_briefings: dict[str, str],
    org_scorecard: dict[str, int],
) -> str:
    """
    Build organization-level briefing markdown.

    Args:
        account_briefings: {account_id: briefing_md, ...}
        org_scorecard: Aggregated scorecard

    Returns:
        Organization briefing markdown
    """
    lines: list[str] = []

    lines.append("# Organization Briefing")
    lines.append("")

    lines.append("## Organization Scorecard")
    lines.append(
        "| KRA | Score |"
    )
    lines.append(
        "| --- | ----- |"
    )
    for kra, score in org_scorecard.items():
        lines.append(
            f"| {kra} | {score} |"
        )
    lines.append("")

    lines.append("## Per-Account Briefings")
    lines.append("")
    for (
        account_id,
        briefing,
    ) in account_briefings.items():
        lines.append(
            f"### Account {account_id}"
        )
        lines.append("")
        lines.append(briefing)
        lines.append("")

    return "\n".join(lines)
