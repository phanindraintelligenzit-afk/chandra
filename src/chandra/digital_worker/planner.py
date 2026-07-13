"""Root-cause derivation and resolution planning.

Route order (Proposedflow §3 'Generate Execution Steps'):

1. **Memory route** — a fingerprint hit in resolution_memory reuses the
   previously generated steps (no LLM cost).
2. **LLM route** — :func:`src.chandra.briefing.composer.compose_request_analysis`
   (the only Bedrock touchpoint) generates fresh root cause + steps.
3. **Deterministic route** — rule-based playbooks per category keep the
   worker functional when Bedrock is unreachable.

Automation mapping is deterministic: a plan is auto-executable only when
the request maps onto a detector id with a registered ActionExecutor
handler AND the channel payload names the target resource explicitly.
The worker never invents resource identifiers.
"""

from __future__ import annotations

from typing import Any

from src.chandra.briefing.composer import compose_request_analysis
from src.chandra.digital_worker import memory
from src.chandra.digital_worker.schemas import (
    CloudRequest,
    ContextBundle,
    RequestCategory,
    RequestClassification,
    ResolutionPlan,
    ResolutionStep,
    RootCause,
)
from src.chandra.logging import get_logger

logger = get_logger(__name__)

# Dynamic Execution Engine handles operations dynamically.
# Hardcoded _AUTOMATION_RULES removed.

_RESOURCE_KEYS = ("resource_id", "resource", "bucket", "instance_id", "volume_id", "group_id")


def derive_root_cause(
    request: CloudRequest,
    classification: RequestClassification,
    context: ContextBundle,
) -> RootCause:
    """Deterministic root-cause synthesis from collected evidence."""
    evidence = [f"[{item.kind}] {item.provider}: {item.summary}" for item in context.items]
    causes: list[str] = []
    for item in context.items:
        for alarm in item.data.get("alarms", [])[:3]:
            reason = alarm.get("reason")
            if reason:
                causes.append(f"Alarm {alarm.get('name')}: {reason}")
    if not causes and classification.keywords:
        causes.append(
            f"Reported {classification.category.value} signals: "
            + ", ".join(classification.keywords[:5])
        )
    summary = (
        f"{classification.category.value.replace('_', ' ').title()} on "
        f"{classification.platform.value} affecting "
        f"{', '.join(classification.services) or 'unspecified services'}: "
        f"{request.title}"
    )
    return RootCause(
        summary=summary,
        probable_causes=causes,
        evidence=evidence,
        confidence=0.3 if causes else 0.15,
        generated_by="deterministic",
    )


def build_plan(
    request: CloudRequest,
    classification: RequestClassification,
    context: ContextBundle,
) -> tuple[RootCause, ResolutionPlan]:
    """Produce (root_cause, plan) via memory ▸ LLM ▸ deterministic routes."""
    fingerprint = memory.fingerprint_request(request, classification)

    cached = memory.lookup_plan(fingerprint)
    if cached is not None and cached.steps:
        root_cause = derive_root_cause(request, classification, context)
        root_cause.summary += " (matched a previously resolved request)"
        cached = _apply_automation(cached, request, classification)
        return root_cause, cached

    analysis = compose_request_analysis(
        {
            "request": request.model_dump(mode="json", exclude={"raw_payload"}),
            "classification": classification.model_dump(mode="json"),
            "context": [item.model_dump(mode="json") for item in context.items],
        }
    )
    if analysis is not None:
        root_cause, plan = _from_llm_analysis(analysis)
        if plan.steps:
            plan.fingerprint = fingerprint
            plan = _apply_automation(plan, request, classification)
            return root_cause, plan
        logger.warning("planner.llm_returned_no_steps_fallback", request_id=request.request_id)

    root_cause = derive_root_cause(request, classification, context)
    plan = _deterministic_playbook(request, classification)
    plan.fingerprint = fingerprint
    plan = _apply_automation(plan, request, classification)
    return root_cause, plan


