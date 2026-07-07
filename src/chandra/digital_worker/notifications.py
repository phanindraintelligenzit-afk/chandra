"""Outbound notifications: Slack, Microsoft Teams, email, SNS.

Channels are enabled purely by configuration — a channel without its
environment variables is reported as ``skipped``. Send failures are
captured per-channel; the workflow never fails because a webhook was
down.

Configuration:

* ``SLACK_WEBHOOK_URL``   — Slack incoming-webhook URL.
* ``TEAMS_WEBHOOK_URL``   — Teams incoming-webhook (connector) URL.
* ``SMTP_HOST`` / ``SMTP_PORT`` / ``SMTP_USER`` / ``SMTP_PASSWORD``
  ``NOTIFY_EMAIL_FROM`` / ``NOTIFY_EMAIL_TO`` — email notifications.
* ``SNS_TOPIC_ARN``       — reuses the existing escalation publisher.
"""

from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage

import requests
from src.chandra.config import settings
from src.chandra.digital_worker.schemas import NotificationResult
from src.chandra.escalation.publisher import SNSPublisher
from src.chandra.escalation.schemas import EscalationPayload
from src.chandra.logging import get_logger

logger = get_logger(__name__)

_TIMEOUT_S = 10


def notify_slack(title: str, body: str) -> NotificationResult:
    url = os.getenv("SLACK_WEBHOOK_URL")
    if not url:
        return NotificationResult(
            channel="slack", status="skipped", detail="SLACK_WEBHOOK_URL not set"
        )
    try:
        response = requests.post(url, json={"text": f"*{title}*\n{body}"}, timeout=_TIMEOUT_S)
        response.raise_for_status()
        logger.info("notify.slack_sent", title=title[:80])
        return NotificationResult(channel="slack", status="sent")
    except requests.RequestException as exc:
        logger.warning("notify.slack_failed", error=str(exc))
        return NotificationResult(channel="slack", status="failed", detail=str(exc))


def notify_teams(title: str, body: str) -> NotificationResult:
    url = os.getenv("TEAMS_WEBHOOK_URL")
    if not url:
        return NotificationResult(
            channel="teams", status="skipped", detail="TEAMS_WEBHOOK_URL not set"
        )
    try:
        response = requests.post(
            url,
            json={"@type": "MessageCard", "title": title, "text": body},
            timeout=_TIMEOUT_S,
        )
        response.raise_for_status()
        logger.info("notify.teams_sent", title=title[:80])
        return NotificationResult(channel="teams", status="sent")
    except requests.RequestException as exc:
        logger.warning("notify.teams_failed", error=str(exc))
        return NotificationResult(channel="teams", status="failed", detail=str(exc))


def notify_email(title: str, body: str) -> NotificationResult:
    host = os.getenv("SMTP_HOST")
    sender = os.getenv("NOTIFY_EMAIL_FROM")
    recipient = os.getenv("NOTIFY_EMAIL_TO")
    if not (host and sender and recipient):
        return NotificationResult(
            channel="email", status="skipped", detail="SMTP_HOST/NOTIFY_EMAIL_* not set"
        )
    try:
        message = EmailMessage()
        message["Subject"] = title
        message["From"] = sender
        message["To"] = recipient
        message.set_content(body)
        port = int(os.getenv("SMTP_PORT", "587"))
        with smtplib.SMTP(host, port, timeout=_TIMEOUT_S) as smtp:
            user = os.getenv("SMTP_USER")
            password = os.getenv("SMTP_PASSWORD")
            if user and password:
                smtp.starttls()
                smtp.login(user, password)
            smtp.send_message(message)
        logger.info("notify.email_sent", to=recipient, title=title[:80])
        return NotificationResult(channel="email", status="sent")
    except (OSError, smtplib.SMTPException) as exc:
        logger.warning("notify.email_failed", error=str(exc))
        return NotificationResult(channel="email", status="failed", detail=str(exc))


def notify_sns(payload: EscalationPayload) -> NotificationResult:
    """High-severity escalation via the existing SNS publisher."""
    topic_arn = settings.sns_topic_arn
    if not topic_arn:
        return NotificationResult(channel="sns", status="skipped", detail="SNS_TOPIC_ARN not set")
    result = SNSPublisher(topic_arn, region=settings.aws_default_region).publish(payload)
    status = "sent" if result.status == "success" else result.status
    return NotificationResult(channel="sns", status=status, detail=result.error or "")


def dispatch_all(title: str, body: str) -> list[NotificationResult]:
    """Fan the notification out to every configured chat/email channel."""
    return [
        notify_slack(title, body),
        notify_teams(title, body),
        notify_email(title, body),
    ]
