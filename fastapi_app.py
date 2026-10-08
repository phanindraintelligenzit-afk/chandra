"""
FastAPI application for the AWS Observability Agent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor
from fastapi import FastAPI, Header, Query, HTTPException, Body
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

# Context and multi-source tracking for full live log retention
import contextvars

_active_job_id_cv = contextvars.ContextVar("active_job_id", default=None)
_thread_to_job_id: Dict[int, str] = {}
_request_id_to_job_id: Dict[str, str] = {}
_ticket_to_job_id: Dict[str, str] = {}
_job_live_logs: Dict[str, List[Dict[str, Any]]] = {}
_job_sandbox_map: Dict[str, str] = {}
_synced_frontend_logs: Dict[str, List[Dict[str, Any]]] = {}

def _clean_job_id(jid: Optional[str]) -> str:
    if not jid:
        return ""
    j = str(jid).strip()
    if j.lower().startswith("dw-"):
        j = j[3:]
    return j.lower()

def register_job_context(
    job_id: str,
    thread_id: Optional[int] = None,
    request_id: Optional[str] = None,
    ticket: Optional[str] = None,
    sandbox_path: Optional[str] = None,
) -> None:
    clean_id = _clean_job_id(job_id)
    if not clean_id:
        return
    if clean_id not in _job_live_logs:
        _job_live_logs[clean_id] = []
    if thread_id is not None:
        _thread_to_job_id[thread_id] = clean_id
    if request_id:
        _request_id_to_job_id[str(request_id).strip().lower()] = clean_id
    if ticket:
        raw_tick = str(ticket).strip()
        _ticket_to_job_id[raw_tick.upper()] = clean_id
        _ticket_to_job_id[raw_tick.lower()] = clean_id
        m = re.search(r'\b([A-Z][A-Z0-9]+-\d+)\b', raw_tick.upper())
        if m:
            _ticket_to_job_id[m.group(1)] = clean_id
            _ticket_to_job_id[m.group(1).lower()] = clean_id
    if sandbox_path:
        _job_sandbox_map[clean_id] = str(sandbox_path)

# Job tracking for long-running orchestrations
class JobStoreDict(dict):
    def __init__(self, job_id, *args, **kwargs):
        self.job_id = job_id
        super().__init__(*args, **kwargs)
        if "message" in self:
            logger.info(f"Job {self.job_id} | Status: {self.get('status', 'unknown')} | Progress: {self.get('progress', 0)}% | Message: {self['message']}")
        self._persist()

    def _persist(self):
        try:
            import json
            from pathlib import Path
            meta_path = Path("logs") / f"{self.job_id}.meta.json"
            meta_path.parent.mkdir(parents=True, exist_ok=True)
            data = {}
            for k, v in self.items():
                if k in ("thread_id",):
                    continue
                try:
                    json.dumps(v)
                    data[k] = v
                except Exception:
                    data[k] = str(v)
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except Exception:
            pass

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        if key == "message":
            logger.info(f"Job {self.job_id} | Status: {self.get('status', 'unknown')} | Progress: {self.get('progress', 0)}% | Message: {value}")
        elif key == "status":
            logger.info(f"Job {self.job_id} | Status changed to: {value}")
        self._persist()

class JobStoreManager(dict):
    def __setitem__(self, key, value):
        if isinstance(value, dict) and not isinstance(value, JobStoreDict):
            value = JobStoreDict(key, value)
        super().__setitem__(key, value)

    def _load_from_disk_if_needed(self, key):
        if not super().__contains__(key) and isinstance(key, str):
            try:
                from pathlib import Path
                import json
                meta_path = Path("logs") / f"{key}.meta.json"
                if meta_path.is_file():
                    with open(meta_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, dict):
                        super().__setitem__(key, JobStoreDict(key, data))
            except Exception:
                pass

    def __contains__(self, key):
        self._load_from_disk_if_needed(key)
        return super().__contains__(key)

    def __getitem__(self, key):
        self._load_from_disk_if_needed(key)
        return super().__getitem__(key)

    def get(self, key, default=None):
        self._load_from_disk_if_needed(key)
        return super().get(key, default)

_job_store: Dict[str, Dict[str, Any]] = JobStoreManager()

def _load_jobs_from_disk(limit: int = 50, digital_worker_only: bool = False):
    """Load existing jobs from logs/*.meta.json into _job_store on startup or when needed."""
    try:
        import os
        import json
        if not os.path.exists("logs"):
            return
        entries = [e for e in os.scandir("logs") if e.name.endswith(".meta.json")]
        entries.sort(key=lambda e: e.stat().st_mtime, reverse=True)
        seen_tickets = set()
        dw_loaded = 0
        for entry in entries:
            try:
                job_id = entry.name[:-10]
                if job_id in _job_store:
                    if _job_store[job_id].get("kind") == "digital_worker":
                        dw_loaded += 1
                    continue
                with open(entry.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not data or not isinstance(data, dict):
                    continue
                is_dw = data.get("kind") == "digital_worker"
                if digital_worker_only and not is_dw:
                    continue
                res = data.get("result") or {}
                approval = res.get("approval_request") or {}
                ticket = approval.get("external_id") or data.get("external_id")
                if ticket:
                    if ticket in seen_tickets:
                        continue
                js_dict = dict(data)
                # Cleanup zombie running/pending jobs from disk:
                # If a job was saved with status "running" or "pending" but server restarted,
                # mark it as failed so it doesn't spin as active forever (e.g. DEV-987)
                if js_dict.get("status") in ("running", "pending"):
                    js_dict["status"] = "failed"
                    js_dict["error"] = "Process interrupted by server restart"
                    js_dict["message"] = "Execution halted when backend restarted"
                super(JobStoreManager, _job_store).__setitem__(job_id, js_dict)
                if is_dw:
                    dw_loaded += 1
                if digital_worker_only and dw_loaded >= limit:
                    break
                elif not digital_worker_only and len(_job_store) >= limit:
                    break
            except Exception:
                pass
    except Exception as e:
        logger.warning("Failed to load jobs from disk: %s", e)

try:
    _load_jobs_from_disk(50)
except Exception:
    pass

def _recover_job_from_disk(job_id: str) -> Optional[Dict[str, Any]]:
    """Recover a job record from sandbox execution_result.json, meta.json, or log files."""
    if not job_id:
        return None
    import json
    from pathlib import Path

    # 1. Check metadata file in logs/
    meta_path = Path("logs") / f"{job_id}.meta.json"
    if meta_path.exists():
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if data and isinstance(data, dict):
                    return data
        except Exception:
            pass

    # 2. Check sandbox execution_result.json across terraform_runs and iac/sandbox
    for search_root in (Path("terraform_runs"), Path("iac/sandbox")):
        if search_root.exists():
            for res_file in search_root.glob(f"**/{job_id}/execution_result.json"):
                try:
                    with open(res_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        is_success = data.get("status") in ("success", "completed")
                        return {
                            "job_id": job_id,
                            "status": "completed" if is_success else "failed",
                            "progress": 100,
                            "message": data.get("message", "Completed successfully"),
                            "result": {
                                "statusCode": 200 if is_success else 500,
                                "summary": data.get("summary", ""),
                                "outputs": data.get("outputs", {}),
                            },
                            "error": None if is_success else data.get("message"),
                            "sandbox_path": data.get("sandbox_path", str(res_file.parent)),
                        }
                except Exception:
                    pass

    # 3. Check logs/<job_id>.log for completion or progress
    log_path = Path("logs") / f"{job_id}.log"
    if log_path.exists():
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            if "PIPELINE COMPLETED SUCCESSFULLY" in content or "OK EXECUTION COMPLETED SUCCESSFULLY" in content:
                summary = ""
                if "OK LLM summary generated:" in content:
                    summary_part = content.split("OK LLM summary generated:", 1)[1]
                    summary = summary_part.split("═════════", 1)[0].strip()
                return {
                    "job_id": job_id,
                    "status": "completed",
                    "progress": 100,
                    "message": "Completed successfully",
                    "result": {
                        "statusCode": 200,
                        "summary": summary or "Execution completed successfully via Terraform.",
                    },
                    "error": None,
                }
            elif "FAILED" in content and "Traceback" in content:
                return {
                    "job_id": job_id,
                    "status": "failed",
                    "progress": 100,
                    "message": "Execution encountered an error",
                    "error": "Execution failed in pipeline",
                }
        except Exception:
            pass

    return None

def _preload_job_store_from_disk() -> None:
    """Preload recent jobs into _job_store so server reload never loses in-flight or completed jobs."""
    try:
        from pathlib import Path
        import json
        logs_dir = Path("logs")
        if logs_dir.exists():
            for meta_file in sorted(logs_dir.glob("*.meta.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:50]:
                try:
                    jid = meta_file.name.replace(".meta.json", "")
                    with open(meta_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if jid and jid not in _job_store:
                        _job_store[jid] = data
                except Exception:
                    pass
        runs_dir = Path("terraform_runs")
        if runs_dir.exists():
            import shutil
            for res_file in sorted(runs_dir.glob("*/*/execution_result.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:50]:
                jid = res_file.parent.name
                if jid and jid not in _job_store:
                    recovered = _recover_job_from_disk(jid)
                    if recovered:
                        _job_store[jid] = recovered
            # Automatically free disk space by purging heavy .terraform provider binaries from old runs
            import stat
            for tf_cache in runs_dir.glob("*/*/.terraform"):
                try:
                    for item in tf_cache.rglob("*"):
                        try:
                            os.chmod(item, stat.S_IWRITE | stat.S_IREAD)
                        except Exception:
                            pass
                    shutil.rmtree(tf_cache, ignore_errors=True)
                except Exception:
                    pass
    except Exception:
        pass

    # Ensure shared Terraform plugin cache and temp dir are globally configured on workspace drive
    try:
        import os
        cache_dir = os.path.abspath(".terraform_cache")
        os.makedirs(cache_dir, exist_ok=True)
        os.environ["TF_PLUGIN_CACHE_DIR"] = cache_dir

        local_tmp = os.path.abspath(os.path.join("terraform_runs", ".tmp"))
        os.makedirs(local_tmp, exist_ok=True)
        os.environ["TMP"] = local_tmp
        os.environ["TEMP"] = local_tmp
    except Exception:
        pass

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
# Process-safe: restarts loop if PID changes after uvicorn forks worker processes.
_bg_loop: Optional[asyncio.AbstractEventLoop] = None
_bg_loop_thread: Optional[threading.Thread] = None
_bg_loop_pid: Optional[int] = None
_bg_loop_lock = threading.Lock()

def _ensure_bg_loop() -> asyncio.AbstractEventLoop:
    global _bg_loop, _bg_loop_thread, _bg_loop_pid
    curr_pid = os.getpid()
    with _bg_loop_lock:
        if (
            _bg_loop is None
            or _bg_loop_pid != curr_pid
            or _bg_loop.is_closed()
            or not (_bg_loop_thread and _bg_loop_thread.is_alive())
        ):
            loop = asyncio.new_event_loop()
            def _runner(l: asyncio.AbstractEventLoop) -> None:
                asyncio.set_event_loop(l)
                l.run_forever()
            t = threading.Thread(
                target=_runner, args=(loop,), daemon=True, name=f"bg-async-loop-{curr_pid}"
            )
            t.start()
            _bg_loop = loop
            _bg_loop_thread = t
            _bg_loop_pid = curr_pid
    return _bg_loop

_ensure_bg_loop()


def _run_async(coro, timeout: float = 120.0) -> Any:
    """Run an async coroutine on the shared background event loop and block until done."""
    loop = _ensure_bg_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout)  # blocks the calling thread until coroutine completes with safety timeout


class LogCapture(logging.Handler):
    """Custom handler to capture logs into memory buffer and real-time disk streams."""
    def emit(self, record: logging.LogRecord) -> None:
        try:
            jid = getattr(_thread_local, "job_id", None)
            if not jid:
                try:
                    jid = _active_job_id_cv.get()
                except Exception:
                    jid = None
            if not jid:
                jid = _thread_to_job_id.get(threading.get_ident())
            if not jid and record.name and "." in record.name:
                parts = record.name.split(".")
                if len(parts) >= 2 and len(parts[-1]) >= 8:
                    candidate = parts[-1].strip().lower()
                    if candidate in _job_live_logs or candidate in _job_store or re.match(r"^[0-9a-f\-]{30,}$", candidate):
                        jid = candidate

            msg = self.format(record)

            # Check message for known tickets
            if not jid and msg:
                for ticket, mapped_jid in list(_ticket_to_job_id.items()):
                    if ticket and ticket in msg:
                        jid = mapped_jid
                        break

            # Check message for known UUIDs or request IDs
            if not jid and msg:
                for m in re.finditer(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", msg, re.IGNORECASE):
                    found_uuid = m.group(0).lower()
                    if found_uuid in _request_id_to_job_id:
                        jid = _request_id_to_job_id[found_uuid]
                        break
                    if found_uuid in _job_store or found_uuid in _job_live_logs:
                        jid = found_uuid
                        break

            clean_jid = _clean_job_id(jid) if jid else None

            log_entry = {
                "timestamp": record.created,
                "level": record.levelname,
                "logger": record.name,
                "message": msg,
                "job_id": clean_jid
            }
            _log_buffer.append(log_entry)
            if len(_log_buffer) > _max_logs:
                _log_buffer.pop(0)

            # Real-time persistence for the active job
            if clean_jid:
                if clean_jid not in _job_live_logs:
                    _job_live_logs[clean_jid] = []
                _job_live_logs[clean_jid].append(log_entry)
                if len(_job_live_logs[clean_jid]) > 5000:
                    _job_live_logs[clean_jid].pop(0)

                # Real-time append to logs/<clean_jid>.live.log
                try:
                    os.makedirs("logs", exist_ok=True)
                    live_path = os.path.join("logs", f"{clean_jid}.live.log")
                    with open(live_path, "a", encoding="utf-8", errors="replace") as f:
                        f.write(f"{msg}\n")
                except Exception:
                    pass

                # If sandbox is known, append directly to live_logs.txt in sandbox
                sb_path = _job_sandbox_map.get(clean_jid) or os.path.join("terraform_runs", "default_worker", clean_jid)
                if sb_path and os.path.exists(sb_path):
                    try:
                        with open(os.path.join(sb_path, "live_logs.txt"), "a", encoding="utf-8", errors="replace") as f:
                            f.write(f"{msg}\n")
                    except Exception:
                        pass
        except Exception:
            pass

def _preload_logs_from_disk() -> None:
    """Pre-populate _log_buffer and _job_live_logs from disk logs on server startup/reload."""
    try:
        logs_dir = Path("logs")
        if not logs_dir.exists():
            return
        log_files = sorted(logs_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not log_files:
            return
        recent_entries = []
        for lf in log_files[:10]:
            try:
                base_name = lf.stem
                is_live = base_name.endswith(".live")
                clean_stem = _clean_job_id(base_name[:-5] if is_live else base_name)
                jid = clean_stem if len(clean_stem) >= 30 else None
                mtime = lf.stat().st_mtime
                with open(lf, "r", encoding="utf-8", errors="replace") as f:
                    for line in f.readlines()[-300:]:
                        line_str = line.strip()
                        if not line_str or line_str.startswith("===") or line_str.startswith("CHANDRA"):
                            continue
                        entry = {
                            "timestamp": mtime,
                            "level": "INFO" if "INFO" in line_str else ("ERROR" if "ERROR" in line_str else "WARN"),
                            "logger": f"ExecutionAgents.{jid}" if jid else "system",
                            "message": line_str,
                            "job_id": jid
                        }
                        recent_entries.append(entry)
                        if jid:
                            if jid not in _job_live_logs:
                                _job_live_logs[jid] = []
                            _job_live_logs[jid].append(entry)
            except Exception:
                pass
        if recent_entries:
            _log_buffer.extend(recent_entries[-1000:])
    except Exception:
        pass

_preload_logs_from_disk()
_preload_job_store_from_disk()

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
    
    # Safe DB initialization check
    try:
        from src.chandra.db.models import Base
        from src.chandra.db.session import get_engine
        Base.metadata.create_all(bind=get_engine())
        logger.info("Database schema verified/created successfully.")
    except Exception as exc:
        logger.warning("Could not auto-create database tables on startup (PostgreSQL might still be initializing): %s", exc)

    yield

    # Stop background tasks gracefully to prevent dangling threads during app teardown
    # This prevents 'cannot schedule new futures' and 'I/O operation on closed file' in pytest
    logger.info("Shutting down background thread pool...")
    _thread_pool.shutdown(wait=True)
    
    logger.info("Shutting down background async loop...")
    try:
        if _bg_loop and _bg_loop.is_running():
            _bg_loop.call_soon_threadsafe(_bg_loop.stop)
            if _bg_loop_thread:
                _bg_loop_thread.join(timeout=3.0)
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

from fastapi import Request

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled application exception on %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "error": str(exc), "path": request.url.path}
    )

# Built once so MemorySaver persists across requests (keyed by sessionId / thread_id)
# Wrapped in try/except so FastAPI still starts even if an agent fails to initialize
# (e.g. Bedrock unreachable, Postgres timeout, missing env var)
def get_copilot_agent():
    global _copilot_agent
    if _copilot_agent is None:
        try:
            _copilot_agent = build_graph()
            logger.info("Copilot agent initialized successfully")
        except Exception as _e:
            logger.error("Failed to initialize copilot agent: %s", _e)
    return _copilot_agent

def get_digital_worker():
    global _digital_worker
    if _digital_worker is None:
        try:
            _digital_worker = build_digital_worker_graph()
            logger.info("Digital Worker graph initialized successfully")
        except Exception as _e:
            logger.error("Failed to initialize Digital Worker graph: %s", _e)
    return _digital_worker

try:
    _copilot_agent = build_graph()
    logger.info("Copilot agent initialized successfully")
except Exception as _e:
    logger.error("Failed to initialize copilot agent: %s", _e)
    _copilot_agent = None

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
    deployment: Optional[Dict[str, Any]] = Field(default=None, description="Deployment configuration from onboarding wizard")

    model_config = {"extra": "allow"}


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

    copilot = get_copilot_agent()
    dw = get_digital_worker()
    components["copilot_agent"] = "ok" if copilot is not None else "unavailable"
    components["digital_worker"] = "ok" if dw is not None else "unavailable"

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
async def get_logs(
    limit: int = Query(500, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    job_id: Optional[str] = Query(default=None)
):
    """Get recent backend logs (stored in memory or loaded from disk for job_id)"""
    target_logs = _log_buffer
    if job_id:
        clean_id = _clean_job_id(job_id)
        
        # 1. Start with dedicated in-memory live logs for this job
        matched = list(_job_live_logs.get(clean_id, []))
        
        # 2. Add synced logs from frontend
        if clean_id in _synced_frontend_logs:
            existing_msgs = {l.get("message") for l in matched}
            for fl in _synced_frontend_logs[clean_id]:
                if fl.get("message") not in existing_msgs:
                    matched.append(fl)
                    existing_msgs.add(fl.get("message"))

        # 3. Add matches from global buffer
        existing_msgs = {l.get("message") for l in matched}
        for l in _log_buffer:
            l_jid = _clean_job_id(l.get("job_id"))
            l_logger = str(l.get("logger", "")).lower()
            l_msg = str(l.get("message", "")).lower()
            if (l_jid == clean_id or clean_id in l_logger or clean_id in l_msg) and l.get("message") not in existing_msgs:
                if "AWSOBSERVABILITYAGENT" in str(l.get("logger", "")).upper() and clean_id not in l_msg:
                    continue
                matched.append(l)
                existing_msgs.add(l.get("message"))

        # 4. Check real-time live log file on disk (logs/{clean_id}.live.log)
        live_disk_path = Path(f"logs/{clean_id}.live.log")
        if live_disk_path.exists():
            try:
                mtime = live_disk_path.stat().st_mtime
                with open(live_disk_path, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        l_str = line.strip()
                        if l_str and l_str not in existing_msgs:
                            matched.append({
                                "timestamp": mtime,
                                "level": "INFO" if "INFO" in l_str else ("ERROR" if "ERROR" in l_str else "WARN"),
                                "logger": f"ExecutionAgents.{clean_id}",
                                "message": l_str,
                                "job_id": clean_id
                            })
                            existing_msgs.add(l_str)
            except Exception:
                pass

        # 5. Check completed disk log file (logs/{clean_id}.log)
        disk_path = Path(f"logs/{clean_id}.log")
        if disk_path.exists() and len(matched) < 5:
            try:
                mtime = disk_path.stat().st_mtime
                with open(disk_path, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        l_str = line.strip()
                        if l_str and not l_str.startswith("===") and not l_str.startswith("CHANDRA") and l_str not in existing_msgs:
                            matched.append({
                                "timestamp": mtime,
                                "level": "INFO" if "INFO" in l_str else ("ERROR" if "ERROR" in l_str else "WARN"),
                                "logger": f"ExecutionAgents.{clean_id}",
                                "message": l_str,
                                "job_id": clean_id
                            })
                            existing_msgs.add(l_str)
            except Exception:
                pass

        if matched:
            target_logs = matched

    start = max(0, len(target_logs) - limit - offset)
    end = max(0, len(target_logs) - offset)
    return JSONResponse(status_code=200, content={"logs": target_logs[start:end]})

@app.get("/getDetectorIssues")
def get_detector_issues():
    """Submit detector scan as an async job. Poll /jobs/status/{job_id} for result."""
    job_id = str(uuid.uuid4())
    logger.info("GET /getDetectorIssues -> async job_id=%s", job_id)
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
    logger.info("POST /getPredefinedKraIssues -> async job_id=%s, kras=%s", job_id, request.selected_kras)
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
    logger.info("POST /getCloudWatchMetrics -> async job_id=%s region=%s", job_id, request.region)
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
        "POST /getAgentObservations -> async job_id=%s region=%s kras=%s",
        job_id, request.region, [k.code for k in request.kras],
    )
    if request.deployment and isinstance(request.deployment, dict):
        deploy_agent = request.deployment.get("agent_name")
        if deploy_agent and str(deploy_agent).strip():
            try:
                from src.chandra.digital_worker.tracker import set_active_agent_name
                set_active_agent_name(str(deploy_agent).strip())
            except Exception:
                pass
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
    job: Dict[str, Any] = {}
    try:
        with _job_store_lock:
            if job_id not in _job_store:
                return JSONResponse(status_code=404, content={
                    "job_id": job_id, "status": "not_found",
                    "message": "No job with this ID exists"
                })
            job = dict(_job_store[job_id])

        from fastapi.encoders import jsonable_encoder
        clean_content = jsonable_encoder({"job_id": job_id, **job})
        return JSONResponse(status_code=200, content=clean_content)
    except Exception as exc:
        logger.warning("Error serializing job status for %s: %s", job_id, exc)
        safe_job = {
            "job_id": job_id,
            "status": str(job.get("status") or "running"),
            "progress": int(job.get("progress") or 0),
            "message": str(job.get("message") or ""),
            "error": str(job.get("error") or "") if job.get("error") else None,
            "sandbox_path": str(job.get("sandbox_path") or "") if job.get("sandbox_path") else None
        }
        return JSONResponse(status_code=200, content=safe_job)


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
    model_config = ConfigDict(extra="ignore")
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
    isAwsTask: Optional[bool] = Field(default=None, description="Flag indicating if this is an AWS Task")


class AnalyzerRequest(BaseModel):
    actions: List[ActionInput] = Field(description="List of remediation actions to analyze")
    projectKey: str = Field(default="DEV", description="Jira project key for ticket creation")


@app.post("/analyzeActions")
def analyze_actions(request: AnalyzerRequest):
    """Submit action analysis as an async job. Poll /jobs/status/{job_id} for result."""
    job_id = str(uuid.uuid4())
    logger.info(
        "POST /analyzeActions -> async job_id=%s actions=%d projectKey=%s",
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
        agent = get_copilot_agent()
        if agent is None:
            return JSONResponse(status_code=503, content={"status": "error", "message": "Copilot agent unavailable"})
        reply = copilot_chat(agent, request.sessionId, request.message)
        return JSONResponse(status_code=200, content={"sessionId": request.sessionId, "reply": reply})
    except Exception as exc:
        logger.exception("Copilot chat failed: %s", exc)
        return JSONResponse(status_code=500, content={"status": "error", "exception": str(exc)})



class OrchestrateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
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
    jira_url: Optional[str] = Field(
        default=None,
        description="Alias for jiraUrl.",
    )
    jira_issue_key: Optional[str] = Field(
        default=None,
        description="Jira issue key (e.g. DEV-1069) associated with this task.",
    )
    jiraKey: Optional[str] = Field(
        default=None,
        description="Alias for jira_issue_key.",
    )
    max_iterations: int = Field(
        default=5,
        description="Maximum number of generate-execute iterations (default: 5).",
    )
    aws_permissions: Optional[List[str]] = Field(
        default=None,
        description="List of AWS permissions selected during onboarding.",
    )


def _build_full_job_logs(job_id: str) -> str:
    """Build the comprehensive live log stream and execution report for a job."""
    if not job_id:
        return "No job ID provided."

    import json
    import time
    from datetime import datetime

    app_root = Path(__file__).resolve().parent
    clean_id = _clean_job_id(job_id)

    # If clean_id looks like a Jira ticket (e.g., DEV-1068) or action ID, resolve to real UUID
    jira_match = re.search(r'\b([A-Z][A-Z0-9]+-\d+)\b', str(job_id).upper())
    resolved_ticket = jira_match.group(1) if jira_match else None

    if resolved_ticket and resolved_ticket in _ticket_to_job_id:
        clean_id = _clean_job_id(_ticket_to_job_id[resolved_ticket])
    elif resolved_ticket:
        with _job_store_lock:
            for jid, val in _job_store.items():
                if val.get("external_id") == resolved_ticket or resolved_ticket in str(val.get("title", "")):
                    clean_id = _clean_job_id(jid)
                    break
        if clean_id == _clean_job_id(job_id) or not clean_id:
            # Check meta.json files
            for m_file in (app_root / "logs").glob("*.meta.json"):
                try:
                    with open(m_file, "r", encoding="utf-8") as f:
                        m_val = json.load(f)
                    if m_val.get("external_id") == resolved_ticket:
                        clean_id = _clean_job_id(m_file.name[:-10])
                        break
                except Exception:
                    pass

    log_file_path = str(app_root / "logs" / f"{clean_id}.log")
    meta_file = str(app_root / "logs" / f"{clean_id}.meta.json")
    if not os.path.exists(meta_file):
        alt_meta = str(app_root / "logs" / f"dw-{clean_id}.meta.json")
        if os.path.exists(alt_meta):
            meta_file = alt_meta

    # 1. Recover job info
    job_info = dict(_job_store.get(clean_id, {}) or _job_store.get(f"dw-{clean_id}", {}))
    if not job_info:
        recovered = _recover_job_from_disk(clean_id)
        if recovered:
            job_info = recovered

    meta_data = {}
    if os.path.exists(meta_file):
        try:
            with open(meta_file, "r", encoding="utf-8") as f:
                meta_data = json.load(f)
        except Exception:
            pass

    # 2. Gather result and sandbox artifacts
    result = job_info.get("result") or meta_data.get("result", {})
    output_data = result.get("output", {}) if isinstance(result, dict) else {}
    req_meta = output_data.get("request", {}) if isinstance(output_data, dict) else {}
    exec_meta = output_data.get("execution", {}) if isinstance(output_data, dict) else (result.get("execution", {}) if isinstance(result, dict) else {})

    sandbox_candidates = [
        job_info.get("sandbox_path"),
        meta_data.get("sandbox_path"),
        exec_meta.get("sandbox_path"),
        _job_sandbox_map.get(clean_id),
        str((app_root / "terraform_runs" / "default_worker" / clean_id).resolve()),
        str((app_root / "terraform_runs" / clean_id).resolve()),
        os.path.join("terraform_runs", "default_worker", clean_id),
    ]
    sandbox_path = None
    for sc in sandbox_candidates:
        if sc and os.path.exists(sc):
            sandbox_path = os.path.abspath(sc)
            break
    if not sandbox_path:
        sandbox_path = str((app_root / "terraform_runs" / "default_worker" / clean_id).resolve())

    exec_result_data = {}
    if sandbox_path and os.path.exists(sandbox_path):
        res_file = os.path.join(sandbox_path, "execution_result.json")
        if os.path.exists(res_file):
            try:
                with open(res_file, "r", encoding="utf-8") as f:
                    exec_result_data = json.load(f)
            except Exception:
                pass

    # 3. Job attributes
    title = job_info.get("title") or meta_data.get("title") or req_meta.get("title") or "Cloud Operations Automation"
    external_id = meta_data.get("external_id") or req_meta.get("external_id") or (resolved_ticket if resolved_ticket else "N/A")
    request_id = req_meta.get("request_id") or ""
    raw_status = job_info.get("status") or meta_data.get("status")
    status = raw_status if (raw_status and str(raw_status).lower() != "none") else "COMPLETED"
    progress = job_info.get("progress") if job_info.get("progress") is not None else meta_data.get("progress", 100)
    raw_msg = job_info.get("message") or meta_data.get("message")
    message = raw_msg if (raw_msg and str(raw_msg).lower() != "none") else "Execution completed successfully."

    started_at = job_info.get("started_at") or meta_data.get("started_at")
    completed_at = job_info.get("completed_at") or meta_data.get("completed_at")
    started_str = datetime.fromtimestamp(started_at).strftime("%Y-%m-%d %H:%M:%S UTC") if started_at else "N/A"
    completed_str = datetime.fromtimestamp(completed_at).strftime("%Y-%m-%d %H:%M:%S UTC") if completed_at else "N/A"

    # Register context mappings for future lookups
    register_job_context(
        clean_id,
        request_id=request_id if request_id else None,
        ticket=external_id if external_id and external_id != "N/A" else None,
        sandbox_path=sandbox_path
    )

    # 4. Gather live stream logs from all sources
    ticket_lower = str(external_id).lower().strip() if external_id and external_id != "N/A" else ""
    req_id_lower = str(request_id).lower().strip() if request_id else ""

    def _is_unrelated_scan(line_str: str) -> bool:
        up = line_str.upper()
        if "AWSOBSERVABILITYAGENT" in up or "FETCH_ACCOUNT_AUDIT" in up or "CHECK_RECORDER_STATUS" in up or "FETCH_GLOBAL_XRAY_SUMMARY" in up:
            low = line_str.lower()
            if clean_id in low or (ticket_lower and ticket_lower in low) or (req_id_lower and req_id_lower in low):
                return False
            return True
        return False

    raw_stream_candidates: List[Dict[str, Any]] = []

    # A. Synced logs from frontend (guarantees what the user saw is preserved)
    if clean_id in _synced_frontend_logs:
        raw_stream_candidates.extend(_synced_frontend_logs[clean_id])

    # B. Dedicated per-job in-memory logs
    if clean_id in _job_live_logs:
        raw_stream_candidates.extend(_job_live_logs[clean_id])

    # C. Real-time disk live stream file (logs/<clean_id>.live.log)
    live_log_file = os.path.join("logs", f"{clean_id}.live.log")
    if os.path.exists(live_log_file):
        try:
            mtime = os.path.getmtime(live_log_file)
            with open(live_log_file, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    stripped = line.strip()
                    if stripped and not _is_unrelated_scan(stripped):
                        raw_stream_candidates.append({
                            "timestamp": mtime,
                            "level": "INFO" if "INFO" in stripped else ("ERROR" if "ERROR" in stripped else "WARN"),
                            "logger": f"ExecutionAgents.{clean_id}",
                            "message": stripped,
                            "job_id": clean_id,
                        })
        except Exception:
            pass

    # C2. Sandbox directory live logs (terraform_runs/.../<clean_id>/live_logs.txt)
    if sandbox_path and os.path.exists(sandbox_path):
        for candidate_sb_name in ("live_logs.txt", "execution_logs.txt"):
            sb_log_path = os.path.join(sandbox_path, candidate_sb_name)
            if os.path.exists(sb_log_path):
                try:
                    mtime = os.path.getmtime(sb_log_path)
                    with open(sb_log_path, "r", encoding="utf-8", errors="replace") as f:
                        for line in f:
                            stripped = line.strip()
                            if stripped and not _is_unrelated_scan(stripped):
                                raw_stream_candidates.append({
                                    "timestamp": mtime,
                                    "level": "INFO" if "INFO" in stripped else ("ERROR" if "ERROR" in stripped else "WARN"),
                                    "logger": f"ExecutionAgents.{clean_id}",
                                    "message": stripped,
                                    "job_id": clean_id,
                                })
                except Exception:
                    pass

    # D. In-memory global _log_buffer entries matching this job
    for entry in _log_buffer:
        entry_jid = _clean_job_id(entry.get("job_id"))
        entry_logger = str(entry.get("logger", "")).lower()
        entry_msg = str(entry.get("message", "")).lower()

        matches_jid = (entry_jid == clean_id) or (clean_id in entry_logger) or (clean_id in entry_msg)
        matches_ticket = bool(ticket_lower and ticket_lower in entry_msg)
        matches_req = bool(req_id_lower and req_id_lower in entry_msg)

        if matches_jid or matches_ticket or matches_req:
            if not _is_unrelated_scan(str(entry.get("message", ""))):
                raw_stream_candidates.append(entry)

    # E. Read existing disk logs from logs/<clean_id>.log (extracting backend stream section)
    if os.path.exists(log_file_path):
        try:
            mtime = os.path.getmtime(log_file_path)
            with open(log_file_path, "r", encoding="utf-8", errors="replace") as f:
                in_backend_section = False
                has_report_markers = False
                for line in f:
                    stripped = line.rstrip()
                    if not stripped:
                        continue
                    if "CHANDRA CLOUD OPERATIONS — FULL EXECUTION & LIVE LOG REPORT" in stripped:
                        has_report_markers = True
                        continue
                    if "BACKEND LIVE LOG STREAM" in stripped:
                        in_backend_section = True
                        continue
                    if any(header in stripped for header in [
                        "TERRAFORM & INFRASTRUCTURE AUTOMATION LOGS",
                        "EXECUTION AUDIT TRAIL & LIFECYCLE PHASES",
                        "EXECUTION OUTCOME & VERIFIED RESOURCES",
                        "END OF LOG REPORT",
                    ]):
                        in_backend_section = False
                        continue
                    if stripped.startswith("========"):
                        continue
                    if any(stripped.startswith(prefix) for prefix in [
                        "Job ID:", "Task / Title:", "Jira Ticket:", "Status:", "Progress:",
                        "Message:", "Started At:", "Completed At:", "Sandbox Dir:", "Outcome:", "Outputs:"
                    ]):
                        if has_report_markers:
                            continue

                    if has_report_markers and not in_backend_section:
                        continue

                    if not _is_unrelated_scan(stripped):
                        raw_stream_candidates.append({
                            "timestamp": mtime,
                            "level": "INFO" if "INFO" in stripped else ("ERROR" if "ERROR" in stripped else "WARN"),
                            "logger": f"ExecutionAgents.{clean_id}",
                            "message": stripped,
                            "job_id": clean_id,
                        })
        except Exception:
            pass

    # 5. Gather Audit Trail and synthesize into structured lifecycle logs
    audit_trail = (
        meta_data.get("audit_trail")
        or (output_data.get("audit_trail") if isinstance(output_data, dict) else [])
        or (meta_data.get("result", {}).get("output", {}).get("audit_trail") if isinstance(meta_data.get("result"), dict) else [])
        or (meta_data.get("result", {}).get("audit_trail") if isinstance(meta_data.get("result"), dict) else [])
        or (job_info.get("result", {}).get("audit_trail") if isinstance(job_info.get("result"), dict) else [])
    )

    if audit_trail:
        for entry in audit_trail:
            if isinstance(entry, dict):
                at_t = entry.get("at", "")
                node = entry.get("node", "lifecycle")
                event = entry.get("event", "event")
                d_val = entry.get("data", {})
                d_str = f" {json.dumps(d_val)}" if d_val else ""
                
                # Parse timestamp for sorting
                ts_val = 0.0
                if at_t:
                    try:
                        clean_at = at_t.replace("Z", "+00:00")
                        ts_val = datetime.fromisoformat(clean_at).timestamp()
                    except Exception:
                        ts_val = time.time()
                
                formatted_audit_line = f"[{at_t}] [INFO] LifecycleAudit - [Node: {node}] {event}:{d_str}"
                raw_stream_candidates.append({
                    "timestamp": ts_val,
                    "level": "INFO",
                    "logger": f"Lifecycle.{node}",
                    "message": formatted_audit_line,
                    "job_id": clean_id,
                })

    # Deduplicate and sort raw stream logs
    seen_messages = set()
    sorted_stream_logs: List[str] = []

    # Sort by timestamp
    raw_stream_candidates.sort(key=lambda x: x.get("timestamp", 0) if isinstance(x.get("timestamp"), (int, float)) else 0)

    for entry in raw_stream_candidates:
        msg = entry.get("message", "")
        if not msg:
            continue
        # Normalize message for deduplication key
        try:
            norm_key = re.sub(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}[,\.\d]*\s*", "", msg).strip()
            norm_key = re.sub(r"^\[\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}[^\]]*\]\s*", "", norm_key).strip()
        except Exception:
            norm_key = msg.strip()

        if norm_key in seen_messages:
            continue
        seen_messages.add(norm_key)

        ts = entry.get("timestamp", 0)
        lvl = entry.get("level", "INFO")
        logger_name = entry.get("logger") or "system"

        try:
            is_preformatted = msg.startswith("[") or " [" in msg[:30] or bool(re.match(r"^\d{4}-\d{2}-\d{2}", msg))
        except Exception:
            is_preformatted = msg.startswith("[") or " [" in msg[:30]

        if is_preformatted:
            sorted_stream_logs.append(msg)
        else:
            try:
                ts_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S,%f")[:-3] if ts else datetime.now().strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
            except Exception:
                ts_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
            sorted_stream_logs.append(f"[{ts_str}] [{lvl}] {logger_name} - {msg}")

    # 6. Gather Terraform stdout / stderr
    exec_logs = (
        exec_meta.get("execution_logs")
        or exec_result_data.get("execution_logs")
        or exec_result_data.get("stdout")
        or output_data.get("execution", {}).get("execution_logs")
        or meta_data.get("result", {}).get("output", {}).get("execution", {}).get("execution_logs")
        or meta_data.get("result", {}).get("execution_logs")
    )

    # 7. Detail & Outputs
    detail = (
        exec_meta.get("detail")
        or exec_result_data.get("detail")
        or output_data.get("execution", {}).get("detail")
        or meta_data.get("result", {}).get("output", {}).get("execution", {}).get("detail")
    )
    outputs = (
        output_data.get("outputs")
        or exec_result_data.get("outputs")
        or meta_data.get("result", {}).get("output", {}).get("terraform_apply_result", {}).get("outputs")
        or meta_data.get("result", {}).get("output", {}).get("outputs")
    )

    lines = []
    lines.append("=" * 80)
    lines.append("CHANDRA CLOUD OPERATIONS — FULL EXECUTION & LIVE LOG REPORT")
    lines.append("=" * 80)
    lines.append(f"Job ID:          {clean_id}")
    lines.append(f"Task / Title:    {title}")
    lines.append(f"Jira Ticket:     {external_id}")
    lines.append(f"Status:          {str(status).upper()}")
    lines.append(f"Progress:        {progress}%")
    lines.append(f"Message:         {message}")
    lines.append(f"Started At:      {started_str}")
    lines.append(f"Completed At:    {completed_str}")
    lines.append(f"Sandbox Dir:     {sandbox_path}")
    lines.append("")

    if sorted_stream_logs:
        lines.append("=" * 80)
        lines.append("BACKEND LIVE LOG STREAM")
        lines.append("=" * 80)
        lines.extend(sorted_stream_logs)
        lines.append("")

    if exec_logs:
        lines.append("=" * 80)
        lines.append("TERRAFORM & INFRASTRUCTURE AUTOMATION LOGS (STDOUT / STDERR)")
        lines.append("=" * 80)
        lines.append(str(exec_logs).strip())
        lines.append("")

    if audit_trail:
        lines.append("=" * 80)
        lines.append("EXECUTION AUDIT TRAIL & LIFECYCLE PHASES")
        lines.append("=" * 80)
        for entry in audit_trail:
            if isinstance(entry, dict):
                at_t = entry.get("at", "")
                node = entry.get("node", "")
                event = entry.get("event", "")
                d_str = json.dumps(entry.get("data", {})) if entry.get("data") else ""
                lines.append(f"[{at_t}] [{node}] {event}: {d_str}")
        lines.append("")

    if detail or outputs:
        lines.append("=" * 80)
        lines.append("EXECUTION OUTCOME & VERIFIED RESOURCES")
        lines.append("=" * 80)
        if detail:
            lines.append(f"Outcome: {detail}")
        if outputs and isinstance(outputs, dict):
            lines.append("Outputs:")
            for k, v in outputs.items():
                val = v.get("value") if isinstance(v, dict) else v
                lines.append(f"  - {k}: {val}")
        lines.append("")

    lines.append("=" * 80)
    lines.append("END OF LOG REPORT")
    lines.append("=" * 80)

    content = "\n".join(lines)
    try:
        (app_root / "logs").mkdir(parents=True, exist_ok=True)
        with open(log_file_path, "w", encoding="utf-8") as f:
            f.write(content)
        if clean_id != _clean_job_id(job_id):
            with open(str(app_root / "logs" / f"{_clean_job_id(job_id)}.log"), "w", encoding="utf-8") as f:
                f.write(content)
        if sandbox_path and os.path.exists(sandbox_path):
            with open(os.path.join(sandbox_path, "live_logs.txt"), "w", encoding="utf-8") as f:
                f.write(content)
            with open(os.path.join(sandbox_path, "execution_logs.txt"), "w", encoding="utf-8") as f:
                f.write(content)
    except Exception:
        pass

    return content


@app.get("/download_sandbox")
def download_sandbox(path: Optional[str] = None, job_id: Optional[str] = None, jiraUrl: Optional[str] = None):
    """Zip and download the sandbox directory and full artifacts for a completed job."""
    import json
    import re
    app_root = Path(__file__).resolve().parent

    effective_id = _clean_job_id(job_id) or (os.path.basename(path.rstrip("/\\")) if path else None)
    if effective_id:
        effective_id = _clean_job_id(effective_id)

    # Check for Jira ticket in job_id, path, or jiraUrl
    jira_match = re.search(r'\b([A-Z][A-Z0-9]+-\d+)\b', f"{job_id or ''} {path or ''} {jiraUrl or ''}".upper())
    resolved_ticket = jira_match.group(1) if jira_match else None
    if resolved_ticket:
        if resolved_ticket in _ticket_to_job_id:
            effective_id = _clean_job_id(_ticket_to_job_id[resolved_ticket])
        else:
            with _job_store_lock:
                for jid, val in _job_store.items():
                    if val.get("external_id") == resolved_ticket or resolved_ticket in str(val.get("title", "")):
                        effective_id = _clean_job_id(jid)
                        break

    target_path = None
    raw_path = (path or "").strip()
    candidates = []

    if raw_path:
        candidates.append(raw_path)
        clean_rel = raw_path.replace("\\", "/").lstrip("/")
        candidates.extend([
            str((app_root / clean_rel).resolve()),
            os.path.join(os.getcwd(), clean_rel),
            os.path.abspath(raw_path),
        ])

    if effective_id:
        candidates.extend([
            str((app_root / "terraform_runs" / "default_worker" / effective_id).resolve()),
            str((app_root / "terraform_runs" / effective_id).resolve()),
            os.path.join("terraform_runs", "default_worker", effective_id),
        ])
        with _job_store_lock:
            for k in [effective_id, f"dw-{effective_id}"]:
                if k in _job_store:
                    sp = _job_store[k].get("sandbox_path")
                    if sp:
                        candidates.append(sp)
                        candidates.append(str((app_root / sp.replace("\\", "/").lstrip("/")).resolve()))

        for meta_name in [f"logs/{effective_id}.meta.json", f"logs/dw-{effective_id}.meta.json"]:
            meta_p = app_root / meta_name
            if meta_p.exists():
                try:
                    with open(meta_p, "r", encoding="utf-8") as f:
                        m_data = json.load(f)
                    m_sp = (
                        m_data.get("sandbox_path") 
                        or (m_data.get("result", {}).get("output", {}).get("execution", {}) or {}).get("sandbox_path")
                        or (m_data.get("result", {}).get("sandbox_path"))
                    )
                    if m_sp:
                        candidates.append(m_sp)
                        candidates.append(str((app_root / m_sp.replace("\\", "/").lstrip("/")).resolve()))
                except Exception:
                    pass

    # If still not found and resolved_ticket, inspect meta.json files
    if resolved_ticket:
        logs_dir = app_root / "logs"
        if logs_dir.exists():
            for meta_p in logs_dir.glob("*.meta.json"):
                try:
                    with open(meta_p, "r", encoding="utf-8") as f:
                        m_data = json.load(f)
                    if m_data.get("external_id") == resolved_ticket or resolved_ticket in str(m_data.get("title", "")):
                        effective_id = _clean_job_id(meta_p.name[:-10])
                        m_sp = m_data.get("sandbox_path")
                        if m_sp:
                            candidates.append(m_sp)
                            candidates.append(str((app_root / m_sp.replace("\\", "/").lstrip("/")).resolve()))
                except Exception:
                    pass

    for c in candidates:
        if c and os.path.exists(c):
            if os.path.isfile(c):
                c = os.path.dirname(c)
            if os.path.isdir(c):
                target_path = os.path.abspath(c)
                break

    full_logs = ""
    if effective_id:
        try:
            full_logs = _build_full_job_logs(effective_id)
        except Exception as e:
            logger.warning("Could not build full job logs for sandbox zip %s: %s", effective_id, e)

    if not full_logs and target_path and os.path.exists(target_path):
        for candidate_name in ("live_logs.txt", "execution_logs.txt"):
            c_p = os.path.join(target_path, candidate_name)
            if os.path.exists(c_p):
                try:
                    with open(c_p, "r", encoding="utf-8", errors="replace") as f:
                        full_logs = f.read()
                    if full_logs:
                        break
                except Exception:
                    pass

    # Always ensure live_logs.txt and execution_logs.txt on disk are up to date
    if target_path and os.path.exists(target_path) and full_logs:
        try:
            with open(os.path.join(target_path, "live_logs.txt"), "w", encoding="utf-8") as f:
                f.write(full_logs)
            with open(os.path.join(target_path, "execution_logs.txt"), "w", encoding="utf-8") as f:
                f.write(full_logs)
        except Exception:
            pass

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        written_arcnames = set()
        if target_path and os.path.exists(target_path):
            for root, dirs, files in os.walk(target_path):
                for skip in [".terraform", ".git", "__pycache__"]:
                    if skip in dirs:
                        dirs.remove(skip)
                for file in files:
                    # Skip disk live_logs/execution_logs if full_logs is available so we write the fresh version
                    if full_logs and file in ("live_logs.txt", "execution_logs.txt"):
                        continue
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, target_path)
                    zip_file.write(file_path, arcname)
                    written_arcnames.add(arcname.replace("\\", "/"))

            # Always write the definitive full_logs to the archive
            if full_logs:
                zip_file.writestr("live_logs.txt", full_logs)
                zip_file.writestr("execution_logs.txt", full_logs)
        else:
            job_info = _job_store.get(effective_id, {}) if effective_id else {}
            summary_content = {
                "job_id": effective_id or "unknown",
                "status": job_info.get("status", "completed"),
                "message": job_info.get("message", "Execution artifacts"),
                "result": job_info.get("result", {}),
            }
            zip_file.writestr("execution_summary.json", json.dumps(summary_content, indent=2))
            zip_file.writestr("README.txt", f"Chandra execution artifacts for job {effective_id}\n")
            if full_logs:
                zip_file.writestr("live_logs.txt", full_logs)
                zip_file.writestr("execution_logs.txt", full_logs)

    buffer.seek(0)
    dl_filename = f"{effective_id}_artifacts.zip" if effective_id else "execution_artifacts.zip"
    return StreamingResponse(
        buffer, 
        media_type="application/zip", 
        headers={
            "Content-Disposition": f"attachment; filename={dl_filename}",
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
        }
    )

class DestroyRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: Optional[str] = None
    job_id: Optional[str] = None
    jiraUrl: Optional[str] = None
    jira_url: Optional[str] = None
    jira_issue_key: Optional[str] = None
    jiraKey: Optional[str] = None

@app.post("/destroy_sandbox")
def destroy_sandbox(request: DestroyRequest):
    """Run terraform destroy on a completed sandbox directory."""
    import json
    import re
    app_root = Path(__file__).resolve().parent

    effective_id = request.job_id
    if effective_id:
        effective_id = str(effective_id).strip()
        if effective_id.startswith("dw-"):
            effective_id = effective_id[3:]
    
    # Extract Jira ticket key if present in request.jiraUrl, request.job_id, or request.path
    jira_raw = f"{request.jiraUrl or ''} {request.job_id or ''} {request.path or ''}"
    jira_match = re.search(r'\b([A-Z][A-Z0-9]+-\d+)\b', jira_raw.upper())
    resolved_ticket = jira_match.group(1) if jira_match else None

    # If effective_id is not a UUID or is empty, try resolving from ticket
    uuid_pattern = r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
    if resolved_ticket:
        if resolved_ticket in _ticket_to_job_id:
            effective_id = _clean_job_id(_ticket_to_job_id[resolved_ticket])
        else:
            with _job_store_lock:
                for jid, val in _job_store.items():
                    if val.get("external_id") == resolved_ticket or resolved_ticket in str(val.get("title", "")):
                        effective_id = _clean_job_id(jid)
                        break

    # If effective_id is still not provided or empty, extract from path
    if (not effective_id or not re.search(uuid_pattern, effective_id)) and request.path:
        uuid_match = re.search(uuid_pattern, request.path)
        if uuid_match:
            effective_id = uuid_match.group(0)

    if effective_id:
        _thread_local.job_id = effective_id
        
    target_path = None
    raw_path = (request.path or "").strip()
    candidates = []

    if raw_path:
        candidates.append(raw_path)
        # Strip leading slashes to prevent Windows os.path.join from dropping the parent directory
        clean_rel = raw_path.replace("\\", "/").lstrip("/")
        candidates.extend([
            str((app_root / clean_rel).resolve()),
            os.path.join(os.getcwd(), clean_rel),
            os.path.join(str(app_root), clean_rel),
            os.path.abspath(raw_path),
        ])

    if effective_id:
        eff_lower = effective_id.lower()
        eff_upper = effective_id.upper()
        candidates.extend([
            str((app_root / "terraform_runs" / "default_worker" / effective_id).resolve()),
            str((app_root / "terraform_runs" / "default_worker" / eff_lower).resolve()),
            str((app_root / "terraform_runs" / "default_worker" / eff_upper).resolve()),
            str((app_root / "terraform_runs" / effective_id).resolve()),
            str((app_root / "terraform_runs" / eff_lower).resolve()),
            os.path.join("terraform_runs", "default_worker", effective_id),
            os.path.join("terraform_runs", "default_worker", eff_lower),
        ])
        
        with _job_store_lock:
            for k in [effective_id, eff_lower, f"dw-{effective_id}", f"dw-{eff_lower}"]:
                if k in _job_store:
                    sp = _job_store[k].get("sandbox_path")
                    if sp:
                        candidates.append(sp)
                        sp_clean = sp.replace("\\", "/").lstrip("/")
                        candidates.append(str((app_root / sp_clean).resolve()))

        for meta_name in [f"{effective_id}.meta.json", f"{eff_lower}.meta.json"]:
            meta_file = app_root / "logs" / meta_name
            if meta_file.exists():
                try:
                    with open(meta_file, "r", encoding="utf-8") as f:
                        m_data = json.load(f)
                        m_sp = (
                            m_data.get("sandbox_path") 
                            or (m_data.get("result", {}).get("output", {}).get("execution", {}) or {}).get("sandbox_path")
                            or (m_data.get("result", {}).get("sandbox_path"))
                        )
                        if m_sp:
                            candidates.append(m_sp)
                            m_clean = m_sp.replace("\\", "/").lstrip("/")
                            candidates.append(str((app_root / m_clean).resolve()))
                except Exception:
                    pass

        # Glob match in terraform_runs as fallback
        tf_runs_dir = app_root / "terraform_runs"
        if tf_runs_dir.exists():
            for match in tf_runs_dir.rglob(f"*{eff_lower}*"):
                if match.is_dir():
                    candidates.append(str(match.resolve()))

    # Check for candidates matching resolved Jira ticket
    if resolved_ticket:
        logs_dir = app_root / "logs"
        if logs_dir.exists():
            for m_file in logs_dir.glob("*.meta.json"):
                try:
                    with open(m_file, "r", encoding="utf-8") as f:
                        m_data = json.load(f)
                    if m_data.get("external_id") == resolved_ticket or resolved_ticket in str(m_data.get("title", "")):
                        m_sp = m_data.get("sandbox_path")
                        if m_sp:
                            candidates.append(m_sp)
                            candidates.append(str((app_root / m_sp.replace("\\", "/").lstrip("/")).resolve()))
                except Exception:
                    pass

        tf_worker_dir = app_root / "terraform_runs" / "default_worker"
        if tf_worker_dir.exists():
            for run_dir in tf_worker_dir.iterdir():
                if run_dir.is_dir():
                    for check_file in ["execution_logs.txt", "live_logs.txt", "execution_result.json"]:
                        cf = run_dir / check_file
                        if cf.exists():
                            try:
                                with open(cf, "r", encoding="utf-8", errors="replace") as f:
                                    snippet = f.read(4000)
                                if resolved_ticket in snippet:
                                    candidates.append(str(run_dir.resolve()))
                                    break
                            except Exception:
                                pass

    for c in candidates:
        if c and os.path.exists(c):
            if os.path.isfile(c):
                c = os.path.dirname(c)
            if os.path.isdir(c):
                target_path = os.path.abspath(c)
                break

    jira_url = request.jiraUrl or resolved_ticket
    if not jira_url and effective_id:
        for meta_name in [f"{effective_id}.meta.json", f"{effective_id.lower()}.meta.json"]:
            meta_file = app_root / "logs" / meta_name
            if meta_file.exists():
                try:
                    with open(meta_file, "r", encoding="utf-8") as f:
                        m_data = json.load(f)
                        ext_id = (
                            m_data.get("external_id") 
                            or (m_data.get("result", {}).get("output", {}).get("request", {}) or {}).get("external_id")
                        )
                        if ext_id:
                            jira_url = ext_id
                            break
                except Exception:
                    pass

    if not target_path or not os.path.exists(target_path):
        logger.error(f"Sandbox directory not found for destroy request: path={request.path}, job_id={request.job_id}, effective_id={effective_id}, ticket={resolved_ticket}")
        # If sandbox already deleted from disk but ticket exists, still delete the Jira ticket
        if jira_url:
            try:
                from tools.jira_tools.create_jira_ticket import delete_jira_ticket
                issue_key = jira_url.rstrip("/").split("/")[-1]
                logger.info(f"Sandbox not found on disk, deleting Jira ticket {issue_key} directly...")
                delete_jira_ticket(issue_key)
                return JSONResponse(status_code=200, content={"status": "success", "message": f"Sandbox already removed; Jira ticket {issue_key} deleted successfully."})
            except Exception as j_err:
                logger.warning("Failed to delete Jira ticket on missing sandbox: %s", j_err)
        return JSONResponse(status_code=404, content={"error": "Sandbox not found"})

    env = os.environ.copy()
    tf_dir = os.environ.get("TERRAFORM_BIN_DIR")
    winget_dir = str(Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links")
    paths_to_add = [p for p in [tf_dir, winget_dir] if p and os.path.exists(p)]
    if paths_to_add:
        env["PATH"] = os.pathsep.join(paths_to_add) + os.pathsep + env.get("PATH", "")

    cache_dir = str((app_root / ".terraform_cache").resolve())
    if os.path.exists(cache_dir):
        env["TF_PLUGIN_CACHE_DIR"] = cache_dir

    local_tmp = str((app_root / "terraform_runs" / ".tmp").resolve())
    try:
        os.makedirs(local_tmp, exist_ok=True)
        env["TMP"] = local_tmp
        env["TEMP"] = local_tmp
    except Exception:
        pass

    if "AWS_DEFAULT_REGION" in env and "AWS_REGION" not in env:
        env["AWS_REGION"] = env["AWS_DEFAULT_REGION"]
    elif "AWS_REGION" in env and "AWS_DEFAULT_REGION" not in env:
        env["AWS_DEFAULT_REGION"] = env["AWS_REGION"]
        
    script_path = str((app_root / "scripts" / "destroy_terraform.py").resolve())
    cmd = [sys.executable, script_path, target_path]
    if jira_url:
        cmd.extend(["--jiraUrl", jira_url])
        
    try:
        logger.info(f"Starting infrastructure destruction for: {target_path}")
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            errors="replace"
        )
        
        output = []
        for line in proc.stdout:
            clean_line = line.rstrip('\n')
            output.append(clean_line)
            logger.info(clean_line)
            
        proc.wait()
        full_output = "\n".join(output)

        # Directly ensure Jira ticket is deleted from Jira upon destruction
        if jira_url:
            try:
                from tools.jira_tools.create_jira_ticket import delete_jira_ticket
                issue_key = jira_url.rstrip("/").split("/")[-1]
                logger.info(f"Ensuring Jira ticket {issue_key} is deleted...")
                del_res = delete_jira_ticket(issue_key)
                logger.info(f"Jira deletion response for {issue_key}: {del_res}")
            except Exception as j_err:
                logger.warning(f"Could not delete Jira ticket {jira_url} in destroy_sandbox: {j_err}")
        
        if proc.returncode != 0:
            return JSONResponse(status_code=500, content={"error": "Destroy failed", "details": full_output})

        if effective_id:
            eff_lower = effective_id.lower()
            with _job_store_lock:
                for k in [effective_id, eff_lower, f"dw-{effective_id}", f"dw-{eff_lower}"]:
                    if k in _job_store:
                        _job_store[k]["status"] = "destroyed"
                        
            for meta_name in [f"{effective_id}.meta.json", f"{eff_lower}.meta.json"]:
                meta_file = app_root / "logs" / meta_name
                if meta_file.exists():
                    try:
                        with open(meta_file, "r+", encoding="utf-8") as f:
                            m_data = json.load(f)
                            m_data["status"] = "destroyed"
                            f.seek(0)
                            json.dump(m_data, f, indent=2)
                            f.truncate()
                    except Exception:
                        pass

        return JSONResponse(status_code=200, content={"status": "success", "message": full_output})
    except Exception as e:
        logger.exception("Failed to destroy sandbox at %s: %s", target_path, e)
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.post("/delete_sandbox")
def delete_sandbox(request: DestroyRequest):
    """Delete a sandbox folder without running terraform destroy."""
    import shutil
    import re
    app_root = Path(__file__).resolve().parent
    effective_id = request.job_id
    if effective_id:
        effective_id = str(effective_id).strip()
        if effective_id.startswith("dw-"):
            effective_id = effective_id[3:]

    raw_path = (request.path or "").strip()
    target_path = None
    candidates = []
    if raw_path:
        candidates.append(raw_path)
        clean_rel = raw_path.replace("\\", "/").lstrip("/")
        candidates.extend([
            str((app_root / clean_rel).resolve()),
            os.path.join(os.getcwd(), clean_rel),
            os.path.abspath(raw_path),
        ])
    if effective_id:
        candidates.extend([
            str((app_root / "terraform_runs" / "default_worker" / effective_id).resolve()),
            str((app_root / "terraform_runs" / "default_worker" / effective_id.lower()).resolve()),
        ])

    for c in candidates:
        if c and os.path.exists(c) and os.path.isdir(c):
            target_path = Path(c)
            break

    try:
        if not target_path or not target_path.exists() or not target_path.is_dir():
            return JSONResponse(status_code=404, content={"error": f"Directory not found: {request.path}"})
        logger.info("Deleting sandbox folder: %s", target_path)
        shutil.rmtree(str(target_path), ignore_errors=True)
        logger.info("Sandbox folder deleted: %s", target_path)
        return JSONResponse(status_code=200, content={"status": "success", "message": f"Deleted {target_path}"})
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
    clean_id = _clean_job_id(job_id)
    resolved_ticket = (
        getattr(request, "jira_issue_key", None)
        or getattr(request, "jiraKey", None)
        or getattr(request, "jiraUrl", None)
        or getattr(request, "jira_url", None)
    )
    register_job_context(
        clean_id,
        ticket=resolved_ticket,
        sandbox_path=request.sandbox_path,
    )
    
    logger.info(
        "POST /orchestrate submitted | job_id=%s | action=%s | jiraUrl=%s",
        job_id,
        request.action.actionName,
        resolved_ticket or "None",
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
    model_config = ConfigDict(extra="ignore")
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
            result.get("status") == "needs_clarification" or
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
        clean_id = _clean_job_id(job_id)
        _thread_local.job_id = clean_id
        try:
            _active_job_id_cv.set(clean_id)
        except Exception:
            pass
        register_job_context(clean_id, thread_id=threading.get_ident())
        try:
            with _job_store_lock:
                _job_store[job_id]["thread_id"] = threading.get_ident()

            dw = get_digital_worker()
            if dw is None:
                raise RuntimeError("Digital Worker graph is not initialized")

            # Resume the DW graph with the user's answers or permission_set_id as the interrupt value
            from langgraph.types import Command as LGCommand
            
            resume_payload: Any = answers
            if request.permission_set_id:
                resume_payload = {"permission_set_id": request.permission_set_id}
                
            final_state = dw.invoke(
                LGCommand(resume=resume_payload),
                config=_dw_thread_config(job_id),
            )

            snapshot = dw.get_state(_dw_thread_config(job_id))
            
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
                if os.environ.get("CHANDRA_AUTO_APPROVE", "1").lower() in {"1", "true", "yes"}:
                    logger.info("DW RESUME [%s] auto-resuming Gate 2 execution review directly", job_id)
                    final_state = dw.invoke(
                        LGCommand(resume={"approved": True, "approver": "system", "comment": "Gate 2 auto-approved directly"}),
                        config=_dw_thread_config(job_id),
                    )
                    _dw_finalize_job(job_id, final_state, start_time)
                    return
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
            _notify_jira_job_failure(job_id, str(exc))
        finally:
            _thread_local.job_id = None

    def _run_execution_resume():
        """Resume a direct Execution Agent job (AWS Task or KRA)."""
        import time
        start_time = time.time()
        clean_id = _clean_job_id(job_id)
        _thread_local.job_id = clean_id
        try:
            _active_job_id_cv.set(clean_id)
        except Exception:
            pass
        register_job_context(clean_id, thread_id=threading.get_ident())
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

            if response.statusCode == 200:
                jira_ref = stored_action.get("jiraUrl") or _job_store[job_id].get("external_id") or _job_store[job_id].get("title")
                if jira_ref:
                    try:
                        from src.chandra.digital_worker.tracker import post_jira_completion
                        post_jira_completion(
                            issue_key_or_url=jira_ref,
                            action=stored_action,
                            sandbox_path=response.sandbox_path or sandbox_path,
                            summary=response.summary or "",
                            duration_seconds=int(time.time() - start_time),
                        )
                    except Exception as e:
                        logger.warning("Could not post Jira completion on resume: %s", e)
            elif response.statusCode >= 400:
                jira_ref = stored_action.get("jiraUrl") or _job_store[job_id].get("external_id") or _job_store[job_id].get("title")
                if jira_ref:
                    try:
                        from src.chandra.digital_worker.tracker import post_jira_failure
                        post_jira_failure(
                            issue_key_or_url=jira_ref,
                            error=response.summary or f"Execution failed with statusCode {response.statusCode}",
                            job_id=job_id,
                            action=stored_action,
                        )
                    except Exception as e:
                        logger.warning("Could not post Jira failure on resume: %s", e)
            
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
            _notify_jira_job_failure(job_id, str(exc))

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
def get_orchestrate_status(job_id: str):
    """Poll the status of a submitted orchestration job."""
    with _job_store_lock:
        if job_id not in _job_store:
            recovered = _recover_job_from_disk(job_id)
            if recovered:
                _job_store[job_id] = recovered
            else:
                return JobStatusResponse(
                    job_id=job_id,
                    status="not_found",
                    message="Job ID not found",
                    error="No job with this ID exists"
                )
        # Copy inside the lock so we don't race with the task thread modifying the dict
        job = dict(_job_store[job_id])

    return JobStatusResponse(job_id=job_id, **job)

class LogSyncPayload(BaseModel):
    logs: List[Dict[str, Any]] = Field(default_factory=list)

@app.post("/orchestrate/logs/{job_id}/sync")
def sync_job_logs(job_id: str, payload: LogSyncPayload):
    """Sync frontend-accumulated live logs for a job so they are permanently preserved."""
    clean_id = _clean_job_id(job_id)
    if not clean_id or not payload.logs:
        return {"status": "ok", "synced": 0}

    if clean_id not in _synced_frontend_logs:
        _synced_frontend_logs[clean_id] = []

    existing_msgs = {l.get("message") for l in _synced_frontend_logs[clean_id]}
    added = 0
    for l in payload.logs:
        msg = l.get("message")
        if msg and msg not in existing_msgs:
            _synced_frontend_logs[clean_id].append(l)
            existing_msgs.add(msg)
            added += 1

    try:
        _build_full_job_logs(clean_id)
    except Exception:
        pass

    return {"status": "ok", "synced": added, "total": len(_synced_frontend_logs[clean_id])}

@app.get("/orchestrate/logs/{job_id}")
def download_orchestrate_logs(job_id: str):
    """Download the full live logs for a specific orchestration or digital worker job."""
    from fastapi.responses import Response
    import re
    app_root = Path(__file__).resolve().parent
    clean_id = _clean_job_id(job_id)

    # Check if job_id was passed as a Jira ticket (e.g. DEV-1068)
    jira_match = re.search(r'\b([A-Z][A-Z0-9]+-\d+)\b', str(job_id).upper())
    resolved_ticket = jira_match.group(1) if jira_match else None
    if resolved_ticket and resolved_ticket in _ticket_to_job_id:
        clean_id = _clean_job_id(_ticket_to_job_id[resolved_ticket])
    elif resolved_ticket:
        with _job_store_lock:
            for jid, val in _job_store.items():
                if val.get("external_id") == resolved_ticket or resolved_ticket in str(val.get("title", "")):
                    clean_id = _clean_job_id(jid)
                    break
        if clean_id == _clean_job_id(job_id) or not clean_id:
            for m_file in (app_root / "logs").glob("*.meta.json"):
                try:
                    with open(m_file, "r", encoding="utf-8") as f:
                        m_val = json.load(f)
                    if m_val.get("external_id") == resolved_ticket:
                        clean_id = _clean_job_id(m_file.name[:-10])
                        break
                except Exception:
                    pass

    content = ""
    try:
        content = _build_full_job_logs(clean_id)
    except Exception as e:
        logger.warning("Could not build full job logs for download_orchestrate_logs %s: %s", clean_id, e)

    # If full live log content is available and substantial, return it directly
    if content and len(content.strip()) > 100:
        return Response(
            content=content,
            media_type='text/plain; charset=utf-8',
            headers={
                "Content-Disposition": f'attachment; filename="{clean_id}.log"',
                "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            }
        )

    # Fallback: check files on disk
    for candidate in [
        app_root / "logs" / f"{clean_id}.log",
        app_root / "logs" / f"dw-{clean_id}.log",
        app_root / "terraform_runs" / "default_worker" / clean_id / "live_logs.txt",
        app_root / "terraform_runs" / "default_worker" / clean_id / "execution_logs.txt",
    ]:
        if candidate.exists() and candidate.stat().st_size > 50:
            try:
                with open(candidate, "r", encoding="utf-8", errors="replace") as f:
                    disk_content = f.read()
                if len(disk_content.strip()) > 50:
                    return Response(
                        content=disk_content,
                        media_type='text/plain; charset=utf-8',
                        headers={
                            "Content-Disposition": f'attachment; filename="{clean_id}.log"',
                            "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
                            "Pragma": "no-cache",
                            "Expires": "0",
                        }
                    )
            except Exception:
                pass

    if content:
        return Response(
            content=content,
            media_type='text/plain; charset=utf-8',
            headers={
                "Content-Disposition": f'attachment; filename="{clean_id}.log"',
                "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            }
        )

    return Response(
        content=f"No logs found for job {clean_id}.",
        media_type='text/plain; charset=utf-8',
        headers={
            "Content-Disposition": f'attachment; filename="{clean_id}.log"',
            "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        }
    )

def _run_orchestration_task(job_id: str, request: OrchestrateRequest):
    """Background worker to run orchestration without blocking the API."""
    import time
    start_time = time.time()
    clean_id = _clean_job_id(job_id)
    _thread_local.job_id = clean_id
    try:
        _active_job_id_cv.set(clean_id)
    except Exception:
        pass
    resolved_ticket = (
        getattr(request, "jira_issue_key", None)
        or getattr(request, "jiraKey", None)
        or getattr(request, "jiraUrl", None)
        or getattr(request, "jira_url", None)
    )
    register_job_context(
        clean_id,
        thread_id=threading.get_ident(),
        ticket=resolved_ticket,
        sandbox_path=request.sandbox_path,
    )

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
        resolved_jira = (
            getattr(request, "jira_issue_key", None)
            or getattr(request, "jiraKey", None)
            or getattr(request, "jiraUrl", None)
            or getattr(request, "jira_url", None)
        )
        if resolved_jira:
            action_dict["jiraUrl"] = resolved_jira
            action_dict["jiraKey"] = resolved_jira

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

            if is_success:
                jira_ref = action_dict.get("jiraUrl") or request.jiraUrl or _job_store[job_id].get("external_id") or _job_store[job_id].get("title")
                if jira_ref:
                    try:
                        from src.chandra.digital_worker.tracker import post_jira_completion
                        post_jira_completion(
                            issue_key_or_url=jira_ref,
                            action=action_dict,
                            sandbox_path=response.sandbox_path or _job_store[job_id].get("sandbox_path"),
                            summary=response.summary or "",
                            duration_seconds=int(time.time() - start_time),
                        )
                    except Exception as e:
                        logger.warning("Could not post Jira completion on orchestrate: %s", e)
            else:
                jira_ref = action_dict.get("jiraUrl") or request.jiraUrl or _job_store[job_id].get("external_id") or _job_store[job_id].get("title")
                if jira_ref:
                    try:
                        from src.chandra.digital_worker.tracker import post_jira_failure
                        post_jira_failure(
                            issue_key_or_url=jira_ref,
                            error=response.summary or f"Orchestration completed with errors (statusCode={response.statusCode})",
                            job_id=job_id,
                            action=action_dict,
                        )
                    except Exception as e:
                        logger.warning("Could not post Jira failure on orchestrate: %s", e)

        logger.info(
            "ORCHESTRATION TASK [%s] completed | statusCode=%d | duration=%.1fs",
            job_id,
            response.statusCode,
            time.time() - start_time
        )
        try:
            _build_full_job_logs(job_id)
        except Exception as e:
            logger.warning("Could not build full job logs on orchestration completion: %s", e)

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
        _notify_jira_job_failure(job_id, str(exc))
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
    agent_name: Optional[str] = Field(default=None, description="Active digital worker agent name.")
    comment: str = Field(default="", description="Optional decision rationale.")
    permission_set_id: Optional[str] = Field(default=None, description="Optional permission set ID attached by Copilot.")
    permission_set_document: Optional[Dict[str, Any]] = Field(default=None, description="Optional mocked permission set for E2E testing.")


def _dw_thread_config(job_id: str) -> Dict[str, Any]:
    return {"configurable": {"thread_id": job_id}}


def _notify_jira_job_failure(job_id: str, error_detail: str) -> None:
    """Ensure a failure comment is always posted to Jira when an execution fails or crashes."""
    try:
        from src.chandra.digital_worker.tracker import post_jira_failure
        issue_key = None
        with _job_store_lock:
            job_info = _job_store.get(job_id, {})
            issue_key = job_info.get("external_id")
            if not issue_key:
                res = job_info.get("result") or {}
                app = res.get("approval_request") or {}
                issue_key = app.get("external_id")
            if not issue_key:
                act = job_info.get("action_dict") or {}
                issue_key = act.get("jiraUrl") or act.get("jira_issue_key")
            if not issue_key:
                title = job_info.get("title") or ""
                import re
                m = re.search(r"\b([A-Z][A-Z0-9]+-\d+)\b", str(title))
                if m:
                    issue_key = m.group(1)

        # Fallback 1: LangGraph checkpoint state
        if not issue_key:
            try:
                dw = get_digital_worker()
                if dw:
                    st = dw.get_state(_dw_thread_config(job_id))
                    if st and st.values:
                        req = st.values.get("request")
                        if req:
                            issue_key = getattr(req, "external_id", None) or (req.get("external_id") if isinstance(req, dict) else None)
            except Exception:
                pass

        # Fallback 2: Disk metadata
        if not issue_key:
            try:
                meta_file = f"logs/{job_id}.meta.json"
                if os.path.exists(meta_file):
                    import json
                    with open(meta_file, "r", encoding="utf-8") as f:
                        m = json.load(f)
                        issue_key = m.get("external_id") or m.get("jira_key")
            except Exception:
                pass

        if issue_key:
            post_jira_failure(issue_key, error=error_detail, job_id=job_id)
        else:
            logger.warning("Could not resolve Jira issue_key for failed job %s", job_id)
    except Exception as e:
        logger.warning("Could not post Jira failure comment for job %s: %s", job_id, e)


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

        # Set status based on governed execution results
        tf_res = final_state.get("terraform_apply_result") or {}
        if tf_res.get("dry_run") or final_state.get("final_status") == "INDETERMINATE" or (execution and getattr(execution, "dry_run", False)) or (execution and getattr(execution, "status", None) == "dry_run"):
            actual_status = "dry_run"
        elif final_state.get("final_status") == "FAILED" or (
            tf_res and not tf_res.get("success") and not tf_res.get("dry_run")
        ):
            actual_status = "failed"
        elif tf_res.get("success") or final_state.get("final_status") == "COMPLETED":
            actual_status = "completed"

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
        if actual_status == "failed":
            fail_reason = (
                final_state.get("terraform_apply_result", {}).get("detail")
                or (execution.detail if execution and hasattr(execution, "detail") else None)
                or _job_store[job_id].get("error")
                or _job_store[job_id].get("message")
                or "Workflow execution failed"
            )
            _notify_jira_job_failure(job_id, str(fail_reason))
        
        sandbox_path = None
        if execution:
            sandbox_path = execution.get("sandbox_path") if isinstance(execution, dict) else getattr(execution, "sandbox_path", None)
        if not sandbox_path:
            sandbox_path = final_state.get("sandbox_path") or _job_store[job_id].get("sandbox_path")
        if not sandbox_path:
            candidate = os.path.join("terraform_runs", "default_worker", job_id)
            if os.path.exists(candidate):
                sandbox_path = candidate

        if sandbox_path:
            _job_store[job_id]["sandbox_path"] = sandbox_path
            # Write execution_result.json into sandbox so /download_sandbox artifact zip includes it
            try:
                import json
                from pathlib import Path
                s_dir = Path(sandbox_path)
                if s_dir.exists() and s_dir.is_dir():
                    res_path = s_dir / "execution_result.json"
                    with res_path.open("w", encoding="utf-8") as rf:
                        json.dump({
                            "job_id": job_id,
                            "status": actual_status,
                            "message": _job_store[job_id]["message"],
                            "sandbox_path": str(s_dir),
                            "outputs": final_state.get("terraform_apply_result", {}).get("outputs", {}),
                        }, rf, indent=2, ensure_ascii=False)
            except Exception as e:
                logger.warning("Could not write execution_result.json to sandbox: %s", e)

        # Compile full execution & live logs report to logs/<job_id>.log and sandbox files
        try:
            _build_full_job_logs(job_id)
        except Exception as e:
            logger.warning("Could not build full job logs on job finalization: %s", e)


def _run_digital_worker_task(job_id: str, submission: CloudRequestSubmission) -> None:
    """Background worker for /requests and /webhooks/{source}."""
    import time
    import threading
    start_time = time.time()
    clean_id = _clean_job_id(job_id)
    _thread_local.job_id = clean_id
    try:
        _active_job_id_cv.set(clean_id)
    except Exception:
        pass

    ticket = None
    if isinstance(submission.payload, dict):
        ticket = submission.payload.get("issue", {}).get("key") or submission.payload.get("key")
    register_job_context(
        clean_id,
        thread_id=threading.get_ident(),
        ticket=ticket,
        sandbox_path=os.path.join("terraform_runs", "default_worker", clean_id)
    )
    try:
        dw = get_digital_worker()
        if dw is None:
            raise RuntimeError("Digital Worker graph is not initialized")
        with _job_store_lock:
            _job_store[job_id]["status"] = "running"
            _job_store[job_id]["job_type"] = "dw"
            _job_store[job_id]["started_at"] = start_time
            _job_store[job_id]["progress"] = 10
            _job_store[job_id]["message"] = f"Processing {submission.source} request..."
            _job_store[job_id]["thread_id"] = threading.get_ident()

        final_state = dw.invoke(
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
        snapshot = dw.get_state(_dw_thread_config(job_id))
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
            
        # Handle permission selection pause
        if snapshot.next and "permission_selection_pause" in snapshot.next:
            interrupts = snapshot.tasks[0].interrupts if snapshot.tasks else []
            interrupt_val = interrupts[0].value if interrupts else {}
            with _job_store_lock:
                _job_store[job_id]["status"] = "awaiting_permission"
                _job_store[job_id]["progress"] = 75
                _job_store[job_id]["message"] = "Awaiting Copilot permission attachment"
                _job_store[job_id]["result"] = interrupt_val
            logger.info("DIGITAL WORKER JOB [%s] awaiting permission attachment", job_id)
            return

        # Handle Gate 2 execution review pause (governed Jira path)
        if snapshot.next and "gate_2_review" in snapshot.next:
            if os.environ.get("CHANDRA_AUTO_APPROVE", "1").lower() in {"1", "true", "yes"}:
                from langgraph.types import Command as _LGCommand
                logger.info("DIGITAL WORKER JOB [%s] auto-resuming Gate 2 execution review directly", job_id)
                final_state = dw.invoke(
                    _LGCommand(resume={"approved": True, "approver": "system", "comment": "Gate 2 auto-approved directly"}),
                    config=_dw_thread_config(job_id),
                )
                _dw_finalize_job(job_id, final_state, start_time)
                return
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
            logger.info("DIGITAL WORKER JOB [%s] awaiting Gate 2 execution review", job_id)
            return

        # Handle Execution Agent HITL pause!
        if snapshot.next and "execute_automation" in snapshot.next:
            interrupts = snapshot.tasks[0].interrupts if snapshot.tasks else []
            interrupt_val = interrupts[0].value if interrupts else {}
            questions = interrupt_val.get("questions", ["Please provide the required input to proceed."])
            summary = interrupt_val.get("summary", "Awaiting input")
            interrupt_type = interrupt_val.get("type", "clarification")

            with _job_store_lock:
                if interrupt_type == "gate2_approval":
                    _job_store[job_id]["status"] = "awaiting_gate2"
                else:
                    _job_store[job_id]["status"] = "completed"
                _job_store[job_id]["progress"] = 100
                _job_store[job_id]["message"] = "Awaiting user input"
                _job_store[job_id]["job_type"] = "dw"
                _job_store[job_id]["result"] = {
                    "statusCode": 202,
                    "status": "needs_clarification",
                    "type": interrupt_type,
                    "thread_id": job_id,
                    "questions": questions,
                    "summary": summary,
                }
            logger.info("DIGITAL WORKER JOB [%s] paused for HITL (%s)", job_id, interrupt_type)
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
        _notify_jira_job_failure(job_id, str(exc))
    finally:
        _thread_local.job_id = None


def _resume_digital_worker_task(job_id: str, approval: ApprovalSubmission) -> None:
    """Background worker for /requests/{job_id}/approve — resumes the interrupt."""
    import time
    import threading
    from langgraph.types import Command

    start_time = time.time()
    clean_id = _clean_job_id(job_id)
    _thread_local.job_id = clean_id
    try:
        _active_job_id_cv.set(clean_id)
    except Exception:
        pass
    register_job_context(clean_id, thread_id=threading.get_ident())
    try:
        dw = get_digital_worker()
        if dw is None:
            raise RuntimeError("Digital Worker graph is not initialized")
            
        with _job_store_lock:
            # Check the current status before changing to running
            current_status = _job_store[job_id].get("status")
            _job_store[job_id]["status"] = "running"
            _job_store[job_id]["job_type"] = "dw"
            _job_store[job_id]["progress"] = 80
            _job_store[job_id]["message"] = "Resuming after approval decision..."
            _job_store[job_id]["thread_id"] = threading.get_ident()
            # Flag that this job was explicitly approved by a human
            _job_store[job_id]["approved_by_human"] = True
            _job_store[job_id]["approved_by"] = approval.approver or "operator"
            _job_store[job_id]["requires_approval"] = False
            if approval.agent_name and str(approval.agent_name).strip():
                try:
                    from src.chandra.digital_worker.tracker import set_active_agent_name
                    set_active_agent_name(str(approval.agent_name).strip())
                except Exception:
                    pass
            
        # Resolve permission_set_document if not provided
        perm_doc = approval.permission_set_document
        if not perm_doc and approval.permission_set_id:
            try:
                all_perms = _load_aws_permissions_from_disk()
                for p in all_perms:
                    if p.get("id") == approval.permission_set_id or p.get("name", "").lower() == approval.permission_set_id.lower():
                        perm_doc = p
                        break
            except Exception:
                pass
        if perm_doc is None:
            perm_doc = {}

        # Route resume payload based on which gate we're at
        if current_status == "awaiting_permission":
            resume_payload = {
                "permission_set_id": approval.permission_set_id,
                "permission_set_document": perm_doc,
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
            resume_payload["permission_set_document"] = perm_doc

        final_state = dw.invoke(
            Command(resume=resume_payload),
            config=_dw_thread_config(job_id),
        )

        snapshot = dw.get_state(_dw_thread_config(job_id))

        # Handle permission selection pause
        if snapshot.next and "permission_selection_pause" in snapshot.next:
            interrupts = snapshot.tasks[0].interrupts if snapshot.tasks else []
            interrupt_val = interrupts[0].value if interrupts else {}
            with _job_store_lock:
                _job_store[job_id]["status"] = "awaiting_permission"
                _job_store[job_id]["progress"] = 75
                _job_store[job_id]["message"] = "Awaiting Copilot permission attachment"
                _job_store[job_id]["result"] = interrupt_val
            logger.info("DIGITAL WORKER JOB [%s] awaiting permission attachment", job_id)
            return

        # Handle Gate 2 execution review pause (governed Jira path)
        if snapshot.next and "gate_2_review" in snapshot.next:
            if os.environ.get("CHANDRA_AUTO_APPROVE", "1").lower() in {"1", "true", "yes"}:
                logger.info("DIGITAL WORKER JOB [%s] auto-resuming Gate 2 execution review directly", job_id)
                final_state = dw.invoke(
                    Command(resume={"approved": True, "approver": "system", "comment": "Gate 2 auto-approved directly"}),
                    config=_dw_thread_config(job_id),
                )
                _dw_finalize_job(job_id, final_state, start_time)
                return
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
            logger.info("DIGITAL WORKER JOB [%s] awaiting Gate 2 execution review", job_id)
            return

        # Handle Execution Agent HITL pause
        if snapshot.next and "execute_automation" in snapshot.next:
            interrupts = snapshot.tasks[0].interrupts if snapshot.tasks else []
            interrupt_val = interrupts[0].value if interrupts else {}
            questions = interrupt_val.get("questions", ["Please provide the required input to proceed."])
            summary = interrupt_val.get("summary", "Awaiting input")
            interrupt_type = interrupt_val.get("type", "clarification")

            with _job_store_lock:
                if interrupt_type == "gate2_approval":
                    _job_store[job_id]["status"] = "awaiting_gate2"
                else:
                    _job_store[job_id]["status"] = "completed"
                _job_store[job_id]["progress"] = 100
                _job_store[job_id]["message"] = "Awaiting user input"
                _job_store[job_id]["job_type"] = "dw"
                _job_store[job_id]["result"] = {
                    "statusCode": 202,
                    "status": "needs_clarification",
                    "type": interrupt_type,
                    "thread_id": job_id,
                    "questions": questions,
                    "summary": summary,
                }
            logger.info("DIGITAL WORKER JOB [%s] paused for HITL (%s)", job_id, interrupt_type)
            return

        _dw_finalize_job(job_id, final_state, start_time)
        logger.info(
            "DIGITAL WORKER JOB [%s] resumed (approved=%s) and completed",
            job_id, approval.approved if hasattr(approval, "approved") else True,
        )
    except Exception as exc:
        logger.exception("DIGITAL WORKER JOB [%s] resume failed", job_id)
        with _job_store_lock:
            _job_store[job_id]["status"] = "failed"
            _job_store[job_id]["error"] = str(exc)
            _job_store[job_id]["completed_at"] = time.time()
            _job_store[job_id]["message"] = f"Resume failed: {str(exc)[:200]}"
        _notify_jira_job_failure(job_id, str(exc))
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
        "external_id": approval.get("external_id") or request.get("external_id") or job.get("external_id"),
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
    raw_source = (submission.source or "").lower().strip()
    if raw_source in ("onboarding", "portal", "ui", "wizard"):
        submission.source = "rest_api"

    if submission.source not in SUPPORTED_SOURCES:
        return JSONResponse(status_code=400, content={
            "status": "error",
            "message": f"Unsupported source '{submission.source}'. Expected one of: {', '.join(SUPPORTED_SOURCES)}",
        })
        
    dw = get_digital_worker()
    if dw is None:
        return JSONResponse(status_code=503, content={
            "status": "error", "message": "Digital Worker graph is not initialized",
        })

    payload = submission.payload or {}
    issue = payload.get("issue") or {}
    jira_key = issue.get("key") if isinstance(issue, dict) else None
    if not jira_key and isinstance(payload, dict):
        jira_key = payload.get("key") or payload.get("issue_key")
    if jira_key and submission.source == "jira":
        with _job_store_lock:
            for jid, existing in _job_store.items():
                if existing.get("source") == "jira":
                    res = existing.get("result") or {}
                    app = res.get("approval_request") or {}
                    ext = app.get("external_id") or existing.get("external_id") or ""
                    title = existing.get("title") or ""
                    if (jira_key in ext or jira_key in title) and existing.get("status") in ("awaiting_approval", "running", "pending"):
                        logger.info("Jira ticket %s already active in job %s (%s) — returning existing job", jira_key, jid, existing.get("status"))
                        return JSONResponse(status_code=202, content={
                            "job_id": jid, "status": "accepted",
                            "message": f"Job {jid} already processing {jira_key}",
                            "poll_url": f"/jobs/status/{jid}",
                        })

    job_id = str(uuid.uuid4())
    logger.info("Digital Worker request submitted -> job_id=%s source=%s", job_id, submission.source)
    import time
    with _job_store_lock:
        _job_store[job_id] = {
            # ``kind`` tags this job as a Digital Worker request so the
            # GET /requests discovery endpoint can list it apart from the
            # legacy observation/orchestrate jobs that share _job_store.
            "kind": "digital_worker",
            "source": submission.source,
            "title": _dw_submission_title(submission),
            "external_id": jira_key,
            "dry_run": submission.dry_run,
            "submitted_at": (submission.payload.get("submitted_at") if isinstance(submission.payload, dict) else None) or time.time(),
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


@app.get("/")
def root_status():
    """Root endpoint for status check and reverse proxy validation."""
    return {"status": "ok", "service": "Chandra Digital Worker", "webhooks_url": "/webhooks/jira"}


@app.get("/webhooks/{source}")
def webhook_source_status(source: str):
    """Liveness probe for a specific webhook channel."""
    return {"status": "ok", "channel": source, "method": "POST required"}


@app.post("/")
def receive_root_webhook(
    payload: Dict[str, Any] = Body(default_factory=dict),
    x_chandra_webhook_token: Optional[str] = Header(default=None),
    token: Optional[str] = Query(default=None),
):
    """Fallback handler when webhooks are configured with the root URL (e.g. ngrok root URL).

    Automatically detects and routes Jira, Slack, Teams, or Cloud monitoring payloads.
    """
    logger.info("Received POST / at root URL (fallback routing)")
    # Detect Jira
    if (
        "issue" in payload
        or "webhookEvent" in payload
        or payload.get("issue_event_type_name")
        or "jira" in str(payload.get("webhookEvent", "")).lower()
        or (isinstance(payload.get("issue"), dict) and "key" in payload["issue"])
    ):
        return _process_webhook("jira", payload, None, x_chandra_webhook_token, token)
    # Detect Slack
    if payload.get("type") == "url_verification" or "event" in payload or "challenge" in payload:
        return _process_webhook("slack", payload, None, x_chandra_webhook_token, token)
    # Detect Teams
    if "teams" in str(payload).lower() or payload.get("type") == "message":
        return _process_webhook("teams", payload, None, x_chandra_webhook_token, token)
    # Default to Jira webhook if payload looks like an issue or has key/fields
    if "key" in payload or "fields" in payload:
        return _process_webhook("jira", payload, None, x_chandra_webhook_token, token)
    # Default to generic webhook
    return _process_webhook("webhook", payload, None, x_chandra_webhook_token, token)


@app.post("/webhooks/{source}/{path_token}")
def receive_webhook_with_path(
    source: str,
    path_token: str,
    payload: Dict[str, Any] = Body(default_factory=dict),
    x_chandra_webhook_token: Optional[str] = Header(default=None),
    token: Optional[str] = Query(default=None),
):
    return _process_webhook(source, payload or {}, path_token, x_chandra_webhook_token, token)

@app.post("/webhooks/{source}")
@app.post("/webhooks/{source}/")
def receive_webhook(
    source: str,
    payload: Dict[str, Any] = Body(default_factory=dict),
    x_chandra_webhook_token: Optional[str] = Header(default=None),
    token: Optional[str] = Query(default=None),
):
    return _process_webhook(source, payload or {}, None, x_chandra_webhook_token, token)


def _process_webhook(
    source: str,
    payload: Dict[str, Any],
    path_token: Optional[str] = None,
    x_chandra_webhook_token: Optional[str] = None,
    token: Optional[str] = None,
):
    try:
        import json
        logger.info(f"Received webhook from {source} with payload: {json.dumps(payload, default=str)}")
    except Exception:
        logger.info(f"Received webhook from {source} with non-serializable payload")
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
        logger.warning("Webhook rejected: bad or missing X-Chandra-Webhook-Token (source=%s)", source)
        return JSONResponse(status_code=401, content={
            "status": "error", "message": "invalid or missing X-Chandra-Webhook-Token",
        })
    dry_run = payload.get("dry_run", False)
    return _submit_digital_worker_job(
        CloudRequestSubmission(source=source, payload=payload, dry_run=dry_run)
    )


@app.post("/requests/{job_id}/approve")
@app.post("/requests/{job_id}/approve/")
def approve_cloud_request(job_id: str, approval: ApprovalSubmission):
    """Approve or reject a workflow paused at the human approval gate (or attach permissions)."""
    if approval.agent_name and str(approval.agent_name).strip():
        try:
            from src.chandra.digital_worker.tracker import set_active_agent_name
            set_active_agent_name(str(approval.agent_name).strip())
        except Exception:
            pass
    with _job_store_lock:
        job = _job_store.get(job_id)
        if job is None:
            return JSONResponse(status_code=404, content={"error": "Job not found"})
        if job.get("status") not in ["awaiting_approval", "awaiting_permission", "awaiting_gate2"]:
            return JSONResponse(status_code=409, content={
                "error": f"Job is '{job.get('status')}', not awaiting_approval/awaiting_permission/awaiting_gate2",
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
    agent_name: Optional[str] = Field(default=None, description="Onboarded agent name.")
    onboarded_at: Optional[float] = Field(default=None, description="Timestamp when agent was onboarded.")

@app.get("/settings/digital-worker", response_model=DigitalWorkerSettings)
@app.get("/settings/digital-worker/", response_model=DigitalWorkerSettings)
def get_digital_worker_settings():
    """Get the global digital worker settings."""
    config_path = os.path.join(os.path.dirname(__file__), "digital_worker_config.json")
    data = {}
    if os.path.exists(config_path):
        import json
        try:
            with open(config_path, "r") as f:
                data = json.load(f)
        except Exception as e:
            logger.warning("Failed to load digital_worker_config.json: %s", e)
    try:
        from src.chandra.digital_worker.tracker import get_active_agent_name, get_active_agent_onboarded_at
        if "agent_name" not in data or not data["agent_name"]:
            data["agent_name"] = get_active_agent_name()
        if "onboarded_at" not in data or not data["onboarded_at"]:
            data["onboarded_at"] = get_active_agent_onboarded_at()
    except Exception:
        pass
    return DigitalWorkerSettings(**data)

@app.post("/settings/digital-worker")
@app.post("/settings/digital-worker/")
def update_digital_worker_settings(settings: DigitalWorkerSettings):
    """Update the global digital worker settings."""
    config_path = os.path.join(os.path.dirname(__file__), "digital_worker_config.json")
    import json
    try:
        existing = {}
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            except Exception:
                pass
        data = existing.copy()
        data["max_iterations"] = settings.max_iterations
        data["command_timeout"] = settings.command_timeout

        new_name = (settings.agent_name or "").strip()
        if new_name and new_name.upper() not in ("DFTE", "CONSOLE", "OPERATOR", "SYSTEM", "HUMAN APPROVER", "UNKNOWN", "SUGAR BABY"):
            data["agent_name"] = new_name
            try:
                from src.chandra.digital_worker.tracker import set_active_agent_name, get_active_agent_onboarded_at
                set_active_agent_name(new_name)
                if not data.get("onboarded_at"):
                    data["onboarded_at"] = get_active_agent_onboarded_at()
            except Exception:
                pass
        elif "agent_name" not in data or not data["agent_name"]:
            data["agent_name"] = new_name or "CHANDRA DIGITAL WORKER"

        if settings.onboarded_at:
            data["onboarded_at"] = settings.onboarded_at
        elif not data.get("onboarded_at"):
            import time
            data["onboarded_at"] = time.time()

        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
        return {"status": "success"}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/requests/sync-jira")
@app.post("/webhooks/jira/sync")
def sync_jira_issues(
    limit: int = Query(default=10, ge=1, le=50),
    project: Optional[str] = Query(default=None),
):
    """Pull recent Jira issues directly from Jira REST API and submit any that haven't been processed yet."""
    try:
        from src.chandra.digital_worker.tracker import _jira_client
        client = _jira_client()
        if client is None:
            return JSONResponse(status_code=503, content={
                "status": "error",
                "message": "Jira is not configured (missing JIRA_SERVER, JIRA_EMAIL, or JIRA_API_TOKEN)"
            })
        
        jql = f"project = {project} AND created >= -30d ORDER BY created DESC" if project else "created >= -30d ORDER BY created DESC"
        issues = client.search_issues(jql, maxResults=limit)
        imported = []
        already_present = []
        for issue in issues:
            key = issue.key
            # Check if this issue is already in _job_store
            has_job = False
            with _job_store_lock:
                for jid, existing in _job_store.items():
                    if existing.get("kind") == "digital_worker":
                        res = existing.get("result") or {}
                        app = res.get("approval_request") or {}
                        ext = app.get("external_id") or existing.get("external_id") or ""
                        title = existing.get("title") or ""
                        if key == ext or key in ext or key in title:
                            has_job = True
                            break
            if has_job:
                already_present.append(key)
                continue

            # Submit issue as digital worker request
            raw_desc = getattr(issue.fields, "description", "") or ""
            payload = {
                "issue": {
                    "key": key,
                    "fields": {
                        "summary": issue.fields.summary,
                        "description": raw_desc,
                        "priority": {"name": getattr(issue.fields.priority, "name", "P3") if getattr(issue.fields, "priority", None) else "P3"},
                        "reporter": {"displayName": getattr(issue.fields.reporter, "displayName", "Jira User") if getattr(issue.fields, "reporter", None) else "Jira User"},
                        "labels": getattr(issue.fields, "labels", []) or []
                    }
                }
            }
            sub = CloudRequestSubmission(source="jira", payload=payload, dry_run=False)
            res = _submit_digital_worker_job(sub)
            imported.append(key)

        return JSONResponse(status_code=200, content={
            "status": "ok",
            "imported": imported,
            "already_present": already_present,
            "count": len(imported),
            "message": f"Synced {len(imported)} new Jira issue(s) ({len(already_present)} already tracked)"
        })
    except Exception as exc:
        logger.exception("Failed to sync Jira issues: %s", exc)
        return JSONResponse(status_code=500, content={
            "status": "error",
            "message": f"Failed to sync Jira issues: {exc}"
        })


_last_jira_auto_sync: float = 0.0

def _trigger_background_jira_sync():
    """Lightweight, non-blocking background check for recent Jira tickets (runs at most once every 5 seconds)."""
    global _last_jira_auto_sync
    import time
    now = time.time()
    if now - _last_jira_auto_sync < 5.0:
        return
    _last_jira_auto_sync = now

    def _sync_worker():
        try:
            from src.chandra.digital_worker.tracker import _jira_client, get_active_agent_onboarded_at
            from src.chandra.digital_worker.intake import _extract_jira_description
            from datetime import datetime
            import os
            import requests
            from requests.auth import HTTPBasicAuth

            agent_onboarded_at = get_active_agent_onboarded_at()
            issues_data = []

            # Method 1: Try JIRA client with bounded query
            client = _jira_client()
            if client is not None:
                try:
                    # Bounded query: Atlassian Cloud strictly forbids unbounded JQL queries like "ORDER BY created DESC"
                    jql = "created >= -7d ORDER BY created DESC"
                    issues = client.search_issues(jql, maxResults=10)
                    for issue in issues:
                        raw_desc = getattr(issue.fields, "description", "") or ""
                        clean_desc = _extract_jira_description(raw_desc) or getattr(issue.fields, "summary", "")
                        raw_created = getattr(issue.fields, "created", None)
                        p_name = getattr(issue.fields.priority, "name", "P3") if getattr(issue.fields, "priority", None) else "P3"
                        r_name = getattr(issue.fields.reporter, "displayName", "Jira User") if getattr(issue.fields, "reporter", None) else "Jira User"
                        labels = getattr(issue.fields, "labels", []) or []
                        issues_data.append({
                            "key": issue.key,
                            "summary": getattr(issue.fields, "summary", "Jira request"),
                            "description": clean_desc,
                            "priority": p_name,
                            "reporter": r_name,
                            "labels": labels,
                            "created": raw_created,
                        })
                except Exception as e:
                    logger.warning("client.search_issues failed in background sync: %s", e)

            # Method 2: Direct REST fallback if JIRA client had an issue or returned empty
            if not issues_data:
                server = (os.getenv("JIRA_SERVER") or "").strip().rstrip("/")
                email = (os.getenv("JIRA_EMAIL") or "").strip()
                token = (os.getenv("JIRA_API_TOKEN") or "").strip()
                if server and email and token:
                    try:
                        url = f"{server}/rest/api/2/search"
                        resp = requests.get(
                            url,
                            params={"jql": "created >= -7d ORDER BY created DESC", "maxResults": 10},
                            auth=HTTPBasicAuth(email, token),
                            headers={"Accept": "application/json"},
                            timeout=6,
                        )
                        if resp.status_code == 200:
                            data = resp.json()
                            for item in data.get("issues", []):
                                fields = item.get("fields", {})
                                raw_desc = fields.get("description", "") or ""
                                clean_desc = _extract_jira_description(raw_desc) or fields.get("summary", "")
                                p_obj = fields.get("priority") or {}
                                r_obj = fields.get("reporter") or {}
                                issues_data.append({
                                    "key": item.get("key"),
                                    "summary": fields.get("summary", "Jira request"),
                                    "description": clean_desc,
                                    "priority": p_obj.get("name", "P3") if isinstance(p_obj, dict) else "P3",
                                    "reporter": r_obj.get("displayName", "Jira User") if isinstance(r_obj, dict) else "Jira User",
                                    "labels": fields.get("labels", []) or [],
                                    "created": fields.get("created"),
                                })
                    except Exception as e:
                        logger.warning("Direct Jira REST search failed: %s", e)

            # Process discovered issues
            for item in issues_data:
                key = item.get("key")
                if not key:
                    continue

                # Filter out tickets created before current active agent was onboarded
                issue_ts = None
                raw_created = item.get("created")
                if raw_created:
                    try:
                        dt = datetime.fromisoformat(str(raw_created).replace("Z", "+00:00"))
                        issue_ts = dt.timestamp()
                    except Exception:
                        pass

                if agent_onboarded_at and issue_ts is not None:
                    # Allow 60s tolerance for clock drift
                    if issue_ts < (agent_onboarded_at - 60):
                        continue

                # Check if this issue is already in _job_store
                has_job = False
                with _job_store_lock:
                    for jid, existing in _job_store.items():
                        if existing.get("kind") == "digital_worker":
                            res = existing.get("result") or {}
                            app = res.get("approval_request") or {}
                            ext = app.get("external_id") or existing.get("external_id") or ""
                            title = existing.get("title") or ""
                            if key == ext or key in ext or key in title:
                                has_job = True
                                break

                if not has_job:
                    logger.info("Background Jira sync: discovered new ticket %s ('%s') — ingesting now", key, item.get("summary"))
                    payload = {
                        "issue": {
                            "key": key,
                            "fields": {
                                "summary": item.get("summary"),
                                "description": item.get("description"),
                                "priority": {"name": item.get("priority", "P3")},
                                "reporter": {"displayName": item.get("reporter", "Jira User")},
                                "labels": item.get("labels", []),
                            }
                        }
                    }
                    if issue_ts:
                        payload["submitted_at"] = issue_ts
                    sub = CloudRequestSubmission(source="jira", payload=payload, dry_run=False)
                    _submit_digital_worker_job(sub)

        except Exception as exc:
            logger.warning("Background Jira sync exception: %s", exc)

    _thread_pool.submit(_sync_worker)


@app.get("/requests")
def list_cloud_requests(
    status: Optional[str] = Query(default=None),
    since: Optional[float] = Query(default=None),
):
    """List Digital Worker requests for the Human Approval Center.

    Optional ``?status=`` filter (e.g. ``awaiting_approval``, ``running``,
    ``completed``, ``failed``). Results are newest-first. This is the
    discovery endpoint the approval center polls — no job_id needed.
    """
    try:
        _trigger_background_jira_sync()
        with _job_store_lock:
            dw_count = sum(1 for j in _job_store.values() if j.get("kind") == "digital_worker")
            if dw_count == 0:
                _load_jobs_from_disk(50, digital_worker_only=True)

            # Sort all digital_worker jobs newest first
            sorted_jobs = sorted(
                _job_store.items(),
                key=lambda pair: pair[1].get("submitted_at") or 0,
                reverse=True
            )
            seen_tickets = set()
            items = []
            for job_id, job in sorted_jobs:
                if job.get("kind") != "digital_worker":
                    continue
                if since is not None:
                    job_time = job.get("submitted_at") or job.get("started_at") or 0
                    if job_time < since:
                        continue
                if status is not None and job.get("status") != status:
                    continue
                res = job.get("result") or {}
                approval = res.get("approval_request") or {}
                ticket = approval.get("external_id") or job.get("external_id")
                if ticket:
                    if ticket in seen_tickets:
                        continue
                    seen_tickets.add(ticket)
                items.append(_dw_request_summary(job_id, job))

            counts: Dict[str, int] = {}
            for item in items:
                k = str(item.get("status"))
                counts[k] = counts.get(k, 0) + 1

        return JSONResponse(status_code=200, content={
            "status": "ok",
            "count": len(items),
            "counts": counts,
            "requests": items,
        })
    except Exception as exc:
        logger.exception("list_cloud_requests error: %s", exc)
        return JSONResponse(status_code=200, content={
            "status": "ok",
            "count": 0,
            "counts": {},
            "requests": [],
        })


@app.get("/requests/{job_id}")
def get_cloud_request(job_id: str):
    """Full detail for one Digital Worker request (approval payload +
    terminal workflow result when complete)."""
    with _job_store_lock:
        job = _job_store.get(job_id)
        if job is None:
            recovered = _recover_job_from_disk(job_id)
            if recovered:
                _job_store[job_id] = recovered
                job = recovered
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

class RecommendPermissionSetsPayload(BaseModel):
    required_permissions: List[Dict[str, Any]]

@app.post("/api/permission-sets/recommend")
def recommend_permission_sets(payload: RecommendPermissionSetsPayload):
    try:
        from src.chandra.llm import get_llm
        import json
        import json_repair
        
        perms = _load_aws_permissions_from_disk()
        llm = get_llm()
        
        prompt = (
            "You are an AWS IAM expert. Given a list of required permissions and a list of existing permission sets, "
            "recommend the BEST existing permission set that covers all required permissions using wildcard matching. "
            "If no existing permission set is adequate, suggest creating a new one. "
            "Return a JSON object with: "
            "1. 'recommendation_type': 'existing' or 'new' "
            "2. 'permission_set_id': The ID of the existing set if 'existing', else null "
            "3. 'reason': Why this set is recommended, or why a new one is needed "
            "4. 'suggested_new_set': If 'new', provide a suggested name and actions array."
        )
        response = llm.invoke([
            ("system", prompt),
            ("user", json.dumps({"required_permissions": payload.required_permissions, "existing_sets": perms}))
        ])
        
        text = response.content if isinstance(response.content, str) else str(response.content)
        parsed = json_repair.loads(text)
        
        return JSONResponse(status_code=200, content={"status": "success", "recommendation": parsed})
    except Exception as exc:
        logger.exception("Failed to recommend permission sets: %s", exc)
        return JSONResponse(status_code=500, content={"status": "error", "exception": str(exc)})


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
        "iam:PutUserPolicy", "iam:GetUser", "iam:ListAttachedUserPolicies", "iam:PassRole"
    ],
    "lambda": [
        "lambda:CreateFunction", "lambda:UpdateFunctionCode", "lambda:UpdateFunctionConfiguration",
        "lambda:DeleteFunction", "lambda:GetFunction", "lambda:InvokeFunction",
        "lambda:CreateEventSourceMapping", "lambda:DeleteEventSourceMapping"
    ],
    "dynamodb": [
        "dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem",
        "dynamodb:Scan", "dynamodb:Query", "dynamodb:CreateTable"
    ],
    "cloudwatch": [
        "logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "cloudwatch:PutMetricData"
    ],
    "vpc": [
        "ec2:CreateVpc", "ec2:CreateSubnet", "ec2:CreateRouteTable", "ec2:CreateInternetGateway",
        "ec2:DescribeVpcs", "ec2:DescribeSubnets", "ec2:DeleteVpc", "ec2:DeleteSubnet"
    ],
    "rds": [
        "rds:CreateDBInstance", "rds:DeleteDBInstance", "rds:ModifyDBInstance", 
        "rds:DescribeDBInstances", "rds:CreateDBCluster", "rds:CreateDBSnapshot"
    ],
    "sqs": [
        "sqs:CreateQueue", "sqs:DeleteQueue", "sqs:SendMessage", "sqs:ReceiveMessage",
        "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:ListQueues"
    ],
    "sns": [
        "sns:CreateTopic", "sns:DeleteTopic", "sns:Publish", "sns:Subscribe",
        "sns:Unsubscribe", "sns:ListTopics", "sns:ListSubscriptions"
    ],
    "ecs": [
        "ecs:CreateCluster", "ecs:DeleteCluster", "ecs:RegisterTaskDefinition", 
        "ecs:RunTask", "ecs:StartTask", "ecs:StopTask", "ecs:DescribeClusters"
    ],
    "elb": [
        "elasticloadbalancing:CreateLoadBalancer", "elasticloadbalancing:DeleteLoadBalancer",
        "elasticloadbalancing:RegisterTargets", "elasticloadbalancing:DescribeLoadBalancers"
    ],
    "cloudfront": [
        "cloudfront:CreateDistribution", "cloudfront:UpdateDistribution",
        "cloudfront:DeleteDistribution", "cloudfront:GetDistribution", "cloudfront:CreateInvalidation"
    ],
    "elasticache": [
        "elasticache:CreateCacheCluster", "elasticache:DeleteCacheCluster",
        "elasticache:DescribeCacheClusters", "elasticache:CreateReplicationGroup"
    ],
    "apigateway": [
        "apigateway:POST", "apigateway:GET", "apigateway:PUT", "apigateway:DELETE", "apigateway:PATCH"
    ],
    "kms": [
        "kms:CreateKey", "kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey", 
        "kms:DescribeKey", "kms:ScheduleKeyDeletion"
    ],
    "secretsmanager": [
        "secretsmanager:CreateSecret", "secretsmanager:GetSecretValue", 
        "secretsmanager:PutSecretValue", "secretsmanager:DeleteSecret"
    ],
    "route53": [
        "route53:CreateHostedZone", "route53:ChangeResourceRecordSets",
        "route53:ListHostedZones", "route53:ListResourceRecordSets"
    ],
    "stepfunctions": [
        "states:CreateStateMachine", "states:UpdateStateMachine", "states:DeleteStateMachine",
        "states:StartExecution", "states:DescribeExecution"
    ],
    "athena": [
        "athena:StartQueryExecution", "athena:GetQueryExecution", 
        "athena:GetQueryResults", "athena:CreateWorkGroup"
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