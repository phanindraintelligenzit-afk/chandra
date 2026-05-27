"""
AWS Executor Agent
==================
Executes Python scripts or Terraform files from a specified folder.
Runs commands step by step, capturing output at each stage.

Input to RunPipeline:
    action            – dict with actionName and actionDescription
    executeFolder     – path to the folder containing generated scripts / tf files
    executableSteps   – list of ordered shell commands to run (from generator_agent output)
    command_timeout   – per-command timeout in seconds (default: 300 = 5 minutes)
                        Pass a dict to override per-step: {"terraform plan": 60, "default": 300}
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict, Union

from pydantic import BaseModel, Field
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage
from langchain_aws import ChatBedrockConverse
from langchain_community.agent_toolkits import FileManagementToolkit
from langgraph.graph import END, StateGraph

load_dotenv(override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("ExecutorAgent")

# Default per-command timeout (seconds). Override via RunPipeline(command_timeout=N).
DEFAULT_COMMAND_TIMEOUT = 300  # 5 minutes


# ── Pydantic models ────────────────────────────────────────────────

class ExecutionCommand(BaseModel):
    command: str = Field(description="Shell command to execute")
    description: str = Field(description="What this command does")
    working_dir: str = Field(
        default=".",
        description="Working directory relative to execute_folder ('.' for root)",
    )
    order: int = Field(description="Execution order (1 = first)")


class ExecutionPlan(BaseModel):
    execution_type: str = Field(description="'python' | 'terraform' | 'shell' | 'mixed'")
    commands: List[ExecutionCommand] = Field(description="Ordered list of commands to execute")
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


class AgentState(TypedDict):
    action: Dict[str, Any]
    executeFolder: str
    executableSteps: Optional[List[Dict]]   # List of {"description": str, "command": str}
    command_timeout: int                    # per-command timeout in seconds
    folder_contents: Optional[str]
    execution_plan: Optional[Dict]
    execution_results: List[Dict]
    summary: str
    success: bool


class ExecutorPipelineResponse(BaseModel):
    statusCode: int
    status: str = Field(description="'success' | 'failed' | 'error'")
    exception: Optional[str] = None
    thread_id: Optional[str] = None
    executeFolder: Optional[str] = None
    execution_results: Optional[List[ExecutionResult]] = None
    summary: Optional[str] = None
    success: Optional[bool] = None


# ── Agent ──────────────────────────────────────────────────────────

class ExecutorAgent:

    def __init__(self):
        logger.info("Initialising ExecutorAgent")
        try:
            self.Llm = ChatBedrockConverse(model_id=os.getenv("MODEL_NAME"))
            self.Graph = self._build_graph()
            logger.info("ExecutorAgent initialised successfully")
        except Exception as exc:
            logger.exception("Failed to initialise ExecutorAgent: %s", exc)
            raise

    # ── Nodes ──────────────────────────────────────────────────────

    def _scan_folder_node(self, state: AgentState) -> dict:
        execute_folder = state["executeFolder"]
        logger.info("Scanning folder: %s", execute_folder)

        folder_path = Path(execute_folder)
        if not folder_path.exists():
            raise FileNotFoundError(f"execute_folder not found: {execute_folder}")

        contents = sorted(
            str(p.relative_to(folder_path))
            for p in folder_path.rglob("*")
            if p.is_file()
        )
        folder_contents = "\n".join(contents) if contents else "(empty folder)"
        logger.info("Found %d file(s) in %s", len(contents), execute_folder)
        return {"folder_contents": folder_contents}

    def _plan_node(self, state: AgentState) -> dict:
        action = state["action"]
        execute_folder = state["executeFolder"]
        folder_contents = state["folder_contents"] or ""

        logger.info("=" * 80)
        logger.info("PLANNING EXECUTION STRATEGY")
        logger.info("=" * 80)

        if state.get("executableSteps"):
            logger.info("✓ Using %d executable steps from generator agent", len(state["executableSteps"]))
            logger.info("   Skipping LLM planning (steps already provided)")
            logger.info("-" * 80)
            for idx, step in enumerate(state["executableSteps"], 1):
                logger.info("   [%d] %s", idx, step.get("description", step.get("command", "")))
            logger.info("-" * 80)

            commands = [
                {
                    "command": step.get("command") or step.get("cmd") or "",
                    "description": step.get("description", ""),
                    "working_dir": ".",
                    "order": i + 1,  # always set from enumerate; never rely on incoming dict
                }
                for i, step in enumerate(state["executableSteps"])
            ]
            return {
                "execution_plan": {
                    "execution_type": "mixed",
                    "commands": commands,
                    "reasoning": "Steps provided by generator agent.",
                }
            }

        logger.info("Planning execution for action: %s", action.get("actionName"))
        logger.info("Invoking LLM to analyze folder contents and create plan...")

        toolkit = FileManagementToolkit(root_dir=execute_folder)
        tools_by_name = {t.name: t for t in toolkit.get_tools()}
        read_tool = tools_by_name["read_file"]

        readable_extensions = {".py", ".tf", ".sh", ".yaml", ".yml", ".json", ".tfvars"}
        file_previews: List[str] = []
        for rel_path in folder_contents.splitlines():
            rel_path = rel_path.strip()
            if not rel_path:
                continue
            if Path(rel_path).suffix in readable_extensions:
                try:
                    content = read_tool.invoke({"file_path": rel_path})
                    lines = content.splitlines()
                    preview = "\n".join(lines[:60])
                    if len(lines) > 60:
                        preview += f"\n... ({len(lines) - 60} more lines)"
                    file_previews.append(f"=== {rel_path} ===\n{preview}")
                except Exception:
                    pass

        file_context = "\n\n".join(file_previews) if file_previews else "(no readable source files)"

        prompt = f"""You are an AWS automation engineer. Create an execution plan for the files in this folder.

