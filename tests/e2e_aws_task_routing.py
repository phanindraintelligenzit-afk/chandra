import requests
import time
import uuid
import sys

BASE_URL = "http://127.0.0.1:6001"

def run_e2e_test():
    print("Starting E2E Test for AWS Task Routing Fix...")
    job_id = str(uuid.uuid4())
    
    # 1. Simulate the /orchestrate API call made by WorkerActionExecutionCenter
    # when an AWS Task is approved.
    payload = {
        "action": {
            "actionName": "Create S3 Bucket",
            "actionDescription": "Test bucket creation",
            "service": "AWS",
            "isAwsTask": True,
            "region": "us-east-1"
        },
        "command_timeout": 300,
        "max_iterations": 5,
        "aws_permissions": ["AmazonS3FullAccess"]
    }
    
    print(f"Triggering orchestration for job: {job_id}")
    try:
        response = requests.post(f"{BASE_URL}/orchestrate", json=payload, params={"job_id": job_id})
        response.raise_for_status()
        res_json = response.json()
        print(f"Orchestrate accepted: {res_json}")
        actual_job_id = res_json.get("job_id", job_id)
    except requests.exceptions.RequestException as e:
        print(f"Failed to start orchestration: {e}")
        if e.response:
            print(f"Response: {e.response.text}")
        sys.exit(1)

    print("Polling job status...")
    max_polls = 30
    for i in range(max_polls):
        time.sleep(2)
        try:
            res = requests.get(f"{BASE_URL}/orchestrate/status/{actual_job_id}")
            data = res.json()
            status = data.get("status")
            progress = data.get("progress")
            msg = data.get("message")
            print(f"[{i+1}/{max_polls}] Status: {status}, Progress: {progress}%, Message: {msg}")
            
            if status in ["completed", "failed", "skipped", "blocked", "unverified"]:
                print(f"Job finished with final status: {status}")
                break
        except Exception as e:
            print(f"Error polling: {e}")
            
    # Verify the job wasn't routed to action_executor_node which returns SKIPPED
    if status == "skipped" and "No handler registered" in str(msg):
         print("FAILED: Job incorrectly routed to action_executor_node!")
         sys.exit(1)
         
    print("\nTest completed successfully. Verify backend logs for ExecutionAgents routing.")

if __name__ == "__main__":
    run_e2e_test()
