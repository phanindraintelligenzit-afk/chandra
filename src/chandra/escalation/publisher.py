import json

from src.chandra.aws.client_factory import get_default_factory
from src.chandra.escalation.schemas import (
    EscalationPayload,
    EscalationResult,
)


class SNSPublisher:
    def __init__(self, topic_arn: str, region: str = "us-east-1", factory=None):
        self.topic_arn = topic_arn
        factory = factory or get_default_factory()
        self.client = factory.client("sns", region=region)

    def publish(self, payload: EscalationPayload) -> EscalationResult:
        try:
            # Wrap in AWS Chatbot Custom Notification format so Slack doesn't drop it
            chatbot_message = {
                "version": "1.0",
                "source": "custom",
                "content": {
                    "title": (
                        f":rotating_light: [{payload.severity.upper()}] "
                        f"Escalation: {payload.finding_id}"
                    ),
                    "description": (
                        f"{payload.summary}\n\n*Resource:* `{payload.resource_id}`\n"
                        f"*Service:* {payload.service} ({payload.region})"
                    ),
                    "nextSteps": [payload.recommended_action],
                },
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
            try:
                import botocore.exceptions  # noqa: PLC0415  # lazy: optional dep

                if (
                    isinstance(e, botocore.exceptions.ClientError)
                    and e.response["Error"]["Code"] == "NotFound"
                ):
                    return EscalationResult(
                        status="skipped",
                        error="Topic does not exist",
                    )
            except ImportError:
                pass
            return EscalationResult(
                status="failed",
                error=str(e),
            )
