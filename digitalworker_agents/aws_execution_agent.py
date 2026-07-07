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

class AgentState(TypedDict):
    action: Dict[str, Any]
    reference_folder: str
    command_timeout: int
    max_iterations: int
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

    pre_apply_results: List[Dict]
    plan_review: Optional[Dict]
    plan_review_iteration: int
    plan_review_precheck_failed: bool
    plan_review_skipped: bool

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

class ExecutionAgents:

    def __init__(self, max_iterations: int = MAX_ITERATIONS, memory_path: Optional[str] = None, job_id: Optional[str] = None) -> None:
        self.max_iterations = max_iterations
        self.job_id = job_id or "default"
        self._quotas_cache = {}
        self._docs_cache = {}
        self.logger = logging.getLogger(f"ExecutionAgents.{self.job_id}")
        self.logger.propagate = True
        
        os.makedirs("logs", exist_ok=True)
        fh = logging.FileHandler(f"logs/{self.job_id}.log", mode='a', encoding='utf-8')
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s"))
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

    def _run_mcp_aws_command(self, command: str) -> dict:
        import os
        import json
        import asyncio
        from langchain_mcp_adapters.client import MultiServerMCPClient
        
        server_config = {
            "aws_api": {
                "command": "uvx",
                "args": ["awslabs.aws-api-mcp-server@latest"],
                "env": {
                    "AWS_REGION": os.getenv("AWS_REGION", "us-east-1"),
                    "AWS_DEFAULT_REGION": os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
                    **({"AWS_ACCESS_KEY_ID": os.getenv("AWS_ACCESS_KEY_ID")} if os.getenv("AWS_ACCESS_KEY_ID") else {}),
                    **({"AWS_SECRET_ACCESS_KEY": os.getenv("AWS_SECRET_ACCESS_KEY")} if os.getenv("AWS_SECRET_ACCESS_KEY") else {}),
                    **({"AWS_SESSION_TOKEN": os.getenv("AWS_SESSION_TOKEN")} if os.getenv("AWS_SESSION_TOKEN") else {}),
                    **({"AWS_PROFILE": os.getenv("AWS_PROFILE")} if os.getenv("AWS_PROFILE") else {}),
                },
                "transport": "stdio",
            },
        }

        async def _run():
            try:
                client = MultiServerMCPClient(server_config)
                tools = await client.get_tools(server_name="aws_api")
                aws_tool = next((t for t in tools if t.name == "call_aws"), None)
                if aws_tool:
                    res = await aws_tool.ainvoke({"cli_command": command})
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
                return {}
            except Exception as exc:
                self.logger.warning(f"MCP AWS CLI execution failed for '{command}': {exc}")
                return {}

        return asyncio.run(_run())

    def _run_mcp_terraform_docs(self, provider_name: str, provider_namespace: str, service_slug: str, provider_document_type: str) -> str:
        import os
        import json
        import asyncio
        import re
        from langchain_mcp_adapters.client import MultiServerMCPClient
        
        _SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
        TERRAFORM_MCP_BINARY = os.getenv("TERRAFORM_MCP_BINARY", os.path.join(os.path.dirname(_SCRIPT_DIR), "terraform", "terraform-mcp-server.exe"))
        
        server_config = {
            "terraform": {
                "command": TERRAFORM_MCP_BINARY,
                "args": ["stdio"],
                "transport": "stdio",
            }
        }
        
        async def _run():
            try:
                client = MultiServerMCPClient(server_config)
                tools = await client.get_tools(server_name="terraform")
                search_tool = next((t for t in tools if t.name == "search_providers"), None)
                details_tool = next((t for t in tools if t.name == "get_provider_details"), None)
                
                if search_tool and details_tool:
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
                    if match:
                        provider_doc_id = match.group(1)
                        details_raw = await details_tool.ainvoke({"provider_doc_id": provider_doc_id})
                        if isinstance(details_raw, list) and details_raw and isinstance(details_raw[0], dict) and "text" in details_raw[0]:
                            return details_raw[0]["text"]
                        return details_raw if isinstance(details_raw, str) else json.dumps(details_raw, default=str)
                return ""
            except Exception as exc:
                self.logger.warning(f"MCP Terraform docs failed: {exc}")
                return ""

        return asyncio.run(_run())

    def _gather_aws_context(self) -> str:
        try:
            import os
            region = os.getenv("AWS_DEFAULT_REGION") or os.getenv("AWS_REGION") or "us-east-1"
            lines = ["AWS ACCOUNT GROUNDING (live, fetched at pipeline start — treat as ground truth):"]

            identity = self._run_mcp_aws_command("aws sts get-caller-identity")
            if identity:
                lines.append(f"  Account ID : {identity.get('Account')}")
                lines.append(f"  Caller ARN : {identity.get('Arn')}")
            else:
                lines.append("  Account ID : (unavailable — could not call sts:GetCallerIdentity via MCP)")

            lines.append(f"  Region     : {region}")

            vpcs = self._run_mcp_aws_command(f"aws ec2 describe-vpcs --filters Name=is-default,Values=true --region {region}")
            default_vpc = (vpcs.get("Vpcs") or [{}])[0].get("VpcId") if vpcs else None
            
            if default_vpc:
                lines.append(f"  Default VPC: {default_vpc}")
                subnets = self._run_mcp_aws_command(f"aws ec2 describe-subnets --filters Name=vpc-id,Values={default_vpc} --region {region}")
                subnet_ids = [s["SubnetId"] for s in (subnets.get("Subnets") or [])] if subnets else []
                if subnet_ids:
                    lines.append(f"  Default VPC subnets: {', '.join(subnet_ids)}")
            else:
                lines.append("  Default VPC: (none found in this account/region)")

            azs = self._run_mcp_aws_command(f"aws ec2 describe-availability-zones --region {region}")
            az_names = [az["ZoneName"] for az in (azs.get("AvailabilityZones") or []) if az["State"] == "available"] if azs else []
            if az_names:
                lines.append(f"  Available AZs: {', '.join(az_names)}")

            key_pairs = self._run_mcp_aws_command(f"aws ec2 describe-key-pairs --region {region}")
            kp_names = [kp["KeyName"] for kp in (key_pairs.get("KeyPairs") or [])] if key_pairs else []
            if kp_names:
                lines.append(f"  Existing Key Pairs: {', '.join(kp_names)}")
            else:
                lines.append("  Existing Key Pairs: (none found, you must generate one if needed)")

            if default_vpc:
                sgs = self._run_mcp_aws_command(f"aws ec2 describe-security-groups --filters Name=vpc-id,Values={default_vpc} --region {region}")
                sg_info = [f"{sg['GroupName']} ({sg['GroupId']})" for sg in (sgs.get("SecurityGroups") or [])] if sgs else []
                if sg_info:
                    lines.append(f"  Existing Security Groups: {', '.join(sg_info)}")
                else:
                    lines.append("  Existing Security Groups: (none found)")

            zones = self._run_mcp_aws_command(f"aws route53 list-hosted-zones --region {region}")
            zone_info = [f"{z['Name']} (ID: {z['Id']})" for z in (zones.get("HostedZones") or []) if not z.get("Config", {}).get("PrivateZone")] if zones else []
            if zone_info:
                lines.append(f"  Public Route53 Zones: {', '.join(zone_info)}")
            else:
                lines.append("  Public Route53 Zones: (none found)")

            lines.append(
                "\nIf an ID is provided in the context above (VPC, Subnets, Security Groups, Key Pairs, Route53 Zones), hardcode it directly in your Terraform code to avoid data source filter errors. "
                "ONLY use Terraform data sources (e.g. data \"aws_vpc\") if the required resource is marked as '(none found)' or is missing from the context.\n"
                "\nTERRAFORM GOLDEN RULES:\n"
                "1. S3 Buckets: Names must be globally unique. Always use random_id or random_pet to append a suffix to bucket names.\n"
                "2. IAM Roles/Policies: Always use name_prefix instead of name to avoid conflicts with existing roles.\n"
                "3. EC2/RDS Security Groups: Prefer using existing security groups if they match your needs, or use name_prefix when creating new ones.\n"
                "4. Circular Dependencies: Never make a Security Group depend on an EC2 instance's IP if the EC2 instance also depends on that Security Group.\n"
                "5. Hardcoding: Hardcode environment IDs (like VPCs) *only* if they are provided in the context above. NEVER hardcode full ARNs or Regions (use data.aws_caller_identity.current and data.aws_region.current instead).\n"
                "6. Stateful Resources: For databases (RDS, DynamoDB) and storage (S3), always set lifecycle { prevent_destroy = true } unless instructed otherwise.\n"
                "7. Provider Version: Use the required_providers block to specify hashicorp/aws version ~> 5.0 to ensure modern syntax is supported.\n"
                "8. Local Files: NEVER use shell commands (like jq, echo, or icacls) to save files or parse Terraform output. Use the Terraform local_file resource instead."
            )
            ctx_str = "\n".join(lines)
            self.logger.info("Dynamic Grounding: _gather_aws_context output:\n%s", ctx_str)
            return ctx_str
        except Exception as exc:
            self.logger.warning("aws_context.failed: %s", exc)
            return ""

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

