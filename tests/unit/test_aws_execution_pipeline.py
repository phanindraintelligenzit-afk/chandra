import pytest
from unittest.mock import patch, MagicMock

from digitalworker_agents.aws_execution_agent import ExecutionAgents, PipelineResponse

def test_run_pipeline_returns_valid_response():
    """
    Regression test to prove RunPipeline completes its control flow and returns a PipelineResponse,
    rather than silently returning None after generating context (which caused the AWS Task crash).
    """
    agent = ExecutionAgents(max_iterations=1, job_id="test-job-id")
    
    # Mock the internal AWS context gatherer to avoid real AWS calls
    agent._gather_aws_context = MagicMock(return_value="Mocked AWS Context")
    
    # We want to mock the graph invocation so it returns immediately but simulates a run.
    # The actual bug was that RunPipeline lacked the Graph.invoke block entirely and returned None.
    # If the block is present, it will call invoke and get_state, then return PipelineResponse.
    mock_invoke = MagicMock()
    mock_get_state = MagicMock()
    
    # Setup mock state snapshot
    mock_snapshot = MagicMock()
    mock_snapshot.tasks = []
    mock_snapshot.values = {
        "final_status": "success",
        "records": [],
        "execution_results": [],
        "sandbox_path": "/tmp/sandbox",
        "iteration": 1,
        "executor_summary": "Mocked summary"
    }
    mock_get_state.return_value = mock_snapshot
    
    agent.Graph.invoke = mock_invoke
    agent.Graph.get_state = mock_get_state
    
    action = {
        "actionName": "Test Action",
        "actionDescription": "Test Desc",
        "service": "s3",
        "steps": []
    }
    
    # Execute
    response = agent.RunPipeline(action=action)
    
    # Verify the method actually returns a PipelineResponse and has a statusCode
    assert response is not None, "RunPipeline returned None! (Regression of truncated function)"
    assert isinstance(response, PipelineResponse)
    assert response.statusCode == 200
    assert response.status == "success"
    
    # Verify Graph was actually invoked
    mock_invoke.assert_called_once()
    mock_get_state.assert_called_once()
