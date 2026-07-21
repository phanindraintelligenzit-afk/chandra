"""Structured execution-plan contract.

The core anti-hallucination principle: a local model may *propose* a plan,
but the plan is a strictly-typed object — never free-form text, never a
shell string. Every step is one of a small allow-list of typed actions
(AWS API call with named parameters, Terraform HCL, Kubernetes manifest,
or noop). ``extra="forbid"`` means a model that invents extra fields is
rejected at the door rather than silently executed.

Downstream (``execution.validator`` + the executor) only ever consume an
``ExecutionPlan`` — so ``aws_execution_agent`` can be refactored to accept
one of these instead of raw model output, which is the "never execute raw
LLM output" requirement.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def _new_id() -> str:
    return str(uuid4())


class ActionKind(StrEnum):
    """Allow-listed step kinds. Anything else is a rejected plan."""

    AWS_API = "aws_api"
    TERRAFORM = "terraform"
    KUBERNETES = "kubernetes"
    NOOP = "noop"


class AwsApiAction(BaseModel):
    """A single boto3-style API call with *named* parameters.

    Deliberately not a CLI string: ``service`` + ``operation`` map to a
    boto3 client method, and ``parameters`` is a JSON object. There is no
    place to smuggle ``; rm -rf`` — the executor calls
    ``client.<operation>(**parameters)``, nothing else.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal[ActionKind.AWS_API] = ActionKind.AWS_API
    service: str = Field(description="boto3 service name, e.g. 's3', 'ec2', 'rds'.")
    operation: str = Field(description="boto3 client method, e.g. 'put_public_access_block'.")
    parameters: dict[str, Any] = Field(default_factory=dict)
    region: str | None = None
    resource_id: str | None = Field(
        default=None, description="Primary resource this call targets (for audit + intent check)."
    )


class TerraformAction(BaseModel):
    """A Terraform change proposed as HCL, to be validated before apply.

    The HCL never runs as-is: it must pass ``terraform fmt`` /
    ``validate`` / ``plan`` (see ``execution.terraform``) before any
    executor is allowed to apply it.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal[ActionKind.TERRAFORM] = ActionKind.TERRAFORM
    hcl: str = Field(description="Terraform configuration (main.tf contents).")
    working_dir_name: str = Field(
        default="plan", description="Logical name for the generated Terraform workspace."
    )
    targets: list[str] = Field(
        default_factory=list, description="Optional -target addresses to scope the plan."
    )
    resource_id: str | None = None


class KubernetesAction(BaseModel):
    """A Kubernetes manifest apply, proposed as structured YAML/JSON docs."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal[ActionKind.KUBERNETES] = ActionKind.KUBERNETES
    manifest: dict[str, Any] = Field(
        description="A single Kubernetes object (apiVersion/kind/...)."
    )
    namespace: str | None = None
    resource_id: str | None = None


class NoopAction(BaseModel):
    """An explicit no-op — used when the plan is advisory/guidance only."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal[ActionKind.NOOP] = ActionKind.NOOP
    note: str = ""


StepAction = AwsApiAction | TerraformAction | KubernetesAction | NoopAction


class ExecutionStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order: int = Field(ge=1)
    action: StepAction = Field(discriminator="kind")
    rationale: str = Field(default="", description="Why this step; used by intent verification.")
    requires_approval: bool = Field(
        default=True, description="Whether this step is gated on human approval."
    )


class ExecutionPlan(BaseModel):
    """The only object an executor is allowed to consume.

    A plan that does not parse into this model — extra fields, wrong
    types, unknown action kinds — is rejected before execution.
    """

    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(default_factory=_new_id)
    intent: str = Field(description="One-line statement of what the plan is meant to achieve.")
    kra_code: str | None = None
    generated_by: str = Field(
        default="llm", description="'llm' | 'memory' | 'deterministic' — provenance for audit."
    )
    dry_run: bool = True
    steps: list[ExecutionStep] = Field(default_factory=list)
