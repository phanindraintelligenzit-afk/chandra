"""
FastAPI application for the AWS Observability Agent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor
from fastapi import FastAPI, Header, Query, HTTPException
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("fastapi_app")

# In-memory log buffer (keep last 2000 logs for better tracking)
_log_buffer: List[Dict[str, Any]] = []
_max_logs = 2000

# Job tracking for long-running orchestrations
_job_store: Dict[str, Dict[str, Any]] = {}
# Use RLock (reentrant) so background worker threads that already hold the
# lock can re-enter it without deadlocking the FastAPI HTTP threads that
# serve GET /requests and GET /jobs/status while a job is running.
_job_store_lock = threading.RLock()
_thread_pool = ThreadPoolExecutor(max_workers=8)

_thread_local = threading.local()

# ── Shared background event loop ──────────────────────────────────────────────
# Using asyncio.run() in multiple background threads simultaneously creates
# competing event loops that crash uvicorn. Instead, we keep ONE persistent
# event loop running in a dedicated daemon thread and submit all async work
# to it via asyncio.run_coroutine_threadsafe().
_bg_loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()

def _start_bg_loop(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)
    loop.run_forever()

_bg_loop_thread = threading.Thread(
    target=_start_bg_loop, args=(_bg_loop,), daemon=True, name="bg-async-loop"
)
_bg_loop_thread.start()


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
            "job_id": getattr(_thread_local, "job_id", None)
        }
        _log_buffer.append(log_entry)
        # Keep only last 500 logs
        if len(_log_buffer) > _max_logs:
            _log_buffer.pop(0)

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
        _bg_loop_thread.join(timeout=5.0)
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

# Built once so MemorySaver persists across requests (keyed by sessionId / thread_id)
# Wrapped in try/except so FastAPI still starts even if an agent fails to initialize
# (e.g. Bedrock unreachable, Postgres timeout, missing env var)
try:
    _copilot_agent = build_graph()
    logger.info("Copilot agent initialized successfully")
except Exception as _e:
    logger.error("Failed to initialize copilot agent: %s", _e)
    _copilot_agent = None

# Digital Worker request workflow (omnichannel intake). Built once so the
# in-memory checkpointer persists across requests — approval resumes are
# keyed by thread_id == job_id.
try:
    _digital_worker = build_digital_worker_graph()
    logger.info("Digital Worker graph initialized successfully")
except Exception as _e:
    logger.error("Failed to initialize Digital Worker graph: %s", _e)
    _digital_worker = None


class KRAInput(BaseModel):
    code: Optional[str] = Field(default=None, description="Optional KRA identifier (e.g. KRA-01). Auto-labelled if omitted.")
    name: Optional[str] = Field(default=None, description="Optional short name/title for the KRA (e.g. 'Disaster Recovery Drills'). For custom KRAs this is the user-provided kraName.")
    description: str = Field(description="Free-form goal or objective. Can be an observability target (e.g. 'IAM drift monitoring') or any operational task (e.g. 'Deploy code from github.com/org/repo to EC2 in us-east-1').")


class PipelineRequest(BaseModel):
    region: str = Field(default=DEFAULT_REGION, description="AWS region to run the pipeline against")
    kras: List[KRAInput] = Field(description="List of KRAs to evaluate during the observability run")


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

    components["copilot_agent"] = "ok" if _copilot_agent is not None else "unavailable"
    components["digital_worker"] = "ok" if _digital_worker is not None else "unavailable"

    try:
        from sqlalchemy import text as _sql_text
        from src.chandra.db.session import get_engine

        with get_engine().connect() as conn:
            conn.execute(_sql_text("SELECT 1"))
        components["postgres"] = "ok"
    except Exception as exc:
        components["postgres"] = f"unavailable: {str(exc)[:120]}"

    degraded = [name for name, state in components.items() if state != "ok"]
    status_code = 200 if not degraded else 503
    return JSONResponse(status_code=status_code, content={
        "status": "ok" if not degraded else "degraded",
        "components": components,
    })


# ── Custom KRA persistence (customKras.json) ──────────────────────────────────
CUSTOM_KRA_FILE = Path(__file__).parent / "customKras.json"
_custom_kra_lock = threading.Lock()


def _load_custom_kras_from_disk() -> list:
    """Load custom KRAs from the JSON file on disk. Returns an empty list if the file is missing or invalid."""
    try:
        if not CUSTOM_KRA_FILE.exists():
            return []
        with CUSTOM_KRA_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        # Normalize: ensure each entry is { name, description, selected? }
        normalized: list = []
        seen: set = set()
        for entry in data:
            if isinstance(entry, str):
                name = entry.strip()
                if not name:
                    continue
                key = name.lower()
                if key in seen:
                    continue
                seen.add(key)
                normalized.append({"name": name, "description": name, "selected": True})
            elif isinstance(entry, dict):
                name = str(entry.get("name") or entry.get("code") or "").strip()
                if not name:
                    continue
                key = name.lower()
                if key in seen:
                    continue
                seen.add(key)
                description = str(entry.get("description") or entry.get("desc") or name).strip()
                normalized.append(
                    {
                        "name": name,
                        "description": description or name,
                        "selected": entry.get("selected", True),
                    }
                )
        return normalized
    except Exception as exc:
        logger.exception("Failed to read customKras.json: %s", exc)
        return []


def _save_custom_kras_to_disk(entries: list) -> int:
    """Persist custom KRAs to customKras.json. Returns the number of entries written."""
    with _custom_kra_lock:
        # Deduplicate by name (case-insensitive) and re-normalize.
        seen: set = set()
        cleaned: list = []
        for entry in entries:
            if isinstance(entry, str):
                name = entry.strip()
                if not name:
                    continue
                key = name.lower()
                if key in seen:
                    continue
                seen.add(key)
                cleaned.append({"name": name, "description": name, "selected": True})
                continue
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or entry.get("code") or "").strip()
            if not name:
                continue
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            description = str(entry.get("description") or entry.get("desc") or name).strip()
            cleaned.append(
                {
                    "name": name,
                    "description": description or name,
                    "selected": entry.get("selected", True),
                }
            )
        # Atomic write so a partial file is never observed on disk.
        tmp_path = CUSTOM_KRA_FILE.with_suffix(".json.tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(cleaned, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, CUSTOM_KRA_FILE)
        return len(cleaned)


@app.get("/customKras")
def get_custom_kras():
    """Read all custom KRAs persisted in customKras.json."""
    entries = _load_custom_kras_from_disk()
    return JSONResponse(
        status_code=200,
        content={"status": "success", "count": len(entries), "kras": entries},
    )


class CustomKrasPayload(BaseModel):
    kras: List[Dict[str, Any]] = Field(
        description="Full list of custom KRAs to persist. Replaces the contents of customKras.json."
    )


@app.put("/customKras")
def put_custom_kras(payload: CustomKrasPayload):
    """Replace the contents of customKras.json with the supplied list."""
    try:
        written = _save_custom_kras_to_disk(payload.kras)
        return JSONResponse(
            status_code=200,
            content={"status": "success", "count": written, "message": f"Saved {written} custom KRAs"},
        )
    except Exception as exc:
        logger.exception("Failed to write customKras.json: %s", exc)
        return JSONResponse(status_code=500, content={"status": "error", "exception": str(exc)})

@app.get("/logs")
async def get_logs(limit: int = Query(500, ge=1, le=2000), offset: int = Query(0, ge=0)):
    """Get recent backend logs (last 2000 stored in memory)"""
    start = max(0, len(_log_buffer) - limit - offset)
    end = max(0, len(_log_buffer) - offset)
    return JSONResponse(status_code=200, content={"logs": _log_buffer[start:end]})

@app.get("/getDetectorIssues")
def get_detector_issues():
    """Submit detector scan as an async job. Poll /jobs/status/{job_id} for result."""
    job_id = str(uuid.uuid4())
    logger.info("GET /getDetectorIssues → async job_id=%s", job_id)
    with _job_store_lock:
        _job_store[job_id] = {
            "status": "pending", "progress": 0,
            "message": "Queued: detector scan",
            "result": None, "error": None,
            "started_at": None, "completed_at": None,
        }
    _thread_pool.submit(_run_detector_task, job_id)
    return JSONResponse(status_code=202, content={
        "job_id": job_id, "status": "accepted",
        "message": f"Detector scan submitted. Poll /jobs/status/{job_id}",
        "poll_url": f"/jobs/status/{job_id}"
    })

class PredefinedKraRequest(BaseModel):
    selected_kras: List[str] = Field(default_factory=list, description="List of KRA codes/names to run detectors for")

@app.post("/getPredefinedKraIssues")
def get_predefined_kra_issues(request: PredefinedKraRequest):
    """Submit detector scan as an async job for selected KRAs."""
    job_id = str(uuid.uuid4())
    logger.info("POST /getPredefinedKraIssues → async job_id=%s, kras=%s", job_id, request.selected_kras)
    with _job_store_lock:
        _job_store[job_id] = {
            "status": "pending", "progress": 0,
            "message": f"Queued: detector scan for {request.selected_kras}",
            "result": None, "error": None,
            "started_at": None, "completed_at": None,
        }
    _thread_pool.submit(_run_predefined_kra_task, job_id, request.selected_kras)
    return JSONResponse(status_code=202, content={
        "job_id": job_id, "status": "accepted",
        "message": f"Detector scan submitted. Poll /jobs/status/{job_id}",
        "poll_url": f"/jobs/status/{job_id}"
    })

class CostMetricsRequest(BaseModel):
    days_lookback: int = Field(default=7, ge=1, le=365, description="Number of days to look back")
    granularity: str = Field(default="DAILY", description="Cost granularity: DAILY or MONTHLY")

@app.post("/getCostMetrics")
async def get_cost_metrics(request: CostMetricsRequest) -> JSONResponse:
    logger.info("POST /getCostMetrics called with days_lookback=%d, granularity=%s", request.days_lookback, request.granularity)
    try:
        fetcher = AWSCostExplorerFetcher()
        summary: Dict[str, Any] = await fetcher.fetch_costs_summary(days_lookback=request.days_lookback)
        return JSONResponse(status_code=200, content={"status": "success", "output": summary})
    except Exception as exc:
        logger.exception("Cost metrics fetch failed: %s", exc)
        return JSONResponse(status_code=500, content={"status": "error", "exception": str(exc)})

class CloudWatchMetricsRequest(BaseModel):
    region: str = Field(default=os.getenv("AWS_DEFAULT_REGION", "us-east-1"), description="AWS region to fetch metrics from")
    last_hours: int = Field(default=12, description="Hours to look back")
    period: int = Field(default=1200, description="Period in seconds")
    timezone_str: str = Field(default="Asia/Kolkata", description="Timezone for timestamps (e.g. 'Asia/Kolkata', 'US/Eastern')")

@app.get("/aws/regions")
def get_aws_regions() -> JSONResponse:
    """Fetch all available AWS regions dynamically."""
    try:
        session = boto3.Session()
        regions = session.get_available_regions('cloudwatch')
        return JSONResponse(status_code=200, content={"regions": sorted(regions)})
    except Exception as exc:
        logger.exception("Failed to fetch regions: %s", exc)
        return JSONResponse(status_code=500, content={"status": "error", "message": str(exc)})

@app.post("/getCloudWatchMetrics")
def get_cloudwatch_metrics(request: CloudWatchMetricsRequest) -> JSONResponse:
    """Submit CloudWatch metrics fetch as an async job. Poll /jobs/status/{job_id} for result."""
    job_id = str(uuid.uuid4())
    logger.info("POST /getCloudWatchMetrics → async job_id=%s region=%s", job_id, request.region)
    with _job_store_lock:
        _job_store[job_id] = {
            "status": "pending", "progress": 0,
            "message": "Queued: CloudWatch metrics fetch",
            "result": None, "error": None,
            "started_at": None, "completed_at": None,
        }
    _thread_pool.submit(_run_cloudwatch_task, job_id, request)
    return JSONResponse(status_code=202, content={
        "job_id": job_id, "status": "accepted",
        "message": f"CloudWatch fetch submitted. Poll /jobs/status/{job_id}",
        "poll_url": f"/jobs/status/{job_id}"
    })


@app.post("/getAgentObservations")
def run_pipeline(request: PipelineRequest):
    """Submit observability pipeline as an async job. Poll /jobs/status/{job_id} for result."""
    job_id = str(uuid.uuid4())
    logger.info(
        "POST /getAgentObservations → async job_id=%s region=%s kras=%s",
        job_id, request.region, [k.code for k in request.kras],
    )
    with _job_store_lock:
        _job_store[job_id] = {
            "status": "pending", "progress": 0,
            "message": "Queued: AWS observability pipeline",
            "result": None, "error": None,
            "started_at": None, "completed_at": None,
        }
    _thread_pool.submit(_run_observations_task, job_id, request)
    return JSONResponse(status_code=202, content={
        "job_id": job_id, "status": "accepted",
        "message": f"Pipeline submitted. Poll /jobs/status/{job_id}",
        "poll_url": f"/jobs/status/{job_id}"
    })


# ── Generic job status endpoint (shared by all async jobs) ────────────────────
@app.get("/jobs/status/{job_id}")
def get_job_status_generic(job_id: str):
    """Poll the status of any submitted async job."""
    with _job_store_lock:
        if job_id not in _job_store:
            return JSONResponse(status_code=404, content={
                "job_id": job_id, "status": "not_found",
                "message": "No job with this ID exists"
            })
        job = dict(_job_store[job_id])
    return JSONResponse(status_code=200, content={"job_id": job_id, **job})


# ── Background task functions ─────────────────────────────────────────────────

def _run_observations_task(job_id: str, request: PipelineRequest):
    """Background worker for /getAgentObservations."""
    import time
    start_time = time.time()
    _thread_local.job_id = job_id
    try:
        with _job_store_lock:
            _job_store[job_id]["status"] = "running"
            _job_store[job_id]["started_at"] = start_time
            _job_store[job_id]["progress"] = 10
            _job_store[job_id]["message"] = "Initializing AWS agent..."
            _job_store[job_id]["thread_id"] = threading.get_ident()

        agent = AwsObservabilityAgent(region=request.region, kras=request.kras)

        with _job_store_lock:
            _job_store[job_id]["progress"] = 25
            _job_store[job_id]["message"] = "Running 11 AWS tools in parallel..."

        response = agent.RunPipeline()
        elapsed = time.time() - start_time

        with _job_store_lock:
            _job_store[job_id]["status"] = "completed"
            _job_store[job_id]["progress"] = 100
            _job_store[job_id]["result"] = response.model_dump()
            _job_store[job_id]["completed_at"] = time.time()
            _job_store[job_id]["message"] = f"Completed in {elapsed:.1f}s"

        logger.info("OBSERVATIONS JOB [%s] completed in %.1fs", job_id, elapsed)

    except (InterruptedError, SystemExit):
        logger.info("OBSERVATIONS JOB [%s] was stopped by the user", job_id)
        with _job_store_lock:
            if _job_store[job_id].get("status") != "stopped":
                _job_store[job_id]["status"] = "stopped"
                _job_store[job_id]["completed_at"] = time.time()
    except Exception as exc:
        logger.exception("OBSERVATIONS JOB [%s] failed", job_id)
        with _job_store_lock:
            _job_store[job_id]["status"] = "failed"
            _job_store[job_id]["error"] = str(exc)
            _job_store[job_id]["completed_at"] = time.time()
            _job_store[job_id]["message"] = f"Failed: {str(exc)[:200]}"
    finally:
        _thread_local.job_id = None


def _run_detector_task(job_id: str):
    """Background worker for /getDetectorIssues."""
    import time
    start_time = time.time()
    _thread_local.job_id = job_id
    try:
        with _job_store_lock:
            _job_store[job_id]["status"] = "running"
            _job_store[job_id]["started_at"] = start_time
            _job_store[job_id]["progress"] = 10
            _job_store[job_id]["message"] = "Running compliance/security detectors..."
            _job_store[job_id]["thread_id"] = threading.get_ident()

        # Use shared bg loop — avoids competing event loops crashing uvicorn
        selected = [k["name"].lower() for k in _load_custom_kras_from_disk() if k.get("selected")]
        valid_modules = [m for m in ["compliance", "security", "reliability", "performance", "cost"] if m in selected]
        
        if valid_modules:
            findings = _run_async(run_predefined_kra_detectors(valid_modules))
        else:
            findings = {}
            
        if isinstance(findings, dict):
            total_issues = sum(len(g) for g in findings.values())
            output = findings
        else:
            total_issues = len(findings)
            output = [f.model_dump() if hasattr(f, "model_dump") else f for f in findings]

        elapsed = time.time() - start_time
        with _job_store_lock:
            _job_store[job_id]["status"] = "completed"
            _job_store[job_id]["progress"] = 100
            _job_store[job_id]["result"] = {"status": "success", "output": output}
            _job_store[job_id]["completed_at"] = time.time()
            _job_store[job_id]["message"] = f"Found {total_issues} issues in {elapsed:.1f}s"

        logger.info("DETECTOR JOB [%s] found %d issues in %.1fs", job_id, total_issues, elapsed)

    except (InterruptedError, SystemExit):
        logger.info("DETECTOR JOB [%s] was stopped by the user", job_id)
        with _job_store_lock:
            if _job_store[job_id].get("status") != "stopped":
                _job_store[job_id]["status"] = "stopped"
                _job_store[job_id]["completed_at"] = time.time()
    except Exception as exc:
        logger.exception("DETECTOR JOB [%s] failed", job_id)
        with _job_store_lock:
            _job_store[job_id]["status"] = "failed"
            _job_store[job_id]["error"] = str(exc)
            _job_store[job_id]["completed_at"] = time.time()
            _job_store[job_id]["message"] = f"Failed: {str(exc)[:200]}"
    finally:
        _thread_local.job_id = None


def _run_predefined_kra_task(job_id: str, selected_kras: List[str]):
    """Background worker for /getPredefinedKraIssues."""
    import time
    start_time = time.time()
    _thread_local.job_id = job_id
    try:
        with _job_store_lock:
            _job_store[job_id]["status"] = "running"
            _job_store[job_id]["started_at"] = start_time
            _job_store[job_id]["progress"] = 10
            _job_store[job_id]["message"] = f"Running detectors for {selected_kras}..."
            _job_store[job_id]["thread_id"] = threading.get_ident()

        findings = _run_async(run_predefined_kra_detectors(selected_kras))
        if isinstance(findings, dict):
            total_issues = sum(len(g) for g in findings.values())
            output = findings
        else:
            total_issues = len(findings)
            output = [f.model_dump() if hasattr(f, "model_dump") else f for f in findings]

        elapsed = time.time() - start_time
        with _job_store_lock:
            _job_store[job_id]["status"] = "completed"
            _job_store[job_id]["progress"] = 100
            _job_store[job_id]["result"] = {"status": "success", "output": output}
            _job_store[job_id]["completed_at"] = time.time()
            _job_store[job_id]["message"] = f"Found {total_issues} issues in {elapsed:.1f}s"

        logger.info("PREDEFINED KRA JOB [%s] found %d issues in %.1fs", job_id, total_issues, elapsed)

    except (InterruptedError, SystemExit):
        logger.info("PREDEFINED KRA JOB [%s] was stopped by the user", job_id)
        with _job_store_lock:
            if _job_store[job_id].get("status") != "stopped":
                _job_store[job_id]["status"] = "stopped"
                _job_store[job_id]["completed_at"] = time.time()
    except Exception as exc:
        logger.exception("PREDEFINED KRA JOB [%s] failed", job_id)
        with _job_store_lock:
            _job_store[job_id]["status"] = "failed"
            _job_store[job_id]["error"] = str(exc)
            _job_store[job_id]["completed_at"] = time.time()
            _job_store[job_id]["message"] = f"Failed: {str(exc)[:200]}"
    finally:
        _thread_local.job_id = None


def _run_cloudwatch_task(job_id: str, request: CloudWatchMetricsRequest):
    """Background worker for /getCloudWatchMetrics."""
    import time
    start_time = time.time()
    _thread_local.job_id = job_id
    try:
        with _job_store_lock:
            _job_store[job_id]["status"] = "running"
            _job_store[job_id]["started_at"] = start_time
            _job_store[job_id]["progress"] = 10
            _job_store[job_id]["message"] = "Discovering CloudWatch metrics..."

        fetcher = CloudWatchMetricsFetcher()
        # Use shared bg loop — avoids competing event loops crashing uvicorn
        summary = _run_async(fetcher.fetch_all_metrics(
            region=request.region,
            last_hours=request.last_hours,
            period=request.period,
            timezone_str=request.timezone_str,
        ))
        elapsed = time.time() - start_time
        total = summary.get("metadata", {}).get("total_metrics_found", 0)

        with _job_store_lock:
            _job_store[job_id]["status"] = "completed"
            _job_store[job_id]["progress"] = 100
            _job_store[job_id]["result"] = {"status": "success", "output": summary}
            _job_store[job_id]["completed_at"] = time.time()
            _job_store[job_id]["message"] = f"Fetched {total} metrics in {elapsed:.1f}s"

        logger.info("CLOUDWATCH JOB [%s] found %d metrics in %.1fs", job_id, total, elapsed)

    except Exception as exc:
        logger.exception("CLOUDWATCH JOB [%s] failed", job_id)
        with _job_store_lock:
            _job_store[job_id]["status"] = "failed"
            _job_store[job_id]["error"] = str(exc)
            _job_store[job_id]["completed_at"] = time.time()
            _job_store[job_id]["message"] = f"Failed: {str(exc)[:200]}"
    finally:
        _thread_local.job_id = None

class ActionInput(BaseModel):
    actionName: str = Field(description="Short name of the action")
    actionDescription: str = Field(description="Detailed description of what needs to be done")
    service: Optional[str] = Field(default="AWS", description="AWS service this action applies to")
    kraCode: Optional[str] = Field(default=None, description="KRA identifier (e.g. KRA-01)")
    priorityLevel: Optional[str] = Field(default=None, description="Priority level (e.g. P1)")
    steps: Optional[List[str]] = Field(default=None, description="Implementation steps to add as a Jira comment")
    detectorId: Optional[str] = Field(default=None, description="Detector ID for predefined KRA actions")
    resourceArn: Optional[str] = Field(default=None, description="Target resource ARN for predefined KRA actions")
    region: Optional[str] = Field(default=os.getenv("AWS_DEFAULT_REGION", "us-east-1"), description="Target region for predefined KRA actions")
    action_type: Optional[str] = Field(default="KRA_REMEDIATION", description="Execution path discriminator. Either AWS_TASK or KRA_REMEDIATION.")
    permission_set_id: Optional[str] = Field(default=None, description="AWS permission set selected during onboarding")


class AnalyzerRequest(BaseModel):
    actions: List[ActionInput] = Field(description="List of remediation actions to analyze")
    projectKey: str = Field(default="DEV", description="Jira project key for ticket creation")


@app.post("/analyzeActions")
def analyze_actions(request: AnalyzerRequest):
    """Submit action analysis as an async job. Poll /jobs/status/{job_id} for result."""
    job_id = str(uuid.uuid4())
    logger.info(
        "POST /analyzeActions → async job_id=%s actions=%d projectKey=%s",
        job_id, len(request.actions), request.projectKey,
    )
    with _job_store_lock:
        _job_store[job_id] = {
            "status": "pending", "progress": 0,
            "message": f"Queued: analyzing {len(request.actions)} actions",
            "result": None, "error": None,
            "started_at": None, "completed_at": None,
        }
    _thread_pool.submit(_run_analyzer_task, job_id, request)
    return JSONResponse(status_code=202, content={
        "job_id": job_id, "status": "accepted",
        "message": f"Analysis submitted. Poll /jobs/status/{job_id}",
        "poll_url": f"/jobs/status/{job_id}"
    })


def _run_analyzer_task(job_id: str, request: AnalyzerRequest):
    """Background worker for /analyzeActions."""
    import time
    start_time = time.time()
    _thread_local.job_id = job_id
    try:
        with _job_store_lock:
            _job_store[job_id]["status"] = "running"
            _job_store[job_id]["started_at"] = start_time
            _job_store[job_id]["progress"] = 10
            _job_store[job_id]["message"] = "Analyzing actions with LLM..."

        agent = AnalyzerAgent()

        with _job_store_lock:
            _job_store[job_id]["progress"] = 40
            _job_store[job_id]["message"] = "Creating Jira tickets..."

        response = agent.RunPipeline(request.model_dump())
        elapsed = time.time() - start_time

        with _job_store_lock:
            _job_store[job_id]["status"] = "completed"
            _job_store[job_id]["progress"] = 100
            _job_store[job_id]["result"] = response.model_dump()
            _job_store[job_id]["completed_at"] = time.time()
            _job_store[job_id]["message"] = f"Completed in {elapsed:.1f}s"

        logger.info("ANALYZER JOB [%s] completed in %.1fs", job_id, elapsed)

    except Exception as exc:
        logger.exception("ANALYZER JOB [%s] failed", job_id)
        with _job_store_lock:
            _job_store[job_id]["status"] = "failed"
            _job_store[job_id]["error"] = str(exc)
            _job_store[job_id]["completed_at"] = time.time()
            _job_store[job_id]["message"] = f"Failed: {str(exc)[:200]}"
    finally:
        _thread_local.job_id = None



class CopilotRequest(BaseModel):
    sessionId: str = Field(description="Conversation thread ID — reuse to retain memory across turns")
    message: str = Field(description="User message to the copilot agent")


class CopilotResponse(BaseModel):
    sessionId: str
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
        _thread_pool.submit(_delete_sandbox, sandbox_path)

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
    
    # Initialize job record
    with _job_store_lock:
        _job_store[job_id] = {
            "status": "pending",
            "progress": 0,
            "message": "Waiting to start",
            "result": None,
            "error": None,
            "started_at": None,
            "completed_at": None,
            "sandbox_path": request.sandbox_path or None,
        }
    
    # Submit to thread pool
    _thread_pool.submit(
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
    answers: List[str] = Field(description="User's answers to the HITL questions")


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
        if result.get("status") != "needs_clarification":
            raise HTTPException(
                status_code=400,
                detail=f"Job {job_id} is not paused for HITL (current status={result.get('status')})"
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

            # Resume the DW graph with the user's answers as the interrupt value
            from langgraph.types import Command as LGCommand
            final_state = _digital_worker.invoke(
                LGCommand(resume=answers),
                config=_dw_thread_config(job_id),
            )

            snapshot = _digital_worker.get_state(_dw_thread_config(job_id))
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
        exec_thread_id = f"exec-{job_id}"
        try:
            with _job_store_lock:
                _job_store[job_id]["thread_id"] = threading.get_ident()
                aws_permissions = _job_store[job_id].get("aws_permissions", [])

            orchestrator = ExecutionAgents(max_iterations=5, job_id=job_id)
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
        _thread_pool.submit(_run_dw_resume)
    else:
        _thread_pool.submit(_run_execution_resume)

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
        orchestrator = ExecutionAgents(max_iterations=request.max_iterations, job_id=job_id)

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


class CloudRequestSubmission(BaseModel):
    source: str = Field(default="rest_api", description=f"One of: {', '.join(SUPPORTED_SOURCES)}")
    payload: Dict[str, Any] = Field(
        description="Channel-native payload; for rest_api use {title, description, priority, requester}."
    )
    dry_run: bool = Field(
        default=False,
        description="When False, approved automations perform real mutating AWS calls.",
    )


class ApprovalSubmission(BaseModel):
    approved: bool = Field(description="True to approve automated execution, False to reject.")
    approver: Optional[str] = Field(default=None, description="Who decided.")
    comment: str = Field(default="", description="Optional decision rationale.")


def _dw_thread_config(job_id: str) -> Dict[str, Any]:
    return {"configurable": {"thread_id": job_id}}


def _dw_finalize_job(job_id: str, final_state: Dict[str, Any], start_time: float) -> None:
    """Translate terminal graph state into the shared job-store shape."""
    import time
    with _job_store_lock:
        if _job_store[job_id].get("status") == "stopped":
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

        _job_store[job_id]["status"] = actual_status
        _job_store[job_id]["progress"] = 100
        
        result_dict = pipeline_res.copy() if pipeline_res else {}
        result_dict["status"] = actual_status
        result_dict["output"] = final_state.get("result", {})
        
        _job_store[job_id]["result"] = result_dict
        _job_store[job_id]["completed_at"] = time.time()
        _job_store[job_id]["message"] = (
            f"Workflow {actual_status} in {time.time() - start_time:.1f}s"
        )
        
        if execution:
            sandbox_path = execution.get("sandbox_path") if isinstance(execution, dict) else getattr(execution, "sandbox_path", None)
            if sandbox_path:
                _job_store[job_id]["sandbox_path"] = sandbox_path


def _run_digital_worker_task(job_id: str, submission: CloudRequestSubmission) -> None:
    """Background worker for /requests and /webhooks/{source}."""
    import time
    import threading
    start_time = time.time()
    _thread_local.job_id = job_id
    try:
        if _digital_worker is None:
            raise RuntimeError("Digital Worker graph is not initialized")
        with _job_store_lock:
            _job_store[job_id]["status"] = "running"
            _job_store[job_id]["started_at"] = start_time
            _job_store[job_id]["progress"] = 10
            _job_store[job_id]["message"] = f"Processing {submission.source} request..."
            _job_store[job_id]["thread_id"] = threading.get_ident()

        final_state = _digital_worker.invoke(
            {
                "source": submission.source,
                "payload": submission.payload,
                "dry_run": submission.dry_run,
                "job_id": job_id,
            },
            config=_dw_thread_config(job_id),
        )

        # interrupt_before=["approval_gate"] pauses the run when human
        # approval is required. Surface that state instead of completing.
        snapshot = _digital_worker.get_state(_dw_thread_config(job_id))
        if snapshot.next and "approval_gate" in snapshot.next:
            values = snapshot.values
            request = values["request"]
            classification = values["classification"]
            root_cause = values["root_cause"]
            with _job_store_lock:
                _job_store[job_id]["status"] = "awaiting_approval"
                _job_store[job_id]["progress"] = 70
                _job_store[job_id]["message"] = "Awaiting human approval"
                _job_store[job_id]["title"] = request.title
                _job_store[job_id]["result"] = {
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

        # Handle Execution Agent HITL pause!
        if snapshot.next and "execute_automation" in snapshot.next:
            interrupts = snapshot.tasks[0].interrupts if snapshot.tasks else []
            interrupt_val = interrupts[0].value if interrupts else {}
            questions = interrupt_val.get("questions", ["Please provide the required input to proceed."])
            summary = interrupt_val.get("summary", "Awaiting input")
            with _job_store_lock:
                _job_store[job_id]["status"] = "completed"
                _job_store[job_id]["progress"] = 100
                _job_store[job_id]["message"] = "Awaiting user input"
                # Store job_type='dw' so resume endpoint resumes the DW graph,
                # not the Execution Agent directly.
                _job_store[job_id]["job_type"] = "dw"
                _job_store[job_id]["result"] = {
                    "statusCode": 202,
                    "status": "needs_clarification",
                    "thread_id": job_id,  # DW graph thread_id = job_id
                    "questions": questions,
                    "summary": summary,
                }
            logger.info("DIGITAL WORKER JOB [%s] paused for HITL", job_id)
            return

        _dw_finalize_job(job_id, final_state, start_time)

        logger.info("DIGITAL WORKER JOB [%s] completed in %.1fs", job_id, time.time() - start_time)

    except (InterruptedError, SystemExit):
        # Both are raised by our stop mechanism — status is already "stopped", do nothing
        logger.info("DIGITAL WORKER JOB [%s] was stopped by the user", job_id)
        with _job_store_lock:
            if _job_store[job_id].get("status") != "stopped":
                _job_store[job_id]["status"] = "stopped"
                _job_store[job_id]["completed_at"] = time.time()
    except BaseException as exc:
        logger.exception("DIGITAL WORKER JOB [%s] failed with exception", job_id)
        with _job_store_lock:
            if _job_store[job_id].get("status") != "stopped":
                _job_store[job_id]["status"] = "failed"
                _job_store[job_id]["error"] = str(exc)
                _job_store[job_id]["completed_at"] = time.time()
                _job_store[job_id]["message"] = f"Failed: {str(exc)[:200]}"
    finally:
        _thread_local.job_id = None


def _resume_digital_worker_task(job_id: str, approval: ApprovalSubmission) -> None:
    """Background worker for /requests/{job_id}/approve — resumes the interrupt."""
    import time
    import threading
    from langgraph.types import Command

    start_time = time.time()
    _thread_local.job_id = job_id
    try:
        if _digital_worker is None:
            raise RuntimeError("Digital Worker graph is not initialized")
        with _job_store_lock:
            _job_store[job_id]["status"] = "running"
            _job_store[job_id]["progress"] = 80
            _job_store[job_id]["message"] = "Resuming after approval decision..."
            _job_store[job_id]["thread_id"] = threading.get_ident()
            # Flag that this job was explicitly approved by a human so the
            # Worker Action Execution Center can distinguish it from the
            # initial "running" phase (before the approval gate is reached).
            _job_store[job_id]["approved_by_human"] = True
            _job_store[job_id]["requires_approval"] = False

        final_state = _digital_worker.invoke(
            Command(resume=approval.model_dump()),
            config=_dw_thread_config(job_id),
        )
        _dw_finalize_job(job_id, final_state, start_time)
        logger.info(
            "DIGITAL WORKER JOB [%s] resumed (approved=%s) and completed",
            job_id, approval.approved,
        )
    except Exception as exc:
        logger.exception("DIGITAL WORKER JOB [%s] resume failed", job_id)
        with _job_store_lock:
            _job_store[job_id]["status"] = "failed"
            _job_store[job_id]["error"] = str(exc)
            _job_store[job_id]["completed_at"] = time.time()
            _job_store[job_id]["message"] = f"Resume failed: {str(exc)[:200]}"
    finally:
        _thread_local.job_id = None


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
            clean_text = re.sub(r'<at>.*?</at>', '', text, flags=re.IGNORECASE)
            clean_text = re.sub(r'<[^>]+>', '', clean_text)
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


def _dw_request_summary(job_id: str, job: Dict[str, Any]) -> Dict[str, Any]:
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
    summary: Dict[str, Any] = {
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
        "decision_mode": approval.get("decision_mode") or out_decision.get("mode") or job.get("decision_mode"),
        "reason": approval.get("reason") or out_decision.get("reason"),
        "requires_approval": job.get("requires_approval", job.get("status") == "awaiting_approval"),
        "approved_by_human": job.get("approved_by_human", False),
        "workflow_status": output.get("status"),
        "submitted_at": job.get("submitted_at"),
        "started_at": job.get("started_at"),
        "completed_at": job.get("completed_at"),
        "sandbox_path": job.get("sandbox_path") or (output.get("execution") or {}).get("sandbox_path"),
    }
    return summary


def _submit_digital_worker_job(submission: CloudRequestSubmission) -> JSONResponse:
    if submission.source not in SUPPORTED_SOURCES:
        return JSONResponse(status_code=400, content={
            "status": "error",
            "message": f"Unsupported source '{submission.source}'. Expected one of: {', '.join(SUPPORTED_SOURCES)}",
        })
        
    if _digital_worker is None:
        return JSONResponse(status_code=503, content={
            "status": "error", "message": "Digital Worker graph is not initialized",
        })
    job_id = str(uuid.uuid4())
    logger.info("Digital Worker request submitted → job_id=%s source=%s", job_id, submission.source)
    import time
    with _job_store_lock:
        _job_store[job_id] = {
            # ``kind`` tags this job as a Digital Worker request so the
            # GET /requests discovery endpoint can list it apart from the
            # legacy observation/orchestrate jobs that share _job_store.
            "kind": "digital_worker",
            "source": submission.source,
            "title": _dw_submission_title(submission),
            "dry_run": submission.dry_run,
            "submitted_at": time.time(),
            "status": "pending", "progress": 0,
            "message": f"Queued: {submission.source} request workflow",
            "result": None, "error": None,
            "started_at": None, "completed_at": None,
        }
    _thread_pool.submit(_run_digital_worker_task, job_id, submission)
    
    if submission.source == "teams":
        # Teams requires a specific Bot Framework JSON schema to avoid showing an error in the channel
        return JSONResponse(status_code=200, content={
            "type": "message",
            "text": f"Digital Worker request accepted! Monitor progress on your dashboard. (Job ID: {job_id})"
        })
        
    return JSONResponse(status_code=202, content={
        "job_id": job_id, "status": "accepted",
        "message": f"Request submitted. Poll /jobs/status/{job_id}",
        "poll_url": f"/jobs/status/{job_id}",
    })


@app.post("/requests")
def submit_cloud_request(submission: CloudRequestSubmission):
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


@app.post("/webhooks/{source}")
def receive_webhook(
    source: str,
    payload: Dict[str, Any],
    x_chandra_webhook_token: Optional[str] = Header(default=None),
):
    import json
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

    if expected_token and x_chandra_webhook_token != expected_token:
        logger.warning("Webhook rejected: bad or missing X-Chandra-Webhook-Token (source=%s)", source)
        return JSONResponse(status_code=401, content={
            "status": "error", "message": "invalid or missing X-Chandra-Webhook-Token",
        })
    dry_run = payload.get("dry_run", False)
    return _submit_digital_worker_job(
        CloudRequestSubmission(source=source, payload=payload, dry_run=dry_run)
    )


@app.post("/requests/{job_id}/approve")
def approve_cloud_request(job_id: str, approval: ApprovalSubmission):
    """Approve or reject a workflow paused at the human approval gate."""
    with _job_store_lock:
        job = _job_store.get(job_id)
        if job is None:
            return JSONResponse(status_code=404, content={"error": "Job not found"})
        if job.get("status") != "awaiting_approval":
            return JSONResponse(status_code=409, content={
                "error": f"Job is '{job.get('status')}', not awaiting_approval",
            })
    _thread_pool.submit(_resume_digital_worker_task, job_id, approval)
    return JSONResponse(status_code=202, content={
        "job_id": job_id, "status": "accepted",
        "message": f"Approval decision submitted. Poll /jobs/status/{job_id}",
        "poll_url": f"/jobs/status/{job_id}",
    })


class DigitalWorkerSettings(BaseModel):
    max_iterations: int = Field(default=5, description="Maximum agent loop iterations.")
    command_timeout: int = Field(default=300, description="Timeout for shell commands.")

@app.get("/settings/digital-worker", response_model=DigitalWorkerSettings)
def get_digital_worker_settings():
    """Get the global digital worker settings."""
    config_path = os.path.join(os.path.dirname(__file__), "digital_worker_config.json")
    if os.path.exists(config_path):
        import json
        try:
            with open(config_path, "r") as f:
                data = json.load(f)
                return DigitalWorkerSettings(**data)
        except Exception as e:
            logger.warning("Failed to load digital_worker_config.json: %s", e)
    return DigitalWorkerSettings()

@app.post("/settings/digital-worker")
def update_digital_worker_settings(settings: DigitalWorkerSettings):
    """Update the global digital worker settings."""
    config_path = os.path.join(os.path.dirname(__file__), "digital_worker_config.json")
    import json
    try:
        with open(config_path, "w") as f:
            json.dump(settings.model_dump(), f, indent=4)
        return {"status": "success"}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/requests")
async def list_cloud_requests(status: Optional[str] = Query(default=None)):
    """List Digital Worker requests for the Human Approval Center.

    Optional ``?status=`` filter (e.g. ``awaiting_approval``, ``running``,
    ``completed``, ``failed``). Results are newest-first. This is the
    discovery endpoint the approval center polls — no job_id needed.
    """
    # Acquire the lock once for both the item list and the counts so the
    # HTTP thread holds it for the shortest possible time (single acquisition
    # instead of two, reducing contention with background worker threads).
    with _job_store_lock:
        items = [
            _dw_request_summary(job_id, job)
            for job_id, job in _job_store.items()
            if job.get("kind") == "digital_worker" and (status is None or job.get("status") == status)
        ]
        counts: Dict[str, int] = {}
        for job in _job_store.values():
            if job.get("kind") == "digital_worker":
                key = str(job.get("status"))
                counts[key] = counts.get(key, 0) + 1
    items.sort(key=lambda row: row.get("submitted_at") or 0, reverse=True)
    return JSONResponse(status_code=200, content={
        "status": "ok",
        "count": len(items),
        "counts": counts,
        "requests": items,
    })


@app.get("/requests/{job_id}")
def get_cloud_request(job_id: str):
    """Full detail for one Digital Worker request (approval payload +
    terminal workflow result when complete)."""
    with _job_store_lock:
        job = _job_store.get(job_id)
        if job is None or job.get("kind") != "digital_worker":
            return JSONResponse(status_code=404, content={
                "status": "not_found",
                "message": f"No Digital Worker request with id {job_id}",
            })
        job_copy = dict(job)
    summary = _dw_request_summary(job_id, job_copy)
    return JSONResponse(status_code=200, content={
        "status": "ok",
        "request": summary,
        "detail": job_copy.get("result"),
        "error": job_copy.get("error"),
    })


# =====================================================================
# AWS Tasks and AWS Permissions Implementation
# =====================================================================
from pathlib import Path
import threading
import json

AWS_TASKS_FILE = Path(__file__).parent / "aws_tasks.json"
AWS_PERMISSIONS_FILE = Path(__file__).parent / "aws_permissions.json"

_aws_tasks_lock = threading.Lock()
_aws_permissions_lock = threading.Lock()

def _load_aws_tasks_from_disk() -> list:
    try:
        if not AWS_TASKS_FILE.exists():
            return []
        with AWS_TASKS_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as exc:
        logger.exception("Failed to read aws_tasks.json: %s", exc)
        return []

def _save_aws_tasks_to_disk(entries: list) -> int:
    with _aws_tasks_lock:
        tmp_path = AWS_TASKS_FILE.with_suffix(".json.tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(entries, f, indent=2, ensure_ascii=False)
        import os
        os.replace(tmp_path, AWS_TASKS_FILE)
        return len(entries)

def _load_aws_permissions_from_disk() -> list:
    try:
        if not AWS_PERMISSIONS_FILE.exists():
            return []
        with AWS_PERMISSIONS_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as exc:
        logger.exception("Failed to read aws_permissions.json: %s", exc)
        return []

def _save_aws_permissions_to_disk(entries: list) -> int:
    with _aws_permissions_lock:
        tmp_path = AWS_PERMISSIONS_FILE.with_suffix(".json.tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(entries, f, indent=2, ensure_ascii=False)
        import os
        os.replace(tmp_path, AWS_PERMISSIONS_FILE)
        return len(entries)

class AwsTasksPayload(BaseModel):
    tasks: List[Dict[str, Any]] = Field(description="List of AWS Tasks to persist.")

@app.get("/api/aws-tasks")
@app.get("/aws-tasks")
def get_aws_tasks():
    tasks = _load_aws_tasks_from_disk()
    return JSONResponse(
        status_code=200,
        content={"status": "success", "count": len(tasks), "tasks": tasks}
    )

@app.put("/api/aws-tasks")
@app.put("/aws-tasks")
def put_aws_tasks(payload: AwsTasksPayload):
    try:
        written = _save_aws_tasks_to_disk(payload.tasks)
        return JSONResponse(
            status_code=200,
            content={"status": "success", "count": written, "message": f"Saved {written} AWS tasks"}
        )
    except Exception as exc:
        logger.exception("Failed to write aws_tasks.json: %s", exc)
        return JSONResponse(status_code=500, content={"status": "error", "exception": str(exc)})

class PermissionSetsPayload(BaseModel):
    permissions: List[Dict[str, Any]] = Field(description="List of permission sets to persist.")

@app.get("/api/permission-sets")
def get_permission_sets():
    perms = _load_aws_permissions_from_disk()
    return JSONResponse(
        status_code=200,
        content={"status": "success", "count": len(perms), "permissions": perms}
    )

@app.put("/api/permission-sets")
def put_permission_sets(payload: PermissionSetsPayload):
    try:
        written = _save_aws_permissions_to_disk(payload.permissions)
        return JSONResponse(
            status_code=200,
            content={"status": "success", "count": written, "message": f"Saved {written} permission sets"}
        )
    except Exception as exc:
        logger.exception("Failed to write aws_permissions.json: %s", exc)
        return JSONResponse(status_code=500, content={"status": "error", "exception": str(exc)})

AWS_ACTION_CATALOG = {
    "ec2": [
        "ec2:RunInstances", "ec2:StopInstances", "ec2:StartInstances", "ec2:TerminateInstances",
        "ec2:DescribeInstances", "ec2:DescribeSecurityGroups", "ec2:AuthorizeSecurityGroupIngress",
        "ec2:CreateTags", "ec2:CreateVolume", "ec2:AttachVolume"
    ],
    "s3": [
        "s3:CreateBucket", "s3:DeleteBucket", "s3:PutObject", "s3:GetObject",
        "s3:DeleteObject", "s3:ListBucket", "s3:PutBucketPolicy", "s3:PutEncryptionConfiguration"
    ],
    "iam": [
        "iam:CreateUser", "iam:CreateRole", "iam:AttachUserPolicy", "iam:AttachRolePolicy",
        "iam:PutUserPolicy", "iam:GetUser", "iam:ListAttachedUserPolicies"
    ]
}

@app.get("/api/permission-sets/actions")
def get_permission_actions(aws_service: Optional[str] = Query(default=None)):
    if aws_service:
        service_key = aws_service.lower().strip()
        actions = AWS_ACTION_CATALOG.get(service_key, [])
        return JSONResponse(status_code=200, content={"service": service_key, "actions": actions})
    all_actions = [act for service_actions in AWS_ACTION_CATALOG.values() for act in service_actions]
    return JSONResponse(status_code=200, content={"actions": all_actions})

COMMON_RESOURCE_ARNS = [
    "arn:aws:s3:::*",
    "arn:aws:ec2:*:*:instance/*",
    "arn:aws:ec2:*:*:security-group/*",
    "arn:aws:iam::*:user/*",
    "arn:aws:iam::*:role/*"
]

@app.get("/api/permission-sets/resource-arns")
def get_resource_arns():
    return JSONResponse(status_code=200, content={"resource_arns": COMMON_RESOURCE_ARNS})



if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=6001)