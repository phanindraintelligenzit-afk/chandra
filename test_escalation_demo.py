"""Demo script for escalation node."""
from datetime import datetime, timezone
from src.chandra.escalation.schemas import EscalationPayload
from src.chandra.escalation.publisher import SNSPublisher
from src.chandra.aws.client_factory import get_default_factory
from dotenv import load_dotenv
load_dotenv(override=True)

print("=" * 70)
print("CHANDRA ESCALATION NODE DEMO")
print("=" * 70)
print()

escalations = [
    {
        "severity": "CRITICAL",
        "finding_type": "PublicS3Bucket",
        "resource_arn": "arn:aws:s3:::logs-prod",
        "title": "Public S3 bucket detected",
        "description": "S3 bucket logs-prod is publicly accessible",
        "recommendation": "Block public access immediately",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "run_id": "run-abc123"
    },
    {
        "severity": "HIGH",
        "finding_type": "CloudTrailDisabled",
        "resource_arn": "arn:aws:cloudtrail:us-east-1:123456789:trail/production-trail",
        "title": "CloudTrail logging disabled",
        "description": "CloudTrail production-trail is not logging",
        "recommendation": "Enable CloudTrail logging for audit compliance",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "run_id": "run-abc123"
    },
    {
        "severity": "HIGH",
        "finding_type": "UnencryptedS3",
        "resource_arn": "arn:aws:s3:::backup-bucket",
        "title": "S3 bucket encryption disabled",
        "description": "S3 bucket backup-bucket has encryption disabled",
        "recommendation": "Enable default encryption on S3 bucket",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "run_id": "run-abc123"
    }
]
# Configure your SNS Topic ARN here
SNS_TOPIC_ARN = "arn:aws:sns:us-east-1:827295473120:chandra-escalation-critical"

print("Using direct credentials to publish...")
base_factory = get_default_factory()

# Use the base factory directly without assuming the role
publisher = SNSPublisher(topic_arn=SNS_TOPIC_ARN, factory=base_factory)
print(f"Configured SNSPublisher with topic: {SNS_TOPIC_ARN}\n")

for esc in escalations:
    # 1. Map the raw dictionary to an EscalationPayload object
    # Extract service and region from ARN if available (e.g. arn:aws:s3:::bucket -> service=s3, region=global)
    service_name = esc["resource_arn"].split(":")[2] if "arn:aws:" in esc["resource_arn"] else "AWS"
    region_name = (esc["resource_arn"].split(":")[3] or "global") if "arn:aws:" in esc["resource_arn"] else "global"

    payload = EscalationPayload(
        finding_id=esc["finding_type"],
        resource_id=esc["resource_arn"],
        severity=esc["severity"].lower(),  # Must be one of: low, medium, high, critical
        service=service_name,
        region=region_name,
        summary=f"{esc['title']} - {esc['description']}",
        recommended_action=esc["recommendation"]
    )
    
    print(f"Publishing {esc['finding_type']} escalation to SNS...")
    
    # 2. Publish using the publisher
    result = publisher.publish(payload)
    
    # 3. Handle the EscalationResult
    if result.status == "success":
        print(f"✅ Successfully sent! Message ID: {result.message_id}")
    else:
        print(f"❌ Failed to send: {result.error}")
        
    print("-" * 70)
