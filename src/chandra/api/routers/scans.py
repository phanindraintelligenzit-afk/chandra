"""Detector scans, cloud metrics and action analysis.

Fourth extraction. These endpoints submit background work and their worker
functions mutate the job store, so unlike the jobs router they need the write
side of the shared runtime — which is exactly what ``api/runtime.py`` was
extracted to provide.

Each endpoint returns 202 with a poll URL; progress arrives either by polling
``/jobs/status/{job_id}`` or live on ``/ws/jobs/{job_id}``, since the job store
broadcasts its own transitions.
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from typing import Any

import boto3
from digitalworker_agents.analyzer_agent import AnalyzerAgent
from digitalworker_agents.observation_agent import DEFAULT_REGION, AwsObservabilityAgent
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from src.chandra.api import runtime
from src.chandra.api.deps import config_repo
from src.chandra.api.models import ActionInput
from tools.aws_cloud_tools.cost_explorer import AWSCostExplorerFetcher
from tools.aws_cloud_tools.metrics_fetcher import CloudWatchMetricsFetcher
from tools.aws_cloud_tools.tool_findings import run_predefined_kra_detectors

logger = logging.getLogger("fastapi_app")

router = APIRouter(tags=["scans"])


class KRAInput(BaseModel):
    code: str | None = Field(
        default=None, description="Optional KRA identifier (e.g. KRA-01). Auto-labelled if omitted."
    )
    name: str | None = Field(
        default=None,
        description=(
            "Optional short name/title for the KRA (e.g. 'Disaster Recovery Drills'). "
            "For custom KRAs this is the user-provided kraName."
        ),
    )
    description: str = Field(
        description=(
            "Free-form goal or objective. Can be an observability target (e.g. 'IAM drift "
            "monitoring') or any operational task (e.g. 'Deploy code from "
            "github.com/org/repo to EC2 in us-east-1')."
        )
    )


class PipelineRequest(BaseModel):
    region: str = Field(
        default=DEFAULT_REGION, description="AWS region to run the pipeline against"
    )
    kras: list[KRAInput] = Field(
        description="List of KRAs to evaluate during the observability run"
    )


@router.get("/getDetectorIssues")
def get_detector_issues() -> JSONResponse:
    """Submit detector scan as an async job. Poll /jobs/status/{job_id} for result."""
    job_id = str(uuid.uuid4())
    logger.info("GET /getDetectorIssues -> async job_id=%s", job_id)
    runtime.register_job(job_id, "Queued: detector scan")
    runtime.submit_with_context(_run_detector_task, job_id)
    return JSONResponse(
        status_code=202,
        content={
            "job_id": job_id,
            "status": "accepted",
            "message": f"Detector scan submitted. Poll /jobs/status/{job_id}",
            "poll_url": f"/jobs/status/{job_id}",
        },
    )


class PredefinedKraRequest(BaseModel):
    selected_kras: list[str] = Field(
        default_factory=list, description="List of KRA codes/names to run detectors for"
    )


@router.post("/getPredefinedKraIssues")
def get_predefined_kra_issues(request: PredefinedKraRequest) -> JSONResponse:
    """Submit detector scan as an async job for selected KRAs."""
    job_id = str(uuid.uuid4())
    logger.info(
        "POST /getPredefinedKraIssues -> async job_id=%s, kras=%s", job_id, request.selected_kras
    )
    runtime.register_job(job_id, f"Queued: detector scan for {request.selected_kras}")
    runtime.submit_with_context(_run_predefined_kra_task, job_id, request.selected_kras)
    return JSONResponse(
        status_code=202,
        content={
            "job_id": job_id,
            "status": "accepted",
            "message": f"Detector scan submitted. Poll /jobs/status/{job_id}",
            "poll_url": f"/jobs/status/{job_id}",
        },
    )


class CostMetricsRequest(BaseModel):
    days_lookback: int = Field(default=7, ge=1, le=365, description="Number of days to look back")
    granularity: str = Field(default="DAILY", description="Cost granularity: DAILY or MONTHLY")


@router.post("/getCostMetrics")
async def get_cost_metrics(request: CostMetricsRequest) -> JSONResponse:
    logger.info(
        "POST /getCostMetrics called with days_lookback=%d, granularity=%s",
        request.days_lookback,
        request.granularity,
    )
    try:
        fetcher = AWSCostExplorerFetcher()
        summary: dict[str, Any] = await fetcher.fetch_costs_summary(
            days_lookback=request.days_lookback
        )
        return JSONResponse(status_code=200, content={"status": "success", "output": summary})
    except Exception as exc:
        logger.exception("Cost metrics fetch failed: %s", exc)
        return JSONResponse(status_code=500, content={"status": "error", "exception": str(exc)})


class CloudWatchMetricsRequest(BaseModel):
    region: str = Field(
        default=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
        description="AWS region to fetch metrics from",
    )
    last_hours: int = Field(default=12, description="Hours to look back")
    period: int = Field(default=1200, description="Period in seconds")
    timezone_str: str = Field(
        default="Asia/Kolkata",
        description="Timezone for timestamps (e.g. 'Asia/Kolkata', 'US/Eastern')",
    )


@router.get("/aws/regions")
def get_aws_regions() -> JSONResponse:
    """Fetch all available AWS regions dynamically."""
    try:
        session = boto3.Session()
        regions = session.get_available_regions("cloudwatch")
        return JSONResponse(status_code=200, content={"regions": sorted(regions)})
    except Exception as exc:
        logger.exception("Failed to fetch regions: %s", exc)
        return JSONResponse(status_code=500, content={"status": "error", "message": str(exc)})


@router.post("/getCloudWatchMetrics")
def get_cloudwatch_metrics(request: CloudWatchMetricsRequest) -> JSONResponse:
    """Submit CloudWatch metrics fetch as an async job. Poll /jobs/status/{job_id} for result."""
    job_id = str(uuid.uuid4())
    logger.info("POST /getCloudWatchMetrics -> async job_id=%s region=%s", job_id, request.region)
    runtime.register_job(job_id, "Queued: CloudWatch metrics fetch")
    runtime.submit_with_context(_run_cloudwatch_task, job_id, request)
    return JSONResponse(
        status_code=202,
        content={
            "job_id": job_id,
            "status": "accepted",
            "message": f"CloudWatch fetch submitted. Poll /jobs/status/{job_id}",
            "poll_url": f"/jobs/status/{job_id}",
        },
    )


@router.post("/getAgentObservations")
def run_pipeline(request: PipelineRequest) -> JSONResponse:
    """Submit observability pipeline as an async job. Poll /jobs/status/{job_id} for result."""
    job_id = str(uuid.uuid4())
    logger.info(
        "POST /getAgentObservations -> async job_id=%s region=%s kras=%s",
        job_id,
        request.region,
        [k.code for k in request.kras],
    )
    runtime.register_job(job_id, "Queued: AWS observability pipeline")
    runtime.submit_with_context(_run_observations_task, job_id, request)
    return JSONResponse(
        status_code=202,
        content={
            "job_id": job_id,
            "status": "accepted",
            "message": f"Pipeline submitted. Poll /jobs/status/{job_id}",
            "poll_url": f"/jobs/status/{job_id}",
        },
    )


# ── Generic job status endpoint (shared by all async jobs) ────────────────────
# ── Background task functions ─────────────────────────────────────────────────


def _run_observations_task(job_id: str, request: PipelineRequest) -> None:
    """Background worker for /getAgentObservations."""
    import time

    start_time = time.time()
    runtime.thread_local.job_id = job_id
    try:
        with runtime.job_store_lock:
            runtime.job_store[job_id]["status"] = "running"
            runtime.job_store[job_id]["started_at"] = start_time
            runtime.job_store[job_id]["progress"] = 10
            runtime.job_store[job_id]["message"] = "Initializing AWS agent..."
            runtime.job_store[job_id]["thread_id"] = threading.get_ident()

        agent = AwsObservabilityAgent(region=request.region, kras=request.kras)

        with runtime.job_store_lock:
            runtime.job_store[job_id]["progress"] = 25
            runtime.job_store[job_id]["message"] = "Running 11 AWS tools in parallel..."

        response = agent.RunPipeline()
        elapsed = time.time() - start_time

        with runtime.job_store_lock:
            runtime.job_store[job_id]["status"] = "completed"
            runtime.job_store[job_id]["progress"] = 100
            runtime.job_store[job_id]["result"] = response.model_dump()
            runtime.job_store[job_id]["completed_at"] = time.time()
            runtime.job_store[job_id]["message"] = f"Completed in {elapsed:.1f}s"

        logger.info("OBSERVATIONS JOB [%s] completed in %.1fs", job_id, elapsed)

    except (InterruptedError, SystemExit):
        logger.info("OBSERVATIONS JOB [%s] was stopped by the user", job_id)
        with runtime.job_store_lock:
            if runtime.job_store[job_id].get("status") != "stopped":
                runtime.job_store[job_id]["status"] = "stopped"
                runtime.job_store[job_id]["completed_at"] = time.time()
    except Exception as exc:
        logger.exception("OBSERVATIONS JOB [%s] failed", job_id)
        with runtime.job_store_lock:
            runtime.job_store[job_id]["status"] = "failed"
            runtime.job_store[job_id]["error"] = str(exc)
            runtime.job_store[job_id]["completed_at"] = time.time()
            runtime.job_store[job_id]["message"] = f"Failed: {str(exc)[:200]}"
    finally:
        runtime.thread_local.job_id = None


def _run_detector_task(job_id: str) -> None:
    """Background worker for /getDetectorIssues."""
    import time

    start_time = time.time()
    runtime.thread_local.job_id = job_id
    try:
        with runtime.job_store_lock:
            runtime.job_store[job_id]["status"] = "running"
            runtime.job_store[job_id]["started_at"] = start_time
            runtime.job_store[job_id]["progress"] = 10
            runtime.job_store[job_id]["message"] = "Running compliance/security detectors..."
            runtime.job_store[job_id]["thread_id"] = threading.get_ident()

        # Use shared bg loop — avoids competing event loops crashing uvicorn
        selected = [
            k["name"].lower() for k in config_repo().list_custom_kras() if k.get("selected")
        ]
        valid_modules = [
            m
            for m in ["compliance", "security", "reliability", "performance", "cost"]
            if m in selected
        ]

        if valid_modules:
            findings = runtime.run_async(run_predefined_kra_detectors(valid_modules))
        else:
            findings = {}

        output: dict[str, Any] | list[Any]
        if isinstance(findings, dict):
            total_issues = sum(len(g) for g in findings.values())
            output = findings
        else:
            total_issues = len(findings)
            output = [f.model_dump() if hasattr(f, "model_dump") else f for f in findings]

        elapsed = time.time() - start_time
        with runtime.job_store_lock:
            runtime.job_store[job_id]["status"] = "completed"
            runtime.job_store[job_id]["progress"] = 100
            runtime.job_store[job_id]["result"] = {"status": "success", "output": output}
            runtime.job_store[job_id]["completed_at"] = time.time()
            runtime.job_store[job_id]["message"] = f"Found {total_issues} issues in {elapsed:.1f}s"

        logger.info("DETECTOR JOB [%s] found %d issues in %.1fs", job_id, total_issues, elapsed)

    except (InterruptedError, SystemExit):
        logger.info("DETECTOR JOB [%s] was stopped by the user", job_id)
        with runtime.job_store_lock:
            if runtime.job_store[job_id].get("status") != "stopped":
                runtime.job_store[job_id]["status"] = "stopped"
                runtime.job_store[job_id]["completed_at"] = time.time()
    except Exception as exc:
        logger.exception("DETECTOR JOB [%s] failed", job_id)
        with runtime.job_store_lock:
            runtime.job_store[job_id]["status"] = "failed"
            runtime.job_store[job_id]["error"] = str(exc)
            runtime.job_store[job_id]["completed_at"] = time.time()
            runtime.job_store[job_id]["message"] = f"Failed: {str(exc)[:200]}"
    finally:
        runtime.thread_local.job_id = None


def _run_predefined_kra_task(job_id: str, selected_kras: list[str]) -> None:
    """Background worker for /getPredefinedKraIssues."""
    import time

    start_time = time.time()
    runtime.thread_local.job_id = job_id
    try:
        with runtime.job_store_lock:
            runtime.job_store[job_id]["status"] = "running"
            runtime.job_store[job_id]["started_at"] = start_time
            runtime.job_store[job_id]["progress"] = 10
            runtime.job_store[job_id]["message"] = f"Running detectors for {selected_kras}..."
            runtime.job_store[job_id]["thread_id"] = threading.get_ident()

        findings = runtime.run_async(run_predefined_kra_detectors(selected_kras))
        output: dict[str, Any] | list[Any]
        if isinstance(findings, dict):
            total_issues = sum(len(g) for g in findings.values())
            output = findings
        else:
            total_issues = len(findings)
            output = [f.model_dump() if hasattr(f, "model_dump") else f for f in findings]

        elapsed = time.time() - start_time
        with runtime.job_store_lock:
            runtime.job_store[job_id]["status"] = "completed"
            runtime.job_store[job_id]["progress"] = 100
            runtime.job_store[job_id]["result"] = {"status": "success", "output": output}
            runtime.job_store[job_id]["completed_at"] = time.time()
            runtime.job_store[job_id]["message"] = f"Found {total_issues} issues in {elapsed:.1f}s"

        logger.info(
            "PREDEFINED KRA JOB [%s] found %d issues in %.1fs", job_id, total_issues, elapsed
        )

    except (InterruptedError, SystemExit):
        logger.info("PREDEFINED KRA JOB [%s] was stopped by the user", job_id)
        with runtime.job_store_lock:
            if runtime.job_store[job_id].get("status") != "stopped":
                runtime.job_store[job_id]["status"] = "stopped"
                runtime.job_store[job_id]["completed_at"] = time.time()
    except Exception as exc:
        logger.exception("PREDEFINED KRA JOB [%s] failed", job_id)
        with runtime.job_store_lock:
            runtime.job_store[job_id]["status"] = "failed"
            runtime.job_store[job_id]["error"] = str(exc)
            runtime.job_store[job_id]["completed_at"] = time.time()
            runtime.job_store[job_id]["message"] = f"Failed: {str(exc)[:200]}"
    finally:
        runtime.thread_local.job_id = None


def _run_cloudwatch_task(job_id: str, request: CloudWatchMetricsRequest) -> None:
    """Background worker for /getCloudWatchMetrics."""
    import time

    start_time = time.time()
    runtime.thread_local.job_id = job_id
    try:
        with runtime.job_store_lock:
            runtime.job_store[job_id]["status"] = "running"
            runtime.job_store[job_id]["started_at"] = start_time
            runtime.job_store[job_id]["progress"] = 10
            runtime.job_store[job_id]["message"] = "Discovering CloudWatch metrics..."

        fetcher = CloudWatchMetricsFetcher()
        # Use shared bg loop — avoids competing event loops crashing uvicorn
        summary = runtime.run_async(
            fetcher.fetch_all_metrics(
                region=request.region,
                last_hours=request.last_hours,
                period=request.period,
                timezone_str=request.timezone_str,
            )
        )
        elapsed = time.time() - start_time
        total = summary.get("metadata", {}).get("total_metrics_found", 0)

        with runtime.job_store_lock:
            runtime.job_store[job_id]["status"] = "completed"
            runtime.job_store[job_id]["progress"] = 100
            runtime.job_store[job_id]["result"] = {"status": "success", "output": summary}
            runtime.job_store[job_id]["completed_at"] = time.time()
            runtime.job_store[job_id]["message"] = f"Fetched {total} metrics in {elapsed:.1f}s"

        logger.info("CLOUDWATCH JOB [%s] found %d metrics in %.1fs", job_id, total, elapsed)

    except Exception as exc:
        logger.exception("CLOUDWATCH JOB [%s] failed", job_id)
        with runtime.job_store_lock:
            runtime.job_store[job_id]["status"] = "failed"
            runtime.job_store[job_id]["error"] = str(exc)
            runtime.job_store[job_id]["completed_at"] = time.time()
            runtime.job_store[job_id]["message"] = f"Failed: {str(exc)[:200]}"
    finally:
        runtime.thread_local.job_id = None


class AnalyzerRequest(BaseModel):
    actions: list[ActionInput] = Field(description="List of remediation actions to analyze")
    projectKey: str = Field(default="DEV", description="Jira project key for ticket creation")


@router.post("/analyzeActions")
def analyze_actions(request: AnalyzerRequest) -> JSONResponse:
    """Submit action analysis as an async job. Poll /jobs/status/{job_id} for result."""
    job_id = str(uuid.uuid4())
    logger.info(
        "POST /analyzeActions -> async job_id=%s actions=%d projectKey=%s",
        job_id,
        len(request.actions),
        request.projectKey,
    )
    runtime.register_job(job_id, f"Queued: analyzing {len(request.actions)} actions")
    runtime.submit_with_context(_run_analyzer_task, job_id, request)
    return JSONResponse(
        status_code=202,
        content={
            "job_id": job_id,
            "status": "accepted",
            "message": f"Analysis submitted. Poll /jobs/status/{job_id}",
            "poll_url": f"/jobs/status/{job_id}",
        },
    )


def _run_analyzer_task(job_id: str, request: AnalyzerRequest) -> None:
    """Background worker for /analyzeActions."""
    import time

    start_time = time.time()
    runtime.thread_local.job_id = job_id
    try:
        with runtime.job_store_lock:
            runtime.job_store[job_id]["status"] = "running"
            runtime.job_store[job_id]["started_at"] = start_time
            runtime.job_store[job_id]["progress"] = 10
            runtime.job_store[job_id]["message"] = "Analyzing actions with LLM..."

        agent = AnalyzerAgent()

        with runtime.job_store_lock:
            runtime.job_store[job_id]["progress"] = 40
            runtime.job_store[job_id]["message"] = "Creating Jira tickets..."

        response = agent.RunPipeline(request.model_dump())
        elapsed = time.time() - start_time

        with runtime.job_store_lock:
            runtime.job_store[job_id]["status"] = "completed"
            runtime.job_store[job_id]["progress"] = 100
            runtime.job_store[job_id]["result"] = response.model_dump()
            runtime.job_store[job_id]["completed_at"] = time.time()
            runtime.job_store[job_id]["message"] = f"Completed in {elapsed:.1f}s"

        logger.info("ANALYZER JOB [%s] completed in %.1fs", job_id, elapsed)

    except Exception as exc:
        logger.exception("ANALYZER JOB [%s] failed", job_id)
        with runtime.job_store_lock:
            runtime.job_store[job_id]["status"] = "failed"
            runtime.job_store[job_id]["error"] = str(exc)
            runtime.job_store[job_id]["completed_at"] = time.time()
            runtime.job_store[job_id]["message"] = f"Failed: {str(exc)[:200]}"
    finally:
        runtime.thread_local.job_id = None
