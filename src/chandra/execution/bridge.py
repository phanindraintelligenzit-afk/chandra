"""Typed request → execution bridge — the Jira/Slack/…→AWS seam.

One function, :func:`plan_and_execute`, wires the whole safe pipeline that
replaces "ask a model for text, then run the text":

    intent (+ RAG context)
        → generate_execution_plan()      # provider-abstracted, self-correcting
        → validated ExecutionPlan         # schema + safety + intent verified
        → execute_plan()                  # deterministic, typed, dry-run default

The reasoning backend (Claude/Bedrock or local vLLM) is chosen entirely by
``LLM_PROVIDER`` inside :func:`generate_execution_plan`; this module never
names a provider. It is the single call the AWS Execution Agent (and the
Digital Worker graph) uses so that **the only thing that ever reaches an
executor is a validated ExecutionPlan** — never free-form model output.

Approval + dry-run contract:
* ``approved=False``  → nothing mutates; the validated plan is returned for
  the human approval gate (this is the default).
* ``approved=True, dry_run=True`` → executor runs in dry-run (records
  intended calls) — used to preview an approved action.
* ``approved=True, dry_run=False`` → real, mutating execution. Both flags
  must be set explicitly; either left at its default keeps it safe.
"""

from __future__ import annotations

from pydantic import BaseModel
from src.chandra.aws.client_factory import AwsClientFactory
from src.chandra.execution.executor import ExecutionResult, execute_plan
from src.chandra.execution.planner import PlanGenerationResult, generate_execution_plan
from src.chandra.execution.schemas import ExecutionPlan
from src.chandra.llm.providers import BaseLLM
from src.chandra.logging import get_logger

logger = get_logger(__name__)


class BridgeResult(BaseModel):
    """Outcome of one request through the typed pipeline."""

    intent: str
    planned: bool  # a validated, actionable plan was produced by the model
    executed: bool  # the executor ran (dry-run counts as executed)
    dry_run: bool
    plan: ExecutionPlan
    generation: PlanGenerationResult
    execution: ExecutionResult | None = None

    @property
    def ok(self) -> bool:
        """True when a valid plan ran without executor errors."""
        return self.planned and self.execution is not None and self.execution.ok


def plan_and_execute(
    intent: str,
    context: str = "",
    *,
    kra_code: str | None = None,
    approved: bool = False,
    dry_run: bool = True,
    llm: BaseLLM | None = None,
    factory: AwsClientFactory | None = None,
    max_attempts: int = 3,
) -> BridgeResult:
    """Plan an action with the configured provider and execute it safely."""
    generation = generate_execution_plan(
        intent, context, kra_code=kra_code, llm=llm, max_attempts=max_attempts
    )
    plan = generation.plan

    if not generation.valid:
        # No valid/actionable plan (model exhausted self-correction → no-op).
        # Never execute; hand the guidance plan back for engineer follow-up.
        logger.warning("bridge.no_valid_plan", intent=intent[:120], attempts=generation.attempts)
        return BridgeResult(
            intent=intent,
            planned=False,
            executed=False,
            dry_run=True,
            plan=plan,
            generation=generation,
        )

    if not approved:
        # Validated plan awaiting the human approval gate — do not execute.
        logger.info("bridge.plan_awaiting_approval", plan_id=plan.plan_id, steps=len(plan.steps))
        return BridgeResult(
            intent=intent,
            planned=True,
            executed=False,
            dry_run=True,
            plan=plan,
            generation=generation,
        )

    # Approved. A real mutation requires dry_run=False here AND on the plan;
    # otherwise the executor stays in dry-run (fail-safe).
    effective = plan if dry_run else plan.model_copy(update={"dry_run": False})
    result = execute_plan(effective, factory=factory, force_execute=not dry_run)
    logger.info(
        "bridge.executed",
        plan_id=plan.plan_id,
        dry_run=result.dry_run,
        ok=result.ok,
        errors=len(result.errors),
    )
    return BridgeResult(
        intent=intent,
        planned=True,
        executed=True,
        dry_run=result.dry_run,
        plan=effective,
        generation=generation,
        execution=result,
    )
