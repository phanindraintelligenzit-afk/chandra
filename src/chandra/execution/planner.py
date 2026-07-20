"""Structured-JSON planning with self-correction.

The reasoning engine (Claude on Bedrock today, a local vLLM model
tomorrow) is asked to return an :class:`ExecutionPlan` as JSON — never
prose, never markdown. Its output is run through the M1 validator; on
rejection the errors are fed back and the model is asked to correct
itself, up to ``max_attempts`` times. If it still cannot produce a valid
plan, a deterministic guidance-only plan is returned so the workflow
degrades safely instead of executing garbage.

This is the seam between "AI proposes" and "deterministic system
executes": the only thing that leaves this module is a *validated*
``ExecutionPlan`` (or an explicit non-actionable fallback).
"""

from __future__ import annotations

import json

from pydantic import BaseModel
from src.chandra.execution.schemas import ExecutionPlan, ExecutionStep, NoopAction
from src.chandra.execution.validator import PlanValidationResult, validate_plan
from src.chandra.execution.verification import IntentVerification, verify_intent_matches_plan
from src.chandra.llm.providers import BaseLLM, get_provider
from src.chandra.logging import get_logger

logger = get_logger(__name__)

_PLAN_EXAMPLE = {
    "intent": "Block public access on S3 bucket acme-logs",
    "kra_code": "security",
    "dry_run": True,
    "steps": [
        {
            "order": 1,
            "requires_approval": True,
            "rationale": "close public exposure",
            "action": {
                "kind": "aws_api",
                "service": "s3",
                "operation": "put_public_access_block",
                "resource_id": "acme-logs",
                "parameters": {"Bucket": "acme-logs"},
            },
        }
    ],
}

SYSTEM_PROMPT = f"""You are Chandra's cloud-operations planner.
You DO NOT execute anything. You ONLY output an execution plan as JSON.

Hard rules:
- Output ONLY a single JSON object. No prose, no markdown, no code fences, no explanation.
- Every step's action.kind is one of: "aws_api", "terraform", "kubernetes", "noop".
- For "aws_api": provide service (boto3 name), operation (boto3 method), parameters
  (a JSON object of named arguments), and resource_id. Never a shell/CLI string.
- Never put shell commands, ';', '&&', '|', or 'rm -rf' anywhere.
- If the request is advisory or you are unsure, return a single "noop" step with a note.
- Do not invent resources that were not mentioned in the request/context.

The JSON MUST match this shape exactly (extra fields are rejected):
{json.dumps(_PLAN_EXAMPLE, indent=2)}
"""


class PlanGenerationResult(BaseModel):
    valid: bool
    plan: ExecutionPlan
    attempts: int
    errors: list[str] = []
    warnings: list[str] = []
    verification: IntentVerification | None = None
    generated_by: str = "llm"


def _fallback_plan(intent: str, kra_code: str | None, errors: list[str]) -> ExecutionPlan:
    """A safe, non-actionable plan used when the model can't produce valid JSON."""
    return ExecutionPlan(
        intent=intent,
        kra_code=kra_code,
        generated_by="deterministic",
        dry_run=True,
        steps=[
            ExecutionStep(
                order=1,
                requires_approval=True,
                rationale="model did not return a valid execution plan; hand off to engineer",
                action=NoopAction(note="; ".join(errors)[:500] or "no valid plan produced"),
            )
        ],
    )


def _user_prompt(intent: str, context: str, prior_errors: list[str]) -> str:
    parts = [f"Request intent:\n{intent}", f"Context:\n{context or '(none)'}"]
    if prior_errors:
        parts.append(
            "Your previous output was REJECTED for these reasons:\n"
            + "\n".join(f"- {e}" for e in prior_errors)
            + "\nReturn a corrected JSON object only."
        )
    return "\n\n".join(parts)


def generate_execution_plan(
    intent: str,
    context: str = "",
    *,
    kra_code: str | None = None,
    llm: BaseLLM | None = None,
    max_attempts: int = 3,
) -> PlanGenerationResult:
    """Ask the model for a validated ExecutionPlan, self-correcting on failure."""
    provider = llm or get_provider()
    prior_errors: list[str] = []
    last_result: PlanValidationResult | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            raw = provider.complete(SYSTEM_PROMPT, _user_prompt(intent, context, prior_errors))
        except Exception as exc:  # provider failure → fall back deterministically
            logger.warning("planner.provider_failed", attempt=attempt, error=str(exc))
            break

        result = validate_plan(raw)
        last_result = result
        if result.valid and result.plan is not None:
            verification = verify_intent_matches_plan(intent, result.plan)
            if not verification.passed:
                # A schema-valid but off-intent plan is also a correction case.
                prior_errors = verification.reasons
                logger.warning("planner.intent_rejected", attempt=attempt)
                continue
            logger.info("planner.plan_ready", attempt=attempt, steps=len(result.plan.steps))
            return PlanGenerationResult(
                valid=True,
                plan=result.plan,
                attempts=attempt,
                warnings=result.warnings + verification.scope_warnings,
                verification=verification,
            )
        prior_errors = result.errors
        logger.warning("planner.schema_rejected", attempt=attempt, errors=len(result.errors))

    errors = last_result.errors if last_result else ["provider unavailable"]
    logger.warning("planner.exhausted_fallback", attempts=max_attempts, errors=len(errors))
    return PlanGenerationResult(
        valid=False,
        plan=_fallback_plan(intent, kra_code, errors),
        attempts=max_attempts,
        errors=errors,
        generated_by="deterministic",
    )
