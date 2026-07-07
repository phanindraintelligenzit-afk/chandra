"""Multi-source intake: normalize channel-native payloads into ``CloudRequest``.

Every supported channel has a deterministic adapter. Adapters never call
an LLM and never call cloud APIs — they only reshape the payload the
channel delivered. Unknown or malformed fields degrade gracefully to
defaults instead of raising, so a noisy webhook can never take the
worker down; whatever could not be parsed stays available in
``raw_payload`` for the context-collection stage.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from src.chandra.digital_worker.schemas import (
    CloudRequest,
    RequestPriority,
    RequestSource,
)
from src.chandra.logging import get_logger

logger = get_logger(__name__)

_PRIORITY_ALIASES: dict[str, RequestPriority] = {
    "p1": RequestPriority.P1,
    "1": RequestPriority.P1,
    "highest": RequestPriority.P1,
    "critical": RequestPriority.P1,
    "blocker": RequestPriority.P1,
    "sev1": RequestPriority.P1,
    "p2": RequestPriority.P2,
    "2": RequestPriority.P2,
    "high": RequestPriority.P2,
    "major": RequestPriority.P2,
    "sev2": RequestPriority.P2,
    "p3": RequestPriority.P3,
    "3": RequestPriority.P3,
    "medium": RequestPriority.P3,
    "low": RequestPriority.P3,
    "lowest": RequestPriority.P3,
    "minor": RequestPriority.P3,
}


def parse_priority(value: Any) -> RequestPriority | None:
    """Map a channel-native priority label onto P1/P2/P3, or ``None``."""
    if value is None:
        return None
    return _PRIORITY_ALIASES.get(str(value).strip().lower())


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip()
    return str(value)


def _from_jira(payload: dict[str, Any]) -> CloudRequest:
    """Jira webhook (`jira:issue_created` / REST issue shape)."""
    issue = payload.get("issue", payload)
    fields = issue.get("fields", {}) if isinstance(issue, dict) else {}
    priority_field = fields.get("priority") or {}
    reporter = fields.get("reporter") or {}
    labels = fields.get("labels") or []
    return CloudRequest(
        source=RequestSource.JIRA,
        external_id=_text(issue.get("key")) or None if isinstance(issue, dict) else None,
        title=_text(fields.get("summary"), default="Jira request"),
        description=_text(fields.get("description")),
        priority=parse_priority(
            priority_field.get("name") if isinstance(priority_field, dict) else priority_field
        ),
        requester=_text(reporter.get("displayName") if isinstance(reporter, dict) else reporter)
        or None,
        labels=[_text(label) for label in labels if _text(label)],
        raw_payload=payload,
    )


def _from_slack(payload: dict[str, Any]) -> CloudRequest:
    """Slack Events API message / slash-command payload."""
    event = payload.get("event", payload)
    text = _text(event.get("text") or payload.get("text"), default="Slack request")
    title = text.splitlines()[0][:200] if text else "Slack request"
    return CloudRequest(
        source=RequestSource.SLACK,
        external_id=_text(event.get("ts") or event.get("event_ts")) or None,
        title=title,
        description=text,
        requester=_text(event.get("user") or payload.get("user_name")) or None,
        labels=[label for label in [_text(event.get("channel"))] if label],
        raw_payload=payload,
    )


def _from_teams(payload: dict[str, Any]) -> CloudRequest:
    """Microsoft Teams bot-framework activity payload."""
    text = _text(payload.get("text"), default="Teams request")
    sender = payload.get("from") or {}
    return CloudRequest(
        source=RequestSource.TEAMS,
        external_id=_text(payload.get("id")) or None,
        title=text.splitlines()[0][:200] if text else "Teams request",
        description=text,
        requester=_text(sender.get("name") if isinstance(sender, dict) else sender) or None,
        raw_payload=payload,
    )


def _from_email(payload: dict[str, Any]) -> CloudRequest:
    """Parsed inbound email ({subject, body, from, message_id})."""
    return CloudRequest(
        source=RequestSource.EMAIL,
        external_id=_text(payload.get("message_id")) or None,
        title=_text(payload.get("subject"), default="Email request"),
        description=_text(payload.get("body") or payload.get("text")),
        requester=_text(payload.get("from") or payload.get("sender")) or None,
        raw_payload=payload,
    )


def _from_rest_api(payload: dict[str, Any]) -> CloudRequest:
    """First-party REST submission ({title, description, priority, requester})."""
    return CloudRequest(
        source=RequestSource.REST_API,
        external_id=_text(payload.get("external_id")) or None,
        title=_text(payload.get("title") or payload.get("actionName"), default="API request"),
        description=_text(payload.get("description") or payload.get("actionDescription")),
        priority=parse_priority(payload.get("priority") or payload.get("priorityLevel")),
        requester=_text(payload.get("requester")) or None,
        labels=[_text(label) for label in payload.get("labels", []) if _text(label)],
        raw_payload=payload,
    )


def _from_monitoring(payload: dict[str, Any]) -> CloudRequest:
    """Generic monitoring event ({alert_name, description, severity, source_system})."""
    return CloudRequest(
        source=RequestSource.MONITORING,
        external_id=_text(payload.get("alert_id") or payload.get("event_id")) or None,
        title=_text(payload.get("alert_name") or payload.get("title"), default="Monitoring alert"),
        description=_text(payload.get("description") or payload.get("message")),
        priority=parse_priority(payload.get("severity") or payload.get("priority")),
        labels=[label for label in [_text(payload.get("source_system"))] if label],
        raw_payload=payload,
    )


def _from_cloudwatch(payload: dict[str, Any]) -> CloudRequest:
    """CloudWatch alarm notification (direct or SNS-wrapped)."""
    alarm = payload
    if "AlarmName" not in alarm and isinstance(payload.get("Message"), dict):
        alarm = payload["Message"]
    name = _text(alarm.get("AlarmName"), default="CloudWatch alarm")
    reason = _text(alarm.get("NewStateReason") or alarm.get("AlarmDescription"))
    state = _text(alarm.get("NewStateValue"))
    return CloudRequest(
        source=RequestSource.CLOUDWATCH,
        external_id=_text(alarm.get("AlarmArn")) or None,
        title=f"[{state}] {name}" if state else name,
        description=reason,
        priority=RequestPriority.P1 if state == "ALARM" else None,
        labels=[label for label in [_text(alarm.get("Region"))] if label],
        raw_payload=payload,
    )


def _from_azure_monitor(payload: dict[str, Any]) -> CloudRequest:
    """Azure Monitor common alert schema."""
    data = payload.get("data", payload)
    essentials = data.get("essentials", {}) if isinstance(data, dict) else {}
    severity = _text(essentials.get("severity"))
    sev_map = {"sev0": RequestPriority.P1, "sev1": RequestPriority.P1, "sev2": RequestPriority.P2}
    return CloudRequest(
        source=RequestSource.AZURE_MONITOR,
        external_id=_text(essentials.get("alertId")) or None,
        title=_text(essentials.get("alertRule"), default="Azure Monitor alert"),
        description=_text(essentials.get("description")),
        priority=sev_map.get(severity.lower(), RequestPriority.P3) if severity else None,
        labels=[_text(t) for t in essentials.get("targetResourceType", "").split(",") if _text(t)],
        raw_payload=payload,
    )


def _from_gcp_monitoring(payload: dict[str, Any]) -> CloudRequest:
    """GCP Cloud Monitoring webhook (incident envelope)."""
    incident = payload.get("incident", payload)
    policy = _text(incident.get("policy_name"), default="GCP Monitoring alert")
    return CloudRequest(
        source=RequestSource.GCP_MONITORING,
        external_id=_text(incident.get("incident_id")) or None,
        title=policy,
        description=_text(incident.get("summary") or incident.get("documentation")),
        priority=RequestPriority.P1 if _text(incident.get("state")) == "open" else None,
        raw_payload=payload,
    )


def _from_webhook(payload: dict[str, Any]) -> CloudRequest:
    """Generic webhook — best-effort field mapping."""
    return CloudRequest(
        source=RequestSource.WEBHOOK,
        external_id=_text(payload.get("id") or payload.get("event_id")) or None,
        title=_text(
            payload.get("title") or payload.get("summary") or payload.get("event"),
            default="Webhook event",
        ),
        description=_text(
            payload.get("description") or payload.get("message") or payload.get("body")
        ),
        priority=parse_priority(payload.get("priority") or payload.get("severity")),
        requester=_text(payload.get("requester") or payload.get("sender")) or None,
        raw_payload=payload,
    )


_ADAPTERS: dict[RequestSource, Callable[[dict[str, Any]], CloudRequest]] = {
    RequestSource.JIRA: _from_jira,
    RequestSource.SLACK: _from_slack,
    RequestSource.TEAMS: _from_teams,
    RequestSource.EMAIL: _from_email,
    RequestSource.REST_API: _from_rest_api,
    RequestSource.MONITORING: _from_monitoring,
    RequestSource.CLOUDWATCH: _from_cloudwatch,
    RequestSource.AZURE_MONITOR: _from_azure_monitor,
    RequestSource.GCP_MONITORING: _from_gcp_monitoring,
    RequestSource.WEBHOOK: _from_webhook,
}

SUPPORTED_SOURCES: tuple[str, ...] = tuple(source.value for source in _ADAPTERS)


def normalize_request(source: RequestSource | str, payload: dict[str, Any]) -> CloudRequest:
    """Normalize a channel-native ``payload`` into a :class:`CloudRequest`.

    Raises :class:`ValueError` for an unknown source — the only condition
    callers must handle; malformed payload fields degrade to defaults.
    """
    try:
        resolved = RequestSource(source) if not isinstance(source, RequestSource) else source
    except ValueError as exc:
        raise ValueError(
            f"unsupported request source {source!r}; expected one of {SUPPORTED_SOURCES}"
        ) from exc
    request = _ADAPTERS[resolved](payload)
    logger.info(
        "intake.request_normalized",
        source=resolved.value,
        request_id=request.request_id,
        external_id=request.external_id,
        title=request.title[:120],
    )
    return request
