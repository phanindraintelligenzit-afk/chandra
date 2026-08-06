"""Pydantic models shared by every stage of the Digital Worker workflow.

The envelope of record is :class:`CloudRequest`. Every intake channel
(Jira, Slack, Teams, email, REST, monitoring, webhooks) is normalized
into this shape by :mod:`src.chandra.digital_worker.intake` before any
downstream node sees it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    return str(uuid4())


class RequiredPermission(BaseModel):
    """Least-privilege permission required for the operation."""
    action: str = Field(description="The IAM action, e.g., s3:PutObject")
    resource: str = Field(default="*", description="The target resource ARN, if known")
    reason: str = Field(description="Why this permission is needed")



class RequestSource(StrEnum):
    """Where a request entered the system."""

    JIRA = "jira"
    SLACK = "slack"
    TEAMS = "teams"
    EMAIL = "email"
    REST_API = "rest_api"
    MONITORING = "monitoring"
    CLOUDWATCH = "cloudwatch"
    AZURE_MONITOR = "azure_monitor"
    GCP_MONITORING = "gcp_monitoring"
    WEBHOOK = "webhook"


class CloudPlatform(StrEnum):
    """Cloud platform a request targets."""

    AWS = "aws"
    AZURE = "azure"
    GCP = "gcp"
    KUBERNETES = "kubernetes"
    UNKNOWN = "unknown"


class RequestCategory(StrEnum):
    """Operational category assigned during classification."""

    INCIDENT = "incident"
    SERVICE_REQUEST = "service_request"
    CHANGE_REQUEST = "change_request"
    COST_OPTIMIZATION = "cost_optimization"
    SECURITY = "security"
    COMPLIANCE = "compliance"
    PERFORMANCE = "performance"
    RELIABILITY = "reliability"
    QUESTION = "question"
    UNKNOWN = "unknown"


class RequestPriority(StrEnum):
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class DecisionMode(StrEnum):
    """Deterministic outcome of the decision node."""

    AUTO_EXECUTE = "auto_execute"
    AWAIT_APPROVAL = "await_approval"
    ENGINEER_GUIDANCE = "engineer_guidance"


class CloudRequest(BaseModel):
    """Normalized envelope for every incoming request, whatever the channel."""

    request_id: str = Field(default_factory=_new_id)
    source: RequestSource
    external_id: str | None = Field(
        default=None,
        description="Channel-native identifier (Jira key, Slack ts, alarm ARN, message id).",
    )
    title: str
    description: str = ""
    priority: RequestPriority | None = Field(
        default=None, description="Priority as stated by the channel; classifier fills gaps."
    )
    requester: str | None = None
    received_at: datetime = Field(default_factory=_utcnow)
    labels: list[str] = Field(default_factory=list)
    raw_payload: dict[str, Any] = Field(default_factory=dict)


class RequestClassification(BaseModel):
    """Deterministic classification of a normalized request."""

    category: RequestCategory = RequestCategory.UNKNOWN
    platform: CloudPlatform = CloudPlatform.UNKNOWN
    services: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    priority: RequestPriority = RequestPriority.P3
    summary: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class ContextItem(BaseModel):
    """One piece of evidence gathered during context collection."""

    provider: str = Field(
        description="Collector that produced this item (e.g. 'cloudwatch_alarms')."
    )
    kind: str = Field(
        description=(
            "Evidence kind: monitoring | logs | cloud_api | tracker | knowledge_base | memory."
        )
    )
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)


class ContextBundle(BaseModel):
    items: list[ContextItem] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class RootCause(BaseModel):
    """Root-cause analysis result (LLM-assisted via composer, with fallback)."""

    summary: str = ""
    probable_causes: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    generated_by: str = Field(default="deterministic", description="'llm' or 'deterministic'.")


class ResolutionStep(BaseModel):
    order: int
    action: str
    detail: str = ""
    command: str | None = None
    expected_outcome: str | None = None


class ResolutionPlan(BaseModel):
    """Ordered plan to resolve the request.

    ``generated_by`` records the route taken (Proposedflow §3):
    ``memory`` — reused from the past-history database;
    ``llm`` — freshly generated via the composer;
    ``deterministic`` — rule-based fallback when Bedrock is unavailable.
    """

    plan_id: str = Field(default_factory=_new_id)
    generated_by: str = "deterministic"
    fingerprint: str = Field(default="", description="Stable hash used for memory lookups.")
    steps: list[ResolutionStep] = Field(default_factory=list)
    rollback_steps: list[ResolutionStep] = Field(default_factory=list)
    detector_id: str | None = Field(
        default=None,
        description=(
            "When set, maps to a registered ActionExecutor handler for automated remediation."
        ),
    )
    automation_available: bool = False
    notes: str = ""


class RiskAssessment(BaseModel):
    level: RiskLevel = RiskLevel.MEDIUM
    score: int = Field(default=0, ge=0, le=100)
    factors: list[str] = Field(default_factory=list)
    reversible: bool = True
    requires_approval: bool = True


class ExecutionDecision(BaseModel):
    mode: DecisionMode
    reason: str


class ExecutionOutcome(BaseModel):
    status: str = Field(default="skipped", description="executed | dry_run | skipped | failed.")
    dry_run: bool = True
    detail: str = ""
    step_results: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    execution_code: str = ""
    execution_logs: str = ""
    rollback_code: str = ""
    rollback_logs: str = ""
    sandbox_path: str | None = None
    pipeline_response: dict[str, Any] | None = None


class ValidationCheck(BaseModel):
    name: str
    passed: bool
    detail: str = ""


class ValidationResult(BaseModel):
    passed: bool = False
    checks: list[ValidationCheck] = Field(default_factory=list)
    verification_code: str = ""
    verification_logs: str = ""


class NotificationResult(BaseModel):
    channel: str
    status: str = Field(default="skipped", description="sent | skipped | failed.")
    detail: str = ""


class TrackerUpdate(BaseModel):
    tracker: str = "jira"
    issue_key: str | None = None
    status: str = Field(default="skipped", description="updated | created | skipped | failed.")
    detail: str = ""


class ApprovalRecord(BaseModel):
    approved: bool
    approver: str | None = None
    comment: str = ""
    decided_at: datetime = Field(default_factory=_utcnow)


class AuditEvent(BaseModel):
    at: datetime = Field(default_factory=_utcnow)
    node: str
    event: str
    data: dict[str, Any] = Field(default_factory=dict)


class WorkflowResult(BaseModel):
    """Terminal summary returned to callers (FastAPI job result)."""

    request: CloudRequest
    classification: RequestClassification
    root_cause: RootCause
    plan: ResolutionPlan
    risk: RiskAssessment
    decision: ExecutionDecision
    execution: ExecutionOutcome
    validation: ValidationResult
    tracker_updates: list[TrackerUpdate] = Field(default_factory=list)
    notifications: list[NotificationResult] = Field(default_factory=list)
    guidance_md: str = ""
    audit_trail: list[AuditEvent] = Field(default_factory=list)
    status: str = "completed"
    required_permissions: list[RequiredPermission] = Field(default_factory=list)
