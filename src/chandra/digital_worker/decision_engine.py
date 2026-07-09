"""Automation Decision Engine.

Evaluates request context, risk, and policy to dynamically determine
whether an operation should be automatically executed, await human
approval, or just produce engineer guidance.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError
from src.chandra.config import settings
from src.chandra.digital_worker.schemas import (
    CloudRequest,
    DecisionMode,
    ExecutionDecision,
    RequestClassification,
    ResolutionPlan,
    RiskAssessment,
)
from src.chandra.logging import get_logger
from src.chandra.observability.callbacks import UsageCapture

logger = get_logger(__name__)

POLICY_PROMPT = """You are an Enterprise Cloud Security Policy Engine.
Your job is to evaluate a proposed cloud operation and decide its execution mode.

Inputs provided in JSON:
- request: The user's original request.
- classification: The category, platform, and priority.
- risk: Risk level and score.
- plan: The step-by-step resolution plan.

Execution Modes:
1. "auto_execute": Use for low-risk, non-destructive, reversible changes (e.g., adding tags, rotating a key, closing an open security group on non-prod).
2. "await_approval": Use for medium/high risk, changes to production, or any potentially destructive action (delete, terminate, drop).
3. "engineer_guidance": Use only if the request is highly ambiguous, impossible to automate, or explicitly asks for advice rather than action.

Rules:
- NEVER auto-execute a deletion of a stateful resource (e.g., S3 bucket, database, volume) unless explicitly safe and non-production.
- ALWAYS require approval for 'prod' or 'production' environments.
- Prioritize action over guidance. If a plan exists, try to execute or approve it.

Output JSON ONLY with this schema:
{
    "mode": "auto_execute" | "await_approval" | "engineer_guidance",
    "reason": "Clear, concise justification referencing policy rules."
}
"""


def evaluate_decision(
    request: CloudRequest,
    classification: RequestClassification,
    plan: ResolutionPlan,
    risk: RiskAssessment,
) -> ExecutionDecision:
    """Dynamically decide execution mode. 
    (LLM policy engine bypassed; ambiguity is handled via HITL in the execution agent)"""
    # Fallback to deterministic gate if automation is completely absent
    if not plan.automation_available:
        return ExecutionDecision(
            mode=DecisionMode.ENGINEER_GUIDANCE,
            reason="No automation plan generated; producing engineer guidance.",
        )

    # Use deterministic fallback directly. Risk score triggers approval gate,
    # otherwise we auto_execute (which then hits the HITL logic if ambiguous).
    return _deterministic_fallback(classification, risk)


def _deterministic_fallback(classification: RequestClassification, risk: RiskAssessment) -> ExecutionDecision:
    """Deterministic fallback if Bedrock is down."""
    if not risk.requires_approval:
        return ExecutionDecision(
            mode=DecisionMode.AUTO_EXECUTE,
            reason=f"Fallback: Risk {risk.level.value} needs no approval."
        )
    return ExecutionDecision(
        mode=DecisionMode.AWAIT_APPROVAL,
        reason=f"Fallback: Risk {risk.level.value} requires human approval."
    )
