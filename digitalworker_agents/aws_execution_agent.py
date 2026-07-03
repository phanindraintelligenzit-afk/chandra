"""
AWS Unified Agent
=================
Single-agent, self-healing AWS automation pipeline.

Combines the previously separate Generator, Executor, and Orchestrator
agents into one LangGraph state machine with a single shared AgentState.

Flow per iteration:
    read_existing -> read_reference -> analyze -> [hitl?] -> generate
        -> write_files -> scan_folder -> plan -> execute -> report -> route

route:
    - success                    -> save_memory -> END
    - failed (retry left)        -> back to read_existing (next iteration, with feedback)
    - failed (exhausted/stuck)   -> mid_run_hitl -> (user answers) -> read_existing
    - needs_clarification (pre)  -> END (interrupt at analyze, resume with answers)

Memory (cross-run, persistent JSON file):
    - AgentMemory stores a rolling log of past actions: what was attempted, errors
      encountered, fixes applied, and whether it succeeded.
    - Loaded at pipeline start and injected into the analyze + generate prompts.
    - Written back at end of every run (success or failure).
    - Location: agent_memory.json (override via AGENT_MEMORY_PATH env var).

Mid-run HITL (when stuck):
    - After N consecutive identical error types with no progress, the pipeline
      pauses and asks the user for guidance instead of silently exhausting retries.
    - Resumes like normal HITL: re-call RunPipeline with answers + thread_id.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import platform
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from itertools import zip_longest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, TypedDict

from dotenv import load_dotenv
from langchain_aws import ChatBedrockConverse
from langchain_community.agent_toolkits import FileManagementToolkit
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field
from tools.jira_tools.create_jira_ticket import add_summary_comment, update_ticket_status

load_dotenv(override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("ExecutionAgents")

MAX_ITERATIONS = 6
DEFAULT_COMMAND_TIMEOUT = 500  # seconds
# Trigger mid-run HITL when the same error class repeats this many times with no fix
STUCK_THRESHOLD = 2


# ── Persistent cross-run memory ───────────────────────────────────────────────

class AgentMemory:
    """
    Lightweight JSON-file memory that persists across RunPipeline calls.

    Stores a rolling log of past pipeline runs so the agent can learn from
    previous mistakes and successes without repeating the same errors.

    Schema (agent_memory.json):
    {
      "runs": [
        {
          "timestamp": "2026-06-26T11:00:00",
          "action_name": "Deploy EC2 ...",
          "iterations": 2,
          "final_status": "failed",
          "errors_encountered": ["InvalidAMIID.NotFound: ami-0c55...", ...],
          "fixes_applied": ["Switched to aws_ami data source", ...],
          "lesson": "AMI IDs are region-specific — always use aws_ami data source"
        },
        ...
      ]
    }
    """

    MAX_RUNS = 50          # keep last N runs to cap file size
    MAX_ERRORS_PER_RUN = 5 # store only the most informative errors per run

    def __init__(self, memory_path: Optional[str] = None) -> None:
        self.path = Path(
            memory_path
            or os.getenv("AGENT_MEMORY_PATH", "agent_memory.json")
        )
        self._data: Dict[str, Any] = self._load()

    def _load(self) -> Dict[str, Any]:
        if self.path.exists():
            try:
                with open(self.path, encoding="utf-8") as f:
                    data = json.load(f)
                logger.info("memory.loaded  path=%s  runs=%d", self.path, len(data.get("runs", [])))
                return data
            except Exception as exc:
                logger.warning("memory.load_failed path=%s err=%s — starting fresh", self.path, exc)
        return {"runs": []}

    def _save(self) -> None:
        try:
            # trim to max runs
            self._data["runs"] = self._data["runs"][-self.MAX_RUNS:]
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, ensure_ascii=False)
            logger.info("memory.saved  path=%s  runs=%d", self.path, len(self._data["runs"]))
        except Exception as exc:
            logger.warning("memory.save_failed: %s", exc)

    @property
    def runs(self) -> List[Dict]:
        return self._data.get("runs", [])

    def record_run(
        self,
        action_name: str,
        iterations_used: int,
        final_status: str,
        execution_results: List[Dict],
        records: List[Dict],
        lesson: str = "",
    ) -> None:
        """Append a run summary and persist to disk."""
        # Strip ANSI colour codes AND Terraform box-drawing characters
        _ANSI_AND_BOX = re.compile(r"\x1b\[[0-9;]*m|[╷│╵]")

        def _clean(text: str) -> str:
            return _ANSI_AND_BOX.sub("", text)

        # Collect unique errors from all failed commands
        errors: List[str] = []
        for r in execution_results:
            if not r.get("success") and r.get("stderr"):
                raw = _clean(r["stderr"])
                # Skip blank lines and leftover box-drawing artifacts
                first_line = next(
                    (
                        ln.strip()
                        for ln in raw.splitlines()
                        if ln.strip() and not set(ln.strip()).issubset(set("╷│╵ "))
                    ),
                    raw.strip()[:200],
                )
                if first_line and first_line not in errors:
                    errors.append(first_line)
                if len(errors) >= self.MAX_ERRORS_PER_RUN:
                    break

        # Collect fix descriptions from the LLM-written executor_summary fields
        # in each iteration record (plain-English, not raw feedback blobs).
        fixes: List[str] = []
        for rec in records:
            summary = rec.get("executor_summary") or rec.get("feedback_used") or ""
            if summary:
                # Take only the first sentence and cap length
                sentence = summary.split(".")[0].strip()[:200]
                if sentence and sentence not in fixes:
                    fixes.append(sentence)

        entry = {
            "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
            "action_name": action_name,
            "iterations_used": iterations_used,
            "final_status": final_status,
            "errors_encountered": errors,
            "fixes_applied": fixes,
            "lesson": lesson,
        }
        self._data.setdefault("runs", []).append(entry)
        self._save()

    def context_for_action(self, action_name: str, max_relevant: int = 5) -> str:
        """
        Return a formatted memory context string to inject into prompts.
        Prioritises runs for the same action, then recent runs for any action.
        """
        runs = self._data.get("runs", [])
        if not runs:
            return ""

        # Score: same-action recent first, then other-action recent.
        # FIX-A1: clamp n_other to max(0, ...) so that when same fills the quota,
        # `other[-0:]` is NOT evaluated (which would return the entire list).
        same = [r for r in runs if r.get("action_name", "") == action_name]
        other = [r for r in runs if r.get("action_name", "") != action_name]
        n_other = max(0, max_relevant - len(same))
        relevant = (same[-max_relevant:] + (other[-n_other:] if n_other else []))[-max_relevant:]

        if not relevant:
            return ""

        lines = ["AGENT MEMORY — past pipeline runs (most recent last):"]
        for r in relevant:
            status_icon = "✓" if r["final_status"] == "success" else "✗"
            lines.append(
                f"  [{r['timestamp']}] {status_icon} {r['action_name']} "
                f"({r['iterations_used']} iter, {r['final_status']})"
            )
            if r.get("errors_encountered"):
                lines.append("    Errors seen: " + " | ".join(r["errors_encountered"][:3]))
            if r.get("lesson"):
                lines.append(f"    Lesson: {r['lesson']}")
        lines.append(
            "Apply these lessons to avoid repeating past mistakes. "
            "If the same error appears in memory AND in the current feedback, "
            "you MUST try a different fix this time."
        )
        return "\n".join(lines)


# ── Checkpointer ──────────────────────────────────────────────────────────────

def _build_checkpointer() -> Any:
    """Return a checkpointer using a three-tier fallback strategy.

    Tier 1: Postgres  (production)
    Tier 2: SQLite    (local disk, 'database/' folder)
    Tier 3: MemorySaver (in-process fallback)
    """
    # ── Tier 1: Postgres ──────────────────────────────────────────────────────
    try:
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg_pool import ConnectionPool
        import psycopg  # noqa: PLC0415
        import atexit

        conn_string = os.getenv("POSTGRES_URL", "")
        if conn_string:
            if conn_string.startswith("postgresql+psycopg://"):
                conn_string = conn_string.replace("postgresql+psycopg://", "postgresql://", 1)
            with psycopg.connect(conn_string, autocommit=True) as conn:
                PostgresSaver(conn).setup()
            pool = ConnectionPool(conn_string, max_size=10)
            atexit.register(pool.close)  # Cleanly close pool to prevent PythonFinalizationError on shutdown
            checkpointer = PostgresSaver(pool)
            logger.info("checkpointer.postgres_setup_success")
            return checkpointer
        logger.warning("checkpointer.postgres_url_missing")
    except ImportError:
        logger.warning("checkpointer.postgres_unavailable")
    except Exception as exc:
        logger.warning("checkpointer.postgres_setup_failed", exc_info=exc)

    # ── Tier 2: SQLite ────────────────────────────────────────────────────────
    try:
        import sqlite3  # noqa: PLC0415
        from langgraph.checkpoint.sqlite import SqliteSaver  # noqa: PLC0415

        db_dir = Path("database")
        db_dir.mkdir(parents=True, exist_ok=True)
        db_path = db_dir / "checkpoints.sqlite"
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        checkpointer = SqliteSaver(conn)
        checkpointer.setup()
        logger.info("checkpointer.sqlite_setup_success at %s", db_path)
        return checkpointer
    except ImportError:
        logger.warning("checkpointer.sqlite_unavailable")
    except Exception as exc:
        logger.warning("checkpointer.sqlite_setup_failed", exc_info=exc)

    # ── Tier 3: In-memory ─────────────────────────────────────────────────────
    logger.warning("checkpointer.fallback_to_memory_saver")
    return MemorySaver()


# ── Pydantic models ───────────────────────────────────────────────────────────

class ActionAnalysis(BaseModel):
    needs_clarification: bool = Field(
        description="True ONLY when critical information is missing that cannot be resolved dynamically"
    )
    questions: List[str] = Field(default_factory=list)
    recommended_approach: str = Field(description="'python' | 'terraform' | 'both'")
    reasoning: str = Field(description="Brief justification")
    dynamic_resolutions: List[str] = Field(
        default_factory=list,
        description=(
            "List of values that MUST be resolved dynamically at runtime rather than hardcoded. "
            "Derive these from what THIS SPECIFIC action actually provisions — do not assume EC2. "
            "Generic categories to consider, only where actually applicable to this action: "
            "AMI/image IDs, VPC/subnet/AZ IDs, account ID, region-specific ARNs, globally-unique "
            "resource names (e.g. S3 bucket names), KMS key references, IAM role/policy ARNs, "
            "DB engine version/parameter group families, security group IDs. "
            "This field instructs the generator to never hardcode these values."
        ),
    )
    requires_remote_access_credentials: bool = Field(
        default=False,
        description=(
            "True ONLY if this action provisions something a human will need to directly log "
            "into or connect to post-deployment (e.g. an EC2/EKS node needing SSH, an RDS "
            "instance needing a DB password, a Windows instance needing RDP). "
            "False for resources with no direct login surface (e.g. S3 bucket, IAM policy, "
            "SNS topic, CloudWatch alarm, Lambda function with no SSH access)."
        ),
    )
    credential_resolution_strategy: str = Field(
        default="",
        description=(
            "If requires_remote_access_credentials is True, briefly state HOW the credential "
            "should be generated dynamically for this specific resource type — e.g. "
            "'Generate SSH key pair via tls_private_key + aws_key_pair, save as .pem' for EC2, "
            "or 'Generate random_password for master_password, store via aws_secretsmanager_secret' "
            "for RDS. Leave empty if requires_remote_access_credentials is False."
        ),
    )
    post_deploy_outputs: List[str] = Field(
        default_factory=list,
        description=(
            "List of short output names (valid Terraform output identifiers, e.g. 'bucket_arn', "
            "'db_endpoint', 'instance_id', 'function_arn') that a human operator would actually "
            "need to see after THIS action succeeds, so they can use/verify the result. "
            "Derive this from what the action creates — do not copy a fixed template. "
            "Examples by resource (illustrative, not exhaustive): S3 bucket -> bucket_name, "
            "bucket_arn; RDS instance -> db_endpoint, db_port, db_name; EC2 instance -> "
            "instance_id, public_ip, ssh_command (if requires_remote_access_credentials); "
            "Lambda -> function_arn, function_name; IAM role -> role_arn. "
            "Always include at least one identifying output (ARN, ID, or name) for any resource "
            "created."
        ),
    )


class GeneratedFile(BaseModel):
    filename: str
    content: str
    file_type: str
    description: str


class ExecutableStep(BaseModel):
    description: str
    command: str


class CodeGenerationResult(BaseModel):
    files: List[GeneratedFile]
    executableSteps: List[ExecutableStep]
    summary: str


class ExecutionCommand(BaseModel):
    command: str = Field(description="Shell command to execute")
    description: str = Field(description="What this command does")
    working_dir: str = Field(
        default=".",
        description="Working directory relative to execute_folder ('.' = root)",
    )
    order: int = Field(description="Execution order (1 = first)")


class ExecutionPlan(BaseModel):
    execution_type: str = Field(description="'python' | 'terraform' | 'shell' | 'mixed'")
    commands: List[ExecutionCommand] = Field(description="Ordered list of commands to run")
    reasoning: str = Field(description="Brief explanation of the execution strategy")


class ExecutionResult(BaseModel):
    command: str
    description: str
    working_dir: str
    stdout: str
    stderr: str
    return_code: int
    success: bool
    timed_out: bool = False
    timeout_seconds: int = DEFAULT_COMMAND_TIMEOUT


class IterationRecord(BaseModel):
    iteration: int
    generator_status: str
    executor_status: str
    executor_success: bool
    feedback_used: Optional[str] = None
    executor_summary: Optional[str] = None   # LLM-written plain-English outcome
    sandbox_path: Optional[str] = None


class AgentState(TypedDict):
    # shared / control
    action: Dict[str, Any]
    reference_folder: str
    command_timeout: int
    max_iterations: int
    iteration: int
    records: List[Dict]
    feedback_summary: str
    memory_context: str          # cross-run memory injected into prompts
    consecutive_same_error: int  # tracks stuck-loop detection for mid-run HITL
    # FIX-5: explicit field for last seen error class — avoids false-positive
    # substring matches in the old `current_error_class in prior_feedback` check.
    last_error_class: str

    # generator-related
    analysis: Optional[Dict]
    clarification: Optional[Dict]
    generated_files: List[Dict]
    executable_steps: List[Dict]
    sandbox_path: str
    existing_files: List[Dict]
    reference_files: List[Dict]
    input_sandbox_path: str
    generator_summary: str

    # executor-related
    folder_contents: Optional[str]
    execution_plan: Optional[Dict]
    execution_results: List[Dict]
    success: bool
    executor_summary: str

    # final
    final_status: str
    final_summary: str


class PipelineResponse(BaseModel):
    statusCode: int
    status: str = Field(description="'success' | 'failed' | 'error' | 'needs_clarification'")
    exception: Optional[str] = None
    thread_id: str
    sandbox_path: Optional[str] = None
    iterations_used: int = 0
    iterations: List[IterationRecord] = Field(default_factory=list)
    execution_results: Optional[List[ExecutionResult]] = None
    summary: Optional[str] = None
    questions: Optional[List[str]] = None

# ── Shell execution helper ────────────────────────────────────────────────────
# FIX-1: Removed @tool decorator. execute_shell_command is called directly as a
# plain function — there is no reason to wrap it as a LangChain tool. Using @tool
# for direct programmatic calls adds unnecessary overhead and risks silent behaviour
# changes if LangChain ever alters how handle_tool_error defaults propagate.

_active_subprocesses = {}
_cancelled_threads = set()
_subprocesses_lock = threading.Lock()

def check_cancelled():
    if threading.get_ident() in _cancelled_threads:
        raise InterruptedError("Execution cancelled by user")

def register_subprocess(proc: subprocess.Popen):
    thread_id = threading.get_ident()
    with _subprocesses_lock:
        if thread_id not in _active_subprocesses:
            _active_subprocesses[thread_id] = []
        _active_subprocesses[thread_id].append(proc)

def unregister_subprocess(proc: subprocess.Popen):
    thread_id = threading.get_ident()
    with _subprocesses_lock:
        if thread_id in _active_subprocesses and proc in _active_subprocesses[thread_id]:
            _active_subprocesses[thread_id].remove(proc)

def cancel_thread_execution(thread_id: int):
    with _subprocesses_lock:
        _cancelled_threads.add(thread_id)
        procs = _active_subprocesses.get(thread_id, [])
        for proc in procs:
            try:
                if sys.platform == "win32":
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False)
                else:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except Exception:
                        proc.kill()
            except Exception:
                pass

def cleanup_thread_state():
    """Remove this thread's cancellation and subprocess state. Call in finally."""
    thread_id = threading.get_ident()
    with _subprocesses_lock:
        _cancelled_threads.discard(thread_id)
        _active_subprocesses.pop(thread_id, None)

