"""Intent-vs-plan verification.

A deterministic Verification Agent: given the original request intent and
a validated :class:`ExecutionPlan`, check that the plan actually addresses
the intent and does not silently exceed it. This is the "compare requested
intent with generated actions" guard — it runs *after* schema/safety
validation and is the last gate before human approval / execution.

Deliberately rule-based (no LLM): the whole point is to catch a
hallucinating model, so the checker must not itself depend on one.
"""

from __future__ import annotations

import re

from pydantic import BaseModel
from src.chandra.execution.schemas import ActionKind, AwsApiAction, ExecutionPlan
from src.chandra.logging import get_logger

logger = get_logger(__name__)

_WORD = re.compile(r"[a-z0-9][a-z0-9\-_]*")

#: Verbs that imply a destructive/irreversible action; if a step performs
#: one but the intent never asked for it, that is a scope violation.
_DESTRUCTIVE = frozenset(
    {"delete", "terminate", "destroy", "remove", "revoke", "drop", "purge", "release"}
)


class IntentVerification(BaseModel):
    passed: bool
    reasons: list[str] = []
    scope_warnings: list[str] = []


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def verify_intent_matches_plan(intent: str, plan: ExecutionPlan) -> IntentVerification:
    """Check a validated plan against the request intent."""
    reasons: list[str] = []
    scope_warnings: list[str] = []
    request_tokens = _tokens(intent)
    # Destructive-intent check may consider the plan's own restatement;
    # scope check uses ONLY the request (the plan's intent could itself
    # be hallucinated).
    intent_tokens = request_tokens | _tokens(plan.intent)

    actionable = [s for s in plan.steps if s.action.kind is not ActionKind.NOOP]
    if not actionable:
        # Guidance-only plans are allowed but must be recognized as such.
        logger.info("execution.intent_guidance_only", plan_id=plan.plan_id)
        return IntentVerification(passed=True, reasons=["plan is advisory (no actions)"])

    # 1. Every mutating AWS step should reference a resource that the intent
    #    (or the plan intent) actually mentions — a plan touching resources
    #    nobody asked about is a hallucination signal.
    for step in actionable:
        action = step.action
        if isinstance(action, AwsApiAction) and action.resource_id:
            rid_tokens = _tokens(action.resource_id)
            if rid_tokens and not (rid_tokens & request_tokens):
                scope_warnings.append(
                    f"step {step.order}: targets {action.resource_id!r}, "
                    "not referenced in the request intent"
                )

    # 2. Destructive operations must be justified by destructive intent.
    intent_is_destructive = bool(intent_tokens & _DESTRUCTIVE)
    for step in actionable:
        action = step.action
        op = action.operation.lower() if isinstance(action, AwsApiAction) else ""
        step_is_destructive = any(v in op for v in _DESTRUCTIVE) or any(
            v in step.rationale.lower() for v in _DESTRUCTIVE
        )
        if step_is_destructive and not intent_is_destructive:
            reasons.append(
                f"step {step.order}: destructive operation {op or 'action'!r} "
                "but the request did not ask for a destructive change"
            )

    passed = not reasons
    logger.info(
        "execution.intent_verified",
        plan_id=plan.plan_id,
        passed=passed,
        scope_warnings=len(scope_warnings),
    )
    return IntentVerification(passed=passed, reasons=reasons, scope_warnings=scope_warnings)