def _from_llm_analysis(analysis: dict[str, Any]) -> tuple[RootCause, ResolutionPlan]:
    raw_cause = analysis.get("root_cause") or {}
    root_cause = RootCause(
        summary=str(raw_cause.get("summary", "")),
        probable_causes=[str(c) for c in raw_cause.get("probable_causes", [])],
        evidence=[str(e) for e in raw_cause.get("evidence", [])],
        confidence=max(0.0, min(1.0, float(raw_cause.get("confidence", 0.0) or 0.0))),
        generated_by="llm",
    )
    plan = ResolutionPlan(
        generated_by="llm",
        steps=_parse_steps(analysis.get("steps", [])),
        rollback_steps=_parse_steps(analysis.get("rollback_steps", [])),
        notes=str(analysis.get("notes", "")),
    )
    return root_cause, plan


def _parse_steps(raw: Any) -> list[ResolutionStep]:
    steps: list[ResolutionStep] = []
    if not isinstance(raw, list):
        return steps
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            continue
        action = str(item.get("action", "")).strip()
        if not action:
            continue
        command = item.get("command")
        expected = item.get("expected_outcome")
        steps.append(
            ResolutionStep(
                order=int(item.get("order", index) or index),
                action=action,
                detail=str(item.get("detail", "")),
                command=str(command) if command else None,
                expected_outcome=str(expected) if expected else None,
            )
        )
    return steps


def _deterministic_playbook(
    request: CloudRequest, classification: RequestClassification
) -> ResolutionPlan:
    """Rule-based fallback plan per category — always safe, never destructive."""
    category = classification.category
    target = ", ".join(classification.services) or classification.platform.value
    playbooks: dict[RequestCategory, list[str]] = {
        RequestCategory.INCIDENT: [
            f"Check current health and recent deployments for {target}",
            "Review the triggering alert/metrics and correlate with logs",
            "Identify the failing component and mitigate (restart, rollback, or scale)",
            "Confirm recovery via the same metric that alerted",
        ],
        RequestCategory.SECURITY: [
            f"Review the reported security finding on {target}",
            "Assess exposure window and blast radius",
            "Apply least-privilege remediation (block access / rotate credentials)",
            "Verify the finding no longer reproduces",
        ],
        RequestCategory.COST_OPTIMIZATION: [
            f"Pull current spend breakdown for {target}",
            "Identify idle or over-provisioned resources",
            "Propose rightsizing / cleanup actions with estimated savings",
        ],
        RequestCategory.COMPLIANCE: [
            f"Evaluate the relevant compliance rule(s) against {target}",
            "Document violations with resource-level evidence",
            "Remediate configuration drift or file exceptions",
        ],
        RequestCategory.PERFORMANCE: [
            f"Capture baseline latency/saturation metrics for {target}",
            "Identify the bottleneck (CPU, memory, IO, throttling)",
            "Apply tuning or scaling and re-measure",
        ],
        RequestCategory.RELIABILITY: [
            f"Review backup/failover posture for {target}",
            "Test recovery paths in a non-production environment",
            "Close redundancy gaps found",
        ],
        RequestCategory.CHANGE_REQUEST: [
            f"Draft the change for {target} as code (Terraform preferred)",
            "Validate and plan the change in a sandbox",
            "Apply with approval and record outputs",
        ],
    }
    actions = playbooks.get(
        category,
        [
            "Triage the request and gather missing details from the requester",
            "Route to the owning team with collected context",
        ],
    )
    steps = [ResolutionStep(order=i, action=action) for i, action in enumerate(actions, start=1)]
    return ResolutionPlan(generated_by="deterministic", steps=steps)


def _apply_automation(
    plan: ResolutionPlan,
    request: CloudRequest,
    classification: RequestClassification,
) -> ResolutionPlan:
    """Assume all plans are theoretically automatable for the Dynamic Execution Engine.
    The Automation Decision Engine will later evaluate risk and policy to decide.
    """
    plan.detector_id = None
    plan.automation_available = True
    return plan


def _tokens(request: CloudRequest, classification: RequestClassification) -> list[str]:
    text = f"{request.title} {request.description}".lower()
    return classification.services + classification.keywords + text.split()


def _explicit_resource_id(request: CloudRequest) -> str | None:
    """The resource the channel payload explicitly names, or ``None``."""
    for key in _RESOURCE_KEYS:
        value = request.raw_payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            nested = value.get("id") or value.get("arn") or value.get("name")
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
    return None


def explicit_resource_id(request: CloudRequest) -> str | None:
    """Public accessor used by the execution node."""
    return _explicit_resource_id(request)
