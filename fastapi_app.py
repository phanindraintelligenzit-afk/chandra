"""
FastAPI application for the AWS Observability Agent.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
import uvicorn
from observation_agent import (
    AwsObservabilityAgent,
    PipelineResponse,
    DEFAULT_REGION,
)
from analyzer_agent import AnalyzerAgent, AnalyzerPipelineResponse, ActionResult
from tools.aws_cloud_tools.cost_explorer import AWSCostExplorerFetcher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("fastapi_app")

app = FastAPI(
    title="AWS Observability Agent API",
    description="Runs the KRA-aligned AWS observability pipeline and returns a structured report.",
    version="1.0.0",
)


class KRAInput(BaseModel):
    code: str = Field(description="KRA identifier, e.g. KRA-01")
    description: str = Field(description="What this KRA measures / targets")


class PipelineRequest(BaseModel):
    region: str = Field(default=DEFAULT_REGION, description="AWS region to run the pipeline against")
    kras: List[KRAInput] = Field(description="List of KRAs to evaluate during the observability run")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/getCostMetrics")
async def get_cost_metrics(
    days_lookback: int = Query(default=1, ge=1, le=365, description="Number of days to look back"),
    granularity: str = Query(default="DAILY", description="Cost granularity: DAILY or MONTHLY"),
) -> JSONResponse:
    logger.info("GET /getCostMetrics called with days_lookback=%d, granularity=%s", days_lookback, granularity)
    try:
        fetcher = AWSCostExplorerFetcher()
        summary: Dict[str, Any] = await fetcher.fetch_costs_summary(days_lookback=days_lookback)
        return JSONResponse(status_code=200, content={"status": "success", "output": summary})
    except Exception as exc:
        logger.exception("Cost metrics fetch failed: %s", exc)
        return JSONResponse(status_code=500, content={"status": "error", "exception": str(exc)})


@app.post("/getAgentObservations", response_model=PipelineResponse)
def run_pipeline(request: PipelineRequest):
    logger.info(
        "POST /getAgentObservations called with region=%s, kras=%s",
        request.region,
        [k.code for k in request.kras],
    )
    try:
        agent = AwsObservabilityAgent(region=request.region, kras=request.kras)
        response = agent.RunPipeline()
    except Exception as exc:
        logger.exception("Agent initialisation failed: %s", exc)
        response = PipelineResponse(
            statusCode=500,
            status="error",
            exception=str(exc),
            output=None,
        )

    return JSONResponse(status_code=response.statusCode, content=response.model_dump())

class ActionInput(BaseModel):
    actionName: str = Field(description="Short name of the action")
    actionDescription: str = Field(description="Detailed description of what needs to be done")
    service: str = Field(description="AWS service this action applies to")


class AnalyzerRequest(BaseModel):
    actions: List[ActionInput] = Field(description="List of remediation actions to analyze")
    projectKey: str = Field(default="DEV", description="Jira project key for ticket creation")


@app.post("/analyzeActions", response_model=AnalyzerPipelineResponse)
def analyze_actions(request: AnalyzerRequest):
    logger.info(
        "POST /analyzeActions called with %d actions, projectKey=%s",
        len(request.actions),
        request.projectKey,
    )
    try:
        agent = AnalyzerAgent()
        response = agent.RunPipeline(request.model_dump())
    except Exception as exc:
        logger.exception("AnalyzerAgent initialisation failed: %s", exc)
        response = AnalyzerPipelineResponse(
            statusCode=500,
            status="error",
            exception=str(exc),
            output=None,
        )

    return JSONResponse(status_code=response.statusCode, content=response.model_dump())


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=6001)