"""
FastAPI application for the AWS Observability Agent.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor
from fastapi import FastAPI, Header, HTTPException, Query, Request, WebSocket
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field
import os
import io
import sys
import subprocess
import zipfile
import boto3
from dotenv import load_dotenv

load_dotenv(override=True)
import uvicorn
from digitalworker_agents.observation_agent import (
    AwsObservabilityAgent,
    PipelineResponse,
    DEFAULT_REGION,
)
from digitalworker_agents.analyzer_agent import AnalyzerAgent, AnalyzerPipelineResponse, ActionResult
from digitalworker_agents.aws_execution_agent import ExecutionAgents, PipelineResponse
from tools.aws_cloud_tools.cost_explorer import AWSCostExplorerFetcher
from tools.aws_cloud_tools.metrics_fetcher import CloudWatchMetricsFetcher
from tools.aws_cloud_tools.tool_findings import run_all_detectors, run_predefined_kra_detectors
from copilot_agents.graph import build_graph, chat as copilot_chat
from src.chandra.digital_worker.graph import build_digital_worker_graph
from src.chandra.digital_worker.intake import SUPPORTED_SOURCES
from src.chandra.governance import (
    AuthorizationError,
    Capability,
    PolicyRule,
    PolicyRuleStore,
    Principal,
    RbacEngine,
    Role,
    RoleAssignmentStore,
    RolesUnavailableError,
)
from src.chandra.observability import correlation
from src.chandra import security
from src.chandra.api import WebSocketManager
from src.chandra.api import deps, graphs, runtime
from src.chandra.api.models import ActionInput
from src.chandra.api.logbuffer import log_buffer
from src.chandra.memory.cache import get_cache
from src.chandra.api.routers import (
    catalog_router,
    governance_router,
    intake_router,
    jobs_router,
    scans_router,
)
from src.chandra.config import settings
from src.chandra.security.ratelimit import RateLimiter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("fastapi_app")

# The in-memory log buffer lives in src/chandra/api/logbuffer.py so the /logs
# endpoint can be served from a router while this handler keeps filling it.

# Job tracking, the worker pool, the background loop and live fan-out now live
# in src/chandra/api/runtime.py, so routers can reach them without importing
# this module. Aliased here for the endpoints still defined below.
_job_store = runtime.job_store
_job_store_lock = runtime.job_store_lock
_thread_pool = runtime.thread_pool
_thread_local = runtime.thread_local
_bg_loop = runtime.bg_loop
ws_manager = runtime.ws_manager
_submit_with_context = runtime.submit_with_context
_publish_job_state = runtime.publish_job_state
JobStoreDict = runtime.JobStoreDict
JobStoreManager = runtime.JobStoreManager



def _run_async(coro) -> Any:
    """Run an async coroutine on the shared background event loop and block until done."""
    future = asyncio.run_coroutine_threadsafe(coro, _bg_loop)
    return future.result()  # blocks the calling thread until coroutine completes


class LogCapture(logging.Handler):
    """Custom handler to capture logs into memory buffer"""
    def emit(self, record: logging.LogRecord) -> None:
        log_entry = {
            "timestamp": record.created,
            "level": record.levelname,
            "logger": record.name,
            "message": self.format(record),
            "job_id": getattr(_thread_local, "job_id", None),
            "correlation_id": correlation.get_correlation_id(),
        }
        log_buffer.append(log_entry)

# Add custom handler to root logger
log_capture = LogCapture()
log_capture.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s"))
logging.getLogger().addHandler(log_capture)

# Add StreamHandler for terminal output, but filter out uvicorn access logs to prevent spam
class UvicornFilter(logging.Filter):
    def filter(self, record):
        return not record.name.startswith("uvicorn")

console_handler = logging.StreamHandler(sys.stderr)
console_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s"))
console_handler.addFilter(UvicornFilter())
logging.getLogger().addHandler(console_handler)

# Job models
class JobStatusResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")  # Ignore extra fields from job dict

    job_id: str
    status: str  # "pending", "running", "completed", "failed", "stopped"
    progress: int = 0  # 0-100
    message: str = ""
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    sandbox_path: Optional[str] = None

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

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    security.validate_auth_configuration()
    from src.chandra.config import settings
    provider = (settings.llm_provider or "bedrock").strip().lower()
    
    logger.info("=== STARTUP: LLM CONFIGURATION ===")
    logger.info("LLM provider = %s", provider)
    if provider == "bedrock":
        logger.info("Model ID = %s", settings.bedrock_model_id)
        logger.info("Region/endpoint = %s", settings.aws_default_region)
    elif provider in ("openai", "openai_compatible", "vllm"):
        model_id = settings.vllm_model or settings.openai_model_name
        endpoint = settings.vllm_api_base or settings.openai_api_base
        logger.info("Model ID = %s", model_id)
        logger.info("Region/endpoint = %s", endpoint)
    else:
        logger.info("Model ID = %s", settings.ollama_model)
        logger.info("Region/endpoint = %s", settings.ollama_host)
    logger.info("==================================")
    
    yield

    # Stop background tasks gracefully to prevent dangling threads during app teardown
    # This prevents 'cannot schedule new futures' and 'I/O operation on closed file' in pytest
    logger.info("Shutting down background thread pool...")
    _thread_pool.shutdown(wait=True)
    
    logger.info("Shutting down background async loop...")
    try:
        _bg_loop.call_soon_threadsafe(_bg_loop.stop)
        runtime.bg_loop_thread.join(timeout=5.0)
    except Exception as e:
        logger.error(f"Error shutting down background loop: {e}")

app = FastAPI(
    title="AWS Observability Agent API",
    description="Runs the KRA-aligned AWS observability pipeline and returns a structured report.",
    version="1.0.0",
    lifespan=lifespan,
)

# Configure CORS for frontend access
allowed_origins = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    os.getenv("FRONTEND_URL", ""),  # Production frontend domain from env var
]
allowed_origins = [origin for origin in allowed_origins if origin]  # Remove empty strings

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins if allowed_origins else ["*"],  # Allow all if no specific origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Paths reachable without a token: liveness/readiness must answer before auth is
# configured, and the docs are how an operator discovers the auth requirement.
_PUBLIC_PATHS = frozenset(
    {"/health/live", "/health/ready", "/docs", "/redoc", "/openapi.json", "/"}
)


def _identity_from_request(request: Request) -> tuple[str | None, str]:
    """Resolve (principal_id, tenant_id) for this request.

    With auth enabled both come from verified JWT claims and the headers are
    ignored entirely — otherwise a caller could authenticate as themselves and
    then assert someone else's principal in a header.
    """
    if security.auth_enabled():
        identity = security.decode_token(security.bearer_token(request.headers.get("Authorization")) or "")
        return identity.principal_id, identity.tenant_id
    return (
        request.headers.get(PRINCIPAL_HEADER),
        correlation.sanitize(
            request.headers.get(correlation.TENANT_HEADER), fallback=correlation.DEFAULT_TENANT
        ),
    )


@app.middleware("http")
async def _edge_middleware(request: Request, call_next):
    """PRD L2 stage 2 + 26.11: authenticate, rate limit, and bind correlation.

    Order matters. Correlation is bound first so an auth failure is still
    traceable; rate limiting runs before authentication so an unauthenticated
    flood is cheap to shed.
    """
    cid = correlation.sanitize(
        request.headers.get(correlation.CORRELATION_HEADER),
        fallback=correlation.new_correlation_id(),
    )
    path = request.url.path
    public = path in _PUBLIC_PATHS

    try:
        if not public:
            allowed, retry_after = rate_limiter.check(_rate_limit_key(request))
            if not allowed:
                correlation.bind(cid, correlation.DEFAULT_TENANT)
                logger.warning("edge.rate_limited path=%s", path)
                response = JSONResponse(
                    status_code=429,
                    content={"status": "error", "detail": "Rate limit exceeded"},
                )
                response.headers["Retry-After"] = str(retry_after)
                response.headers[correlation.CORRELATION_HEADER] = cid
                return response

        try:
            principal_id, tenant_id = (None, correlation.DEFAULT_TENANT) if public else _identity_from_request(request)
        except security.AuthError as exc:
            correlation.bind(cid, correlation.DEFAULT_TENANT)
            logger.warning("edge.unauthenticated path=%s reason=%s", path, exc)
            response = JSONResponse(
                status_code=401,
                content={"status": "error", "detail": str(exc)},
                headers={"WWW-Authenticate": "Bearer"},
            )
            response.headers[correlation.CORRELATION_HEADER] = cid
            return response

        correlation.bind(cid, tenant_id)
        _thread_local.principal_id = principal_id
        request.state.principal_id = principal_id
        response = await call_next(request)
    finally:
        correlation.clear()
        _thread_local.principal_id = None
    response.headers[correlation.CORRELATION_HEADER] = cid
    return response


# Both compiled graphs live in src/chandra/api/graphs.py so routers can reach
# them without importing this module. Built once: the Digital Worker
# checkpointer keys paused approvals by thread_id == job_id, so rebuilding
# between a submission and its approval would orphan every in-flight interrupt.
graphs.init_copilot_agent()
graphs.init_digital_worker()
_copilot_agent = graphs.copilot_agent
_digital_worker = graphs.digital_worker


# Scan/metrics request models moved to src/chandra/api/routers/scans.py.


@app.get("/health")
def health():
    """Liveness probe: the process is up and serving. No dependencies checked."""
    return {"status": "ok"}


@app.get("/health/ready")
def health_ready():
    """Readiness probe: per-component status for orchestrators and dashboards.

    Reports each dependency independently so a degraded component (e.g.
    Postgres down → audit rows lost but workflows still run) is visible
    without flapping the whole service. Returns 200 with status "ok" when
    every component is up, 503 with status "degraded" otherwise.
    """
    components: Dict[str, str] = {}

    components.update(graphs.component_status())

    try:
        from sqlalchemy import text as _sql_text
        from src.chandra.db.session import get_engine

        with get_engine().connect() as conn:
            conn.execute(_sql_text("SELECT 1"))
        components["postgres"] = "ok"
    except Exception as exc:
        components["postgres"] = f"unavailable: {str(exc)[:120]}"

    # Redis is optional by design: "disabled" is a healthy state, not a degraded
    # one. Only a configured-but-unreachable cache counts against readiness.
    redis_state = get_cache().health()
    components["redis"] = "ok" if redis_state in ("ok", "disabled") else redis_state
    if redis_state == "disabled":
        components["redis"] = "disabled"

    # Reported, never graded: these are configuration postures an operator should
    # be able to see at a glance, not faults. A deployment intentionally running
    # without auth must not fail its own readiness probe.
    posture = {
        "authentication": "enforced" if security.auth_enabled() else "disabled",
        "rate_limiting": (
            f"{settings.rate_limit_per_minute}/min"
            if settings.rate_limit_per_minute
            else "disabled"
        ),
        "durable_checkpointer_required": settings.require_durable_checkpointer,
        "semantic_memory": "on" if settings.semantic_memory_enabled else "off",
    }

    degraded = [
        name
        for name, state in components.items()
        if state not in ("ok", "disabled")
    ]
    status_code = 200 if not degraded else 503
    return JSONResponse(status_code=status_code, content={
        "status": "ok" if not degraded else "degraded",
        "components": components,
        "posture": posture,
    })


# ── Digital Worker configuration (Postgres system of record, PRD §26.8) ──────
# aws_tasks / permission_sets / custom_kras / tenant_settings are read and
# written only through ConfigRepository. Fresh installs load the packaged
# catalogue with `chandra catalog seed`; migrating an existing deployment's
# JSON files: `chandra catalog seed --from <dir>`.
from src.chandra.catalog import DEFAULT_TENANT, DIGITAL_WORKER_SETTINGS_KEY, ConfigRepository



PRINCIPAL_HEADER = "X-Principal-ID"

rate_limiter = RateLimiter(settings.rate_limit_per_minute)


def _rate_limit_key(request: Request) -> str:
    """Per-principal when we know who is calling, per-source-IP otherwise."""
    principal = request.headers.get(PRINCIPAL_HEADER)
    if principal:
        return f"principal:{principal}"
    client = request.client
    return f"ip:{client.host if client else 'unknown'}"


# RBAC and configuration access live in src/chandra/api/deps.py, shared with the
# extracted routers. These wrappers exist so the remaining endpoints in this
# module keep working during the incremental split; they delegate rather than
# duplicate, so there is exactly one seam for tests to redirect.


def _require_capability(request: Request, capability: Capability) -> Principal:
    return deps.require_capability(request, capability)


def _config_repo(tenant_id: str | None = None) -> ConfigRepository:
    return deps.config_repo(tenant_id)


# Catalogue and settings endpoints live in src/chandra/api/routers/catalog.py.


# Routers extracted from this module (PRD L2 stage 2 decomposition).
app.include_router(governance_router)
app.include_router(catalog_router)
app.include_router(jobs_router)
app.include_router(scans_router)
app.include_router(intake_router)


# Detector scans, cloud metrics and action analysis live in
# src/chandra/api/routers/scans.py.


class CopilotRequest(BaseModel):
    sessionId: str = Field(  # noqa: N815 - existing console wire contract
        description="Conversation thread ID - reuse to retain memory across turns"
    )
    message: str = Field(description="User message to the copilot agent")


class CopilotResponse(BaseModel):
    sessionId: str  # noqa: N815 - existing console wire contract
    reply: str


@app.post("/copilot/chat", response_model=CopilotResponse)
def copilot_chat_endpoint(request: CopilotRequest):
    """
    Example request:
    {
        "sessionId": "session-abc123",
        "message": "What were the top 3 cost drivers in my AWS account last week?"
    }
    """
    logger.info("POST /copilot/chat sessionId=%s", request.sessionId)
    try:
        reply = copilot_chat(_copilot_agent, request.sessionId, request.message)
        return JSONResponse(status_code=200, content={"sessionId": request.sessionId, "reply": reply})
    except Exception as exc:
        logger.exception("Copilot chat failed: %s", exc)
        return JSONResponse(status_code=500, content={"status": "error", "exception": str(exc)})



class OrchestrateRequest(BaseModel):
    action: ActionInput = Field(description="Action to generate and execute")
    sandbox_path: Optional[str] = Field(
        default=None,
        description="Path to an existing sandbox folder. If provided, the orchestrator updates existing files.",
    )
    reference_folder: Optional[str] = Field(
        default=None,
        description="Path to folder containing reference code (style, patterns, best practices) for consistent code generation.",
    )
    thread_id: Optional[str] = Field(
        default=None,
        description="Thread ID from a previous needs_clarification response.",
    )
    answers: Optional[List[str]] = Field(
        default=None,
        description="Answers to clarification questions from a previous response.",
    )
    command_timeout: int = Field(
        default=300,
        description="Per-command timeout in seconds (default: 300 = 5 minutes).",
    )
    jiraUrl: Optional[str] = Field(
        default=None,
        description="Full Jira URL to post final summary comment to after orchestration completes.",
    )
    max_iterations: int = Field(
        default=5,
        description="Maximum number of generate-execute iterations (default: 5).",
    )
    aws_permissions: Optional[List[str]] = Field(
        default=None,
        description="List of AWS permissions selected during onboarding.",
    )


@app.get("/download_sandbox")
def download_sandbox(path: str):
    """Zip and download the sandbox directory for a completed job."""
    if not path or not os.path.exists(path):
        return JSONResponse(status_code=404, content={"error": "Sandbox not found"})
        
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
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
        headers={"Content-Disposition": "attachment; filename=execution_artifacts.zip"}
    )

class DestroyRequest(BaseModel):
    path: str
    job_id: Optional[str] = None
    jiraUrl: Optional[str] = None

@app.post("/destroy_sandbox")
def destroy_sandbox(request: DestroyRequest):
    """Run terraform destroy on a completed sandbox directory."""
    if request.job_id:
        _thread_local.job_id = request.job_id
        
    if not request.path or not os.path.exists(request.path):
        return JSONResponse(status_code=404, content={"error": "Sandbox not found"})
        
    script_path = os.path.join(os.path.dirname(__file__), "scripts", "destroy_terraform.py")
    
    cmd = [sys.executable, script_path, request.path]
    if request.jiraUrl:
        cmd.extend(["--jiraUrl", request.jiraUrl])
        
    try:
        logger.info(f"Starting infrastructure destruction for: {request.path}")
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace"
        )
        
        output = []
        for line in proc.stdout:
            clean_line = line.rstrip('\\n')
            output.append(clean_line)
            # Log to the parent process logger so it goes to the UI stream
            logger.info(clean_line)
            
        proc.wait()
        full_output = "\\n".join(output)
        
        if proc.returncode != 0:
            return JSONResponse(status_code=500, content={"error": "Destroy failed", "details": full_output})
        return JSONResponse(status_code=200, content={"status": "success", "message": full_output})
    except Exception as e:
        logger.exception("Failed to destroy sandbox at %s: %s", request.path, e)
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.post("/delete_sandbox")
def delete_sandbox(request: DestroyRequest):
    """Delete a sandbox folder without running terraform destroy."""
    import shutil
    try:
        path = Path(request.path)
        if not path.exists() or not path.is_dir():
            return JSONResponse(status_code=404, content={"error": f"Directory not found: {request.path}"})
        logger.info("Deleting sandbox folder: %s", request.path)
        shutil.rmtree(str(path), ignore_errors=True)
        logger.info("Sandbox folder deleted: %s", request.path)
        return JSONResponse(status_code=200, content={"status": "success", "message": f"Deleted {request.path}"})
    except Exception as e:
        logger.exception("Failed to delete sandbox: %s", e)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/orchestrate/stop/{job_id}")
def stop_orchestration(job_id: str):
    import time
    import ctypes
    import shutil
    from digitalworker_agents.aws_execution_agent import cancel_thread_execution

    with _job_store_lock:
        if job_id not in _job_store:
            return JSONResponse(status_code=404, content={"error": "Job not found"})
        
        current_status = _job_store[job_id].get("status")
        
        # Check if the job is paused for HITL (Execution Agents pause)
        is_hitl = False
        if current_status == "completed":
            res = _job_store[job_id].get("result") or {}
            if res.get("status") == "needs_clarification":
                is_hitl = True
                
        # If the job is already in a terminal state, the thread has been released.
        terminal_states = ["failed", "exhausted", "stopped", "destroyed"]
        if not is_hitl:
            terminal_states.append("completed")

        if current_status in terminal_states:
            return JSONResponse(status_code=200, content={"status": "already_finished", "message": f"Job is already {current_status}"})
            
        thread_id = _job_store[job_id].get("thread_id")
        sandbox_path = _job_store[job_id].get("sandbox_path")
        # Mark stopped FIRST so any exception handler won't overwrite it
        _job_store[job_id]["status"] = "stopped"
        _job_store[job_id]["message"] = "Execution stopped by user"
        _job_store[job_id]["completed_at"] = time.time()

    # Kill active terraform/shell subprocesses
    if thread_id:
        cancel_thread_execution(thread_id)

    # Auto-delete sandbox folder in a background thread so we don't block the response
    def _delete_sandbox(path: str):
        try:
            import time as _time
            _time.sleep(2)  # Small delay to let the process fully die before deleting
            if Path(path).exists():
                shutil.rmtree(path, ignore_errors=True)
                logger.info("Auto-deleted sandbox folder after stop: %s", path)
        except Exception as e:
            logger.warning("Failed to auto-delete sandbox %s: %s", path, e)

    if sandbox_path:
        _submit_with_context(_delete_sandbox, sandbox_path)

    return JSONResponse(status_code=200, content={"status": "success"})



@app.post("/orchestrate", response_model=OrchestrateJobResponse)
def orchestrate_action(request: OrchestrateRequest):
    """
    Submit a long-running orchestration job. Returns immediately with a job_id.
    Poll /orchestrate/status/{job_id} to get progress and results.

    Example request:
    {
        "action": {
            "actionName": "Deploy RDS Instance with Terraform",
            "actionDescription": "Deploy a production PostgreSQL RDS instance with encryption enabled.",
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
    
    runtime.register_job(
        job_id, "Waiting to start", sandbox_path=request.sandbox_path or None
    )
    
    # Submit to thread pool
    _submit_with_context(
        _run_orchestration_task,
        job_id,
        request
    )
    
    return OrchestrateJobResponse(
        job_id=job_id,
        status="accepted",
        message=f"Job {job_id} submitted. Poll /orchestrate/status/{job_id} for progress.",
        poll_url=f"/orchestrate/status/{job_id}"
    )

class ResumeRequest(BaseModel):
    answers: List[str] = Field(default_factory=list, description="User's answers to the HITL questions")
    permission_set_id: Optional[str] = Field(default=None, description="Optional permission set ID for approval")


@app.post("/orchestrate/{job_id}/resume", response_model=OrchestrateJobResponse)
def resume_orchestration(job_id: str, request: ResumeRequest):
    """
    Resume a paused HITL job using the SAME job_id.
    Handles two job types:
    - 'dw'  : Digital Worker graph job (Jira webhook). Resumes the DW LangGraph.
    - 'kra' : Direct Execution Agent job (/orchestrate). Resumes RunPipeline.
    """
    with _job_store_lock:
        if job_id not in _job_store:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
        job = dict(_job_store[job_id])  # snapshot inside lock
        result = job.get("result") or {}
        
        is_paused = (
            result.get("status") in ("needs_clarification", "awaiting_gate2_review") or
            result.get("action_required") == "awaiting_permission_set"
        )
        
        if not is_paused:
            raise HTTPException(
                status_code=400,
                detail=f"Job {job_id} is not paused for HITL (current state={result})"
            )
        # Reset to running — same job_id, no new entry
        _job_store[job_id]["status"] = "running"
        _job_store[job_id]["progress"] = 5
        _job_store[job_id]["message"] = "Resuming with your answers..."
        _job_store[job_id]["result"] = None
        _job_store[job_id]["completed_at"] = None

    job_type = job.get("job_type", "kra")  # default to kra for backwards compat
    sandbox_path = job.get("sandbox_path")
    stored_action = job.get("action_dict", {})
    answers = request.answers

    logger.info(
        "POST /orchestrate/%s/resume | job_type=%s | answers=%s",
        job_id, job_type, answers
    )

    def _run_dw_resume():
        """Resume a Digital Worker graph that is paused at execute_automation HITL."""
        import time
        start_time = time.time()
        _thread_local.job_id = job_id
        try:
            with _job_store_lock:
                _job_store[job_id]["thread_id"] = threading.get_ident()

            if _digital_worker is None:
                raise RuntimeError("Digital Worker graph is not initialized")

            # Resume the DW graph with the user's answers or permission_set_id as the interrupt value
            from langgraph.types import Command as LGCommand
            
            resume_payload: Any = answers
            if request.permission_set_id:
                resume_payload = {"permission_set_id": request.permission_set_id}
            elif result.get("status") == "awaiting_gate2_review":
                is_approved = bool(answers and answers[0].lower() == "approved")
                resume_payload = {
                    "approved": is_approved,
                    "approver": "human",
                    "comment": "Approved via UI" if is_approved else "Rejected via UI"
                }
                
            final_state = _digital_worker.invoke(
                LGCommand(resume=resume_payload),
                config=_dw_thread_config(job_id),
            )

            snapshot = _digital_worker.get_state(_dw_thread_config(job_id))
            
            if snapshot.next and "permission_selection_pause" in snapshot.next:
                interrupts = snapshot.tasks[0].interrupts if snapshot.tasks else []
                interrupt_val = interrupts[0].value if interrupts else {}
                with _job_store_lock:
                    _job_store[job_id]["status"] = "awaiting_permission"
                    _job_store[job_id]["progress"] = 75
                    _job_store[job_id]["message"] = "Awaiting Copilot permission attachment"
                    _job_store[job_id]["result"] = interrupt_val
                logger.info("DW RESUME [%s] awaiting permission attachment", job_id)
                return

            if snapshot.next and "gate_2_review" in snapshot.next:
                interrupts = snapshot.tasks[0].interrupts if snapshot.tasks else []
                interrupt_val = interrupts[0].value if interrupts else {}
                with _job_store_lock:
                    _job_store[job_id]["status"] = "awaiting_gate2"
                    _job_store[job_id]["progress"] = 85
                    _job_store[job_id]["message"] = "Awaiting Gate 2 human execution review"
                    _job_store[job_id]["job_type"] = "dw"
                    _job_store[job_id]["result"] = {
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
                questions = interrupt_val.get("questions", ["Please provide the required input to proceed."])
                summary = interrupt_val.get("summary", "Awaiting input")
                with _job_store_lock:
                    _job_store[job_id]["status"] = "completed"
                    _job_store[job_id]["progress"] = 100
                    _job_store[job_id]["message"] = "Awaiting further user input"
                    _job_store[job_id]["job_type"] = "dw"
                    _job_store[job_id]["result"] = {
                        "statusCode": 202,
                        "status": "needs_clarification",
                        "thread_id": job_id,
                        "questions": questions,
                        "summary": summary,
                    }
                return

            with _job_store_lock:
                if _job_store[job_id].get("status") == "stopped":
                    return
            _dw_finalize_job(job_id, final_state, start_time)
            logger.info("DW RESUME [%s] completed in %.1fs", job_id, time.time() - start_time)

        except Exception as exc:
            logger.exception("DW RESUME [%s] failed", job_id)
            with _job_store_lock:
                if _job_store[job_id].get("status") != "stopped":
                    _job_store[job_id]["status"] = "failed"
                    _job_store[job_id]["error"] = str(exc)
                    _job_store[job_id]["message"] = f"Resume failed: {str(exc)[:200]}"
        finally:
            _thread_local.job_id = None

    def _run_execution_resume():
        """Resume a direct Execution Agent job (AWS Task or KRA)."""
        import time
        start_time = time.time()
        with _job_store_lock:
            _stored = _job_store.get(job_id, {})
        correlation.bind(_stored.get("correlation_id") or job_id, _stored.get("tenant_id"))
        exec_thread_id = f"exec-{job_id}"
        try:
            with _job_store_lock:
                _job_store[job_id]["thread_id"] = threading.get_ident()
                aws_permissions = _job_store[job_id].get("aws_permissions", [])

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
                aws_permissions=aws_permissions
            )

            with _job_store_lock:
                if _job_store[job_id].get("status") == "stopped":
                    return
                # Another HITL round?
                if response.statusCode == 202:
                    _job_store[job_id]["status"] = "completed"
                    _job_store[job_id]["progress"] = 100
                    _job_store[job_id]["message"] = "Awaiting further user input"
                    _job_store[job_id]["result"] = {
                        "statusCode": 202,
                        "status": "needs_clarification",
                        "thread_id": exec_thread_id,
                        "questions": response.questions or ["Please provide the required input."],
                        "summary": response.summary or "",
                    }
                    return
                # Check status and set it gracefully, 500 = failed
                if response.statusCode >= 400:
                    _job_store[job_id]["status"] = "failed"
                else:
                    _job_store[job_id]["status"] = "completed"
                _job_store[job_id]["progress"] = 100
                _job_store[job_id]["result"] = response.model_dump()
                _job_store[job_id]["completed_at"] = time.time()
                _job_store[job_id]["sandbox_path"] = response.sandbox_path or sandbox_path
                _job_store[job_id]["message"] = (
                    f"Completed in {time.time() - start_time:.1f}s"
                    if response.statusCode == 200
                    else response.summary or "Completed with errors"
                )
            
            job_label = "AWS_TASK" if stored_action.get("action_type") == "AWS_TASK" else "KRA"
            logger.info("%s RESUME [%s] completed | statusCode=%d", job_label, job_id, response.statusCode)
        except Exception as exc:
            job_label = "AWS_TASK" if stored_action.get("action_type") == "AWS_TASK" else "KRA"
            logger.exception("%s RESUME [%s] failed", job_label, job_id)
            with _job_store_lock:
                if _job_store[job_id].get("status") != "stopped":
                    _job_store[job_id]["status"] = "failed"
                    _job_store[job_id]["error"] = str(exc)
                    _job_store[job_id]["message"] = f"Resume failed: {str(exc)[:200]}"

    if job_type == "dw":
        _submit_with_context(_run_dw_resume)
    else:
        _submit_with_context(_run_execution_resume)

    return OrchestrateJobResponse(
        job_id=job_id,
        status="accepted",
        message=f"Job {job_id} resumed. Poll /orchestrate/status/{job_id} for progress.",
        poll_url=f"/orchestrate/status/{job_id}"

    )


@app.get("/orchestrate/status/{job_id}", response_model=JobStatusResponse)

async def get_orchestrate_status(job_id: str):
    """Poll the status of a submitted orchestration job."""
    with _job_store_lock:
        if job_id not in _job_store:
            return JobStatusResponse(
                job_id=job_id,
                status="not_found",
                message="Job ID not found",
                error="No job with this ID exists"
            )
        # Copy inside the lock so we don't race with the task thread modifying the dict
        job = dict(_job_store[job_id])

    return JobStatusResponse(job_id=job_id, **job)

@app.get("/orchestrate/logs/{job_id}")
def download_orchestrate_logs(job_id: str):
    """Download the logs for a specific orchestration job."""
    log_file_path = f"logs/{job_id}.log"
    if not os.path.exists(log_file_path):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=404, content={"error": "Log file not found"})
    from fastapi.responses import FileResponse
    return FileResponse(log_file_path, media_type='text/plain', filename=f"{job_id}.log")

def _run_orchestration_task(job_id: str, request: OrchestrateRequest):
    """Background worker to run orchestration without blocking the API."""
    import time
    start_time = time.time()
    _thread_local.job_id = job_id
    correlation.bind(correlation.get_correlation_id() or job_id, correlation.get_tenant_id())
    with _job_store_lock:
        if job_id in _job_store:
            _job_store[job_id]["correlation_id"] = correlation.get_correlation_id()
            _job_store[job_id]["tenant_id"] = correlation.get_tenant_id()

    try:
        with _job_store_lock:
            # GUARD: if stop was clicked before this thread even started, bail immediately
            if _job_store[job_id].get("status") == "stopped":
                logger.info("ORCHESTRATION TASK [%s] was stopped before it could start", job_id)
                return
            _job_store[job_id]["status"] = "running"
            _job_store[job_id]["message"] = f"Starting orchestration for {request.action.actionName}"
            _job_store[job_id]["started_at"] = start_time
            _job_store[job_id]["progress"] = 5
            # Register thread ID NOW so stop endpoint can target this exact thread
            _job_store[job_id]["thread_id"] = threading.get_ident()

        logger.info("ORCHESTRATION TASK [%s] started", job_id)

        # Check if this is a predefined KRA remediation
        # GUARD: AWS Tasks must NEVER enter this branch. They must always reach
        # the ExecutionAgents LangGraph pipeline below.
        if request.action.detectorId and getattr(request.action, "action_type", None) != "AWS_TASK":
            from src.chandra.graphs.action_nodes.action_executor import action_executor_node
            from src.chandra.graphs.state import ChandraState
            from src.chandra.briefing.schemas import ProposedWrite

            logger.info("ORCHESTRATION TASK [%s] routing to action_executor_node for predefined KRA %s", job_id, request.action.detectorId)
            pw = ProposedWrite(
                action=f"remediate_{request.action.detectorId}",
                target_arn=request.action.resourceArn or getattr(request.action, "resourceId", "") or "unknown-arn",
                region=request.action.region or os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
                payload={},
                requested_by="supervisor",
                justification=request.action.actionDescription,
                risk_level="low",
                severity="medium",
                summary=request.action.actionName
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
                with _job_store_lock:
                    if _job_store[job_id].get("status") == "stopped":
                        return
                    # Status semantics: SKIPPED must never appear as COMPLETED
                    _job_store[job_id]["status"] = final_status if final_status in ["completed", "skipped", "failed", "running", "blocked", "unverified"] else "failed"
                    _job_store[job_id]["progress"] = 100
                    _job_store[job_id]["message"] = output_msg
                    _job_store[job_id]["result"] = {"status": status_val, "output": res}
                    _job_store[job_id]["completed_at"] = time.time()
                
                logger.info("ORCHESTRATION TASK [%s] action_executor_node completed", job_id)
                return

            except Exception as e:
                logger.exception("ORCHESTRATION TASK [%s] action_executor_node failed", job_id)
                with _job_store_lock:
                    _job_store[job_id]["status"] = "failed"
                    _job_store[job_id]["error"] = str(e)
                    _job_store[job_id]["completed_at"] = time.time()
                    _job_store[job_id]["message"] = f"Failed: {str(e)[:200]}"
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

        # Store action_dict and aws_permissions so the /resume endpoint can use it without needing a new request body
        aws_perms = request.aws_permissions or []
        if not aws_perms and request.action.permission_set_id:
            aws_perms = [request.action.permission_set_id]

        with _job_store_lock:
            _job_store[job_id]["action_dict"] = action_dict
            _job_store[job_id]["aws_permissions"] = aws_perms
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
        with _job_store_lock:
            if _job_store[job_id].get("status") == "stopped":
                logger.info("ORCHESTRATION TASK [%s] finished naturally but was already stopped", job_id)
                return

            # ── HITL pause: RunPipeline returns 202 when it needs human input ──
            if response.statusCode == 202:
                exec_thread_id = f"exec-{job_id}"
                _job_store[job_id]["status"] = "completed"
                _job_store[job_id]["progress"] = 100
                _job_store[job_id]["message"] = "Awaiting user input"
                _job_store[job_id]["job_type"] = "kra"  # Resume via _run_kra_resume
                _job_store[job_id]["result"] = {
                    "statusCode": 202,
                    "status": "needs_clarification",
                    "thread_id": exec_thread_id,
                    "questions": response.questions or ["Please provide the required input to proceed."],
                    "hitl_payload": response.hitl_payload,
                    "summary": response.summary or "Agent needs clarification",
                }
                logger.info(
                    "ORCHESTRATION TASK [%s] paused for HITL | exec thread_id=%s",
                    job_id, exec_thread_id
                )
                return

            is_success = response.statusCode == 200
            _job_store[job_id]["status"] = "completed"
            _job_store[job_id]["progress"] = 100
            _job_store[job_id]["result"] = response.model_dump()
            _job_store[job_id]["completed_at"] = time.time()
            _job_store[job_id]["sandbox_path"] = response.sandbox_path or _job_store[job_id].get("sandbox_path")
            _job_store[job_id]["message"] = (
                f"Completed successfully in {_job_store[job_id]['completed_at'] - start_time:.1f}s"
                if is_success else response.summary or "Orchestration completed with errors"
            )

            # Preserve execution artifact inside the sandbox so the /download_sandbox ZIP contains the final result
            if _job_store[job_id].get("sandbox_path"):
                import json
                from pathlib import Path
                sandbox_dir = Path(_job_store[job_id]["sandbox_path"])
                if sandbox_dir.exists() and sandbox_dir.is_dir():
                    artifact_path = sandbox_dir / "execution_result.json"
                    try:
                        with artifact_path.open("w", encoding="utf-8") as af:
                            json.dump({
                                "job_id": job_id,
                                "status": "success" if is_success else "failed",
                                "message": _job_store[job_id]["message"],
                                "action": action_dict,
                                "sandbox_path": str(sandbox_dir),
                                "iterations_used": response.iterations_used,
                                "summary": response.summary,
                                "aws_permissions_used": aws_perms,
                            }, af, indent=2, ensure_ascii=False)
                    except Exception as e:
                        logger.warning("Could not write execution_result.json to sandbox: %s", e)

        logger.info(
            "ORCHESTRATION TASK [%s] completed | statusCode=%d | duration=%.1fs",
            job_id,
            response.statusCode,
            time.time() - start_time
        )

    except (InterruptedError, SystemExit):
        # Both are raised by our stop mechanism — status is already "stopped", do nothing
        logger.info("ORCHESTRATION TASK [%s] was stopped by the user", job_id)
    except BaseException as exc:
        logger.exception("ORCHESTRATION TASK [%s] failed with exception", job_id)
        with _job_store_lock:
            # Only write failed if not already stopped
            if _job_store[job_id].get("status") != "stopped":
                _job_store[job_id]["status"] = "failed"
                _job_store[job_id]["error"] = str(exc)
                _job_store[job_id]["completed_at"] = time.time()
                _job_store[job_id]["message"] = f"Failed: {str(exc)[:200]}"
    finally:
        _thread_local.job_id = None
        # Clean up cancellation state so this thread ID can be safely reused by the pool
        try:
            from digitalworker_agents.aws_execution_agent import cleanup_thread_state
            cleanup_thread_state()
        except Exception:
            pass



# ── Digital Worker: omnichannel request intake ────────────────────────────────
# Requests can arrive from Jira, Slack, Teams, email, monitoring systems
# (CloudWatch / Azure Monitor / GCP), generic webhooks, or this REST API.
# Every source funnels into the same LangGraph workflow:
#   understand → classify → identify platform → collect context → RCA →
#   plan (memory ▸ LLM ▸ deterministic) → risk → decision →
#   execute | guidance → validate → update Jira → notify → audit → persist
# Long runs use the existing async job pattern (poll /jobs/status/{job_id}).


# Digital Worker intake (requests, webhooks, approvals) lives in
# src/chandra/api/routers/intake.py.




# =====================================================================
# AWS Tasks and AWS Permissions Implementation
# =====================================================================

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=6001)