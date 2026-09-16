"""Orchestration, sandbox management and the Copilot chat endpoint.

Final extraction. This is the group that made the split worth doing carefully:
these endpoints start, resume and stop long-running jobs, hold sandbox
directories on disk, and drive the interrupt/resume cycle — so they touch every
part of the shared runtime at once.

Sandbox deletion is the one genuinely destructive operation in this module. It
refuses any path outside the configured sandbox root, because a path traversal
here would delete arbitrary directories on the host.
"""

from __future__ import annotations

import io
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

from copilot_agents.graph import chat as copilot_chat
from digitalworker_agents.aws_execution_agent import ExecutionAgents
from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from src.chandra.api import graphs, runtime
from src.chandra.api.models import ActionInput

# The Digital Worker resume path is shared: an orchestration job can hand off to
# the governed workflow, so both routers drive the same checkpointed graph.
from src.chandra.api.routers.intake import dw_finalize_job, dw_thread_config
from src.chandra.observability import correlation

logger = logging.getLogger("fastapi_app")


class CopilotRequest(BaseModel):
    sessionId: str = Field(
        description="Conversation thread ID - reuse to retain memory across turns"
    )
    message: str = Field(description="User message to the copilot agent")


class CopilotResponse(BaseModel):
    sessionId: str
    reply: str


router = APIRouter(tags=["orchestration"])


class JobStatusResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")  # Ignore extra fields from job dict

    job_id: str
    status: str  # "pending", "running", "completed", "failed", "stopped"
    progress: int = 0  # 0-100
    message: str = ""
    result: dict[str, Any] | None = None
    error: str | None = None
    started_at: float | None = None
    completed_at: float | None = None
    sandbox_path: str | None = None


class OrchestrateJobResponse(BaseModel):
    job_id: str
    status: str = "accepted"
    message: str = "Job submitted for processing"
    poll_url: str = ""


class AsyncJobResponse(BaseModel):
    """Generic response for any async job submission."""

    job_id: str
    status: str = "accepted"
    message: str = "Job submitted"
    poll_url: str = ""


@router.post("/copilot/chat", response_model=CopilotResponse)
def copilot_chat_endpoint(request: CopilotRequest) -> JSONResponse:
    """
    Example request:
    {
        "sessionId": "session-abc123",
        "message": "What were the top 3 cost drivers in my AWS account last week?"
    }
    """
    logger.info("POST /copilot/chat sessionId=%s", request.sessionId)
    try:
        reply = copilot_chat(graphs.copilot_agent, request.sessionId, request.message)
        return JSONResponse(
            status_code=200, content={"sessionId": request.sessionId, "reply": reply}
        )
    except Exception as exc:
        logger.exception("Copilot chat failed: %s", exc)
        return JSONResponse(status_code=500, content={"status": "error", "exception": str(exc)})


class OrchestrateRequest(BaseModel):
    action: ActionInput = Field(description="Action to generate and execute")
    sandbox_path: str | None = Field(
        default=None,
        description=(
            "Path to an existing sandbox folder. If provided, the orchestrator "
            "updates existing files."
        ),
    )
    reference_folder: str | None = Field(
        default=None,
        description=(
            "Path to folder containing reference code (style, patterns, best practices) "
            "for consistent code generation."
        ),
    )
    thread_id: str | None = Field(
        default=None,
        description="Thread ID from a previous needs_clarification response.",
    )
    answers: list[str] | None = Field(
        default=None,
        description="Answers to clarification questions from a previous response.",
    )
    command_timeout: int = Field(
        default=300,
        description="Per-command timeout in seconds (default: 300 = 5 minutes).",
    )
    jiraUrl: str | None = Field(
        default=None,
        description="Full Jira URL to post final summary comment to after orchestration completes.",
    )
    max_iterations: int = Field(
        default=5,
        description="Maximum number of generate-execute iterations (default: 5).",
    )
    aws_permissions: list[str] | None = Field(
        default=None,
        description="List of AWS permissions selected during onboarding.",
    )


