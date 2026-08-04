import asyncio
from digitalworker_agents.aws_execution_agent import ExecutionAgents
import uuid

async def test_run():
    print("Testing AWS Task E2E via ExecutionAgents (Bedrock)...")
    job_id = str(uuid.uuid4())
    agent = ExecutionAgents(job_id=job_id, max_iterations=2)
    
    try:
        action = {
            "job_id": job_id,
            "action": "Create a simple AWS S3 bucket for testing named chandra-test-bucket-12345",
            "permission_set_id": "arn:aws:sso:::permissionSet/ssoins-123/ps-123",
            "answers": "Create a simple AWS S3 bucket for testing named chandra-test-bucket-12345"
        }
        async for chunk in agent.RunPipeline(action):
            print("Received chunk:", chunk)
    except Exception as e:
        print(f"E2E Test Failed/Blocked: {e}")

if __name__ == "__main__":
    asyncio.run(test_run())
