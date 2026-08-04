import pytest
import os
import json
from unittest.mock import patch, MagicMock

from digitalworker_agents.aws_execution_agent import ExecutionAgents

@pytest.fixture
def mock_execution_agent():
    return ExecutionAgents(max_iterations=1, job_id="test-timings-1234")

def test_execute_node_timing_initialization(mock_execution_agent):
    """
    Regression test to ensure timings and start_t are correctly initialized 
    in _execute_node so it doesn't crash with NameError after a successful apply.
    """
    state = {
        "action": {
            "actionName": "Test AWS Task",
            "service": "s3",
            "action_type": "AWS_TASK"
        },
        "plan_review": {
            "commands": [
                {"command": "echo 'terraform apply'", "order": 1}
            ]
        },
        "sandbox_path": "terraform_runs/test_worker/test-timings-1234",
        "command_timeout": 10,
        "pre_apply_results": [],
        "aws_permissions": [{"id": "S3FullAccess", "actions": [{"action": "s3:*"}]}],
        "execution_plan": {
            "commands": [
                {"command": "echo 'terraform apply'", "order": 1}
            ]
        }
    }

    # Mock the _run_commands to simulate a successful execution
    with patch.object(mock_execution_agent, '_run_commands') as mock_run:
        mock_run.return_value = ([{
            "command": "echo 'terraform apply'",
            "success": True,
            "return_code": 0,
            "stdout": "Success",
            "stderr": "",
            "timed_out": False
        }], False)
        
        # Mock execute_shell_command for the verification step
        with patch('digitalworker_agents.aws_execution_agent.execute_shell_command') as mock_exec_shell:
            mock_exec_shell.return_value = {
                "success": True,
                "stdout": json.dumps({"bucket_name": {"value": "test-bucket"}}),
                "stderr": "",
                "return_code": 0
            }
            
            # Since this is a test, let's also mock the AwsResourceVerifier
            with patch('src.chandra.execution.services.AwsResourceVerifier') as mock_verifier_class:
                mock_verifier = MagicMock()
                mock_verifier.verify_resource.return_value = "VERIFIED"
                mock_verifier_class.return_value = mock_verifier
                
                try:
                    result = mock_execution_agent._execute_node(state)
                    
                    assert result["success"] is True
                    assert "timings" in result
                    assert "terraform_apply" in result["timings"]
                    assert result["timings"]["terraform_apply"] >= 0
                except NameError as e:
                    pytest.fail(f"_execute_node crashed with NameError: {e}")
