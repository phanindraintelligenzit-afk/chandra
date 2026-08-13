import os

FILE_PATH = r"d:\DFTE-Chandra\chandra\digitalworker_agents\aws_execution_agent.py"

with open(FILE_PATH, "r", encoding="utf-8") as f:
    content = f.read()

# We need to insert the missing code right after `self.logger.info("")` which is inside RunPipeline, before `def GenerateTerraformOnly`.

# Let's search for the exact block where it should be inserted.
target = """            aws_ctx = self._gather_aws_context(force_refresh=True) if not answers else ""

        self.logger.info("")

    def GenerateTerraformOnly("""

replacement = """            aws_ctx = self._gather_aws_context(force_refresh=True) if not answers else ""

        self.logger.info("")
        self._banner("UNIFIED AGENT PIPELINE STARTED")
        self.logger.info("Thread ID        : %s", tid)
        self.logger.info("Action           : %s", action.get("actionName"))
        self.logger.info("Reference Folder : %s", reference_folder or "None")
        self.logger.info("Max Iterations   : %d", self.max_iterations)
        self.logger.info("Command Timeout  : %ds per command", command_timeout)
        self.logger.info("Memory entries   : %d total runs loaded", len(self.Memory.runs))
        self.logger.info("")

        try:
            if answers:
                self.Graph.invoke(Command(resume=answers), config=config)
            else:
                self.Graph.invoke(
                    {
                        "action": action,
                        "aws_permissions": aws_permissions or [],
                        "reference_folder": reference_folder or "",
                        "command_timeout": command_timeout,
                        "max_iterations": self.max_iterations,
                        "iteration": 1,
                        "records": [],
                        "feedback_summary": "",
                        "memory_context": memory_ctx,
                        "aws_context": aws_ctx,
                        "consecutive_same_error": 0,
                        "last_error_class": "",

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

            if snapshot.tasks and any(t.interrupts for t in snapshot.tasks):
                questions = snapshot.tasks[0].interrupts[0].value
                is_mid_run = snapshot.values.get("consecutive_same_error", 0) >= 3
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
            sandbox_path_final = final.get("sandbox_path") or None

            if final_status == "success":
                return PipelineResponse(
                    statusCode=200,
                    status="success",
                    thread_id=tid,
                    sandbox_path=sandbox_path_final,
                    iterations_used=final.get("iteration", len(records)),
                    iterations=records,
                    execution_results=results,
                    summary=final.get("final_summary") or final.get("executor_summary"),
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
                summary=final.get("final_summary") or final.get("executor_summary"),
            )

        except Exception as exc:
            self.logger.exception("RunPipeline error: %s", exc)
            try:
                snapshot = self.Graph.get_state(config)
                orphaned_sandbox = snapshot.values.get("sandbox_path") if snapshot.values else None
                if orphaned_sandbox:
                    self._cleanup_sandbox(orphaned_sandbox)
            except Exception:
                pass
            return PipelineResponse(
                statusCode=500,
                status="error",
                exception=str(exc),
                thread_id=tid,
            )

    def GenerateTerraformOnly("""

if target in content:
    new_content = content.replace(target, replacement)
    with open(FILE_PATH, "w", encoding="utf-8") as f:
        f.write(new_content)
    print("Success")
else:
    print("Target not found")
