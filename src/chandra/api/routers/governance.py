"""Governance administration: policy rules and role assignments (PRD L2 stage 6).

First router extracted from ``fastapi_app``. Chosen to go first because it is the
newest code, has no shared mutable state (no job store, no thread pool, no graph
handle), and therefore isolates the mechanics of the split from the risk of
moving the older endpoints.

Every route requires CONFIGURE_WORKER, reads included: the policy rules and role
assignments are the configuration that decides who may do what, so listing them
is itself privileged.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from src.chandra.api import deps
from src.chandra.governance import Capability, PolicyRule, Role

router = APIRouter(tags=["governance"])


class PolicyRulePayload(BaseModel):
    name: str
    effect: str = Field(description="allow or deny")
    priority: int = 100
    enabled: bool = True
    reason: str = ""
    categories: list[str] = Field(default_factory=list)
    platforms: list[str] = Field(default_factory=list)
    responsibility_areas: list[str] = Field(default_factory=list)
    maturity_levels: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    risk_levels: list[str] = Field(default_factory=list)
    tasks: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)


class RoleGrantPayload(BaseModel):
    principal_id: str
    role: str


@router.get("/api/policy-rules")
def list_policy_rules(request: Request) -> JSONResponse:
    """List the tenant's policy rules, highest priority first."""
    deps.require_capability(request, Capability.CONFIGURE_WORKER)
    rules = deps.policy_store().list_rules()
    return JSONResponse(
        status_code=200,
        content={
            "status": "success",
            "count": len(rules),
            "rules": [r.model_dump(mode="json") for r in rules],
        },
    )


@router.put("/api/policy-rules/{rule_id}")
def upsert_policy_rule(rule_id: str, payload: PolicyRulePayload, request: Request) -> JSONResponse:
    """Create or replace one policy rule."""
    deps.require_capability(request, Capability.CONFIGURE_WORKER)
    try:
        rule = PolicyRule(id=rule_id, **payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid rule: {exc}") from exc
    deps.policy_store().upsert(rule)
    return JSONResponse(
        status_code=200, content={"status": "success", "rule": rule.model_dump(mode="json")}
    )


@router.delete("/api/policy-rules/{rule_id}")
def delete_policy_rule(rule_id: str, request: Request) -> JSONResponse:
    """Remove one policy rule."""
    deps.require_capability(request, Capability.CONFIGURE_WORKER)
    if not deps.policy_store().delete(rule_id):
        raise HTTPException(status_code=404, detail=f"No policy rule '{rule_id}'")
    return JSONResponse(status_code=200, content={"status": "success", "deleted": rule_id})


@router.get("/api/role-assignments")
def list_role_assignments(request: Request) -> JSONResponse:
    """List role assignments for the tenant."""
    deps.require_capability(request, Capability.CONFIGURE_WORKER)
    assignments: dict[str, Any] = deps.role_store().list_assignments()
    return JSONResponse(
        status_code=200,
        content={
            "status": "success",
            "count": len(assignments),
            "assignments": assignments,
            "known_roles": [r.value for r in Role],
        },
    )


@router.post("/api/role-assignments")
def grant_role(payload: RoleGrantPayload, request: Request) -> JSONResponse:
    """Grant a role to a principal.

    The first grant in a tenant switches RBAC from unconfigured to configured,
    which immediately drops unassigned principals to agent_user. The response
    reports that rather than leaving it to be discovered when someone loses
    access.
    """
    principal = deps.require_capability(request, Capability.CONFIGURE_WORKER)
    try:
        role = Role(payload.role)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown role '{payload.role}'. Known roles: "
            + ", ".join(r.value for r in Role),
        ) from exc
    store = deps.role_store()
    was_unconfigured = not store.list_assignments()
    store.grant(payload.principal_id, role, granted_by=principal.id)
    return JSONResponse(
        status_code=200,
        content={
            "status": "success",
            "principal_id": payload.principal_id,
            "role": role.value,
            "rbac_activated": was_unconfigured,
        },
    )


@router.delete("/api/role-assignments/{principal_id}/{role}")
def revoke_role(principal_id: str, role: str, request: Request) -> JSONResponse:
    """Revoke one role from a principal."""
    deps.require_capability(request, Capability.CONFIGURE_WORKER)
    try:
        parsed = Role(role)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Unknown role '{role}'") from exc
    if not deps.role_store().revoke(principal_id, parsed):
        raise HTTPException(status_code=404, detail="No such assignment")
    return JSONResponse(status_code=200, content={"status": "success"})