Action Name: {action["actionName"]}
Action Description: {action["actionDescription"]}
Steps: {json.dumps(action.get("steps") or [], indent=2)}

Files in execute_folder:
{folder_contents}

File contents preview:
{file_context}

Create a detailed execution plan:

1. execution_type — choose the best fit:
   - "python"    → only Python scripts present
   - "terraform" → only Terraform (.tf) files present
   - "shell"     → only shell scripts (.sh) present
   - "mixed"     → combination (e.g. Terraform infra + Python Lambda handler)

2. commands — ordered list of shell commands. Guidelines:
   Terraform workflow (always in this order):
     terraform init  →  terraform validate  →  terraform plan  →  terraform apply -auto-approve
   Python workflow:
     pip install -r requirements.txt (if requirements.txt exists)  →  python <script>.py [args]
   Shell scripts:
     chmod +x <script>.sh  →  ./<script>.sh
   Use the exact filenames from the listing above.

3. reasoning — one or two sentences.

Only include commands that are needed based on the actual files present."""

        try:
            structured_llm = self.Llm.with_structured_output(ExecutionPlan)
            plan: ExecutionPlan = structured_llm.invoke([HumanMessage(content=prompt)])

            logger.info("=" * 80)
            logger.info("✓ EXECUTION PLAN CREATED BY LLM")
            logger.info("=" * 80)
            logger.info("Execution Type: %s", plan.execution_type)
            logger.info("Commands Planned: %d", len(plan.commands))
            logger.info("Reasoning: %s", plan.reasoning)
            logger.info("-" * 80)
            for cmd in plan.commands:
                logger.info("[%d] %s", cmd.order, cmd.description)
                logger.info("    Command: %s", cmd.command)
            logger.info("=" * 80)

            return {"execution_plan": plan.model_dump()}
        except Exception as exc:
            logger.exception("✗ Execution planning failed: %s", exc)
            raise

    def _validate_command(self, command: str, execute_folder: str) -> tuple[bool, str]:
        """
        Validate command syntax before execution.
        Returns (is_valid, error_message).
        """
        base_path = Path(execute_folder).resolve()

        # Check for file references that don't exist
        if "-var-file=" in command:
            import re
            match = re.search(r'-var-file="?([^"\s]+)"?', command)
            if match:
                var_file = match.group(1)
                full_path = base_path / var_file
                if not full_path.exists():
                    return False, f"Variable file does not exist: {var_file}"

        return True, ""

    def _execute_node(self, state: AgentState) -> dict:
        execute_folder = state["executeFolder"]
        plan = state["execution_plan"]
        # Bug fix: use explicit key access; state.get() with `or` falsely falls back on 0.
        # LangGraph propagates all TypedDict keys so "command_timeout" is always present.
        _raw_timeout = state.get("command_timeout")
        timeout = _raw_timeout if isinstance(_raw_timeout, int) and _raw_timeout > 0 else DEFAULT_COMMAND_TIMEOUT

        if not plan or not plan.get("commands"):
            logger.error("No execution plan or commands found — skipping execution")
            return {"execution_results": [], "success": False}

        logger.info("=" * 80)
        logger.info(
            "EXECUTION STARTED: %d command(s) in %s  [timeout=%ds per command]",
            len(plan["commands"]),
            execute_folder,
            timeout,
        )
        logger.info("=" * 80)

        base_path = Path(execute_folder).resolve()
        results: List[Dict] = []
        total_commands = len(plan["commands"])

        for idx, cmd_info in enumerate(sorted(plan["commands"], key=lambda c: c.get("order", 9999)), 1):
            command = cmd_info["command"]
            description = cmd_info.get("description", "")
            relative_dir = cmd_info.get("working_dir", ".")

            cwd = (base_path / relative_dir).resolve()
            if not cwd.exists():
                cwd = base_path

            # Pre-execution validation
            is_valid, error_msg = self._validate_command(command, str(cwd))
            if not is_valid:
                logger.warning(
                    "[%d/%d] ✗ VALIDATION FAILED: %s",
                    idx, total_commands, description or command,
                )
                logger.warning("        Validation Error: %s", error_msg)
                result = {
                    "command": command,
                    "description": description,
                    "working_dir": str(cwd),
                    "stdout": "",
                    "stderr": f"Pre-execution validation failed: {error_msg}",
                    "return_code": -1,
                    "success": False,
                    "timed_out": False,
                    "timeout_seconds": timeout,
                }
                results.append(result)
                logger.error("=" * 80)
                logger.error(
                    "EXECUTION HALTED (VALIDATION FAILED): stopping remaining %d command(s)",
                    total_commands - idx,
                )
                logger.error("=" * 80)
                break

            logger.info("-" * 80)
            logger.info("[%d/%d] EXECUTING: %s", idx, total_commands, description or command)
            logger.info("        Command    : %s", command)
            logger.info("        Working Dir: %s", cwd)
            logger.info("        Timeout    : %ds", timeout)
            logger.info("-" * 80)

            timed_out = False
            try:
                # ── Bug fix: subprocess.run(shell=True, timeout=...) on Windows spawns
                # cmd.exe as parent; killing it leaves the child (terraform) running as
                # an orphan and never raises TimeoutExpired.
                # Popen + communicate(timeout) + proc.kill() works correctly on all platforms.
                proc = subprocess.Popen(
                    command,
                    shell=True,
                    cwd=str(cwd),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                try:
                    stdout_data, stderr_data = proc.communicate(timeout=timeout)
                except subprocess.TimeoutExpired:
                    # Kill the entire process tree, not just the shell wrapper.
                    # On Windows, proc.kill() only kills cmd.exe; the grandchild
                    # (e.g. terraform) survives. Use taskkill /T to kill the tree.
                    import sys as _sys
                    if _sys.platform == "win32":
                        import subprocess as _sp
                        _sp.run(
                            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                            capture_output=True,
                        )
                    else:
                        import os as _os, signal as _signal
                        try:
                            _os.killpg(_os.getpgid(proc.pid), _signal.SIGKILL)
                        except Exception:
                            proc.kill()
                    try:
                        stdout_data, stderr_data = proc.communicate(timeout=10)
                    except subprocess.TimeoutExpired:
                        stdout_data, stderr_data = "", ""
                    raise  # re-raise so the outer except subprocess.TimeoutExpired block handles it

                success = proc.returncode == 0

                # Log full output if truncated (for debugging)
                if stdout_data and len(stdout_data) > 10000:
                    logger.debug(f"Full stdout (first 20KB): {stdout_data[:20000]}")
                if stderr_data and len(stderr_data) > 5000:
                    logger.debug(f"Full stderr (first 10KB): {stderr_data[:10000]}")

                result = {
                    "command": command,
                    "description": description,
                    "working_dir": str(cwd),
                    "stdout": (stdout_data or "")[:10000],  # Increased from 5000
                    "stderr": (stderr_data or "")[:5000],   # Increased from 2000
                    "return_code": proc.returncode,
                    "success": success,
                    "timed_out": False,
                    "timeout_seconds": timeout,
                }

                if success:
                    logger.info(
                        "[%d/%d] ✓ SUCCESS: %s completed (rc=%d)",
                        idx, total_commands, description or command, proc.returncode,
                    )
                    if stdout_data:
                        logger.info(
                            "        Output: %s%s",
                            stdout_data[:300],
                            "..." if len(stdout_data) > 300 else "",
                        )
                else:
                    logger.warning(
                        "[%d/%d] ✗ FAILED: %s (rc=%d)",
                        idx, total_commands, description or command, proc.returncode,
                    )
                    logger.warning("        Error: %s", stderr_data[:500])

            except subprocess.TimeoutExpired:
                timed_out = True
                timeout_msg = (
                    f"Command '{command}' timed out after {timeout} seconds. "
                    f"This step ({description}) did not complete within the allowed time. "
                    "For Terraform commands, this usually means the provider tried to connect "
                    "to AWS but credentials are missing or the network is unreachable. "
                    "Consider adding explicit provider timeouts, using -var flags for credentials, "
                    "or skipping long-running steps in validation."
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
                logger.error(
                    "[%d/%d] ✗ TIMEOUT: %s exceeded %ds",
                    idx, total_commands, description or command, timeout,
                )
                logger.error("        %s", timeout_msg)

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
                logger.exception(
                    "[%d/%d] ✗ EXCEPTION: %s - %s",
                    idx, total_commands, description or command, exc,
                )

            results.append(result)

            if not result["success"]:
                halt_reason = "TIMEOUT" if timed_out else "COMMAND FAILED"
                logger.error("=" * 80)
                logger.error(
                    "EXECUTION HALTED (%s): stopping remaining %d command(s)",
                    halt_reason,
                    total_commands - idx,
                )
                logger.error("=" * 80)
                break

        overall_success = (
            bool(results)
            and all(r["success"] for r in results)
            and len(results) == len(plan["commands"])
        )

        logger.info("=" * 80)
        if overall_success:
            logger.info(
                "✓ EXECUTION COMPLETED SUCCESSFULLY: All %d command(s) executed", len(results)
            )
        else:
            timed_out_cmds = [r for r in results if r.get("timed_out")]
            if timed_out_cmds:
                logger.warning(
                    "✗ EXECUTION COMPLETED WITH TIMEOUT(S): %d command(s) timed out",
                    len(timed_out_cmds),
                )
            else:
                logger.warning(
                    "✗ EXECUTION COMPLETED WITH FAILURES: %d/%d command(s) succeeded",
                    sum(1 for r in results if r["success"]),
                    len(results),
                )
        logger.info("=" * 80)

        return {"execution_results": results, "success": overall_success}

    def _report_node(self, state: AgentState) -> dict:
        action = state["action"]
        results = state.get("execution_results", [])
        success = state.get("success", False)
        timeout = state.get("command_timeout") or DEFAULT_COMMAND_TIMEOUT

        logger.info("=" * 80)
        logger.info("GENERATING EXECUTION REPORT")
        logger.info("=" * 80)
        logger.info("Action: %s", action.get("actionName"))
        logger.info("Overall Status: %s", "SUCCESS" if success else "FAILED")
        logger.info("Commands Executed: %d", len(results))
        logger.info("Commands Succeeded: %d", sum(1 for r in results if r["success"]))
        logger.info("Commands Failed: %d", sum(1 for r in results if not r["success"]))

        # Build a rich results text — flag timeouts explicitly so the LLM summary is specific
        results_text_parts = []
        for r in results:
            status_label = "SUCCESS" if r["success"] else ("TIMED OUT" if r.get("timed_out") else "FAILED")
            parts = [
                f"Command    : {r['command']}",
                f"Status     : {status_label} (rc={r['return_code']})",
            ]
            if r.get("timed_out"):
                parts.append(
                    f"Timeout    : exceeded {r.get('timeout_seconds', timeout)}s limit — "
                    "command was killed before it could complete"
                )
            parts.append(f"Output     : {r['stdout'][:500] or '(none)'}")
            parts.append(f"Errors     : {r['stderr'][:300] or '(none)'}")
            results_text_parts.append("\n".join(parts))

        results_text = "\n\n".join(results_text_parts) or "(no commands were executed)"

        prompt = f"""Summarize the execution of this AWS automation action in 2–4 sentences.

