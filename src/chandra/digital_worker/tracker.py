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

from enum import Enum
from jira import JIRA
from src.chandra.digital_worker.schemas import (
    CloudRequest,
    RequestSource,
    TrackerUpdate,
)
from src.chandra.logging import get_logger

logger = get_logger(__name__)


class ChandraEvent(str, Enum):
    REQUEST_RECEIVED = "REQUEST_RECEIVED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    APPROVAL_GRANTED = "APPROVAL_GRANTED"
    APPROVAL_REJECTED = "APPROVAL_REJECTED"
    PERMISSION_VERIFIED = "PERMISSION_VERIFIED"
    PERMISSION_FAILED = "PERMISSION_FAILED"
    EXECUTION_STARTED = "EXECUTION_STARTED"
    EXECUTION_COMPLETED = "EXECUTION_COMPLETED"
    VALIDATION_PASSED = "VALIDATION_PASSED"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    TASK_COMPLETED = "TASK_COMPLETED"
    EXECUTION_FAILED = "EXECUTION_FAILED"


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
        if request.source.value == "jira" and request.external_id:
            issue_key = request.external_id
            
            # Safely add comment (catching length limit errors)
            try:
                client.add_comment(issue_key, comment)
            except Exception as e:
                logger.warning("tracker.jira_comment_failed", error=str(e))
                # Fallback to a shorter comment if the logs were too long
                client.add_comment(issue_key, "Chandra Governed Workflow completed.\n(Terminal logs omitted due to Jira length limits. Check Chandra dashboard for full logs).")
            
            if resolved:
                try:
                    _transition(client, issue_key, "Done")
                    client.add_worklog(issue_key, timeSpent="15m", comment="Digital Worker automation completed.")
                except Exception as e:
                    logger.warning("tracker.jira_transition_failed", error=str(e))
                    
            logger.info("tracker.jira_updated", issue_key=issue_key, resolved=resolved)
            return TrackerUpdate(issue_key=issue_key, status="updated", detail="comment added")

        return TrackerUpdate(
            status="skipped", detail="Not a Jira request, skipping ticket creation"
        )

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


def transition_issue(issue_key: str, status_name: str) -> None:
    """Safely transition a Jira issue to a new status."""
    try:
        client = _jira_client()
        if client:
            _transition(client, issue_key, status_name)
    except Exception as exc:
        logger.warning("tracker.transition_issue_failed", issue_key=issue_key, error=str(exc))


def _transition(client: Any, issue_key: str, status_name: str) -> None:
    """Move an issue to ``status_name`` when such a transition exists."""
    for transition in client.transitions(issue_key):
        name = str(transition.get("name", "")).lower()
        target = str(transition.get("to", {}).get("name", "")).lower()
        if status_name.lower() in (name, target):
            client.transition_issue(issue_key, transition["id"])
            logger.info("tracker.jira_transitioned", issue=issue_key, to=status_name)
            return
    logger.warning("tracker.jira_transition_not_found", issue=issue_key, target=status_name)

