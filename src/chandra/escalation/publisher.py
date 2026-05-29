import json

from chandra.aws.client_factory import get_default_factory
from chandra.escalation.schemas import (
    EscalationPayload,
    EscalationResult,
)
from chandra.escalation.formatter import format_escalation_message


class SNSPublisher:
    def __init__(self, topic_arn: str, region: str = "us-east-1", factory=None):
        self.topic_arn = topic_arn
        factory = factory or get_default_factory()
        self.client = factory.client("sns", region=region)

    def publish(self, payload: EscalationPayload) -> EscalationResult:
        try:
            # Base dictionary
            raw_message = format_escalation_message(payload)
            
            # Wrap in AWS Chatbot Custom Notification format so Slack doesn't drop it
            chatbot_message = {
                "version": "1.0",
                "source": "custom",
                "content": {
                    "title": f":rotating_light: [{payload.severity.upper()}] Escalation: {payload.finding_id}",
                    "description": f"{payload.summary}\n\n*Resource:* `{payload.resource_id}`\n*Service:* {payload.service} ({payload.region})",
                    "nextSteps": [
                        payload.recommended_action
                    ]
                }
            }

            response = self.client.publish(
                TopicArn=self.topic_arn,
                Message=json.dumps(chatbot_message),
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