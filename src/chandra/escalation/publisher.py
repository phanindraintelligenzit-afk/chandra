import json

from chandra.aws.client_factory import get_default_factory
from chandra.escalation.schemas import (
    EscalationPayload,
    EscalationResult,
)
from chandra.escalation.formatter import format_escalation_message


class SNSPublisher:
    def __init__(self, topic_arn: str, region: str = "us-east-1"):
        self.topic_arn = topic_arn
        factory = get_default_factory()
        self.client = factory.client("sns", region=region)

    def publish(self, payload: EscalationPayload) -> EscalationResult:
        try:
            message = format_escalation_message(payload)

            response = self.client.publish(
                TopicArn=self.topic_arn,
                Message=json.dumps(message),
                Subject=f"[{payload.severity.upper()}] Chandra Escalation",
            )

            return EscalationResult(
                status="success",
                message_id=response.get("MessageId"),
            )

        except Exception as e:
            return EscalationResult(
                status="failed",
                error=str(e),
            )