Action: {action["actionName"]}
Description: {action["actionDescription"]}
Overall Success: {success}
Per-command timeout used: {timeout}s

Execution Results:
{results_text}

Rules:
- If any command TIMED OUT, explicitly state which command timed out and how long the timeout was.
  Example: "The 'terraform plan' step timed out after {timeout} seconds, likely due to missing AWS
  credentials or network unreachability."
- If there were validation/argument errors, name the exact argument and the correct fix.
- Cover: what was executed, whether it succeeded, any important resource identifiers created,
  and concrete next steps if there were failures."""

        logger.info("Invoking LLM to generate summary...")
        try:
            response = self.Llm.invoke([HumanMessage(content=prompt)])
            summary = response.content
            logger.info("✓ LLM summary generated successfully")
        except Exception as exc:
            logger.warning("✗ LLM summary failed, using fallback: %s", exc)
            timed_out_cmds = [r for r in results if r.get("timed_out")]
            if timed_out_cmds:
                names = ", ".join(f"'{r['command']}'" for r in timed_out_cmds)
                summary = (
                    f"Execution of '{action['actionName']}' failed: {names} timed out after "
                    f"{timeout}s. This typically indicates missing AWS credentials or network "
                    "issues. Ensure AWS credentials are configured and retry."
                )
            else:
                succeeded = sum(1 for r in results if r["success"])
                summary = (
                    f"Executed {succeeded}/{len(results)} command(s) for '{action['actionName']}'. "
                    + ("All steps completed successfully." if success else "Some commands failed — review stderr for details.")
                )

        logger.info("=" * 80)
        logger.info("REPORT GENERATION COMPLETED")
        logger.info("=" * 80)
        return {"summary": summary}

    # ── Graph ──────────────────────────────────────────────────────

    def _build_graph(self):
        logger.info("Building LangGraph execution pipeline")
        try:
            builder = StateGraph(AgentState)
            builder.add_node("scan_folder", self._scan_folder_node)
            builder.add_node("plan", self._plan_node)
            builder.add_node("execute", self._execute_node)
            builder.add_node("report", self._report_node)

            builder.set_entry_point("scan_folder")
            builder.add_edge("scan_folder", "plan")
            builder.add_edge("plan", "execute")
            builder.add_edge("execute", "report")
            builder.add_edge("report", END)

            graph = builder.compile()
            logger.info("Execution graph compiled successfully")
            return graph
        except Exception as exc:
            logger.exception("Failed to build execution graph: %s", exc)
            raise

    # ── Public API ─────────────────────────────────────────────────

    def RunPipeline(
        self,
        action: Dict[str, Any],
        executeFolder: str,
        executableSteps: Optional[List[Dict]] = None,
        command_timeout: int = DEFAULT_COMMAND_TIMEOUT,
    ) -> ExecutorPipelineResponse:
        """
        Run the execution pipeline end-to-end.

        Args:
            action:           Dict with 'actionName' and 'actionDescription'
            executeFolder:    Path to folder containing generated files
            executableSteps:  List of ordered steps from generator_agent, each with
                              'description' and 'command'. If None, LLM plans from
                              folder contents.
            command_timeout:  Per-command timeout in seconds (default: 300 = 5 minutes).
                              If a command exceeds this, it is killed and its error
                              message describes the timeout so the generator can fix it.
        """
        tid = str(uuid.uuid4())

        logger.info("")
        logger.info("╔" + "=" * 78 + "╗")
        logger.info("║" + " " * 78 + "║")
        logger.info("║ " + "EXECUTOR AGENT PIPELINE STARTED".ljust(76) + " ║")
        logger.info("║" + " " * 78 + "║")
        logger.info("╚" + "=" * 78 + "╝")
        logger.info("")
        logger.info("Action             : %s", action.get("actionName"))
        logger.info("Description        : %s", action.get("actionDescription"))
        logger.info("Execute Folder     : %s", executeFolder)
        logger.info("Command Timeout    : %ds per command", command_timeout)
        logger.info("Executable Steps   : %s", "Yes" if executableSteps else "No (LLM will plan)")
        if executableSteps:
            logger.info("  ├─ Total Steps   : %d", len(executableSteps))
        logger.info("Thread ID          : %s", tid)
        logger.info("")

        try:
            final = self.Graph.invoke(
                {
                    "action": action,
                    "executeFolder": executeFolder,
                    "executableSteps": executableSteps,
                    "command_timeout": command_timeout,
                    "folder_contents": None,
                    "execution_plan": None,
                    "execution_results": [],
                    "summary": "",
                    "success": False,
                }
            )

            results = [ExecutionResult(**r) for r in final.get("execution_results", [])]
            overall_success = final.get("success", False)

            logger.info("")
            logger.info("╔" + "=" * 78 + "╗")
            logger.info("║" + " " * 78 + "║")
            if overall_success:
                logger.info("║ " + "✓ PIPELINE COMPLETED SUCCESSFULLY".ljust(76) + " ║")
            else:
                timed_out_count = sum(1 for r in results if r.timed_out)
                if timed_out_count:
                    logger.info(
                        "║ " + f"✗ PIPELINE FAILED — {timed_out_count} COMMAND(S) TIMED OUT".ljust(76) + " ║"
                    )
                else:
                    logger.info("║ " + "✗ PIPELINE COMPLETED WITH FAILURES".ljust(76) + " ║")
            logger.info("║" + " " * 78 + "║")
            logger.info("╚" + "=" * 78 + "╝")
            logger.info("")
            logger.info("Summary Statistics:")
            logger.info("  ├─ Total Commands  : %d", len(results))
            logger.info("  ├─ Succeeded       : %d", sum(1 for r in results if r.success))
            logger.info("  ├─ Failed          : %d", sum(1 for r in results if not r.success))
            logger.info("  ├─ Timed Out       : %d", sum(1 for r in results if r.timed_out))
            logger.info("  └─ Overall Status  : %s", "SUCCESS" if overall_success else "FAILED")
            logger.info("")

            return ExecutorPipelineResponse(
                statusCode=200 if overall_success else 207,
                status="success" if overall_success else "failed",
                thread_id=tid,
                executeFolder=executeFolder,
                execution_results=results,
                summary=final.get("summary"),
                success=overall_success,
            )

        except Exception as exc:
            logger.exception("Unexpected error in RunPipeline: %s", exc)
            return ExecutorPipelineResponse(
                statusCode=500,
                status="error",
                exception=str(exc),
                thread_id=tid,
            )


# if __name__ == "__main__":
#     executor = ExecutorAgent()
#
#     response = executor.RunPipeline(
#         action={
#             "actionName": "Deploy Production RDS Instance with Terraform",
#             "actionDescription": "Deploy a single-AZ PostgreSQL RDS instance..."
#         },
#         executeFolder="sandbox_123",
#         command_timeout=300,   # 5 minutes per command
#         executableSteps=[
#             {"description": "Initialize Terraform",          "command": "terraform -chdir=infrastructure init"},
#             {"description": "Validate Terraform configuration", "command": "terraform -chdir=infrastructure validate"},
#             {"description": "Plan Terraform deployment",     "command": "terraform -chdir=infrastructure plan"},
#             {"description": "Apply Terraform configuration", "command": "terraform -chdir=infrastructure apply -auto-approve"},
#         ]
#     )
#     print(response.model_dump_json(indent=2))