def execute_shell_command(command: str, cwd: str, timeout: int) -> Dict[str, Any]:
    """Execute a shell command in a specific directory with a timeout."""
    proc_env = os.environ.copy()
    proc_env.setdefault("NO_COLOR", "1")
    proc_env.setdefault("TF_IN_AUTOMATION", "1")
    proc_env.setdefault("PYTHONIOENCODING", "utf-8")

    proc = subprocess.Popen(
        command,
        shell=True,
        cwd=cwd,
        env=proc_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    
    register_subprocess(proc)

    try:
        deadline = time.time() + timeout
        while True:
            check_cancelled()
            try:
                stdout_data, stderr_data = proc.communicate(timeout=1.0)
                break
            except subprocess.TimeoutExpired as exc:
                if time.time() > deadline:
                    raise exc
    except subprocess.TimeoutExpired as exc:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
            )
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                proc.kill()
        try:
            stdout_data, stderr_data = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            stdout_data, stderr_data = "", ""
        raise TimeoutError(stderr_data or "Command timed out") from exc
    finally:
        unregister_subprocess(proc)

    check_cancelled()

    return {
        "stdout": stdout_data or "",
        "stderr": stderr_data or "",
        "return_code": proc.returncode,
    }

# ── Unified Agent ─────────────────────────────────────────────────────────────

