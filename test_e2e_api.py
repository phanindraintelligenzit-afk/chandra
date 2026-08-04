import requests
import time

def test_api():
    print("Testing /orchestrate API (E2E)...")
    url = "http://127.0.0.1:6001/orchestrate"
    
    payload = {
        "action": {
            "actionName": "Create S3 bucket",
            "actionDescription": "Create a simple AWS S3 bucket for testing named chandra-test-bucket-12345",
            "service": "AWS",
            "action_type": "AWS_TASK",
            "permission_set_id": "arn:aws:sso:::permissionSet/ssoins-123/ps-123"
        },
        "max_iterations": 2,
        "command_timeout": 600
    }
    
    resp = requests.post(url, json=payload)
    if resp.status_code != 200:
        print(f"Failed to submit: {resp.text}")
        return
        
    data = resp.json()
    job_id = data["job_id"]
    print(f"Submitted successfully. Job ID: {job_id}")
    
    # Poll status
    status_url = f"http://127.0.0.1:6001/orchestrate/status/{job_id}"
    resume_url = f"http://127.0.0.1:6001/orchestrate/{job_id}/resume"
    hitl_count = 0
    
    while True:
        s_resp = requests.get(status_url)
        s_data = s_resp.json()
        print(f"Status: {s_data['status']} | Progress: {s_data['progress']}% | Message: {s_data.get('message', '')}")
        
        if s_data["status"] == "needs_clarification":
            print(f"HITL triggered. Resuming...")
            requests.post(resume_url, json={"answers": ["yes"]})
            hitl_count += 1
            if hitl_count > 2:
                print("Too many HITLs, breaking.")
                break
                
        elif s_data["status"] in ("completed", "failed", "stopped"):
            if s_data["status"] == "completed" and s_data.get("result", {}).get("status") == "needs_clarification":
                 print(f"HITL triggered (inside result). Resuming...")
                 requests.post(resume_url, json={"answers": ["yes"]})
                 hitl_count += 1
                 if hitl_count > 2:
                     print("Too many HITLs, breaking.")
                     break
                 continue
                 
            print("Final State reached.")
            if "result" in s_data and s_data["result"]:
                print("Result:", s_data["result"])
            break
        time.sleep(2)

if __name__ == "__main__":
    test_api()
