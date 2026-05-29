"""
AWS Generator Agent
===================
Generates Python scripts or Terraform files to execute AWS actions.
Supports Reference Folder for style and pattern matching.
"""

from __future__ import annotations

import json
import logging
import os
import random
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict
from chandra.config import settings
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
def _build_checkpointer() -> Any:
    """Return a checkpointer using a three-tier fallback strategy.

    Tier 1: Postgres (production)
    Tier 2: SQLite (local disk inside 'database/' folder)
    Tier 3: In-memory MemorySaver fallback
    """
    # ── Tier 1: Try Postgres ──
    try:
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg_pool import ConnectionPool
        import psycopg
        if hasattr(settings, "postgres_url") and settings.postgres_url:
            # Convert SQLAlchemy URL format to psycopg native format
            # postgresql+psycopg://... → postgresql://...
            conn_string = settings.postgres_url.replace("postgresql+psycopg://", "postgresql://")
            
            with psycopg.connect(conn_string, autocommit=True) as conn:
                PostgresSaver(conn).setup()
                
            pool = ConnectionPool(conn_string, max_size=10)
            checkpointer = PostgresSaver(pool)
            logger.info("checkpointer.postgres_setup_success")
            return checkpointer
        else:
            logger.warning("checkpointer.postgres_url_missing")
    except ImportError:
        logger.warning("checkpointer.postgres_unavailable")
    except Exception as exc:
        logger.warning(
            "checkpointer.postgres_setup_failed_fallback_to_sqlite",
            exc_info=exc,
        )

    # ── Tier 2: Try SQLite ──
    try:
        import sqlite3
        from langgraph.checkpoint.sqlite import SqliteSaver
        
        # Ensure the database directory exists
        db_dir = Path("database")
        db_dir.mkdir(parents=True, exist_ok=True)
        db_path = db_dir / "checkpoints.sqlite"
        
        # Initialize SQLite connection and checkpointer
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        checkpointer = SqliteSaver(conn)
        checkpointer.__enter__()  # Initialize the context manager
        logger.info(f"checkpointer.sqlite_setup_success at {db_path}")
        return checkpointer
    except ImportError:
        logger.warning("checkpointer.sqlite_unavailable")
    except Exception as exc:
        logger.warning(
            "checkpointer.sqlite_setup_failed_fallback_to_memory",
            exc_info=exc,
        )

    # ── Tier 3: MemorySaver ──
    logger.warning("checkpointer.fallback_to_memory_saver")
    return MemorySaver()

# ── Pydantic models ────────────────────────────────────────────────

class ActionAnalysis(BaseModel):
    needs_clarification: bool = Field(
        description="True ONLY when critical information is missing"
    )
    questions: List[str] = Field(default_factory=list)
    recommended_approach: str = Field(description="'python' | 'terraform' | 'both'")
    reasoning: str = Field(description="Brief justification")


class GeneratedFile(BaseModel):
    filename: str = Field(...)
    content: str = Field(...)
    file_type: str = Field(...)
    description: str = Field(...)


class ExecutableStep(BaseModel):
    description: str = Field(...)
    command: str = Field(...)


class CodeGenerationResult(BaseModel):
    files: List[GeneratedFile] = Field(...)
    executableSteps: List[ExecutableStep] = Field(...)
    summary: str = Field(...)


class AgentState(TypedDict):
    action: Dict[str, Any]
    analysis: Optional[Dict]
    clarification: Optional[Dict]
    generated_files: List[Dict]
    executable_steps: List[Dict]
    sandbox_path: str
    summary: str
    existing_files: List[Dict]
    reference_files: List[Dict]          # NEW
    feedback_summary: str
    input_sandbox_path: str
    reference_folder: str                # NEW


class GeneratorPipelineResponse(BaseModel):
    statusCode: int
    status: str
    exception: Optional[str] = None
    thread_id: Optional[str] = None
    sandbox_path: Optional[str] = None
    executableSteps: Optional[List[ExecutableStep]] = None
    summary: Optional[str] = None
    questions: Optional[List[str]] = None


# ── Agent ──────────────────────────────────────────────────────────