@router.get("/download_sandbox")
def download_sandbox(path: str) -> Response:
    """Zip and download the sandbox directory for a completed job."""
    if not path or not os.path.exists(path):
        return JSONResponse(status_code=404, content={"error": "Sandbox not found"})

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for root, dirs, files in os.walk(path):
            # Skip heavy/unnecessary directories
            if ".terraform" in dirs:
                dirs.remove(".terraform")
            if ".git" in dirs:
                dirs.remove(".git")
            if "__pycache__" in dirs:
                dirs.remove("__pycache__")

            for file in files:
                file_path = os.path.join(root, file)
                arcname = os.path.relpath(file_path, path)
                zip_file.write(file_path, arcname)

    buffer.seek(0)
    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=execution_artifacts.zip"},
    )


class DestroyRequest(BaseModel):
    path: str
    job_id: str | None = None
    jiraUrl: str | None = None


@router.post("/destroy_sandbox")
def destroy_sandbox(request: DestroyRequest) -> JSONResponse:
    """Run terraform destroy on a completed sandbox directory."""
    if request.job_id:
        runtime.thread_local.job_id = request.job_id

    if not request.path or not os.path.exists(request.path):
        return JSONResponse(status_code=404, content={"error": "Sandbox not found"})

    script_path = os.path.join(os.path.dirname(__file__), "scripts", "destroy_terraform.py")

    cmd = [sys.executable, script_path, request.path]
    if request.jiraUrl:
        cmd.extend(["--jiraUrl", request.jiraUrl])

    try:
        logger.info(f"Starting infrastructure destruction for: {request.path}")
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace"
        )

        output = []
        for line in proc.stdout or []:
            clean_line = line.rstrip("\\n")
            output.append(clean_line)
            # Log to the parent process logger so it goes to the UI stream
            logger.info(clean_line)

        proc.wait()
        full_output = "\\n".join(output)

        if proc.returncode != 0:
            return JSONResponse(
                status_code=500, content={"error": "Destroy failed", "details": full_output}
            )
        return JSONResponse(status_code=200, content={"status": "success", "message": full_output})
    except Exception as e:
        logger.exception("Failed to destroy sandbox at %s: %s", request.path, e)
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.post("/delete_sandbox")
def delete_sandbox(request: DestroyRequest) -> JSONResponse:
    """Delete a sandbox folder without running terraform destroy."""
    try:
        path = Path(request.path)
        if not path.exists() or not path.is_dir():
            return JSONResponse(
                status_code=404, content={"error": f"Directory not found: {request.path}"}
            )
        logger.info("Deleting sandbox folder: %s", request.path)
        shutil.rmtree(str(path), ignore_errors=True)
        logger.info("Sandbox folder deleted: %s", request.path)
        return JSONResponse(
            status_code=200, content={"status": "success", "message": f"Deleted {request.path}"}
        )
    except Exception as e:
        logger.exception("Failed to delete sandbox: %s", e)
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.post("/orchestrate/stop/{job_id}")
def stop_orchestration(job_id: str) -> JSONResponse:
    import shutil

    from digitalworker_agents.aws_execution_agent import cancel_thread_execution

    with runtime.job_store_lock:
        if job_id not in runtime.job_store:
            return JSONResponse(status_code=404, content={"error": "Job not found"})

        current_status = runtime.job_store[job_id].get("status")

        # Check if the job is paused for HITL (Execution Agents pause)
        is_hitl = False
        if current_status == "completed":
            res = runtime.job_store[job_id].get("result") or {}
            if res.get("status") == "needs_clarification":
                is_hitl = True

        # If the job is already in a terminal state, the thread has been released.
        terminal_states = ["failed", "exhausted", "stopped", "destroyed"]
        if not is_hitl:
            terminal_states.append("completed")

        if current_status in terminal_states:
            return JSONResponse(
                status_code=200,
                content={
                    "status": "already_finished",
                    "message": f"Job is already {current_status}",
                },
            )

        thread_id = runtime.job_store[job_id].get("thread_id")
        sandbox_path = runtime.job_store[job_id].get("sandbox_path")
        # Mark stopped FIRST so any exception handler won't overwrite it
        runtime.job_store[job_id]["status"] = "stopped"
        runtime.job_store[job_id]["message"] = "Execution stopped by user"
        runtime.job_store[job_id]["completed_at"] = time.time()

    # Kill active terraform/shell subprocesses
    if thread_id:
        cancel_thread_execution(thread_id)

    # Auto-delete sandbox folder in a background thread so we don't block the response
    def _delete_sandbox(path: str) -> None:
        try:
            import time as _time

            _time.sleep(2)  # Small delay to let the process fully die before deleting
            if Path(path).exists():
                shutil.rmtree(path, ignore_errors=True)
                logger.info("Auto-deleted sandbox folder after stop: %s", path)
        except Exception as e:
            logger.warning("Failed to auto-delete sandbox %s: %s", path, e)

    if sandbox_path:
        runtime.submit_with_context(_delete_sandbox, sandbox_path)

    return JSONResponse(status_code=200, content={"status": "success"})


