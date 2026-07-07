"""Chandra Digital Cloud Engineer — event-driven request workflow.

This package implements the omnichannel Digital Worker described in
``Proposedflow.tex``: requests arrive from many channels (Jira, Slack,
Teams, email, REST, monitoring events, webhooks), are normalized into a
single :class:`~src.chandra.digital_worker.schemas.CloudRequest`
envelope, and flow through a LangGraph workflow:

    receive → understand → classify → identify_platform → collect_context
    → root_cause → plan_resolution (memory cache ▸ LLM ▸ deterministic)
    → risk_analysis → decision → execute_automation | engineer_guidance
    → validate → update_tracker → notify → audit → [approval] → persist

Architectural invariants inherited from the core platform:

* LangGraph is the only orchestration framework.
* Bedrock is called ONLY via :mod:`src.chandra.briefing.composer`.
* ``decision``, ``execute_automation`` and all routing are deterministic.
* Postgres writes happen only in the graph's ``persist`` node.
* AWS clients come from :func:`src.chandra.aws.client_factory.get_default_factory`.
"""

from src.chandra.digital_worker.schemas import (
    CloudPlatform,
    CloudRequest,
    RequestCategory,
    RequestPriority,
    RequestSource,
)

__all__ = [
    "CloudPlatform",
    "CloudRequest",
    "RequestCategory",
    "RequestPriority",
    "RequestSource",
]
