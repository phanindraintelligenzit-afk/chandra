import json
import boto3

from chandra.escalation.schemas import (
    EscalationPayload,
    EscalationResult,
)
from chandra.escalation.formatter import format_escalation_message


class SNSPublisher:
    def __init__(self, topic_arn: str):
        self.topic_arn = topic_arn
        self.client = boto3.client("sns")

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