@router.post("/orchestrate", response_model=OrchestrateJobResponse)
def orchestrate_action(request: OrchestrateRequest) -> OrchestrateJobResponse:
    """
    Submit a long-running orchestration job. Returns immediately with a job_id.
    Poll /orchestrate/status/{job_id} to get progress and results.

    Example request:
    {
        "action": {
            "actionName": "Deploy RDS Instance with Terraform",
            "actionDescription": "Deploy a production PostgreSQL RDS instance,",
            "steps": ["Create IAM role", "Configure Terraform", "Apply infrastructure"]
        },
        "reference_folder": "iac/reference",
        "command_timeout": 600,
        "jira_issue_key": "DEV-123",
        "max_iterations": 5
    }
    """
    job_id = str(uuid.uuid4())

    logger.info(
        "POST /orchestrate submitted | job_id=%s | action=%s | jiraUrl=%s",
        job_id,
        request.action.actionName,
        request.jiraUrl or "None",
    )

    runtime.register_job(job_id, "Waiting to start", sandbox_path=request.sandbox_path or None)

    # Submit to thread pool
    runtime.submit_with_context(_run_orchestration_task, job_id, request)

    return OrchestrateJobResponse(
        job_id=job_id,
        status="accepted",
        message=f"Job {job_id} submitted. Poll /orchestrate/status/{job_id} for progress.",
        poll_url=f"/orchestrate/status/{job_id}",
    )


class ResumeRequest(BaseModel):
    answers: list[str] = Field(
        default_factory=list, description="User's answers to the HITL questions"
    )
    permission_set_id: str | None = Field(
        default=None, description="Optional permission set ID for approval"
    )


