"""LangGraph state for the Digital Worker request workflow."""

from __future__ import annotations

from operator import add
from typing import Annotated, Any, TypedDict

from src.chandra.digital_worker.schemas import (
    ApprovalRecord,
    AuditEvent,
    CloudRequest,
    ContextBundle,
    ExecutionDecision,
    ExecutionOutcome,
    NotificationResult,
    RequestClassification,
    RequiredPermission,
    ResolutionPlan,
    RiskAssessment,
    RootCause,
    TrackerUpdate,
    ValidationResult,
)


class DigitalWorkerState(TypedDict, total=False):
    """Full workflow state. ``total=False`` so partial node returns are legal.

    ``audit_trail`` / ``notifications`` / ``tracker_updates`` / ``errors``
    use the ``add`` reducer so every node can append without clobbering.
    """

    # Raw intake (set by the caller)
    source: str
    payload: dict[str, Any]
    job_id: str
    # When False, execute_automation performs the real mutating call for
    # plans with a registered handler. Defaults to True (dry run).
    dry_run: bool

    # Workflow artifacts, in stage order
    request: CloudRequest
    intent: str
    classification: RequestClassification
    context: ContextBundle
    root_cause: RootCause
    plan: ResolutionPlan
    risk: RiskAssessment
    decision: ExecutionDecision
    approval: ApprovalRecord
    execution: ExecutionOutcome
    guidance_md: str
    validation: ValidationResult

    tracker_updates: Annotated[list[TrackerUpdate], add]
    notifications: Annotated[list[NotificationResult], add]
    audit_trail: Annotated[list[AuditEvent], add]
    errors: Annotated[list[dict[str, Any]], add]

    status: str
    result: dict[str, Any]
    required_permissions: list[RequiredPermission]
    permission_set_id: str
    gate_1_passed: bool
    gate_1_result: dict[str, Any]

    # Phase 3C: Terraform preparation
    terraform_hcl: str
    terraform_validation: dict[str, Any]
    terraform_plan_result: dict[str, Any]

    # Phase 3D: Gate 2 human execution review
    gate_2_passed: bool
    gate_2_result: dict[str, Any]

    # Phase 3E: Execution + verification
    terraform_apply_result: dict[str, Any]
    boto3_verification: dict[str, Any]
    final_status: str