class ExecutionAgents:

    def __init__(self, max_iterations: int = MAX_ITERATIONS, memory_path: Optional[str] = None, job_id: Optional[str] = None) -> None:
        self.max_iterations = max_iterations
        self.job_id = job_id or "default"
        self.logger = logging.getLogger(f"ExecutionAgents.{self.job_id}")
        self.logger.propagate = True
        
        os.makedirs("logs", exist_ok=True)
        fh = logging.FileHandler(f"logs/{self.job_id}.log", mode='a', encoding='utf-8')
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s"))
        # Add handler if not already present
        if not any(isinstance(h, logging.FileHandler) and h.baseFilename == fh.baseFilename for h in self.logger.handlers):
            self.logger.addHandler(fh)
            
        self.logger.info("Initialising ExecutionAgents (max_iterations=%d, job_id=%s)", max_iterations, self.job_id)
        try:
            model_name = os.getenv("MODEL_NAME")
            if not model_name:
                raise ValueError(
                    "MODEL_NAME environment variable is not set. "
                    "Add MODEL_NAME=<bedrock-model-id> to your .env file."
                )
            self.Llm = ChatBedrockConverse(model_id=model_name)
            self.Memory = AgentMemory(memory_path)
            self.Checkpointer = _build_checkpointer()
            self.Graph = self._build_graph()
            self.logger.info("ExecutionAgents initialised successfully with model %s", model_name)
        except Exception as exc:
            self.logger.exception("Failed to initialise ExecutionAgents: %s", exc)
            raise

    # ── helpers ───────────────────────────────────────────────────────────────

    def _banner(self, text: str, char: str = "=", width: int = 78) -> None:
        self.logger.info(char * width)
        self.logger.info(text)
        self.logger.info(char * width)

    def _cleanup_sandbox(self, sandbox_path: Optional[str]) -> None:
        if sandbox_path and Path(sandbox_path).exists():
            try:
                shutil.rmtree(sandbox_path)
                self.logger.info("Cleaned up sandbox: %s", sandbox_path)
            except Exception as exc:
                self.logger.warning("Failed to clean up sandbox %s: %s", sandbox_path, exc)

    def _read_files_from_folder(self, folder_path: str, purpose: str = "files") -> List[Dict]:
        if not folder_path:
            return []

        path = Path(folder_path)
        if not path.exists() or not path.is_dir():
            self.logger.warning("%s folder '%s' does not exist", purpose, folder_path)
            return []

        ignore_dirs = {".terraform", ".git", "__pycache__"}
        allowed_extensions = {".tf", ".py", ".json", ".yaml", ".yml", ".md", ".sh", ".pem"}
        skip_names = {"terraform.tfstate", "terraform.tfstate.backup", ".terraform.lock.hcl"}

        files: List[Dict] = []
        for file_path in sorted(path.rglob("*")):
            if not file_path.is_file():
                continue
            if any(part in ignore_dirs for part in file_path.parts):
                continue
            if file_path.name in skip_names:
                continue
            if (
                file_path.suffix not in allowed_extensions
                and not file_path.name.endswith(".tfvars.example")
            ):
                continue
            try:
                if file_path.stat().st_size > 100_000:
                    continue
                content = file_path.read_text(encoding="utf-8", errors="replace")
                rel = file_path.relative_to(path)
                files.append({"filename": str(rel).replace("\\", "/"), "content": content})
            except Exception as exc:
                self.logger.warning("Could not read %s: %s", file_path, exc)

        self.logger.info("Read %d %s file(s) from %s", len(files), purpose, folder_path)
        return files

    # ── generator nodes ───────────────────────────────────────────────────────

    def _read_existing_node(self, state: AgentState) -> dict:
        existing = self._read_files_from_folder(
            state.get("input_sandbox_path", ""), "existing"
        )
        return {"existing_files": existing, "clarification": None}

    def _read_reference_node(self, state: AgentState) -> dict:
        reference = self._read_files_from_folder(
            state.get("reference_folder", ""), "reference"
        )
        return {"reference_files": reference}

    def _analyze_node(self, state: AgentState) -> dict:
        check_cancelled()
        action = state["action"]
        reference_files = state.get("reference_files") or []
        existing_files = state.get("existing_files") or []
        feedback = state.get("feedback_summary") or ""
        memory_ctx = state.get("memory_context") or ""

        ref_ctx = ""
        if reference_files:
            ref_list = "\n".join(f"  - {f['filename']}" for f in reference_files)
            ref_ctx = (
                f"\n\nREFERENCE FILES (Match their style, naming, and structure):\n{ref_list}"
            )

        existing_ctx = ""
        if existing_files:
            existing_ctx = "\n\nEXISTING FILES (Update these):\n" + "\n".join(
                f"  - {f['filename']}" for f in existing_files
            )

        feedback_ctx = (
            f"\n\nPREVIOUS EXECUTION FEEDBACK (errors to avoid repeating):\n{feedback}"
            if feedback else ""
        )

        memory_section = f"\n\n{memory_ctx}" if memory_ctx else ""

        prompt = f"""You are an AWS automation engineer. Analyze this action request and identify \
what must be resolved DYNAMICALLY at runtime (never hardcoded).

Action Name: {action["actionName"]}
Action Description: {action["actionDescription"]}
Steps: {json.dumps(action.get("steps") or [], indent=2)}{ref_ctx}{existing_ctx}{feedback_ctx}{memory_section}

This action may provision ANY AWS resource type (compute, storage, database, networking, IAM,
messaging, serverless, etc.) — do not assume it is EC2 unless the description says so. Reason
from first principles about what THIS resource type actually needs.

Determine:

1. needs_clarification — True ONLY when critical information is TRULY missing and CANNOT be
   resolved dynamically (e.g. the user must choose between multiple existing VPCs and we have no
   way to pick the right one, or must choose between two non-default options that materially
   change cost/behavior). Do NOT ask about IDs, names, or regions that can be resolved via AWS
   data sources or sensible defaults — that applies regardless of resource type.

2. dynamic_resolutions — every value that the generator MUST resolve dynamically instead of
   hardcoding, specific to what THIS action provisions. Think about what would break if this ran
   in a different account/region. Only list items that are actually relevant to this action's
   resource type(s) — do not pad the list with categories that don't apply (e.g. do not mention
   AMIs or SSH keys for an S3-only action).

3. requires_remote_access_credentials — True only if a human will need to log into / connect
   directly to what gets created (SSH, RDP, DB client connection). False for resources with no
   login surface.

4. credential_resolution_strategy — if (3) is True, the dynamic-generation pattern appropriate
   to THIS resource type specifically (do not default to SSH/EC2 patterns for non-compute
   resources).

5. post_deploy_outputs — the short list of output names a human operator actually needs to see
   to use/verify what this action created. Tailor this to the resource type; do not copy a fixed
   template from another resource type.

6. recommended_approach — 'python' | 'terraform' | 'both'

7. reasoning — Brief justification. If agent memory above contains lessons for this action,
   incorporate them.

Be conservative with clarification requests: if the generator can figure it out from AWS data
sources or sensible defaults, do NOT ask the user."""

        try:
            structured_llm = self.Llm.with_structured_output(ActionAnalysis)
            analysis: ActionAnalysis = structured_llm.invoke([HumanMessage(content=prompt)])
            return {"analysis": analysis.model_dump()}
        except Exception as exc:
            self.logger.exception("Analysis failed: %s", exc)
            raise

    def _hitl_node(self, state: AgentState) -> dict:
        questions: List[str] = state["analysis"].get("questions") or []
        if not questions:
            self.logger.warning(
                "_hitl_node reached with empty questions list — "
                "bypassing interrupt and proceeding to generate"
            )
            return {"clarification": None}

        answers = interrupt(questions)
        answers_list = answers if isinstance(answers, list) else [answers]
        return {
            "clarification": {
                "questions": questions,
                "answers": answers_list,
            }
        }

    def _generate_node(self, state: AgentState) -> dict:
        check_cancelled()
        action = state["action"]
        analysis = state["analysis"]
        clarification = state.get("clarification")
        existing_files = state.get("existing_files") or []
        reference_files = state.get("reference_files") or []
        feedback = state.get("feedback_summary") or ""
        dynamic_resolutions = analysis.get("dynamic_resolutions") or []
        requires_creds = bool(analysis.get("requires_remote_access_credentials"))
        credential_strategy = analysis.get("credential_resolution_strategy") or ""
        post_deploy_outputs = analysis.get("post_deploy_outputs") or []
        memory_ctx = state.get("memory_context") or ""

        os_name = platform.system()

        # Reference context
        reference_context = ""
        if reference_files:
            ref_dump = "\n\n".join(
                f"=== REFERENCE: {f['filename']} ===\n{f['content']}"
                for f in reference_files[:6]
            )
            reference_context = (
                "\nIMPORTANT: Follow the coding style, structure, variable naming, and best "
                f"practices from these reference files:\n\n{ref_dump}\n"
            )

        # Existing files context
        existing_context = ""
        if existing_files:
            existing_context = "\n\nEXISTING FILES TO UPDATE:\n" + "\n\n".join(
                f"=== {f['filename']} ===\n{f['content']}" for f in existing_files
            )

        clarification_context = ""
        if clarification:
            qa_lines = "\n".join(
                f"Q: {q}\nA: {a if a is not None else '(no answer provided)'}"
                for q, a in zip_longest(
                    clarification["questions"], clarification["answers"]
                )
            )
            clarification_context = f"\n\nCLARIFICATIONS FROM USER:\n{qa_lines}"

        feedback_context = (
            f"\n\nPREVIOUS EXECUTION FEEDBACK (MUST FIX — do not repeat these errors):\n{feedback}"
            if feedback else ""
        )

        dynamic_context = ""
        if dynamic_resolutions:
            items = "\n".join(f"  - {r}" for r in dynamic_resolutions)
            dynamic_context = f"""

DYNAMIC RESOLUTION REQUIREMENTS (CRITICAL — enforced by the analysis step):
The following values MUST be resolved at Terraform plan/apply time using data sources.
NEVER hardcode any of these — your code will fail in a different region or account:
{items}"""

        memory_section = f"\n{memory_ctx}" if memory_ctx else ""

        mode_instruction = (
            "UPDATE the existing files shown below while preserving their structure."
            if existing_files
            else "Generate complete, production-ready code from scratch."
        )

        shell_note = (
            "The executableSteps will run via subprocess with shell=True on WINDOWS (cmd.exe), "
            "inheriting the parent process's environment variables."
            if os_name == "Windows"
            else (
                "The executableSteps will run via subprocess with shell=True on a POSIX shell, "
                "inheriting the parent process's environment variables."
            )
        )
        creds_note = (
            "AWS credentials (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_DEFAULT_REGION / "
            "AWS_SESSION_TOKEN if applicable) are ALREADY loaded into the environment via dotenv "
            "and are automatically inherited by every executed command. "
            "DO NOT generate steps to set, export, or configure AWS credentials — terraform and "
            "boto3 will pick them up automatically."
        )

        # Only relevant when the credential is actually a private-key file (e.g. SSH) —
        # a DB password or token has no file-permission concern, so don't emit this note
        # for every credential type. Default to NOT assuming a key file when the
        # strategy is unspecified, since wrongly assuming SSH/.pem for e.g. a DB
        # password would just reintroduce EC2-shaped bias under a different guise.
        is_key_file_credential = requires_creds and bool(credential_strategy) and (
            "key" in credential_strategy.lower()
            or "pem" in credential_strategy.lower()
            or "ssh" in credential_strategy.lower()
        )
        pem_note = ""
        if is_key_file_credential:
            pem_note = (
                "WINDOWS .pem FILE HANDLING: On Windows, 'chmod 400' does not exist. "
                "After saving the .pem file, use 'icacls <file> /inheritance:r /grant:r \"%USERNAME%:R\"' "
                "to restrict permissions so SSH clients will accept the key."
                if os_name == "Windows"
                else (
                    "POSIX .pem FILE HANDLING: After saving the .pem private key file, "
                    "run 'chmod 400 <keyname>.pem' before any SSH usage."
                )
            )

        # Generic, analysis-driven credential guidance — replaces a hardcoded EC2/SSH
        # assumption. Only present when the analyzer determined this action actually
        # needs remote-access credentials (and the analyzer also says HOW, per resource type).
        credential_context = ""
        if requires_creds:
            credential_context = f"""

REMOTE ACCESS CREDENTIALS REQUIRED (per analysis step):
This action provisions something that needs direct human access post-deployment.
Resolution strategy for THIS resource type: {credential_strategy or "Generate the credential dynamically using the appropriate Terraform resource for this resource type — never hardcode a password, key, or token."}
{pem_note}
Save any generated secret (private key, password, token) to a local file or expose it via a
sensitive Terraform output — never print secrets in plain stdout logs."""

        # Generic, analysis-driven output requirements — replaces a hardcoded EC2
        # output list. post_deploy_outputs is whatever the analyzer determined a human
        # operator needs to see for THIS specific action.
        outputs_context = ""
        if post_deploy_outputs:
            outputs_list = "\n".join(f"    output \"{name}\" {{ ... }}" for name in post_deploy_outputs)
            outputs_terraform_cmds = "\n".join(f"    terraform output {name}" for name in post_deploy_outputs)
            outputs_context = f"""

MANDATORY OUTPUTS (per analysis step) — define exactly these in outputs.tf, one block each,
each referencing the real resource attribute it corresponds to (no placeholders):
{outputs_list}
The executableSteps after apply MUST run each of these so the user sees every relevant detail:
{outputs_terraform_cmds}"""

        prompt = f"""You are a senior AWS automation engineer. {mode_instruction}

Action Name: {action["actionName"]}
Action Description: {action["actionDescription"]}
Steps: {json.dumps(action.get("steps") or [], indent=2)}
Recommended approach: {analysis["recommended_approach"]}
Execution environment: {os_name}. {shell_note} {creds_note}
{dynamic_context}
{credential_context}
{outputs_context}
{memory_section}
{reference_context}{clarification_context}{feedback_context}{existing_context}

═══════════════════════════════════════════════════════════════
ABSOLUTE RULES — violating any of these causes immediate failure
═══════════════════════════════════════════════════════════════

RULE 0 — HCL SYNTAX: HEREDOC FOR MULTI-LINE STRINGS:
  Terraform/HCL does NOT support Python-style triple-quotes (\'\'\'  or \"\"\").
  For any multi-line string value (user_data, inline policy JSON, etc.) you MUST
  use HCL heredoc syntax:

    WRONG  (causes parse error):
      user_data = <<< triple-quotes >>>
      #!/bin/bash
      echo hello
      <<< end triple-quotes >>>

    CORRECT — heredoc:
      user_data = <<-EOT
        #!/bin/bash
        echo hello
      EOT

  Single-line strings still use normal double-quotes: name = "my-instance"

RULE 1 — NEVER HARDCODE ENVIRONMENT-SPECIFIC VALUES:
  This rule applies to ANY AWS resource type. The examples below cover several common resource
  types so you have a concrete pattern to follow — only apply the ones relevant to what this
  action actually creates; do not add irrelevant data sources for a resource type this action
  does not provision.

  ✗ WRONG:  ami = "ami-0c55b159cbfafe1f0"   ← region-specific, breaks everywhere else
  ✓ CORRECT: Use a data source:
      data "aws_ami" "latest" {{
        most_recent = true
        owners      = ["amazon"]          # or "099720109477" for Canonical Ubuntu
        filter {{
          name   = "name"
          values = ["al2023-ami-*-x86_64"] # Amazon Linux 2023; adjust for the OS needed
        }}
        filter {{
          name   = "virtualization-type"
          values = ["hvm"]
        }}
      }}
      # Then reference: ami = data.aws_ami.latest.id

  ✗ WRONG:  vpc_id = "vpc-0abc123"
  ✓ CORRECT: data "aws_vpc" "default" {{ default = true }}
             vpc_id = data.aws_vpc.default.id

  ✗ WRONG:  subnet_id = "subnet-0abc123"
  ✓ CORRECT: data "aws_subnets" "default" {{
               filter {{ name = "vpc-id" values = [data.aws_vpc.default.id] }}
             }}
             subnet_id = data.aws_subnets.default.ids[0]

  ✗ WRONG:  availability_zone = "us-east-1a"
  ✓ CORRECT (when you also set subnet_id): DO NOT set availability_zone at all —
             AWS infers it automatically from the subnet. Setting both causes
             InvalidParameterValue if they don't match.
  ✓ CORRECT (if you must set it): fetch the subnet's own AZ:
             data "aws_subnet" "selected" {{ id = data.aws_subnets.default.ids[0] }}
             availability_zone = data.aws_subnet.selected.availability_zone
             (This guarantees AZ and subnet are always consistent.)

  ✗ WRONG:  bucket = "my-app-data"   ← S3 bucket names are globally unique across ALL AWS
             accounts; a fixed name will collide and fail on any account where it's taken.
  ✓ CORRECT: derive a unique suffix at plan time:
      resource "random_id" "suffix" {{ byte_length = 4 }}
      resource "aws_s3_bucket" "this" {{
        bucket = "${{var.bucket_prefix}}-${{random_id.suffix.hex}}"
      }}

  ✗ WRONG:  master_password = "ChangeMe123!"   ← hardcoded DB credential, also a secret leak
  ✓ CORRECT: generate and store it, never inline it:
      resource "random_password" "db" {{
        length  = 20
        special = true
      }}
      resource "aws_secretsmanager_secret" "db_password" {{
        name = "${{var.db_identifier}}-master-password"
      }}
      resource "aws_secretsmanager_secret_version" "db_password" {{
        secret_id     = aws_secretsmanager_secret.db_password.id
        secret_string = random_password.db.result
      }}
      # Then reference: password = random_password.db.result

  ✗ WRONG:  account_id = "123456789012"
  ✓ CORRECT: data "aws_caller_identity" "current" {{}}
             account_id = data.aws_caller_identity.current.account_id

RULE 2 — REMOTE ACCESS CREDENTIALS (only applies if this action needs them):
  If — and only if — this action provisions something a human needs to directly connect to
  (the analysis step flagged requires_remote_access_credentials), generate that credential
  dynamically using the Terraform pattern appropriate to the resource type. See
  "REMOTE ACCESS CREDENTIALS REQUIRED" above for the specific strategy for THIS action.
  General pattern for any generated secret material:
    - Generate it with Terraform (e.g. tls_private_key for SSH keys, random_password for DB
      passwords) — never invent or hardcode a literal credential value.
    - Persist it where the user can retrieve it: a local_sensitive_file for keys, or an
      aws_secretsmanager_secret(+_version) for passwords, or a Terraform output marked
      sensitive = true.
    - Never print the raw secret to plain stdout/logs.
  If this action does NOT need remote-access credentials (e.g. S3, IAM, SNS, most Lambda/DynamoDB
  cases), do NOT generate any SSH key pair, password, or other credential — doing so adds
  unnecessary resources and will confuse the user.

RULE 3 — TERRAFORM BLOCK UNIQUENESS:
  Each of `provider`, `terraform {{}}`, and any given `output` name must be defined
  EXACTLY ONCE across all files. Check ALL files before adding a block. When updating
  existing files, remove duplicate declarations — never add a new output or provider
  that already exists elsewhere.

RULE 4 — NO INVENTED PROVIDER ARGUMENTS:
  The AWS provider only supports documented arguments (region, profile, assume_role,
  default_tags, etc.). Do NOT invent arguments like timeout_client or timeout_server.

RULE 5 — tfvars CONVENTION:
  Write terraform.tfvars.example with placeholder values appropriate to this action's variables.
  Do NOT write a real terraform.tfvars. Sensible defaults belong in variables.tf.

RULE 6 — LEARN FROM FEEDBACK:
  If the feedback_summary above contains a specific error (e.g. InvalidAMIID.NotFound,
  DuplicateOutputDefinition, provider argument error), fix THAT EXACT issue in this
  iteration. Do not repeat errors from previous iterations.

RULE 7 — EXECUTABLE STEPS MUST BE COMPLETE:
  Include all steps needed: terraform init → terraform validate → terraform plan →
  terraform apply -auto-approve → then run terraform output for every output declared in
  outputs.tf (see "MANDATORY OUTPUTS" above, derived from this action's actual resources)
  so the user gets every useful detail in the final summary. Do not run `terraform output`
  for names that don't exist in outputs.tf, and don't omit any that do.

Generate the complete set of files now."""

        try:
            structured_llm = self.Llm.with_structured_output(CodeGenerationResult)
            result: CodeGenerationResult = structured_llm.invoke([HumanMessage(content=prompt)])
            return {
                "generated_files": [f.model_dump() for f in result.files],
                "executable_steps": [s.model_dump() for s in result.executableSteps],
                "generator_summary": result.summary,
            }
        except Exception as exc:
            self.logger.exception("Generation failed: %s", exc)
            raise

    def _write_files_node(self, state: AgentState) -> dict:
        # FIX-4: resolve to absolute path so it stays stable regardless of cwd changes.
        input_path = state.get("input_sandbox_path") or ""
        if input_path:
            sandbox_dir = str(Path(input_path).resolve())
        else:
            sandbox_dir = str(Path(f"aws_executed_files/sandbox_{secrets.token_hex(6)}").resolve())

        Path(sandbox_dir).mkdir(parents=True, exist_ok=True)

        # FIX-2: Remove stale generated files before re-writing so Terraform (or Python)
        # does not pick up files that the LLM intentionally dropped in this iteration.
        # We only remove files with the same extensions we write; we preserve
        # .terraform/, terraform.tfstate*, .pem, and other runtime artefacts.
        CLEANABLE_EXTENSIONS = {".tf", ".py", ".sh", ".yaml", ".yml", ".json", ".md"}
        PRESERVE_NAMES = {
            "terraform.tfstate",
            "terraform.tfstate.backup",
            ".terraform.lock.hcl",
        }
        sandbox_path_obj = Path(sandbox_dir)
        # FIX-A2: only clean on retry iterations (iteration > 1).
        # The original guard `if state.get("input_sandbox_path")` was truthy on
        # iteration 1 whenever the caller passed sandbox_path= to RunPipeline,
        # which would delete legitimate pre-existing files on first write.
        if state.get("iteration", 1) > 1 and state.get("input_sandbox_path"):
            for existing_file in sandbox_path_obj.rglob("*"):
                if not existing_file.is_file():
                    continue
                if existing_file.name in PRESERVE_NAMES:
                    continue
                if any(part == ".terraform" for part in existing_file.parts):
                    continue
                if existing_file.suffix in CLEANABLE_EXTENSIONS:
                    try:
                        existing_file.unlink()
                        self.logger.debug("Removed stale file: %s", existing_file)
                    except Exception as exc:
                        self.logger.warning("Could not remove stale file %s: %s", existing_file, exc)

        toolkit = FileManagementToolkit(root_dir=sandbox_dir)
        write_tool = {t.name: t for t in toolkit.get_tools()}["write_file"]

        for file_info in state["generated_files"]:
            filename = file_info["filename"]
            if filename.endswith(".tfvars") and filename.lower() != "terraform.tfvars":
                self.logger.warning(
                    "Possible filename typo: '%s' — expected 'terraform.tfvars'", filename
                )
            write_tool.invoke({"file_path": filename, "text": file_info["content"]})
            self.logger.info("Wrote %s", filename)

        return {"sandbox_path": sandbox_dir, "input_sandbox_path": sandbox_dir}

    # ── executor nodes ────────────────────────────────────────────────────────

    def _scan_folder_node(self, state: AgentState) -> dict:
        execute_folder = state["sandbox_path"]
        self.logger.info("Scanning folder: %s", execute_folder)
        folder_path = Path(execute_folder)
        if not folder_path.exists():
            raise FileNotFoundError(f"sandbox_path not found: {execute_folder}")

        contents = sorted(
            str(p.relative_to(folder_path))
            for p in folder_path.rglob("*")
            if p.is_file()
        )
        folder_contents = "\n".join(contents) if contents else "(empty folder)"
        self.logger.info("Found %d file(s) in %s", len(contents), execute_folder)
        return {"folder_contents": folder_contents}

    def _plan_node(self, state: AgentState) -> dict:
        check_cancelled()
        action = state["action"]
        execute_folder = state["sandbox_path"]
        folder_contents = state.get("folder_contents") or ""

        self.logger.info("=" * 80)
        self.logger.info("PLANNING EXECUTION STRATEGY")
        self.logger.info("=" * 80)

        # Fast path: generator already supplied steps — no LLM call needed
        if state.get("executable_steps"):
            steps = state["executable_steps"]
            self.logger.info("✓ Using %d executable steps from generator", len(steps))
            self.logger.info("-" * 80)
            for idx, step in enumerate(steps, 1):
                self.logger.info("   [%d] %s", idx, step.get("description", step.get("command", "")))
            self.logger.info("-" * 80)
            commands = [
                {
                    "command": step.get("command") or step.get("cmd") or "",
                    "description": step.get("description", ""),
                    "working_dir": ".",
                    "order": i + 1,
                }
                for i, step in enumerate(steps)
            ]
            return {
                "execution_plan": {
                    "execution_type": "mixed",
                    "commands": commands,
                    "reasoning": "Steps provided by generator node.",
                }
            }

        # Slow path: ask LLM to derive a plan from the folder contents
        self.logger.info("Planning for action: %s", action.get("actionName"))
        os_name = platform.system()
        shell_note = (
            "Commands run via subprocess with shell=True on WINDOWS (cmd.exe)."
            if os_name == "Windows"
            else "Commands run via subprocess with shell=True on a POSIX shell."
        )
        creds_note = (
            "AWS credentials are ALREADY in the environment via dotenv. "
            "DO NOT generate steps to set or export AWS credentials."
        )

        toolkit = FileManagementToolkit(root_dir=execute_folder)
        read_tool = {t.name: t for t in toolkit.get_tools()}["read_file"]
        readable_exts = {".py", ".tf", ".sh", ".yaml", ".yml", ".json", ".tfvars"}

        file_previews: List[str] = []
        for rel_path in folder_contents.splitlines():
            rel_path = rel_path.strip()
            if not rel_path or Path(rel_path).suffix not in readable_exts:
                continue
            try:
                content = read_tool.invoke({"file_path": rel_path})
                lines = content.splitlines()
                preview = "\n".join(lines[:60])
                if len(lines) > 60:
                    preview += f"\n... ({len(lines) - 60} more lines)"
                file_previews.append(f"=== {rel_path} ===\n{preview}")
            except Exception:
                pass

        file_context = "\n\n".join(file_previews) or "(no readable source files)"

        prompt = f"""You are an AWS automation engineer. Create an execution plan for the files in this folder.

Action Name: {action["actionName"]}
Action Description: {action["actionDescription"]}
Steps: {json.dumps(action.get("steps") or [], indent=2)}

Execution environment: {os_name}. {shell_note} {creds_note}

Files in execute_folder:
{folder_contents}

File contents preview:
{file_context}

Create a detailed execution plan:

1. execution_type: "python" | "terraform" | "shell" | "mixed"

2. commands — ordered list. Follow these workflows:
   Terraform: terraform init → terraform validate → terraform plan → terraform apply -auto-approve
              → terraform output (always run this last to display connection info, IDs, etc.)
   Python:    pip install -r requirements.txt (if present) → python <script>.py [args]
   Shell:     chmod +x <script>.sh → ./<script>.sh

3. reasoning — one or two sentences.

Only include commands needed for the actual files present."""

        try:
            structured_llm = self.Llm.with_structured_output(ExecutionPlan)
            plan: ExecutionPlan = structured_llm.invoke([HumanMessage(content=prompt)])
            self.logger.info("✓ EXECUTION PLAN — type=%s  commands=%d", plan.execution_type, len(plan.commands))
            self.logger.info("  Reasoning: %s", plan.reasoning)
            for cmd in plan.commands:
                self.logger.info("  [%d] %s | %s", cmd.order, cmd.description, cmd.command)
            return {"execution_plan": plan.model_dump()}
        except Exception as exc:
            self.logger.exception("Execution planning failed: %s", exc)
            raise

    def _validate_command(self, command: str, execute_folder: str) -> Tuple[bool, str]:
        """Pre-execution validation. Returns (is_valid, error_message)."""
        base_path = Path(execute_folder).resolve()
        match = re.search(r'-var-file="?([^"\s]+)"?', command)
        if match:
            var_file = match.group(1)
            if not (base_path / var_file).exists():
                return False, f"Variable file does not exist: {var_file}"
        return True, ""

    def _execute_node(self, state: AgentState) -> dict:
        check_cancelled()
        execute_folder = state["sandbox_path"]
        plan = state["execution_plan"]
        _raw_timeout = state.get("command_timeout")
        timeout: int = (
            _raw_timeout
            if isinstance(_raw_timeout, int) and _raw_timeout > 0
            else DEFAULT_COMMAND_TIMEOUT
        )

        if not plan or not plan.get("commands"):
            self.logger.error("No execution plan / commands found — skipping execution")
            return {"execution_results": [], "success": False}

        base_path = Path(execute_folder).resolve()
        total_commands = len(plan["commands"])
        results: List[Dict] = []

        self.logger.info("=" * 80)
        self.logger.info(
            "EXECUTION STARTED: %d command(s) in %s  [timeout=%ds/cmd]",
            total_commands, execute_folder, timeout,
        )
        self.logger.info("=" * 80)

        for idx, cmd_info in enumerate(
            sorted(plan["commands"], key=lambda c: c.get("order", 9999)), 1
        ):
            command: str = cmd_info["command"]
            description: str = cmd_info.get("description", "")
            relative_dir: str = cmd_info.get("working_dir", ".")

            cwd = (base_path / relative_dir).resolve()
            if not cwd.exists():
                cwd = base_path

            # Pre-execution validation
            is_valid, error_msg = self._validate_command(command, str(cwd))
            if not is_valid:
                self.logger.warning("[%d/%d] ✗ VALIDATION FAILED: %s", idx, total_commands, description or command)
                self.logger.warning("        Validation Error: %s", error_msg)
                results.append({
                    "command": command,
                    "description": description,
                    "working_dir": str(cwd),
                    "stdout": "",
                    "stderr": f"Pre-execution validation failed: {error_msg}",
                    "return_code": -1,
                    "success": False,
                    "timed_out": False,
                    "timeout_seconds": timeout,
                })
                self.logger.error(
                    "EXECUTION HALTED (VALIDATION FAILED): %d remaining command(s) skipped",
                    total_commands - idx,
                )
                break

            self.logger.info("-" * 80)
            self.logger.info("[%d/%d] EXECUTING: %s", idx, total_commands, description or command)
            self.logger.info("        Command    : %s", command)
            self.logger.info("        Working Dir: %s", cwd)
            self.logger.info("        Timeout    : %ds", timeout)
            self.logger.info("-" * 80)

            timed_out = False
            result: Dict[str, Any]

            try:
                # FIX-1: call as a plain function, not via .invoke()
                tool_output = execute_shell_command(
                    command=command,
                    cwd=str(cwd),
                    timeout=timeout,
                )

                success = tool_output["return_code"] == 0
                stdout_data = tool_output["stdout"]
                stderr_data = tool_output["stderr"]
                proc_returncode = tool_output["return_code"]

                result = {
                    "command": command,
                    "description": description,
                    "working_dir": str(cwd),
                    "stdout": stdout_data[:10_000],
                    "stderr": stderr_data[:8_000],
                    "return_code": proc_returncode,
                    "success": success,
                    "timed_out": False,
                    "timeout_seconds": timeout,
                }

                if success:
                    self.logger.info(
                        "[%d/%d] ✓ SUCCESS: %s (rc=%d)",
                        idx, total_commands, description or command, proc_returncode,
                    )
                    if stdout_data:
                        self.logger.info(
                            "        Output: %s%s",
                            stdout_data[:300],
                            "..." if len(stdout_data) > 300 else "",
                        )
                else:
                    self.logger.warning(
                        "[%d/%d] ✗ FAILED: %s (rc=%d)",
                        idx, total_commands, description or command, proc_returncode,
                    )
                    self.logger.warning("        Error: %s", stderr_data[:500])

            except TimeoutError as exc:
                timed_out = True
                stderr_data = str(exc)
                timeout_msg = (
                    f"Command '{command}' timed out after {timeout}s. "
                    f"Step '{description}' did not complete within the allowed time. "
                    "If this is 'terraform init', it is likely a slow provider-plugin download "
                    "on first run (not a code or credentials issue) — DO NOT add invalid provider "
                    "arguments like 'timeout_client'/'timeout_server'. "
                    "If this is 'terraform plan'/'apply', check AWS credentials or network access. "
                    "The orchestrator will retry automatically; no code change is needed."
                )
                result = {
                    "command": command,
                    "description": description,
                    "working_dir": str(cwd),
                    "stdout": "",
                    "stderr": timeout_msg,
                    "return_code": -1,
                    "success": False,
                    "timed_out": True,
                    "timeout_seconds": timeout,
                }
                self.logger.error(
                    "[%d/%d] ✗ TIMEOUT: %s exceeded %ds",
                    idx, total_commands, description or command, timeout,
                )

            except Exception as exc:
                result = {
                    "command": command,
                    "description": description,
                    "working_dir": str(cwd),
                    "stdout": "",
                    "stderr": str(exc),
                    "return_code": -1,
                    "success": False,
                    "timed_out": False,
                    "timeout_seconds": timeout,
                }
                self.logger.exception(
                    "[%d/%d] ✗ EXCEPTION in '%s': %s",
                    idx, total_commands, description or command, exc,
                )

            results.append(result)

            if not result["success"]:
                halt_reason = "TIMEOUT" if timed_out else "COMMAND FAILED"
                self.logger.error(
                    "EXECUTION HALTED (%s): %d remaining command(s) skipped",
                    halt_reason, total_commands - idx,
                )
                break

        overall_success = (
            bool(results)
            and all(r["success"] for r in results)
            and len(results) == total_commands
        )

        self.logger.info("=" * 80)
        if overall_success:
            self.logger.info("✓ EXECUTION COMPLETED SUCCESSFULLY: All %d command(s) ran", len(results))
        else:
            timed_out_count = sum(1 for r in results if r.get("timed_out"))
            if timed_out_count:
                self.logger.warning("✗ EXECUTION: %d command(s) timed out", timed_out_count)
            else:
                self.logger.warning(
                    "✗ EXECUTION: %d/%d command(s) succeeded",
                    sum(1 for r in results if r["success"]), len(results),
                )
        self.logger.info("=" * 80)

        return {"execution_results": results, "success": overall_success}

    def _report_node(self, state: AgentState) -> dict:
        check_cancelled()
        action = state["action"]
        results = state.get("execution_results") or []
        success = state.get("success", False)
        timeout = state.get("command_timeout") or DEFAULT_COMMAND_TIMEOUT
        analysis = state.get("analysis") or {}
        post_deploy_outputs = analysis.get("post_deploy_outputs") or []

        self.logger.info("=" * 80)
        self.logger.info("GENERATING EXECUTION REPORT")
        self.logger.info("Action: %s | Status: %s", action.get("actionName"), "SUCCESS" if success else "FAILED")
        self.logger.info(
            "Commands: %d total, %d succeeded, %d failed",
            len(results),
            sum(1 for r in results if r["success"]),
            sum(1 for r in results if not r["success"]),
        )
        self.logger.info("=" * 80)

        parts = []
        for r in results:
            status_label = (
                "SUCCESS" if r["success"] else ("TIMED OUT" if r.get("timed_out") else "FAILED")
            )
            entry = [
                f"Command    : {r['command']}",
                f"Status     : {status_label} (rc={r['return_code']})",
            ]
            if r.get("timed_out"):
                entry.append(
                    f"Timeout    : exceeded {r.get('timeout_seconds', timeout)}s — "
                    "process was killed"
                )
            entry.append(f"Output     : {r['stdout'][:3000] or '(none)'}")
            entry.append(f"Errors     : {r['stderr'][:500] or '(none)'}")
            parts.append("\n".join(entry))

        results_text = "\n\n".join(parts) or "(no commands were executed)"
        any_timed_out = any(r.get("timed_out") for r in results)

        timeout_rule = (
            f"- One or more commands TIMED OUT. Explicitly state which command timed out "
            f"and that it exceeded the {timeout}s limit."
            if any_timed_out
            else (
                "- NO command timed out (timed_out=false for all results). DO NOT use the phrase "
                "'timed out'. Describe the ACTUAL error from the 'Errors' field."
            )
        )

        # Generic, analysis-driven output-listing instruction — replaces a hardcoded
        # EC2-specific mandate. post_deploy_outputs is whatever the analyzer derived
        # for THIS action (e.g. bucket_arn for S3, db_endpoint for RDS, instance_id for EC2).
        if post_deploy_outputs and success:
            output_lines = "\n".join(f"    {name} : <value from terraform output {name}>" for name in post_deploy_outputs)
            outputs_rule = (
                "- On success, you MUST explicitly list ALL of these on separate lines, with "
                "exact values extracted from the Output fields above — do not invent them:\n"
                f"{output_lines}"
            )
        else:
            outputs_rule = (
                "- On success, list any resource IDs/ARNs/names visible in the Output fields "
                "above so the user can identify what was created."
            )

        prompt = f"""Summarize the execution of this AWS automation action in 2–4 sentences.

Action: {action["actionName"]}
Description: {action["actionDescription"]}
Overall Success: {success}
Per-command timeout: {timeout}s

Execution Results:
{results_text}

Rules:
{timeout_rule}
- Name exact argument/error from the 'Errors' field — do not invent errors.
- Cover: what ran, whether it succeeded, resource IDs created, concrete next steps on failure.
{outputs_rule}"""

        try:
            response = self.Llm.invoke([HumanMessage(content=prompt)])
            summary = response.content
            self.logger.info("✓ LLM summary generated")
        except Exception as exc:
            self.logger.warning("LLM summary failed, using fallback: %s", exc)
            timed_out_cmds = [r for r in results if r.get("timed_out")]
            if timed_out_cmds:
                names = ", ".join(f"'{r['command']}'" for r in timed_out_cmds)
                summary = (
                    f"Execution of '{action['actionName']}' failed: {names} timed out after "
                    f"{timeout}s. Check AWS credentials and network access, then retry."
                )
            else:
                succeeded = sum(1 for r in results if r["success"])
                summary = (
                    f"Executed {succeeded}/{len(results)} command(s) for "
                    f"'{action['actionName']}'. "
                    + ("All steps completed successfully." if success else "Some commands failed — review stderr.")
                )

        return {"executor_summary": summary}

    # ── orchestration node ────────────────────────────────────────────────────

    @staticmethod
    def _extract_error_class(stderr: str) -> str:
        """
        Extract a short canonical error class from stderr so we can detect when
        the agent is stuck repeating the same error type across iterations.
        Examples:
            "InvalidAMIID.NotFound: ..."  -> "InvalidAMIID.NotFound"
            "Error: Duplicate output ..." -> "Duplicate output definition"
            "Error: No such file ..."     -> "No such file"
        """
        clean = re.sub(r"\x1b\[[0-9;]*m", "", stderr)
        # AWS API errors like "api error SomeCode: ..."
        m = re.search(r"api error ([A-Za-z0-9_.]+)", clean)
        if m:
            return m.group(1)
        # Terraform "Error: <title>" — grab first non-blank line after "Error:"
        m = re.search(r"Error:\s+(.+)", clean)
        if m:
            return m.group(1).strip()[:80]
        return clean.split("\n")[0].strip()[:80]

    def _record_iteration_node(self, state: AgentState) -> dict:
        iteration = state["iteration"]
        records = list(state.get("records") or [])

        record = IterationRecord(
            iteration=iteration,
            generator_status="success",
            executor_status="success" if state.get("success") else "failed",
            executor_success=bool(state.get("success")),
            feedback_used=state.get("feedback_summary") or None,
            executor_summary=state.get("executor_summary") or None,
            sandbox_path=state.get("sandbox_path"),
        )
        records.append(record.model_dump())

        if state.get("success"):
            self._banner(f"✓ PIPELINE COMPLETED SUCCESSFULLY on iteration {iteration}", char="═")
            return {
                "records": records,
                "final_status": "success",
                "final_summary": state.get("executor_summary"),
                "consecutive_same_error": 0,
                "last_error_class": "",
            }

        max_iter = state.get("max_iterations", MAX_ITERATIONS)

        # ── Build per-iteration feedback ──────────────────────────────────────
        raw_error = ""
        current_error_class = ""
        for r in state.get("execution_results") or []:
            if not r.get("success"):
                raw_error = (
                    f"Command '{r['command']}' failed (rc={r['return_code']}).\n"
                    f"stderr:\n{r['stderr'][:6000]}"
                )
                current_error_class = self._extract_error_class(r.get("stderr", ""))
                break

        # FIX-3: Compare error classes directly using the stored last_error_class field
        # instead of doing an unreliable substring search in the full feedback string.
        # This prevents false-positive stuck detection when short error strings like
        # "No such file" happen to appear anywhere in the accumulated feedback text.
        prior_error_class = state.get("last_error_class") or ""
        consecutive = state.get("consecutive_same_error", 0)

        if current_error_class and current_error_class == prior_error_class:
            consecutive += 1
        else:
            consecutive = 1 if current_error_class else 0

        self.logger.info(
            "[Iteration %d] error_class=%r  prior_error_class=%r  consecutive_same=%d  stuck_threshold=%d",
            iteration, current_error_class, prior_error_class, consecutive, STUCK_THRESHOLD,
        )

        # Accumulate full history so the LLM sees every past attempt
        prior_feedback = state.get("feedback_summary") or ""
        feedback_parts: List[str] = []
        if prior_feedback:
            feedback_parts.append(
                "PRIOR ITERATION HISTORY (already attempted — do NOT repeat these mistakes):\n"
                + prior_feedback
            )
        if raw_error:
            feedback_parts.append(
                "THIS ITERATION'S EXACT ERROR (ground truth — fix THIS now):\n" + raw_error
            )
        if state.get("executor_summary"):
            feedback_parts.append(
                "AI-generated summary (may be inaccurate; exact error above takes priority):\n"
                + state["executor_summary"]
            )

        feedback = "\n\n".join(feedback_parts) or (
            f"Execution failed on iteration {iteration}. "
            "Review the error output and fix the generated files."
        )
        self.logger.info(
            "[Iteration %d] Feedback for next iteration: %s",
            iteration,
            feedback[:400] + "..." if len(feedback) > 400 else feedback,
        )

        # ── Exhausted iterations ──────────────────────────────────────────────
        if iteration >= max_iter:
            self._banner(f"✗ PIPELINE EXHAUSTED {max_iter} ITERATIONS WITHOUT SUCCESS", char="═")
            summary = (
                f"Action '{state['action'].get('actionName')}' could not be completed after "
                f"{iteration} iteration(s).\n\n"
                f"{state.get('executor_summary') or '(No AI summary generated)'}"
            )
            return {
                "records": records,
                "final_status": "failed",
                "final_summary": summary,
                "feedback_summary": feedback,
                "consecutive_same_error": consecutive,
                "last_error_class": current_error_class,
            }

        # ── Stuck loop → trigger mid-run HITL ────────────────────────────────
        if consecutive >= STUCK_THRESHOLD:
            self.logger.warning(
                "[Iteration %d] STUCK DETECTED — same error '%s' repeated %d times. "
                "Escalating to mid-run HITL.",
                iteration, current_error_class, consecutive,
            )
            return {
                "records": records,
                "final_status": "stuck",        # internal status — routes to mid_run_hitl
                "feedback_summary": feedback,
                "consecutive_same_error": consecutive,
                "last_error_class": current_error_class,
            }

        # ── Normal retry ──────────────────────────────────────────────────────
        return {
            "records": records,
            "iteration": iteration + 1,
            "feedback_summary": feedback,
            "final_status": "in_progress",
            "consecutive_same_error": consecutive,
            "last_error_class": current_error_class,
        }

    def _mid_run_hitl_node(self, state: AgentState) -> dict:
        """
        Pause the pipeline mid-execution and ask the user for guidance when the
        agent is stuck (same error repeated >= STUCK_THRESHOLD times).

        The user's answers are prepended to feedback_summary so the generator
        sees them as high-priority instructions on the next iteration.
        """
        action = state["action"]
        feedback = state.get("feedback_summary") or ""
        iteration = state.get("iteration", 1)
        max_iter = state.get("max_iterations", MAX_ITERATIONS)

        # Extract the repeated error for display
        error_snippet = ""
        for r in reversed(state.get("execution_results") or []):
            if not r.get("success"):
                raw = re.sub(r"\x1b\[[0-9;]*m", "", r.get("stderr", "") or "")
                
                lines = []
                for ln in raw.splitlines():
                    # Strip Terraform box drawing characters without discarding the line
                    cleaned = re.sub(r"[╷╵│─╭╮╰╯]+", "", ln).strip()
                    if cleaned:
                        lines.append(cleaned)
                        
                error_snippet = "\n".join(lines)[:600]
                break

        questions = [
            f"The agent is stuck on iteration {iteration}/{max_iter} for action "
            f"'{action.get('actionName')}'. "
            f"The same error keeps repeating:\n\n{error_snippet}\n\n"
            "Please tell me how to fix this, or provide any missing information "
            "(e.g. correct AMI ID, correct region, specific resource name, IAM role ARN, etc.).",
        ]

        self.logger.warning("MID-RUN HITL: pausing pipeline, asking user for guidance")
        self.logger.warning("Error context: %s", error_snippet[:200])

        user_guidance = interrupt(questions)
        guidance_list = user_guidance if isinstance(user_guidance, list) else [user_guidance]
        guidance_text = "\n".join(str(g) for g in guidance_list)

        # Prepend user guidance as highest-priority instruction
        updated_feedback = (
            f"USER GUIDANCE (HIGHEST PRIORITY — apply this immediately):\n{guidance_text}\n\n"
            + feedback
        )

        self.logger.info("MID-RUN HITL: received user guidance, resuming pipeline")
        return {
            "feedback_summary": updated_feedback,
            "final_status": "in_progress",
            "iteration": state.get("iteration", 1) + 1,
            "consecutive_same_error": 0,   # reset after human input
            "last_error_class": "",        # reset so next error is compared fresh
        }

    def _save_memory_node(self, state: AgentState) -> dict:
        """
        Persist a run summary to AgentMemory after the pipeline finishes.
        Runs on both success and failure paths.
        """
        action = state["action"]
        records = state.get("records") or []
        results = state.get("execution_results") or []
        final_status = state.get("final_status", "unknown")
        iteration = state.get("iteration", 1)

        # Ask the LLM to distil a one-sentence lesson from this run
        lesson = ""
        try:
            errors_seen = []
            for r in results:
                if not r.get("success") and r.get("stderr"):
                    raw = re.sub(r"\x1b\[[0-9;]*m", "", r["stderr"])
                    errors_seen.append(raw[:300])

            if errors_seen or final_status == "success":
                lesson_prompt = (
                    f"Action: {action.get('actionName')}\n"
                    f"Final status: {final_status}\n"
                    f"Errors encountered: {json.dumps(errors_seen[:3])}\n\n"
                    "In ONE short sentence, state the most important lesson learned from this run "
                    "that would help avoid the same problem next time. "
                    "If successful, note what approach worked. "
                    "Be specific (e.g. 'Always use aws_ami data source — hardcoded AMI IDs are "
                    "region-specific and will fail in other regions.')."
                )
                resp = self.Llm.invoke([HumanMessage(content=lesson_prompt)])
                lesson = resp.content.strip()
                self.logger.info("memory.lesson: %s", lesson)
        except Exception as exc:
            self.logger.warning("memory.lesson_generation_failed: %s", exc)

        self.Memory.record_run(
            action_name=action.get("actionName", "unknown"),
            iterations_used=iteration,
            final_status=final_status,
            execution_results=results,
            records=records,
            lesson=lesson,
        )
        self.logger.info("memory.run_recorded  status=%s  lesson=%r", final_status, lesson[:80] if lesson else "")
        return {}

    # ── routing ───────────────────────────────────────────────────────────────

    @staticmethod
    def _route_after_analysis(state: AgentState) -> str:
        if state["analysis"]["needs_clarification"]:
            return "hitl"
        return "generate"

    @staticmethod
    def _route_after_record(state: AgentState) -> str:
        status = state.get("final_status")
        if status == "success":
            return "save_memory_success"
        if status == "failed":
            return "save_memory_fail"
        if status == "stuck":
            return "mid_run_hitl"
        return "retry"

    # ── graph construction ────────────────────────────────────────────────────

    def _build_graph(self):
        builder = StateGraph(AgentState)

        # generator nodes
        builder.add_node("read_existing", self._read_existing_node)
        builder.add_node("read_reference", self._read_reference_node)
        builder.add_node("analyze", self._analyze_node)
        builder.add_node("hitl", self._hitl_node)
        builder.add_node("generate", self._generate_node)
        builder.add_node("write_files", self._write_files_node)

        # executor nodes
        builder.add_node("scan_folder", self._scan_folder_node)
        builder.add_node("plan", self._plan_node)
        builder.add_node("execute", self._execute_node)
        builder.add_node("report", self._report_node)

        # orchestration nodes
        builder.add_node("record_iteration", self._record_iteration_node)
        builder.add_node("mid_run_hitl", self._mid_run_hitl_node)   # stuck-loop escape hatch
        builder.add_node("save_memory_success", self._save_memory_node)
        builder.add_node("save_memory_fail", self._save_memory_node)

        builder.set_entry_point("read_existing")
        builder.add_edge("read_existing", "read_reference")
        builder.add_edge("read_reference", "analyze")
        builder.add_conditional_edges(
            "analyze",
            self._route_after_analysis,
            {"hitl": "hitl", "generate": "generate"},
        )
        builder.add_edge("hitl", "generate")
        builder.add_edge("generate", "write_files")
        builder.add_edge("write_files", "scan_folder")
        builder.add_edge("scan_folder", "plan")
        builder.add_edge("plan", "execute")
        builder.add_edge("execute", "report")
        builder.add_edge("report", "record_iteration")
        builder.add_conditional_edges(
            "record_iteration",
            self._route_after_record,
            {
                "save_memory_success": "save_memory_success",
                "save_memory_fail": "save_memory_fail",
                "mid_run_hitl": "mid_run_hitl",
                "retry": "read_existing",
            },
        )
        # After saving memory → END
        builder.add_edge("save_memory_success", END)
        builder.add_edge("save_memory_fail", END)
        # After mid-run HITL the user provides guidance → resume from generate
        # (files are already written; skip analysis to avoid re-triggering HITL)
        builder.add_edge("mid_run_hitl", "generate")

        return builder.compile(checkpointer=self.Checkpointer)

    # ── public API ────────────────────────────────────────────────────────────

    def RunPipeline(
        self,
        action: Dict[str, Any],
        sandbox_path: Optional[str] = None,
        reference_folder: Optional[str] = None,
        thread_id: Optional[str] = None,
        answers: Optional[List[str]] = None,
        command_timeout: int = DEFAULT_COMMAND_TIMEOUT,
    ) -> PipelineResponse:
        """
        Run the generate → execute → (retry on failure) loop until success or
        max_iterations is exhausted, all within a single agent / single graph.

        Pass ``answers`` (non-empty list) together with the original ``thread_id``
        to resume either a pre-execution clarification pause OR a mid-run HITL pause.
        Both pause types return statusCode=202 / status='needs_clarification'.
        """
        tid = thread_id or str(uuid.uuid4())

        # FIX-A7: resuming without the original thread_id silently creates a new
        # checkpoint context with no history, causing an opaque LangGraph error.
        if answers and not thread_id:
            raise ValueError(
                "thread_id is required when resuming with answers. "
                "Pass the thread_id returned from the original RunPipeline call "
                "that returned status='needs_clarification'."
            )

        config = {"configurable": {"thread_id": tid}}

        # Load cross-run memory context once per pipeline call
        memory_ctx = self.Memory.context_for_action(action.get("actionName", ""))

        self.logger.info("")
        self._banner("UNIFIED AGENT PIPELINE STARTED")
        self.logger.info("Thread ID        : %s", tid)
        self.logger.info("Action           : %s", action.get("actionName"))
        self.logger.info("Description      : %s", action.get("actionDescription", "None"))
        self.logger.info("Service          : %s", action.get("service", "None"))
        self.logger.info("KRA Code         : %s", action.get("kraCode", "None"))
        self.logger.info("Priority         : %s", action.get("priorityLevel", "None"))
        self.logger.info("Reference Folder : %s", reference_folder or "None")
        self.logger.info("Max Iterations   : %d", self.max_iterations)
        self.logger.info("Command Timeout  : %ds per command", command_timeout)
        self.logger.info("Memory entries   : %d total runs loaded", len(self.Memory.runs))
        steps = action.get("steps", [])
        if steps:
            self.logger.info("Steps            : %d provided", len(steps))
            for i, step in enumerate(steps, 1):
                self.logger.info("  [%d] %s", i, step)
        self.logger.info("")

        try:
            if answers:
                self.Graph.invoke(Command(resume=answers), config=config)
            else:
                self.Graph.invoke(
                    {
                        "action": action,
                        "reference_folder": reference_folder or "",
                        "command_timeout": command_timeout,
                        "max_iterations": self.max_iterations,
                        "iteration": 1,
                        "records": [],
                        "feedback_summary": "",
                        "memory_context": memory_ctx,
                        "consecutive_same_error": 0,
                        "last_error_class": "",   # FIX-5: initialise new state field

                        "analysis": None,
                        "clarification": None,
                        "generated_files": [],
                        "executable_steps": [],
                        "sandbox_path": "",
                        "existing_files": [],
                        "reference_files": [],
                        "input_sandbox_path": sandbox_path or "",
                        "generator_summary": "",

                        "folder_contents": None,
                        "execution_plan": None,
                        "execution_results": [],
                        "success": False,
                        "executor_summary": "",

                        "final_status": "in_progress",
                        "final_summary": "",
                    },
                    config=config,
                )

            snapshot = self.Graph.get_state(config)

            # HITL pause — covers both pre-execution clarification AND mid-run stuck HITL
            if snapshot.tasks and any(t.interrupts for t in snapshot.tasks):
                questions = snapshot.tasks[0].interrupts[0].value
                is_mid_run = snapshot.values.get("consecutive_same_error", 0) >= STUCK_THRESHOLD
                summary_msg = (
                    f"Agent is stuck and needs your input (thread_id={tid}). "
                    f"Re-call RunPipeline with answers=['your guidance'] and thread_id='{tid}'."
                    if is_mid_run else
                    f"Agent needs clarification before starting (thread_id={tid}). "
                    f"Re-call RunPipeline with answers=[...] and thread_id='{tid}'."
                )
                return PipelineResponse(
                    statusCode=202,
                    status="needs_clarification",
                    thread_id=tid,
                    sandbox_path=snapshot.values.get("sandbox_path") or None,
                    iterations_used=snapshot.values.get("iteration", 1),
                    iterations=[
                        IterationRecord(**r) for r in snapshot.values.get("records", [])
                    ],
                    questions=questions,
                    summary=summary_msg,
                )

            final = snapshot.values
            records = [IterationRecord(**r) for r in final.get("records", [])]
            results = [ExecutionResult(**r) for r in final.get("execution_results", [])]
            final_status = final.get("final_status", "failed")
            sandbox_path_final: Optional[str] = final.get("sandbox_path") or None
            final_summary_text = final.get("final_summary") or final.get("executor_summary")

            jira_url = action.get("jiraUrl")
            if jira_url and final_summary_text:
                try:
                    issue_key = jira_url.rstrip("/").split("/")[-1]
                    add_summary_comment(issue_key, final_summary_text)
                    self.logger.info("Added final summary comment to Jira ticket %s", issue_key)
                    
                    target_status = "Done" if final_status == "success" else "Backlog"
                    update_ticket_status(issue_key, target_status)
                    self.logger.info("Transitioned Jira ticket %s to %s", issue_key, target_status)
                except Exception as e:
                    self.logger.warning("Failed to update Jira with final summary and status: %s", e)

            if final_status == "success":
                # Sandbox intentionally kept: contains terraform.tfstate and .pem key
                return PipelineResponse(
                    statusCode=200,
                    status="success",
                    thread_id=tid,
                    sandbox_path=sandbox_path_final,
                    iterations_used=final.get("iteration", len(records)),
                    iterations=records,
                    execution_results=results,
                    summary=final_summary_text,
                )

            # failed / exhausted
            self._cleanup_sandbox(sandbox_path_final)
            return PipelineResponse(
                statusCode=207,
                status="failed",
                thread_id=tid,
                sandbox_path=sandbox_path_final,
                iterations_used=final.get("iteration", len(records)),
                iterations=records,
                execution_results=results,
                summary=final_summary_text,
            )

        except BaseException as exc:
            self.logger.exception("RunPipeline error or interrupt: %s", exc)
            # FIX-A13: attempt to clean up the sandbox if one was created before the crash/stop.
            try:
                snapshot = self.Graph.get_state(config)
                orphaned_sandbox = snapshot.values.get("sandbox_path") if snapshot.values else None
                if orphaned_sandbox:
                    self._cleanup_sandbox(orphaned_sandbox)
            except Exception:
                pass  # best-effort only; don't mask the original exception
            
            # If it's a SystemExit or KeyboardInterrupt, we MUST re-raise it so the thread dies properly
            if isinstance(exc, (SystemExit, KeyboardInterrupt, InterruptedError)):
                raise

            return PipelineResponse(
                statusCode=500,
                status="error",
                exception=str(exc),
                thread_id=tid,
            )


