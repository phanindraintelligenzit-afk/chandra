"""Structured execution safety layer.

The gate that makes local-LLM output safe to act on: a model *proposes* a
strictly-typed :class:`ExecutionPlan`; :func:`validate_plan` rejects
anything malformed or unsafe; :func:`validate_terraform` proves generated
HCL before apply; :func:`verify_intent_matches_plan` confirms the plan
matches what was actually requested. No executor consumes raw model text.
"""

from src.chandra.execution.executor import ExecutionResult, StepResult, execute_plan
from src.chandra.execution.planner import (
    PlanGenerationResult,
    generate_execution_plan,
)
from src.chandra.execution.schemas import (
    ActionKind,
    AwsApiAction,
    ExecutionPlan,
    ExecutionStep,
    KubernetesAction,
    NoopAction,
    TerraformAction,
)
from src.chandra.execution.terraform import (
    TerraformValidation,
    terraform_available,
    validate_terraform,
)
from src.chandra.execution.validator import (
    ALLOWED_AWS_SERVICES,
    PlanValidationResult,
    validate_plan,
)
from src.chandra.execution.verification import IntentVerification, verify_intent_matches_plan

__all__ = [
    "ALLOWED_AWS_SERVICES",
    "ActionKind",
    "AwsApiAction",
    "ExecutionPlan",
    "ExecutionResult",
    "ExecutionStep",
    "IntentVerification",
    "KubernetesAction",
    "NoopAction",
    "PlanGenerationResult",
    "PlanValidationResult",
    "StepResult",
    "TerraformAction",
    "TerraformValidation",
    "execute_plan",
    "generate_execution_plan",
    "terraform_available",
    "validate_plan",
    "validate_terraform",
    "verify_intent_matches_plan",
]
