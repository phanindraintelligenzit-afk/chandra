"""Shared dependencies for API routers.

Extracted from ``fastapi_app`` so routers can be split out of that module without
importing it — importing the app module from a router it is included in would be
circular. Everything here is a small factory, which also gives tests a single
seam to redirect at an in-memory database.
"""

from __future__ import annotations

from fastapi import HTTPException, Request
from src.chandra.catalog import DEFAULT_TENANT, ConfigRepository
from src.chandra.governance import (
    AuthorizationError,
    Capability,
    PolicyRuleStore,
    Principal,
    RbacEngine,
    RoleAssignmentStore,
    RolesUnavailableError,
)
from src.chandra.logging import get_logger
from src.chandra.observability import correlation

logger = get_logger(__name__)

PRINCIPAL_HEADER = "X-Principal-ID"


def config_repo(tenant_id: str | None = None) -> ConfigRepository:
    return ConfigRepository(tenant_id=tenant_id or correlation.get_tenant_id() or DEFAULT_TENANT)


def policy_store(tenant_id: str | None = None) -> PolicyRuleStore:
    return PolicyRuleStore(tenant_id=tenant_id or correlation.get_tenant_id() or DEFAULT_TENANT)


def role_store(tenant_id: str | None = None) -> RoleAssignmentStore:
    return RoleAssignmentStore(tenant_id=tenant_id or correlation.get_tenant_id() or DEFAULT_TENANT)


def rbac_engine(tenant_id: str | None = None) -> RbacEngine:
    return RbacEngine(tenant_id=tenant_id or correlation.get_tenant_id() or DEFAULT_TENANT)


def require_capability(request: Request, capability: Capability) -> Principal:
    """Enforce RBAC for the calling principal.

    With authentication enabled the principal was bound from a verified JWT claim
    by the edge middleware; the header is only consulted when auth is off.
    """
    principal_id = getattr(request.state, "principal_id", None) or request.headers.get(
        PRINCIPAL_HEADER
    )
    try:
        return rbac_engine().authorize(principal_id, capability)
    except AuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except RolesUnavailableError as exc:
        raise HTTPException(status_code=503, detail=f"Role assignments unavailable: {exc}") from exc
