"""
AWS Action Analyzer Agent
=========================
Analyzes cloud remediation actions, assigns severity/priority/human-review,
and creates a Jira ticket for each action.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, TypedDict

from pydantic import BaseModel, Field
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage
from src.chandra.llm import build_chat_model
from langgraph.graph import END, StateGraph

from tools.jira_tools.create_jira_ticket import add_approval_comment, add_comment_to_ticket, create_jira_ticket
from src.chandra.escalation.publisher import SNSPublisher
from src.chandra.escalation.schemas import EscalationPayload

load_dotenv(override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("AnalyzerAgent")


# ── Pydantic models ────────────────────────────────────────────────

class AnalyzedAction(BaseModel):
    actionName: str = Field(description="Exact actionName from the input")
    severity: str = Field(description="Severity level: HIGH | MEDIUM | LOW")
    HumanReviewNeeded: bool = Field(description="Whether human review is required before executing this action")
    priority: str = Field(description="Execution priority: HIGH | MEDIUM | LOW")


class AnalysisResult(BaseModel):
    actions: List[AnalyzedAction] = Field(description="Analysis for every input action")


class ActionResult(BaseModel):
    actionName: str
    severity: str
    HumanReviewNeeded: bool
    JiraIssueKey: str
    JiraUrl: str
    priority: str


class AgentState(TypedDict):
    actionsDict: Dict[str, Any]
    analyzed_actions: List[Dict]
    final_output: List[Dict]


class AnalyzerPipelineResponse(BaseModel):
    statusCode: int
    status: str
    exception: Optional[str] = None
    output: Optional[List[ActionResult]] = None


# ── Agent ──────────────────────────────────────────────────────────

class AnalyzerAgent:

    def __init__(self):
        logger.info("Initialising AnalyzerAgent")
        try:
            # MODEL_NAME is an optional override; when unset, the factory
            # falls back to the configured provider's model (BEDROCK_MODEL_ID /
            # OPENAI_MODEL_NAME / OLLAMA_MODEL). No hard requirement — a
            # missing env var must not crash the workflow.
            self.Llm = build_chat_model(model=os.getenv("MODEL_NAME"))
            self.Graph = self._build_graph()
            logger.info("AnalyzerAgent initialised successfully")
        except Exception as exc:
            logger.exception("Failed to initialise AnalyzerAgent: %s", exc)
            raise

    def _analyze_node(self, state: AgentState) -> dict:
        actions = state["actionsDict"].get("actions", [])
        logger.info("Analyzing %d actions", len(actions))

        prompt = f"""You are a cloud security and operations analyst reviewing AWS remediation actions.

For each action below, determine:

- **severity** (impact of the underlying issue):
  - HIGH   → active security threat or outage risk (e.g. public data exposure, credential compromise)
  - MEDIUM → significant risk that is not yet actively exploited (e.g. misconfiguration, cost overrun)
  - LOW    → best-practice gap or optimization (e.g. missing logging, idle resource)

- **HumanReviewNeeded** (before executing the remediation):
  - true  → human review required; action has high blast radius, is irreversible, or involves security-sensitive changes
  - false → safe to automate; low blast radius and easily reversible

- **priority** (order of execution):
  - HIGH   → execute immediately
  - MEDIUM → execute within current sprint
  - LOW    → schedule for backlog

Important: any service or label values must not contain spaces — use camelCase instead (e.g. "costExplorer", "awsConfig").

Actions to analyze:
{json.dumps(actions, indent=2)}

