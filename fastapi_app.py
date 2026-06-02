"""
FastAPI application for the AWS Observability Agent.
"""

from __future__ import annotations

import logging
import threading
import uuid
from typing import Any, Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field
import os
from dotenv import load_dotenv

load_dotenv(override=True)
import uvicorn
from digitalworker_agents.observation_agent import (
    AwsObservabilityAgent,
    PipelineResponse,
    DEFAULT_REGION,
)
from digitalworker_agents.analyzer_agent import AnalyzerAgent, AnalyzerPipelineResponse, ActionResult
from digitalworker_agents.generator_agent import GeneratorAgent, GeneratorPipelineResponse
from digitalworker_agents.executor_agent import ExecutorAgent, ExecutorPipelineResponse
from digitalworker_agents.orchestrator_agent import OrchestratorAgent, OrchestratorResponse
from tools.aws_cloud_tools.cost_explorer import AWSCostExplorerFetcher
from tools.aws_cloud_tools.metrics_fetcher import CloudWatchMetricsFetcher
from tools.aws_cloud_tools.tool_findings import run_all_detectors
from copilot_agents.graph import build_graph, chat as copilot_chat

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
_job_store_lock = threading.Lock()
_thread_pool = ThreadPoolExecutor(max_workers=8)

_thread_local = threading.local()

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

# Job models
class JobStatusResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")  # Ignore extra fields from job dict

    job_id: str
    status: str  # "pending", "running", "completed", "failed"
    progress: int = 0  # 0-100
    message: str = ""
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    started_at: Optional[float] = None
    completed_at: Optional[float] = None

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

