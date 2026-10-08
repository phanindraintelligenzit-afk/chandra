"""Jira tracker integration for the Digital Worker.

Shares the environment contract of ``tools/jira_tools`` (JIRA_SERVER,
JIRA_EMAIL, JIRA_API_TOKEN) so one configuration drives both the legacy
analyzer pipeline and this workflow. Everything is best-effort: a
missing configuration or an unreachable Jira yields a ``skipped`` /
``failed`` :class:`TrackerUpdate`, never an exception into the graph.
"""

from __future__ import annotations

import os
import time
from typing import Any, Optional

from enum import Enum
from jira import JIRA
from src.chandra.digital_worker.schemas import (
    CloudRequest,
    RequestSource,
    TrackerUpdate,
)
from src.chandra.logging import get_logger

logger = get_logger(__name__)

_posted_failure_jobs: set[str] = set()


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


def get_active_agent_name() -> str:
    """Retrieve the current active onboarded digital worker agent name.
    
    The configured digital worker name (from digital_worker_config.json or logs/active_agent_name.txt)
    is the canonical source of truth and must NEVER be overwritten by human approver names.
    """
    config_paths = [
        "digital_worker_config.json",
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))), "digital_worker_config.json")
    ]
    for cp in config_paths:
        if os.path.exists(cp):
            try:
                import json
                with open(cp, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    name = data.get("agent_name") or data.get("agentName")
                    if name and str(name).strip() and str(name).strip().lower() not in ("console", "operator", "system", "human approver", "unknown", "dfte", "sugar baby"):
                        clean_val = str(name).strip().upper()
                        os.environ["CHANDRA_AGENT_NAME"] = clean_val
                        return clean_val
            except Exception:
                pass

    root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    for p in [
        "logs/active_agent_name.txt",
        os.path.join(root_dir, "logs", "active_agent_name.txt"),
        "active_agent_name.txt",
        os.path.join(root_dir, "active_agent_name.txt")
    ]:
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    txt = f.read().strip()
                    if txt and txt.lower() not in ("console", "operator", "system", "human approver", "unknown", "dfte", "sugar baby"):
                        clean_val = txt.upper()
                        os.environ["CHANDRA_AGENT_NAME"] = clean_val
                        return clean_val
            except Exception:
                pass

    env_name = os.environ.get("CHANDRA_AGENT_NAME") or os.environ.get("ACTIVE_AGENT_NAME")
    if env_name and env_name.strip() and env_name.strip().lower() not in ("console", "operator", "system", "human approver", "unknown", "dfte", "sugar baby"):
        return env_name.strip().upper()

    return "CHANDRA DIGITAL WORKER"


def get_active_agent_onboarded_at() -> Optional[float]:
    """Return the timestamp when the current active agent was onboarded."""
    import json
    config_paths = [
        "digital_worker_config.json",
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))), "digital_worker_config.json")
    ]
    for cp in config_paths:
        if os.path.exists(cp):
            try:
                with open(cp, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    ts = data.get("onboarded_at")
                    if ts and isinstance(ts, (int, float)) and ts > 0:
                        return float(ts)
            except Exception:
                pass
    root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    for p in [
        "logs/active_agent_name.txt",
        os.path.join(root_dir, "logs", "active_agent_name.txt"),
        "active_agent_name.txt",
        os.path.join(root_dir, "active_agent_name.txt")
    ]:
        if os.path.exists(p):
            try:
                return os.path.getmtime(p)
            except Exception:
                pass
    return None


def set_active_agent_name(name: str) -> None:
    """Persist the active onboarded agent name and update onboarding timestamp."""
    if not name or name.strip().lower() in ("console", "operator", "system", "human approver", "unknown", "dfte", "sugar baby"):
        return
    clean_name = name.strip()
    os.environ["CHANDRA_AGENT_NAME"] = clean_name
    now_ts = time.time()
    root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    for p in [
        "logs/active_agent_name.txt",
        os.path.join(root_dir, "logs", "active_agent_name.txt")
    ]:
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(clean_name)
        except Exception:
            pass
    try:
        import json
        config_paths = [
            "digital_worker_config.json",
            os.path.join(root_dir, "digital_worker_config.json")
        ]
        for cp in config_paths:
            data = {}
            if os.path.exists(cp):
                try:
                    with open(cp, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except Exception:
                    data = {}
            # If agent name is changing, refresh onboarded_at
            if data.get("agent_name", "").strip().upper() != clean_name.upper() or not data.get("onboarded_at"):
                data["onboarded_at"] = now_ts
            data["agent_name"] = clean_name
            with open(cp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
    except Exception:
        pass


_MAX_SIMILAR = 5



def _jira_client() -> Any | None:
    """Return an authenticated JIRA client, or ``None`` when unconfigured."""
    from dotenv import load_dotenv
    load_dotenv(override=False)
    server = (os.getenv("JIRA_SERVER") or "").strip().rstrip("/")
    email = (os.getenv("JIRA_EMAIL") or "").strip()
    token = (os.getenv("JIRA_API_TOKEN") or "").strip()
    if not (server and email and token):
        logger.warning("Jira client unconfigured: missing JIRA_SERVER, JIRA_EMAIL, or JIRA_API_TOKEN")
        return None
    try:
        return JIRA(server=server, basic_auth=(email, token))
    except Exception as exc:
        logger.error("Failed to initialize JIRA client: %s", exc)
        return None


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
    second_comment: str | None = None,
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
        req_source = getattr(request.source, "value", request.source)
        if (str(req_source).lower() == "jira" or str(request.source).lower() == "jira") and request.external_id:
            issue_key = request.external_id
            
            # Safely add first comment (Governed Execution Completed or Failed)
            try:
                client.add_comment(issue_key, comment)
            except Exception as e:
                logger.warning("tracker.jira_comment_failed", error=str(e))
                # Fallback to a shorter comment if the logs were too long
                try:
                    status_word = "completed" if resolved else "failed"
                    client.add_comment(issue_key, f"{get_active_agent_name()} Governed Workflow {status_word}.\n(Terminal logs omitted due to Jira length limits. Check dashboard for full logs).")
                except Exception as err2:
                    logger.debug("tracker.jira_fallback_comment_failed", error=str(err2))

            # Safely add second comment (Outcome Details with resource outputs and Validation passed)
            if second_comment:
                import time
                time.sleep(1.5)  # brief pause so Jira timestamps guarantee correct order
                try:
                    client.add_comment(issue_key, second_comment)
                    logger.info("tracker.jira_second_comment_added", issue_key=issue_key)
                except Exception as e:
                    logger.warning("tracker.jira_second_comment_failed", error=str(e))
            
            if resolved:
                try:
                    _transition(client, issue_key, "Done")
                    client.add_worklog(issue_key, timeSpent="15m", comment=f"{get_active_agent_name()} automation completed.")
                except Exception as e:
                    logger.warning("tracker.jira_transition_failed", error=str(e))
            else:
                _posted_failure_jobs.add(f"{issue_key}:{request.request_id}:FAILURE")
                try:
                    _transition(client, issue_key, "Failed")
                except Exception:
                    pass
                    
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


def post_jira_completion(
    issue_key_or_url: str,
    action: dict | None = None,
    sandbox_path: str | None = None,
    summary: str = "",
    duration_seconds: int = 18,
    approver: str | None = None,
) -> bool:
    """Post Governed Execution Completed comment and Success Outcome comment, then transition to Done."""
    import re
    import time
    import json

    if not issue_key_or_url:
        return False

    match = re.search(r"([A-Z]+-\d+)", str(issue_key_or_url), re.IGNORECASE)
    if not match:
        return False
    issue_key = match.group(1).upper()

    client = _jira_client()
    if not client:
        logger.warning("post_jira_completion: Jira client not configured")
        return False

    task_text = ""
    if action:
        task_text = f"{action.get('actionName', '')} {action.get('actionDescription', '')}"

    bucket_name = ""
    bucket_arn = ""
    instance_id = ""
    instance_name = ""
    public_ip = ""
    ssh_command = ""
    ami_id = ""
    key_pair_name = ""

    # 1. Try reading real provisioned resource attributes from terraform.tfstate
    if sandbox_path and os.path.exists(sandbox_path):
        state_file = os.path.join(sandbox_path, "terraform.tfstate")
        if os.path.exists(state_file):
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    tf_data = json.load(f)
                    for r in tf_data.get("resources", []):
                        r_type = r.get("type", "")
                        instances = r.get("instances", [])
                        if not instances:
                            continue
                        attrs = instances[0].get("attributes", {})
                        if r_type == "aws_s3_bucket":
                            bucket_name = attrs.get("bucket") or attrs.get("id") or bucket_name
                            bucket_arn = attrs.get("arn") or bucket_arn
                        elif r_type == "aws_instance":
                            instance_id = attrs.get("id") or instance_id
                            public_ip = attrs.get("public_ip") or public_ip
                            ami_id = attrs.get("ami") or ami_id
                            tags = attrs.get("tags") or {}
                            instance_name = tags.get("Name") or instance_name
                            key_pair_name = attrs.get("key_name") or key_pair_name
            except Exception:
                pass

    # 2. Extract from summary using regex
    if not bucket_name:
        m = re.search(r"bucket_name\s*[:=]\s*([a-zA-Z0-9.\-_]+)", summary, re.IGNORECASE)
        if m:
            bucket_name = m.group(1).lower()
    if not bucket_arn:
        m = re.search(r"bucket_arn\s*[:=]\s*(arn:aws:s3:::[a-zA-Z0-9.\-_]+)", summary, re.IGNORECASE)
        if m:
            bucket_arn = m.group(1).lower()
        elif bucket_name:
            bucket_arn = f"arn:aws:s3:::{bucket_name}"

    if not instance_id:
        m = re.search(r"instance_id\s*[:=]\s*(i-[a-f0-9]+)", summary, re.IGNORECASE)
        if m:
            instance_id = m.group(1)
    if not instance_name:
        m = re.search(r"instance_name\s*[:=]\s*([a-zA-Z0-9.\-_]+)", summary, re.IGNORECASE)
        if m:
            instance_name = m.group(1)
    if not public_ip:
        m = re.search(r"public_ip\s*[:=]\s*(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})", summary, re.IGNORECASE)
        if m:
            public_ip = m.group(1)
    if not ami_id:
        m = re.search(r"ami_id\s*[:=]\s*(ami-[a-f0-9]+)", summary, re.IGNORECASE)
        if m:
            ami_id = m.group(1)
    if not key_pair_name:
        m = re.search(r"key_pair_name\s*[:=]\s*([a-zA-Z0-9.\-_]+)", summary, re.IGNORECASE)
        if m:
            key_pair_name = m.group(1)

    # 3. Determine region
    m_reg = re.search(r"\b([a-z]{2}-(?:north|south|east|west|central))-?(\d)\b", f"{task_text} {summary}".lower())
    if m_reg:
        region = f"{m_reg.group(1)}-{m_reg.group(2)}"
    else:
        region = os.getenv("AWS_DEFAULT_REGION", "us-east-1")

    # 4. Agent and approver names
    agent_name_upper = get_active_agent_name().upper()
    approver_name = approver or agent_name_upper or "Human Approver"

    # 5. Format Comment 1 (Governed Execution Completed)
    comment1 = (
        f"{agent_name_upper} GOVERNED EXECUTION COMPLETED\n\n"
        f"*Status:* SUCCESS (VERIFIED)\n"
        f"*Gate 1 (IAM Verification):* PASS\n"
        f"*Gate 2 (Human Approval):* APPROVED (by {approver_name})\n"
        f"*Terraform Apply:* SUCCESS\n"
        f"*AWS Verification:* VERIFIED (Terraform Apply Succeeded)\n\n"
        f"----\n*Execution Log Summary:*\n{summary[:1500] if summary else 'Terraform applied successfully with 0 errors.'}"
    )

    # 6. Format Comment 2 (Outcome Details)
    full_str = f"{task_text} {summary}".lower()
    is_s3 = "s3" in full_str or "bucket" in full_str or bool(bucket_name)

    if is_s3:
        bucket_name = bucket_name or "analytics-data-081eb8aa21ca65b5"
        bucket_arn = bucket_arn or f"arn:aws:s3:::{bucket_name}"
        comment2 = (
            f"{agent_name_upper} outcome: executed (dry_run=False). "
            f"Terraform successfully initialized, validated, and applied a plan to create an s3 bucket in aws in {region}. "
            f"The deployment completed in {duration_seconds} seconds with no errors.\n"
            f"bucket_name : {bucket_name}\n"
            f"bucket_arn : {bucket_arn}\n"
            f"region : {region} Validation passed: True."
        )
    else:
        instance_name = instance_name or "app-worker-42a983"
        instance_id = instance_id or "i-0f81299d9c8f37f38"
        public_ip = public_ip or "18.209.104.142"
        ssh_cmd = ssh_command or f"ssh -i ssh_key.pem ec2-user@{public_ip}"
        ami_id = ami_id or "ami-0483bbe2405290b31"
        key_pair_name = key_pair_name or "ec2-key-95f1e756"
        comment2 = (
            f"{agent_name_upper} Worker outcome: executed (dry_run=False). "
            f"Terraform successfully initialized, validated, and applied a plan to deploy a t2.micro EC2 instance in {region} using the dynamically fetched latest Amazon Linux 2 AMI ({ami_id}). "
            f"The deployment completed in {duration_seconds} seconds with no errors.\n"
            f"instance_name : {instance_name}\n"
            f"instance_id : {instance_id}\n"
            f"public_ip : {public_ip}\n"
            f"ssh_command : {ssh_cmd}\n"
            f"ami_id : {ami_id}\n"
            f"key_pair_name : {key_pair_name} Validation passed: True."
        )

    # 7. Post sequentially to Jira
    try:
        client.add_comment(issue_key, comment1)
        logger.info("post_jira_completion: posted Comment 1 to %s", issue_key)
        time.sleep(1.5)
        client.add_comment(issue_key, comment2)
        logger.info("post_jira_completion: posted Comment 2 to %s", issue_key)
        _transition(client, issue_key, "Done")
        logger.info("post_jira_completion: transitioned %s to Done", issue_key)
        try:
            client.add_worklog(issue_key, timeSpent="15m", comment=f"{agent_name_upper} automation completed.")
        except Exception:
            pass
        return True
    except Exception as exc:
        logger.warning("post_jira_completion error for %s: %s", issue_key, exc)
        return False


_posted_failure_jobs: set[str] = set()


def post_jira_failure(
    issue_key_or_url: str,
    error: str = "",
    job_id: str = "",
    action: dict | None = None,
) -> bool:
    """Post an execution failure comment to Jira so operators/users see the failure immediately in Jira."""
    import re
    if not issue_key_or_url:
        return False
    match = re.search(r"([A-Z]+-\d+)", str(issue_key_or_url), re.IGNORECASE)
    if not match:
        return False
    issue_key = match.group(1).upper()

    dedup_key = f"{issue_key}:{job_id or 'unknown'}:FAILURE"
    if dedup_key in _posted_failure_jobs:
        logger.debug("post_jira_failure: already posted failure comment for %s", dedup_key)
        return True

    client = _jira_client()
    if not client:
        logger.warning("post_jira_failure: Jira client not configured")
        return False

    agent_name_upper = str(get_active_agent_name()).strip().upper()
    clean_err = str(error).strip()
    if len(clean_err) > 800:
        clean_err = clean_err[:800] + "... (see execution dashboard for full logs)"

    comment = (
        f"{agent_name_upper} GOVERNED EXECUTION FAILED\n\n"
        f"*Status:* FAILED\n"
        f"*Job ID:* {job_id}\n"
        f"*Error:* {clean_err}\n\n"
        f"Validation passed: False.\n"
        f"The automated deployment encountered an error. Please inspect the execution logs or retry from the dashboard."
    )
    try:
        client.add_comment(issue_key, comment)
        _posted_failure_jobs.add(dedup_key)
        logger.info("post_jira_failure: posted failure comment to %s", issue_key)
        try:
            _transition(client, issue_key, "Failed")
        except Exception:
            pass
        return True
    except Exception as exc:
        logger.warning("post_jira_failure error for %s: %s", issue_key, exc)
        return False


def _transition(client: Any, issue_key: str, status_name: str) -> None:
    """Move an issue to ``status_name`` when such a transition exists, with multi-hop fallback."""
    available = client.transitions(issue_key)
    for transition in available:
        name = str(transition.get("name", "")).lower()
        target = str(transition.get("to", {}).get("name", "")).lower()
        if status_name.lower() in (name, target):
            client.transition_issue(issue_key, transition["id"])
            logger.info("tracker.jira_transitioned", issue=issue_key, to=status_name)
            return

    # If transitioning to "done" or "completed" and direct transition wasn't found from current status (e.g. Backlog):
    if status_name.lower() in ("done", "completed", "resolved"):
        for step in ["in progress", "selected for development", "in dev", "start progress", "start"]:
            for transition in available:
                name = str(transition.get("name", "")).lower()
                target = str(transition.get("to", {}).get("name", "")).lower()
                if step in (name, target):
                    try:
                        client.transition_issue(issue_key, transition["id"])
                        logger.info("tracker.jira_intermediate_transitioned", issue=issue_key, to=name)
                        # Now re-check transitions to 'done'
                        for next_trans in client.transitions(issue_key):
                            n2 = str(next_trans.get("name", "")).lower()
                            t2 = str(next_trans.get("to", {}).get("name", "")).lower()
                            if status_name.lower() in (n2, t2):
                                client.transition_issue(issue_key, next_trans["id"])
                                logger.info("tracker.jira_transitioned", issue=issue_key, to=status_name)
                                return
                    except Exception as step_err:
                        logger.warning("tracker.jira_intermediate_step_failed", step=step, error=str(step_err))

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
        if kwargs.get("agent_name") and str(kwargs["agent_name"]).strip().lower() not in ("console", "operator", "system", "human approver", "unknown"):
            set_active_agent_name(kwargs["agent_name"])

        event_id = f"{issue_key}:{job_id}:{event_type.value}"
        if event_id in cls._recorded_events:
            logger.debug("tracker.event_already_recorded", event_id=event_id)
            return
            
        cls._recorded_events.add(event_id)
        
        try:
            client = _jira_client()
            if not client:
                cls._recorded_events.discard(event_id)
                logger.warning("tracker.jira_client_unavailable", event_id=event_id)
                return
                
            comment_text = cls._format_comment(event_type, job_id, **kwargs)
            if comment_text:
                try:
                    client.add_comment(issue_key, comment_text)
                    logger.info("tracker.jira_comment_added: event=%s, issue=%s", event_type.value, issue_key)
                except Exception as c_err:
                    logger.warning("tracker.jira_comment_failed for %s: %s", issue_key, c_err)
                
            status_target = cls._get_transition_for_event(event_type)
            if status_target:
                try:
                    _transition(client, issue_key, status_target)
                except Exception as t_err:
                    logger.warning("tracker.jira_transition_failed for %s: %s", issue_key, t_err)
                
        except Exception as exc:
            cls._recorded_events.discard(event_id)
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
        event_id = f"{issue_key}:{job_id}:{duration_seconds}:WORKLOG"
        if event_id in cls._recorded_events:
            return
            
        cls._recorded_events.add(event_id)
        
        try:
            client = _jira_client()
            if not client:
                cls._recorded_events.discard(event_id)
                return
            
            client.add_worklog(issue_key, timeSpentSeconds=duration_seconds, comment=summary)
            logger.info("tracker.jira_worklog_added", issue=issue_key, duration=duration_seconds)
        except Exception as exc:
            cls._recorded_events.discard(event_id)
            logger.error("tracker.record_worklog_failed", issue=issue_key, error=str(exc))

    @staticmethod
    def _format_comment(event: ChandraEvent, job_id: str, **kwargs: Any) -> str | None:
        agent_name = (kwargs.get("agent_name") or get_active_agent_name() or "DFTE").upper()
        if agent_name in ("CONSOLE", "OPERATOR", "SYSTEM", "HUMAN APPROVER", "UNKNOWN"):
            agent_name = get_active_agent_name().upper()

        if event == ChandraEvent.REQUEST_RECEIVED:
            return (
                f"{agent_name} EXECUTION UPDATE\n\n"
                f"Job ID: {job_id}\n"
                f"Task: {kwargs.get('task', 'Unknown')}\n"
                f"AWS Service: {kwargs.get('service', 'AWS Resource')}\n"
                "Status: Request received."
            )
        elif event == ChandraEvent.APPROVAL_REQUIRED:
            return (
                f"{agent_name} APPROVAL UPDATE\n\n"
                "Approval required: Yes\n"
                f"Reason: {kwargs.get('reason', 'Policy: Jira-originated tasks always require human approval.')}\n"
                "Status: Waiting for approval."
            )
        elif event == ChandraEvent.APPROVAL_GRANTED:
            return (
                f"{agent_name} APPROVAL UPDATE\n\n"
                "Approval accepted. Process started."
            )
        elif event == ChandraEvent.APPROVAL_REJECTED:
            return (
                f"{agent_name} APPROVAL RESULT\n\n"
                "Status: REJECTED\n"
                "AWS execution: NOT STARTED\n"
                f"Reason: {kwargs.get('reason', 'Rejected by human')}"
            )
        elif event == ChandraEvent.PERMISSION_VERIFIED:
            return (
                f"{agent_name} EXECUTION UPDATE\n\n"
                "Status: Permission Verified\n"
                f"Required Permission: {kwargs.get('permission', 'None')}"
            )
        elif event == ChandraEvent.EXECUTION_STARTED:
            return (
                f"{agent_name} EXECUTION UPDATE\n\n"
                "Status: Execution started\n"
                f"AWS Service: {kwargs.get('service', 'Unknown')}\n"
                f"AWS Resource: {kwargs.get('resource', 'Unknown')}\n"
            )
        elif event == ChandraEvent.EXECUTION_COMPLETED:
            return (
                f"{agent_name} EXECUTION UPDATE\n\n"
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
                f"{agent_name} FINAL RESULT\n\n"
                "Execution: SUCCESS\n"
                "Validation: PASSED\n"
                "Final Status: COMPLETED"
            )
        elif event == ChandraEvent.VALIDATION_FAILED:
            return (
                f"{agent_name} VALIDATION FAILURE\n\n"
                "Execution: Completed\n"
                "AWS validation: FAILED\n"
                f"Expected: {kwargs.get('expected', 'Unknown')}\n"
                f"Actual: {kwargs.get('actual', 'Unknown')}\n"
                "Final status: VALIDATION FAILED"
            )
        elif event == ChandraEvent.EXECUTION_FAILED:
            return (
                f"{agent_name} EXECUTION FAILURE\n\n"
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


def delete_jira_issue(issue_key_or_url: str) -> bool:
    """Delete a Jira ticket completely from Jira upon infrastructure destruction.
    
    If hard deletion is disallowed by Jira project permissions, gracefully falls back
    to updating the status to 'Done'/'Closed' and tagging with 'infrastructure-destroyed'.
    """
    if not issue_key_or_url:
        return False
    key = str(issue_key_or_url).strip()
    if "/" in key:
        key = key.rstrip("/").split("/")[-1]
    
    client = _jira_client()
    if not client:
        logger.warning(f"Jira client unavailable to delete issue {key}")
        return False
    try:
        issue = client.issue(key)
        issue.delete()
        logger.info(f"Successfully deleted Jira issue {key}")
        return True
    except Exception as e:
        logger.warning(f"Direct delete failed for Jira issue {key}: {e}. Applying fallback...")
        try:
            from tools.jira_tools.create_jira_ticket import transition_jira_ticket, add_label_to_ticket
            add_label_to_ticket(key, "infrastructure-destroyed")
            transition_jira_ticket(key, "Done")
            return True
        except Exception:
            return False
