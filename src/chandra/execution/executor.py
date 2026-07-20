"""Deterministic executor — the ONLY thing that touches the cloud.

A model proposes; this module disposes. It consumes a *validated*
:class:`ExecutionPlan` (never raw model text) and performs each typed step
through the sanctioned client factory. There is no string parsing, no
``eval``, no shell: an :class:`AwsApiAction` becomes exactly
``getattr(client, operation)(**parameters)`` and nothing else.

Safety invariants enforced here (defence in depth on top of the validator):

* The plan is re-validated before anything runs — a plan that would not
  pass :func:`validate_plan` is refused outright.
* ``dry_run`` (default, and forced on unless the caller *and* the plan
  both opt out) short-circuits every mutating call: the intended call is
  recorded, not issued.
* Terraform HCL is re-validated immediately before apply; apply itself is
  deliberately not wired to a live backend yet.
* AWS clients come only from :func:`get_default_factory` — never
  ``boto3.client`` directly.

This is the module ``aws_execution_agent`` should delegate to instead of
interpreting model output itself.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field
from src.chandra.aws.client_factory import AwsClientFactory, get_default_factory
from src.chandra.execution.schemas import (
    ActionKind,
    AwsApiAction,
    ExecutionPlan,
    ExecutionStep,
    KubernetesAction,
    NoopAction,
    TerraformAction,
)
from src.chandra.execution.terraform import validate_terraform
from src.chandra.execution.validator import validate_plan
from src.chandra.logging import get_logger

logger = get_logger(__name__)

StepStatus = Literal["executed", "dry_run", "skipped", "failed", "rejected", "unsupported"]


class StepResult(BaseModel):
    order: int
    kind: ActionKind
    status: StepStatus
    detail: str = ""
    #: For AWS calls: the boto3 response (JSON-safe subset) or the intended
    #: call recorded during a dry run. Never contains secrets.
    output: dict[str, Any] = Field(default_factory=dict)


class ExecutionResult(BaseModel):
    plan_id: str
    executed: bool
    dry_run: bool
    steps: list[StepResult] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.executed and not self.errors


def _jsonsafe(value: Any) -> Any:
    """Best-effort reduce a boto3 response to something serialisable."""
    if isinstance(value, dict):
        return {str(k): _jsonsafe(v) for k, v in value.items() if k != "ResponseMetadata"}
    if isinstance(value, (list, tuple)):
        return [_jsonsafe(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _run_aws_step(
    step: ExecutionStep,
    action: AwsApiAction,
    factory: AwsClientFactory,
    *,
    dry_run: bool,
) -> StepResult:
    intended = {
        "service": action.service,
        "operation": action.operation,
        "parameters": action.parameters,
        "region": action.region,
        "resource_id": action.resource_id,
    }
    if dry_run:
        return StepResult(
            order=step.order,
            kind=ActionKind.AWS_API,
            status="dry_run",
            detail=f"would call {action.service}.{action.operation}",
            output={"intended_call": intended},
        )

    client = factory.client(action.service, region=action.region)
    method = getattr(client, action.operation, None)
    if not callable(method):
        return StepResult(
            order=step.order,
            kind=ActionKind.AWS_API,
            status="failed",
            detail=f"boto3 client for {action.service!r} has no operation {action.operation!r}",
        )
    try:
        response = method(**action.parameters)
    except Exception as exc:  # boundary: one bad step must not crash the run
        logger.warning(
            "executor.aws_step_failed",
            order=step.order,
            service=action.service,
            operation=action.operation,
            error=str(exc),
        )
        return StepResult(
            order=step.order,
            kind=ActionKind.AWS_API,
            status="failed",
            detail=f"{type(exc).__name__}: {exc}",
        )
    logger.info(
        "executor.aws_step_executed",
        order=step.order,
        service=action.service,
        operation=action.operation,
    )
    return StepResult(
        order=step.order,
        kind=ActionKind.AWS_API,
        status="executed",
        detail=f"called {action.service}.{action.operation}",
        output=_jsonsafe(response) if isinstance(response, dict) else {},
    )


def _run_terraform_step(
    step: ExecutionStep, action: TerraformAction, *, dry_run: bool
) -> StepResult:
    validation = validate_terraform(action.hcl, run_plan=not dry_run)
    if validation.status == "invalid":
        return StepResult(
            order=step.order,
            kind=ActionKind.TERRAFORM,
            status="rejected",
            detail=f"terraform validation failed: {validation.detail}",
        )
    if validation.status == "unavailable":
        return StepResult(
            order=step.order,
            kind=ActionKind.TERRAFORM,
            status="skipped",
            detail="terraform binary unavailable; HCL could not be validated before apply",
        )
    # Valid HCL. Applying to a live backend is intentionally not wired here:
    # dry_run records the validated intent; a real apply is a separate,
    # backend-scoped concern handled by the IaC pipeline, not this agent.
    return StepResult(
        order=step.order,
        kind=ActionKind.TERRAFORM,
        status="dry_run" if dry_run else "skipped",
        detail="terraform HCL validated"
        + ("" if dry_run else "; live apply is delegated to the IaC pipeline"),
        output={"stages": [s.name for s in validation.stages]},
    )


def execute_plan(
    plan: ExecutionPlan | Any,
    *,
    factory: AwsClientFactory | None = None,
    force_execute: bool = False,
) -> ExecutionResult:
    """Execute a validated plan, step by step, deterministically.

    ``dry_run`` is the safe default: a real (mutating) call is issued only
    when the plan itself sets ``dry_run=False`` *and* the caller passes
    ``force_execute=True``. Either one left at its default keeps the run in
    dry-run mode — a fail-safe, not a fail-open, gate.
    """
    # Defence in depth: never trust that the caller validated. Re-validate.
    checked = validate_plan(plan)
    if not checked.valid or checked.plan is None:
        logger.warning("executor.refused_invalid_plan", errors=len(checked.errors))
        plan_id = plan.plan_id if isinstance(plan, ExecutionPlan) else "unknown"
        return ExecutionResult(
            plan_id=plan_id,
            executed=False,
            dry_run=True,
            errors=["plan failed validation; refusing to execute", *checked.errors],
        )

    valid_plan = checked.plan
    dry_run = valid_plan.dry_run or not force_execute
    factory = factory or get_default_factory()
    results: list[StepResult] = []
    errors: list[str] = []

    for step in sorted(valid_plan.steps, key=lambda s: s.order):
        action = step.action
        if isinstance(action, NoopAction):
            results.append(
                StepResult(
                    order=step.order,
                    kind=ActionKind.NOOP,
                    status="skipped",
                    detail=action.note or "no-op",
                )
            )
        elif isinstance(action, AwsApiAction):
            result = _run_aws_step(step, action, factory, dry_run=dry_run)
            results.append(result)
            if result.status == "failed":
                errors.append(f"step {step.order}: {result.detail}")
        elif isinstance(action, TerraformAction):
            result = _run_terraform_step(step, action, dry_run=dry_run)
            results.append(result)
            if result.status == "rejected":
                errors.append(f"step {step.order}: {result.detail}")
        elif isinstance(action, KubernetesAction):
            results.append(
                StepResult(
                    order=step.order,
                    kind=ActionKind.KUBERNETES,
                    status="unsupported",
                    detail="kubernetes apply not wired to a cluster in this runtime",
                )
            )

    logger.info(
        "executor.plan_complete",
        plan_id=valid_plan.plan_id,
        dry_run=dry_run,
        steps=len(results),
        errors=len(errors),
    )
    return ExecutionResult(
        plan_id=valid_plan.plan_id,
        executed=True,
        dry_run=dry_run,
        steps=results,
        errors=errors,
    )
