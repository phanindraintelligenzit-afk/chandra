"""
AWS Orchestrator Agent
======================
Combines GeneratorAgent and ExecutorAgent into a self-healing loop.

Now supports reference_folder for consistent code style across generations.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field
from dotenv import load_dotenv

# ── Import both agents ─────────────────────────────────────────────
from digitalworker_agents.generator_agent import GeneratorAgent, GeneratorPipelineResponse
from digitalworker_agents.executor_agent import ExecutorAgent, ExecutorPipelineResponse

load_dotenv(override=True)

logging.basicConfig(
    filename="orchestrator.log",
    filemode="w",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("OrchestratorAgent")

MAX_ITERATIONS = 5


# ── Response model ─────────────────────────────────────────────────

class IterationRecord(BaseModel):
    iteration: int
    generator_status: str
    executor_status: str
    executor_success: bool
    feedback_used: Optional[str] = None
    sandbox_path: Optional[str] = None


class OrchestratorResponse(BaseModel):
    statusCode: int
    status: str = Field(description="'success' | 'failed' | 'error' | 'needs_clarification'")
    exception: Optional[str] = None
    thread_id: str
    sandbox_path: Optional[str] = None
    iterations_used: int = 0
    iterations: List[IterationRecord] = Field(default_factory=list)
    final_executor_response: Optional[Dict[str, Any]] = None
    summary: Optional[str] = None
    questions: Optional[List[str]] = None


# ── Orchestrator ───────────────────────────────────────────────────

class OrchestratorAgent:

    def __init__(self, max_iterations: int = MAX_ITERATIONS):
        self.max_iterations = max_iterations
        logger.info("Initialising OrchestratorAgent (max_iterations=%d)", max_iterations)
        self.generator = GeneratorAgent()
        self.executor = ExecutorAgent()
        logger.info("OrchestratorAgent ready")

    # ── helpers ────────────────────────────────────────────────────

    @staticmethod
    def _banner(text: str, char: str = "=", width: int = 78) -> None:
        logger.info(char * width)
        logger.info(text)
        logger.info(char * width)

    def _log_iteration_header(self, iteration: int, feedback: Optional[str]) -> None:
        self._banner(
            f"ORCHESTRATOR — ITERATION {iteration}/{self.max_iterations}"
            + ("  [with feedback]" if feedback else "  [fresh run]")
        )

    # ── main loop ──────────────────────────────────────────────────

    def RunPipeline(
        self,
        action: Dict[str, Any],
        sandbox_path: Optional[str] = None,
        reference_folder: Optional[str] = None,      # ← NEW
        thread_id: Optional[str] = None,
        answers: Optional[List[str]] = None,
        generator_thread_id: Optional[str] = None,
        command_timeout: int = 300,
    ) -> OrchestratorResponse:
        """
        Run the generate → execute loop until success or max_iterations.

        New parameter:
            reference_folder: Path to folder containing reference code (style, patterns, best practices).
                              Passed to GeneratorAgent on every iteration.
        """
        orch_tid = thread_id or str(uuid.uuid4())
        records: List[IterationRecord] = []
        feedback: Optional[str] = None
        current_sandbox: Optional[str] = sandbox_path
        gen_tid: Optional[str] = generator_thread_id

        logger.info("")
        self._banner("ORCHESTRATOR AGENT PIPELINE STARTED", char="╔")
        logger.info("Orchestrator Thread ID : %s", orch_tid)
        logger.info("Action                 : %s", action.get("actionName"))
        logger.info("Reference Folder       : %s", reference_folder or "None (no style reference)")
        logger.info("Max Iterations         : %d", self.max_iterations)
        logger.info("Command Timeout        : %ds per command", command_timeout)
        logger.info("")

        for iteration in range(1, self.max_iterations + 1):
            self._log_iteration_header(iteration, feedback)

            # ── Step 1: Generate (or update) files ────────────────
            logger.info("[Iteration %d] Calling GeneratorAgent …", iteration)

            if answers and gen_tid:
                # Resume HITL
                gen_response: GeneratorPipelineResponse = self.generator.RunPipeline(
                    action=action,
                    thread_id=gen_tid,
                    answers=answers,
                )
                answers = None
                gen_tid = None
            else:
                gen_response = self.generator.RunPipeline(
                    action=action,
                    sandbox_path=current_sandbox,
                    reference_folder=reference_folder,           # ← Passed here
                    feedback_summary=feedback,
                )

            # HITL handling
            if gen_response.status == "needs_clarification":
                logger.info(
                    "[Iteration %d] GeneratorAgent paused — waiting for HITL answers",
                    iteration,
                )
                return OrchestratorResponse(
                    statusCode=202,
                    status="needs_clarification",
                    thread_id=orch_tid,
                    sandbox_path=current_sandbox,
                    iterations_used=iteration,
                    iterations=records,
                    questions=gen_response.questions,
                    summary=(
                        f"GeneratorAgent needs clarification (thread_id={gen_response.thread_id}). "
                        "Re-call OrchestratorAgent.RunPipeline with answers= and "
                        f"generator_thread_id='{gen_response.thread_id}'."
                    ),
                )

            if gen_response.status != "success" or not gen_response.sandbox_path:
                logger.error(
                    "[Iteration %d] GeneratorAgent failed: %s",
                    iteration,
                    gen_response.exception,
                )
                return OrchestratorResponse(
                    statusCode=500,
                    status="error",
                    exception=f"GeneratorAgent error on iteration {iteration}: {gen_response.exception}",
                    thread_id=orch_tid,
                    sandbox_path=current_sandbox,
                    iterations_used=iteration,
                    iterations=records,
                )

            current_sandbox = gen_response.sandbox_path
            logger.info(
                "[Iteration %d] GeneratorAgent succeeded. sandbox=%s",
                iteration,
                current_sandbox,
            )

            # ── Step 2: Execute files ──────────────────────────────
            logger.info("[Iteration %d] Calling ExecutorAgent …", iteration)

            exec_response: ExecutorPipelineResponse = self.executor.RunPipeline(
                action=action,
                executeFolder=current_sandbox,
                executableSteps=[
                    s.model_dump() if hasattr(s, "model_dump") else dict(s)
                    for s in (gen_response.executableSteps or [])
                ],
                command_timeout=command_timeout,
            )

            record = IterationRecord(
                iteration=iteration,
                generator_status=gen_response.status,
                executor_status=exec_response.status,
                executor_success=exec_response.success or False,
                feedback_used=feedback,
                sandbox_path=current_sandbox,
            )
            records.append(record)

            # ── Step 3: Check outcome ──────────────────────────────
            if exec_response.success:
                self._banner(
                    f"✓ ORCHESTRATOR COMPLETED SUCCESSFULLY on iteration {iteration}",
                    char="═",
                )
                return OrchestratorResponse(
                    statusCode=200,
                    status="success",
                    thread_id=orch_tid,
                    sandbox_path=current_sandbox,
                    iterations_used=iteration,
                    iterations=records,
                    final_executor_response=exec_response.model_dump(),
                    summary=exec_response.summary,
                )

            # Execution failed → prepare feedback
            logger.warning(
                "[Iteration %d] ExecutorAgent failed. Preparing feedback for next iteration …",
                iteration,
            )
            feedback = exec_response.summary or (
                f"Execution failed on iteration {iteration}. "
                "Review the error output and fix the generated files."
            )
            logger.info("[Iteration %d] Feedback summary: %s", iteration, feedback[:300] + "..." if len(feedback) > 300 else feedback)

        # ── Exhausted all iterations ───────────────────────────────
        self._banner(
            f"✗ ORCHESTRATOR EXHAUSTED {self.max_iterations} ITERATIONS WITHOUT SUCCESS",
            char="═",
        )
        last_exec = exec_response
        return OrchestratorResponse(
            statusCode=207,
            status="failed",
            thread_id=orch_tid,
            sandbox_path=current_sandbox,
            iterations_used=self.max_iterations,
            iterations=records,
            final_executor_response=last_exec.model_dump(),
            summary=(
                f"Action '{action.get('actionName')}' could not be completed after "
                f"{self.max_iterations} iterations. Last error: {last_exec.summary}"
            ),
        )


# ── Entry point ────────────────────────────────────────────────────

# if __name__ == "__main__":
#     orchestrator = OrchestratorAgent(max_iterations=5)

#     action_payload = {
#         "actionName": "Deploy Production RDS Instance with Terraform",
#         "actionDescription": (
#             "Deploy a single-AZ PostgreSQL RDS instance (REL-001) tagged as production "
#             "in the default VPC using Terraform. This provisions the database infrastructure "
#             "with storage encryption enabled and applies production environment tags."
#         ),
#         "steps": [],
#     }

#     response = orchestrator.RunPipeline(
#         action=action_payload,
#         reference_folder="reference_aws",        # ← Set your reference folder here
#         command_timeout=1600,                    # Increased for Terraform operations
#     )
#     print(response.model_dump_json(indent=2))