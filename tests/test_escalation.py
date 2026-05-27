from chandra.escalation.schemas import EscalationPayload
from chandra.escalation.formatter import format_escalation_message


def test_payload_formatting():
    payload = EscalationPayload(
        finding_id="SEC-001",
        resource_id="bucket-123",
        severity="critical",
        service="s3",
        region="us-east-1",
        summary="Public S3 bucket",
        recommended_action="Block public access",
    )

    result = format_escalation_message(payload)

    assert result["severity"] == "critical"
    assert result["finding_id"] == "SEC-001"