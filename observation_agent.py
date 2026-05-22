"""
AWS Observability Agent - KRA-Aligned Version
============================================
"""

from __future__ import annotations

import json
import logging
import os
from typing import Annotated, Any, Dict, List, Optional, TypedDict

from pydantic import BaseModel, Field
from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_aws import ChatBedrockConverse
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from tools.langchain_tools import DEFAULT_REGION, TOOLS_LIST, default_tool_args

load_dotenv(override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("AwsObservabilityAgent")

ORIGINAL_KRAS = """
**KRA-01** Cloud cost anomaly detection & remediation
**KRA-02** IAM drift & security posture monitoring
**KRA-03** Incident triage & first-response
**KRA-04** Compliance evidence collection
**KRA-05** Infra documentation & runbook authoring
"""

class CostEntry(BaseModel):
    service: str = Field(description="AWS service name")
    daily_avg_usd: float = Field(description="Average daily cost in USD")
    change_24h_pct: Optional[float] = Field(default=None, description="24h cost change percentage")
    note: Optional[str] = Field(default=None, description="Anomaly flag, savings opportunity, etc.")


class ActionItem(BaseModel):
    actionName: str = Field(description="Short name or title of the action (e.g. 'Revoke S3 Public Access')")
    actionDescription: str = Field(description="Detailed description of what needs to be done and why")
    service: str = Field(description="AWS service this action applies to (e.g. 'S3', 'RDS', 'Bedrock')")


class KRAStatus(BaseModel):
    kra_code: str = Field(description="e.g. KRA-01")
    status: str = Field(description="Green | Yellow | Red")
    achievement: str = Field(description="Current performance against target")
    note: Optional[str] = Field(default=None)


class ObservabilityReport(BaseModel):
    """Structured report aligned with the AWS Observability / Cloud SRE role."""
    health: str = Field(description="Overall health status: Healthy | Degraded | Critical")
    kra_status: List[KRAStatus] = Field(description="Status against each of the 5 defined KRAs")
    issues: List[str] = Field(description="Critical issues / anomalies (P1/P2 level). Empty if none.")
    observations: List[str] = Field(description="Key findings from metrics, CloudTrail, Config, Security Hub, etc.")
    cost_snapshot: List[CostEntry] = Field(description="Top cost drivers with anomaly detection")
    security_posture: List[str] = Field(description="IAM drift, Security Hub findings, misconfigurations")
    compliance_summary: str = Field(description="Compliance evidence readiness summary")
    actions: List[ActionItem] = Field(description="Prioritised actionable recommendations (most urgent first)")


class AgentState(TypedDict):
    messages: Annotated[list, add_messages]
    raw_results: Dict[str, Any]
    final_summary: str
    region: str


class ToolExecutionError(Exception):
    """Raised when one or more AWS tools fail during execution."""


class SummaryGenerationError(Exception):
    """Raised when the LLM fails to produce a structured report."""


class PipelineResponse(BaseModel):
    statusCode: int = Field(description="HTTP-style status code")
    status: str = Field(description="'success' or 'error'")
    exception: Optional[str] = Field(default=None, description="Exception message if an error occurred")
    output: Optional[ObservabilityReport] = Field(default=None, description="Report on success, None on error")


class AwsObservabilityAgent:

    def __init__(self, region: str = DEFAULT_REGION, kras: Optional[List[Any]] = None):
        self.Region = region
        self.Kras = self._build_kras_str(kras)
        logger.info("Initialising AwsObservabilityAgent for region=%s", region)
        try:
            self.Llm = ChatBedrockConverse(model_id=os.getenv("MODEL_NAME"))
            self.Graph = self.BuildGraph()
            logger.info("Agent initialised successfully")
        except Exception as exc:
            logger.exception("Failed to initialise agent: %s", exc)
            raise

    @staticmethod
    def _build_kras_str(kras: Optional[List[Any]]) -> str:
        if not kras:
            return ORIGINAL_KRAS
        lines = [f"**{k.code}** {k.description}" for k in kras]
        return "\n".join(lines)

    def TriggerToolsNode(self, state: AgentState) -> dict:
        region = state.get("region", self.Region)
        logger.info("Triggering %d tools for region=%s", len(TOOLS_LIST), region)
        try:
            tool_calls = [
                {
                    "id": f"call_{t.name}",
                    "name": t.name,
                    "args": default_tool_args(t.name, region),
                    "type": "tool_call",
                }
                for t in TOOLS_LIST
            ]
            logger.debug("Tool calls prepared: %s", [tc["name"] for tc in tool_calls])
            return {"messages": [AIMessage(content="", tool_calls=tool_calls)]}
        except Exception as exc:
            logger.exception("Error building tool calls: %s", exc)
            raise ToolExecutionError(f"Failed to build tool calls: {exc}") from exc

    def NormalizeNode(self, state: AgentState) -> dict:
        logger.info("Normalising tool results")
        raw_results: Dict[str, Any] = {}
        failed_tools: List[str] = []

        for msg in state["messages"]:
            if not isinstance(msg, ToolMessage):
                continue
            tool_name = msg.name or "unknown_tool"
            try:
                content = json.loads(msg.content) if isinstance(msg.content, str) else msg.content
                raw_results[tool_name] = content
                logger.debug("Normalised result for tool=%s", tool_name)
            except (json.JSONDecodeError, TypeError) as exc:
                logger.warning("Could not parse result for tool=%s: %s", tool_name, exc)
                raw_results[tool_name] = {"raw": str(msg.content)}
                failed_tools.append(tool_name)

        if failed_tools:
            logger.warning("Parse failures for tools: %s", failed_tools)

        logger.info("Normalisation complete. Tools collected=%d", len(raw_results))
        return {"raw_results": raw_results}

    def SummaryNode(self, state: AgentState) -> dict:
        raw_results = state.get("raw_results", {})
        logger.info("Generating summary. Available tool results=%d", len(raw_results))

        if not raw_results:
            logger.warning("No raw results available; returning empty report")
            empty_report = ObservabilityReport(
                health="Unknown",
                kra_status=[],
                issues=["No data collected from AWS tools."],
                observations=[],
                cost_snapshot=[],
                security_posture=["Unable to assess security posture"],
                compliance_summary="Insufficient data for compliance assessment",
                actions=[],
            )
            return {"final_summary": empty_report.model_dump_json()}

        try:
            raw_str = json.dumps(raw_results, indent=2, default=str)
            prompt = f"""You are an autonomous AWS Cloud SRE / Observability Agent.

**Job Context & KRAs:**
{self.Kras}

Raw data from AWS tools:
{raw_str}

Analyze the data and produce a professional structured report."""

            structured_llm = self.Llm.with_structured_output(ObservabilityReport)
            report: ObservabilityReport = structured_llm.invoke([HumanMessage(content=prompt)])
            logger.info("Structured report generated. health=%s", report.health)
            return {"final_summary": report.model_dump_json()}
        except Exception as exc:
            logger.exception("LLM summary generation failed: %s", exc)
            raise SummaryGenerationError(f"Failed to generate observability report: {exc}") from exc

    def BuildGraph(self):
        logger.info("Building LangGraph pipeline")
        try:
            builder = StateGraph(AgentState)
            builder.add_node("trigger_tools", self.TriggerToolsNode)
            builder.add_node("execute_tools", ToolNode(TOOLS_LIST))
            builder.add_node("normalize", self.NormalizeNode)
            builder.add_node("summary", self.SummaryNode)
            builder.set_entry_point("trigger_tools")
            builder.add_edge("trigger_tools", "execute_tools")
            builder.add_edge("execute_tools", "normalize")
            builder.add_edge("normalize", "summary")
            builder.add_edge("summary", END)
            graph = builder.compile()
            logger.info("Graph compiled successfully")
            return graph
        except Exception as exc:
            logger.exception("Failed to build graph: %s", exc)
            raise

    def RunPipeline(self) -> PipelineResponse:
        logger.info("RunPipeline started for region=%s", self.Region)
        try:
            final_state = self.Graph.invoke({
                "messages": [],
                "raw_results": {},
                "final_summary": "",
                "region": self.Region,
            })

            raw_json = final_state.get("final_summary", "")
            if not raw_json:
                logger.warning("Pipeline completed but final_summary is empty")
                return PipelineResponse(
                    statusCode=500,
                    status="error",
                    exception="Pipeline completed but produced no output",
                    output=None,
                )

            report = ObservabilityReport.model_validate_json(raw_json)
            logger.info("RunPipeline completed. health=%s, issues=%d", report.health, len(report.issues))
            return PipelineResponse(
                statusCode=200,
                status="success",
                exception=None,
                output=report,
            )

        except ToolExecutionError as exc:
            logger.error("Tool execution failed: %s", exc)
            return PipelineResponse(
                statusCode=502,
                status="error",
                exception=str(exc),
                output=None,
            )

        except SummaryGenerationError as exc:
            logger.error("Summary generation failed: %s", exc)
            return PipelineResponse(
                statusCode=503,
                status="error",
                exception=str(exc),
                output=None,
            )

        except Exception as exc:
            logger.exception("Unexpected error in RunPipeline: %s", exc)
            return PipelineResponse(
                statusCode=500,
                status="error",
                exception=str(exc),
                output=None,
            )


# if __name__ == "__main__":
#     agent = AwsObservabilityAgent()
#     response = agent.RunPipeline()
#     print(response)