class GeneratorAgent:

    def __init__(self):
        logger.info("Initialising GeneratorAgent with Reference Folder support")
        try:
            self.Llm = ChatBedrockConverse(model_id=os.getenv("MODEL_NAME"))
            self.Checkpointer = _build_checkpointer()
            self.Graph = self._build_graph()
            logger.info("GeneratorAgent initialised successfully")
        except Exception as exc:
            logger.exception("Failed to initialise GeneratorAgent: %s", exc)
            raise

    # ── File Reading Helper ────────────────────────────────────────

    def _read_files_from_folder(self, folder_path: str, purpose: str = "files") -> List[Dict]:
        if not folder_path:
            return []

        path = Path(folder_path)
        if not path.exists() or not path.is_dir():
            logger.warning(f"{purpose} folder '{folder_path}' does not exist")
            return []

        files = []
        ignore_dirs = {".terraform", ".git", "__pycache__"}
        allowed_extensions = {".tf", ".py", ".json", ".yaml", ".yml", ".md", ".sh"}

        for file_path in sorted(path.rglob("*")):
            if not file_path.is_file():
                continue
            if any(part in ignore_dirs for part in file_path.parts):
                continue
            if file_path.name in {"terraform.tfstate", "terraform.tfstate.backup", ".terraform.lock.hcl"}:
                continue
            if file_path.suffix not in allowed_extensions and not file_path.name.endswith(".tfvars.example"):
                continue

            try:
                if file_path.stat().st_size > 100_000:
                    continue
                content = file_path.read_text(encoding="utf-8", errors="replace")
                rel = file_path.relative_to(path)
                files.append({
                    "filename": str(rel).replace("\\", "/"),
                    "content": content
                })
            except Exception as exc:
                logger.warning("Could not read %s: %s", file_path, exc)

        logger.info("Read %d %s file(s) from %s", len(files), purpose, folder_path)
        return files

    # ── Nodes ──────────────────────────────────────────────────────

    def _read_existing_node(self, state: AgentState) -> dict:
        existing = self._read_files_from_folder(state.get("input_sandbox_path"), "existing")
        return {"existing_files": existing}

    def _read_reference_node(self, state: AgentState) -> dict:
        reference = self._read_files_from_folder(state.get("reference_folder"), "reference")
        return {"reference_files": reference}

    def _analyze_node(self, state: AgentState) -> dict:
        action = state["action"]
        reference_files = state.get("reference_files") or []
        existing_files = state.get("existing_files") or []

        ref_ctx = ""
        if reference_files:
            ref_list = "\n".join(f"  - {f['filename']}" for f in reference_files)
            ref_ctx = f"\n\nREFERENCE FILES (Match their style, naming, and structure):\n{ref_list}"

        existing_ctx = ""
        if existing_files:
            existing_ctx = f"\n\nEXISTING FILES (Update these):\n" + "\n".join(f"  - {f['filename']}" for f in existing_files)

        prompt = f"""You are an AWS automation engineer. Analyze this action request.

Action Name: {action["actionName"]}
Action Description: {action["actionDescription"]}
Steps: {json.dumps(action.get("steps") or [], indent=2)}{ref_ctx}{existing_ctx}

Determine:
1. needs_clarification (True only if critical info is missing)
2. recommended_approach: 'python' | 'terraform' | 'both'
3. reasoning

Be conservative with clarification requests."""

        try:
            structured_llm = self.Llm.with_structured_output(ActionAnalysis)
            analysis: ActionAnalysis = structured_llm.invoke([HumanMessage(content=prompt)])
            return {"analysis": analysis.model_dump()}
        except Exception as exc:
            logger.exception("Analysis failed: %s", exc)
            raise

    def _hitl_node(self, state: AgentState) -> dict:
        questions = state["analysis"]["questions"]
        answers = interrupt(questions)
        answers_list = answers if isinstance(answers, list) else [answers]
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
        existing_files = state.get("existing_files") or []
        reference_files = state.get("reference_files") or []
        feedback = state.get("feedback_summary") or ""

        # Reference Context
        reference_context = ""
        if reference_files:
            ref_dump = "\n\n".join(
                f"=== REFERENCE: {f['filename']} ===\n{f['content']}" 
                for f in reference_files[:6]  # Limit for token safety
            )
            reference_context = f"""
IMPORTANT: Follow the coding style, structure, variable naming, and best practices from these reference files:

{ref_dump}
"""

        # Existing files context
        existing_context = ""
        if existing_files:
            existing_context = f"\n\nEXISTING FILES TO UPDATE:\n" + "\n\n".join(
                f"=== {f['filename']} ===\n{f['content']}" for f in existing_files
            )

        clarification_context = ""
        if clarification:
            qa = "\n".join(f"Q: {q}\nA: {a}" for q, a in zip(clarification["questions"], clarification["answers"]))
            clarification_context = f"\n\nCLARIFICATIONS:\n{qa}"

        feedback_context = ""
        if feedback:
            feedback_context = f"\n\nPREVIOUS EXECUTION FEEDBACK (MUST FIX):\n{feedback}"

        mode_instruction = (
            "UPDATE the existing files shown below while preserving their structure."
            if existing_files
            else "Generate complete, production-ready code from scratch."
        )

        prompt = f"""You are an AWS automation engineer. {mode_instruction}

Action Name: {action["actionName"]}
Action Description: {action["actionDescription"]}
Steps: {json.dumps(action.get("steps") or [], indent=2)}
Recommended approach: {analysis["recommended_approach"]}
{reference_context}{clarification_context}{feedback_context}{existing_context}

Requirements:
- All files must be complete and runnable
- Match the style from reference files exactly
- Python: use boto3 + argparse
- Terraform: proper module structure with variables
- Include all necessary executable steps
- CRITICAL: If feedback mentions a specific error or fix, apply it immediately

Generate the complete set of files."""

        try:
            structured_llm = self.Llm.with_structured_output(CodeGenerationResult)
            result: CodeGenerationResult = structured_llm.invoke([HumanMessage(content=prompt)])
            return {
                "generated_files": [f.model_dump() for f in result.files],
                "executable_steps": [s.model_dump() for s in result.executableSteps],
                "summary": result.summary,
            }
        except Exception as exc:
            logger.exception("Generation failed: %s", exc)
            raise

    def _write_files_node(self, state: AgentState) -> dict:
        input_path = state.get("input_sandbox_path") or ""
        if input_path:
            sandbox_dir = input_path
            Path(sandbox_dir).mkdir(exist_ok=True)
        else:
            rand_id = str(random.randint(100_000, 999_999))
            sandbox_dir = f"sandbox_{rand_id}"
            Path(sandbox_dir).mkdir(exist_ok=True)

        toolkit = FileManagementToolkit(root_dir=sandbox_dir)
        write_tool = {t.name: t for t in toolkit.get_tools()}["write_file"]

        for file_info in state["generated_files"]:
            filename = file_info["filename"]

            # Validate Terraform filenames for common typos
            if filename.endswith(".tfvars"):
                if "terrafor" in filename.lower() and filename.lower() != "terraform.tfvars":
                    logger.warning("Detected potential filename typo: '%s' → will be written as-is but may cause issues", filename)

            write_tool.invoke({"file_path": filename, "text": file_info["content"]})
            logger.info("Wrote %s", filename)

        return {"sandbox_path": sandbox_dir}

    # ── Routing ────────────────────────────────────────────────────

    @staticmethod
    def _route_after_analysis(state: AgentState) -> str:
        if state["analysis"]["needs_clarification"]:
            return "hitl"
        return "generate"

    # ── Graph ──────────────────────────────────────────────────────

    def _build_graph(self):
        builder = StateGraph(AgentState)
        builder.add_node("read_existing", self._read_existing_node)
        builder.add_node("read_reference", self._read_reference_node)
        builder.add_node("analyze", self._analyze_node)
        builder.add_node("hitl", self._hitl_node)
        builder.add_node("generate", self._generate_node)
        builder.add_node("write_files", self._write_files_node)

        builder.set_entry_point("read_existing")
        builder.add_edge("read_existing", "read_reference")
        builder.add_edge("read_reference", "analyze")

        builder.add_conditional_edges(
            "analyze",
            self._route_after_analysis,
            {"hitl": "hitl", "generate": "generate"}
        )
        builder.add_edge("hitl", "generate")
        builder.add_edge("generate", "write_files")
        builder.add_edge("write_files", END)

        return builder.compile(checkpointer=self.Checkpointer)

    # ── Public API ─────────────────────────────────────────────────

    def RunPipeline(
        self,
        action: Dict[str, Any],
        thread_id: Optional[str] = None,
        answers: Optional[List[str]] = None,
        sandbox_path: Optional[str] = None,
        reference_folder: Optional[str] = None,
        feedback_summary: Optional[str] = None,
    ) -> GeneratorPipelineResponse:
        tid = thread_id or str(uuid.uuid4())
        config = {"configurable": {"thread_id": tid}}

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
                        "existing_files": [],
                        "reference_files": [],
                        "feedback_summary": feedback_summary or "",
                        "input_sandbox_path": sandbox_path or "",
                        "reference_folder": reference_folder or "",
                    },
                    config=config,
                )

            snapshot = self.Graph.get_state(config)
            if snapshot.tasks and any(t.interrupts for t in snapshot.tasks):
                questions = snapshot.tasks[0].interrupts[0].value
                return GeneratorPipelineResponse(
                    statusCode=202,
                    status="needs_clarification",
                    thread_id=tid,
                    questions=questions,
                )

            final = snapshot.values
            return GeneratorPipelineResponse(
                statusCode=200,
                status="success",
                thread_id=tid,
                sandbox_path=final.get("sandbox_path"),
                executableSteps=final.get("executable_steps", []),
                summary=final.get("summary"),
            )

        except Exception as exc:
            logger.exception("RunPipeline error: %s", exc)
            return GeneratorPipelineResponse(
                statusCode=500,
                status="error",
                exception=str(exc),
                thread_id=tid,
            )


# if __name__ == "__main__":
#     agent = GeneratorAgent()

#     action_payload = {
#         "actionName": "Deploy Production RDS Instance with Terraform",
#         "actionDescription": "Deploy a single-AZ PostgreSQL RDS instance tagged as production.",
#         "steps": [""]
#     }

#     response = agent.RunPipeline(
#         action=action_payload,
#         sandbox_path=None,                    # Set to existing folder if updating
#         reference_folder="iac",     # ← Your reference folder
#         feedback_summary=""
#     )

#     print(response.model_dump_json(indent=2))