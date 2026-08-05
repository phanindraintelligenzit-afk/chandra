import pytest
from digitalworker_agents.aws_execution_agent import ExecutionAgents

def test_gate2_retry_classification():
    agent = ExecutionAgents()
    state = {
        "execution_results": [
            {"command": "terraform apply", "success": True, "return_code": 0, "stderr": ""}
        ],
        "plan_review_precheck_failed": True,
        "plan_review_issue": "Unrelated resource aws_iam_role detected for EC2 task.",
        "last_error_class": "Gate2PlanValidationFailed",
        "consecutive_same_error": 2, # Will become 3
        "max_iterations": 4,
        "iteration": 2
    }
    
    result = agent._record_iteration_node(state)
    
    assert result.get("last_error_class") == "Gate2PlanValidationFailed"
    assert result.get("consecutive_same_error") == 3
    # With stuck threshold=3, we expect the pipeline to be halted/failed
    # Actually _evaluator_node handles "stuck_threshold" and if consecutive >= STUCK_THRESHOLD, it returns "failed"
    assert result.get("final_status") == "stuck"