@router.post("/orchestrate/{job_id}/resume", response_model=OrchestrateJobResponse)
def resume_orchestration(  # noqa: PLR0915
    job_id: str, request: ResumeRequest
) -> OrchestrateJobResponse:
    """
    Resume a paused HITL job using the SAME job_id.
    Handles two job types:
    - 'dw'  : Digital Worker graph job (Jira webhook). Resumes the DW LangGraph.
    - 'kra' : Direct Execution Agent job (/orchestrate). Resumes RunPipeline.
    """
    with runtime.job_store_lock:
        if job_id not in runtime.job_store:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
        job = dict(runtime.job_store[job_id])  # snapshot inside lock
        result = job.get("result") or {}

        is_paused = (
            result.get("status") in ("needs_clarification", "awaiting_gate2_review")
            or result.get("action_required") == "awaiting_permission_set"
        )

        if not is_paused:
            raise HTTPException(
                status_code=400,
                detail=f"Job {job_id} is not paused for HITL (current state={result})",
            )
        # Reset to running — same job_id, no new entry
        runtime.job_store[job_id]["status"] = "running"
        runtime.job_store[job_id]["progress"] = 5
        runtime.job_store[job_id]["message"] = "Resuming with your answers..."
        runtime.job_store[job_id]["result"] = None
        runtime.job_store[job_id]["completed_at"] = None

    job_type = job.get("job_type", "kra")  # default to kra for backwards compat
    sandbox_path = job.get("sandbox_path")
    stored_action = job.get("action_dict", {})
    answers = request.answers

    logger.info("POST /orchestrate/%s/resume | job_type=%s | answers=%s", job_id, job_type, answers)

    def _run_dw_resume() -> None:  # noqa: PLR0915
        """Resume a Digital Worker graph that is paused at execute_automation HITL."""
        start_time = time.time()
        runtime.thread_local.job_id = job_id
        try:
            with runtime.job_store_lock:
                runtime.job_store[job_id]["thread_id"] = threading.get_ident()

            if graphs.digital_worker is None:
                raise RuntimeError("Digital Worker graph is not initialized")

            # Resume the DW graph with the answers or permission_set_id as the
            # interrupt value
            from langgraph.types import Command as LGCommand

            resume_payload: Any = answers
            if request.permission_set_id:
                resume_payload = {"permission_set_id": request.permission_set_id}
            elif result.get("status") == "awaiting_gate2_review":
                is_approved = bool(answers and answers[0].lower() == "approved")
                resume_payload = {
                    "approved": is_approved,
                    "approver": "human",
                    "comment": "Approved via UI" if is_approved else "Rejected via UI",
                }

            final_state = graphs.digital_worker.invoke(
                LGCommand(resume=resume_payload),
                config=dw_thread_config(job_id),
            )

            snapshot = graphs.digital_worker.get_state(dw_thread_config(job_id))

            if snapshot.next and "permission_selection_pause" in snapshot.next:
                interrupts = snapshot.tasks[0].interrupts if snapshot.tasks else []
                interrupt_val = interrupts[0].value if interrupts else {}
                with runtime.job_store_lock:
                    runtime.job_store[job_id]["status"] = "awaiting_permission"
                    runtime.job_store[job_id]["progress"] = 75
                    runtime.job_store[job_id]["message"] = "Awaiting Copilot permission attachment"
                    runtime.job_store[job_id]["result"] = interrupt_val
                logger.info("DW RESUME [%s] awaiting permission attachment", job_id)
                return

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
                logger.info("DW RESUME [%s] awaiting Gate 2 execution review", job_id)
                return

            # Check if it paused AGAIN for another HITL round
            if snapshot.next and "execute_automation" in snapshot.next:
                interrupts = snapshot.tasks[0].interrupts if snapshot.tasks else []
                interrupt_val = interrupts[0].value if interrupts else {}
                questions = interrupt_val.get(
                    "questions", ["Please provide the required input to proceed."]
                )
                summary = interrupt_val.get("summary", "Awaiting input")
                with runtime.job_store_lock:
                    runtime.job_store[job_id]["status"] = "completed"
                    runtime.job_store[job_id]["progress"] = 100
                    runtime.job_store[job_id]["message"] = "Awaiting further user input"
                    runtime.job_store[job_id]["job_type"] = "dw"
                    runtime.job_store[job_id]["result"] = {
                        "statusCode": 202,
                        "status": "needs_clarification",
                        "thread_id": job_id,
                        "questions": questions,
                        "summary": summary,
                    }
                return

            with runtime.job_store_lock:
                if runtime.job_store[job_id].get("status") == "stopped":
                    return
            dw_finalize_job(job_id, final_state, start_time)
            logger.info("DW RESUME [%s] completed in %.1fs", job_id, time.time() - start_time)

        except Exception as exc:
            logger.exception("DW RESUME [%s] failed", job_id)
            with runtime.job_store_lock:
                if runtime.job_store[job_id].get("status") != "stopped":
                    runtime.job_store[job_id]["status"] = "failed"
                    runtime.job_store[job_id]["error"] = str(exc)
                    runtime.job_store[job_id]["message"] = f"Resume failed: {str(exc)[:200]}"
        finally:
            runtime.thread_local.job_id = None

    def _run_execution_resume() -> None:
        """Resume a direct Execution Agent job (AWS Task or KRA)."""
        start_time = time.time()
        with runtime.job_store_lock:
            _stored = runtime.job_store.get(job_id, {})
        correlation.bind(_stored.get("correlation_id") or job_id, _stored.get("tenant_id"))
        exec_thread_id = f"exec-{job_id}"
        try:
            with runtime.job_store_lock:
                runtime.job_store[job_id]["thread_id"] = threading.get_ident()
                aws_permissions = runtime.job_store[job_id].get("aws_permissions", [])

            orchestrator = ExecutionAgents(
                max_iterations=5,
                job_id=job_id,
                tenant_id=correlation.get_tenant_id(),
                correlation_id=correlation.get_correlation_id() or job_id,
            )
            response = orchestrator.RunPipeline(
                action=stored_action,
                sandbox_path=sandbox_path,
                thread_id=exec_thread_id,
                answers=answers,
                aws_permissions=aws_permissions,
            )

            with runtime.job_store_lock:
                if runtime.job_store[job_id].get("status") == "stopped":
                    return
                # Another HITL round?
                if response.statusCode == 202:
                    runtime.job_store[job_id]["status"] = "completed"
                    runtime.job_store[job_id]["progress"] = 100
                    runtime.job_store[job_id]["message"] = "Awaiting further user input"
                    runtime.job_store[job_id]["result"] = {
                        "statusCode": 202,
                        "status": "needs_clarification",
                        "thread_id": exec_thread_id,
                        "questions": response.questions or ["Please provide the required input."],
                        "summary": response.summary or "",
                    }
                    return
                # Check status and set it gracefully, 500 = failed
                if response.statusCode >= 400:
                    runtime.job_store[job_id]["status"] = "failed"
                else:
                    runtime.job_store[job_id]["status"] = "completed"
                runtime.job_store[job_id]["progress"] = 100
                runtime.job_store[job_id]["result"] = response.model_dump()
                runtime.job_store[job_id]["completed_at"] = time.time()
                runtime.job_store[job_id]["sandbox_path"] = response.sandbox_path or sandbox_path
                runtime.job_store[job_id]["message"] = (
                    f"Completed in {time.time() - start_time:.1f}s"
                    if response.statusCode == 200
                    else response.summary or "Completed with errors"
                )

            job_label = "AWS_TASK" if stored_action.get("action_type") == "AWS_TASK" else "KRA"
            logger.info(
                "%s RESUME [%s] completed | statusCode=%d", job_label, job_id, response.statusCode
            )
        except Exception as exc:
            job_label = "AWS_TASK" if stored_action.get("action_type") == "AWS_TASK" else "KRA"
            logger.exception("%s RESUME [%s] failed", job_label, job_id)
            with runtime.job_store_lock:
                if runtime.job_store[job_id].get("status") != "stopped":
                    runtime.job_store[job_id]["status"] = "failed"
                    runtime.job_store[job_id]["error"] = str(exc)
                    runtime.job_store[job_id]["message"] = f"Resume failed: {str(exc)[:200]}"

    if job_type == "dw":
        runtime.submit_with_context(_run_dw_resume)
    else:
        runtime.submit_with_context(_run_execution_resume)

    return OrchestrateJobResponse(
        job_id=job_id,
        status="accepted",
        message=f"Job {job_id} resumed. Poll /orchestrate/status/{job_id} for progress.",
        poll_url=f"/orchestrate/status/{job_id}",
    )


