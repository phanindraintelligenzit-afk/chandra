import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from fastapi_app import app, OrchestrateRequest, ActionInput, _run_orchestration_task, _job_store

client = TestClient(app)

@patch("fastapi_app.ExecutionAgents")
@patch("src.chandra.graphs.action_nodes.action_executor.action_executor_node")
def test_aws_task_never_routes_to_detector_path(mock_action_executor, mock_execution_agents):
    """Test that an AWS Task with a detectorId does NOT route to action_executor_node."""
    request = OrchestrateRequest(
        action=ActionInput(
            actionName="Create S3 Bucket",
            actionDescription="Test",
            detectorId="task_123",
            isAwsTask=True,
            action_type="AWS_TASK"
        )
    )
    
    _job_store["job_1"] = {"status": "running"}
    _run_orchestration_task("job_1", request)
    
    mock_action_executor.assert_not_called()
    mock_execution_agents.assert_called_once()

@patch("fastapi_app.ExecutionAgents")
@patch("src.chandra.graphs.action_nodes.action_executor.action_executor_node")
def test_kra_detector_still_routes_correctly(mock_action_executor, mock_execution_agents):
    """Test that a KRA remediation request (isAwsTask=False) with detectorId routes to action_executor_node."""
    request = OrchestrateRequest(
        action=ActionInput(
            actionName="Remediate KRA",
            actionDescription="Test",
            detectorId="kra_123",
            isAwsTask=False
        )
    )
    
    # We mock action_executor_node to return a basic state
    mock_action_executor.return_value = {"action_results": [{"status": "success", "result": "done"}]}
    
    _job_store["job_2"] = {"status": "running"}
    _run_orchestration_task("job_2", request)
    
    mock_action_executor.assert_called_once()
    mock_execution_agents.assert_not_called()

@patch("fastapi_app.ExecutionAgents")
@patch("src.chandra.graphs.action_nodes.action_executor.action_executor_node")
def test_aws_task_without_detector_id_routes_to_execution_agents(mock_action_executor, mock_execution_agents):
    """Test that a normal AWS Task without detectorId routes correctly."""
    request = OrchestrateRequest(
        action=ActionInput(
            actionName="Create EC2",
            actionDescription="Test",
            isAwsTask=True,
            action_type="AWS_TASK"
        )
    )
    
    _job_store["job_3"] = {"status": "running"}
    _run_orchestration_task("job_3", request)
    
    mock_action_executor.assert_not_called()
    mock_execution_agents.assert_called_once()

@patch("fastapi_app.ExecutionAgents")
@patch("src.chandra.graphs.action_nodes.action_executor.action_executor_node")
def test_aws_task_status_skipped_never_becomes_completed(mock_action_executor, mock_execution_agents):
    """Test that if action_executor_node returns skipped, it does not become completed."""
    request = OrchestrateRequest(
        action=ActionInput(
            actionName="Remediate KRA Skip",
            actionDescription="Test",
            detectorId="kra_456",
            isAwsTask=False
        )
    )
    
    mock_action_executor.return_value = {"action_results": [{"status": "skipped", "result": "Not applicable"}]}
    
    _job_store["job_4"] = {"status": "running"}
    
    _run_orchestration_task("job_4", request)
    
    assert _job_store["job_4"]["status"] == "skipped"

def test_duplicate_approval_prevented():
    """Test that approving a job not in 'awaiting_approval' status returns 409."""
    # Setup job store
    _job_store["job_duplicate"] = {"status": "completed"}
    
    response = client.post("/requests/job_duplicate/approve", json={"approved": True, "approver": "test"})
    assert response.status_code == 409