# ── Entry point ────────────────────────────────────────────────────────────────

# if __name__ == "__main__":
#     # Ensure stdout uses UTF-8 on Windows (default cp1252 can't encode
#     # Terraform box-drawing characters like ╷ that may appear in stored feedback).
#     if hasattr(sys.stdout, "reconfigure"):
#         sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

#     agent = ExecutionAgents(max_iterations=6)

#     action_payload = {
#         "actionName": "Deploy EC2 Instance with Terraform",
#         "actionDescription": (
#             "Deploy a t2.micro EC2 instance using Terraform to satisfy KRA-05. The us-east-1 region has active monitoring and is the primary compute zone, making it the ideal deployment target."
#         ),
#         "jiraUrl": "https://dummyintelligenzit.atlassian.net/browse/DEV-422",  # Replace with actual Jira URL
#         "steps": [
#             "1. Create a new Terraform configuration file (main.tf) defining a t2.micro EC2 instance with default AMI, key pair, and VPC.",
#             "2. Configure AWS provider in Terraform to use us-east-1 region and AWS credentials with EC2 and IAM permissions.",
#             "3. Run 'terraform init' to initialize the backend and providers.",
#             "4. Run 'terraform plan' to validate the configuration and review the execution plan.",
#             "5. Run 'terraform apply' to deploy the EC2 instance.",
#             "6. Verify instance creation using 'aws ec2 describe-instances --filters Name=instance-type,Values=t2.micro' and confirm it appears in CloudWatch metrics."
#         ],
#     }
#     response = agent.RunPipeline(
#         action=action_payload,
#         reference_folder="",
#         command_timeout=180,
#     )
#     print("Pipeline Response:", json.dumps(response.model_dump(), indent=2))