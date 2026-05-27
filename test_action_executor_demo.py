import json

print("=" * 70)
print("ACTION EXECUTOR DEMO - Fixing AWS Misconfigurations")
print("=" * 70)
print()

# Simulate action executor fixing issues
actions_taken = [
    {
        "action_id": "act-001",
        "resource_arn": "arn:aws:s3:::bucket-public-data",
        "action_type": "fix_public_s3",
        "status": "FIXED",
        "description": "Made S3 bucket private - blocked public access",
        "cost_saved": 245.50,
    },
    {
        "action_id": "act-002",
        "resource_arn": "arn:aws:ec2:us-east-1:827295473120:security-group/sg-0abc1234",
        "action_type": "fix_open_sg",
        "status": "FIXED",
        "description": "Closed security group - removed 0.0.0.0/0 ingress rule",
        "cost_saved": 0.0,
    },
    {
        "action_id": "act-003",
        "resource_arn": "arn:aws:iam::827295473120:user/service-user",
        "action_type": "disable_iam_key",
        "status": "FIXED",
        "description": "Disabled inactive IAM access key - AKIA2BHVHDXQ34VIZFL",
        "cost_saved": 0.0,
    },
]

print("ACTIONS EXECUTED:")
print("-" * 70)
for i, action in enumerate(actions_taken, 1):
    print(f"\n{i}. {action['action_type'].upper()}")
    print(f"   Resource: {action['resource_arn']}")
    print(f"   Status: {action['status']}")
    print(f"   Description: {action['description']}")
    print(f"   Cost Saved: ${action['cost_saved']}")

print()
print("=" * 70)
print("SUMMARY")
print("=" * 70)
total_actions = len(actions_taken)
total_cost_saved = sum(a['cost_saved'] for a in actions_taken)
print(f"Total Actions Executed: {total_actions}")
print(f"Total Cost Saved: ${total_cost_saved:.2f}")
print(f"Success Rate: 100%")
print("=" * 70)
print()

print("JSON OUTPUT:")
print("-" * 70)
json_output = {
    "run_id": "chandra-run-20260530-001",
    "actions_executed": actions_taken,
    "summary": {
        "total_actions": total_actions,
        "total_cost_saved": total_cost_saved,
        "success_rate": "100%",
        "timestamp": "2026-05-30T10:15:00Z",
    }
}
print(json.dumps(json_output, indent=2))
print()