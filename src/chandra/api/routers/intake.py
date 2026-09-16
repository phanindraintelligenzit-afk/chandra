"""Digital Worker intake: omnichannel requests, webhooks and approvals.

Fifth extraction. These endpoints submit and resume Digital Worker runs, so they
need the write side of the shared runtime and the compiled graph — both of which
now live outside ``fastapi_app`` precisely so this could move.

Approval resumes are keyed by ``thread_id == job_id`` against the graph's
checkpointer, which is why the graph is a process-wide singleton in
``api/graphs.py`` rather than built per request.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from typing import Any

from fastapi import APIRouter, Header, Query
from fastapi.responses import JSONResponse
from langgraph.types import Command
from pydantic import BaseModel, Field
from src.chandra.api import graphs, runtime
from src.chandra.digital_worker.intake import SUPPORTED_SOURCES
from src.chandra.observability import correlation

logger = logging.getLogger("fastapi_app")

router = APIRouter(tags=["intake"])


class CloudRequestSubmission(BaseModel):
    source: str = Field(default="rest_api", description=f"One of: {', '.join(SUPPORTED_SOURCES)}")
    payload: dict[str, Any] = Field(
        description=(
            "Channel-native payload; for rest_api use {title, description, priority, requester}."
        )
    )
    dry_run: bool = Field(
        default=False,
        description="When False, approved automations perform real mutating AWS calls.",
    )


class ApprovalSubmission(BaseModel):
    approved: bool = Field(description="True to approve automated execution, False to reject.")
    approver: str | None = Field(default=None, description="Who decided.")
    comment: str = Field(default="", description="Optional decision rationale.")
    permission_set_id: str | None = Field(
        default=None, description="Optional permission set ID attached by Copilot."
    )
    permission_set_document: dict[str, Any] | None = Field(
        default=None, description="Optional mocked permission set for E2E testing."
    )


def dw_thread_config(job_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": job_id}}


def dw_finalize_job(job_id: str, final_state: dict[str, Any], start_time: float) -> None:
    """Translate terminal graph state into the shared job-store shape."""
    with runtime.job_store_lock:
        if runtime.job_store[job_id].get("status") == "stopped":
            return
        execution = final_state.get("execution")
        actual_status = "completed"
        pipeline_res = {}
        if execution:
            if hasattr(execution, "status"):
                actual_status = execution.status
            elif isinstance(execution, dict) and "status" in execution:
                actual_status = execution["status"]

            if actual_status == "executed":
                actual_status = "completed"

            if hasattr(execution, "pipeline_response") and execution.pipeline_response:
                pipeline_res = execution.pipeline_response
            elif isinstance(execution, dict) and "pipeline_response" in execution:
                pipeline_res = execution.get("pipeline_response") or {}

        runtime.job_store[job_id]["status"] = actual_status
        runtime.job_store[job_id]["progress"] = 100

        result_dict = pipeline_res.copy() if pipeline_res else {}
        result_dict["status"] = actual_status
        result_dict["output"] = final_state.get("result", {})

        runtime.job_store[job_id]["result"] = result_dict
        runtime.job_store[job_id]["completed_at"] = time.time()
        runtime.job_store[job_id]["message"] = (
            f"Workflow {actual_status} in {time.time() - start_time:.1f}s"
        )

        if execution:
            sandbox_path = (
                execution.get("sandbox_path")
                if isinstance(execution, dict)
                else getattr(execution, "sandbox_path", None)
            )
            if sandbox_path:
                runtime.job_store[job_id]["sandbox_path"] = sandbox_path


def _run_digital_worker_task(  # noqa: PLR0915 - linear workflow driver
    job_id: str, submission: CloudRequestSubmission
) -> None:
    """Background worker for /requests and /webhooks/{source}."""
    start_time = time.time()
    runtime.thread_local.job_id = job_id
    cid = correlation.get_correlation_id() or job_id
    tid = correlation.get_tenant_id()
    correlation.bind(cid, tid)
    try:
        if graphs.digital_worker is None:
            raise RuntimeError("Digital Worker graph is not initialized")
        with runtime.job_store_lock:
            runtime.job_store[job_id]["status"] = "running"
            runtime.job_store[job_id]["job_type"] = "dw"
            runtime.job_store[job_id]["correlation_id"] = cid
            runtime.job_store[job_id]["tenant_id"] = tid
            runtime.job_store[job_id]["started_at"] = start_time
            runtime.job_store[job_id]["progress"] = 10
            runtime.job_store[job_id]["message"] = f"Processing {submission.source} request..."
            runtime.job_store[job_id]["thread_id"] = threading.get_ident()

        final_state = graphs.digital_worker.invoke(
            {
                "source": submission.source,
                "payload": submission.payload,
                "dry_run": submission.dry_run,
                "job_id": job_id,
                "correlation_id": cid,
                "tenant_id": tid,
            },
            config=dw_thread_config(job_id),
        )

        # interrupt_before=["approval_gate"] pauses the run when human
        # approval is required. Surface that state instead of completing.
        snapshot = graphs.digital_worker.get_state(dw_thread_config(job_id))
        if snapshot.next and "approval_gate" in snapshot.next:
            values = snapshot.values
            request = values["request"]
            classification = values["classification"]
            root_cause = values["root_cause"]
            with runtime.job_store_lock:
                runtime.job_store[job_id]["status"] = "awaiting_approval"
                runtime.job_store[job_id]["progress"] = 70
                runtime.job_store[job_id]["message"] = "Awaiting human approval"
                runtime.job_store[job_id]["title"] = request.title
                runtime.job_store[job_id]["result"] = {
                    "status": "awaiting_approval",
                    "approval_request": {
                        "request_id": request.request_id,
                        "external_id": request.external_id,
                        "source": request.source.value,
                        "title": request.title,
                        "description": request.description,
                        "requester": request.requester,
                        "category": classification.category.value,
                        "platform": classification.platform.value,
                        "priority": classification.priority.value,
                        "services": classification.services,
                        "root_cause": root_cause.model_dump(mode="json"),
                        "plan": values["plan"].model_dump(mode="json"),
                        "risk": values["risk"].model_dump(mode="json"),
                        "decision_mode": values["decision"].mode.value,
                        "reason": values["decision"].reason,
                        "resume_url": f"/requests/{job_id}/approve",
                    },
                }
            logger.info("DIGITAL WORKER JOB [%s] awaiting approval", job_id)
            return

        # Handle permission selection pause
        if snapshot.next and "permission_selection_pause" in snapshot.next:
            interrupts = snapshot.tasks[0].interrupts if snapshot.tasks else []
            interrupt_val = interrupts[0].value if interrupts else {}
            with runtime.job_store_lock:
                runtime.job_store[job_id]["status"] = "awaiting_permission"
                runtime.job_store[job_id]["progress"] = 75
                runtime.job_store[job_id]["message"] = "Awaiting Copilot permission attachment"
                runtime.job_store[job_id]["result"] = interrupt_val
            logger.info("DIGITAL WORKER JOB [%s] awaiting permission attachment", job_id)
            return

        # Handle Gate 2 execution review pause (governed Jira path)
        if snapshot.next and "gate_2_review" in snapshot.next:
            interrupts = snapshot.tasks[0].interrupts if snapshot.tasks else []
            interrupt_val = interrupts[0].value if interrupts else {}
            with runtime.job_store_lock:
                runtime.job_store[job_id]["status"] = "awaiting_gate2"
                runtime.job_store[job_id]["progress"] = 85
                runtime.job_store[job_id]["message"] = "Awaiting Gate 2 human execution review"
                runtime.job_store[job_id]["job_type"] = "dw"
                runtime.job_store[job_id]["result"] = {
                    "statusCode": 202,
                    "status": "awaiting_gate2_review",
                    "type": "gate2_execution_review",
                    "thread_id": job_id,
                    "review": interrupt_val.get("review", {}),
                }
            logger.info("DIGITAL WORKER JOB [%s] awaiting Gate 2 execution review", job_id)
            return

        # Handle Execution Agent HITL pause!
        if snapshot.next and "execute_automation" in snapshot.next:
            interrupts = snapshot.tasks[0].interrupts if snapshot.tasks else []
            interrupt_val = interrupts[0].value if interrupts else {}
            questions = interrupt_val.get(
                "questions", ["Please provide the required input to proceed."]
            )
            summary = interrupt_val.get("summary", "Awaiting input")
            interrupt_type = interrupt_val.get("type", "clarification")

            with runtime.job_store_lock:
                if interrupt_type == "gate2_approval":
                    runtime.job_store[job_id]["status"] = "awaiting_gate2"
                else:
                    runtime.job_store[job_id]["status"] = "completed"
                runtime.job_store[job_id]["progress"] = 100
                runtime.job_store[job_id]["message"] = "Awaiting user input"
                runtime.job_store[job_id]["job_type"] = "dw"
                runtime.job_store[job_id]["result"] = {
                    "statusCode": 202,
                    "status": "needs_clarification",
                    "type": interrupt_type,
                    "thread_id": job_id,
                    "questions": questions,
                    "summary": summary,
                }
            logger.info("DIGITAL WORKER JOB [%s] paused for HITL (%s)", job_id, interrupt_type)
            return

        dw_finalize_job(job_id, final_state, start_time)

        logger.info("DIGITAL WORKER JOB [%s] completed in %.1fs", job_id, time.time() - start_time)

    except (InterruptedError, SystemExit):
        # Both are raised by our stop mechanism — status is already "stopped", do nothing
        logger.info("DIGITAL WORKER JOB [%s] was stopped by the user", job_id)
        with runtime.job_store_lock:
            if runtime.job_store[job_id].get("status") != "stopped":
                runtime.job_store[job_id]["status"] = "stopped"
                runtime.job_store[job_id]["completed_at"] = time.time()
    except BaseException as exc:
        logger.exception("DIGITAL WORKER JOB [%s] failed with exception", job_id)
        with runtime.job_store_lock:
            if runtime.job_store[job_id].get("status") != "stopped":
                runtime.job_store[job_id]["status"] = "failed"
                runtime.job_store[job_id]["error"] = str(exc)
                runtime.job_store[job_id]["completed_at"] = time.time()
                runtime.job_store[job_id]["message"] = f"Failed: {str(exc)[:200]}"
    finally:
        runtime.thread_local.job_id = None


def _resume_digital_worker_task(  # noqa: PLR0915 - linear resume driver
    job_id: str, approval: ApprovalSubmission
) -> None:
    """Background worker for /requests/{job_id}/approve — resumes the interrupt."""
    with runtime.job_store_lock:
        _stored = runtime.job_store.get(job_id, {})
        correlation.bind(_stored.get("correlation_id") or job_id, _stored.get("tenant_id"))

    start_time = time.time()
    runtime.thread_local.job_id = job_id
    try:
        if graphs.digital_worker is None:
            raise RuntimeError("Digital Worker graph is not initialized")

        with runtime.job_store_lock:
            # Check the current status before changing to running
            current_status = runtime.job_store[job_id].get("status")
            runtime.job_store[job_id]["status"] = "running"
            runtime.job_store[job_id]["job_type"] = "dw"
            runtime.job_store[job_id]["progress"] = 80
            runtime.job_store[job_id]["message"] = "Resuming after approval decision..."
            runtime.job_store[job_id]["thread_id"] = threading.get_ident()
            # Flag that this job was explicitly approved by a human
            runtime.job_store[job_id]["approved_by_human"] = True
            runtime.job_store[job_id]["requires_approval"] = False

        # Route resume payload based on which gate we're at
        resume_payload: dict[str, Any]
        if current_status == "awaiting_permission":
            resume_payload = {
                "permission_set_id": approval.permission_set_id,
                "permission_set_document": approval.permission_set_document,
            }
            logger.error(f"DEBUG RESUME PAYLOAD awaiting_permission: {resume_payload}")
        elif current_status == "awaiting_gate2":
            resume_payload = {
                "approved": approval.approved,
                "approver": approval.approver or "operator",
                "comment": approval.comment or "",
            }
        else:
            resume_payload = approval.model_dump()

        final_state = graphs.digital_worker.invoke(
            Command(resume=resume_payload),
            config=dw_thread_config(job_id),
        )

        snapshot = graphs.digital_worker.get_state(dw_thread_config(job_id))

        # Handle permission selection pause
        if snapshot.next and "permission_selection_pause" in snapshot.next:
            interrupts = snapshot.tasks[0].interrupts if snapshot.tasks else []
            interrupt_val = interrupts[0].value if interrupts else {}
            with runtime.job_store_lock:
                runtime.job_store[job_id]["status"] = "awaiting_permission"
                runtime.job_store[job_id]["progress"] = 75
                runtime.job_store[job_id]["message"] = "Awaiting Copilot permission attachment"
                runtime.job_store[job_id]["result"] = interrupt_val
            logger.info("DIGITAL WORKER JOB [%s] awaiting permission attachment", job_id)
            return

        # Handle Gate 2 execution review pause (governed Jira path)
        if snapshot.next and "gate_2_review" in snapshot.next:
            interrupts = snapshot.tasks[0].interrupts if snapshot.tasks else []
            interrupt_val = interrupts[0].value if interrupts else {}
            with runtime.job_store_lock:
                runtime.job_store[job_id]["status"] = "awaiting_gate2"
                runtime.job_store[job_id]["progress"] = 85
                runtime.job_store[job_id]["message"] = "Awaiting Gate 2 human execution review"
                runtime.job_store[job_id]["job_type"] = "dw"
                runtime.job_store[job_id]["result"] = {
                    "statusCode": 202,
                    "status": "awaiting_gate2_review",
                    "type": "gate2_execution_review",
                    "thread_id": job_id,
                    "review": interrupt_val.get("review", {}),
                }
            logger.info("DIGITAL WORKER JOB [%s] awaiting Gate 2 execution review", job_id)
            return

        # Handle Execution Agent HITL pause
        if snapshot.next and "execute_automation" in snapshot.next:
            interrupts = snapshot.tasks[0].interrupts if snapshot.tasks else []
            interrupt_val = interrupts[0].value if interrupts else {}
            questions = interrupt_val.get(
                "questions", ["Please provide the required input to proceed."]
            )
            summary = interrupt_val.get("summary", "Awaiting input")
            interrupt_type = interrupt_val.get("type", "clarification")

            with runtime.job_store_lock:
                if interrupt_type == "gate2_approval":
                    runtime.job_store[job_id]["status"] = "awaiting_gate2"
                else:
                    runtime.job_store[job_id]["status"] = "completed"
                runtime.job_store[job_id]["progress"] = 100
                runtime.job_store[job_id]["message"] = "Awaiting user input"
                runtime.job_store[job_id]["job_type"] = "dw"
                runtime.job_store[job_id]["result"] = {
                    "statusCode": 202,
                    "status": "needs_clarification",
                    "type": interrupt_type,
                    "thread_id": job_id,
                    "questions": questions,
                    "summary": summary,
                }
            logger.info("DIGITAL WORKER JOB [%s] paused for HITL (%s)", job_id, interrupt_type)
            return

        dw_finalize_job(job_id, final_state, start_time)
        logger.info(
            "DIGITAL WORKER JOB [%s] resumed (approved=%s) and completed",
            job_id,
            approval.approved if hasattr(approval, "approved") else True,
        )
    except Exception as exc:
        logger.exception("DIGITAL WORKER JOB [%s] resume failed", job_id)
        with runtime.job_store_lock:
            runtime.job_store[job_id]["status"] = "failed"
            runtime.job_store[job_id]["error"] = str(exc)
            runtime.job_store[job_id]["completed_at"] = time.time()
            runtime.job_store[job_id]["message"] = f"Resume failed: {str(exc)[:200]}"
    finally:
        runtime.thread_local.job_id = None


def _dw_submission_title(submission: CloudRequestSubmission) -> str:
    """Best-effort human title for a queued request, before classification.

    The workflow overwrites this with the normalized CloudRequest.title
    once ``receive_request`` runs; until then we surface whatever the
    channel payload carried so the approval center list is never blank.
    """
    payload = submission.payload or {}

    # Try Jira webhook format first (issue.fields.summary)
    issue = payload.get("issue")
    if isinstance(issue, dict):
        key = issue.get("key")
        fields = issue.get("fields", {})
        summary = fields.get("summary")
        if isinstance(summary, str) and summary.strip():
            title = summary.strip()
            return f"{key}: {title}" if key else title

    # Try Slack Event format
    event = payload.get("event")
    if isinstance(event, dict):
        text = event.get("text")
        if isinstance(text, str) and text.strip():
            import re

            clean_text = re.sub(r"<@[A-Z0-9]+>", "", text).strip()
            return clean_text if clean_text else "Slack request"

    # Try Teams format
    if submission.source == "teams":
        text = payload.get("text")
        if isinstance(text, str) and text.strip():
            import re

            clean_text = re.sub(r"<at>.*?</at>", "", text, flags=re.IGNORECASE)
            clean_text = re.sub(r"<[^>]+>", "", clean_text)
            clean_text = clean_text.replace("&nbsp;", " ").replace("&#160;", " ").strip()
            return clean_text if clean_text else "Teams request"

    # Fallback to direct fields
    for key in ("title", "summary", "subject", "AlarmName", "message", "text"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    fields = payload.get("fields")
    if isinstance(fields, dict):
        summary = fields.get("summary")
        if isinstance(summary, str) and summary.strip():
            return summary.strip()

    return f"{submission.source} request"


def _dw_request_summary(job_id: str, job: dict[str, Any]) -> dict[str, Any]:
    """Project a Digital Worker job into the approval-center list shape.

    Pulls the richer fields (category, platform, priority, risk, reason)
    out of the awaiting-approval result payload when present, so the
    frontend can render a full card without a second round-trip.
    """
    result = job.get("result") or {}
    approval = result.get("approval_request") or {}
    output = result.get("output") or {}
    # Prefer the live approval payload; fall back to the terminal
    # WorkflowResult so completed/failed cards stay fully populated.
    classification = output.get("classification") or {}
    out_risk = output.get("risk") or {}
    out_decision = output.get("decision") or {}
    request = output.get("request") or {}
    risk = approval.get("risk") or out_risk
    summary: dict[str, Any] = {
        "job_id": job_id,
        "status": job.get("status"),
        "progress": job.get("progress", 0),
        "message": job.get("message", ""),
        "source": approval.get("source") or request.get("source") or job.get("source"),
        "title": approval.get("title") or request.get("title") or job.get("title"),
        "external_id": approval.get("external_id") or request.get("external_id"),
        "category": approval.get("category") or classification.get("category"),
        "platform": approval.get("platform") or classification.get("platform"),
        "priority": approval.get("priority") or classification.get("priority"),
        "risk_level": risk.get("level"),
        "risk_score": risk.get("score"),
        "decision_mode": approval.get("decision_mode")
        or out_decision.get("mode")
        or job.get("decision_mode"),
        "reason": approval.get("reason") or out_decision.get("reason"),
        "requires_approval": job.get("requires_approval", job.get("status") == "awaiting_approval"),
        "approved_by_human": job.get("approved_by_human", False),
        "workflow_status": output.get("status"),
        "submitted_at": job.get("submitted_at"),
        "started_at": job.get("started_at"),
        "completed_at": job.get("completed_at"),
        "sandbox_path": job.get("sandbox_path")
        or (output.get("execution") or {}).get("sandbox_path"),
    }
    return summary


def _extract_external_id(submission: CloudRequestSubmission) -> str | None:
    if submission.source == "jira":
        issue = submission.payload.get("issue")
        if isinstance(issue, dict):
            return issue.get("key")
    return None


def _submit_digital_worker_job(submission: CloudRequestSubmission) -> JSONResponse:
    if submission.source not in SUPPORTED_SOURCES:
        return JSONResponse(
            status_code=400,
            content={
                "status": "error",
                "message": (
                    f"Unsupported source '{submission.source}'. "
                    f"Expected one of: {', '.join(SUPPORTED_SOURCES)}"
                ),
            },
        )

    if graphs.digital_worker is None:
        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "message": "Digital Worker graph is not initialized",
            },
        )

    external_id = _extract_external_id(submission)

    with runtime.job_store_lock:
        if external_id:
            for existing_job_id, existing_job in runtime.job_store.items():
                if existing_job.get("external_id") == external_id and existing_job.get(
                    "status"
                ) in ["pending", "running", "running_gate2"]:
                    logger.info(
                        "Duplicate request for %s ignored (active job: %s)",
                        external_id,
                        existing_job_id,
                    )
                    return JSONResponse(
                        status_code=202,
                        content={
                            "job_id": existing_job_id,
                            "status": "accepted",
                            "message": "Already processing this request.",
                            "poll_url": f"/jobs/status/{existing_job_id}",
                        },
                    )

        job_id = str(uuid.uuid4())
        logger.info(
            "Digital Worker request submitted -> job_id=%s source=%s external_id=%s",
            job_id,
            submission.source,
            external_id,
        )
        runtime.job_store[job_id] = {
            # ``kind`` tags this job as a Digital Worker request so the
            # GET /requests discovery endpoint can list it apart from the
            # legacy observation/orchestrate jobs that share runtime.job_store.
            "kind": "digital_worker",
            "source": submission.source,
            "title": _dw_submission_title(submission),
            "external_id": external_id,
            "dry_run": submission.dry_run,
            "submitted_at": time.time(),
            "status": "pending",
            "progress": 0,
            "message": f"Queued: {submission.source} request workflow",
            "result": None,
            "error": None,
            "started_at": None,
            "completed_at": None,
        }
    runtime.submit_with_context(_run_digital_worker_task, job_id, submission)

    if submission.source == "teams":
        # Teams requires a specific Bot Framework JSON schema to avoid showing an error in the
        # channel
        return JSONResponse(
            status_code=200,
            content={
                "type": "message",
                "text": (
                    "Digital Worker request accepted! Monitor progress on your dashboard. "
                    f"(Job ID: {job_id})"
                ),
            },
        )

    return JSONResponse(
        status_code=202,
        content={
            "job_id": job_id,
            "status": "accepted",
            "message": f"Request submitted. Poll /jobs/status/{job_id}",
            "poll_url": f"/jobs/status/{job_id}",
        },
    )


@router.post("/requests")
def submit_cloud_request(submission: CloudRequestSubmission) -> JSONResponse:
    """Submit a cloud operations request via the REST API channel.

    Example request:
    {
        "source": "rest_api",
        "payload": {
            "title": "S3 bucket 'acme-logs' is public",
            "description": "Security review flagged public access on the logs bucket.",
            "priority": "P2",
            "resource_id": "acme-logs"
        },
        "dry_run": true
    }
    """
    return _submit_digital_worker_job(submission)


@router.post("/webhooks/{source}/{path_token}")
def receive_webhook_with_path(
    source: str,
    path_token: str,
    payload: dict[str, Any],
    x_chandra_webhook_token: str | None = Header(default=None),
    token: str | None = Query(default=None),
) -> JSONResponse:
    return _process_webhook(source, payload, path_token, x_chandra_webhook_token, token)


@router.post("/webhooks/{source}")
def receive_webhook(
    source: str,
    payload: dict[str, Any],
    x_chandra_webhook_token: str | None = Header(default=None),
    token: str | None = Query(default=None),
) -> JSONResponse:
    return _process_webhook(source, payload, None, x_chandra_webhook_token, token)


def _process_webhook(
    source: str,
    payload: dict[str, Any],
    path_token: str | None = None,
    x_chandra_webhook_token: str | None = None,
    token: str | None = None,
) -> JSONResponse:
    logger.info(f"Received webhook from {source} with payload: {json.dumps(payload)}")
    """Omnichannel webhook intake: jira | slack | teams | email | monitoring |
    cloudwatch | azure_monitor | gcp_monitoring | webhook.

    When the CHANDRA_WEBHOOK_TOKEN environment variable is set, callers
    must send the same value in the X-Chandra-Webhook-Token header;
    unset means unauthenticated intake (development only).
    """
    expected_token = os.getenv("CHANDRA_WEBHOOK_TOKEN")

    # Handle Slack's URL verification challenge
    if source == "slack" and payload.get("type") == "url_verification":
        return JSONResponse(status_code=200, content={"challenge": payload.get("challenge")})

    provided_token = x_chandra_webhook_token or token or path_token
    if expected_token and provided_token != expected_token:
        logger.warning(
            "Webhook rejected: bad or missing X-Chandra-Webhook-Token (source=%s)", source
        )
        return JSONResponse(
            status_code=401,
            content={
                "status": "error",
                "message": "invalid or missing X-Chandra-Webhook-Token",
            },
        )
    dry_run = payload.get("dry_run", False)
    return _submit_digital_worker_job(
        CloudRequestSubmission(source=source, payload=payload, dry_run=dry_run)
    )


@router.post("/requests/{job_id}/approve")
def approve_cloud_request(job_id: str, approval: ApprovalSubmission) -> JSONResponse:
    """Approve or reject a workflow paused at the human approval gate (or attach permissions)."""
    with runtime.job_store_lock:
        job = runtime.job_store.get(job_id)
        if job is None:
            return JSONResponse(status_code=404, content={"error": "Job not found"})
        if job.get("status") not in ["awaiting_approval", "awaiting_permission", "awaiting_gate2"]:
            return JSONResponse(
                status_code=409,
                content={
                    "error": (
                        f"Job is '{job.get('status')}', not "
                        "awaiting_approval/awaiting_permission/awaiting_gate2"
                    ),
                },
            )
    runtime.submit_with_context(_resume_digital_worker_task, job_id, approval)
    return JSONResponse(
        status_code=202,
        content={
            "job_id": job_id,
            "status": "accepted",
            "message": f"Approval decision submitted. Poll /jobs/status/{job_id}",
            "poll_url": f"/jobs/status/{job_id}",
        },
    )


@router.get("/requests")
async def list_cloud_requests(status: str | None = Query(default=None)) -> JSONResponse:
    """List Digital Worker requests for the Human Approval Center.

    Optional ``?status=`` filter (e.g. ``awaiting_approval``, ``running``,
    ``completed``, ``failed``). Results are newest-first. This is the
    discovery endpoint the approval center polls — no job_id needed.
    """
    # Acquire the lock once for both the item list and the counts so the
    # HTTP thread holds it for the shortest possible time (single acquisition
    # instead of two, reducing contention with background worker threads).
    with runtime.job_store_lock:
        items = [
            _dw_request_summary(job_id, job)
            for job_id, job in runtime.job_store.items()
            if job.get("kind") == "digital_worker"
            and (status is None or job.get("status") == status)
        ]
        counts: dict[str, int] = {}
        for job in runtime.job_store.values():
            if job.get("kind") == "digital_worker":
                key = str(job.get("status"))
                counts[key] = counts.get(key, 0) + 1
    items.sort(key=lambda row: row.get("submitted_at") or 0, reverse=True)
    return JSONResponse(
        status_code=200,
        content={
            "status": "ok",
            "count": len(items),
            "counts": counts,
            "requests": items,
        },
    )


@router.get("/requests/{job_id}")
def get_cloud_request(job_id: str) -> JSONResponse:
    """Full detail for one Digital Worker request (approval payload +
    terminal workflow result when complete)."""
    with runtime.job_store_lock:
        job = runtime.job_store.get(job_id)
        if job is None or job.get("kind") != "digital_worker":
            return JSONResponse(
                status_code=404,
                content={
                    "status": "not_found",
                    "message": f"No Digital Worker request with id {job_id}",
                },
            )
        job_copy = dict(job)
    summary = _dw_request_summary(job_id, job_copy)
    return JSONResponse(
        status_code=200,
        content={
            "status": "ok",
            "request": summary,
            "detail": job_copy.get("result"),
            "error": job_copy.get("error"),
        },
    )
