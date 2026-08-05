"""
AWS Unified Agent
=================
Single-agent, self-healing AWS automation pipeline.

Combines the previously separate Generator, Executor, and Orchestrator
agents into one LangGraph state machine with a single shared AgentState.

Flow per iteration:
    read_existing -> read_reference -> analyze -> [hitl?] -> generate
        -> write_files -> validate -> scan_folder -> plan -> plan_review -> execute -> report -> route

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

import asyncio
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
from src.chandra.config import settings as chandra_settings
from src.chandra.execution.bridge import BridgeResult, plan_and_execute
from src.chandra.llm import build_chat_model
from langchain_community.agent_toolkits import FileManagementToolkit
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field
from tools.jira_tools.create_jira_ticket import add_summary_comment, update_ticket_status
from src.chandra.digital_worker.tracker import add_comment_to_issue

load_dotenv(override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("ExecutionAgents")

MAX_ITERATIONS = 6
DEFAULT_COMMAND_TIMEOUT = 500
STUCK_THRESHOLD = 3
VALIDATE_MAX_RETRIES = 3
PLAN_REVIEW_MAX_RETRIES = 2

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

    MAX_RUNS = 50
    MAX_ERRORS_PER_RUN = 5

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
        _ANSI_AND_BOX = re.compile(r"\x1b\[[0-9;]*m|[╷│╵]")

        def _clean(text: str) -> str:
            return _ANSI_AND_BOX.sub("", text)

        errors: List[str] = []
        for r in execution_results:
            if not r.get("success") and r.get("stderr"):
                raw = _clean(r["stderr"])
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

        fixes: List[str] = []
        for rec in records:
            summary = rec.get("executor_summary") or rec.get("feedback_used") or ""
            if summary:
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

        same = [r for r in runs if r.get("action_name", "") == action_name]
        other = [r for r in runs if r.get("action_name", "") != action_name]
        n_other = max(0, max_relevant - len(same))
        relevant = (same[-max_relevant:] + (other[-n_other:] if n_other else []))[-max_relevant:]

        if not relevant:
            return ""

        lines = ["AGENT MEMORY — past pipeline runs (most recent last):"]
        for r in relevant:
            status_icon = "OK" if r["final_status"] == "success" else "X"
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

# ── Singleton checkpointer ─────────────────────────────────────────────────────
# CRITICAL: This MUST be a module-level singleton.
# Each HTTP request creates a new ExecutionAgents instance. If each instance
# had its own checkpointer, the pause state saved by request-1 would be in
# Memory A, which is garbage-collected when that request ends. Request-2
# (resume) would create empty Memory B and fail with KeyError: 'action'.
# A singleton means ALL instances share the same store, so pause → resume works.
_SHARED_CHECKPOINTER: Any = None
_SHARED_CHECKPOINTER_LOCK = __import__("threading").Lock()


def _get_shared_checkpointer() -> Any:
    """Return (and lazily create) the process-wide singleton checkpointer."""
    global _SHARED_CHECKPOINTER
    if _SHARED_CHECKPOINTER is not None:
        return _SHARED_CHECKPOINTER
    with _SHARED_CHECKPOINTER_LOCK:
        if _SHARED_CHECKPOINTER is None:
            _SHARED_CHECKPOINTER = _build_checkpointer()
    return _SHARED_CHECKPOINTER


def _build_checkpointer() -> Any:
    """Return a checkpointer using a three-tier fallback strategy.

    Tier 1: Postgres  (production)
    Tier 2: SQLite    (local disk, 'database/' folder)
    Tier 3: MemorySaver (in-process fallback)
    """
    try:
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg_pool import ConnectionPool
        import psycopg
        import atexit

        conn_string = os.getenv("POSTGRES_URL", "")
        if conn_string:
            if conn_string.startswith("postgresql+psycopg://"):
                conn_string = conn_string.replace("postgresql+psycopg://", "postgresql://", 1)
            with psycopg.connect(conn_string, autocommit=True) as conn:
                PostgresSaver(conn).setup()
            pool = ConnectionPool(conn_string, max_size=10)
            atexit.register(pool.close)
            checkpointer = PostgresSaver(pool)
            logger.info("checkpointer.postgres_setup_success")
            return checkpointer
        logger.warning("checkpointer.postgres_url_missing")
    except ImportError:
        logger.warning("checkpointer.postgres_unavailable")
    except Exception as exc:
        logger.warning("checkpointer.postgres_setup_failed", exc_info=exc)

    try:
        import sqlite3
        from langgraph.checkpoint.sqlite import SqliteSaver

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

    logger.warning("checkpointer.fallback_to_memory_saver")
    return MemorySaver()

class ActionAnalysis(BaseModel):
    aws_services_involved: List[str] = Field(
        default_factory=list,
        description="List of AWS service names involved in this action (e.g., ['ec2', 'lambda', 'ecs', 'apigateway', 's3']). Use common short names.",
    )
    expected_resources: List[str] = Field(
        default_factory=list,
        description="List of exact Terraform resource types expected to be used (e.g., ['aws_instance', 'aws_lambda_function', 'aws_api_gateway_rest_api']). Must not be empty.",
    )
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
    aws_discovery_commands: List[str] = Field(
        default_factory=list,
        description=(
            "List of safe READ-ONLY AWS CLI commands (describe-*, list-*, get-*) needed to "
            "fetch the current account state relevant to this action. The backend will execute "
            "these commands and feed the results into the Terraform generator as grounding context. "
            "Always include 'aws sts get-caller-identity' and 'aws ec2 describe-availability-zones'. "
            "Only include commands relevant to THIS action. Examples: "
            "S3 action -> ['aws s3api list-buckets', 'aws kms list-aliases']. "
            "EC2 action -> ['aws ec2 describe-vpcs', 'aws ec2 describe-subnets', "
            "'aws ec2 describe-key-pairs', 'aws ec2 describe-security-groups']. "
            "Lambda action -> ['aws lambda list-functions', 'aws iam list-roles']. "
            "RDS action -> ['aws rds describe-db-instances', 'aws rds describe-db-subnet-groups', "
            "'aws ec2 describe-vpcs', 'aws ec2 describe-subnets', 'aws ec2 describe-security-groups']. "
            "NEVER include commands that create, modify, or delete resources (e.g. no 'aws s3 rm', "
            "no 'aws ec2 terminate-instances')."
        ),
    )

class GeneratedFile(BaseModel):
    filename: str
    content: str
    file_type: str
    description: Optional[str] = None

class ExecutableStep(BaseModel):
    description: str
    command: str

class CodeGenerationResult(BaseModel):
    files: List[GeneratedFile]
    executableSteps: List[ExecutableStep]
    summary: Optional[str] = Field(default="")

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
    executor_summary: Optional[str] = None
    sandbox_path: Optional[str] = None

class PlanReview(BaseModel):
    matches_intent: bool = Field(
        description="True if the plan's resource changes plausibly match the requested action"
    )
    destroy_or_replace_detected: bool = Field(
        description="True if the plan contains any unexpected destroy or replace of a resource"
    )
    concerns: List[str] = Field(
        default_factory=list,
        description="Short list of specific concerns, if any (empty if plan looks correct)",
    )
    reasoning: str = Field(description="One or two sentence justification")

class PermissionCheck(BaseModel):
    is_authorized: bool = Field(description="True if the provided policies cover all required actions for the expected resources.")
    missing_permissions: List[str] = Field(description="List of specific permissions that appear to be missing. Leave empty if is_authorized is True.")
    reasoning: str = Field(description="Brief explanation of the evaluation.")

class AgentState(TypedDict):
    action: Dict[str, Any]
    reference_folder: str
    command_timeout: int
    max_iterations: int
    aws_permissions: List[str]
    iteration: int
    records: List[Dict]
    feedback_summary: str
    memory_context: str
    consecutive_same_error: int
    last_error_class: str
    aws_context: str
    terraform_state_context: str
    service_quotas_context: str
    terraform_docs: str
    terraform_docs_dict: Dict[str, str]
    permission_issues: List[str]
    caller_arn: str
    timings: Dict[str, float]

    analysis: Optional[Dict]
    clarification: Optional[Dict]
    generated_files: List[Dict]
    executable_steps: List[Dict]
    sandbox_path: str
    existing_files: List[Dict]
    reference_files: List[Dict]
    input_sandbox_path: str
    generator_summary: str

    validate_iteration: int
    validate_passed: bool
    validate_feedback: str

    folder_contents: Optional[str]
    execution_plan: Optional[Dict]
    execution_results: List[Dict]
    success: bool
    executor_summary: str
    approval_rejected: bool

    pre_apply_results: List[Dict]
    plan_review: Optional[Dict]
    plan_review_iteration: int
    plan_review_precheck_failed: bool
    plan_review_skipped: bool
    plan_review_issue: str

    final_status: str
    final_summary: str

    # ── User-defined KRA payload (compact, replaces MCP docs) ──
    kra_data: Optional[Dict] = None


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
    hitl_payload: Optional[Dict[str, Any]] = None

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
    
    # Resolve Terraform via explicit env var or fallback to shutil.which
    tf_path = os.environ.get("TERRAFORM_BIN_DIR")
    if not tf_path:
        import shutil
        tf_exe = shutil.which("terraform")
        if tf_exe:
            tf_path = os.path.dirname(tf_exe)
    if tf_path:
        proc_env["PATH"] = proc_env.get("PATH", "") + os.pathsep + tf_path
        
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
        # Without this, the child inherits THIS process's process group on
        # POSIX, so os.killpg(os.getpgid(proc.pid), SIGKILL) on timeout/cancel
        # would signal the whole group — including this agent process itself
        # — instead of just the hung/cancelled shell command. Giving the
        # child its own session scopes killpg to just it and its descendants.
        # No-op on Windows (that branch uses taskkill /T instead).
        start_new_session=True,
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


class _PersistentMCPSession:
    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._start_lock = threading.Lock()
        self._client = None
        self._init_lock: Optional[asyncio.Lock] = None  # bound to the loop, created lazily
        self._aws_tool = None
        self._tf_search_tool = None
        self._tf_details_tool = None

    def _ensure_loop_started(self) -> None:
        if self._loop is not None:
            return
        with self._start_lock:
            if self._loop is not None:
                return
            loop = asyncio.ProactorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()

            def _run_forever():
                asyncio.set_event_loop(loop)
                loop.run_forever()

            t = threading.Thread(target=_run_forever, daemon=True, name="mcp-session-loop")
            t.start()
            self._loop = loop
            self._thread = t

    async def _init_servers(self) -> None:
        """First coroutine on the loop launches BOTH MCP servers at once
        (concurrently, via asyncio.gather); every later coroutine on the same
        loop just reads the cached tool handles — no re-init."""
        if self._init_lock is None:
            self._init_lock = asyncio.Lock()

        async with self._init_lock:
            if self._aws_tool is not None or self._tf_search_tool is not None:
                return  # already initialised by a previous call

            from langchain_mcp_adapters.client import MultiServerMCPClient

            region = os.getenv("AWS_DEFAULT_REGION") or os.getenv("AWS_REGION") or "us-east-1"
            _script_dir = os.path.dirname(os.path.abspath(__file__))
            terraform_binary = os.getenv(
                "TERRAFORM_MCP_BINARY",
                os.path.join(os.path.dirname(_script_dir), "terraform", "terraform-mcp-server.exe"),
            )

            server_config = {
                "aws_api": {
                    "command": "uvx",
                    "args": ["awslabs.aws-api-mcp-server@latest"],
                    "env": {
                        "AWS_REGION": os.getenv("AWS_REGION", region),
                        "AWS_DEFAULT_REGION": os.getenv("AWS_DEFAULT_REGION", region),
                        **({"AWS_ACCESS_KEY_ID": os.getenv("AWS_ACCESS_KEY_ID")} if os.getenv("AWS_ACCESS_KEY_ID") else {}),
                        **({"AWS_SECRET_ACCESS_KEY": os.getenv("AWS_SECRET_ACCESS_KEY")} if os.getenv("AWS_SECRET_ACCESS_KEY") else {}),
                        **({"AWS_SESSION_TOKEN": os.getenv("AWS_SESSION_TOKEN")} if os.getenv("AWS_SESSION_TOKEN") else {}),
                        **({"AWS_PROFILE": os.getenv("AWS_PROFILE")} if os.getenv("AWS_PROFILE") else {}),
                        "UV_LINK_MODE": "copy",
                    },
                    "transport": "stdio",
                },
                "terraform": {
                    "command": terraform_binary,
                    "args": ["stdio"],
                    "env": {
                        "PATH": os.environ.get("PATH", ""),
                        "TF_CLI_CONFIG_FILE": "",
                    },
                    "transport": "stdio",
                },
            }

            logger.info(
                "[MCP SESSION] Cold start: launching aws_api + terraform MCP servers together "
                "(first call in this process only)..."
            )
            t0 = time.perf_counter()
            self._client = MultiServerMCPClient(server_config)

            # ---- initialize both MCP servers AT ONCE, concurrently ----
            aws_tools, tf_tools = await asyncio.gather(
                self._client.get_tools(server_name="aws_api"),
                self._client.get_tools(server_name="terraform"),
                return_exceptions=True,
            )

            if isinstance(aws_tools, Exception):
                logger.warning("[MCP SESSION] aws_api server failed to start: %s", aws_tools)
                aws_tools = []
            if isinstance(tf_tools, Exception):
                logger.warning("[MCP SESSION] terraform server failed to start: %s", tf_tools)
                tf_tools = []

            self._aws_tool = next((t for t in aws_tools if t.name == "call_aws"), None)
            self._tf_search_tool = next((t for t in tf_tools if t.name == "search_providers"), None)
            self._tf_details_tool = next((t for t in tf_tools if t.name == "get_provider_details"), None)

            logger.info(
                "[MCP SESSION] Ready in %.2fs (aws_api=%s, terraform=%s) — reused for every "
                "future MCP call in this process, no more per-call subprocess startup.",
                time.perf_counter() - t0,
                "up" if self._aws_tool else "unavailable",
                "up" if (self._tf_search_tool and self._tf_details_tool) else "unavailable",
            )

    async def get_tools_async(self) -> Dict[str, Any]:
        """For callers already running inside the persistent loop."""
        await self._init_servers()
        return {
            "aws_tool": self._aws_tool,
            "tf_search_tool": self._tf_search_tool,
            "tf_details_tool": self._tf_details_tool,
        }

    def run_coro(self, coro_fn, *args, **kwargs):
        """Submit a coroutine to the persistent loop from any (sync) calling
        thread and block until it completes."""
        self._ensure_loop_started()
        fut = asyncio.run_coroutine_threadsafe(coro_fn(*args, **kwargs), self._loop)
        return fut.result()


_mcp_session = _PersistentMCPSession()

# once for one job/iteration is never re-fetched for another.
_TERRAFORM_DOCS_CACHE: Dict[str, str] = {}

# Process-wide cache: the terraform_docs context is fetched from MCP exactly
# ONCE for the life of the process. Every later call to gather_docs — next
# retry iteration, next job — reuses that first result verbatim, with zero
# MCP calls, even if the resource list differs on a later iteration.
_TERRAFORM_DOCS_CONTEXT_CACHE: Dict[str, Optional[str]] = {"value": None}
_TERRAFORM_DOCS_CONTEXT_LOCK = threading.Lock()


def _parse_call_aws_response(res, command: str, log: logging.Logger = logger, debug: bool = False):
    """Unwraps the nested text/JSON envelope the aws_api MCP server wraps its
    `call_aws` responses in. Returns a dict on success, or None on a parse miss."""
    if debug:
        log.debug("[MCP] %s RAW: %r", command, res)

    inner_text = res if isinstance(res, str) else json.dumps(res)
    try:
        parsed = json.loads(inner_text)
        if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict) and "text" in parsed[0]:
            inner_text = parsed[0]["text"]
        elif isinstance(parsed, dict) and "text" in parsed:
            inner_text = parsed["text"]
    except Exception:
        pass

    try:
        parsed_text = json.loads(inner_text)
        if isinstance(parsed_text, list) and parsed_text and "response" in parsed_text[0]:
            as_json = parsed_text[0]["response"].get("as_json")
            if as_json:
                return json.loads(as_json)
    except Exception:
        pass

    log.warning("[MCP] Could not parse call_aws response for: %s", command)
    return None


async def _mcp_run_aws_command_async(command: str, log: logging.Logger = logger) -> dict:
    """Runs a single `call_aws` invocation against the persistent,
    already-connected aws_api tool handle. No client creation here."""
    check_cancelled()
    tools = await _mcp_session.get_tools_async()
    aws_tool = tools["aws_tool"]
    if aws_tool is None:
        log.warning("[MCP] aws_api 'call_aws' tool unavailable for '%s'", command)
        return {}
    try:
        res = await aws_tool.ainvoke({"cli_command": command})
    except Exception as exc:
        log.warning("MCP AWS CLI execution failed for '%s': %s", command, exc)
        return {}
    parsed = _parse_call_aws_response(res, command, log=log)
    return parsed if parsed is not None else {}


async def _mcp_run_aws_commands_parallel_async(commands: List[str], log: logging.Logger = logger) -> Dict[str, dict]:
    """Fires many independent `call_aws` commands concurrently against the one
    persistent, already-connected aws_tool handle. Returns {command: parsed}."""
    tools = await _mcp_session.get_tools_async()
    aws_tool = tools["aws_tool"]
    if aws_tool is None:
        log.warning("[MCP] aws_api 'call_aws' tool unavailable — skipping %d command(s)", len(commands))
        return {cmd: {} for cmd in commands}

    async def _one(cmd: str):
        try:
            res = await aws_tool.ainvoke({"cli_command": cmd})
            parsed = _parse_call_aws_response(res, cmd, log=log)
            return cmd, (parsed if parsed is not None else {})
        except Exception as exc:
            log.warning("MCP AWS CLI execution failed for '%s': %s", cmd, exc)
            return cmd, {}

    pairs = await asyncio.gather(*[_one(c) for c in commands])
    return dict(pairs)


async def _mcp_fetch_terraform_docs_async(
    provider_name: str,
    provider_namespace: str,
    service_slug: str,
    provider_document_type: str,
    log: logging.Logger = logger,
) -> str:
    """Looks up a Terraform provider resource's argument-reference docs using
    the persistent, already-connected terraform tool handles."""
    check_cancelled()
    tools = await _mcp_session.get_tools_async()
    search_tool = tools["tf_search_tool"]
    details_tool = tools["tf_details_tool"]
    if not (search_tool and details_tool):
        log.warning("[MCP] terraform docs tools unavailable for '%s'", service_slug)
        return ""

    try:
        search_args = {
            "provider_name": provider_name,
            "provider_namespace": provider_namespace,
            "service_slug": service_slug,
            "provider_document_type": provider_document_type,
        }
        search_raw = await search_tool.ainvoke(search_args)
        search_text = search_raw if isinstance(search_raw, str) else json.dumps(search_raw, default=str)

        inner_text = search_text
        try:
            parsed = json.loads(search_text)
            if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict) and "text" in parsed[0]:
                inner_text = parsed[0]["text"]
            elif isinstance(parsed, dict) and "text" in parsed:
                inner_text = parsed["text"]
        except Exception:
            pass

        match = re.search(r"providerDocID:\s*(\d+)", inner_text)
        if not match:
            return ""
        provider_doc_id = match.group(1)
        details_raw = await details_tool.ainvoke({"provider_doc_id": provider_doc_id})
        if isinstance(details_raw, list) and details_raw and isinstance(details_raw[0], dict) and "text" in details_raw[0]:
            return details_raw[0]["text"]
        return details_raw if isinstance(details_raw, str) else json.dumps(details_raw, default=str)
    except Exception as exc:
        log.warning("MCP Terraform docs failed for '%s': %s", service_slug, exc)
        return ""






# ── Terraform Golden Rules (generic best practices, not service-specific) ──────
_TERRAFORM_GOLDEN_RULES = (
    "\nIf an ID is provided in the context above (VPC, Subnets, Security Groups, "
    "Key Pairs, Route53 Zones, AMI, RDS Subnet Groups, Elastic IPs, NAT Gateways, "
    "ACM Certs, KMS Aliases), hardcode it directly in your Terraform code to avoid "
    "data source filter errors. ONLY use Terraform data sources (e.g. data \"aws_vpc\") "
    "if the required resource is marked as '(none found)' or is missing from the context.\n"
    "\nTERRAFORM GOLDEN RULES:\n"
    "1. S3 Buckets: Names must be globally unique. Always use random_id or random_pet to append a suffix.\n"
    "2. IAM Roles/Policies: Always use name_prefix instead of name to avoid conflicts.\n"
    "3. Security Groups: Prefer using existing security groups if they match your needs.\n"
    "4. Circular Dependencies: Never make a Security Group depend on an EC2 instance's IP if the EC2 instance also depends on that Security Group.\n"
    "5. Hardcoding: Hardcode environment IDs only if provided above. NEVER hardcode ARNs or Regions.\n"
    "6. Stateful Resources: Always set lifecycle { prevent_destroy = true } for RDS/DynamoDB/S3 unless instructed otherwise.\n"
    "7. Provider Version: hashicorp/aws ~> 5.0.\n"
    "8. Local Files: use the Terraform local_file resource instead of shell commands.\n"
    "9. AMIs: pick the catalog entry matching the target OS/arch and hardcode that AMI ID. Only use a data \"aws_ami\" lookup if the AMI Catalog is empty or the OS you need isn't in it.\n"
    "10. RDS: reuse an existing DB Subnet Group if listed and spans the needed AZs.\n"
    "11. Elastic IPs: reuse an unassociated EIP if listed.\n"
    "12. HTTPS/ACM: provision with DNS validation or default to HTTP-only and flag it.\n"
    "13. Subnet CIDRs: must be non-overlapping sub-blocks of the VPC CIDR; use cidrsubnet().\n"
    "14. CloudFront + ACM: cert MUST be in us-east-1 regardless of deployment region.\n"
    "15. VPC selection: prefer [DEFAULT] VPC when ambiguous, else ask.\n"
    "16. Public vs private subnets: trust the computed PUBLIC/PRIVATE label above.\n"
    "17. Subnet IP exhaustion: check AvailableIPs before placing IP-hungry resources.\n"
    "18. Private subnet AWS service access: verify NAT Gateway or VPC Endpoint exists before placing resources there."
)

# ── Safety regex: only allow read-only AWS CLI commands ─────────────────────────
_SAFE_DISCOVERY_PATTERN = re.compile(
    r"^aws\s+\S+\s+(describe-|list-|get-)", re.IGNORECASE
)

# ── Generic identifiers to extract from AWS API JSON responses ──────────────────
_GENERIC_ID_KEYS = (
    "Name", "Id", "Arn", "VpcId", "SubnetId", "GroupId", "GroupName",
    "FunctionName", "DBInstanceIdentifier", "BucketName", "KeyName",
    "RoleName", "UserName", "DomainName", "CertificateArn", "AliasName",
    "LoadBalancerName", "TableName", "TopicArn", "QueueUrl", "ClusterName",
    "ServiceName", "HostedZoneId", "PolicyName", "StackName", "Engine",
    "DBInstanceStatus", "State", "Status", "CidrBlock", "PublicIp",
    "PrivateIpAddress", "AvailabilityZone", "InstanceId", "ImageId",
    "KeyId", "Description", "Type", "Endpoint", "Port",
    "IsDefault", "NatGatewayId", "InternetGatewayId", "RouteTableId",
    "AllocationId", "AssociationId", "AvailableIpAddressCount",
)


def _format_discovery_results(
    results: Dict[str, Any], region: str, log: logging.Logger
) -> str:
    """Generic Discovery Engine formatter.

    Converts raw AWS CLI JSON results into structured grounding text
    WITHOUT any service-specific parsing branches. Each result is rendered
    by extracting common identifier keys from the JSON objects.
    """
    lines = ["AWS ACCOUNT GROUNDING (live discovery — treat as ground truth):"]

    # ── Identity (always present from base commands) ──
    identity = results.get("aws sts get-caller-identity")
    if identity:
        lines.append(f"  Account ID : {identity.get('Account')}")
        lines.append(f"  Caller ARN : {identity.get('Arn')}")
    else:
        lines.append("  Account ID : (unavailable — could not call sts:GetCallerIdentity via MCP)")
    lines.append(f"  Region     : {region}")

    # ── AZs (always present from base commands) ──
    azs = results.get(f"aws ec2 describe-availability-zones --region {region}")
    if azs:
        az_names = [
            az["ZoneName"]
            for az in (azs.get("AvailabilityZones") or [])
            if az.get("State") == "available"
        ]
        if az_names:
            lines.append(f"  Available AZs: {', '.join(az_names)}")

    # ── Generic rendering for all other command results ──
    for cmd, result in sorted(results.items()):
        if cmd.startswith("aws sts ") or "describe-availability-zones" in cmd:
            continue  # Already handled above
        if result is None:
            continue

        # Extract the primary data key(s) — skip metadata
        data_keys = [
            k for k in result.keys()
            if k not in ("ResponseMetadata", "NextToken", "Marker", "IsTruncated", "nextToken")
        ]

        # Build a human-readable label from the command
        parts = cmd.split()
        service_label = parts[1] if len(parts) > 1 else "unknown"
        operation = parts[2] if len(parts) > 2 else ""

        for key in data_keys:
            items = result[key]
            if isinstance(items, list):
                lines.append(f"\n  {service_label} {operation} → {key} ({len(items)} found):")
                for item in items[:25]:  # Cap at 25 items per result to control prompt size
                    if isinstance(item, dict):
                        summary_parts = []
                        for id_key in _GENERIC_ID_KEYS:
                            if id_key in item:
                                val = item[id_key]
                                if isinstance(val, (str, int, float, bool)):
                                    summary_parts.append(f"{id_key}={val}")
                        if summary_parts:
                            lines.append(f"    {', '.join(summary_parts[:8])}")
                        else:
                            # Fallback: show first 4 key=value pairs
                            pairs = [
                                f"{k}={v}" for k, v in list(item.items())[:4]
                                if not isinstance(v, (dict, list))
                            ]
                            if pairs:
                                lines.append(f"    {', '.join(pairs)}")
                    elif isinstance(item, str):
                        lines.append(f"    {item}")
            elif isinstance(items, dict):
                # Some commands return a single object (e.g. DistributionList)
                sub_items = None
                for sk, sv in items.items():
                    if isinstance(sv, list):
                        sub_items = sv
                        lines.append(f"\n  {service_label} {operation} → {key}.{sk} ({len(sv)} found):")
                        for si in sv[:25]:
                            if isinstance(si, dict):
                                sp = []
                                for id_key in _GENERIC_ID_KEYS:
                                    if id_key in si:
                                        val = si[id_key]
                                        if isinstance(val, (str, int, float, bool)):
                                            sp.append(f"{id_key}={val}")
                                if sp:
                                    lines.append(f"    {', '.join(sp[:8])}")
                        break
                if sub_items is None:
                    sp = []
                    for id_key in _GENERIC_ID_KEYS:
                        if id_key in items:
                            val = items[id_key]
                            if isinstance(val, (str, int, float, bool)):
                                sp.append(f"{id_key}={val}")
                    if sp:
                        lines.append(f"  {key}: {', '.join(sp[:8])}")
            elif isinstance(items, (str, int, bool)):
                lines.append(f"  {key}: {items}")

    # Append generic Terraform golden rules
    lines.append(_TERRAFORM_GOLDEN_RULES)

    ctx_str = "\n".join(lines)
    log.info("Discovery Engine: generated grounding context (%d chars)", len(ctx_str))
    return ctx_str


async def _gather_aws_context_async(
    log: logging.Logger = logger,
    discovery_commands: Optional[List[str]] = None,
) -> str:
    """Generic Discovery Engine.

    Executes the LLM-specified discovery commands (all read-only),
    then formats the raw JSON results into structured grounding text.

    No service-specific conditionals. No keyword matching. No hardcoded
    command lists. The LLM (via ActionAnalysis.aws_discovery_commands)
    tells us what to fetch; we just execute and format.

    Safety: A regex filter ensures only read-only commands (describe-*,
    list-*, get-*) are executed. Mutating commands are blocked.
    """
    check_cancelled()
    region = os.getenv("AWS_DEFAULT_REGION") or os.getenv("AWS_REGION") or "us-east-1"

    # ── Always-run baseline commands ──
    BASE_COMMANDS = [
        "aws sts get-caller-identity",
        f"aws ec2 describe-availability-zones --region {region}",
    ]

    # Global-service commands that should NOT get --region appended
    GLOBAL_SERVICES = {"sts", "iam", "s3api", "s3", "cloudfront", "route53", "organizations"}

    commands = list(BASE_COMMANDS)
    for cmd in (discovery_commands or []):
        cmd = cmd.strip()
        if not cmd:
            continue

        # Inject --region if not present and not a global service
        if "--region" not in cmd:
            cmd_parts = cmd.split()
            svc = cmd_parts[1] if len(cmd_parts) > 1 else ""
            if svc not in GLOBAL_SERVICES:
                cmd = f"{cmd} --region {region}"

        # De-duplicate base commands
        if cmd in BASE_COMMANDS:
            continue

        # Safety: only allow read-only commands
        if not _SAFE_DISCOVERY_PATTERN.match(cmd):
            log.warning("Discovery Engine: BLOCKED unsafe command: %s", cmd)
            continue

        commands.append(cmd)

    log.info("Discovery Engine: executing %d commands (LLM requested %d)",
             len(commands), len(discovery_commands or []))

    try:
        tools = await _mcp_session.get_tools_async()
        aws_tool = tools["aws_tool"]
        if aws_tool is None:
            log.warning("Discovery Engine: aws_api 'call_aws' tool unavailable")
            return ""

        # Fire ALL commands concurrently
        results = await _mcp_run_aws_commands_parallel_async(commands, log=log)
    except Exception as exc:
        log.warning("Discovery Engine failed (fetch phase): %s", exc)
        return ""

    # Format results generically
    try:
        return _format_discovery_results(results, region, log)
    except Exception as exc:
        log.warning("Discovery Engine failed (formatting phase): %s", exc)
        return ""



_GLOBAL_DOCS_CACHE = {}
_GLOBAL_AWS_CONTEXT_CACHE = {"value": None, "time": 0}
_GLOBAL_AWS_CONTEXT_LOCK = threading.Lock()

class ExecutionAgents:

    def __init__(self, max_iterations: int = MAX_ITERATIONS, memory_path: Optional[str] = None, job_id: Optional[str] = None) -> None:
        self.max_iterations = max_iterations
        self.job_id = job_id or "default"
        self._quotas_cache = {}
        
        self._docs_cache = _GLOBAL_DOCS_CACHE
        self._aws_context_cache = _GLOBAL_AWS_CONTEXT_CACHE
        self._aws_context_cache_lock = _GLOBAL_AWS_CONTEXT_LOCK
        
        self.logger = logging.getLogger(f"ExecutionAgents.{self.job_id}")
        self.logger.propagate = True
        
        os.makedirs("logs", exist_ok=True)
        fh = logging.FileHandler(f"logs/{self.job_id}.log", mode='a', encoding='utf-8')
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s"))
        if not any(isinstance(h, logging.FileHandler) and h.baseFilename == fh.baseFilename for h in self.logger.handlers):
            self.logger.addHandler(fh)
        else:
            fh.close()  # Don't leak the FD if handler already exists
            
        self.logger.info("Initialising ExecutionAgents (max_iterations=%d, job_id=%s)", max_iterations, self.job_id)
        try:
            from src.chandra.llm import get_llm  # noqa: PLC0415
            self.Llm = get_llm()
            self.Memory = AgentMemory(memory_path)
            self.Checkpointer = _get_shared_checkpointer()
            self.Graph = self._build_graph()
            self.logger.info("ExecutionAgents initialised successfully")
        except Exception as exc:
            self.logger.exception("Failed to initialise ExecutionAgents: %s", exc)
            raise

    def _build_reasoning_model(self) -> Any:
        """Build the agent's chat model with deterministic decoding.

        Terraform / boto3 / ExecutionPlan generation must be reproducible
        and complete. Left at provider defaults, an OpenAI-compatible /
        vLLM backend samples at ``temperature=0.7`` with an unpinned output
        budget — which, on a smaller local model, materially raises
        hallucination, ABSOLUTE-RULE dropping, and truncated / malformed
        structured output. Claude on Bedrock tolerates that; a local model
        does not. Pinning low temperature + full top_p and a generous
        ``max_tokens`` makes a valid, complete plan the likely outcome
        regardless of which model serves the request, and is harmless to
        Claude (deterministic generation is desirable here either way).

        All three knobs are env-overridable so ops can tune per model
        without a code change:
          CHANDRA_AGENT_TEMPERATURE (default 0.0)
          CHANDRA_AGENT_TOP_P       (default 1.0)
          CHANDRA_AGENT_MAX_TOKENS  (default 8192 — multi-file Terraform
                                     generation needs room; too small a cap
                                     truncates the JSON mid-file)
        """

        def _env_float(name: str, default: float) -> float:
            raw = os.getenv(name)
            if raw in (None, ""):
                return default
            try:
                return float(raw)  # type: ignore[arg-type]
            except ValueError:
                self.logger.warning("Ignoring invalid %s=%r; using %s", name, raw, default)
                return default

        def _env_int(name: str, default: int) -> int:
            raw = os.getenv(name)
            if raw in (None, ""):
                return default
            try:
                return int(raw)  # type: ignore[arg-type]
            except ValueError:
                self.logger.warning("Ignoring invalid %s=%r; using %s", name, raw, default)
                return default

        kwargs: dict[str, Any] = {
            "temperature": _env_float("CHANDRA_AGENT_TEMPERATURE", 0.0),
            "top_p": _env_float("CHANDRA_AGENT_TOP_P", 1.0),
        }
        # max_tokens caps the OUTPUT. A fixed cap (the old default 8192, or the
        # 4096 some deployments set) truncates multi-file Terraform generation
        # mid-response → LengthFinishReasonError, even when the model has room
        # left in its context. Leave CHANDRA_AGENT_MAX_TOKENS UNSET so vLLM
        # uses all remaining context for the completion (context_len - prompt);
        # only pass a cap when an operator explicitly sets one.
        raw_max = os.getenv("CHANDRA_AGENT_MAX_TOKENS")
        if raw_max not in (None, ""):
            try:
                kwargs["max_tokens"] = int(raw_max)  # type: ignore[arg-type]
            except ValueError:
                self.logger.warning("Ignoring invalid CHANDRA_AGENT_MAX_TOKENS=%r", raw_max)
        return build_chat_model(**kwargs)

    def _structured_llm(self, schema: Any) -> Any:
        """Return a structured-output runnable using the right method per provider.

        LangChain's default (`function_calling`) needs the model to expose
        OpenAI-style tool calling — which for a local vLLM model means the
        server must be started with a *matching* ``--tool-call-parser`` +
        ``--enable-auto-tool-choice``. Getting that wrong (e.g. a Mistral
        parser on a Llama/Qwen model, or a model with no tool support at
        all) makes every structured call fail. vLLM instead supports schema
        enforcement via guided decoding (``response_format`` json_schema)
        for *any* model, no tool parser required — which is far more robust
        across the models that actually fit a 24 GB GPU.

        So: Bedrock/Claude keeps the proven tool-calling path unchanged;
        OpenAI-compatible / vLLM / Ollama default to ``json_schema`` guided
        decoding. Override with ``CHANDRA_STRUCTURED_OUTPUT_METHOD``
        (``json_schema`` | ``json_mode`` | ``function_calling``) if a
        specific server build needs a different one.
        """
        provider = (chandra_settings.llm_provider or "bedrock").strip().lower()
        openai_family = {"openai", "openai_compatible", "vllm", "ollama"}
        default_method = "json_schema" if provider in openai_family else "function_calling"
        method = (os.getenv("CHANDRA_STRUCTURED_OUTPUT_METHOD") or default_method).strip()
        if method == "function_calling":
            # Preserve the exact legacy call (Bedrock/Claude path unchanged).
            return self.Llm.with_structured_output(schema)
        return self.Llm.with_structured_output(schema, method=method)

    def _banner(self, text: str, char: str = "=", width: int = 78) -> None:
        self.logger.info(char * width)
        self.logger.info(text)
        self.logger.info(char * width)

    def _cleanup_sandbox(self, sandbox_path: Optional[str]) -> None:
        if sandbox_path and Path(sandbox_path).exists():
            self.logger.info("Preserving terraform_runs directory: %s", sandbox_path)

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

    def _run_mcp_aws_command(self, command: str) -> dict:
        """Runs a single `call_aws` invocation against the persistent MCP
        session (module-level `_mcp_session`). The aws_api server subprocess
        is started once for the life of the process — every call here (across
        every node, every retry iteration, every job) reuses the same warm
        connection instead of paying subprocess-startup cost again."""
        check_cancelled()
        try:
            return _mcp_session.run_coro(_mcp_run_aws_command_async, command, self.logger)
        except Exception as exc:
            self.logger.warning(f"MCP AWS CLI execution failed for '{command}': {exc}")
            return {}

    def _run_mcp_terraform_docs(self, provider_name: str, provider_namespace: str, service_slug: str, provider_document_type: str) -> str:
        """Looks up Terraform provider resource docs against the persistent
        MCP session's terraform tool handles — no per-call subprocess spin-up."""
        check_cancelled()
        try:
            return _mcp_session.run_coro(
                _mcp_fetch_terraform_docs_async,
                provider_name, provider_namespace, service_slug, provider_document_type,
                self.logger,
            )
        except Exception as exc:
            self.logger.warning(f"MCP Terraform docs failed: {exc}")
            return ""

    def _gather_aws_context(self, force_refresh: bool = False, discovery_commands: Optional[List[str]] = None) -> str:
        """Synchronous wrapper for the Generic Discovery Engine.

        Executes the LLM-specified discovery_commands (read-only AWS CLI
        commands) and returns formatted grounding context. Caches the result
        for 5 minutes to avoid redundant API calls within the same pipeline run.
        """
        import time
        now = time.time()
        
        if not force_refresh:
            cache_entry = self._aws_context_cache.get("value")
            cache_time = self._aws_context_cache.get("time", 0)
            if cache_entry and (now - cache_time < 300): # 5 min TTL
                self.logger.info("Discovery Engine: reusing cached context (TTL %ds remaining).", 300 - (now - cache_time))
                return cache_entry

        with self._aws_context_cache_lock:
            if not force_refresh:
                cache_entry = self._aws_context_cache.get("value")
                cache_time = self._aws_context_cache.get("time", 0)
                if cache_entry and (now - cache_time < 300):
                    return cache_entry

            try:
                start_t = time.time()
                ctx_str = _mcp_session.run_coro(
                    _gather_aws_context_async, self.logger, discovery_commands
                )
                self.logger.info("Discovery Engine: gathered context in %.1fs", time.time() - start_t)
            except Exception as exc:
                self.logger.warning("Discovery Engine failed: %s", exc)
                return ""

            if ctx_str:
                self._aws_context_cache["value"] = ctx_str
                self._aws_context_cache["time"] = time.time()
                self.logger.info("Discovery Engine: context generated (%d chars)", len(ctx_str))
            return ctx_str

    def _get_terraform_state_list(self, sandbox_dir: str) -> str:
        if not sandbox_dir:
            return ""
        path = Path(sandbox_dir)
        if not (path / "terraform.tfstate").exists() and not (path / ".terraform").exists():
            return ""
        try:
            result = execute_shell_command("terraform state list", cwd=str(path), timeout=30)
            if result["return_code"] == 0 and result["stdout"].strip():
                return result["stdout"].strip()
        except Exception as exc:
            self.logger.warning("terraform_state_list.failed: %s", exc)
        return ""

    def _read_existing_node(self, state: AgentState) -> dict:
        existing = self._read_files_from_folder(
            state.get("input_sandbox_path", ""), "existing"
        )
        state_ctx = self._get_terraform_state_list(state.get("input_sandbox_path", ""))
        return {
            "existing_files": existing,
            "clarification": None,
            "terraform_state_context": state_ctx,
            "validate_iteration": 0,
            "validate_passed": False,
            "validate_feedback": "",
        }

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
        aws_ctx = state.get("aws_context") or ""

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
        aws_section = f"\n\n{aws_ctx}" if aws_ctx else ""

        prompt = f"""You are an AWS automation engineer. Analyze this action request and identify \
what must be resolved DYNAMICALLY at runtime (never hardcoded).

Action Name: {action.get("actionName", action.get("action", ""))}
Action Description: {action.get("actionDescription", "")}
Steps: {json.dumps(action.get("steps") or [], indent=2)}{ref_ctx}{existing_ctx}{feedback_ctx}{memory_section}{aws_section}

This action may provision ANY AWS resource type (compute, storage, database, networking, IAM,
messaging, serverless, etc.) — do not assume it is EC2 unless the description says so. Reason
from first principles about what THIS resource type actually needs.

Determine:

1. needs_clarification — True ONLY when critical information is TRULY missing and CANNOT be
   resolved dynamically (e.g. the user must choose between multiple existing VPCs and we have no
   way to pick the right one, or must choose between two non-default options that materially
   change cost/behavior). 
   CRITICAL SECURITY EXCEPTION: For IAM/security requests, if the specific target identity (user/role) 
   or the exact scope of permissions is vague (e.g. "need ec2 permission"), you MUST set this to True 
   to ask the user for clarification. NEVER guess target users or grant overly broad permissions based on vague queries.
   Otherwise, do NOT ask about IDs, names, or regions that can be resolved via AWS data sources.

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

7. aws_services_involved — List of AWS service codes involved (e.g. ["ec2", "lambda"]). This is used to fetch service quotas.

8. expected_resources — Exact Terraform resource block names you plan to use (e.g. ["aws_instance", "aws_lambda_function"]). This is CRITICAL for fetching documentation.

9. reasoning — Brief justification. If agent memory above contains lessons for this action,
   incorporate them.

10. aws_discovery_commands — List of safe READ-ONLY AWS CLI commands (describe-*, list-*, get-*)
    needed to fetch the current account state relevant to this action. The backend will execute
    these and feed the results to the Terraform generator as grounding context.
    Always include 'aws sts get-caller-identity' and 'aws ec2 describe-availability-zones'.
    Only include commands relevant to THIS action. Examples by resource type:
    - S3 action: ['aws s3api list-buckets', 'aws kms list-aliases']
    - EC2 action: ['aws ec2 describe-vpcs', 'aws ec2 describe-subnets', 'aws ec2 describe-key-pairs', 'aws ec2 describe-security-groups', 'aws ssm get-parameters-by-path --path /aws/service/ami-amazon-linux-latest --recursive']
    - Lambda action: ['aws lambda list-functions', 'aws iam list-roles']
    - RDS action: ['aws rds describe-db-instances', 'aws rds describe-db-subnet-groups', 'aws ec2 describe-vpcs', 'aws ec2 describe-subnets', 'aws ec2 describe-security-groups']
    - IAM action: ['aws iam list-roles', 'aws iam list-users', 'aws iam list-policies --scope Local']
    Think about what existing resources the Terraform generator will need to see to avoid
    conflicts, reuse existing infrastructure, and generate correct code. 
    NEVER include commands that create, modify, or delete resources.

Generally be conservative with clarification requests (use sensible defaults when possible), but be AGGRESSIVELY 
cautious regarding IAM and security: ALWAYS ask the user if target identities or permissions are underspecified."""

        try:
            structured_llm = self._structured_llm(ActionAnalysis)
            analysis: ActionAnalysis = structured_llm.invoke([HumanMessage(content=prompt)])
            
            self.logger.info("Discovery Engine: LLM requested %d discovery commands for services %s",
                             len(analysis.aws_discovery_commands), analysis.aws_services_involved)
            aws_ctx = self._gather_aws_context(discovery_commands=analysis.aws_discovery_commands)
            aws_ctx = ExecutionAgents._budget_context(aws_ctx, "CHANDRA_AWS_CTX_MAX_CHARS", 6000, "AWS grounding")
            
            return {
                "analysis": analysis.model_dump(),
                "aws_context": aws_ctx
            }
        except Exception as exc:
            self.logger.exception("Analysis failed: %s", exc)
            raise


    def _check_permissions_node(self, state: AgentState) -> dict:
        check_cancelled()
        from src.chandra.execution.services import TaskAuthorizationService

        action = state.get("action", {})
        action_name = action.get("actionName", "")
        permission_sets = state.get("aws_permissions", [])
        if not permission_sets:
            self.logger.warning("No permission sets provided for task.")
            return {"permission_issues": ["No permission sets selected."]}
            
        permission_set_id = permission_sets[0] if isinstance(permission_sets, list) else permission_sets
        auth_service = TaskAuthorizationService()
        
        is_authorized = auth_service.is_authorized(action_name, permission_set_id)
        if not is_authorized:
             self.logger.warning(f"Task {action_name} not authorized by {permission_set_id}")
             return {"permission_issues": [f"Task {action_name} is not authorized by the selected permission set {permission_set_id}."]}
        
        self.logger.info(f"Gate 1: Task {action_name} authorized by {permission_set_id}")
        return {"permission_issues": [], "caller_arn": "User-Bounded"}
    @staticmethod
    def _route_after_permissions(state: AgentState) -> str:
        issues = state.get("permission_issues") or []
        if issues:
            return "inject_permission_hitl"

        if state.get("analysis") and state["analysis"].get("needs_clarification"):
            return "hitl"

        return "gather_docs"

    def _inject_permission_hitl_node(self, state: AgentState) -> dict:
        """Proper node (not router) that injects permission issues into analysis state."""
        issues = state.get("permission_issues") or []
        analysis = state.get("analysis") or {}
        action = state.get("action") or {}
        action_name = action.get("actionName", "the requested action")
        caller_arn = state.get("caller_arn") or "unknown"

        # Extract a friendly identity label from the ARN
        if ":user/" in caller_arn:
            identity_label = f"IAM User: {caller_arn.split(':user/')[-1]}"
        elif ":assumed-role/" in caller_arn:
            parts = caller_arn.split(":assumed-role/")[-1].split("/")
            identity_label = f"IAM Role: {parts[0]} (session: {parts[-1]})"
        elif ":root" in caller_arn:
            identity_label = "AWS Root Account"
        else:
            identity_label = caller_arn

        # Build a clean, actionable message for the user
        missing_list = "\n".join(f"  - {p}" for p in issues)
        message = (
            f"The following action requires additional AWS permissions to proceed:\n\n"
            f"  Action  : {action_name}\n"
            f"  Caller  : {identity_label}\n\n"
            f"Required permissions that appear to be missing:\n"
            f"{missing_list}\n\n"
            f"Please grant these permissions to the above IAM identity and confirm to continue,\n"
            f"or type 'no' to abort."
        )

        questions = list(analysis.get("questions") or [])
        questions.append(message)
        analysis["questions"] = questions
        analysis["needs_clarification"] = True
        self.logger.warning("Dynamic Grounding: Permission check FAILED — blocking for HITL. Missing: %s", issues)
        return {"analysis": analysis}

    def _hitl_node(self, state: AgentState) -> dict:
        questions: List[str] = state["analysis"].get("questions") or []
        if not questions:
            self.logger.warning(
                "_hitl_node reached with empty questions list — "
                "bypassing interrupt and proceeding to generate"
            )
            return {"clarification": None}

        # Attempt to comment the questions back to Jira
        action = state.get("action", {})
        jira_url = action.get("jiraUrl")
        if jira_url:
            issue_key = jira_url.split("/")[-1]
            if issue_key and issue_key.upper() != "ERROR":
                comment = "The Digital Worker requires more information to proceed with this task. Please provide the following details in the Digital Worker dashboard to resume execution:\n\n"
                for i, q in enumerate(questions, 1):
                    comment += f"{i}. {q}\n"
                add_comment_to_issue(issue_key, comment)

        answers = interrupt(questions)
        answers_list = answers if isinstance(answers, list) else [answers]
        return {
            "clarification": {
                "questions": questions,
                "answers": answers_list,
            }
        }

    @staticmethod
    def _budget_context(text: str, env_var: str, default_chars: int, label: str) -> str:
        """Cap one context block so the code-gen prompt stays within budget.

        Custom KRAs ground the generator with live AWS inventory + full
        Terraform resource docs (an ``aws_s3_bucket`` doc alone is many
        thousands of tokens). Predefined KRAs don't — which is why they stay
        small and fast while Custom KRAs balloon to ~24k prompt tokens and
        then truncate the completion (LengthFinishReasonError). Bedrock's
        large context absorbed it; a local model does not.

        A deterministic per-section char budget (~4 chars/token) keeps the
        prompt small enough that a 7B–14B local model has room to *generate*
        the plan. Bedrock is exempt (budget 0 = unlimited) so the Claude path
        is unchanged. Each budget is env-overridable.
        """
        provider = (chandra_settings.llm_provider or "bedrock").strip().lower()
        if provider == "bedrock":
            return text
        try:
            budget = int(os.getenv(env_var, str(default_chars)))
        except ValueError:
            budget = default_chars
        if budget <= 0 or len(text) <= budget:
            return text
        omitted = len(text) - budget
        logger.warning(
            "%s too large for local model (%d chars); trimming to %d (omitted %d). "
            "Override with %s.",
            label, len(text), budget, omitted, env_var,
        )
        return text[:budget] + (
            f"\n...[{label}: trimmed {omitted} chars to fit the local model's "
            "context budget; only the most relevant portion is shown]..."
        )

    def _generate_node(self, state: AgentState) -> dict:
        import time
        start_t = time.time()
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
        aws_ctx = state.get("aws_context") or ""
        terraform_state_ctx = state.get("terraform_state_context") or ""
        service_quotas_context = state.get("service_quotas_context") or ""
        terraform_docs_context = state.get("terraform_docs") or ""
        # Token Budget Manager: cap the grounding blocks that make Custom-KRA
        # prompts explode (full Terraform resource docs + live AWS inventory
        # + memory). Bedrock is exempt; local providers get bounded so the
        # completion isn't truncated (LengthFinishReasonError). Env-tunable.
        memory_ctx = self._budget_context(
            memory_ctx, "CHANDRA_MEMORY_MAX_CHARS", 3000, "Resolution memory"
        )
        validate_feedback = state.get("validate_feedback") or ""
        plan_review = state.get("plan_review") or {}
        plan_review_feedback = ""
        if plan_review and (not plan_review.get("matches_intent") or plan_review.get("destroy_or_replace_detected")):
            concerns = "; ".join(plan_review.get("concerns") or [])
            plan_review_feedback = (
                f"matches_intent={plan_review.get('matches_intent')}, "
                f"destroy_or_replace_detected={plan_review.get('destroy_or_replace_detected')}, "
                f"concerns: {concerns or '(none listed)'}, "
                f"reasoning: {plan_review.get('reasoning', '')}"
            )

        os_name = platform.system()

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
            self.logger.info("Resuming execution with Clarifications:\n%s", qa_lines)

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
        aws_section = f"\n{aws_ctx}" if aws_ctx else ""

        state_section = ""
        if terraform_state_ctx:
            state_section = f"""

EXISTING TERRAFORM STATE (already applied in this sandbox — do NOT recreate these resources;
reference them or plan an import instead):
{terraform_state_ctx}"""

        validate_section = ""
        if validate_feedback:
            validate_section = f"""

STATIC VALIDATION ERROR (HIGHEST PRIORITY — this blocked a real apply attempt, fix it first):
{validate_feedback}"""

        plan_review_section = ""
        if plan_review_feedback:
            plan_review_section = f"""

PLAN REVIEW FLAGGED A PROBLEM (fix the underlying resource configuration, not just syntax):
{plan_review_feedback}"""

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

        is_key_file_credential = requires_creds and bool(credential_strategy) and (
            "key" in credential_strategy.lower()
            or "pem" in credential_strategy.lower()
            or "ssh" in credential_strategy.lower()
        )
        pem_note = ""
        if is_key_file_credential:
            pem_note = (
                "SSH KEY FILE HANDLING: DO NOT use shell commands or post-deploy scripts to save the .pem file. "
                "You MUST use the Terraform `local_file` resource to write the private key to disk (e.g. `filename = \"ssh_key.pem\"`). "
                "Set `file_permission = \"0400\"` on the local_file resource so it is secured automatically by Terraform."
            )

        credential_context = ""
        if requires_creds:
            credential_context = f"""

REMOTE ACCESS CREDENTIALS REQUIRED (per analysis step):
This action provisions something that needs direct human access post-deployment.
Resolution strategy for THIS resource type: {credential_strategy or "Generate the credential dynamically using the appropriate Terraform resource for this resource type — never hardcode a password, key, or token."}
{pem_note}
Save any generated secret (private key, password, token) via a Terraform `local_file` resource or expose it via a
sensitive Terraform output — never print secrets in plain stdout logs or use shell scripts to parse them."""

        outputs_context = ""
        if post_deploy_outputs:
            outputs_list = "\n".join(f"    output \"{name}\" {{ ... }}" for name in post_deploy_outputs)
            outputs_context = f"""

MANDATORY OUTPUTS (per analysis step) — define exactly these in outputs.tf, one block each,
each referencing the real resource attribute it corresponds to (no placeholders):
{outputs_list}
The executableSteps after apply MUST include a single step running `terraform output` (or `terraform output -json`) so all these details are printed for the user at once."""

        # ── Explicit KRA and Permission Set Evaluation ──
        kra_data = state.get("kra_data")
        custom_kra_instruction = ""
        if kra_data and isinstance(kra_data, dict):
            kras = kra_data.get("kras", [])
            permissions = kra_data.get("permissions", {})
            task_info = kra_data.get("task", {})
            
            custom_kra_instruction = f"""
EVALUATION OBJECTIVES (KRAs) & CONSTRAINTS:
You must implement the requested AWS Task strictly within these operational objectives:
KRAs: {json.dumps(kras, indent=2) if kras else '(None explicitly listed — apply general best practices)'}

AUTHORIZATION BOUNDARY:
You must restrict your Terraform generation to the following IAM role and permission set:
Role: {permissions.get('role', 'Unknown')}
Allowed Permissions: {json.dumps(permissions.get('permissions', []), indent=2)}

TASK CONTEXT:
Task: {task_info.get('title', 'N/A')}
Description: {task_info.get('description', 'N/A')}

IMPORTANT: Evaluate your generated Terraform against these KRAs. If a KRA mandates 'Cost Optimization', ensure instances are right-sized or spot instances are preferred. If 'Security', ensure encryption and private subnets are used. Do NOT exceed the Authorization Boundary.
"""

        # prompt generation
        resources = analysis.get("expected_resources") or ["all"]
        batch_size = len(resources) if resources and resources != ["all"] else 1
        max_retries = 3

        tf_docs_dict = state.get("terraform_docs_dict") or {}

        all_generated_files = {}
        all_exec_steps = []
        summaries = []
        
        start_t = time.time()
        for attempt in range(max_retries):
            try:
                # Split resources into chunks of batch_size
                for i in range(0, len(resources), batch_size):
                    batch = resources[i:i+batch_size]
                    
                    # 1. Build batch-specific docs context
                    batch_docs_ctx = ""
                    for res in batch:
                        if res in tf_docs_dict:
                            batch_docs_ctx += tf_docs_dict[res]
                    if batch_docs_ctx:
                        batch_docs_ctx = "\nTERRAFORM DOCUMENTATION (Argument References) for this batch:\n" + batch_docs_ctx
                        batch_docs_ctx = self._budget_context(batch_docs_ctx, "CHANDRA_TF_DOCS_MAX_CHARS", 8000, "Terraform docs")
                        
                    # 2. Build existing files context from previous batches (if any)
                    prev_batch_ctx = ""
                    if all_generated_files:
                        prev_batch_ctx = "\n\nFILES GENERATED IN PREVIOUS BATCHES (Do not duplicate providers or variables. Append or update these files):\n"
                        for fname, fdata in all_generated_files.items():
                            prev_batch_ctx += f"=== {fname} ===\n{fdata['content']}\n\n"

                    # 3. Construct the prompt
                    batch_prompt = f"""You are a senior AWS automation engineer. {mode_instruction}

Action Name: {action.get("actionName")}
Action Description: {action.get("actionDescription")}
Steps: {json.dumps(action.get("steps") or [], indent=2)}
Recommended approach: {analysis.get("recommended_approach")}
Execution environment: {os_name}. {shell_note} {creds_note}
{dynamic_context}
{credential_context}
{outputs_context}
{memory_section}
{aws_section}
{service_quotas_context}
{custom_kra_instruction}
{terraform_docs_context}
{batch_docs_ctx}
{state_section}
{validate_section}
{plan_review_section}
{reference_context}{clarification_context}{feedback_context}{existing_context}
{prev_batch_ctx}

=========================================================================
ABSOLUTE RULES - violating any of these causes immediate failure
=========================================================================

RULE 0 — HCL SYNTAX: HEREDOC FOR MULTI-LINE STRINGS:
  Terraform/HCL does NOT support Python-style triple-quotes (' ' ' or \"\"\").
  For any multi-line string value (user_data, inline policy JSON, etc.) you MUST
  use HCL heredoc syntax.

RULE 1 — NEVER HARDCODE ENVIRONMENT-SPECIFIC VALUES (use data sources).
RULE 2 — REMOTE ACCESS CREDENTIALS: Use tf resources to gen them, don't hardcode.
RULE 3 — TERRAFORM BLOCK UNIQUENESS: Exact ONE provider, terraform {{}}, and output block.
RULE 4 — NO INVENTED PROVIDER ARGUMENTS.
RULE 5 — tfvars CONVENTION: Write terraform.tfvars.example.
RULE 6 — LEARN FROM FEEDBACK: Do not repeat errors.
RULE 7 — EXECUTABLE STEPS MUST BE COMPLETE.
RULE 8 — SELF-VALIDATING TERRAFORM (use preconditions).
RULE 9 — Do NOT use data sources for random_id or random_string. They are resources.
RULE 10 — NEVER use a "backend" argument for random_id. It is not supported. Use only byte_length.
RULE 11 — ALWAYS use proper HCL string interpolation format: "${{random_id.name.hex}}" instead of "string"[random_id.name.hex].
RULE 12 — DO NOT CHANGE DIRECTORIES: Files are written to the current working directory. Run `terraform init` and `terraform plan` directly without using `mkdir` or `cd` into subdirectories.
RULE 13 — PREVENT DUPLICATES. Do NOT declare the same resource (e.g., local_file.private_key) in multiple files.
RULE 14 — DO NOT ESCAPE INTERPOLATION. Use "${{var}}" exactly. DO NOT output "\\${{var}}".
--- BATCH INSTRUCTIONS ---
This is a partial generation. Generate/Update the configuration ONLY for these resources: {batch}. 
If files were generated in previous batches, output the FULL updated file content (do not output partial snippets).
Keep your explanations extremely short. Do not generate resources not in this list.

Generate the complete set of files now."""
                    
                    structured_llm = self._structured_llm(CodeGenerationResult)
                    result: CodeGenerationResult = structured_llm.invoke([HumanMessage(content=batch_prompt)])
                    
                    # Merge files (overwrite with the latest full file from the LLM)
                    for f in result.files:
                        all_generated_files[f.filename] = f.model_dump()
                            
                    all_exec_steps = [s.model_dump() for s in result.executableSteps] # Only keep the final steps
                    summaries.append(result.summary)
                
                timings = state.get("timings") or {}
                timings["llm_generate"] = time.time() - start_t
                
                return {
                    "generated_files": list(all_generated_files.values()),
                    "executable_steps": all_exec_steps,
                    "generator_summary": " ".join(summaries),
                    "timings": timings,
                }
            except Exception as exc:
                self.logger.warning("Generation failed on attempt %d: %s", attempt + 1, exc)
                # Some models throw generic Exceptions containing 'LengthFinishReasonError' in string
                if batch_size > 1 and ("length" in str(exc).lower() or "context" in str(exc).lower() or "token" in str(exc).lower() or "finish" in str(exc).lower()):
                    batch_size = max(1, batch_size // 2)
                    self.logger.info("Reducing batch size to %d and retrying...", batch_size)
                elif attempt == max_retries - 1:
                    raise
                
                timings = state.get("timings") or {}
                timings["llm_generate"] = time.time() - start_t
                
                # If we retry, we might want to clear them, but the user requested initializing outside try/retry so we keep them.
                # Returning here would abort the retry loop. The previous code had a return inside except! That breaks retries.
                # So we ONLY return if attempt == max_retries - 1, but we raise there. So we shouldn't return here.
                # Actually, if we hit an exception and want to retry, we just continue.
                continue


    def _write_files_node(self, state: AgentState) -> dict:
        input_path = state.get("input_sandbox_path") or ""
        
        # Ensure we use terraform_runs/<worker_id>/<job_id>
        worker_id = state.get("action", {}).get("workerId", "default_worker")
        job_id = getattr(self, "job_id", None) or state.get("action", {}).get("jobId", secrets.token_hex(6))
        
        if input_path and worker_id in input_path and job_id in input_path:
             sandbox_dir = str(Path(input_path).resolve())
        else:
             sandbox_dir = str(Path(f"terraform_runs/{worker_id}/{job_id}").resolve())

        Path(sandbox_dir).mkdir(parents=True, exist_ok=True)
        self.logger.info("Using sandbox directory: %s", sandbox_dir)
        
        # Cross-job idempotency: if using a new sandbox but inheriting from a previous run, copy its state and files.
        if input_path and Path(input_path).exists() and Path(input_path).resolve() != Path(sandbox_dir).resolve():
            import shutil
            for src_file in Path(input_path).glob("*"):
                if src_file.is_file():
                    shutil.copy2(src_file, Path(sandbox_dir) / src_file.name)
            self.logger.info("Copied previous sandbox state from: %s", input_path)

        CLEANABLE_EXTENSIONS = {".tf", ".py", ".sh", ".yaml", ".yml", ".json", ".md"}
        PRESERVE_NAMES = {
            "terraform.tfstate",
            "terraform.tfstate.backup",
            ".terraform.lock.hcl",
        }
        sandbox_path_obj = Path(sandbox_dir)
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

        # Write execution metadata
        metadata = {
            "action": state.get("action", {}),
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "timings": state.get("timings", {}),
            "generated_files": [f["filename"] for f in state.get("generated_files", [])],
        }
        write_tool.invoke({"file_path": "metadata.json", "text": json.dumps(metadata, indent=2)})
        
        execution = {
            "executable_steps": state.get("executable_steps", []),
            "generator_summary": state.get("generator_summary", "")
        }
        write_tool.invoke({"file_path": "execution.json", "text": json.dumps(execution, indent=2)})

        return {"sandbox_path": sandbox_dir, "input_sandbox_path": sandbox_dir}

    def _validate_node(self, state: AgentState) -> dict:
        check_cancelled()
        sandbox_dir = state["sandbox_path"]
        validate_iteration = state.get("validate_iteration", 0) + 1
        errors: List[str] = []

        tf_files = list(Path(sandbox_dir).glob("*.tf"))
        if tf_files:
            try:
                init_result = execute_shell_command(
                    "terraform init -upgrade -input=false", cwd=sandbox_dir, timeout=180
                )
                if init_result["return_code"] != 0:
                    errors.append(
                        f"terraform init failed:\n{init_result['stderr'][:2000] or init_result['stdout'][:2000]}"
                    )
                else:
                    val_result = execute_shell_command(
                        "terraform validate -no-color", cwd=sandbox_dir, timeout=60
                    )
                    if val_result["return_code"] != 0:
                        errors.append(
                            "terraform validate failed:\n"
                            f"{val_result['stdout'][:1500]}\n{val_result['stderr'][:1500]}"
                        )
            except Exception as exc:
                errors.append(f"terraform validate step raised an exception: {exc}")

        py_files = list(Path(sandbox_dir).glob("*.py"))
        for py_file in py_files:
            try:
                result = execute_shell_command(
                    f'python -m py_compile "{py_file.name}"', cwd=sandbox_dir, timeout=30
                )
                if result["return_code"] != 0:
                    errors.append(
                        f"py_compile failed for {py_file.name}:\n{result['stderr'][:1500]}"
                    )
            except Exception as exc:
                errors.append(f"py_compile step raised an exception for {py_file.name}: {exc}")

        passed = not errors
        if passed:
            self.logger.info("[validate] check static checks passed (attempt %d)", validate_iteration)
        else:
            self.logger.warning(
                "[validate] X static checks failed (attempt %d/%d)",
                validate_iteration, VALIDATE_MAX_RETRIES,
            )

        return {
            "validate_iteration": validate_iteration,
            "validate_passed": passed,
            "validate_feedback": "\n\n".join(errors),
        }

    def _validate_exhausted_node(self, state: AgentState) -> dict:
        """
        Static validation kept failing after VALIDATE_MAX_RETRIES attempts.
        Rather than waste a real terraform apply on code we already know is
        broken, synthesize a failed execution result and route straight to
        report/record_iteration — this still consumes one real iteration so
        the main retry budget and stuck-loop detection behave normally.
        """
        feedback = state.get("validate_feedback", "") or "(no details captured)"
        synthetic_result = {
            "command": "terraform validate / py_compile (pre-flight)",
            "description": "Static validation before real apply",
            "working_dir": state.get("sandbox_path", ""),
            "stdout": "",
            "stderr": feedback[:6000],
            "return_code": 1,
            "success": False,
            "timed_out": False,
            "timeout_seconds": 0,
        }
        self.logger.error(
            "[validate] exhausted %d attempts — skipping real apply, reporting as failed iteration",
            VALIDATE_MAX_RETRIES,
        )
        return {
            "execution_results": [synthetic_result],
            "success": False,
            "executor_summary": (
                f"Static validation failed after {VALIDATE_MAX_RETRIES} attempts — "
                f"real apply was skipped to avoid wasting time on known-broken code:\n{feedback[:2000]}"
            ),
        }

    @staticmethod
    def _route_after_validate(state: AgentState) -> str:
        if state.get("validate_passed"):
            return "proceed"
        if state.get("validate_iteration", 0) >= VALIDATE_MAX_RETRIES:
            return "give_up"
        return "retry_generate"

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

        if state.get("executable_steps"):
            steps = state["executable_steps"]
            self.logger.info("OK Using %d executable steps from generator", len(steps))
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
            commands = self._ensure_plan_out_flag(commands)
            return {
                "execution_plan": {
                    "execution_type": "mixed",
                    "commands": commands,
                    "reasoning": "Steps provided by generator node.",
                }
            }

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

Action Name: {action.get("actionName", action.get("action", ""))}
Action Description: {action.get("actionDescription", "")}
Steps: {json.dumps(action.get("steps") or [], indent=2)}

Execution environment: {os_name}. {shell_note} {creds_note}

Files in execute_folder:
{folder_contents}

File contents preview:
{file_context}

Create a detailed execution plan:

1. execution_type: "python" | "terraform" | "shell" | "mixed"

2. commands — ordered list. Follow these workflows:
   Terraform: terraform init → terraform validate → terraform plan -out=tfplan →
              terraform apply -auto-approve tfplan → terraform output (always run this last
              to display connection info, IDs, etc.)
   Python:    pip install -r requirements.txt (if present) → python <script>.py [args]
   Shell:     chmod +x <script>.sh → ./<script>.sh

3. reasoning — one or two sentences.

Only include commands needed for the actual files present.

OUTPUT CONTRACT: return ONLY the structured plan (execution_type, commands, reasoning).
No prose or markdown outside those fields. Each command must be a single complete shell
command string — never a placeholder, never "...", never a comment."""

        try:
            structured_llm = self._structured_llm(ExecutionPlan)
            plan: ExecutionPlan = structured_llm.invoke([HumanMessage(content=prompt)])
            self.logger.info("OK EXECUTION PLAN — type=%s  commands=%d", plan.execution_type, len(plan.commands))
            self.logger.info("  Reasoning: %s", plan.reasoning)
            commands = self._ensure_plan_out_flag([c.model_dump() for c in plan.commands])
            for cmd in commands:
                self.logger.info("  [%d] %s | %s", cmd["order"], cmd["description"], cmd["command"])
            return {
                "execution_plan": {
                    "execution_type": plan.execution_type,
                    "commands": commands,
                    "reasoning": plan.reasoning,
                }
            }
        except Exception as exc:
            self.logger.exception("Execution planning failed: %s", exc)
            raise

    @staticmethod
    def _ensure_plan_out_flag(commands: List[Dict]) -> List[Dict]:
        """
        Make sure the 'terraform plan' step always writes a plan file
        (-out=tfplan) so it can be reviewed before apply, and that the
        matching apply step consumes that exact plan file rather than
        re-planning implicitly.
        """
        updated = []
        for cmd in commands:
            command = cmd.get("command", "")
            cmd_lower = command.lower().strip()
            if cmd_lower.startswith("terraform plan") and "-out" not in cmd_lower:
                command = command.rstrip() + " -out=tfplan"
            elif cmd_lower.startswith("terraform apply") and "tfplan" not in cmd_lower:
                if "-auto-approve" in cmd_lower:
                    command = re.sub(r"-auto-approve\b", "tfplan", command, count=1).strip()
                    if "tfplan" not in command:
                        command = command.rstrip() + " tfplan"
                else:
                    command = command.rstrip() + " tfplan"
            new_cmd = dict(cmd)
            new_cmd["command"] = command
            updated.append(new_cmd)
        return updated

    def _normalize_terraform_command(self, command: str) -> Tuple[str, bool, str]:
        """
        Normalizes LLM-generated commands. Enforces Terraform execution bounds.
        Returns (normalized_cmd, was_modified, reject_reason).
        """
        original = command.strip()
        if not original:
            return "", False, ""
            
        import re
        parts = re.split(r'(&&|;)', original)
        
        normalized_parts = []
        was_modified = False
        
        for part in parts:
            if part.strip() in ('&&', ';'):
                normalized_parts.append(part)
                continue
                
            cmd_trim = part.strip()
            if not cmd_trim:
                continue
                
            cmd_lower = cmd_trim.lower()
            
            if ".." in cmd_trim or re.search(r'(?:^|\s)(?:/[a-zA-Z]|[a-zA-Z]:\\)', cmd_trim):
                return "", True, f"Command contains forbidden directory traversal or absolute path: {cmd_trim}"
                
            if (cmd_lower.startswith("cd ") or cmd_lower == "cd" or
                cmd_lower.startswith("pushd ") or cmd_lower == "pushd" or 
                cmd_lower.startswith("popd ") or cmd_lower == "popd" or
                cmd_lower.startswith("mkdir ") or cmd_lower.startswith("md ") or
                (cmd_lower.startswith("if not exist ") and "mkdir " in cmd_lower)):
                was_modified = True
                continue 
                
            normalized_parts.append(part)
            
        res = "".join(normalized_parts).strip()
        res = re.sub(r'^(&&|;)\s*', '', res)
        res = re.sub(r'\s*(&&|;)$', '', res)
        res = re.sub(r'(&&|;)\s*(&&|;)', r'\1', res).strip()
        
        return res, original != res, ""

    def _validate_command(self, command: str, execute_folder: str) -> Tuple[bool, str]:
        """Pre-execution validation. Returns (is_valid, error_message)."""
        base_path = Path(execute_folder).resolve()
        match = re.search(r'-var-file="?([^"\s]+)"?', command)
        if match:
            var_file = match.group(1)
            if not (base_path / var_file).exists():
                return False, f"Variable file does not exist: {var_file}"
        return True, ""

    def _run_commands(
        self, commands: List[Dict], base_path: Path, timeout: int
    ) -> Tuple[List[Dict], bool]:
        """
        Shared command-runner used by both the pre-apply plan-review pass and
        the main execute node. Returns (results, halted_early).
        """
        results: List[Dict] = []
        halted = False
        for idx, cmd_info in enumerate(sorted(commands, key=lambda c: c.get("order", 9999)), 1):
            original_command: str = cmd_info["command"]
            description: str = cmd_info.get("description", "")
            
            # Authoritative cwd is ALWAYS the sandbox root
            cwd = base_path.resolve()
            if not cwd.exists():
                cwd.mkdir(parents=True, exist_ok=True)

            normalized_cmd, was_modified, reject_reason = self._normalize_terraform_command(original_command)
            
            if reject_reason:
                self.logger.warning("X COMMAND REJECTED: %s — %s", description or original_command, reject_reason)
                results.append({
                    "command": original_command,
                    "description": description,
                    "working_dir": str(cwd),
                    "stdout": "",
                    "stderr": f"Command rejected: {reject_reason}",
                    "return_code": -1,
                    "success": False,
                    "timed_out": False,
                    "timeout_seconds": timeout,
                })
                halted = True
                break
                
            if not normalized_cmd and was_modified:
                self.logger.info("NORMALIZATION: Skipped directory manipulation command: '%s'", original_command)
                results.append({
                    "command": original_command,
                    "description": description,
                    "working_dir": str(cwd),
                    "stdout": "Command normalized to empty (skipped safe structural command).",
                    "stderr": "",
                    "return_code": 0,
                    "success": True,
                    "timed_out": False,
                    "timeout_seconds": timeout,
                })
                continue
            elif not normalized_cmd:
                continue
                
            if was_modified:
                self.logger.info("NORMALIZATION: Original command: '%s'", original_command)
                self.logger.info("NORMALIZATION: Normalized command: '%s'", normalized_cmd)
                self.logger.info("NORMALIZATION: Authoritative CWD: '%s'", str(cwd))

            command = normalized_cmd

            is_valid, error_msg = self._validate_command(command, str(cwd))
            if not is_valid:
                self.logger.warning("X VALIDATION FAILED: %s — %s", description or command, error_msg)
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
                halted = True
                break

            self.logger.info("EXECUTING: %s | %s", description or command, command)

            timed_out = False
            result: Dict[str, Any]
            try:
                tool_output = execute_shell_command(command=command, cwd=str(cwd), timeout=timeout)
                success = tool_output["return_code"] == 0
                result = {
                    "command": command,
                    "description": description,
                    "working_dir": str(cwd),
                    "stdout": tool_output["stdout"][:10_000],
                    "stderr": tool_output["stderr"][:8_000],
                    "return_code": tool_output["return_code"],
                    "success": success,
                    "timed_out": False,
                    "timeout_seconds": timeout,
                }
                if success:
                    self.logger.info("OK SUCCESS: %s (rc=%d)", description or command, tool_output["return_code"])
                else:
                    self.logger.warning("X FAILED: %s (rc=%d)", description or command, tool_output["return_code"])
            except TimeoutError as exc:
                timed_out = True
                timeout_msg = (
                    f"Command '{command}' timed out after {timeout}s. "
                    f"Step '{description}' did not complete within the allowed time."
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
                self.logger.error("X TIMEOUT: %s exceeded %ds", description or command, timeout)
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
                self.logger.exception("X EXCEPTION in '%s': %s", description or command, exc)

            results.append(result)
            if not result["success"]:
                halted = True
                break

        return results, halted

    def _plan_review_node(self, state: AgentState) -> dict:
        check_cancelled()
        execute_folder = state["sandbox_path"]
        plan = state.get("execution_plan") or {}
        commands = sorted(plan.get("commands", []), key=lambda c: c.get("order", 9999))
        timeout = state.get("command_timeout") or DEFAULT_COMMAND_TIMEOUT
        base_path = Path(execute_folder).resolve()

        if plan.get("execution_type") not in ("terraform", "mixed") or not any(
            "terraform" in c.get("command", "").lower() for c in commands
        ):
            return {"plan_review_skipped": True, "pre_apply_results": []}

        pre_apply_cmds = []
        for c in commands:
            cmd_lower = c.get("command", "").lower()
            if "apply" in cmd_lower or "terraform output" in cmd_lower:
                break
            pre_apply_cmds.append(c)

        if not pre_apply_cmds:
            return {"plan_review_skipped": True, "pre_apply_results": []}

        results, halted = self._run_commands(pre_apply_cmds, base_path, timeout)
        if halted:
            return {
                "pre_apply_results": results,
                "plan_review_precheck_failed": True,
                "plan_review_skipped": False,
            }

        tfplan_path = base_path / "tfplan"
        if not tfplan_path.exists():
            self.logger.info("[plan_review] no tfplan file produced — skipping content review")
            return {"pre_apply_results": results, "plan_review_skipped": True}
            
        try:
            show_result = execute_shell_command("terraform show -json tfplan", cwd=str(base_path), timeout=60)
            plan_json_path = base_path / "tfplan.json"
            with open(plan_json_path, "w", encoding="utf-8") as f:
                f.write(show_result["stdout"] or "{}")
                
            from src.chandra.execution.services import TerraformPlanPolicyValidator
            validator = TerraformPlanPolicyValidator()
            
            action_name = state.get("action", {}).get("actionName", "")
            permission_sets = state.get("aws_permissions", [])
            if permission_sets:
                 perm_id = permission_sets[0] if isinstance(permission_sets, list) else permission_sets
                 is_valid, reason = validator.validate_plan(str(plan_json_path), perm_id, action_name)
                 if not is_valid:
                      self.logger.error(f"Gate 2 Plan Validation Failed: {reason}")
                      return {
                          "pre_apply_results": results,
                          "plan_review_precheck_failed": True,
                          "plan_review_skipped": False,
                          "plan_review_issue": reason
                      }
                 else:
                      self.logger.info(f"Gate 2 Plan Validation Passed: {reason}")
        except Exception as exc:
            self.logger.warning("[plan_review] plan validation failed: %s", exc)
            return {"pre_apply_results": results, "plan_review_skipped": True}

        return {"pre_apply_results": results, "plan_review_skipped": False, "plan_review_issue": ""}

    def _human_approval_center_node(self, state: AgentState) -> dict:
        """
        Pauses execution for Human Approval Center.
        Exports the pending job state and waits. Once approved, the job resumes directly to execute (terraform apply).
        """
        check_cancelled()
        sandbox_dir = state.get("sandbox_path") or "."
        action = state.get("action", {})
        
        # Determine plan payload
        plan_results = state.get("pre_apply_results", [])
        tf_plan_output = ""
        for res in plan_results:
            if "plan" in res.get("command", ""):
                tf_plan_output = res.get("stdout", "") or res.get("stderr", "")
                
        approval_payload = {
            "job_id": self.job_id or action.get("actionName", "unknown"),
            "custom_kra": action.get("kraCode", "Unknown"),
            "terraform_files": state.get("generated_files", []),
            "terraform_plan": tf_plan_output,
            "status": "Pending Approval",
            "message": "Please approve the Terraform execution plan."
        }
        
        approval_file = Path(sandbox_dir) / "human_approval_state.json"
        try:
            with open(approval_file, "w", encoding="utf-8") as f:
                json.dump(approval_payload, f, indent=2)
            self.logger.info("Exported pending job state to %s", approval_file)
        except Exception as e:
            self.logger.warning("Failed to write approval state: %s", e)
            
        self.logger.warning("Execution paused for Human Approval Center. Job ID: %s", approval_payload["job_id"])
        # Pause graph execution here
        from langgraph.types import interrupt
        
        auto_approve = os.environ.get("CHANDRA_AUTO_APPROVE") == "1"
        if auto_approve:
            self.logger.info("CHANDRA_AUTO_APPROVE is enabled. Skipping manual Terraform plan approval.")
            approval_response = {"approved": True}
        else:
            approval_response = interrupt([approval_payload])
        
        is_approved = True
        if isinstance(approval_response, dict) and not approval_response.get("approved", True):
            is_approved = False
            
        if not is_approved:
            self.logger.warning("Human Approval Center: Job REJECTED. Halting execution.")
            return {"approval_rejected": True, "success": False}
        
        self.logger.info("Human Approval Center: Job APPROVED. Resuming execution directly to apply phase.")
        return {"approval_rejected": False}


    def _plan_review_precheck_failed_node(self, state: AgentState) -> dict:
        """init/validate/plan itself failed during the review pass — treat exactly
        like a normal execution failure and let report/record_iteration handle it."""
        results = state.get("pre_apply_results") or []
        return {"execution_results": results, "success": False}

    def _route_after_plan_review(self, state: AgentState) -> str:
        if state.get("plan_review_precheck_failed"):
            return "precheck_failed"
        if state.get("plan_review_issue"):
            return "retry_generate"
        return "proceed"

    def _typed_execution_gate(self, state: AgentState) -> Optional[dict]:
        """Route execution through the validated ExecutionPlan pipeline.

        When ``CHANDRA_TYPED_EXECUTION`` is enabled, remediation runs only
        through ``plan_and_execute`` — provider (Bedrock/vLLM via
        ``LLM_PROVIDER``) → structured-JSON planner (self-correcting) →
        schema + safety + Terraform + intent validation → deterministic
        typed executor (AwsClientFactory, no shell/subprocess/eval). The
        model can only *propose* a typed plan; it never produces code that
        is run.

        Returns the node's result dict when it handled execution, or
        ``None`` to fall through to the legacy code-generation engine
        (default, so existing behavior is preserved until an operator opts
        in after end-to-end validation).
        """
        if not chandra_settings.typed_execution_enabled:
            return None

        action = state["action"]
        intent = f"{action.get('actionName', '')}: {action.get('actionDescription', '')}".strip(
            ": "
        )
        steps = action.get("steps") or []
        context_parts = [f"Steps: {json.dumps(steps)}"] if steps else []
        if state.get("aws_context"):
            context_parts.append(str(state["aws_context"]))
        context = "\n".join(context_parts)

        # By execute time the plan has already cleared the graph's approval
        # gate (plan_review + HITL), so this is an approved run. dry_run is
        # opt-out via state; default safe.
        dry_run = bool(state.get("dry_run", True))

        self.logger.info("=" * 80)
        self.logger.info("TYPED EXECUTION (CHANDRA_TYPED_EXECUTION on) — intent=%s", intent[:120])
        self.logger.info("=" * 80)

        result: BridgeResult = plan_and_execute(
            intent, context, approved=True, dry_run=dry_run
        )

        if not result.planned or result.execution is None:
            self.logger.error(
                "TYPED EXECUTION: model produced no valid plan (attempts=%d) — no action taken",
                result.generation.attempts,
            )
            return {
                "execution_results": [
                    {
                        "command": "<typed-plan>",
                        "description": "no valid ExecutionPlan produced",
                        "working_dir": ".",
                        "stdout": "",
                        "stderr": "; ".join(result.generation.errors),
                        "return_code": 1,
                        "success": False,
                        "timed_out": False,
                    }
                ],
                "success": False,
            }

        execution_results = [
            {
                "command": f"{s.kind.value}:{s.detail}",
                "description": s.detail,
                "working_dir": ".",
                "stdout": json.dumps(s.output) if s.output else "",
                "stderr": "" if s.status not in ("failed", "rejected") else s.detail,
                "return_code": 0 if s.status not in ("failed", "rejected") else 1,
                "success": s.status not in ("failed", "rejected"),
                "timed_out": False,
            }
            for s in result.execution.steps
        ]
        self.logger.info(
            "TYPED EXECUTION complete — dry_run=%s ok=%s steps=%d errors=%d",
            result.dry_run, result.ok, len(execution_results), len(result.execution.errors),
        )
        return {"execution_results": execution_results, "success": result.ok}

    def _execute_node(self, state: AgentState) -> dict:
        check_cancelled()
        if state.get("approval_rejected"):
            self.logger.warning("Job was rejected in Human Approval Center. Skipping execution.")
            return {"success": False, "execution_results": []}
        typed = self._typed_execution_gate(state)
        if typed is not None:
            return typed
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

        # Deterministic Permission Enforcement Block
        aws_permissions_ids = state.get("aws_permissions", [])
        if aws_permissions_ids:
            self.logger.info("VALIDATING EXECUTION AGAINST USER-SELECTED PERMISSION SETS BEFORE RUNNING...")
            import json
            from pathlib import Path
            aws_permissions_file = Path(__file__).parent.parent / "aws_permissions.json"
            if not aws_permissions_file.exists():
                aws_permissions_file = Path("aws_permissions.json")
            
            if aws_permissions_file.exists():
                with aws_permissions_file.open("r", encoding="utf-8") as f:
                    all_sets = json.load(f)
                
                allowed_actions = set()
                for pset in all_sets:
                    if str(pset.get("id")) in aws_permissions_ids or pset.get("id") in aws_permissions_ids:
                        for a in pset.get("actions", []):
                            if isinstance(a, dict) and "action" in a:
                                allowed_actions.add(a["action"].lower())
                
                if allowed_actions:
                    # We check if the planned commands / target action is covered.
                    # Since the action has a name or service, we can do a naive check if the service matches the allowed action prefix.
                    action = state.get("action", {})
                    service = action.get("service", "").lower()
                    
                    if not service:
                        # Extract service from the command if service is empty
                        for cmd in plan.get("commands", []):
                            c_str = cmd.get("command", "")
                            if c_str.startswith("aws "):
                                parts = c_str.split(" ")
                                if len(parts) > 1:
                                    service = parts[1].lower()
                                    break
                    
                    # Very simple prefix matching to ensure we don't block valid things if it matches the prefix.
                    # E.g. allowed: s3:createbucket, service is s3
                    # We must check if any allowed action starts with the service prefix
                    if service:
                        is_allowed = any(a.startswith(f"{service}:") or a == f"{service}:*" for a in allowed_actions)
                        if not is_allowed:
                            self.logger.error("BLOCKED: Planned AWS action relies on %s, which is not granted by user-selected permissions.", service)
                            return {"execution_results": [], "success": False, "status": "blocked"}

        base_path = Path(execute_folder).resolve()
        all_commands = sorted(plan["commands"], key=lambda c: c.get("order", 9999))

        pre_apply_results = state.get("pre_apply_results") or []
        commands_to_run = all_commands[len(pre_apply_results):] if pre_apply_results else all_commands

        total_commands = len(all_commands)
        self.logger.info("=" * 80)
        self.logger.info(
            "EXECUTION STARTED: %d command(s) total (%d already run in plan review) in %s  [timeout=%ds/cmd]",
            total_commands, len(pre_apply_results), execute_folder, timeout,
        )
        self.logger.info("=" * 80)

        start_t = time.time()
        new_results, _halted = self._run_commands(commands_to_run, base_path, timeout)
        results: List[Dict] = list(pre_apply_results) + new_results

        overall_success = (
            bool(results)
            and all(r["success"] for r in results)
            and len(results) == total_commands
        )

        self.logger.info("=" * 80)
        if overall_success:
            self.logger.info("OK EXECUTION COMPLETED SUCCESSFULLY: All %d command(s) ran", len(results))
        else:
            timed_out_count = sum(1 for r in results if r.get("timed_out"))
            if timed_out_count:
                self.logger.warning("X EXECUTION: %d command(s) timed out", timed_out_count)
            else:
                self.logger.warning(
                    "X EXECUTION: %d/%d command(s) succeeded",
                    sum(1 for r in results if r["success"]), len(results),
                )
        self.logger.info("=" * 80)

        timings = state.get("timings") or {}
        timings["terraform_apply"] = time.time() - start_t

        if overall_success:
             try:
                 from src.chandra.execution.services import AwsResourceVerifier
                 verifier = AwsResourceVerifier()
                 action_name = state.get("action", {}).get("actionName", "")
                 
                 # Get terraform output -json
                 out_result = execute_shell_command("terraform output -json", cwd=str(base_path), timeout=60)
                 if out_result["success"] and out_result["stdout"]:
                      import json
                      outputs = json.loads(out_result["stdout"])
                      is_verified = verifier.verify_resource(action_name, outputs)
                      if is_verified == "VERIFIED":
                           self.logger.info("Post-Apply Verification Passed: Resource state confirmed via boto3.")
                      elif is_verified == "UNVERIFIED":
                           self.logger.warning("Post-Apply Verification Skipped: Resource type not supported for deterministic verification.")
                           overall_success = False
                      else:
                           self.logger.error("Post-Apply Verification Failed: Resource state could not be confirmed via boto3.")
                           overall_success = False
             except Exception as exc:
                 self.logger.warning("Post-Apply Verification encountered an error: %s", exc)

        return {"execution_results": results, "success": overall_success, "timings": timings}

    def _report_node(self, state: AgentState) -> dict:
        check_cancelled()
        action = state["action"]
        results = state.get("execution_results") or []
        success = state.get("success", False)
        timeout = state.get("command_timeout") or DEFAULT_COMMAND_TIMEOUT
        analysis = state.get("analysis") or {}
        post_deploy_outputs = analysis.get("post_deploy_outputs") or []
        plan_review = state.get("plan_review") or {}

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

        if post_deploy_outputs and success:
            output_lines = "\n".join(f"    {name} : <value extracted from terraform output>" for name in post_deploy_outputs)
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

        plan_review_rule = ""
        if plan_review and ((not plan_review.get("matches_intent", True)) or plan_review.get("destroy_or_replace_detected")):
            plan_review_rule = (
                "\n- IMPORTANT: the automated plan review flagged a possible concern before apply "
                f"(reasoning: {plan_review.get('reasoning', '')}). Mention this caveat explicitly "
                "so the user double-checks the created resources."
            )

        prompt = f"""Summarize the execution of this AWS automation action in 2–4 sentences.

Action: {action.get("actionName", action.get("action", ""))}
Description: {action.get("actionDescription", "")}
Overall Success: {success}
Per-command timeout: {timeout}s

Execution Results:
{results_text}

Rules:
{timeout_rule}
- Name exact argument/error from the 'Errors' field — do not invent errors.
- Cover: what ran, whether it succeeded, resource IDs created, concrete next steps on failure.
{outputs_rule}{plan_review_rule}"""

        try:
            response = self.Llm.invoke([HumanMessage(content=prompt)])
            summary = response.content
            self.logger.info("OK LLM summary generated:\n%s", summary)
        except Exception as exc:
            self.logger.warning("LLM summary failed, using fallback: %s", exc)
            timed_out_cmds = [r for r in results if r.get("timed_out")]
            if timed_out_cmds:
                names = ", ".join(f"'{r['command']}'" for r in timed_out_cmds)
                summary = (
                    f"Execution of '{action.get('actionName', action.get('action', ''))}' failed: {names} timed out after "
                    f"{timeout}s. Check AWS credentials and network access, then retry."
                )
            else:
                succeeded = sum(1 for r in results if r["success"])
                summary = (
                    f"Executed {succeeded}/{len(results)} command(s) for "
                    f"'{action.get('actionName', action.get('action', ''))}'. "
                    + ("All steps completed successfully." if success else "Some commands failed — review stderr.")
                )

        return {"executor_summary": summary}

    @staticmethod
    def _extract_error_class(stderr: str) -> str:
        """
        Extract a short canonical error class from stderr so we can detect when
        the agent is stuck repeating the same error type across iterations.

        Examples:
            "InvalidAMIID.NotFound: ..."       -> "InvalidAMIID.NotFound"
            "Error: Duplicate output ..."      -> "Duplicate output definition"
            "api error SomeCode: ..."          -> "SomeCode"
            "ResourceAlreadyExistsException"   -> "ResourceAlreadyExistsException"
            "ValidationException: ..."         -> "ValidationException"
            "Error creating X: OperationAborted"-> "OperationAborted"
            "Traceback ... FileNotFoundError:" -> "FileNotFoundError"
        """
        clean = re.sub(r"\x1b\[[0-9;]*m", "", stderr)

        m = re.search(r"api error ([A-Za-z0-9_.]+)", clean)
        if m:
            return m.group(1)

        m = re.search(
            r"\b([A-Z][A-Za-z0-9]*(?:Exception|NotFound|AlreadyExists|LimitExceeded|Denied|Invalid[A-Za-z]*))\b",
            clean,
        )
        if m:
            return m.group(1)

        m = re.search(r"Error (?:creating|deleting|updating|reading)[^:]*:\s*([A-Za-z0-9_.]+)", clean)
        if m:
            return m.group(1)

        m = re.search(r"Error:\s+(.+)", clean)
        if m:
            return m.group(1).strip()[:80]

        m = re.search(r"^([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception)):", clean, re.MULTILINE)
        if m:
            return m.group(1)

        return clean.split("\n")[0].strip()[:80]

    @staticmethod
    def _compress_feedback_history(prior_feedback: str, max_detailed_iterations: int = 2) -> str:
        if not prior_feedback:
            return ""

        markers = ["PRIOR ITERATION HISTORY", "THIS ITERATION'S EXACT ERROR", "AI-generated summary"]
        if not any(m in prior_feedback for m in markers):
            return prior_feedback

        chunks = [c for c in prior_feedback.split("\n\n") if c.strip()]
        if len(chunks) <= max_detailed_iterations * 2:
            return prior_feedback

        keep_detailed = chunks[-(max_detailed_iterations * 2):]
        older = chunks[: -(max_detailed_iterations * 2)]

        compressed_lines = ["EARLIER ITERATIONS (compressed — full detail dropped to save space):"]
        for chunk in older:
            first_line = chunk.strip().splitlines()[0][:150]
            compressed_lines.append(f"  - {first_line}")

        return "\n".join(compressed_lines) + "\n\n" + "\n\n".join(keep_detailed)

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
            self._banner(f"OK PIPELINE COMPLETED SUCCESSFULLY on iteration {iteration}", char="═")
            return {
                "records": records,
                "final_status": "success",
                "final_summary": state.get("executor_summary"),
                "consecutive_same_error": 0,
                "last_error_class": "",
            }

        max_iter = state.get("max_iterations", MAX_ITERATIONS)

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

        feedback = self._compress_feedback_history(feedback)

        self.logger.info(
            "[Iteration %d] Feedback for next iteration:\n%s",
            iteration,
            feedback,
        )

        if iteration >= max_iter:
            self._banner(f"X PIPELINE EXHAUSTED {max_iter} ITERATIONS WITHOUT SUCCESS", char="═")
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

        if consecutive >= STUCK_THRESHOLD:
            self.logger.warning(
                "[Iteration %d] STUCK DETECTED — same error '%s' repeated %d times. "
                "Escalating to mid-run HITL.",
                iteration, current_error_class, consecutive,
            )
            return {
                "records": records,
                "final_status": "stuck",
                "feedback_summary": feedback,
                "consecutive_same_error": consecutive,
                "last_error_class": current_error_class,
            }

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

        error_snippet = ""
        for r in reversed(state.get("execution_results") or []):
            if not r.get("success"):
                raw = re.sub(r"\x1b\[[0-9;]*m", "", r.get("stderr", "") or "")
                
                lines = []
                for ln in raw.splitlines():
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

        # Attempt to comment the questions back to Jira
        jira_url = action.get("jiraUrl")
        if jira_url:
            issue_key = jira_url.split("/")[-1]
            if issue_key and issue_key.upper() != "ERROR":
                comment = "The Digital Worker is stuck and requires engineer guidance to proceed. Please provide the following details in the Digital Worker dashboard to resume execution:\n\n"
                for i, q in enumerate(questions, 1):
                    comment += f"{i}. {q}\n"
                add_comment_to_issue(issue_key, comment)

        user_guidance = interrupt(questions)
        guidance_list = user_guidance if isinstance(user_guidance, list) else [user_guidance]
        guidance_text = "\n".join(str(g) for g in guidance_list)

        updated_feedback = (
            f"USER GUIDANCE (HIGHEST PRIORITY — apply this immediately):\n{guidance_text}\n\n"
            + feedback
        )

        self.logger.info("MID-RUN HITL: received user guidance, resuming pipeline")
        return {
            "feedback_summary": updated_feedback,
            "final_status": "in_progress",
            "iteration": state.get("iteration", 1) + 1,
            "consecutive_same_error": 0,
            "last_error_class": "",
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

    @staticmethod
    def _extract_essential_tf_docs(markdown_text: str) -> str:
        """Aggressively strips out all examples, import details, long histories, 
        and prose descriptions, leaving ONLY bare schema definitions (Arguments & Attributes)."""
        if not markdown_text:
            return ""
        
        extracted = []
        capture = False
        
        # We only care about the actual schema references
        keep_headers = ["argument reference", "attributes reference"]
        
        # We absolutely want to drop these massive token hogs
        drop_headers = ["example usage", "import", "migration", "timeouts", "version", "history", "nested blocks"]

        lines = markdown_text.splitlines()
        
        for line in lines:
            lower_line = line.lower().strip()
            
            # Start of a new header block
            if line.startswith("#"):
                # If it's a header we want to drop, turn off capture
                if any(dh in lower_line for dh in drop_headers):
                    capture = False
                    continue
                
                # If it's a core schema header, turn on capture
                if any(kh in lower_line for kh in keep_headers):
                    capture = True
                    extracted.append(line)
                    continue
                    
                # For any other H1/H2 (like resource name), keep the header line 
                # but don't capture the prose beneath it to save tokens
                if line.startswith("# ") or line.startswith("## "):
                    extracted.append(line)
                    capture = False
                    continue
                
            if capture:
                # Strip out long paragraph descriptions and only keep bullet points (the actual arguments/attributes)
                # or nested headers inside the reference blocks
                if line.startswith("- ") or line.startswith("* ") or line.startswith("#"):
                    # Further trim long descriptions within bullet points by cutting at the first period
                    if "- " in line or "* " in line:
                        parts = line.split(". ", 1)
                        if len(parts) > 1:
                            line = parts[0] + "."
                    extracted.append(line)
                    
        return "\n".join(extracted)
                
        # If extraction logic failed completely (e.g. format is weird), fallback to original but truncated
        if len(extracted) < 10:
            return markdown_text[:4000] + "\n...[truncated]..."
            
        result = "\n".join(extracted)
        # Final safety truncation just in case
        if len(result) > 5000:
            result = result[:5000] + "\n...[truncated]..."
        return result

    def _gather_docs_and_quotas_node(self, state: AgentState) -> dict:
        check_cancelled()

        # ── Bypass: if user-defined KRA payload present, skip MCP entirely ──
        kra_data = state.get("kra_data")
        if kra_data and isinstance(kra_data, dict):
            payload_json = json.dumps(kra_data, indent=2)
            self.logger.info(
                "Custom KRA with user-defined payload — skipping MCP Terraform docs. "
                "Payload: %s", payload_json[:500]
            )
            # Cache the user payload so retry iterations reuse it
            self._terraform_docs_context_cache["value"] = json.dumps({
                "type": "custom_kra_payload",
                "instruction": "This is a user-defined KRA payload. Use your internal knowledge of AWS services and Terraform to generate the appropriate Terraform HCL based on the resources/permissions specified below. Do NOT attempt to fetch external documentation.",
                "payload": kra_data
            }, indent=2)
            return {
                "service_quotas_context": "",
                "terraform_docs": self._terraform_docs_context_cache["value"],
            }

        analysis = state.get("analysis") or {}
        services = analysis.get("aws_services_involved") or []
        resources = analysis.get("expected_resources") or []

        docs_dict = {}
        if resources:
            for res_type in resources:
                if res_type in self._docs_cache:
                    docs_dict[res_type] = self._docs_cache[res_type]
                    continue
                if not res_type.startswith("aws_"):
                    continue

                try:
                    raw_markdown = self._run_mcp_terraform_docs(
                        provider_name="aws",
                        provider_namespace="hashicorp",
                        service_slug=res_type,
                        provider_document_type="resources"
                    )

                    markdown = self._extract_essential_tf_docs(raw_markdown)

                    if markdown:
                        res_ctx = f"\n--- {res_type} Documentation ---\n{markdown}\n"
                        self.logger.info("Fetched TF docs for %s (%d chars, condensed from %d chars)", res_type, len(markdown), len(raw_markdown))
                    else:
                        res_ctx = f"\n--- {res_type} ---\nNo docs returned from MCP.\n"
                        self.logger.info("No TF docs returned for %s", res_type)

                    self._docs_cache[res_type] = res_ctx
                    docs_dict[res_type] = res_ctx
                except Exception as e:
                    self.logger.warning("Failed to fetch TF docs for %s: %s", res_type, e)

        self.logger.info(
            "Dynamic Grounding: Analyzer identified services=%s and resources=%s",
            services, resources
        )
        self.logger.info(
            "Dynamic Grounding: Fetched docs for %d resources.",
            len(resources) if resources else 0
        )

        return {
            "service_quotas_context": "",
            "terraform_docs": "",
            "terraform_docs_dict": docs_dict
        }


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

    def _build_graph(self):
        builder = StateGraph(AgentState)

        builder.add_node("read_existing", self._read_existing_node)
        builder.add_node("read_reference", self._read_reference_node)
        builder.add_node("analyze", self._analyze_node)
        builder.add_node("check_permissions", self._check_permissions_node)
        builder.add_node("inject_permission_hitl", self._inject_permission_hitl_node)
        builder.add_node("hitl", self._hitl_node)
        builder.add_node("gather_docs", self._gather_docs_and_quotas_node)
        builder.add_node("generate", self._generate_node)
        builder.add_node("write_files", self._write_files_node)

        builder.add_node("validate", self._validate_node)
        builder.add_node("validate_exhausted", self._validate_exhausted_node)

        builder.add_node("scan_folder", self._scan_folder_node)
        builder.add_node("plan", self._plan_node)
        builder.add_node("plan_review", self._plan_review_node)
        builder.add_node("plan_review_precheck_failed", self._plan_review_precheck_failed_node)
        builder.add_node("human_approval_center", self._human_approval_center_node)
        builder.add_node("execute", self._execute_node)
        builder.add_node("report", self._report_node)

        builder.add_node("record_iteration", self._record_iteration_node)
        builder.add_node("mid_run_hitl", self._mid_run_hitl_node)
        builder.add_node("save_memory_success", self._save_memory_node)
        builder.add_node("save_memory_fail", self._save_memory_node)

        builder.set_entry_point("read_existing")
        builder.add_edge("read_existing", "read_reference")
        builder.add_edge("read_reference", "analyze")
        builder.add_edge("analyze", "check_permissions")
        builder.add_conditional_edges(
            "check_permissions",
            self._route_after_permissions,
            {"inject_permission_hitl": "inject_permission_hitl", "hitl": "hitl", "gather_docs": "gather_docs"},
        )
        builder.add_edge("inject_permission_hitl", "hitl")
        builder.add_edge("hitl", "gather_docs")
        builder.add_edge("gather_docs", "generate")
        builder.add_edge("generate", "write_files")
        builder.add_edge("write_files", "validate")

        builder.add_conditional_edges(
            "validate",
            self._route_after_validate,
            {
                "proceed": "scan_folder",
                "retry_generate": "generate",
                "give_up": "validate_exhausted",
            },
        )
        builder.add_edge("validate_exhausted", "report")

        builder.add_edge("scan_folder", "plan")
        builder.add_edge("plan", "plan_review")

        builder.add_conditional_edges(
            "plan_review",
            self._route_after_plan_review,
            {
                "proceed": "human_approval_center",
                "retry_generate": "generate",
                "precheck_failed": "plan_review_precheck_failed",
            },
        )
        builder.add_edge("human_approval_center", "execute")
        builder.add_edge("plan_review_precheck_failed", "report")

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
        builder.add_edge("save_memory_success", END)
        builder.add_edge("save_memory_fail", END)
        builder.add_edge("mid_run_hitl", "read_existing")

        return builder.compile(checkpointer=self.Checkpointer)

    def RunPipeline(
        self,
        action: Dict[str, Any],
        sandbox_path: Optional[str] = None,
        reference_folder: Optional[str] = None,
        thread_id: Optional[str] = None,
        answers: Optional[List[str]] = None,
        command_timeout: int = DEFAULT_COMMAND_TIMEOUT,
        aws_permissions: Optional[List[str]] = None,
    ) -> PipelineResponse:
        """
        Run the generate → execute → (retry on failure) loop until success or
        max_iterations is exhausted, all within a single agent / single graph.

        Pass ``answers`` (non-empty list) together with the original ``thread_id``
        to resume either a pre-execution clarification pause OR a mid-run HITL pause.
        Both pause types return statusCode=202 / status='needs_clarification'.
        """
        # Prevent checkpointer collisions with parent orchestrators by prefixing
        tid = thread_id or (f"exec-{self.job_id}" if self.job_id else str(uuid.uuid4()))

        if answers and not thread_id:
            raise ValueError(
                "thread_id is required when resuming with answers. "
                "Pass the thread_id returned from the original RunPipeline call "
                "that returned status='needs_clarification'."
            )

        config = {
            "configurable": {"thread_id": tid},
            "recursion_limit": self.max_iterations * 30 + 50
        }

        memory_ctx = self.Memory.context_for_action(action.get("actionName", ""))
        kra_data = action.get("kraData")
        if kra_data and isinstance(kra_data, dict):
            aws_ctx = ""  # User already specified resources — no need for full inventory
            self.logger.info("Custom KRA payload detected — skipping full AWS context gathering")
        else:
            # Feed the action's own text to the Dynamic Context Builder so only
            # the AWS services this KRA touches are inventoried.
            _action_scope_text = " ".join(
                str(part)
                for part in (
                    action.get("actionName", ""),
                    action.get("actionDescription", ""),
                    action.get("service", ""),
                    *(action.get("steps") or []),
                )
            )
            aws_ctx = self._gather_aws_context(force_refresh=True) if not answers else ""

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
            import contextvars
            ctx = contextvars.Context()
            
            if answers:
                ctx.run(self.Graph.invoke, Command(resume=answers), config)
            else:
                ctx.run(
                    self.Graph.invoke,
                    {
                        "action": action,
                        "reference_folder": reference_folder or "",
                        "command_timeout": command_timeout,
                        "max_iterations": self.max_iterations,
                        "aws_permissions": aws_permissions or [],
                        "iteration": 1,
                        "records": [],
                        "feedback_summary": "",
                        "memory_context": memory_ctx,
                        "consecutive_same_error": 0,
                        "last_error_class": "",
                        "aws_context": aws_ctx,
                        "terraform_state_context": "",
                        "service_quotas_context": "",
                        "terraform_docs": "",
                        "terraform_docs_dict": {},
                        "permission_issues": [],
                        "caller_arn": "",

                        "analysis": None,
                        "clarification": None,
                        "generated_files": [],
                        "executable_steps": [],
                        "sandbox_path": "",
                        "existing_files": [],
                        "reference_files": [],
                        "input_sandbox_path": sandbox_path or "",
                        "generator_summary": "",

                        "validate_iteration": 0,
                        "validate_passed": False,
                        "validate_feedback": "",

                        "folder_contents": None,
                        "execution_plan": None,
                        "execution_results": [],
                        "success": False,
                        "executor_summary": "",

                        "pre_apply_results": [],
                        "plan_review": None,
                        "plan_review_iteration": 0,
                        "plan_review_precheck_failed": False,
                        "plan_review_skipped": False,

                        "final_status": "in_progress",
                        "final_summary": "",

                        # ── User-defined KRA payload ──
                        "kra_data": action.get("kraData"),
                    },
                    config
                )

            snapshot = ctx.run(self.Graph.get_state, config)

            if snapshot.tasks and any(t.interrupts for t in snapshot.tasks):
                interrupt_val = snapshot.tasks[0].interrupts[0].value
                questions_out = []
                hitl_payload = None
                
                # Handling list of dicts (e.g. from _execute_terraform_node)
                if isinstance(interrupt_val, list) and len(interrupt_val) > 0:
                    first_val = interrupt_val[0]
                    if isinstance(first_val, dict):
                        questions_out.append(str(first_val.get("message", "Awaiting approval")))
                        hitl_payload = first_val
                    else:
                        questions_out = [str(q) for q in interrupt_val]
                elif isinstance(interrupt_val, dict):
                    questions_out.append(str(interrupt_val.get("message", "Awaiting input")))
                    hitl_payload = interrupt_val
                elif isinstance(interrupt_val, str):
                    questions_out = [interrupt_val]
                else:
                    questions_out = [str(interrupt_val)]

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
                    questions=questions_out,
                    hitl_payload=hitl_payload,
                    summary=summary_msg,
                )

            final = snapshot.values
            records = [IterationRecord(**r) for r in final.get("records", [])]
            results = [ExecutionResult(**r) for r in final.get("execution_results", [])]
            final_status = final.get("final_status", "failed")
            sandbox_path_final: Optional[str] = final.get("sandbox_path") or None
            final_summary_text = final.get("final_summary") or final.get("executor_summary") or ""
            timings = final.get("timings", {})
            if timings:
                timing_str = "\n\n### Execution Timings\n"
                timing_str += "| Stage | Duration (s) |\n|---|---|\n"
                for stage, duration in timings.items():
                    timing_str += f"| {stage} | {duration:.2f} |\n"
                final_summary_text += timing_str
                self.logger.info("Execution Timings: %s", timings)

            jira_url = action.get("jiraUrl")
            skip_jira = action.get("skipJiraUpdate", False)
            if jira_url and final_summary_text and not skip_jira:
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
            try:
                snapshot = self.Graph.get_state(config)
                orphaned_sandbox = snapshot.values.get("sandbox_path") if snapshot.values else None
                if orphaned_sandbox:
                    self._cleanup_sandbox(orphaned_sandbox)
            except Exception:
                pass
            
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
#         command_timeout=300,
#     )
#     print("Pipeline Response:", json.dumps(response.model_dump(), indent=2))