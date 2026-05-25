"""Briefing composer.

Responsibilities:

* Deterministic scorecard math (severity weights, per-KRA + overall).
* Deterministic ranking fallback when Bedrock is unavailable.
* LLM-driven executive summary via :class:`langchain_aws.ChatBedrockConverse`.
* Markdown + JSON rendering.

The composer is the *only* place in the codebase that may call Bedrock.
Detector modules MUST NOT import it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from chandra.briefing.schemas import (
    KRAS,
    SEVERITY_WEIGHTS,
    AnalyzedFinding,
    BriefingPayload,
    Finding,
    Scorecard,
)
from chandra.config import settings
from chandra.logging import get_logger

logger = get_logger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

KRA_TIE_BREAK_ORDER = {
    "security": 0,
    "compliance": 1,
    "reliability": 2,
    "performance": 3,
    "cost": 4,
}


# ---------------------------------------------------------------------------
# Scorecard
# ---------------------------------------------------------------------------


def score_findings(raw_findings: dict[str, list[Finding]]) -> Scorecard:
    """Per-KRA score: ``100 - min(100, sum(severity_weight) * 5)``."""
    scores: dict[str, int] = {}
    for kra in KRAS:
        findings = raw_findings.get(kra, []) or []
        penalty = sum(SEVERITY_WEIGHTS.get(f.severity, 0) for f in findings) * 5
        scores[kra] = max(0, 100 - min(100, penalty))
    overall = round(sum(scores.values()) / len(KRAS))
    return Scorecard(
        cost=scores["cost"],
        security=scores["security"],
        compliance=scores["compliance"],
        performance=scores["performance"],
        reliability=scores["reliability"],
        overall=overall,
    )


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def deterministic_rank(findings: list[Finding]) -> list[AnalyzedFinding]:
    """Stable rank by severity weight, then KRA tie-break, then detector_id.

    Used both as the LLM-free fallback and as the seed order the LLM
    re-ranks from.
    """
    indexed = sorted(
        enumerate(findings),
        key=lambda pair: (
            -SEVERITY_WEIGHTS.get(pair[1].severity, 0),
            KRA_TIE_BREAK_ORDER.get(pair[1].kra, 99),
            pair[1].detector_id,
            pair[1].resource_arn,
        ),
    )
    return [
        AnalyzedFinding(finding=f, rank=i, rationale="")
        for i, (_, f) in enumerate(indexed)
    ]


def llm_rank(findings: list[Finding]) -> list[AnalyzedFinding]:
    """Ask Bedrock to re-rank findings. Falls back deterministically on error."""
    if not findings:
        return []

    try:
        from langchain_aws import ChatBedrockConverse  # type: ignore[import-not-found]
    except ImportError:
        logger.warning("llm.bedrock_unavailable_fallback_to_deterministic")
        return deterministic_rank(findings)

    try:
        llm = ChatBedrockConverse(
            model=settings.bedrock_model_id,
            region_name=settings.aws_default_region,
            # temperature=0.0,
            # max_tokens=2048,
        )
        system = (PROMPTS_DIR / "analyzer.md").read_text(encoding="utf-8")
        payload = [
            {
                "detector_id": f.detector_id,
                "kra": f.kra,
                "severity": f.severity,
                "resource_arn": f.resource_arn,
                "resource_type": f.resource_type,
                "region": f.region,
                "title": f.title,
                "recommendation": f.recommendation,
            }
            for f in findings
        ]
        response = llm.invoke(
            [
                ("system", system),
                ("user", json.dumps({"findings": payload}, default=str)),
            ]
        )
        text = response.content if isinstance(response.content, str) else str(response.content)
        ranked = json.loads(text).get("ranked", [])
    except Exception as exc:  # noqa: BLE001 - LLM is best-effort; fall back.
        logger.warning("llm.rank_failed_fallback_to_deterministic", error=str(exc))
        return deterministic_rank(findings)

    by_arn_detector = {(f.resource_arn, f.detector_id): f for f in findings}
    result: list[AnalyzedFinding] = []
    seen: set[tuple[str, str]] = set()
    for rank, item in enumerate(ranked):
        key = (item.get("resource_arn", ""), item.get("detector_id", ""))
        # Match by detector_id only if ARN missing — the prompt allows that.
        finding = by_arn_detector.get(key)
        if finding is None:
            for (arn, det), f in by_arn_detector.items():
                if det == item.get("detector_id") and (arn, det) not in seen:
                    finding = f
                    key = (arn, det)
                    break
        if finding is None:
            continue
        if key in seen:
            continue
        seen.add(key)
        result.append(
            AnalyzedFinding(
                finding=finding,
                rank=rank,
                rationale=str(item.get("rationale", ""))[:240],
            )
        )

    # Append any findings the LLM dropped to keep total set intact.
    for f in findings:
        key = (f.resource_arn, f.detector_id)
        if key in seen:
            continue
        result.append(AnalyzedFinding(finding=f, rank=len(result), rationale=""))

    return result


# ---------------------------------------------------------------------------
# Executive summary
# ---------------------------------------------------------------------------


def compose_executive_summary(
    analyzed: list[AnalyzedFinding],
    scorecard: dict[str, int] | Scorecard,
) -> list[str]:
    """Three-bullet exec summary. LLM-generated when Bedrock is reachable."""
    score_dict = scorecard.as_dict() if isinstance(scorecard, Scorecard) else scorecard
    try:
        from langchain_aws import ChatBedrockConverse  # type: ignore[import-not-found]
    except ImportError:
        return _deterministic_summary(analyzed, score_dict)

    try:
        llm = ChatBedrockConverse(
            model=settings.bedrock_model_id,
            region_name=settings.aws_default_region,
            # temperature=0.2,
            # max_tokens=512,
        )
        system = (PROMPTS_DIR / "briefer.md").read_text(encoding="utf-8")
        top = [
            {
                "rank": a.rank,
                "kra": a.finding.kra,
                "severity": a.finding.severity,
                "title": a.finding.title,
                "resource_arn": a.finding.resource_arn,
                "recommendation": a.finding.recommendation,
            }
            for a in analyzed[:10]
        ]
        user_payload = {
            "scorecard": score_dict,
            "top_findings": top,
            "total_findings": len(analyzed),
        }
        response = llm.invoke(
            [
                ("system", system),
                ("user", json.dumps(user_payload, default=str)),
            ]
        )
        text = response.content if isinstance(response.content, str) else str(response.content)
        bullets = [
            line[2:].strip()
            for line in text.splitlines()
            if line.strip().startswith("- ")
        ]
        if len(bullets) >= 3:
            return bullets[:3]
    except Exception as exc:  # noqa: BLE001 - LLM is best-effort.
        logger.warning("llm.summary_failed_fallback_to_deterministic", error=str(exc))

    return _deterministic_summary(analyzed, score_dict)


def _deterministic_summary(
    analyzed: list[AnalyzedFinding], scorecard: dict[str, int]
) -> list[str]:
    if not analyzed:
        return [
            "- Account is clean across all five KRAs — no actionable findings this run.",
            "- No urgent action required. Continue current cadence.",
            "- Re-run after any infra change touching IAM, networking, or storage.",
        ]
    worst_kra = min(scorecard.items(), key=lambda kv: kv[1])
    top = analyzed[0].finding
    crit = sum(1 for a in analyzed if a.finding.severity == "critical")
    high = sum(1 for a in analyzed if a.finding.severity == "high")
    return [
        (
            f"Overall posture: {scorecard.get('overall', 0)}/100. Weakest KRA is "
            f"{worst_kra[0]} at {worst_kra[1]}/100."
        ),
        (
            f"Most urgent action: address {top.detector_id} on {top.resource_arn} — "
            f"{top.title.lower()}."
        ),
        (
            f"{crit} critical and {high} high-severity findings detected across "
            f"{len(analyzed)} total."
        ),
    ]


# ---------------------------------------------------------------------------
# Markdown / JSON render
# ---------------------------------------------------------------------------


def render_markdown(
    *,
    run_id: str,
    account_id: str,
    scorecard: dict[str, int],
    executive_summary: list[str],
    top_findings: list[AnalyzedFinding],
    all_findings: list[Finding],
    metadata: dict[str, Any],
    previous_scorecard: dict[str, int] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Render the briefing as both markdown and a structured JSON payload."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines: list[str] = []
    lines.append(f"# Cloud Health Briefing — {account_id} — {today}")
    lines.append("")
    lines.append("## Executive Summary")
    for bullet in executive_summary:
        lines.append(f"- {bullet}")
    lines.append("")

    lines.append("## KRA Scorecard")
    lines.append("| KRA | Score | Δ vs prev run |")
    lines.append("| --- | ----- | ------------- |")
    for kra in (*KRAS, "overall"):
        score = scorecard.get(kra, 0)
        delta = ""
        if previous_scorecard and kra in previous_scorecard:
            delta_val = score - previous_scorecard[kra]
            delta = f"{delta_val:+d}"
        lines.append(f"| {kra} | {score} | {delta} |")
    lines.append("")

    lines.append("## Top 10 Findings")
    if not top_findings:
        lines.append("_No findings — account is clean._")
    else:
        lines.append(
            "| # | Severity | KRA | Resource | Title | Recommendation |"
        )
        lines.append("| - | -------- | --- | -------- | ----- | -------------- |")
        for i, a in enumerate(top_findings, start=1):
            f = a.finding
            lines.append(
                "| {i} | {sev} | {kra} | `{arn}` | {title} | {rec} |".format(
                    i=i,
                    sev=_severity_badge(f.severity),
                    kra=f.kra,
                    arn=_truncate(f.resource_arn, 60),
                    title=_md_escape(f.title),
                    rec=_md_escape(_truncate(f.recommendation, 140)),
                )
            )
    lines.append("")

    lines.append("## All Findings")
    lines.append("<details><summary>Show all findings ({n})</summary>".format(n=len(all_findings)))
    lines.append("")
    lines.append("| Detector | Severity | KRA | Region | Resource |")
    lines.append("| -------- | -------- | --- | ------ | -------- |")
    for f in all_findings:
        lines.append(
            "| {d} | {s} | {k} | {r} | `{arn}` |".format(
                d=f.detector_id,
                s=_severity_badge(f.severity),
                k=f.kra,
                r=f.region,
                arn=_truncate(f.resource_arn, 80),
            )
        )
    lines.append("")
    lines.append("</details>")
    lines.append("")

    lines.append("## Run Metadata")
    lines.append(f"- run_id: `{run_id}`")
    lines.append(f"- regions scanned: {', '.join(metadata.get('regions', [])) or 'n/a'}")
    lines.append(f"- resources inventoried: {metadata.get('inventory_count', len(all_findings))}")
    lines.append(f"- errors: {len(metadata.get('errors', []))}")
    lines.append(f"- generated_at: {metadata.get('generated_at', today)}")

    markdown = "\n".join(lines)

    payload = BriefingPayload(
        run_id=run_id,
        account_id=account_id,
        generated_at=datetime.now(timezone.utc),
        scorecard=Scorecard(
            cost=scorecard.get("cost", 0),
            security=scorecard.get("security", 0),
            compliance=scorecard.get("compliance", 0),
            performance=scorecard.get("performance", 0),
            reliability=scorecard.get("reliability", 0),
            overall=scorecard.get("overall", 0),
        ),
        executive_summary=list(executive_summary),
        top_findings=list(top_findings),
        all_findings=list(all_findings),
        metadata=metadata,
    )
    return markdown, json.loads(payload.model_dump_json())


_SEVERITY_BADGES = {
    "critical": "🟥 critical",
    "high": "🟧 high",
    "medium": "🟨 medium",
    "low": "🟩 low",
    "info": "ℹ️ info",
}


def _severity_badge(sev: str) -> str:
    return _SEVERITY_BADGES.get(sev, sev)


def _md_escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _truncate(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    return text[: n - 1] + "…"
