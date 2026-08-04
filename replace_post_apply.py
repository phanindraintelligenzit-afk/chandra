import sys

with open(r'd:\DFTE-Chandra\chandra\digitalworker_agents\aws_execution_agent.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    if 'timings["terraform_apply"] = time.time() - start_t' in line:
        start_idx = i
        break

new_lines = lines[:start_idx + 1]
new_lines.append("""
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
                      if not is_verified:
                           self.logger.error("Post-Apply Verification Failed: Resource state could not be confirmed via boto3.")
                           overall_success = False
                      else:
                           self.logger.info("Post-Apply Verification Passed: Resource state confirmed via boto3.")
             except Exception as exc:
                 self.logger.warning("Post-Apply Verification encountered an error: %s", exc)
""")
new_lines.extend(lines[start_idx + 1:])

with open(r'd:\DFTE-Chandra\chandra\digitalworker_agents\aws_execution_agent.py', 'w', encoding='utf-8') as f:
    f.writelines(new_lines)
print('Injected post-apply verification')
