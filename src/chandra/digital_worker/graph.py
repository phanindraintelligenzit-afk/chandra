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
    if mode is DecisionMode.AUTO_EXECUTE:
        return "execute_automation"
    if mode is DecisionMode.AWAIT_APPROVAL:
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
        return "execute_automation"
    return "generate_guidance"


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
        response = orchestrator.RunPipeline(
            action=action_dict,
            sandbox_path=None,
            reference_folder=None,
            command_timeout=cmd_timeout,
            thread_id=exec_thread_id,
        )

        if response.statusCode == 202:
            # Propagate pause to Digital Worker graph
            user_answers = interrupt(
                {
                    "type": "clarification",
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
    detail = (
        "automation rejected by approver; engineer guidance produced"
        if approval is not None and not approval.approved
        else "engineer guidance produced"
    )
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

    execution = state["execution"]

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
        validation = verify_execution(
            request=state["request"],
            classification=state["classification"],
            plan=state["plan"],
            execution=execution,
        )

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
    execution = state["execution"]
    validation = state["validation"]
    resolved = execution.status == "executed" and validation.passed
    if state.get("guidance_md"):
        comment = (
            "Chandra Digital Worker analyzed this request and produced engineer "
            f"guidance (decision: {state['decision'].reason}).\n\n{state['guidance_md'][:6000]}"
        )
    else:
        comment = (
            f"Chandra Digital Worker outcome: {execution.status} "
            f"(dry_run={execution.dry_run}). {execution.detail} "
            f"Validation passed: {validation.passed}."
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
    result = WorkflowResult(
        request=state["request"],
        classification=state["classification"],
        root_cause=state["root_cause"],
        plan=state["plan"],
        risk=state["risk"],
        decision=state["decision"],
        execution=state["execution"],
        validation=state["validation"],
        tracker_updates=state.get("tracker_updates", []),
        notifications=state.get("notifications", []),
        guidance_md=state.get("guidance_md", ""),
        audit_trail=state.get("audit_trail", []),
        status="completed" if state["validation"].passed else "completed_with_issues",
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
        ["execute_automation", "generate_guidance"],
    )

    graph.add_edge("execute_automation", "validate_result")
    graph.add_edge("generate_guidance", "validate_result")
    graph.add_edge("validate_result", "update_tracker")
    graph.add_edge("update_tracker", "notify")
    graph.add_edge("notify", "audit")
    graph.add_edge("audit", "persist")
    graph.add_edge("persist", END)

    saver = checkpointer if checkpointer is not None else build_checkpointer()
    # ``interrupt_before`` makes the human gate real (see the core graph's
    # build_graph for why the function-level interrupt alone is not enough
    # in LangGraph 1.x).
    return graph.compile(checkpointer=saver, interrupt_before=["approval_gate"])
