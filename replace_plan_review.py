import sys

with open(r'd:\DFTE-Chandra\chandra\digitalworker_agents\aws_execution_agent.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    if 'def _plan_review_node' in line:
        start_idx = i
        break
for i in range(start_idx + 1, len(lines)):
    if 'def _human_approval_center_node' in lines[i]:
        end_idx = i
        break

new_lines = lines[:start_idx]
new_lines.append("""    def _plan_review_node(self, state: AgentState) -> dict:
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

""")

new_lines.extend(lines[end_idx:])

with open(r'd:\DFTE-Chandra\chandra\digitalworker_agents\aws_execution_agent.py', 'w', encoding='utf-8') as f:
    f.writelines(new_lines)
print('Replaced _plan_review_node')
