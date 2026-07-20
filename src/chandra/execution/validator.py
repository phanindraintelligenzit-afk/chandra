"""Plan validator — the gate between a model and any executor.

``validate_plan`` takes whatever a model returned (a JSON string or an
already-parsed dict) and either produces a strictly-typed
:class:`ExecutionPlan` or a list of concrete errors. Nothing downstream
runs unless this returns ``valid=True``.

Layers of defence, in order:

1. **Parse** — tolerant JSON parse (``json_repair`` when available) so a
   model that wraps JSON in prose or trailing commas still gets a fair
   chance; a total parse failure is a hard reject.
2. **Schema** — Pydantic validation against ``ExecutionPlan``
   (``extra="forbid"``): unknown fields, wrong types, or unknown action
   kinds are rejected. This is the JSON-Schema-validation requirement.
3. **Safety / hallucination checks** — deterministic rules on top of the
   schema: AWS service allow-list, no shell metacharacters smuggled into
   string parameters, non-empty actionable plan, resource-id presence for
   mutating calls. These catch *well-formed but wrong* output.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ValidationError
from src.chandra.execution.schemas import ActionKind, AwsApiAction, ExecutionPlan
from src.chandra.logging import get_logger

logger = get_logger(__name__)

#: boto3 services an AWS-API step is allowed to target. A call to a service
#: outside this set is treated as a hallucination and rejected. Extend as
#: real KRAs need more surface — deliberately conservative by default.
ALLOWED_AWS_SERVICES: frozenset[str] = frozenset(
    {
        "s3",
        "ec2",
        "iam",
        "rds",
        "kms",
        "cloudwatch",
        "cloudtrail",
        "config",
        "sns",
        "sqs",
        "lambda",
        "dynamodb",
        "autoscaling",
        "elbv2",
        "backup",
        "resourcegroupstaggingapi",
        "ce",
        "compute-optimizer",
        "guardduty",
        "securityhub",
        "accessanalyzer",
    }
)

#: Shell/command-injection metacharacters that must never appear in a
#: structured parameter value — their presence means the model tried to
#: smuggle a command through a data field.
_SHELL_INJECTION = re.compile(r"[;&|`$><]|\$\(|\brm\s+-rf\b|\bsudo\b", re.IGNORECASE)


class PlanValidationResult(BaseModel):
    valid: bool
    plan: ExecutionPlan | None = None
    errors: list[str] = []
    warnings: list[str] = []


def _tolerant_parse(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    try:
        import json_repair  # noqa: PLC0415  # optional dep; best-effort recovery

        return json_repair.loads(raw)
    except Exception:  # parse recovery is best-effort by design
        return None


def _scan_for_injection(value: Any, path: str, errors: list[str]) -> None:
    if isinstance(value, str):
        if _SHELL_INJECTION.search(value):
            errors.append(f"shell metacharacters in parameter {path!r}: {value!r}")
    elif isinstance(value, dict):
        for key, item in value.items():
            _scan_for_injection(item, f"{path}.{key}", errors)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            _scan_for_injection(item, f"{path}[{i}]", errors)


def _safety_checks(plan: ExecutionPlan) -> tuple[list[str], list[str]]:
    """Deterministic checks on a schema-valid plan."""
    errors: list[str] = []
    warnings: list[str] = []

    actionable = [s for s in plan.steps if s.action.kind is not ActionKind.NOOP]
    if not actionable:
        warnings.append("plan has no actionable steps (guidance-only)")

    for step in plan.steps:
        action = step.action
        if isinstance(action, AwsApiAction):
            if action.service not in ALLOWED_AWS_SERVICES:
                errors.append(
                    f"step {step.order}: AWS service {action.service!r} not in the allow-list "
                    "(possible hallucination)"
                )
            if not action.operation or " " in action.operation:
                errors.append(f"step {step.order}: invalid boto3 operation {action.operation!r}")
            _scan_for_injection(action.parameters, f"step{step.order}.parameters", errors)
            # A mutating call with no resource target is a red flag for
            # scope; warn (some account-level ops legitimately have none).
            if not action.resource_id and not action.parameters:
                warnings.append(
                    f"step {step.order}: {action.service}.{action.operation} has no "
                    "resource_id and no parameters"
                )
    return errors, warnings


def validate_plan(raw: str | dict[str, Any] | ExecutionPlan) -> PlanValidationResult:
    """Turn raw model output into a validated plan, or explain why not."""
    if isinstance(raw, ExecutionPlan):
        parsed: Any = raw.model_dump(mode="json")
    elif isinstance(raw, str):
        parsed = _tolerant_parse(raw)
        if not isinstance(parsed, dict):
            return PlanValidationResult(
                valid=False, errors=["response is not parseable JSON into a plan object"]
            )
    else:
        parsed = raw

    if not isinstance(parsed, dict):
        return PlanValidationResult(
            valid=False, errors=[f"plan must be a JSON object, got {type(parsed).__name__}"]
        )

    try:
        plan = ExecutionPlan.model_validate(parsed)
    except ValidationError as exc:
        errors = [f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()]
        logger.warning("execution.plan_schema_invalid", errors=len(errors))
        return PlanValidationResult(valid=False, errors=errors)

    errors, warnings = _safety_checks(plan)
    if errors:
        logger.warning("execution.plan_safety_rejected", errors=len(errors))
        return PlanValidationResult(valid=False, plan=None, errors=errors, warnings=warnings)

    logger.info(
        "execution.plan_validated",
        plan_id=plan.plan_id,
        steps=len(plan.steps),
        warnings=len(warnings),
    )
    return PlanValidationResult(valid=True, plan=plan, warnings=warnings)
