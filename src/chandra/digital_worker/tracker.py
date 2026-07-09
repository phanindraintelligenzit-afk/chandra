"""Jira tracker integration for the Digital Worker.

Shares the environment contract of ``tools/jira_tools`` (JIRA_SERVER,
JIRA_EMAIL, JIRA_API_TOKEN) so one configuration drives both the legacy
analyzer pipeline and this workflow. Everything is best-effort: a
missing configuration or an unreachable Jira yields a ``skipped`` /
``failed`` :class:`TrackerUpdate`, never an exception into the graph.
"""

from __future__ import annotations

import os
from typing import Any

from jira import JIRA
from src.chandra.digital_worker.schemas import (
    CloudRequest,
    RequestSource,
    TrackerUpdate,
)
from src.chandra.logging import get_logger

logger = get_logger(__name__)

_MAX_SIMILAR = 5


def _jira_client() -> Any | None:
    """Return an authenticated JIRA client, or ``None`` when unconfigured."""
    server = os.getenv("JIRA_SERVER")
    email = os.getenv("JIRA_EMAIL")
    token = os.getenv("JIRA_API_TOKEN")
    if not (server and email and token):
        return None
    return JIRA(server=server, basic_auth=(email, token))


def search_similar_issues(title: str) -> list[dict[str, Any]]:
    """Find past Jira issues whose text resembles ``title``.

    Returns ``[]`` when Jira is unconfigured. Lets connection errors
    propagate — the caller (context collector) records them as context
    errors.
    """
    client = _jira_client()
    if client is None:
        return []
    sanitized = title.replace('"', " ").strip()[:100]
    if not sanitized:
        return []
    issues = client.search_issues(
        f'text ~ "{sanitized}" ORDER BY created DESC', maxResults=_MAX_SIMILAR
    )
    return [
        {
            "key": issue.key,
            "summary": issue.fields.summary,
            "status": str(issue.fields.status),
        }
        for issue in issues
    ]


def update_request_ticket(
    request: CloudRequest,
    comment: str,
    resolved: bool,
    project_key: str | None = None,
) -> TrackerUpdate:
    """Reflect the workflow outcome in Jira.

    * Request originated from Jira → comment on (and, when resolved,
      transition) the originating issue.
    * Any other channel → create a tracking issue in ``project_key``
      (env ``JIRA_PROJECT_KEY``, default ``DEV``) so every request has a
      ticket of record, then comment the outcome.
    """
    try:
        client = _jira_client()
    except Exception as exc:
        logger.warning("tracker.jira_client_failed", error=str(exc))
        return TrackerUpdate(status="failed", detail=str(exc))
    if client is None:
        logger.info("tracker.jira_unconfigured_skip", request_id=request.request_id)
        return TrackerUpdate(status="skipped", detail="JIRA_* environment variables not set")

    try:
        if request.source is RequestSource.JIRA and request.external_id:
            issue_key = request.external_id
            client.add_comment(issue_key, comment)
            if resolved:
                _transition(client, issue_key, "Done")
            logger.info("tracker.jira_updated", issue_key=issue_key, resolved=resolved)
            return TrackerUpdate(issue_key=issue_key, status="updated", detail="comment added")

        project = project_key or os.getenv("JIRA_PROJECT_KEY", "DEV")
        issue = client.create_issue(
            fields={
                "project": {"key": project},
                "summary": request.title[:250],
                "description": request.description or request.title,
                "issuetype": {"name": "Task"},
                "labels": ["chandra-digital-worker", request.source.value],
            }
        )
        client.add_comment(issue.key, comment)
        if resolved:
            _transition(client, issue.key, "Done")
        logger.info("tracker.jira_created", issue_key=issue.key, source=request.source.value)
        return TrackerUpdate(issue_key=issue.key, status="created", detail="tracking issue created")
    except Exception as exc:
        logger.warning("tracker.jira_update_failed", request_id=request.request_id, error=str(exc))
        return TrackerUpdate(status="failed", detail=str(exc))


def add_comment_to_issue(issue_key: str, comment: str) -> None:
    """Add a simple comment to an existing Jira issue."""
    try:
        client = _jira_client()
        if client:
            client.add_comment(issue_key, comment)
            logger.info("tracker.jira_comment_added", issue_key=issue_key)
    except Exception as exc:
        logger.warning("tracker.jira_comment_failed", issue_key=issue_key, error=str(exc))


def _transition(client: Any, issue_key: str, status_name: str) -> None:
    """Move an issue to ``status_name`` when such a transition exists."""
    for transition in client.transitions(issue_key):
        name = str(transition.get("name", "")).lower()
        target = str(transition.get("to", {}).get("name", "")).lower()
        if status_name.lower() in (name, target):
            client.transition_issue(issue_key, transition["id"])
            return
    logger.info("tracker.jira_transition_unavailable", issue_key=issue_key, status=status_name)
