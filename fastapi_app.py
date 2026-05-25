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
from generator_agent import GeneratorAgent, GeneratorPipelineResponse
from executor_agent import ExecutorAgent, ExecutorPipelineResponse
from tools.aws_cloud_tools.cost_explorer import AWSCostExplorerFetcher
from copilot_agents.graph import build_graph, chat as copilot_chat

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

# Built once so MemorySaver persists across requests (keyed by sessionId / thread_id)
_copilot_agent = build_graph()
_generator_agent = GeneratorAgent()
_executor_agent = ExecutorAgent()


class KRAInput(BaseModel):
    code: Optional[str] = Field(default=None, description="Optional KRA identifier (e.g. KRA-01). Auto-labelled if omitted.")
    description: str = Field(description="Free-form goal or objective. Can be an observability target (e.g. 'IAM drift monitoring') or any operational task (e.g. 'Deploy code from github.com/org/repo to EC2 in us-east-1').")


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
    """
    Example request:
    {
        "region": "us-east-1",
        "kras": [
            {
                "code": "KRA-01",
                "description": "Reduce unexpected Bedrock spend by 60% through auto-remediation of untagged inference calls"
            },
            {
                "code": "KRA-02",
                "description": "Zero high-severity misconfigurations open longer than 24 hours"
            }
        ]
    }
    """
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
    kraCode: Optional[str] = Field(default=None, description="KRA identifier (e.g. KRA-01)")
    priorityLevel: Optional[str] = Field(default=None, description="Priority level (e.g. P1)")
    steps: Optional[List[str]] = Field(default=None, description="Implementation steps to add as a Jira comment")


class AnalyzerRequest(BaseModel):
    actions: List[ActionInput] = Field(description="List of remediation actions to analyze")
    projectKey: str = Field(default="DEV", description="Jira project key for ticket creation")


@app.post("/analyzeActions", response_model=AnalyzerPipelineResponse)
def analyze_actions(request: AnalyzerRequest):
    """
    Example request:
    {
        "projectKey": "DEV",
        "actions": [
            {
                "actionName": "Automate Bedrock inference disablement for untagged usage",
                "actionDescription": "Create a Lambda function triggered by CloudTrail that blocks Bedrock inference requests from untagged roles — auto-remediates unexpected spend to meet KRA-01's 60% auto-remediate target.",
                "service": "Bedrock",
                "kraCode": "KRA-01",
                "priorityLevel": "P1",
                "steps": [
                    "Create an IAM role with CloudWatch Events and Bedrock:InvokeModel permissions",
                    "Write Lambda function that checks userIdentity.principalId against allowed tag values",
                    "If principal has no Environment=prod tag, deny the request",
                    "Deploy Lambda and link to CloudWatch Event rule filtering on InvokeModel events",
                    "Test by simulating an untagged Bedrock call"
                ]
            }
        ]
    }
    """
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


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=6001)