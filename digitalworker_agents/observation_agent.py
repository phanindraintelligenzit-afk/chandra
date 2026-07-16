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
# from langchain_aws import ChatBedrockConverse
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from digitalworker_agents.aws_execution_agent import check_cancelled

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

from tools.langchain_tools import DEFAULT_REGION, TOOLS_LIST, default_tool_args

load_dotenv(override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("AwsObservabilityAgent")




class _TokenUsageCallback(BaseCallbackHandler):
    """Logs actual input/output token counts returned by Bedrock after the LLM call."""

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        for generations in response.generations:
            for gen in generations:
                usage = getattr(gen.message, "usage_metadata", None) if hasattr(gen, "message") else None
                if usage:
                    logger.info(
                        "LLM token usage | input_tokens=%d | output_tokens=%d | total_tokens=%d",
                        usage.get("input_tokens", 0),
                        usage.get("output_tokens", 0),
                        usage.get("total_tokens", 0),
                    )
                    return
        logger.warning("Token usage metadata not available in LLM response")


class CostEntry(BaseModel):
    service: str = Field(description="AWS service name")
    daily_avg_usd: float = Field(description="Average daily cost in USD")
    change_24h_pct: Optional[float] = Field(default=None, description="24h cost change percentage")
    note: Optional[str] = Field(default=None, description="Anomaly flag, savings opportunity, etc.")


class IssueItem(BaseModel):
    issue: str = Field(description="Concise description of the issue or anomaly detected")
    priorityLevel: str = Field(description="Priority level: P1 (critical/immediate) | P2 (high/urgent) | P3 (medium/scheduled)")
    service: str = Field(description="Primary AWS service affected (e.g. S3, RDS, EC2, IAM, Bedrock, EKS, CloudTrail)")
    region: Optional[str] = Field(default=None, description="AWS region where the issue was detected, if applicable")
    resourceId: Optional[str] = Field(default=None, description="Specific resource ID or name involved, if identifiable")


class ActionItem(BaseModel):
    actionName: str = Field(description="Short name or title of the action (e.g. 'Revoke S3 Public Access')")
    actionDescription: str = Field(description="Detailed description of what needs to be done and why")
    service: str = Field(description="AWS service this action applies to (e.g. 'S3', 'RDS', 'EC2', 'CodeDeploy')")
    kraCode: str = Field(description="The KRA code this action addresses (must match one of the input KRA codes)")
    priorityLevel: str = Field(description="Execution priority: P1 (immediate) | P2 (this sprint) | P3 (backlog)")
    steps: Optional[List[str]] = Field(default=None, description="Ordered step-by-step instructions. Required for operational or task-type KRAs (e.g. deployments, migrations, runbook tasks). Omit for simple remediation actions.")


class KRAStatus(BaseModel):
    kra_code: str = Field(description="e.g. KRA-01")
    status: str = Field(description="Green | Yellow | Red")
    achievement: str = Field(description="Current performance against target")
    completedPercentage: int = Field(
        description=(
            "Estimated completion percentage (0–100 integer) reflecting how close this KRA is to its target. "
            "Ground the value in concrete evidence from tool data "
            "(e.g. 'Config enabled in 1/17 regions = 6%', '3 of 6 S3 buckets remediated = 50%'). "
            "General guidance: 0–33 → Red (severely behind), 34–66 → Yellow (at risk / partial), 67–100 → Green (on track or achieved)."
        )
    )
    note: Optional[str] = Field(default=None)


class ObservabilityReport(BaseModel):
    """Structured report aligned with the AWS Observability / Cloud SRE role."""
    health: str = Field(description="Overall health status: Healthy | Degraded | Critical")
    kra_status: List[KRAStatus] = Field(description="Status against each of the defined KRAs")
    issues: List[IssueItem] = Field(description="detected issues and anomalies, each with priority and affected service.")
    observations: List[str] = Field(description="Key findings from metrics, CloudTrail, Config, Security Hub, etc.")
    cost_snapshot: List[CostEntry] = Field(description="Top cost drivers with anomaly detection")
    security_posture: List[str] = Field(description="IAM drift, Security Hub findings, misconfigurations")
    compliance_summary: str = Field(description="Compliance evidence readiness summary")
    actions: List[ActionItem] = Field(description="Recommended actions driven by the input KRAs. One or more actions per KRA, ordered by KRA sequence then priorityLevel (P1 first). Actions reflect what is needed to achieve each KRA — independent of the issues list.")


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
            # self.Llm = ChatBedrockConverse(model_id=os.getenv("MODEL_NAME"))
            self.Llm = ChatOpenAI(
                base_url=os.getenv("OPENAI_API_BASE"),
                api_key=os.getenv("OPENAI_API_KEY"),
                model=os.getenv("OPENAI_MODEL_NAME"),
            )
            self.Graph = self.BuildGraph()
            logger.info("Agent initialised successfully")
        except Exception as exc:
            logger.exception("Failed to initialise agent: %s", exc)
            raise

    @staticmethod
    def _build_kras_str(kras: Optional[List[Any]]) -> str:
        if not kras:
            return ""
        lines = []
        for i, k in enumerate(kras, start=1):
            code = getattr(k, "code", None) or f"KRA-{i:02d}"
            name = getattr(k, "name", None)
            description = getattr(k, "description", "")
            if name and str(name).strip() and str(name).strip() != str(description).strip():
                # Both name and description provided — show name as title, description as detail
                lines.append(f"**{code}** {name}\n   Description: {description}")
            else:
                # Only description (or name == description) — show as before
                lines.append(f"**{code}** {description}")
        return "\n".join(lines)

    def TriggerToolsNode(self, state: AgentState) -> dict:
        check_cancelled()
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
        check_cancelled()
        logger.info("Normalising tool results")
        raw_results: Dict[str, Any] = {}
        failed_tools: List[str] = []

        for msg in state["messages"]:
            if not isinstance(msg, ToolMessage):
                continue
            tool_name = msg.name or "unknown_tool"
            try:
                raw = msg.content
                # Guard against empty / None content — this happens when a tool
                # raises an exception: LangChain stores "" in ToolMessage.content.
                if not raw or (isinstance(raw, str) and not raw.strip()):
                    logger.warning(
                        "Empty content for tool=%s (tool likely raised an exception)", tool_name
                    )
                    raw_results[tool_name] = {"error": "tool returned empty content"}
                    failed_tools.append(tool_name)
                    continue

                content = json.loads(raw) if isinstance(raw, str) else raw
                raw_results[tool_name] = content
                logger.debug("Normalised result for tool=%s", tool_name)
            except (json.JSONDecodeError, TypeError) as exc:
                logger.warning("Could not parse result for tool=%s: %s", tool_name, exc)
                raw_results[tool_name] = {"raw": str(msg.content)}
                failed_tools.append(tool_name)

        if failed_tools:
            logger.warning("Parse failures for tools: %s", failed_tools)

        for tool_name, result in raw_results.items():
            tool_chars = len(json.dumps(result, default=str))
            logger.info("Tool payload | tool=%s | chars=%d | estimated_tokens≈%d", tool_name, tool_chars, tool_chars // 4)

        total_chars = len(json.dumps(raw_results, default=str))
        logger.info(
            "Normalisation complete | tools_collected=%d | total_chars=%d | estimated_tokens≈%d",
            len(raw_results), total_chars, total_chars // 4,
        )
        return {"raw_results": raw_results}

    def SummaryNode(self, state: AgentState) -> dict:
        check_cancelled()
        raw_results = state.get("raw_results", {})
        logger.info("Generating summary. Available tool results=%d", len(raw_results))

        if not raw_results:
            logger.warning("No raw results available; returning empty report")
            empty_report = ObservabilityReport(
                health="Unknown",
                kra_status=[],
                issues=[IssueItem(
                    issue="No data collected from AWS tools.",
                    priorityLevel="P3",
                    service="Unknown",
                )],
                observations=[],
                cost_snapshot=[],
                security_posture=["Unable to assess security posture"],
                compliance_summary="Insufficient data for compliance assessment",
                actions=[],
            )
            return {"final_summary": empty_report.model_dump_json()}

        try:
            raw_str = json.dumps(raw_results, indent=2, default=str)
            prompt = f"""You are an autonomous AWS Cloud Engineer Agent capable of handling both observability analysis and operational tasks.

**User-defined KRAs (Key Result Areas):**
{self.Kras}

**AWS environment data collected by tools:**
{raw_str}

Produce a structured report following these rules:

1. **issues** — Enumerate problems found in the AWS data across cost, security, compliance, performance, and reliability.
   - `issue`: one-sentence description of what is wrong.
   - `priorityLevel`: P1 = active breach or outage, P2 = high risk / cost anomaly, P3 = best-practice gap.
   - `service`: primary AWS service (S3, RDS, EC2, IAM, Bedrock, EKS, CloudTrail, Config, etc.).
   - `region`: AWS region if determinable, else omit.
   - `resourceId`: specific resource name or ID if identifiable, else omit.

2. **observations** — All noteworthy findings from the tool data even when no fix is required.

3. **actions** — Recommended actions MUST be generated EXCLUSIVELY to fulfill the User-defined KRAs provided above. 
   - Do NOT create actions to fix general issues, alerts, or observations found by the tools unless they directly map to a specific input KRA.
   - The tool data (observations/alerts) is ONLY provided as context to help you write accurate `steps` for the requested KRA (e.g., finding existing VPCs, regions, or current state). It is NOT to be used to invent unrequested actions or cleanup tasks.

   For EVERY KRA provided, reason about its objective type and generate the appropriate actions:

   - **Observability / monitoring KRAs** (e.g. cost anomaly detection, IAM drift, compliance): Recommend what must be done to achieve or maintain the KRA target.
   - **Operational / task KRAs** (e.g. "deploy code from GitHub to EC2", "migrate database", "set up CI/CD pipeline"): Produce a concrete action plan with ordered `steps` strictly to achieve the KRA.
   - **Any other KRA type**: Assess what the KRA requires, and recommend actions that directly advance it.

   **CRITICAL RULES FOR ACTIONS & STEPS:**
   - **NO HARDCODED IDS OF ANY KIND**: NEVER hallucinate or assume hardcoded infrastructure IDs (e.g. AMIs like `ami-0c55b159cbfafe1f0`, VPC IDs like `vpc-12345`, Subnet IDs, Security Group IDs, Role ARNs, Account IDs, etc.). LLMs often memorize these from training data tutorials — DO NOT DO THIS. **Exception:** If an ID is EXPLICITLY provided in the User-defined KRA input, you MUST use it. Otherwise, if an existing resource ID is required, you MUST instruct the step to use a Terraform `data` source (e.g. `data "aws_ami"`, `data "aws_vpc"`) to fetch it dynamically, or use AWS CLI queries. You may use standard string constants (e.g. `instance_type="t2.micro"`, standard IAM policies, resource names, or tags).
   - **NO UNREQUESTED DESTRUCTION/CLEANUP**: Do NOT add steps to terminate, delete, or clean up existing resources based on observations (e.g. "Terminate problematic instances") UNLESS the KRA explicitly requests it. Actions must strictly fulfill the KRA and not proactively alter existing infrastructure based on unrelated observations.

   For each action:
   - `kraCode`: must exactly match one of the KRA codes listed above.
   - `actionName`: concise imperative title (≤10 words).
   - `actionDescription`: what to do, why it advances the KRA, and which resource is involved.
   - `service`: primary AWS service.
   - `priorityLevel`: P1 = do immediately, P2 = this sprint, P3 = backlog.
   - `steps`: ordered list of concrete steps — required for operational/task KRAs, optional for observability KRAs.

   Every KRA must have at least one action. Multiple actions per KRA are encouraged when data supports it.
   Order by KRA sequence, then P1 → P3 within each KRA.

4. **cost_snapshot** — Every service with notable spend or anomaly.

5. **security_posture** — List of IAM drift findings, Security Hub findings, and misconfigurations detected. Extract from tool data (e.g. fetch_active_threats, fetch_account_audit, check_recorder_status).

6. **compliance_summary** — A brief statement about the compliance readiness or posture based on available data.

7. **health** — Overall health status: "Healthy" if no critical issues, "Degraded" if issues present, "Critical" if P1 issues exist.

8. **kra_status** — For each KRA provide:
   - `status`: Green (on track) | Yellow (at risk) | Red (behind). Base on available tool data and actions needed.
   - `achievement`: Current performance against the KRA target in plain language.
   - `completedPercentage`: An integer from 0 to 100 estimating how close this KRA is to its target.
     Ground the value in concrete, countable evidence from the tool data, for example:
       • "Config enabled in 1 of 17 regions → 6%"
       • "3 of 6 public S3 buckets remediated → 50%"
       • "No cost anomaly alerts configured → 10%"
     General guidance (not a hard rule — let the evidence decide):
       0–33  → typically Red
       34–66 → typically Yellow
       67–100 → typically Green
   - `note` (optional): any additional context or caveat.

Never fabricate data not present in the tool results. For operational KRAs, steps may use AWS best practices when the tools provide partial context."""

            prompt_chars = len(prompt)
            raw_data_chars = len(raw_str)
            logger.info(
                "Sending to LLM | raw_data_chars=%d | total_prompt_chars=%d | estimated_prompt_tokens≈%d",
                raw_data_chars, prompt_chars, prompt_chars // 4,
            )

            structured_llm = self.Llm.with_structured_output(ObservabilityReport)
            report: ObservabilityReport = structured_llm.invoke(
                [HumanMessage(content=prompt)],
                config={"callbacks": [_TokenUsageCallback()]},
            )
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