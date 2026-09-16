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
    orchestration_router,
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
# Orchestration response models moved to src/chandra/api/routers/orchestration.py.


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
app.include_router(orchestration_router)


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


# Copilot chat, sandbox management and orchestration live in
# src/chandra/api/routers/orchestration.py.


# =====================================================================
# AWS Tasks and AWS Permissions Implementation
# =====================================================================

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=6001)