"""Context collection: gather evidence around a classified request.

Collectors are deterministic and best-effort — each failure is recorded
in ``ContextBundle.errors`` and never aborts the workflow. AWS access
goes through the client factory and always uses paginators.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from src.chandra.aws.client_factory import AwsClientFactory, get_default_factory
from src.chandra.config import REPO_ROOT
from src.chandra.digital_worker.schemas import (
    CloudPlatform,
    CloudRequest,
    ContextBundle,
    ContextItem,
    RequestClassification,
)
from src.chandra.digital_worker.tracker import search_similar_issues
from src.chandra.logging import get_logger

logger = get_logger(__name__)

_MAX_ALARMS = 25
_MAX_RUNBOOKS = 5
_TOKEN = re.compile(r"[a-z0-9][a-z0-9\-]*")

# Directories scanned for internal runbooks / knowledge-base articles.
_KNOWLEDGE_DIRS: tuple[Path, ...] = (
    REPO_ROOT / "docs" / "runbooks",
    REPO_ROOT / "markdown_files",
)


class ContextCollector:
    """Collects monitoring, cloud-API, tracker and knowledge-base context."""

    def __init__(self, factory: AwsClientFactory | None = None) -> None:
        self._factory = factory

    @property
    def factory(self) -> AwsClientFactory:
        if self._factory is None:
            self._factory = get_default_factory()
        return self._factory

    def collect(
        self, request: CloudRequest, classification: RequestClassification
    ) -> ContextBundle:
        bundle = ContextBundle()

        self._collect_raw_event(request, bundle)
        if classification.platform in (CloudPlatform.AWS, CloudPlatform.UNKNOWN):
            self._collect_cloudwatch_alarms(bundle)
        self._collect_knowledge_base(request, classification, bundle)
        self._collect_prior_tickets(request, bundle)

        logger.info(
            "context.collected",
            request_id=request.request_id,
            items=len(bundle.items),
            errors=len(bundle.errors),
        )
        return bundle

    def _collect_raw_event(self, request: CloudRequest, bundle: ContextBundle) -> None:
        """The originating payload is itself first-class evidence."""
        if request.raw_payload:
            bundle.items.append(
                ContextItem(
                    provider=f"intake.{request.source.value}",
                    kind=(
                        "monitoring"
                        if "monitor" in request.source.value or request.source.value == "cloudwatch"
                        else "tracker"
                    ),
                    summary=f"Original {request.source.value} payload for '{request.title[:80]}'",
                    data={"raw_payload": request.raw_payload},
                )
            )

    def _collect_cloudwatch_alarms(self, bundle: ContextBundle) -> None:
        """Active CloudWatch alarms — the live monitoring picture."""
        try:
            cloudwatch = self.factory.client("cloudwatch")
            paginator = cloudwatch.get_paginator("describe_alarms")
            alarms: list[dict[str, Any]] = []
            for page in paginator.paginate(StateValue="ALARM"):
                for alarm in page.get("MetricAlarms", []):
                    alarms.append(
                        {
                            "name": alarm.get("AlarmName"),
                            "metric": alarm.get("MetricName"),
                            "namespace": alarm.get("Namespace"),
                            "reason": alarm.get("StateReason"),
                        }
                    )
                    if len(alarms) >= _MAX_ALARMS:
                        break
                if len(alarms) >= _MAX_ALARMS:
                    break
            bundle.items.append(
                ContextItem(
                    provider="cloudwatch_alarms",
                    kind="monitoring",
                    summary=f"{len(alarms)} CloudWatch alarm(s) currently in ALARM state",
                    data={"alarms": alarms},
                )
            )
        except Exception as exc:
            logger.warning("context.cloudwatch_unavailable", error=str(exc))
            bundle.errors.append(f"cloudwatch_alarms: {exc}")

    def _collect_knowledge_base(
        self,
        request: CloudRequest,
        classification: RequestClassification,
        bundle: ContextBundle,
    ) -> None:
        """Match internal runbooks / docs by token overlap with the request."""
        wanted = set(_TOKEN.findall(request.title.lower()))
        wanted.update(classification.services)
        wanted.update(classification.keywords)
        if not wanted:
            return
        matches: list[dict[str, Any]] = []
        try:
            for directory in _KNOWLEDGE_DIRS:
                if not directory.is_dir():
                    continue
                for path in sorted(directory.glob("*.md")):
                    name_tokens = set(_TOKEN.findall(path.stem.lower()))
                    overlap = wanted & name_tokens
                    if overlap:
                        matches.append(
                            {
                                "path": str(path.relative_to(REPO_ROOT)),
                                "matched": sorted(overlap),
                            }
                        )
                    if len(matches) >= _MAX_RUNBOOKS:
                        break
            if matches:
                bundle.items.append(
                    ContextItem(
                        provider="knowledge_base",
                        kind="knowledge_base",
                        summary=f"{len(matches)} runbook/document match(es) for this request",
                        data={"documents": matches},
                    )
                )
        except OSError as exc:
            logger.warning("context.knowledge_base_unavailable", error=str(exc))
            bundle.errors.append(f"knowledge_base: {exc}")

    def _collect_prior_tickets(self, request: CloudRequest, bundle: ContextBundle) -> None:
        """Similar past Jira issues (best-effort; requires JIRA_* env vars)."""
        try:
            issues = search_similar_issues(request.title)
            if issues:
                bundle.items.append(
                    ContextItem(
                        provider="jira_history",
                        kind="tracker",
                        summary=f"{len(issues)} similar past Jira issue(s)",
                        data={"issues": issues},
                    )
                )
        except Exception as exc:
            logger.warning("context.jira_history_unavailable", error=str(exc))
            bundle.errors.append(f"jira_history: {exc}")