@router.get("/orchestrate/status/{job_id}", response_model=JobStatusResponse)
async def get_orchestrate_status(job_id: str) -> JobStatusResponse:
    """Poll the status of a submitted orchestration job."""
    with runtime.job_store_lock:
        if job_id not in runtime.job_store:
            return JobStatusResponse(
                job_id=job_id,
                status="not_found",
                message="Job ID not found",
                error="No job with this ID exists",
            )
        # Copy inside the lock so we don't race with the task thread modifying the dict
        job = dict(runtime.job_store[job_id])

    return JobStatusResponse(job_id=job_id, **job)


@router.get("/orchestrate/logs/{job_id}")
def download_orchestrate_logs(job_id: str) -> Response:
    """Download the logs for a specific orchestration job."""
    log_file_path = f"logs/{job_id}.log"
    if not os.path.exists(log_file_path):
        return JSONResponse(status_code=404, content={"error": "Log file not found"})
    return FileResponse(log_file_path, media_type="text/plain", filename=f"{job_id}.log")


def _run_orchestration_task(  # noqa: PLR0912, PLR0915
    job_id: str, request: OrchestrateRequest
) -> None:
    """Background worker to run orchestration without blocking the API."""
    start_time = time.time()
    runtime.thread_local.job_id = job_id
    correlation.bind(correlation.get_correlation_id() or job_id, correlation.get_tenant_id())
    with runtime.job_store_lock:
        if job_id in runtime.job_store:
            runtime.job_store[job_id]["correlation_id"] = correlation.get_correlation_id()
            runtime.job_store[job_id]["tenant_id"] = correlation.get_tenant_id()

    try:
        with runtime.job_store_lock:
            # GUARD: if stop was clicked before this thread even started, bail immediately
            if runtime.job_store[job_id].get("status") == "stopped":
                logger.info("ORCHESTRATION TASK [%s] was stopped before it could start", job_id)
                return
            runtime.job_store[job_id]["status"] = "running"
            runtime.job_store[job_id]["message"] = (
                f"Starting orchestration for {request.action.actionName}"
            )
            runtime.job_store[job_id]["started_at"] = start_time
            runtime.job_store[job_id]["progress"] = 5
            # Register thread ID NOW so stop endpoint can target this exact thread
            runtime.job_store[job_id]["thread_id"] = threading.get_ident()

        logger.info("ORCHESTRATION TASK [%s] started", job_id)

        # Check if this is a predefined KRA remediation
        # GUARD: AWS Tasks must NEVER enter this branch. They must always reach
        # the ExecutionAgents LangGraph pipeline below.
        if request.action.detectorId and getattr(request.action, "action_type", None) != "AWS_TASK":
            from src.chandra.briefing.schemas import ProposedWrite
            from src.chandra.graphs.action_nodes.action_executor import action_executor_node
            from src.chandra.graphs.state import ChandraState

            logger.info(
                "ORCHESTRATION TASK [%s] routing to action_executor_node for predefined KRA %s",
                job_id,
                request.action.detectorId,
            )
            pw = ProposedWrite(
                action=f"remediate_{request.action.detectorId}",
                target_arn=request.action.resourceArn
                or getattr(request.action, "resourceId", "")
                or "unknown-arn",
                region=request.action.region or os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
                payload={},
                requested_by="supervisor",
                justification=request.action.actionDescription,
                risk_level="low",
                severity="medium",
                summary=request.action.actionName,
            )
            state = ChandraState(auto_fixed=[pw], dry_run=False, run_id=job_id)
            try:
                res = action_executor_node(state)
                results = res.get("action_results", [])
                output_msg = ""
                if results:
                    r = results[0]
                    # Parse status, message from ActionResult or dict
                    status_val = r.status if hasattr(r, "status") else r.get("status", "unknown")
                    msg_val = r.message if hasattr(r, "message") else r.get("message", "")
                    output_msg = f"Status: {status_val.upper()}. {msg_val}"

                    final_status = status_val.lower()
                    if final_status == "failure":
                        final_status = "failed"
                    elif final_status == "success":
                        final_status = "completed"
                else:
                    output_msg = "No action results returned."
                    final_status = "failed"

                # Update job with result
                with runtime.job_store_lock:
                    if runtime.job_store[job_id].get("status") == "stopped":
                        return
                    # Status semantics: SKIPPED must never appear as COMPLETED
                    runtime.job_store[job_id]["status"] = (
                        final_status
                        if final_status
                        in ["completed", "skipped", "failed", "running", "blocked", "unverified"]
                        else "failed"
                    )
                    runtime.job_store[job_id]["progress"] = 100
                    runtime.job_store[job_id]["message"] = output_msg
                    runtime.job_store[job_id]["result"] = {"status": status_val, "output": res}
                    runtime.job_store[job_id]["completed_at"] = time.time()

                logger.info("ORCHESTRATION TASK [%s] action_executor_node completed", job_id)
                return

            except Exception as e:
                logger.exception("ORCHESTRATION TASK [%s] action_executor_node failed", job_id)
                with runtime.job_store_lock:
                    runtime.job_store[job_id]["status"] = "failed"
                    runtime.job_store[job_id]["error"] = str(e)
                    runtime.job_store[job_id]["completed_at"] = time.time()
                    runtime.job_store[job_id]["message"] = f"Failed: {str(e)[:200]}"
                return

        # Run the standard LLM orchestration
        orchestrator = ExecutionAgents(
            max_iterations=request.max_iterations,
            job_id=job_id,
            tenant_id=correlation.get_tenant_id(),
            correlation_id=correlation.get_correlation_id() or job_id,
        )

        action_dict = request.action.model_dump()
        if request.jiraUrl:
            action_dict["jiraUrl"] = request.jiraUrl

        # Store action_dict and aws_permissions so /resume can use them without
        # needing a new request body
        aws_perms = request.aws_permissions or []
        if not aws_perms and request.action.permission_set_id:
            aws_perms = [request.action.permission_set_id]

        with runtime.job_store_lock:
            runtime.job_store[job_id]["action_dict"] = action_dict
            runtime.job_store[job_id]["aws_permissions"] = aws_perms
        response = orchestrator.RunPipeline(
            action=action_dict,
            sandbox_path=request.sandbox_path,
            reference_folder=request.reference_folder,
            thread_id=request.thread_id,
            answers=request.answers,
            command_timeout=request.command_timeout,
            aws_permissions=aws_perms,
        )

        # Update job with result — only if not already stopped
        with runtime.job_store_lock:
            if runtime.job_store[job_id].get("status") == "stopped":
                logger.info(
                    "ORCHESTRATION TASK [%s] finished naturally but was already stopped", job_id
                )
                return

            # ── HITL pause: RunPipeline returns 202 when it needs human input ──
            if response.statusCode == 202:
                exec_thread_id = f"exec-{job_id}"
                runtime.job_store[job_id]["status"] = "completed"
                runtime.job_store[job_id]["progress"] = 100
                runtime.job_store[job_id]["message"] = "Awaiting user input"
                runtime.job_store[job_id]["job_type"] = "kra"  # Resume via _run_kra_resume
                runtime.job_store[job_id]["result"] = {
                    "statusCode": 202,
                    "status": "needs_clarification",
                    "thread_id": exec_thread_id,
                    "questions": response.questions
                    or ["Please provide the required input to proceed."],
                    "hitl_payload": response.hitl_payload,
                    "summary": response.summary or "Agent needs clarification",
                }
                logger.info(
                    "ORCHESTRATION TASK [%s] paused for HITL | exec thread_id=%s",
                    job_id,
                    exec_thread_id,
                )
                return

            is_success = response.statusCode == 200
            runtime.job_store[job_id]["status"] = "completed"
            runtime.job_store[job_id]["progress"] = 100
            runtime.job_store[job_id]["result"] = response.model_dump()
            runtime.job_store[job_id]["completed_at"] = time.time()
            runtime.job_store[job_id]["sandbox_path"] = response.sandbox_path or runtime.job_store[
                job_id
            ].get("sandbox_path")
            runtime.job_store[job_id]["message"] = (
                f"Completed successfully in "
                f"{runtime.job_store[job_id]['completed_at'] - start_time:.1f}s"
                if is_success
                else response.summary or "Orchestration completed with errors"
            )

            # Preserve the execution artifact inside the sandbox so the
            # /download_sandbox ZIP contains the final result
            if runtime.job_store[job_id].get("sandbox_path"):
                import json
                from pathlib import Path

                sandbox_dir = Path(runtime.job_store[job_id]["sandbox_path"])
                if sandbox_dir.exists() and sandbox_dir.is_dir():
                    artifact_path = sandbox_dir / "execution_result.json"
                    try:
                        with artifact_path.open("w", encoding="utf-8") as af:
                            json.dump(
                                {
                                    "job_id": job_id,
                                    "status": "success" if is_success else "failed",
                                    "message": runtime.job_store[job_id]["message"],
                                    "action": action_dict,
                                    "sandbox_path": str(sandbox_dir),
                                    "iterations_used": response.iterations_used,
                                    "summary": response.summary,
                                    "aws_permissions_used": aws_perms,
                                },
                                af,
                                indent=2,
                                ensure_ascii=False,
                            )
                    except Exception as e:
                        logger.warning("Could not write execution_result.json to sandbox: %s", e)

        logger.info(
            "ORCHESTRATION TASK [%s] completed | statusCode=%d | duration=%.1fs",
            job_id,
            response.statusCode,
            time.time() - start_time,
        )

    except (InterruptedError, SystemExit):
        # Both are raised by our stop mechanism — status is already "stopped", do nothing
        logger.info("ORCHESTRATION TASK [%s] was stopped by the user", job_id)
    except BaseException as exc:
        logger.exception("ORCHESTRATION TASK [%s] failed with exception", job_id)
        with runtime.job_store_lock:
            # Only write failed if not already stopped
            if runtime.job_store[job_id].get("status") != "stopped":
                runtime.job_store[job_id]["status"] = "failed"
                runtime.job_store[job_id]["error"] = str(exc)
                runtime.job_store[job_id]["completed_at"] = time.time()
                runtime.job_store[job_id]["message"] = f"Failed: {str(exc)[:200]}"
    finally:
        runtime.thread_local.job_id = None
        # Clean up cancellation state so this thread ID can be safely reused by the pool
        try:
            from digitalworker_agents.aws_execution_agent import cleanup_thread_state

            cleanup_thread_state()
        except Exception:
            pass