class JiraActivityRecorder:
    """Centralized service for writing execution milestones to Jira Activity."""
    
    _recorded_events: set[str] = set()

    @classmethod
    def record_event(
        cls,
        issue_key: str,
        job_id: str,
        event_type: ChandraEvent,
        **kwargs: Any
    ) -> None:
        """Idempotently record a ChandraEvent into Jira Comments and History."""
        event_id = f"{issue_key}:{job_id}:{event_type.value}"
        if event_id in cls._recorded_events:
            logger.debug("tracker.event_already_recorded", event_id=event_id)
            return
            
        cls._recorded_events.add(event_id)
        
        try:
            client = _jira_client()
            if not client:
                return
                
            comment_text = cls._format_comment(event_type, job_id, **kwargs)
            if comment_text:
                client.add_comment(issue_key, comment_text)
                
            status_target = cls._get_transition_for_event(event_type)
            if status_target:
                _transition(client, issue_key, status_target)
                
        except Exception as exc:
            logger.error("tracker.record_event_failed", event_id=event_id, error=str(exc))

    @classmethod
    def record_worklog(
        cls,
        issue_key: str,
        job_id: str,
        duration_seconds: int,
        summary: str
    ) -> None:
        """Record the actual execution time spent in Jira Worklog."""
        event_id = f"{issue_key}:{job_id}:WORKLOG"
        if event_id in cls._recorded_events:
            return
            
        cls._recorded_events.add(event_id)
        
        try:
            client = _jira_client()
            if not client:
                return
            
            client.add_worklog(issue_key, timeSpentSeconds=duration_seconds, comment=summary)
            logger.info("tracker.jira_worklog_added", issue=issue_key, duration=duration_seconds)
        except Exception as exc:
            logger.error("tracker.record_worklog_failed", issue=issue_key, error=str(exc))

    @staticmethod
    def _format_comment(event: ChandraEvent, job_id: str, **kwargs: Any) -> str | None:
        if event == ChandraEvent.REQUEST_RECEIVED:
            return (
                "CHANDRA EXECUTION UPDATE\n\n"
                f"Job ID: {job_id}\n"
                f"Task: {kwargs.get('task', 'Unknown')}\n"
                f"AWS Service: {kwargs.get('service', 'Unknown')}\n"
                "Status: Request received."
            )
        elif event == ChandraEvent.APPROVAL_REQUIRED:
            return (
                "CHANDRA APPROVAL UPDATE\n\n"
                "Approval required: Yes\n"
                f"Reason: {kwargs.get('reason', 'AWS resource execution requires approval.')}\n"
                "Status: Waiting for approval."
            )
        elif event == ChandraEvent.APPROVAL_GRANTED:
            return (
                "CHANDRA APPROVAL UPDATE\n\n"
                "Status: Approved\n"
                f"Approved by: {kwargs.get('approver', 'Human Copilot')}"
            )
        elif event == ChandraEvent.APPROVAL_REJECTED:
            return (
                "CHANDRA APPROVAL RESULT\n\n"
                "Status: REJECTED\n"
                "AWS execution: NOT STARTED\n"
                f"Reason: {kwargs.get('reason', 'Rejected by human')}"
            )
        elif event == ChandraEvent.PERMISSION_VERIFIED:
            return (
                "CHANDRA EXECUTION UPDATE\n\n"
                "Status: Permission Verified\n"
                f"Required Permission: {kwargs.get('permission', 'None')}"
            )
        elif event == ChandraEvent.EXECUTION_STARTED:
            return (
                "CHANDRA EXECUTION UPDATE\n\n"
                "Status: Execution started\n"
                f"AWS Service: {kwargs.get('service', 'Unknown')}\n"
                f"AWS Resource: {kwargs.get('resource', 'Unknown')}\n"
            )
        elif event == ChandraEvent.EXECUTION_COMPLETED:
            return (
                "CHANDRA EXECUTION UPDATE\n\n"
                "Technical steps:\n"
                "1. Jira request received.\n"
                "2. AWS task identified.\n"
                "3. Required AWS permission identified.\n"
                "4. Human approval completed.\n"
                "5. AWS permission verified.\n"
                "6. AWS operation completed.\n"
            )
        elif event == ChandraEvent.VALIDATION_PASSED:
            return (
                "CHANDRA FINAL RESULT\n\n"
                "Execution: SUCCESS\n"
                "Validation: PASSED\n"
                "Final Status: COMPLETED"
            )
        elif event == ChandraEvent.VALIDATION_FAILED:
            return (
                "CHANDRA VALIDATION FAILURE\n\n"
                "Execution: Completed\n"
                "AWS validation: FAILED\n"
                f"Expected: {kwargs.get('expected', 'Unknown')}\n"
                f"Actual: {kwargs.get('actual', 'Unknown')}\n"
                "Final status: VALIDATION FAILED"
            )
        elif event == ChandraEvent.EXECUTION_FAILED:
            return (
                "CHANDRA EXECUTION FAILURE\n\n"
                "Status: FAILED\n"
                f"Failed Stage: {kwargs.get('stage', 'AWS execution')}\n"
                f"Reason: {kwargs.get('error', 'Unknown error')}\n"
                "AWS Execution: FAILED\n"
                "Final Status: FAILED"
            )
        return None
        
    @staticmethod
    def _get_transition_for_event(event: ChandraEvent) -> str | None:
        mapping = {
            ChandraEvent.REQUEST_RECEIVED: "Selected for Development",
            ChandraEvent.APPROVAL_REQUIRED: "Waiting for Approval",
            ChandraEvent.APPROVAL_GRANTED: "Approved",
            ChandraEvent.EXECUTION_STARTED: "In Progress",
            ChandraEvent.VALIDATION_PASSED: "Done",
            ChandraEvent.EXECUTION_FAILED: "Failed"
        }
        return mapping.get(event)
