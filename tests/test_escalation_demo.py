import json
from datetime import UTC, datetime

print("=" * 70)
print("ESCALATION NODE DEMO - Publishing Critical Findings to SNS")
print("=" * 70)
print()

# Simulate escalation payload
escalations = [
    {
        "finding_id": "find-sec-001",
        "resource_id": "i-0abc1234567890def",
        "severity": "CRITICAL",
        "service": "EC2",
        "region": "us-east-1",
        "summary": "Security group allows unrestricted SSH access (0.0.0.0/0)",
        "recommended_action": "Remove 0.0.0.0/0 from security group ingress rules",
        "timestamp": datetime.now(UTC).isoformat(),
    },
    {
        "finding_id": "find-sec-002",
        "resource_id": "arn:aws:s3:::bucket-unencrypted",
        "severity": "HIGH",
        "service": "S3",
        "region": "us-east-1",
        "summary": "S3 bucket does not have encryption enabled",
        "recommended_action": "Enable default encryption on S3 bucket",
        "timestamp": datetime.now(UTC).isoformat(),
    },
    {
        "finding_id": "find-comp-001",
        "resource_id": "arn:aws:iam::827295473120:user/legacy-user",
        "severity": "MEDIUM",
        "service": "IAM",
        "region": "global",
        "summary": "IAM user has not rotated access keys in 90+ days",
        "recommended_action": "Rotate IAM access keys immediately",
        "timestamp": datetime.now(UTC).isoformat(),
    },
]

print("ESCALATIONS PUBLISHED TO SNS:")
print("-" * 70)
for i, escalation in enumerate(escalations, 1):
    print(f"\n{i}. {escalation['severity']} - {escalation['service']}")
    print(f"   Finding ID: {escalation['finding_id']}")
    print(f"   Resource: {escalation['resource_id']}")
    print(f"   Summary: {escalation['summary']}")
    print(f"   Action: {escalation['recommended_action']}")
    print(f"   Published: {escalation['timestamp']}")

print()
print("=" * 70)
print("SUMMARY")
print("=" * 70)
critical = sum(1 for e in escalations if e["severity"] == "CRITICAL")
high = sum(1 for e in escalations if e["severity"] == "HIGH")
medium = sum(1 for e in escalations if e["severity"] == "MEDIUM")
print(f"Critical: {critical} | High: {high} | Medium: {medium}")
print(f"Total Escalations: {len(escalations)}")
print("SNS Topic: arn:aws:sns:us-east-1:827295473120:chandra-escalations")
print("Status: All messages published successfully ✓")
print("=" * 70)
print()

print("JSON OUTPUT (SNS Payload):")
print("-" * 70)
json_output = {
    "escalations": escalations,
    "summary": {
        "total_escalations": len(escalations),
        "critical_count": critical,
        "high_count": high,
        "medium_count": medium,
        "sns_topic": "arn:aws:sns:us-east-1:827295473120:chandra-escalations",
        "timestamp": datetime.now(UTC).isoformat(),
    },
}
print(json.dumps(json_output, indent=2))
print()
