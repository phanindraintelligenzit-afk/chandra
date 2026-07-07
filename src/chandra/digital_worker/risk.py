"""Deterministic risk analysis for a proposed resolution plan.

Mirrors the platform invariant that everything between the LLM-powered
planning step and execution is rule-based: the same plan for the same
request always yields the same risk verdict and the same approval gate.

Approval policy (Proposedflow §4): P1 requests always require human
approval; otherwise approval is driven by the computed risk level.
"""

from __future__ import annotations

import re

from src.chandra.digital_worker.schemas import (
    CloudPlatform,
    RequestCategory,
    RequestClassification,
    RequestPriority,
    ResolutionPlan,
    RiskAssessment,
    RiskLevel,
)
from src.chandra.logging import get_logger

logger = get_logger(__name__)

# Verbs that mutate or destroy infrastructure, with their risk weight.
_DESTRUCTIVE_PATTERNS: tuple[tuple[re.Pattern[str], int, str], ...] = (
    (
        re.compile(r"\b(delete|destroy|terminate|drop|purge|wipe)\b", re.I),
        35,
        "destructive operation",
    ),
    (re.compile(r"\b(revoke|disable|detach|deregister)\b", re.I), 20, "access/state removal"),
    (
        re.compile(r"\b(modify|update|patch|resize|reboot|restart|rotate)\b", re.I),
        12,
        "mutating operation",
    ),
    (re.compile(r"\b(create|provision|deploy|enable|attach|tag)\b", re.I), 6, "additive operation"),
)

_IRREVERSIBLE = re.compile(r"\b(delete|destroy|terminate|drop|purge|wipe)\b", re.I)
_PRODUCTION = re.compile(r"\b(prod|production|live)\b", re.I)

_CATEGORY_BASE: dict[RequestCategory, int] = {
    RequestCategory.INCIDENT: 20,
    RequestCategory.SECURITY: 20,
    RequestCategory.CHANGE_REQUEST: 15,
    RequestCategory.COMPLIANCE: 10,
    RequestCategory.RELIABILITY: 10,
    RequestCategory.PERFORMANCE: 8,
    RequestCategory.COST_OPTIMIZATION: 5,
    RequestCategory.SERVICE_REQUEST: 5,
    RequestCategory.QUESTION: 0,
    RequestCategory.UNKNOWN: 15,
}


def assess_risk(classification: RequestClassification, plan: ResolutionPlan) -> RiskAssessment:
    """Score the plan 0-100 and derive level, reversibility and approval need."""
    factors: list[str] = []
    score = _CATEGORY_BASE.get(classification.category, 15)
    factors.append(f"category:{classification.category.value}(+{score})")

    plan_text = " ".join(f"{step.action} {step.detail} {step.command or ''}" for step in plan.steps)

    reversible = True
    for pattern, weight, label in _DESTRUCTIVE_PATTERNS:
        if pattern.search(plan_text):
            score += weight
            factors.append(f"{label}(+{weight})")
    if _IRREVERSIBLE.search(plan_text):
        reversible = False
        factors.append("irreversible:no-undo")
    if plan.rollback_steps:
        score -= 10
        reversible = True
        factors.append("rollback-plan(-10)")

    if _PRODUCTION.search(plan_text) or _PRODUCTION.search(classification.summary):
        score += 15
        factors.append("production-target(+15)")

    if classification.platform is CloudPlatform.UNKNOWN:
        score += 10
        factors.append("platform-unknown(+10)")

    if classification.priority is RequestPriority.P1:
        score += 10
        factors.append("priority-p1(+10)")

    if not plan.automation_available:
        # No registered automation → nothing executes, so intrinsic risk is low.
        factors.append("no-automation-path")

    score = max(0, min(100, score))
    level = _level_for(score)
    requires_approval = (
        classification.priority is RequestPriority.P1
        or level in (RiskLevel.HIGH, RiskLevel.CRITICAL)
        or not reversible
    )

    assessment = RiskAssessment(
        level=level,
        score=score,
        factors=factors,
        reversible=reversible,
        requires_approval=requires_approval,
    )
    logger.info(
        "risk.assessed",
        plan_id=plan.plan_id,
        score=score,
        level=level.value,
        requires_approval=requires_approval,
        reversible=reversible,
    )
    return assessment


def _level_for(score: int) -> RiskLevel:
    if score >= 75:
        return RiskLevel.CRITICAL
    if score >= 50:
        return RiskLevel.HIGH
    if score >= 25:
        return RiskLevel.MEDIUM
    return RiskLevel.LOW
