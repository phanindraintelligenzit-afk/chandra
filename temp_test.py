import json

mode_instruction = ""
action = {"actionName": "test", "actionDescription": "test", "steps": []}
analysis = {"recommended_approach": "test"}
os_name = "linux"
shell_note = ""
creds_note = ""
dynamic_context = ""
credential_context = ""
outputs_context = ""
memory_section = ""
aws_section = ""
service_quotas_context = ""
custom_kra_instruction = ""
terraform_docs_context = ""
batch_docs_ctx = ""
state_section = ""
validate_section = ""
plan_review_section = ""
reference_context = ""
clarification_context = ""
feedback_context = ""
existing_context = ""
prev_batch_ctx = ""

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

═══════════════════════════════════════════════════════════════
ABSOLUTE RULES — violating any of these causes immediate failure
═══════════════════════════════════════════════════════════════

RULE 0 — HCL SYNTAX: HEREDOC FOR MULTI-LINE STRINGS:
  Terraform/HCL does NOT support Python-style triple-quotes (''' or \"\"\").
  For any multi-line string value (user_data, inline policy JSON, etc.) you MUST
  use HCL heredoc syntax.

---
Generate the complete set of files now."""