Action Name: {action["actionName"]}
Action Description: {action["actionDescription"]}
Steps: {json.dumps(action.get("steps") or [], indent=2)}{ref_ctx}{existing_ctx}{feedback_ctx}{memory_section}{aws_section}

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

7. aws_services_involved — List of AWS service codes involved (e.g. ["ec2", "lambda"]). This is used to fetch service quotas.

8. expected_resources — Exact Terraform resource block names you plan to use (e.g. ["aws_instance", "aws_lambda_function"]). This is CRITICAL for fetching documentation.

9. reasoning — Brief justification. If agent memory above contains lessons for this action,
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
        aws_ctx = state.get("aws_context") or ""
        terraform_state_ctx = state.get("terraform_state_context") or ""
        service_quotas_context = state.get("service_quotas_context") or ""
        terraform_docs_context = state.get("terraform_docs") or ""
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
{aws_section}
{service_quotas_context}
{terraform_docs_context}
{state_section}
{validate_section}
{plan_review_section}
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
  Include all steps needed: terraform init → terraform validate → terraform plan
  → terraform apply -auto-approve → then run terraform output for every output declared in
  outputs.tf (see "MANDATORY OUTPUTS" above, derived from this action's actual resources)
  so the user gets every useful detail in the final summary. Do not run `terraform output`
  for names that don't exist in outputs.tf, and don't omit any that do. The terraform plan
  step MUST be written as `terraform plan -out=tfplan` (not just `terraform plan`) so the
  plan can be reviewed before apply.

RULE 8 — SELF-VALIDATING TERRAFORM (native checks, not just data sources):
  Where it materially reduces risk of a bad apply, add Terraform's own
  validation primitives so problems surface at `terraform plan` instead of
  `terraform apply`:
  - variable "validation" blocks for any variable with a constrained valid
    range or format (e.g. reject empty strings, validate CIDR format, ensure
    a count variable is >= 0).
  - resource-level `lifecycle {{ precondition {{ ... }} }}` / `postcondition`
    blocks for assumptions the config relies on but can't express as plain
    HCL constraints (e.g. "the chosen AMI must exist in this region",
    "the S3 bucket name must be globally available" — where a data source
    result can be checked).
  - top-level `check` blocks (Terraform 1.5+) for cross-resource invariants
    that don't map to a single resource (e.g. "the RDS instance's subnet
    group must span at least 2 AZs").
  Do not add validation blocks reflexively to every variable — only where a
  bad value would otherwise fail late (at apply, or worse, silently succeed
  with wrong behavior). A single well-placed precondition beats five
  boilerplate ones.

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
        input_path = state.get("input_sandbox_path") or ""
        if input_path:
            sandbox_dir = str(Path(input_path).resolve())
        else:
            sandbox_dir = str(Path(f"aws_executed_files/sandbox_{secrets.token_hex(6)}").resolve())

        Path(sandbox_dir).mkdir(parents=True, exist_ok=True)
        self.logger.info("Using sandbox directory: %s", sandbox_dir)

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
                    "terraform init -input=false", cwd=sandbox_dir, timeout=180
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
            self.logger.info("[validate] ✓ static checks passed (attempt %d)", validate_iteration)
        else:
            self.logger.warning(
                "[validate] ✗ static checks failed (attempt %d/%d)",
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
   Terraform: terraform init → terraform validate → terraform plan -out=tfplan →
              terraform apply -auto-approve tfplan → terraform output (always run this last
              to display connection info, IDs, etc.)
   Python:    pip install -r requirements.txt (if present) → python <script>.py [args]
   Shell:     chmod +x <script>.sh → ./<script>.sh

3. reasoning — one or two sentences.

Only include commands needed for the actual files present."""

        try:
            structured_llm = self.Llm.with_structured_output(ExecutionPlan)
            plan: ExecutionPlan = structured_llm.invoke([HumanMessage(content=prompt)])
            self.logger.info("✓ EXECUTION PLAN — type=%s  commands=%d", plan.execution_type, len(plan.commands))
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
            command: str = cmd_info["command"]
            description: str = cmd_info.get("description", "")
            relative_dir: str = cmd_info.get("working_dir", ".")

            cwd = (base_path / relative_dir).resolve()
            if not cwd.exists():
                cwd = base_path

            is_valid, error_msg = self._validate_command(command, str(cwd))
            if not is_valid:
                self.logger.warning("✗ VALIDATION FAILED: %s — %s", description or command, error_msg)
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
                    self.logger.info("✓ SUCCESS: %s (rc=%d)", description or command, tool_output["return_code"])
                else:
                    self.logger.warning("✗ FAILED: %s (rc=%d)", description or command, tool_output["return_code"])
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
                self.logger.error("✗ TIMEOUT: %s exceeded %ds", description or command, timeout)
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
                self.logger.exception("✗ EXCEPTION in '%s': %s", description or command, exc)

            results.append(result)
            if not result["success"]:
                halted = True
                break

        return results, halted

    def _plan_review_node(self, state: AgentState) -> dict:
        """
        Run only the pre-apply commands (init/validate/plan) for real, capture
        the resulting plan, and have the LLM sanity-check it against the
        requested action BEFORE any apply command runs. Catches "ran fine but
        did the wrong thing" failures that a bare exit-code check would miss.
        """
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

        pre_apply_cmds: List[Dict] = []
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
            plan_json_text = (show_result["stdout"] or "")[:12000]
        except Exception as exc:
            self.logger.warning("[plan_review] terraform show failed: %s", exc)
            return {"pre_apply_results": results, "plan_review_skipped": True}

        if not plan_json_text.strip():
            return {"pre_apply_results": results, "plan_review_skipped": True}

        action = state["action"]
        review_prompt = f"""Review this Terraform plan against what was requested.

Action Name: {action.get("actionName")}
Action Description: {action.get("actionDescription")}

Plan (terraform show -json, truncated):
{plan_json_text}

Determine:
1. matches_intent — does the set of resource changes plausibly match the requested action?
2. destroy_or_replace_detected — is there any destroy or replace action that looks unexpected
   given the request (a fresh deploy should have no destroys; an update might have a few
   deliberate replaces, but flag anything that looks like collateral damage)?
3. concerns — short list of specific issues, if any (empty list if the plan looks correct).
4. reasoning — one or two sentences."""

        try:
            structured_llm = self.Llm.with_structured_output(PlanReview)
            review: PlanReview = structured_llm.invoke([HumanMessage(content=review_prompt)])
            review_dict = review.model_dump()
            self.logger.info(
                "[plan_review] matches_intent=%s destroy_or_replace=%s concerns=%s",
                review.matches_intent, review.destroy_or_replace_detected, review.concerns,
            )
        except Exception as exc:
            self.logger.warning("[plan_review] LLM review failed, proceeding without review: %s", exc)
            return {"pre_apply_results": results, "plan_review_skipped": True}

        flagged = (not review.matches_intent) or review.destroy_or_replace_detected
        next_iteration = state.get("plan_review_iteration", 0) + (1 if flagged else 0)

        return {
            "pre_apply_results": results,
            "plan_review": review_dict,
            "plan_review_iteration": next_iteration,
            "plan_review_skipped": False,
            "plan_review_precheck_failed": False,
        }

    @staticmethod
    def _route_after_plan_review(state: AgentState) -> str:
        if state.get("plan_review_precheck_failed"):
            return "precheck_failed"
        if state.get("plan_review_skipped"):
            return "proceed"
        review = state.get("plan_review") or {}
        flagged = (not review.get("matches_intent", True)) or review.get("destroy_or_replace_detected", False)
        if not flagged:
            return "proceed"
        if state.get("plan_review_iteration", 0) >= PLAN_REVIEW_MAX_RETRIES:
            self_logger = logging.getLogger("ExecutionAgents")
            self_logger.warning(
                "[plan_review] exhausted %d review retries — failing the precheck "
                "rather than proceeding with a dangerously flagged plan.",
                PLAN_REVIEW_MAX_RETRIES,
            )
            return "precheck_failed"
        return "retry_generate"

    def _plan_review_precheck_failed_node(self, state: AgentState) -> dict:
        """init/validate/plan itself failed during the review pass — treat exactly
        like a normal execution failure and let report/record_iteration handle it."""
        results = state.get("pre_apply_results") or []
        return {"execution_results": results, "success": False}

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

        new_results, _halted = self._run_commands(commands_to_run, base_path, timeout)
        results: List[Dict] = list(pre_apply_results) + new_results

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
{outputs_rule}{plan_review_rule}"""

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
            self._banner(f"✓ PIPELINE COMPLETED SUCCESSFULLY on iteration {iteration}", char="═")
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

    def _gather_docs_and_quotas_node(self, state: AgentState) -> dict:
        check_cancelled()
        analysis = state.get("analysis") or {}
        services = analysis.get("aws_services_involved") or []
        resources = analysis.get("expected_resources") or []
        
        docs_context = ""
        if resources:
            docs_context += "\nTERRAFORM DOCUMENTATION (Argument References):\n"
            import re
            for res_type in resources:
                if res_type in self._docs_cache:
                    docs_context += self._docs_cache[res_type]
                    continue
                if not res_type.startswith("aws_"):
                    continue
                
                try:
                    markdown = self._run_mcp_terraform_docs(
                        provider_name="aws",
                        provider_namespace="hashicorp",
                        service_slug=res_type,
                        provider_document_type="resources"
                    )
                        
                    if markdown:
                        res_ctx = f"\n--- {res_type} Documentation ---\n{markdown}\n"
                    else:
                        res_ctx = f"\n--- {res_type} ---\nNo docs returned from MCP.\n"
                    
                    self._docs_cache[res_type] = res_ctx
                    docs_context += res_ctx
                except Exception as e:
                    self.logger.warning("Failed to fetch TF docs for %s: %s", res_type, e)

        self.logger.info(
            "Dynamic Grounding: Analyzer identified services=%s and resources=%s",
            services, resources
        )
        self.logger.info(
            "Dynamic Grounding: Fetched docs for %d resources.",
            len(resources)
        )
        if docs_context:
            self.logger.info("Dynamic Grounding: Docs context output:\n%s", docs_context)
        
        return {
            "service_quotas_context": "",
            "terraform_docs": docs_context
        }

    @staticmethod
    def _route_after_analysis(state: AgentState) -> str:
        if state["analysis"]["needs_clarification"]:
            return "hitl"
        return "gather_docs"

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
        builder.add_node("execute", self._execute_node)
        builder.add_node("report", self._report_node)

        builder.add_node("record_iteration", self._record_iteration_node)
        builder.add_node("mid_run_hitl", self._mid_run_hitl_node)
        builder.add_node("save_memory_success", self._save_memory_node)
        builder.add_node("save_memory_fail", self._save_memory_node)

        builder.set_entry_point("read_existing")
        builder.add_edge("read_existing", "read_reference")
        builder.add_edge("read_reference", "analyze")
        builder.add_conditional_edges(
            "analyze",
            self._route_after_analysis,
            {"hitl": "hitl", "gather_docs": "gather_docs"},
        )
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
                "proceed": "execute",
                "retry_generate": "generate",
                "precheck_failed": "plan_review_precheck_failed",
            },
        )
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
    ) -> PipelineResponse:
        """
        Run the generate → execute → (retry on failure) loop until success or
        max_iterations is exhausted, all within a single agent / single graph.

        Pass ``answers`` (non-empty list) together with the original ``thread_id``
        to resume either a pre-execution clarification pause OR a mid-run HITL pause.
        Both pause types return statusCode=202 / status='needs_clarification'.
        """
        tid = thread_id or str(uuid.uuid4())

        if answers and not thread_id:
            raise ValueError(
                "thread_id is required when resuming with answers. "
                "Pass the thread_id returned from the original RunPipeline call "
                "that returned status='needs_clarification'."
            )

        config = {"configurable": {"thread_id": tid}}

        memory_ctx = self.Memory.context_for_action(action.get("actionName", ""))
        aws_ctx = self._gather_aws_context() if not answers else ""

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
                        "last_error_class": "",
                        "aws_context": aws_ctx,
                        "terraform_state_context": "",

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
                    },
                    config=config,
                )

            snapshot = self.Graph.get_state(config)

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