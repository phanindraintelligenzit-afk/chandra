"""Digital Worker LangGraph — the end-to-end request workflow.

Topology (mission workflow, mapped 1:1 onto nodes)::

    START → receive_request → understand_request → classify_request
          → identify_platform → collect_context → root_cause_analysis
          → plan_resolution → risk_analysis → decision
          → { execute_automation | approval_gate | generate_guidance }
          → validate_result → update_tracker → notify → audit
          → persist → END

Determinism contract: only ``plan_resolution`` may (indirectly, via the
composer) invoke Bedrock. ``decision``, ``execute_automation`` and every
router in this module are deterministic, mirroring the core graph's
``decision_router`` / ``action_executor`` / ``escalation`` invariant.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from sqlalchemy.exc import SQLAlchemyError
from src.chandra.config import settings
from src.chandra.db.models import CloudRequestRecord
from src.chandra.db.session import session_scope
from src.chandra.digital_worker import notifications as channels
from src.chandra.digital_worker.classifier import classify_request, identify_platform
from src.chandra.digital_worker.context import ContextCollector
from src.chandra.digital_worker.guidance import render_guidance
from src.chandra.digital_worker.intake import normalize_request
from src.chandra.digital_worker.memory import persist_plan
from src.chandra.digital_worker.planner import (
    build_plan,
    derive_root_cause,
    explicit_resource_id,
)
from src.chandra.digital_worker.risk import assess_risk
from src.chandra.digital_worker.schemas import (
    ApprovalRecord,
    AuditEvent,
    CloudPlatform,
    CloudRequest,
    DecisionMode,
    ExecutionOutcome,
    NotificationResult,
    RequestPriority,
    RiskLevel,
    ValidationResult,
    WorkflowResult,
)
from src.chandra.digital_worker.state import DigitalWorkerState
from src.chandra.digital_worker.tracker import update_request_ticket
from src.chandra.escalation.schemas import EscalationPayload
from src.chandra.graphs.checkpointer import build_checkpointer
from src.chandra.logging import get_logger

logger = get_logger(__name__)


def _audit(node: str, event: str, **data: Any) -> AuditEvent:
    return AuditEvent(node=node, event=event, data=data)


# ---------------------------------------------------------------------------
# Intake + understanding
# ---------------------------------------------------------------------------


def receive_request(state: DigitalWorkerState) -> dict[str, Any]:
    """Normalize the channel payload into the CloudRequest envelope."""
    request = state.get("request")
    if request is None:
        request = normalize_request(state.get("source", "rest_api"), state.get("payload", {}))
    elif not isinstance(request, CloudRequest):
        request = CloudRequest.model_validate(request)
    logger.info(
        "graph.receive_request",
        request_id=request.request_id,
        source=request.source.value,
    )
    return {
        "request": request,
        "status": "in_progress",
        "audit_trail": [
            _audit(
                "receive_request",
                "request_received",
                source=request.source.value,
                external_id=request.external_id,
                title=request.title,
            )
        ],
    }


def understand_request(state: DigitalWorkerState) -> dict[str, Any]:
    """Distill the request into a one-line intent statement."""
    request = state["request"]
    title = request.title.strip()
    first_line = (request.description or request.title).strip().splitlines()[0]
    intent = f"{title} — {first_line}" if first_line != title else title
    resource = explicit_resource_id(request)
    return {
        "intent": intent[:300],
        "audit_trail": [
            _audit(
                "understand_request",
                "intent_extracted",
                intent=intent[:300],
                explicit_resource=resource,
            )
        ],
    }


def classify_request_node(state: DigitalWorkerState) -> dict[str, Any]:
    classification = classify_request(state["request"])
    return {
        "classification": classification,
        "audit_trail": [
            _audit(
                "classify_request",
                "request_classified",
                category=classification.category.value,
                priority=classification.priority.value,
                confidence=classification.confidence,
            )
        ],
    }


def identify_platform_node(state: DigitalWorkerState) -> dict[str, Any]:
    """Confirm (or refine) the target cloud platform."""
    classification = state["classification"]
    platform = classification.platform
    if platform is CloudPlatform.UNKNOWN:
        platform = identify_platform(state["request"])
        classification = classification.model_copy(update={"platform": platform})
    return {
        "classification": classification,
        "audit_trail": [
            _audit("identify_platform", "platform_identified", platform=platform.value)
        ],
    }


# ---------------------------------------------------------------------------
# Context, RCA, planning, risk
# ---------------------------------------------------------------------------


def collect_context(state: DigitalWorkerState) -> dict[str, Any]:
    bundle = ContextCollector().collect(state["request"], state["classification"])
    return {
        "context": bundle,
        "errors": [{"node": "collect_context", "error": e} for e in bundle.errors],
        "audit_trail": [
            _audit(
                "collect_context",
                "context_collected",
                items=len(bundle.items),
                errors=len(bundle.errors),
            )
        ],
    }


def root_cause_analysis(state: DigitalWorkerState) -> dict[str, Any]:
    root_cause = derive_root_cause(state["request"], state["classification"], state["context"])
    return {
        "root_cause": root_cause,
        "audit_trail": [
            _audit(
                "root_cause_analysis",
                "root_cause_derived",
                confidence=root_cause.confidence,
                generated_by=root_cause.generated_by,
            )
        ],
    }


def plan_resolution(state: DigitalWorkerState) -> dict[str, Any]:
    """Memory ▸ LLM ▸ deterministic planning. The LLM route may also
    upgrade the deterministic root cause from the previous node."""
    root_cause, plan = build_plan(state["request"], state["classification"], state["context"])
    existing = state.get("root_cause")
    if existing is not None and root_cause.generated_by != "llm":
        root_cause = existing
    return {
        "root_cause": root_cause,
        "plan": plan,
        "audit_trail": [
            _audit(
                "plan_resolution",
                "plan_generated",
                generated_by=plan.generated_by,
                steps=len(plan.steps),
                automation_available=plan.automation_available,
                detector_id=plan.detector_id,
            )
        ],
    }


def risk_analysis(state: DigitalWorkerState) -> dict[str, Any]:
    risk = assess_risk(state["classification"], state["plan"])
    return {
        "risk": risk,
        "audit_trail": [
            _audit(
                "risk_analysis",
                "risk_assessed",
                level=risk.level.value,
                score=risk.score,
                requires_approval=risk.requires_approval,
            )
        ],
    }


# ---------------------------------------------------------------------------
# Decision + approval + execution (all deterministic)
# ---------------------------------------------------------------------------


def decision(state: DigitalWorkerState) -> dict[str, Any]:
    """Dynamically evaluate the execute-vs-guidance decision using the Decision Engine."""
    from src.chandra.digital_worker.decision_engine import evaluate_decision

    verdict = evaluate_decision(
        request=state["request"],
        classification=state["classification"],
        plan=state["plan"],
        risk=state["risk"],
        source=state.get("source", ""),
    )

    logger.info(
        "graph.decision",
        request_id=state["request"].request_id,
        mode=verdict.mode.value,
        reason=verdict.reason,
    )
    return {
        "decision": verdict,
        "audit_trail": [
            _audit("decision", "decision_made", mode=verdict.mode.value, reason=verdict.reason)
        ],
    }


def route_decision(state: DigitalWorkerState) -> str:
    mode = state["decision"].mode
    if mode == DecisionMode.AUTO_EXECUTE:
        return "execute_automation"
    if mode == DecisionMode.AWAIT_APPROVAL:
        return "approval_gate"
    return "generate_guidance"


def approval_gate(state: DigitalWorkerState) -> dict[str, Any]:
    """Human-in-the-loop gate. The graph is compiled with
    ``interrupt_before=["approval_gate"]``; on resume the ``interrupt``
    call returns the approval decision payload."""
    plan = state["plan"]
    payload = interrupt(
        {
            "request_id": state["request"].request_id,
            "title": state["request"].title,
            "plan": plan.model_dump(mode="json"),
            "risk": state["risk"].model_dump(mode="json"),
            "reason": state["decision"].reason,
        }
    )
    record = (
        payload if isinstance(payload, ApprovalRecord) else ApprovalRecord.model_validate(payload)
    )
    logger.info(
        "graph.approval_gate",
        request_id=state["request"].request_id,
        approved=record.approved,
        approver=record.approver,
    )
    logger.info(f"TRANSITION: {'HUMAN_APPROVED' if record.approved else 'HUMAN_REJECTED'}")
    return {
        "approval": record,
        "audit_trail": [
            _audit(
                "approval_gate",
                "approval_decided",
                approved=record.approved,
                approver=record.approver,
                comment=record.comment,
            )
        ],
    }


def route_approval(state: DigitalWorkerState) -> str:
    approval = state.get("approval")
    if approval is not None and approval.approved:
        classification = state.get("classification")
        platform = None
        if isinstance(classification, dict):
            platform = classification.get("platform")
        elif classification is not None:
            platform = getattr(classification, "platform", None)
            
        if platform == CloudPlatform.AWS or platform == "aws":
            return "permission_analysis"
        return "execute_automation"
    return "generate_guidance"


def permission_analysis(state: DigitalWorkerState) -> dict[str, Any]:
    """Determine what permissions are needed for the AWS action."""
    from src.chandra.briefing.composer import analyze_required_permissions
    from src.chandra.digital_worker.schemas import RequiredPermission

    logger.info("TRANSITION: PERMISSION_ANALYSIS")
    request_dict = state["request"].model_dump(mode="json", exclude={"raw_payload"})
    plan_dict = state["plan"].model_dump(mode="json")
    
    raw_perms = analyze_required_permissions(request_dict, plan_dict)
    permissions = [RequiredPermission(**p) for p in raw_perms]
    
    return {
        "required_permissions": permissions,
        "audit_trail": [
            _audit("permission_analysis", "permissions_analyzed", status="completed", count=len(permissions))
        ]
    }


def permission_selection_pause(state: DigitalWorkerState) -> dict[str, Any]:
    """Interrupt the graph to wait for Copilot to select the permission set."""
    logger.info("TRANSITION: AWAITING_PERMISSION_SET")
    
    required_perms = [p.model_dump(mode="json") for p in state.get("required_permissions", [])]
    
    payload = interrupt(
        {
            "action_required": "awaiting_permission_set",
            "request_id": state["request"].request_id,
            "required_permissions": required_perms,
        }
    )
    
    # payload will contain the permission_set_id when resumed by Copilot
    permission_set_id = payload.get("permission_set_id") if isinstance(payload, dict) else None
    logger.info("TRANSITION: PERMISSION_SELECTED")
    
    return {
        "permission_set_id": permission_set_id,
        "audit_trail": [
            _audit("permission_selection_pause", "permission_attached", permission_set_id=permission_set_id)
        ]
    }

def gate_1_verification(state: DigitalWorkerState) -> dict[str, Any]:
    """Gate 1: Verify the attached permission set."""
    from src.chandra.execution.services import TaskAuthorizationService
    
    permission_set_id = state.get("permission_set_id")
    if not permission_set_id:
        logger.info("TRANSITION: GATE_1_DENIED")
        return {
            "gate_1_passed": False,
            "gate_1_result": {"pass": False, "missing_actions": [], "reason": "No permission set attached"},
            "audit_trail": [
                _audit("gate_1_verification", "gate_1_denied", reason="No permission set attached")
            ]
        }
        
    auth_svc = TaskAuthorizationService()
    task_name = state["request"].title
    required_actions = [p.action for p in state.get("required_permissions", [])]
    
    # auth_svc.is_authorized now returns a dict
    auth_result = auth_svc.is_authorized(task_name, permission_set_id, required_actions)
    
    if not auth_result.get("pass", False):
        logger.info("TRANSITION: GATE_1_DENIED")
        return {
            "gate_1_passed": False,
            "gate_1_result": auth_result,
            "audit_trail": [
                _audit("gate_1_verification", "gate_1_denied", reason="Authorization denied by TaskAuthorizationService", details=auth_result)
            ]
        }

    logger.info("TRANSITION: GATE_1_PASS")
    return {
        "gate_1_passed": True,
        "gate_1_result": auth_result,
        "audit_trail": [
            _audit("gate_1_verification", "gate_1_passed", permission_set_id=permission_set_id, details=auth_result)
        ]
    }

def route_gate1(state: DigitalWorkerState) -> str:
    logger.info("ROUTING GATE 1: %s", state.get("gate_1_passed"))
    if state.get("gate_1_passed"):
        return "terraform_generate"
    return "permission_selection_pause"


# ---------------------------------------------------------------------------
# Phase 3C: Terraform generation + validation + plan
# ---------------------------------------------------------------------------


def terraform_generate(state: DigitalWorkerState) -> dict[str, Any]:
    """Generate Terraform HCL from the approved request and resolution plan.

    Uses the ExecutionAgents adapter to produce HCL that implements the plan
    and falls back to deterministic template if it fails.
    """
    from src.chandra.digital_worker.schemas import TerraformPlanEvidence
    from digitalworker_agents.aws_execution_agent import ExecutionAgents
    import os
    import tempfile

    request = state["request"]
    plan = state["plan"]
    classification = state["classification"]
    evidence = state.get("gate_1_evidence")
    aws_permissions = evidence.matched_actions if evidence and hasattr(evidence, "matched_actions") else []

    logger.info("TRANSITION: TERRAFORM_GENERATE")

    action_dict = {
        "actionName": request.title or "Digital Worker Resolution",
        "actionDescription": request.description or "Automated execution for request",
        "service": ", ".join(classification.services) if classification.services else classification.platform.value,
        "kraCode": None,
        "priorityLevel": classification.priority.value,
        "steps": [step.action for step in plan.steps],
    }

    job_id = state.get("job_id") or request.request_id
    orchestrator = ExecutionAgents(max_iterations=1, job_id=job_id)
    sandbox_path = tempfile.mkdtemp(prefix=f"chandra-tf-{job_id}-")

    result = orchestrator.GenerateTerraformOnly(
        action=action_dict,
        aws_permissions=aws_permissions,
        sandbox_path=sandbox_path,
        thread_id=job_id,
    )

    hcl = result.get("hcl", "")
    if not hcl or result.get("status") == "error":
        logger.warning("ExecutionAgents generation failed, using fallback.")
        hcl = _deterministic_terraform_template(request, classification)

    return {
        "terraform_hcl": hcl,
        "sandbox_path": sandbox_path,  # pass this so validate can use it
        "audit_trail": [
            _audit(
                "terraform_generate",
                "hcl_generated",
                chars=len(hcl),
                request_id=request.request_id,
            )
        ],
    }


def _generate_terraform_hcl(
    request: CloudRequest, plan: Any, classification: Any
) -> str:
    """Use LLM to generate Terraform HCL, with deterministic fallback."""
    try:
        from src.chandra.llm import get_llm

        llm = get_llm()
        services = ", ".join(classification.services) if classification.services else "AWS"
        steps_text = "\n".join(f"- {s.action}: {s.detail}" for s in plan.steps)

        prompt = (
            "Generate valid Terraform HCL (main.tf) for the following AWS operation.\n"
            "Use the aws provider. Include required provider configuration.\n"
            "Add terraform output blocks for any created resource identifiers.\n"
            "Do NOT include any explanation — only raw HCL.\n\n"
            f"Request: {request.title}\n"
            f"Description: {request.description}\n"
            f"Services: {services}\n"
            f"Steps:\n{steps_text}\n"
        )
        response = llm.invoke(prompt)
        content = response.content if hasattr(response, "content") else str(response)
        # Extract HCL from markdown code blocks if present
        if "```" in content:
            import re
            match = re.search(r"```(?:hcl|terraform)?\s*\n(.*?)```", content, re.DOTALL)
            if match:
                content = match.group(1)
        return content.strip()
    except Exception as exc:
        logger.warning("terraform.llm_generation_failed_fallback", error=str(exc))
        return _deterministic_terraform_template(request, classification)


def _deterministic_terraform_template(request: CloudRequest, classification: Any) -> str:
    """Minimal valid Terraform template when LLM is unavailable."""
    services = classification.services if classification.services else []
    title_lower = request.title.lower()

    if "s3" in title_lower or "bucket" in title_lower or "s3" in [s.lower() for s in services]:
        return (
            'terraform {\n  required_providers {\n    aws = {\n'
            '      source  = "hashicorp/aws"\n      version = "~> 5.0"\n'
            '    }\n  }\n}\n\nprovider "aws" {\n  region = "us-east-1"\n}\n\n'
            'resource "aws_s3_bucket" "managed" {\n'
            '  bucket_prefix = "chandra-managed-"\n'
            '  tags = {\n    ManagedBy = "chandra"\n  }\n}\n\n'
            'output "bucket_name" {\n  value = aws_s3_bucket.managed.id\n}\n'
        )
    if "ec2" in title_lower or "instance" in title_lower or "ec2" in [s.lower() for s in services]:
        return (
            'terraform {\n  required_providers {\n    aws = {\n'
            '      source  = "hashicorp/aws"\n      version = "~> 5.0"\n'
            '    }\n  }\n}\n\nprovider "aws" {\n  region = "us-east-1"\n}\n\n'
            'data "aws_ami" "amazon_linux" {\n  most_recent = true\n'
            '  owners     = ["amazon"]\n  filter {\n    name   = "name"\n'
            '    values = ["amzn2-ami-hvm-*-x86_64-gp2"]\n  }\n}\n\n'
            'resource "aws_instance" "managed" {\n'
            '  ami           = data.aws_ami.amazon_linux.id\n'
            '  instance_type = "t3.micro"\n'
            '  tags = {\n    ManagedBy = "chandra"\n  }\n}\n\n'
            'output "instance_id" {\n  value = aws_instance.managed.id\n}\n'
        )
    # Generic fallback
    return (
        'terraform {\n  required_providers {\n    aws = {\n'
        '      source  = "hashicorp/aws"\n      version = "~> 5.0"\n'
        '    }\n  }\n}\n\nprovider "aws" {\n  region = "us-east-1"\n}\n\n'
        '# Placeholder — LLM unavailable, manual HCL required\n'
        'output "status" {\n  value = "placeholder"\n}\n'
    )


def terraform_validate_plan(state: DigitalWorkerState) -> dict[str, Any]:
    """Run terraform fmt → init → validate → plan on the generated HCL."""
    from src.chandra.digital_worker.schemas import TerraformPlanEvidence
    from src.chandra.execution.terraform import validate_terraform

    hcl = state.get("terraform_hcl", "")
    sandbox_path = state.get("sandbox_path")
    logger.info("TRANSITION: TERRAFORM_VALIDATE_PLAN")

    result = validate_terraform(hcl, run_plan=True, workdir=sandbox_path)

    add_count = 0
    change_count = 0
    destroy_count = 0
    plan_output = ""
    warnings: list[str] = []
    errors: list[str] = []

    for stage in result.stages:
        if not stage.passed:
            errors.append(f"{stage.name}: {stage.output[:500]}")
        elif stage.name == "plan":
            plan_output = stage.output
            # Parse plan counts from output
            import re
            m = re.search(r"(\d+) to add", stage.output)
            if m:
                add_count = int(m.group(1))
            m = re.search(r"(\d+) to change", stage.output)
            if m:
                change_count = int(m.group(1))
            m = re.search(r"(\d+) to destroy", stage.output)
            if m:
                destroy_count = int(m.group(1))

    evidence = TerraformPlanEvidence(
        validation_passed=result.ok,
        plan_passed=result.ok,
        resources_to_add=add_count,
        resources_to_change=change_count,
        resources_to_destroy=destroy_count,
        hcl_snippet=hcl[:2000],
        plan_output=plan_output[:4000],
        warnings=warnings,
        errors=errors,
    )

    return {
        "terraform_validation": evidence.model_dump(mode="json"),
        "terraform_plan_result": {
            "status": result.status,
            "stages": [s.model_dump(mode="json") for s in result.stages],
            "detail": result.detail,
        },
        "audit_trail": [
            _audit(
                "terraform_validate_plan",
                "terraform_validated",
                status=result.status,
                add=add_count,
                change=change_count,
                destroy=destroy_count,
            )
        ],
    }


# ---------------------------------------------------------------------------
# Phase 3D: Gate 2 — Human execution review
# ---------------------------------------------------------------------------


def gate_2_review(state: DigitalWorkerState) -> dict[str, Any]:
    """Gate 2: Present full execution evidence for human review.

    This is a genuine LangGraph interrupt boundary. CHANDRA_AUTO_APPROVE,
    ExecutionAgents, or any other shortcut MUST NOT bypass this gate for
    governed Jira execution.
    """
    from src.chandra.digital_worker.schemas import Gate2Decision, Gate2ReviewPayload

    request = state["request"]
    logger.info("TRANSITION: GATE_2_REVIEW")

    review_payload = Gate2ReviewPayload(
        jira_issue_key=request.external_id,
        original_request=f"{request.title}: {request.description}",
        planned_operation=", ".join(s.action for s in state["plan"].steps),
        required_permissions=[
            p.model_dump(mode="json") for p in state.get("required_permissions", [])
        ],
        permission_set_id=state.get("permission_set_id"),
        permission_set_version=state.get("gate_1_result", {}).get("permission_set_version"),
        gate_1_result=state.get("gate_1_result", {}),
        terraform_validation=state.get("terraform_validation", {}),
        terraform_plan=state.get("terraform_plan_result", {}),
        add_count=state.get("terraform_validation", {}).get("resources_to_add", 0),
        change_count=state.get("terraform_validation", {}).get("resources_to_change", 0),
        destroy_count=state.get("terraform_validation", {}).get("resources_to_destroy", 0),
        risk_level=state["risk"].level.value,
        job_id=state.get("job_id") or request.request_id,
    )

    payload = interrupt(
        {
            "type": "gate2_execution_review",
            "review": review_payload.model_dump(mode="json"),
        }
    )

    decision = (
        payload
        if isinstance(payload, Gate2Decision)
        else Gate2Decision.model_validate(payload)
    )

    logger.info(
        "TRANSITION: GATE_2_%s",
        "APPROVED" if decision.approved else "REJECTED",
    )

    return {
        "gate_2_passed": decision.approved,
        "gate_2_result": {
            "approved": decision.approved,
            "approver": decision.approver,
            "comment": decision.comment,
        },
        "audit_trail": [
            _audit(
                "gate_2_review",
                "gate_2_decided",
                approved=decision.approved,
                approver=decision.approver,
            )
        ],
    }


def route_gate2(state: DigitalWorkerState) -> str:
    if state.get("gate_2_passed"):
        return "terraform_apply"
    return "generate_guidance"


# ---------------------------------------------------------------------------
# Phase 3E: Terraform apply + boto3 verification + Jira completion
# ---------------------------------------------------------------------------


def terraform_apply(state: DigitalWorkerState) -> dict[str, Any]:
    """Execute terraform apply. Real AWS mutation is disabled unless
    CHANDRA_TERRAFORM_APPLY_ENABLED=true is explicitly set."""
    import os
    import subprocess
    import tempfile
    from pathlib import Path

    request = state["request"]
    hcl = state.get("terraform_hcl", "")
    apply_enabled = os.environ.get("CHANDRA_TERRAFORM_APPLY_ENABLED", "false").lower() == "true"

    logger.info("TRANSITION: TERRAFORM_APPLY", enabled=apply_enabled)

    if not apply_enabled:
        return {
            "terraform_apply_result": {
                "success": False,
                "dry_run": True,
                "detail": "Terraform apply disabled (CHANDRA_TERRAFORM_APPLY_ENABLED != true)",
                "outputs": {},
            },
            "execution": ExecutionOutcome(
                status="dry_run",
                dry_run=True,
                detail="Terraform apply disabled — dry run mode",
            ),
            "audit_trail": [
                _audit("terraform_apply", "terraform_apply_skipped", reason="disabled")
            ],
        }

    from src.chandra.execution.terraform import terraform_available

    if not terraform_available():
        return {
            "terraform_apply_result": {
                "success": False,
                "detail": "terraform binary not available",
                "outputs": {},
            },
            "execution": ExecutionOutcome(
                status="failed",
                dry_run=False,
                detail="terraform binary not available",
            ),
            "audit_trail": [
                _audit("terraform_apply", "terraform_unavailable")
            ],
        }

    import contextlib
    @contextlib.contextmanager
    def _get_workdir():
        sandbox_path = state.get("sandbox_path")
        if sandbox_path:
            yield Path(sandbox_path)
        else:
            with tempfile.TemporaryDirectory(prefix="chandra-tf-apply-") as tmp:
                wd = Path(tmp)
                (wd / "main.tf").write_text(hcl, encoding="utf-8")
                yield wd

    with _get_workdir() as workdir:

        # init
        init = subprocess.run(
            ["terraform", "init", "-backend=false", "-input=false", "-no-color"],
            cwd=str(workdir), capture_output=True, text=True, timeout=120, check=False,
        )
        if init.returncode != 0:
            return {
                "terraform_apply_result": {
                    "success": False,
                    "detail": f"terraform init failed: {init.stderr[:1000]}",
                    "outputs": {},
                },
                "execution": ExecutionOutcome(
                    status="failed", dry_run=False,
                    detail=f"terraform init failed: {init.stderr[:500]}",
                ),
                "audit_trail": [_audit("terraform_apply", "init_failed")],
            }

        # apply -auto-approve
        apply = subprocess.run(
            ["terraform", "apply", "-auto-approve", "-input=false", "-no-color"],
            cwd=str(workdir), capture_output=True, text=True, timeout=300, check=False,
        )

        if apply.returncode != 0:
            return {
                "terraform_apply_result": {
                    "success": False,
                    "detail": f"terraform apply failed: {apply.stderr[:1000]}",
                    "outputs": {},
                },
                "execution": ExecutionOutcome(
                    status="failed", dry_run=False,
                    detail=f"terraform apply failed: {apply.stderr[:500]}",
                    execution_logs=apply.stdout[:4000],
                ),
                "audit_trail": [
                    _audit("terraform_apply", "apply_failed", stderr=apply.stderr[:500])
                ],
            }

        # Capture outputs
        outputs_proc = subprocess.run(
            ["terraform", "output", "-json"],
            cwd=str(workdir), capture_output=True, text=True, timeout=30, check=False,
        )
        import json
        outputs = {}
        if outputs_proc.returncode == 0:
            try:
                outputs = json.loads(outputs_proc.stdout)
            except json.JSONDecodeError:
                pass

    return {
        "terraform_apply_result": {
            "success": True,
            "detail": "terraform apply succeeded",
            "outputs": outputs,
            "stdout": apply.stdout[:4000],
        },
        "execution": ExecutionOutcome(
            status="executed",
            dry_run=False,
            detail="Terraform apply succeeded",
            execution_logs=apply.stdout[:4000],
        ),
        "audit_trail": [
            _audit("terraform_apply", "apply_succeeded", outputs=list(outputs.keys()))
        ],
    }


def verify_aws_resources(state: DigitalWorkerState) -> dict[str, Any]:
    """Fresh boto3 verification of the AWS postcondition.

    Terraform success alone MUST NOT mark the workflow VERIFIED or COMPLETED.
    Required semantics:
      Apply SUCCESS + boto3 SUCCESS → VERIFIED → COMPLETED
      Apply SUCCESS + boto3 FAILURE → FAILED
      Apply FAILURE → FAILED
      boto3 unavailable → INDETERMINATE (never SUCCESS)
    """
    from src.chandra.digital_worker.schemas import VerificationEvidence

    apply_result = state.get("terraform_apply_result", {})
    request = state["request"]

    logger.info("TRANSITION: BOTO3_VERIFICATION")

    if apply_result.get("dry_run"):
        evidence = VerificationEvidence(
            terraform_apply_success=False,
            boto3_verification_status="INDETERMINATE",
            detail="Terraform apply was a dry run — no resources to verify",
        )
        final = "INDETERMINATE"
    elif not apply_result.get("success"):
        evidence = VerificationEvidence(
            terraform_apply_success=False,
            boto3_verification_status="FAILED",
            detail="Terraform apply did not succeed — verification skipped",
        )
        final = "FAILED"
    else:
        outputs = apply_result.get("outputs", {})
        try:
            from src.chandra.execution.services import AwsResourceVerifier
            verifier = AwsResourceVerifier()
            status = verifier.verify_resource(request.title, outputs)
            verified_resources = []
            for k, v in outputs.items():
                verified_resources.append({"key": k, "value": v.get("value") if isinstance(v, dict) else v})

            if status == "VERIFIED":
                final = "COMPLETED"
            elif status == "UNVERIFIED":
                final = "INDETERMINATE"
                status = "INDETERMINATE"
            else:
                final = "FAILED"

            evidence = VerificationEvidence(
                terraform_apply_success=True,
                boto3_verification_status=status,
                verified_resources=verified_resources,
                detail=f"boto3 verification: {status}",
            )
        except Exception as exc:
            logger.warning("boto3_verification_failed", error=str(exc))
            evidence = VerificationEvidence(
                terraform_apply_success=True,
                boto3_verification_status="INDETERMINATE",
                detail=f"boto3 verification unavailable: {exc}",
            )
            final = "INDETERMINATE"

    logger.info("TRANSITION: %s", final)

    return {
        "boto3_verification": evidence.model_dump(mode="json"),
        "final_status": final,
        "audit_trail": [
            _audit(
                "verify_aws_resources",
                "verification_complete",
                status=evidence.boto3_verification_status,
                final=final,
            )
        ],
    }


def execute_automation(state: DigitalWorkerState) -> dict[str, Any]:  # noqa: PLR0912,PLR0915
    """Run the execution using the ExecutionAgents orchestrator."""
    import json

    from digitalworker_agents.aws_execution_agent import ExecutionAgents

    request = state["request"]
    plan = state["plan"]
    classification = state["classification"]
    dry_run = state.get("dry_run", False)

    if dry_run:
        from src.chandra.digital_worker.schemas import ExecutionOutcome

        outcome = ExecutionOutcome(
            status="dry_run",
            detail="Dry run requested, skipping execution",
        )
    else:
        # Map ResolutionPlan to ActionInput format
        action_dict = {
            "actionName": request.title or "Digital Worker Resolution",
            "actionDescription": request.description or "Automated execution for request",
            "service": ", ".join(classification.services)
            if classification.services
            else classification.platform.value,
            "kraCode": None,
            "priorityLevel": classification.priority.value,
            "steps": [step.action for step in plan.steps],
            "jiraUrl": f"https://dummyintelligenzit.atlassian.net/browse/{request.external_id}"
            if request.external_id and request.source.value == "jira"
            else "",
            "skipJiraUpdate": True,
        }

        # Instantiate orchestrator using the native job_id injected into state
        dw_job_id = state.get("job_id") or request.request_id

        # Load global digital worker settings if available
        import json
        import os

        # graph.py is in src/chandra/digital_worker/
        # so dirname(dirname(dirname(dirname(__file__)))) is the root
        config_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
            "digital_worker_config.json",
        )
        max_iters = 5
        cmd_timeout = 300
        if os.path.exists(config_path):
            try:
                with open(config_path) as f:
                    data = json.load(f)
                    max_iters = data.get("max_iterations", max_iters)
                    cmd_timeout = data.get("command_timeout", cmd_timeout)
            except Exception:
                pass

        orchestrator = ExecutionAgents(max_iterations=max_iters, job_id=dw_job_id)
        exec_thread_id = f"exec-{dw_job_id}"

        # Register the actual LangGraph worker thread ID into the backend job store
        # so that the /orchestrate/stop endpoint can correctly kill this thread.
        # Also stamp decision_mode="auto_execute" so the Worker Action Execution
        # Center can distinguish this job from a pre-approval running job
        # (which has decision_mode=null while the graph is still classifying).
        import sys
        import threading

        fastapi_app = sys.modules.get("fastapi_app") or sys.modules.get("__main__")
        if (
            fastapi_app
            and hasattr(fastapi_app, "_job_store_lock")
            and hasattr(fastapi_app, "_job_store")
        ):
            with fastapi_app._job_store_lock:
                if dw_job_id in fastapi_app._job_store:
                    fastapi_app._job_store[dw_job_id]["thread_id"] = threading.get_ident()
                    # Only stamp auto_execute if this job wasn't approved by a human
                    # (post-approval resume also calls execute_automation but must
                    # keep decision_mode="await_approval" for the WAEC filter)
                    if not fastapi_app._job_store[dw_job_id].get("approved_by_human"):
                        fastapi_app._job_store[dw_job_id]["decision_mode"] = "auto_execute"

        # Run it synchronously
        logger.info("TRANSITION: EXECUTION_STARTED")
        response = orchestrator.RunPipeline(
            action=action_dict,
            sandbox_path=None,
            reference_folder=None,
            command_timeout=cmd_timeout,
            thread_id=exec_thread_id,
        )

        if response.statusCode == 202:
            is_gate2 = any("terraform" in str(q).lower() or "approval" in str(q).lower() 
                           for q in (response.questions or []))
            interrupt_type = "gate2_approval" if is_gate2 else "clarification"
            
            if is_gate2:
                logger.info("TRANSITION: AWAITING_GATE_2")

            # Propagate pause to Digital Worker graph
            user_answers = interrupt(
                {
                    "type": interrupt_type,
                    "questions": response.questions,
                    "summary": response.summary,
                }
            )
            # When resumed, re-invoke with the SAME thread_id so it finds the checkpoint
            response = orchestrator.RunPipeline(
                action=action_dict,
                sandbox_path=None,
                reference_folder=None,
                command_timeout=300,
                thread_id=exec_thread_id,
                answers=user_answers if isinstance(user_answers, list) else [user_answers],
            )

        from src.chandra.digital_worker.schemas import ExecutionOutcome

        status_map = {
            200: "executed",
            # We treat failed or needs_clarification as failed from the graph's perspective
        }
        status_str = status_map.get(response.statusCode, "failed")

        errors = []
        if response.exception:
            errors.append(response.exception)

        # Parse output logs if available
        execution_logs = ""
        if response.execution_results:
            log_lines = []
            for res in response.execution_results:
                log_lines.append(f"Command: {res.command}")
                if res.stdout:
                    log_lines.append(res.stdout)
                if res.stderr:
                    log_lines.append(res.stderr)
            execution_logs = "\n".join(log_lines)

        outcome = ExecutionOutcome(
            status=status_str,
            dry_run=dry_run,
            detail=response.summary or "Orchestrator completed",
            errors=errors,
            execution_logs=execution_logs,
            execution_code=json.dumps([step.model_dump() for step in plan.steps]),
            sandbox_path=response.sandbox_path,
            pipeline_response=response.model_dump(),
        )

    logger.info(
        "graph.execute_automation",
        request_id=request.request_id,
        status=outcome.status,
        dry_run=dry_run,
    )
    return {
        "execution": outcome,
        "audit_trail": [
            _audit(
                "execute_automation",
                "automation_executed",
                status=outcome.status,
                dry_run=dry_run,
            )
        ],
    }


def generate_guidance(state: DigitalWorkerState) -> dict[str, Any]:
    guidance = render_guidance(
        state["request"],
        state["classification"],
        state["root_cause"],
        state["plan"],
        state["risk"],
        state["context"],
    )
    approval = state.get("approval")
    
    if approval is not None and not approval.approved:
        detail = f"Request REJECTED by {approval.approver}. Reason: {approval.comment}"
    else:
        detail = "engineer guidance produced"
        
    return {
        "guidance_md": guidance,
        "execution": ExecutionOutcome(status="skipped", dry_run=True, detail=detail),
        "audit_trail": [_audit("generate_guidance", "guidance_generated", chars=len(guidance))],
    }


# ---------------------------------------------------------------------------
# Validation, tracker, notifications, audit, persist
# ---------------------------------------------------------------------------


def validate_result(state: DigitalWorkerState) -> dict[str, Any]:
    from src.chandra.digital_worker.verifier import verify_execution

    execution = state.get("execution") or ExecutionOutcome(status="skipped", dry_run=True)

    # Governed path — verification already done in verify_aws_resources
    if state.get("final_status"):
        from src.chandra.digital_worker.schemas import ValidationCheck, ValidationResult
        v_status = state.get("boto3_verification", {}).get("boto3_verification_status", "INDETERMINATE")
        passed = state["final_status"] == "COMPLETED"
        return {
            "validation": ValidationResult(
                passed=passed,
                checks=[ValidationCheck(name="governed_verification", passed=passed, detail=v_status)],
            ),
            "audit_trail": [_audit("validate_result", "governed_validation", status=v_status)],
        }

    if state.get("guidance_md") and execution.status == "skipped":
        # Guidance path, no real execution to verify
        from src.chandra.digital_worker.schemas import (
            ValidationCheck,
            ValidationResult,
        )

        validation = ValidationResult(
            passed=True,
            checks=[
                ValidationCheck(
                    name="guidance_produced", passed=True, detail="engineer guidance rendered"
                )
            ],
        )
    else:
        logger.info("TRANSITION: AWS_VERIFICATION")
        validation = verify_execution(
            request=state["request"],
            classification=state["classification"],
            plan=state["plan"],
            execution=execution,
        )
        if validation.passed:
            logger.info("TRANSITION: VERIFIED")
        else:
            logger.info("TRANSITION: VERIFICATION_FAILED")

    return {
        "validation": validation,
        "audit_trail": [
            _audit(
                "validate_result",
                "validated",
                passed=validation.passed,
                checks=len(validation.checks),
            )
        ],
    }


def update_tracker(state: DigitalWorkerState) -> dict[str, Any]:
    execution = state.get("execution")
    validation = state.get("validation")

    # Governed Jira path (Phase 3E completion)
    final_status = state.get("final_status")
    if final_status:
        verification = state.get("boto3_verification", {})
        gate_2 = state.get("gate_2_result", {})
        resolved = final_status == "COMPLETED"
        comment = (
            f"Chandra Governed Workflow — Final Status: {final_status}\n\n"
            f"Gate 1: {'PASS' if state.get('gate_1_passed') else 'FAIL'}\n"
            f"Gate 2: {'APPROVED' if gate_2.get('approved') else 'REJECTED'} "
            f"(by {gate_2.get('approver', 'unknown')})\n"
            f"Terraform Apply: {'SUCCESS' if state.get('terraform_apply_result', {}).get('success') else 'FAILED/DRY_RUN'}\n"
            f"boto3 Verification: {verification.get('boto3_verification_status', 'N/A')}\n"
            f"Final: {final_status}"
        )
        update = update_request_ticket(state["request"], comment, resolved)
        return {
            "tracker_updates": [update],
            "status": "completed" if resolved else "completed_with_issues",
            "audit_trail": [
                _audit("update_tracker", "governed_tracker_updated",
                       status=update.status, final=final_status)
            ],
        }

    # Standard (non-governed) path
    if execution is None:
        execution = ExecutionOutcome(status="skipped", dry_run=True, detail="No execution")
    resolved = False
    if validation is not None:
        resolved = execution.status == "executed" and validation.passed

    approval = state.get("approval")
    is_rejected = approval is not None and not approval.approved

    if is_rejected:
        comment = f"Chandra Digital Worker outcome: REJECTED\n\n{execution.detail}"
    elif state.get("guidance_md"):
        comment = (
            "Chandra Digital Worker analyzed this request and produced engineer "
            f"guidance (decision: {state['decision'].reason}).\n\n{state['guidance_md'][:6000]}"
        )
    else:
        passed = validation.passed if validation else False
        comment = (
            f"Chandra Digital Worker outcome: {execution.status} "
            f"(dry_run={execution.dry_run}). {execution.detail} "
            f"Validation passed: {passed}."
        )
    update = update_request_ticket(state["request"], comment, resolved)
    return {
        "tracker_updates": [update],
        "audit_trail": [
            _audit(
                "update_tracker",
                "tracker_updated",
                status=update.status,
                issue_key=update.issue_key,
            )
        ],
    }


def notify(state: DigitalWorkerState) -> dict[str, Any]:
    request = state["request"]
    execution = state["execution"]
    title = f"[Chandra] {request.title[:120]} — {execution.status}"
    body = (
        f"Category: {state['classification'].category.value} | "
        f"Platform: {state['classification'].platform.value} | "
        f"Priority: {state['classification'].priority.value} | "
        f"Risk: {state['risk'].level.value}\n"
        f"Decision: {state['decision'].mode.value} — {state['decision'].reason}\n"
        f"Outcome: {execution.detail or execution.status}"
    )
    results: list[NotificationResult] = channels.dispatch_all(title, body)

    if state["classification"].priority is RequestPriority.P1 or state["risk"].level in (
        RiskLevel.HIGH,
        RiskLevel.CRITICAL,
    ):
        severity = "critical" if state["risk"].level is RiskLevel.CRITICAL else "high"
        results.append(
            channels.notify_sns(
                EscalationPayload(
                    finding_id=request.request_id,
                    resource_id=explicit_resource_id(request) or request.external_id or "unknown",
                    severity=severity,
                    service=", ".join(state["classification"].services) or "cloud",
                    region=str(request.raw_payload.get("region") or settings.aws_default_region),
                    summary=request.title[:200],
                    recommended_action=state["decision"].reason,
                )
            )
        )
    return {
        "notifications": results,
        "audit_trail": [
            _audit(
                "notify",
                "notifications_dispatched",
                channels={r.channel: r.status for r in results},
            )
        ],
    }


def audit(state: DigitalWorkerState) -> dict[str, Any]:
    """Assemble the terminal WorkflowResult from all stage artifacts."""
    execution = state.get("execution") or ExecutionOutcome(status="skipped", dry_run=True)
    validation = state.get("validation") or ValidationResult(passed=False)

    final_status = state.get("final_status")
    if final_status:
        status = "completed" if final_status == "COMPLETED" else "completed_with_issues"
    elif validation.passed:
        status = "completed"
    else:
        status = "completed_with_issues"

    result = WorkflowResult(
        request=state["request"],
        classification=state["classification"],
        root_cause=state["root_cause"],
        plan=state["plan"],
        risk=state["risk"],
        decision=state["decision"],
        execution=execution,
        validation=validation,
        tracker_updates=state.get("tracker_updates", []),
        notifications=state.get("notifications", []),
        guidance_md=state.get("guidance_md", ""),
        audit_trail=state.get("audit_trail", []),
        status=status,
        required_permissions=state.get("required_permissions", []),
    )
    return {
        "result": result.model_dump(mode="json"),
        "status": result.status,
        "audit_trail": [_audit("audit", "workflow_summarized", status=result.status)],
    }


def persist(state: DigitalWorkerState) -> dict[str, Any]:
    """Write the audit record + resolution memory. The ONLY node in this
    graph allowed to write to Postgres."""
    request = state["request"]
    try:
        with session_scope() as session:
            session.add(
                CloudRequestRecord(
                    request_id=request.request_id,
                    source=request.source.value,
                    external_id=request.external_id,
                    title=request.title,
                    category=state["classification"].category.value,
                    platform=state["classification"].platform.value,
                    priority=state["classification"].priority.value,
                    risk_level=state["risk"].level.value,
                    decision_mode=state["decision"].mode.value,
                    status=state.get("status", "completed"),
                    result_jsonb=state.get("result", {}),
                    audit_jsonb=[e.model_dump(mode="json") for e in state.get("audit_trail", [])],
                    received_at=request.received_at,
                    completed_at=datetime.now(UTC),
                )
            )
            if state["plan"].fingerprint:
                persist_plan(
                    session,
                    request,
                    state["classification"],
                    state["plan"],
                    outcome=state["execution"].status,
                )
        logger.info("graph.persist", request_id=request.request_id)
        return {}
    except SQLAlchemyError as exc:
        # The workflow result is still returned to the caller; losing the
        # audit row must not lose the work.
        logger.warning("graph.persist_unavailable", request_id=request.request_id, error=str(exc))
        return {"errors": [{"node": "persist", "error": str(exc)}]}


# ---------------------------------------------------------------------------
# Graph assembly
# ---------------------------------------------------------------------------


def build_digital_worker_graph(checkpointer: Any | None = None) -> Any:
    """Compile the Digital Worker request workflow.

    Pass an explicit checkpointer in tests (e.g. a ``MemorySaver``);
    defaults to the shared durable checkpointer (Postgres in production,
    in-memory fallback when unavailable) so a request paused at the human
    approval gate survives a process restart and can still be resumed by
    ``thread_id`` (== the FastAPI job id).
    """
    graph: StateGraph[DigitalWorkerState] = StateGraph(DigitalWorkerState)

    graph.add_node("receive_request", receive_request)
    graph.add_node("understand_request", understand_request)
    graph.add_node("classify_request", classify_request_node)
    graph.add_node("identify_platform", identify_platform_node)
    graph.add_node("collect_context", collect_context)
    graph.add_node("root_cause_analysis", root_cause_analysis)
    graph.add_node("plan_resolution", plan_resolution)
    graph.add_node("risk_analysis", risk_analysis)
    graph.add_node("decision", decision)
    graph.add_node("approval_gate", approval_gate)
    graph.add_node("permission_analysis", permission_analysis)
    graph.add_node("permission_selection_pause", permission_selection_pause)
    graph.add_node("gate_1_verification", gate_1_verification)
    graph.add_node("terraform_generate", terraform_generate)
    graph.add_node("terraform_validate_plan", terraform_validate_plan)
    graph.add_node("gate_2_review", gate_2_review)
    graph.add_node("terraform_apply", terraform_apply)
    graph.add_node("verify_aws_resources", verify_aws_resources)
    graph.add_node("execute_automation", execute_automation)
    graph.add_node("generate_guidance", generate_guidance)
    graph.add_node("validate_result", validate_result)
    graph.add_node("update_tracker", update_tracker)
    graph.add_node("notify", notify)
    graph.add_node("audit", audit)
    graph.add_node("persist", persist)

    graph.add_edge(START, "receive_request")
    graph.add_edge("receive_request", "understand_request")
    graph.add_edge("understand_request", "classify_request")
    graph.add_edge("classify_request", "identify_platform")
    graph.add_edge("identify_platform", "collect_context")
    graph.add_edge("collect_context", "root_cause_analysis")
    graph.add_edge("root_cause_analysis", "plan_resolution")
    graph.add_edge("plan_resolution", "risk_analysis")
    graph.add_edge("risk_analysis", "decision")

    graph.add_conditional_edges(
        "decision",
        route_decision,
        ["execute_automation", "approval_gate", "generate_guidance"],
    )
    graph.add_conditional_edges(
        "approval_gate",
        route_approval,
        ["execute_automation", "generate_guidance", "permission_analysis"],
    )

    # Phase 3B: Permission analysis → Gate 1
    graph.add_edge("permission_analysis", "permission_selection_pause")
    graph.add_edge("permission_selection_pause", "gate_1_verification")

    graph.add_conditional_edges(
        "gate_1_verification",
        route_gate1,
        {
            "terraform_generate": "terraform_generate",
            "permission_selection_pause": "permission_selection_pause",
        },
    )

    # Phase 3C: Terraform generation → validation → plan
    graph.add_edge("terraform_generate", "terraform_validate_plan")
    graph.add_edge("terraform_validate_plan", "gate_2_review")

    # Phase 3D: Gate 2 human execution review
    graph.add_conditional_edges(
        "gate_2_review",
        route_gate2,
        ["terraform_apply", "generate_guidance"],
    )

    # Phase 3E: Terraform apply → verification → completion
    graph.add_edge("terraform_apply", "verify_aws_resources")
    graph.add_edge("verify_aws_resources", "validate_result")

    # Standard paths
    graph.add_edge("execute_automation", "validate_result")
    graph.add_edge("generate_guidance", "validate_result")
    graph.add_edge("validate_result", "update_tracker")
    graph.add_edge("update_tracker", "notify")
    graph.add_edge("notify", "audit")
    graph.add_edge("audit", "persist")
    graph.add_edge("persist", END)

    saver = checkpointer if checkpointer is not None else build_checkpointer()
    return graph.compile(checkpointer=saver, interrupt_before=["approval_gate"])