Return a structured analysis for ALL {len(actions)} actions, preserving the exact actionName from the input."""

        try:
            structured_llm = self.Llm.with_structured_output(AnalysisResult)
            result: AnalysisResult = structured_llm.invoke([HumanMessage(content=prompt)])
            logger.info("Analysis complete for %d actions", len(result.actions))
            return {"analyzed_actions": [a.model_dump() for a in result.actions]}
        except Exception as exc:
            logger.exception("LLM analysis failed: %s", exc)
            raise

    def _create_tickets_node(self, state: AgentState) -> dict:
        analyzed = state["analyzed_actions"]
        original_actions = state["actionsDict"].get("actions", [])
        project_key = state["actionsDict"].get("projectKey", "DEV")
        logger.info("Creating %d Jira tickets in project=%s", len(analyzed), project_key)

        original_map = {a["actionName"]: a for a in original_actions}
        priority_map = {"HIGH": "High", "MEDIUM": "Medium", "LOW": "Low"}
        final_output = []
        sns_payloads_to_send = []

        for action in analyzed:
            jira_priority = priority_map.get(action["priority"], "Medium")
            original = original_map.get(action["actionName"], {})
            summary = action["actionName"]
            description = original.get("actionDescription", "")
            steps = original.get("steps") or []
            service = original.get("service", "")
            words = service.split()
            camel_label = (words[0].lower() + "".join(w.capitalize() for w in words[1:])) if words else ""
            labels = [camel_label] if camel_label else []
            logger.info("Creating ticket for action='%s' priority=%s labels=%s", summary, jira_priority, labels)

            result = create_jira_ticket(
                project_key=project_key,
                summary=summary,
                description=description,
                issuetype="Task",
                priority=jira_priority,
                labels=labels,
            )

            if result["status"] == "success":
                logger.info("Ticket created: %s", result["issue_key"])
                if steps:
                    add_comment_to_ticket(result["issue_key"], steps)
                add_approval_comment(result["issue_key"], action["HumanReviewNeeded"])
                
                # Collect payload for SNS/Slack if Human Review is needed
                if action["HumanReviewNeeded"]:
                    payload = EscalationPayload(
                        finding_id=result["issue_key"],
                        resource_id=action["actionName"],
                        severity=action["severity"].lower() if action["severity"].lower() in ["low", "medium", "high", "critical"] else "high",
                        service=service if service else "AWS",
                        region="us-east-1",
                        summary=f"Human Review Required: {description}",
                        recommended_action=f"Review and approve Jira Ticket: {result['url']}"
                    )
                    sns_payloads_to_send.append(payload)

                final_output.append({
                    "actionName": action["actionName"],
                    "severity": action["severity"],
                    "HumanReviewNeeded": action["HumanReviewNeeded"],
                    "JiraIssueKey": result["issue_key"],
                    "JiraUrl": result["url"],
                    "priority": action["priority"],
                })
            else:
                logger.error(
                    "Failed to create ticket for action='%s': %s",
                    action["actionName"],
                    result.get("message"),
                )
                final_output.append({
                    "actionName": action["actionName"],
                    "severity": action["severity"],
                    "HumanReviewNeeded": action["HumanReviewNeeded"],
                    "JiraIssueKey": "ERROR",
                    "JiraUrl": "",
                    "priority": action["priority"],
                })

        # Fire all collected SNS notifications at the exact same time
        if sns_payloads_to_send:
            try:
                sns_arn = os.getenv("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:827295473120:chandra-escalation-critical")
                publisher = SNSPublisher(topic_arn=sns_arn)
                logger.info("Firing %d collected SNS notifications instantly so Chatbot bundles them...", len(sns_payloads_to_send))
                for payload in sns_payloads_to_send:
                    sns_result = publisher.publish(payload)
                    if sns_result.status == "success":
                        logger.info("Published SNS notification for HumanReviewNeeded action (Ticket: %s, MessageId: %s)", payload.finding_id, sns_result.message_id)
                    else:
                        logger.error("SNS publish failed for Ticket %s: %s", payload.finding_id, sns_result.error)
            except Exception as sns_exc:
                logger.error("Exception while setting up/publishing SNS batch: %s", sns_exc)

        return {"final_output": final_output}

    def _build_graph(self):
        logger.info("Building LangGraph pipeline")
        try:
            builder = StateGraph(AgentState)
            builder.add_node("analyze", self._analyze_node)
            builder.add_node("create_tickets", self._create_tickets_node)
            builder.set_entry_point("analyze")
            builder.add_edge("analyze", "create_tickets")
            builder.add_edge("create_tickets", END)
            graph = builder.compile()
            logger.info("Graph compiled successfully")
            return graph
        except Exception as exc:
            logger.exception("Failed to build graph: %s", exc)
            raise

    def RunPipeline(self, actionsDict: Dict[str, Any]) -> AnalyzerPipelineResponse:
        logger.info(
            "RunPipeline started. actions=%d, projectKey=%s",
            len(actionsDict.get("actions", [])),
            actionsDict.get("projectKey"),
        )
        try:
            final_state = self.Graph.invoke({
                "actionsDict": actionsDict,
                "analyzed_actions": [],
                "final_output": [],
            })

            output = final_state.get("final_output", [])
            if not output:
                return AnalyzerPipelineResponse(
                    statusCode=500,
                    status="error",
                    exception="Pipeline completed but produced no output",
                    output=None,
                )

            action_results = [ActionResult(**item) for item in output]
            logger.info("RunPipeline completed. processed=%d actions", len(action_results))
            return AnalyzerPipelineResponse(
                statusCode=200,
                status="success",
                output=action_results,
            )

        except Exception as exc:
            logger.exception("Unexpected error in RunPipeline: %s", exc)
            return AnalyzerPipelineResponse(
                statusCode=500,
                status="error",
                exception=str(exc),
                output=None,
            )


# if __name__ == "__main__":
#     agent = AnalyzerAgent()
#     actionsDict = {
#         "actions": [
#             {
#                 "actionName": "Revoke Public Access & Re-enable BlockPublicAccess",
#                 "actionDescription": "Immediately revoke public anonymous access to bucket 'chandra-synth-electric-gelding-leaky' by deleting its bucket policy. Re-enable BlockPublicAccess using PutBucketPublicAccessBlock with all settings true. This prevents accidental or malicious exposure. Root cause: github-actions-user made change — review CI/CD pipeline IAM permissions and enforce least privilege.",
#                 "service": "S3"
#             },
#             {
#                 "actionName": "Rotate Credentials & Audit Login Source",
#                 "actionDescription": "Rotate all credentials associated with RDS instance 'generatedfindingdbinstanceid'. Audit CloudTrail logs for the 'GeneratedFindingUserName' account — determine if it is a compromised temporary credential from a CI/CD pipeline. If so, disable the role/identity and reconfigure pipeline with least-privilege credentials. Implement RDS IAM authentication to reduce credential exposure.",
#                 "service": "RDS"
#             },
#             {
#                 "actionName": "Implement Cost Cap & Monitor Token Usage",
#                 "actionDescription": "Set AWS Budget alert at $250/month for Bedrock services. Configure CloudWatch alarm on 'InputTokenCount' for 'qwen.qwen3-next-80b-a3b' model to trigger at >50K tokens/day. Review usage patterns — high token count suggests unapproved LLM use. Implement model usage approval workflow via IAM conditions or AWS Resource Access Manager.",
#                 "service": "Bedrock"
#             },
#             {
#                 "actionName": "Block Port Probes & Harden SSH",
#                 "actionDescription": "Update security group for instance i-0eb04c4c172bf0f81 to block inbound traffic on all non-essential ports. Specifically deny 22 (SSH) from public IPs and restrict to bastion host or corporate IP range. Enforce SSH key-only authentication. Enable GuardDuty findings automation via AWS Lambda to auto-block malicious IPs via Network ACL.",
#                 "service": "EC2"
#             },
#             {
#                 "actionName": "Enable Config Recorder in All Regions",
#                 "actionDescription": "Enable AWS Config recorder and delivery channel in all 16 inactive regions using AWS Organizations SCP. Configure recording of all resource types. This is mandatory for KRA-02 compliance. Create automated remediation rule to auto-enable Config in any region where it's disabled.",
#                 "service": "AWS Config"
#             }
#         ],
#         "projectKey": "DEV"
#     }
#     response = agent.RunPipeline(actionsDict)
#     print(response.model_dump_json(indent=2))
