import sys

with open(r'd:\DFTE-Chandra\chandra\digitalworker_agents\aws_execution_agent.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

new_lines = lines[:1502]
new_lines.append("""
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
""")
new_lines.extend(lines[1677:])

with open(r'd:\DFTE-Chandra\chandra\digitalworker_agents\aws_execution_agent.py', 'w', encoding='utf-8') as f:
    f.writelines(new_lines)
print('Replaced check_permissions_node')