app = FastAPI(
    title="AWS Observability Agent API",
    description="Runs the KRA-aligned AWS observability pipeline and returns a structured report.",
    version="1.0.0",
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

try:
    _generator_agent = GeneratorAgent()
    logger.info("Generator agent initialized successfully")
except Exception as _e:
    logger.error("Failed to initialize generator agent: %s", _e)
    _generator_agent = None

try:
    _executor_agent = ExecutorAgent()
    logger.info("Executor agent initialized successfully")
except Exception as _e:
    logger.error("Failed to initialize executor agent: %s", _e)
    _executor_agent = None

try:
    _orchestrator_agent = OrchestratorAgent()
    logger.info("Orchestrator agent initialized successfully")
except Exception as _e:
    logger.error("Failed to initialize orchestrator agent: %s", _e)
    _orchestrator_agent = None


class KRAInput(BaseModel):
    code: Optional[str] = Field(default=None, description="Optional KRA identifier (e.g. KRA-01). Auto-labelled if omitted.")
    description: str = Field(description="Free-form goal or objective. Can be an observability target (e.g. 'IAM drift monitoring') or any operational task (e.g. 'Deploy code from github.com/org/repo to EC2 in us-east-1').")


class PipelineRequest(BaseModel):
    region: str = Field(default=DEFAULT_REGION, description="AWS region to run the pipeline against")
    kras: List[KRAInput] = Field(description="List of KRAs to evaluate during the observability run")


@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/logs")
def get_logs(limit: int = Query(500, ge=1, le=2000), offset: int = Query(0, ge=0)):
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
    region: str = Field(default="us-east-1", description="AWS region to fetch metrics from")
    last_hours: int = Field(default=12, description="Hours to look back")
    period: int = Field(default=1200, description="Period in seconds")
    timezone_str: str = Field(default="Asia/Kolkata", description="Timezone for timestamps (e.g. 'Asia/Kolkata', 'US/Eastern')")

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
    import asyncio, time
    start_time = time.time()
    _thread_local.job_id = job_id
    try:
        with _job_store_lock:
            _job_store[job_id]["status"] = "running"
            _job_store[job_id]["started_at"] = start_time
            _job_store[job_id]["progress"] = 10
            _job_store[job_id]["message"] = "Running compliance/security detectors..."

        findings = asyncio.run(run_all_detectors())
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

    except Exception as exc:
        logger.exception("DETECTOR JOB [%s] failed", job_id)
        with _job_store_lock:
            _job_store[job_id]["status"] = "failed"
            _job_store[job_id]["error"] = str(exc)
            _job_store[job_id]["completed_at"] = time.time()
            _job_store[job_id]["message"] = f"Failed: {str(exc)[:200]}"
    finally:
        _thread_local.job_id = None


def _run_cloudwatch_task(job_id: str, request: CloudWatchMetricsRequest):
    """Background worker for /getCloudWatchMetrics."""
    import asyncio, time
    start_time = time.time()
    _thread_local.job_id = job_id
    try:
        with _job_store_lock:
            _job_store[job_id]["status"] = "running"
            _job_store[job_id]["started_at"] = start_time
            _job_store[job_id]["progress"] = 10
            _job_store[job_id]["message"] = "Discovering CloudWatch metrics..."

        fetcher = CloudWatchMetricsFetcher()
        summary = asyncio.run(fetcher.fetch_all_metrics(
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
    service: str = Field(description="AWS service this action applies to")
    kraCode: Optional[str] = Field(default=None, description="KRA identifier (e.g. KRA-01)")
    priorityLevel: Optional[str] = Field(default=None, description="Priority level (e.g. P1)")
    steps: Optional[List[str]] = Field(default=None, description="Implementation steps to add as a Jira comment")


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


class GeneratorActionInput(BaseModel):
    actionName: str = Field(description="Short name or title of the action")
    actionDescription: str = Field(description="Detailed description of what needs to be done and why")
    steps: Optional[List[str]] = Field(default=None, description="Optional ordered implementation steps")


class GenerateRequest(BaseModel):
    action: GeneratorActionInput = Field(description="Action to generate code for")
    thread_id: Optional[str] = Field(
        default=None,
        description="Thread ID from a previous needs_clarification response. Omit on first call.",
    )
    answers: Optional[List[str]] = Field(
        default=None,
        description="Answers to the clarification questions, in the same order they were returned.",
    )
    sandbox_path: Optional[str] = Field(
        default=None,
        description="Path to an existing sandbox folder. If provided and contains files, the agent updates them instead of generating from scratch.",
    )
    feedbackSummary: Optional[str] = Field(
        default=None,
        description="Optional free-text feedback or change instructions to apply when updating existing files.",
    )


@app.post("/generateCode", response_model=GeneratorPipelineResponse)
def generate_code(request: GenerateRequest):
    """
    Example request:
    {
        "action": {
            "actionName": "Automate Bedrock inference disablement for untagged usage",
            "actionDescription": "Create a Lambda function triggered by CloudTrail that blocks Bedrock inference requests from untagged roles — auto-remediates unexpected spend to meet KRA-01's 60% auto-remediate target.",
            "steps": [
                "Create an IAM role with CloudWatch Events and Bedrock:InvokeModel permissions",
                "Write Lambda function that checks userIdentity.principalId against allowed tag values",
                "If principal has no Environment=prod tag, deny the request",
                "Deploy Lambda and link to CloudWatch Event rule filtering on InvokeModel events",
                "Test by simulating an untagged Bedrock call"
            ]
        },
        "thread_id": null,
        "answers": null
    }
    """
    logger.info(
        "POST /generateCode | action=%s | thread_id=%s | resuming=%s",
        request.action.actionName,
        request.thread_id,
        request.answers is not None,
    )
    try:
        response = _generator_agent.RunPipeline(
            action=request.action.model_dump(),
            thread_id=request.thread_id,
            answers=request.answers,
            sandbox_path=request.sandbox_path,
            feedback_summary=request.feedbackSummary,
        )
    except Exception as exc:
        logger.exception("GeneratorAgent failed: %s", exc)
        response = GeneratorPipelineResponse(
            statusCode=500,
            status="error",
            exception=str(exc),
        )

    return JSONResponse(status_code=response.statusCode, content=response.model_dump())


class ExecuteActionInput(BaseModel):
    actionName: str = Field(description="Short name or title of the action")
    actionDescription: str = Field(description="Detailed description of what needs to be done and why")
    priorityLevel: Optional[str] = Field(default=None, description="Priority level (e.g. P1)")
    executeFolder: str = Field(description="Path to the folder containing generated scripts / tf files")
    executableSteps: Optional[List[Dict[str, str]]] = Field(
        default=None,
        description="Ordered steps from /generateCode, each with 'description' (human-readable) and 'command' (shell command). If None, will do LLM-based planning from folder contents."
    )


@app.post("/executeCode", response_model=ExecutorPipelineResponse)
def execute_code(request: ExecuteActionInput):
    """
    Example request (with executableSteps from /generateCode):
    {
        "actionName": "Automate Bedrock inference disablement for untagged usage",
        "actionDescription": "Create a Lambda function triggered by CloudTrail that blocks Bedrock inference requests from untagged roles — auto-remediates unexpected spend to meet KRA-01's 60% auto-remediate target.",
        "priorityLevel": "P1",
        "executeFolder": "sandbox_9876",
        "executableSteps": [
            {"description": "Install Python dependencies", "command": "pip install -r lambda/requirements.txt -t lambda/package"},
            {"description": "Initialize Terraform", "command": "terraform -chdir=infrastructure init"},
            {"description": "Validate Terraform configuration", "command": "terraform -chdir=infrastructure validate"},
            {"description": "Plan Terraform deployment", "command": "terraform -chdir=infrastructure plan"},
            {"description": "Apply Terraform configuration", "command": "terraform -chdir=infrastructure apply -auto-approve"}
        ]
    }
    """
    logger.info(
        "POST /executeCode | action=%s | folder=%s | steps=%d",
        request.actionName,
        request.executeFolder,
        len(request.executableSteps) if request.executableSteps else 0,
    )
    try:
        response = _executor_agent.RunPipeline(
            action={
                "actionName": request.actionName,
                "actionDescription": request.actionDescription,
            },
            executeFolder=request.executeFolder,
            executableSteps=request.executableSteps,
        )
    except Exception as exc:
        logger.exception("ExecutorAgent failed: %s", exc)
        response = ExecutorPipelineResponse(
            statusCode=500,
            status="error",
            exception=str(exc),
        )

    return JSONResponse(status_code=response.statusCode, content=response.model_dump())


class OrchestrateRequest(BaseModel):
    action: GeneratorActionInput = Field(description="Action to generate and execute")
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
    generator_thread_id: Optional[str] = Field(
        default=None,
        description="Generator thread ID for resuming HITL.",
    )
    command_timeout: int = Field(
        default=300,
        description="Per-command timeout in seconds (default: 300 = 5 minutes).",
    )
    jira_issue_key: Optional[str] = Field(
        default=None,
        description="Jira issue key to post final summary comment to after orchestration completes.",
    )
    max_iterations: int = Field(
        default=5,
        description="Maximum number of generate-execute iterations (default: 5).",
    )


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
        "POST /orchestrate submitted | job_id=%s | action=%s | jira_issue_key=%s",
        job_id,
        request.action.actionName,
        request.jira_issue_key or "None",
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

@app.get("/orchestrate/status/{job_id}", response_model=JobStatusResponse)
def get_orchestrate_status(job_id: str):
    """Poll the status of a submitted orchestration job."""
    with _job_store_lock:
        if job_id not in _job_store:
            return JobStatusResponse(
                job_id=job_id,
                status="not_found",
                message="Job ID not found",
                error="No job with this ID exists"
            )
        job = _job_store[job_id]
    
    return JobStatusResponse(job_id=job_id, **job)

def _run_orchestration_task(job_id: str, request: OrchestrateRequest):
    """Background worker to run orchestration without blocking the API."""
    import time
    start_time = time.time()
    _thread_local.job_id = job_id
    
    try:
        with _job_store_lock:
            _job_store[job_id]["status"] = "running"
            _job_store[job_id]["message"] = f"Starting orchestration for {request.action.actionName}"
            _job_store[job_id]["started_at"] = start_time
            _job_store[job_id]["progress"] = 5
        
        logger.info("ORCHESTRATION TASK [%s] started", job_id)
        
        # Run the actual orchestration
        orchestrator = OrchestratorAgent(max_iterations=request.max_iterations)
        response = orchestrator.RunPipeline(
            action=request.action.model_dump(),
            sandbox_path=request.sandbox_path,
            reference_folder=request.reference_folder,
            thread_id=request.thread_id,
            answers=request.answers,
            generator_thread_id=request.generator_thread_id,
            command_timeout=request.command_timeout,
            jira_issue_key=request.jira_issue_key,
        )
        
        # Update job with result
        is_success = response.statusCode == 200
        
        with _job_store_lock:
            _job_store[job_id]["status"] = "completed"
            _job_store[job_id]["progress"] = 100
            _job_store[job_id]["result"] = response.model_dump()
            _job_store[job_id]["completed_at"] = time.time()
            _job_store[job_id]["message"] = (
                f"Completed successfully in {_job_store[job_id]['completed_at'] - start_time:.1f}s"
                if is_success else response.summary or "Orchestration completed with errors"
            )
        
        logger.info(
            "ORCHESTRATION TASK [%s] completed | statusCode=%d | duration=%.1fs",
            job_id,
            response.statusCode,
            _job_store[job_id]["completed_at"] - start_time
        )
        
    except Exception as exc:
        logger.exception("ORCHESTRATION TASK [%s] failed with exception", job_id)
        with _job_store_lock:
            _job_store[job_id]["status"] = "failed"
            _job_store[job_id]["error"] = str(exc)
            _job_store[job_id]["completed_at"] = time.time()
            _job_store[job_id]["message"] = f"Failed: {str(exc)[:200]}"
    finally:
        _thread_local.job_id = None


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=6001)