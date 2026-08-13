import requests
import time
import sys

BASE_URL = "http://127.0.0.1:6001"
JIRA_ISSUE_KEY = "DEV-777"

print("--- Starting Manual E2E Jira Workflow Simulation ---")

# 1. Jira Webhook Intake
payload = {
    "issue": {
        "key": JIRA_ISSUE_KEY,
        "fields": {
            "summary": "Create an S3 bucket for testing",
            "description": "Please deploy an S3 bucket named 'chandra-e2e-test-123' in us-east-1."
        }
    }
}
print(f"Triggering Webhook for {JIRA_ISSUE_KEY}...")
resp = requests.post(f"{BASE_URL}/webhooks/jira", json=payload)
if resp.status_code != 202:
    print(f"Webhook failed: {resp.status_code} - {resp.text}")
    sys.exit(1)

job_id = resp.json().get("job_id")
print(f"Accepted with Job ID: {job_id}")

def wait_for_status(target_statuses, timeout=300):
    start = time.time()
    while time.time() - start < timeout:
        res = requests.get(f"{BASE_URL}/orchestrate/status/{job_id}")
        data = res.json()
        current_status = data.get("status")
        print(f"  [Status Check] Job={job_id}, Status={current_status}")
        if current_status in target_statuses:
            return data
        if current_status in ["failed", "error", "completed"]:
            print(f"Unexpected terminal status: {current_status}. Response: {data}")
            sys.exit(1)
        time.time()
        time.sleep(5)
    print("Timeout waiting for status.")
    sys.exit(1)

# 2. Wait for Awaiting Approval
print("\nWaiting for Human Approval Gate...")
status_data = wait_for_status(["awaiting_approval"])
print("Reached Awaiting Approval successfully.")

# 3. Approve the request
print("\nSubmitting Human Approval...")
resp = requests.post(f"{BASE_URL}/requests/{job_id}/approve", json={
    "approved": True,
    "user_message": "Approved by E2E test"
})
if resp.status_code not in [200, 202]:
    print(f"Approval failed: {resp.status_code} - {resp.text}")
    sys.exit(1)

# 4. Wait for Permission Analysis to finish
print("\nWaiting for Permission Analysis (Phase 3A) -> awaiting_permission...")
status_data = wait_for_status(["awaiting_permission"])
print("Reached Awaiting Permission Set.")

req_perms = status_data.get("result", {}).get("required_permissions", [])
print(f"Required Permissions Analyzed by Bedrock: {req_perms}")

# 5. Test Gate 1 FAIL (Bad Permission Set)
print("\nTesting Gate 1 FAIL with insufficient permissions...")
resp = requests.post(f"{BASE_URL}/requests/{job_id}/approve", json={
    "approved": True,
    "permission_set_id": "test-bad-perms",
    "permission_set_document": {
        "permissions": [
            {"action": "s3:ListBucket", "resource": "*", "effect": "Allow"}
        ]
    }
})
if resp.status_code != 202:
    print(f"Failed to submit Gate 1 FAIL: {resp.text}")

print("Waiting for Gate 1 FAIL -> Returns to awaiting_permission...")
status_data = wait_for_status(["awaiting_permission"])
print(f"Successfully caught Gate 1 FAIL. Status is still awaiting_permission.")

# 6. Test Gate 1 PASS (Good Permission Set)
print("\nTesting Gate 1 PASS with sufficient permissions...")
resp = requests.post(f"{BASE_URL}/requests/{job_id}/approve", json={
    "approved": True,
    "permission_set_id": "test-good-perms",
    "permission_set_document": {
        "permissions": [
            {"action": "s3:*", "resource": "*", "effect": "Allow"}
        ]
    }
})
if resp.status_code not in [200, 202]:
    print("Failed to submit good permission set.")
    sys.exit(1)

# 7. Wait for Gate 2
print("\nWaiting for Terraform Generation & ExecutionAgents (Phase 3C) -> Awaiting Gate 2...")
status_data = wait_for_status(["awaiting_gate2", "awaiting_execution"], timeout=300)
print(f"Reached {status_data.get('status')} successfully!")

result = status_data.get("result", {})
print("\n--- Gate 2 Review Data ---")
print(f"Terraform Plan:\n{result.get('terraform_plan', 'None')}")
print(f"Terraform Validated: {result.get('terraform_validation_passed')}")
print(f"Sandbox Path: {result.get('sandbox_path')}")

# 8. Approve Gate 2
print("\nSubmitting Gate 2 Approval...")
resp = requests.post(f"{BASE_URL}/requests/{job_id}/approve", json={
    "approved": True,
    "user_message": "Gate 2 Approved by E2E test"
})
if resp.status_code not in [200, 202]:
    print(f"Gate 2 Approval failed: {resp.status_code} - {resp.text}")
    sys.exit(1)

# 9. Wait for Final Execution and Jira Update
print("\nWaiting for Final Execution (Phase 3D -> 3E)...")
final_data = wait_for_status(["completed", "completed_with_issues", "dry_run"], timeout=600)
print("\n--- FINAL CHANDRA RESULT ---")
import pprint
pprint.pprint(final_data)
print("\n--- TEST FULLY SUCCESSFUL ---")
