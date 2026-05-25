"""
AWS Generator Agent
===================
Generates Python scripts or Terraform files to execute AWS actions.
Supports Human-in-the-Loop (HITL): if the action is ambiguous the pipeline
pauses, returns clarifying questions, and resumes once the caller supplies
answers (identified by thread_id).
"""

from __future__ import annotations

import json
import logging
import os
import random
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict

from pydantic import BaseModel, Field
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage
from langchain_aws import ChatBedrockConverse
from langchain_community.agent_toolkits import FileManagementToolkit
from langgraph.graph import END, StateGraph
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command, interrupt

load_dotenv(override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("GeneratorAgent")


# ── Pydantic models ────────────────────────────────────────────────

class ActionAnalysis(BaseModel):
    needs_clarification: bool = Field(
        description=(
            "True ONLY when critical information is missing and makes correct code generation "
            "impossible. Examples of things you DO need: specific AWS account ID, VPC/subnet IDs "
            "when required by the action, custom IAM role ARNs that cannot be inferred. "
            "Examples of things you do NOT need: default region (use us-east-1), instance types, "
            "tag names, standard resource names."
        )
    )
    questions: List[str] = Field(
        default_factory=list,
        description="Specific, concise questions. Empty list when needs_clarification is False.",
    )
    recommended_approach: str = Field(
        description="'python' | 'terraform' | 'both'",
    )
    reasoning: str = Field(description="Brief justification for approach and clarification decision")


class GeneratedFile(BaseModel):
    filename: str = Field(
        description="Relative path within sandbox folder (e.g. 'main.py', 'modules/iam.tf')"
    )
    content: str = Field(description="Complete, runnable file content — no placeholders")
    file_type: str = Field(description="python | terraform | shell | yaml | json")
    description: str = Field(description="One-sentence description of what this file does")


class ExecutableStep(BaseModel):
    description: str = Field(description="Human-readable description of what this step does (e.g. 'Run terraform init', 'Apply Terraform configuration')")
    command: str = Field(description="Actual shell command to execute from the sandbox root")


class CodeGenerationResult(BaseModel):
    files: List[GeneratedFile] = Field(description="All files needed to execute the action end-to-end")
    executableSteps: List[ExecutableStep] = Field(
        description=(
            "Ordered steps to execute the generated files. Each step has a human-readable description and the actual command. "
            "Examples: "
            "[{description: 'Initialize Terraform', command: 'terraform -chdir=infrastructure init'}, "
            "{description: 'Validate Terraform configuration', command: 'terraform -chdir=infrastructure validate'}, "
            "{description: 'Plan Terraform deployment', command: 'terraform -chdir=infrastructure plan'}, "
            "{description: 'Apply Terraform configuration', command: 'terraform -chdir=infrastructure apply -auto-approve'}] OR "
            "[{description: 'Install Python dependencies', command: 'pip install -r requirements.txt'}, "
            "{description: 'Enable AWS Config globally', command: 'python enable_config_global.py --region us-east-1 --profile default'}]"
        )
    )
    summary: str = Field(
        description="Brief summary of what was generated and the command to run it"
    )


class AgentState(TypedDict):
    action: Dict[str, Any]
    analysis: Optional[Dict]
    clarification: Optional[Dict]   # {"questions": [...], "answers": [...]}
    generated_files: List[Dict]
    executable_steps: List[Dict]  # List of {"description": str, "command": str}
    sandbox_path: str
    summary: str


class GeneratorPipelineResponse(BaseModel):
    statusCode: int
    status: str = Field(description="'success' | 'needs_clarification' | 'error'")
    exception: Optional[str] = None
    thread_id: Optional[str] = None
    sandbox_path: Optional[str] = None
    executableSteps: Optional[List[ExecutableStep]] = None
    summary: Optional[str] = None
    questions: Optional[List[str]] = None


# ── Agent ──────────────────────────────────────────────────────────

class GeneratorAgent:

    def __init__(self):
        logger.info("Initialising GeneratorAgent")
        try:
            self.Llm = ChatBedrockConverse(model_id=os.getenv("MODEL_NAME"))
            self.Checkpointer = MemorySaver()
            self.Graph = self._build_graph()
            logger.info("GeneratorAgent initialised successfully")
        except Exception as exc:
            logger.exception("Failed to initialise GeneratorAgent: %s", exc)
            raise

    # ── Nodes ──────────────────────────────────────────────────────

    def _analyze_node(self, state: AgentState) -> dict:
        action = state["action"]
        logger.info("Analyzing action: %s", action.get("actionName"))

        prompt = f"""You are an AWS automation engineer. Analyze this action request and decide the best code generation strategy.

Action Name: {action["actionName"]}
Action Description: {action["actionDescription"]}
Steps: {json.dumps(action.get("steps") or [], indent=2)}

Determine:

1. needs_clarification — Only True when a specific, concrete value is REQUIRED but absent:
   - Needs: specific AWS account ID that must be hardcoded, VPC/subnet ID for a VPC-bound resource,
     exact S3 bucket name that cannot be derived from the description.
   - Does NOT need: AWS region (default us-east-1), IAM policy names (infer sensible names),
     instance types (use t3.micro), tag values (use sensible defaults).
   Ask as few questions as possible — only what is truly blocking.

2. recommended_approach:
   - "python"    → boto3 scripts; best for one-time operations, multi-step workflows, stateful actions
   - "terraform" → HCL; best for idempotent infra provisioning and resource lifecycle management
   - "both"      → when Terraform creates infra AND a Python/Lambda script performs the operation

3. reasoning — one or two sentences.

Be conservative: only flag needs_clarification for genuinely missing blocking values."""

        try:
            structured_llm = self.Llm.with_structured_output(ActionAnalysis)
            analysis: ActionAnalysis = structured_llm.invoke([HumanMessage(content=prompt)])
            logger.info(
                "Analysis complete. needs_clarification=%s, approach=%s",
                analysis.needs_clarification,
                analysis.recommended_approach,
            )
            return {"analysis": analysis.model_dump()}
        except Exception as exc:
            logger.exception("Action analysis failed: %s", exc)
            raise

    def _hitl_node(self, state: AgentState) -> dict:
        questions = state["analysis"]["questions"]
        logger.info("HITL: pausing for %d question(s)", len(questions))

        # Pauses execution; resumes when caller calls RunPipeline with answers + thread_id
        answers = interrupt(questions)

        answers_list = answers if isinstance(answers, list) else [answers]
        logger.info("HITL: resumed with %d answer(s)", len(answers_list))
        return {
            "clarification": {
                "questions": questions,
                "answers": answers_list,
            }
        }

    def _generate_node(self, state: AgentState) -> dict:
        action = state["action"]
        analysis = state["analysis"]
        clarification = state.get("clarification")
        logger.info("Generating code. approach=%s", analysis["recommended_approach"])

        clarification_context = ""
        if clarification:
            qa_pairs = "\n".join(
                f"Q: {q}\nA: {a}"
                for q, a in zip(clarification["questions"], clarification["answers"])
            )
            clarification_context = f"\n\nUser clarifications provided:\n{qa_pairs}"

        prompt = f"""You are an AWS automation engineer. Generate complete, production-ready code for this action.

Action Name: {action["actionName"]}
Action Description: {action["actionDescription"]}
Steps: {json.dumps(action.get("steps") or [], indent=2)}
Recommended approach: {analysis["recommended_approach"]}
{clarification_context}

Requirements:
- ALL files must be complete and immediately runnable — zero placeholders, no TODOs
- Python files: use boto3; argparse for configurable params (--region, --profile, --dry-run);
  proper try/except with informative error messages; check resource existence before creating
- Terraform files: split into main.tf, variables.tf, outputs.tf; use variable blocks for
  environment-specific values; include a terraform.tfvars.example
- For Lambda functions: include both the Lambda handler file AND the Terraform/deployment code
- Handle idempotency: check if a resource already exists before creating it
- Use least-privilege IAM policies; never use wildcard (*) actions unless absolutely necessary
- Include a README.md only if setup steps are non-trivial (> 3 steps to get running)

executableSteps rules (CRITICAL):
- Return a list of objects, each with 'description' (human-readable) and 'command' (actual shell command)
- Example descriptions: 'Initialize Terraform', 'Run terraform plan', 'Apply configuration', 'Enable Config in all regions'
- Every step is a separate entry — never chain commands with &&
- NEVER include 'cd <dir>' in commands; all run from sandbox root
- Terraform in subdirectories: use 'terraform -chdir=<subdir> init' format
- Python scripts: use relative paths (e.g. 'python scripts/main.py --region us-east-1')
- Include every step separately: pip install, terraform init, validate, plan, apply, python calls

Generate ALL files needed. The code must implement the described action completely."""

        try:
            structured_llm = self.Llm.with_structured_output(CodeGenerationResult)
            result: CodeGenerationResult = structured_llm.invoke([HumanMessage(content=prompt)])
            logger.info("Code generation complete. files=%d | steps=%d", len(result.files), len(result.executableSteps))
            return {
                "generated_files": [f.model_dump() for f in result.files],
                "executable_steps": [s.model_dump() for s in result.executableSteps],
                "summary": result.summary,
            }
        except Exception as exc:
            logger.exception("Code generation failed: %s", exc)
            raise

    def _write_files_node(self, state: AgentState) -> dict:
        rand_id = str(random.randint(100_000, 999_999))
        sandbox_dir = f"sandbox_{rand_id}"
        Path(sandbox_dir).mkdir(exist_ok=True)
        logger.info("Writing %d file(s) to %s", len(state["generated_files"]), sandbox_dir)

        toolkit = FileManagementToolkit(root_dir=sandbox_dir)
        tools_by_name = {t.name: t for t in toolkit.get_tools()}
        write_tool = tools_by_name["write_file"]

        for file_info in state["generated_files"]:
            write_tool.invoke({"file_path": file_info["filename"], "text": file_info["content"]})
            logger.info("  wrote %s (%d bytes)", file_info["filename"], len(file_info["content"]))

        logger.info("All files written to %s", sandbox_dir)
        return {"sandbox_path": sandbox_dir}

    # ── Routing ────────────────────────────────────────────────────

    @staticmethod
    def _route_after_analysis(state: AgentState) -> str:
        if state["analysis"]["needs_clarification"]:
            return "hitl"
        return "generate"

    # ── Graph ──────────────────────────────────────────────────────

    def _build_graph(self):
        logger.info("Building LangGraph pipeline")
        try:
            builder = StateGraph(AgentState)
            builder.add_node("analyze", self._analyze_node)
            builder.add_node("hitl", self._hitl_node)
            builder.add_node("generate", self._generate_node)
            builder.add_node("write_files", self._write_files_node)

            builder.set_entry_point("analyze")
            builder.add_conditional_edges(
                "analyze",
                self._route_after_analysis,
                {"hitl": "hitl", "generate": "generate"},
            )
            builder.add_edge("hitl", "generate")
            builder.add_edge("generate", "write_files")
            builder.add_edge("write_files", END)

            graph = builder.compile(checkpointer=self.Checkpointer)
            logger.info("Graph compiled successfully")
            return graph
        except Exception as exc:
            logger.exception("Failed to build graph: %s", exc)
            raise

    # ── Public API ─────────────────────────────────────────────────

    def RunPipeline(
        self,
        action: Dict[str, Any],
        thread_id: Optional[str] = None,
        answers: Optional[List[str]] = None,
    ) -> GeneratorPipelineResponse:
        """
        Run or resume the generation pipeline.

        First call:  pass `action` only (thread_id is auto-generated and returned).
        Resume call: pass the `thread_id` from the previous response and the user's `answers`.
                     `action` is retained from checkpointed state and may be omitted on resume,
                     but passing it again is harmless.
        """
        tid = thread_id or str(uuid.uuid4())
        config = {"configurable": {"thread_id": tid}}

        logger.info(
            "RunPipeline started. action=%s | thread_id=%s | resuming=%s",
            action.get("actionName"),
            tid,
            answers is not None,
        )

        try:
            if answers is not None:
                self.Graph.invoke(Command(resume=answers), config=config)
            else:
                self.Graph.invoke(
                    {
                        "action": action,
                        "analysis": None,
                        "clarification": None,
                        "generated_files": [],
                        "executable_steps": [],
                        "sandbox_path": "",
                        "summary": "",
                    },
                    config=config,
                )

            # Inspect checkpoint to detect whether the graph paused at HITL
            snapshot = self.Graph.get_state(config)
            if snapshot.tasks and any(t.interrupts for t in snapshot.tasks):
                questions = snapshot.tasks[0].interrupts[0].value
                logger.info("Pipeline paused for HITL. questions=%d | thread_id=%s", len(questions), tid)
                return GeneratorPipelineResponse(
                    statusCode=202,
                    status="needs_clarification",
                    thread_id=tid,
                    questions=questions,
                )

            final = snapshot.values
            logger.info(
                "RunPipeline completed. sandbox=%s | files=%d",
                final.get("sandbox_path"),
                len(final.get("generated_files", [])),
            )
            return GeneratorPipelineResponse(
                statusCode=200,
                status="success",
                thread_id=tid,
                sandbox_path=final.get("sandbox_path"),
                executableSteps=final.get("executable_steps", []),
                summary=final.get("summary"),
            )

        except Exception as exc:
            logger.exception("Unexpected error in RunPipeline: %s", exc)
            return GeneratorPipelineResponse(
                statusCode=500,
                status="error",
                exception=str(exc),
                thread_id=tid,
            )


# if __name__ == "__main__":
#     agent = GeneratorAgent()
#
#     # --- First call (no HITL expected for this action) ---
#     response = agent.RunPipeline({
#         "actionName": "Enable AWS Config globally with Auto-Remediation",
#         "actionDescription": (
#             "Enable AWS Config in all 17 regions and enable auto-remediation for open SSH rules "
#             "and unencrypted EBS volumes. This achieves KRA-02's 'zero high-severity "
#             "misconfigurations open >24h' target."
#         ),
#         "steps": [
#             "Create a Lambda function that removes IngressRule with CidrIp: '0.0.0.0/0' from port 22",
#             "Assign this Lambda as an AWS Config remediation action for rule 'security-group-ssh-restricted'",
#             "Deploy the AWS Config configuration recorder in every region using AWS Organizations management account",
#             "Enable 'all' resource types and schedule recording every 3 hours",
#             "Enable auto-remediation for all critical rules via AWS Config rules console",
#         ],
#     })
#     print(response.model_dump_json(indent=2))
#
#     # --- If needs_clarification, resume with answers ---
#     # if response.status == "needs_clarification":
#     #     response = agent.RunPipeline(
#     #         action={"actionName": "..."},  # kept for signature; state is loaded from checkpoint
#     #         thread_id=response.thread_id,
#     #         answers=["us-east-1", "123456789012"],
#     #     )
#     #     print(response.model_dump_json(indent=2))
