import pytest
from fastapi.testclient import TestClient
from fastapi_app import app, _job_store

client = TestClient(app)

def test_frontend_payload_permission_propagation():
    """
    Regression test to ensure the real UI payload format correctly propagates
    aws_permissions to the orchestrator job store.
    The real UI sends aws_permissions at the root of the payload, not inside action.
    """
    payload = {
        "action": {
            "actionName": "Create S3 bucket",
            "actionDescription": "Provision a new S3 bucket named 'chandra-test-bucket' in us-east-1",
            "service": "S3",
            "region": "us-east-1",
            "isAwsTask": True,
            "action_type": "AWS_TASK"
        },
        "aws_permissions": ["arn:aws:sso:::permissionSet/ssoins-123/ps-123"]
    }
    
    response = client.post("/orchestrate", json=payload)
    assert response.status_code == 200
    
    data = response.json()
    assert "job_id" in data
    
    job_id = data["job_id"]
    
    import time
    # Poll until the background task populates the job_store
    timeout = 5
    start_time = time.time()
    while time.time() - start_time < timeout:
        if job_id in _job_store and "aws_permissions" in _job_store[job_id]:
            break
        time.sleep(0.1)
    
    # Check if the job store correctly received aws_permissions
    assert job_id in _job_store
    job_state = _job_store[job_id]
    
    # The permissions should be loaded from the payload root and stored in job state
    assert "aws_permissions" in job_state
    assert job_state["aws_permissions"] == ["arn:aws:sso:::permissionSet/ssoins-123/ps-123"]
