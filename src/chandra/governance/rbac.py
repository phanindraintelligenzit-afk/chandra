"""RBAC — role-based authorization (PRD L2 stage 6, §26.7, §8).

Two populations, per the PRD's onboarding workflow (§6) and stage-6 control
table: the **agent spinner** configures a Digital Worker (role, maturity,
responsibility areas, tasks, permission sets, deploy) and the **agent user**
submits work to it and approves what it proposes. Configuring the worker and
using the worker are different authorities and must not be conflated — the
spinner defines what the worker may ever do, the user decides whether one
particular run proceeds.

Enforcement is Python-level and deterministic. No LLM, no inference: a principal
either holds a capability or it does not.

**Identity is not yet authenticated.** There is no auth layer until Phase 3, so
the principal is supplied by the caller. That makes this an *authorization*
layer with an unauthenticated subject, which is only meaningful as defence in
depth. Phase 3 binds ``principal_id`` and ``tenant_id`` to verified JWT claims;
the enforcement points and capability model here do not change when it does.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from src.chandra.catalog.repository import DEFAULT_TENANT, SessionFactory
from src.chandra.db.models import PrincipalRoleRecord
from src.chandra.db.session import session_scope as _default_session_scope
from src.chandra.logging import get_logger

logger = get_logger(__name__)


class Role(StrEnum):
    AGENT_SPINNER = "agent_spinner"
    AGENT_USER = "agent_user"
    AUDITOR = "auditor"


class Capability(StrEnum):
    """What a principal may do. Named for the action, not the endpoint, so the
    same capability governs a REST call, a Slack command and a Jira transition."""

    CONFIGURE_WORKER = "configure_worker"
    """Edit the DFTE configuration: tasks, permission sets, KRAs, settings."""

    SUBMIT_REQUEST = "submit_request"
    """Send work to a Digital Worker."""

    APPROVE_REQUEST = "approve_request"
    """Grant the Gate 2 human approval that releases execution."""

    ATTACH_PERMISSION_SET = "attach_permission_set"
    """Choose the permission set a run executes under (Gate 1 input)."""

    VIEW_AUDIT = "view_audit"
    """Read audit trails, evidence and execution history."""


ROLE_CAPABILITIES: dict[Role, frozenset[Capability]] = {
    Role.AGENT_SPINNER: frozenset(
        {
            Capability.CONFIGURE_WORKER,
            Capability.SUBMIT_REQUEST,
            Capability.ATTACH_PERMISSION_SET,
            Capability.VIEW_AUDIT,
        }
    ),
    # Deliberately NOT a superset of the spinner. The user approves runs; the
    # spinner configures the worker. Neither implies the other: a spinner who
    # could also approve could widen a permission set and then approve its use
    # unilaterally, which is the separation-of-duty this split exists to prevent.
    Role.AGENT_USER: frozenset(
        {
            Capability.SUBMIT_REQUEST,
            Capability.APPROVE_REQUEST,
            Capability.VIEW_AUDIT,
        }
    ),
    Role.AUDITOR: frozenset({Capability.VIEW_AUDIT}),
}


class AuthorizationError(PermissionError):
    """A principal attempted something its roles do not permit."""

    def __init__(self, principal_id: str, capability: Capability, roles: list[Role]) -> None:
        self.principal_id = principal_id
        self.capability = capability
        self.roles = roles
        role_list = ", ".join(r.value for r in roles) or "none"
        super().__init__(
            f"Principal '{principal_id}' (roles: {role_list}) lacks capability '{capability.value}'"
        )


class RolesUnavailableError(RuntimeError):
    """Role assignments could not be read. Never treat this as 'no roles'."""


class Principal(BaseModel):
    """The acting subject. ``id`` is caller-supplied until Phase 3 authenticates it."""

    id: str
    tenant_id: str = DEFAULT_TENANT
    roles: list[Role] = Field(default_factory=list)

    @property
    def capabilities(self) -> frozenset[Capability]:
        granted: set[Capability] = set()
        for role in self.roles:
            granted |= ROLE_CAPABILITIES.get(role, frozenset())
        return frozenset(granted)

    def has(self, capability: Capability) -> bool:
        return capability in self.capabilities

    def require(self, capability: Capability) -> None:
        if not self.has(capability):
            raise AuthorizationError(self.id, capability, self.roles)


class RbacEngine:
    """Resolves a principal's roles for a tenant.

    ``default_roles`` applies to a principal with no assignment on record.

    When the tenant has **no assignments at all**, RBAC is treated as
    unconfigured and the default is every role: before this layer existed any
    caller could do anything, and there is no authenticated identity yet, so
    locking configuration writes on upgrade would break every running deployment
    while providing no real security (the subject is unverified either way).

    The moment an administrator grants a single role, the tenant is configured:
    the default drops to ``AGENT_USER`` and unassigned principals can no longer
    configure the worker. RBAC therefore switches on by being used, with no
    flag day. Passing ``default_roles`` explicitly overrides both behaviours —
    ``[]`` makes assignment mandatory, which is the right setting once Phase 3
    authenticates callers.
    """

    def __init__(
        self,
        tenant_id: str = DEFAULT_TENANT,
        session_factory: SessionFactory | None = None,
        default_roles: list[Role] | None = None,
        assignments: dict[str, list[Role]] | None = None,
    ) -> None:
        self.tenant_id = tenant_id
        self._session_factory = session_factory
        self._explicit_default_roles = None if default_roles is None else list(default_roles)
        self._assignments = assignments

    def _load_assignments(self) -> dict[str, list[Role]]:
        scope = self._session_factory or _default_session_scope
        try:
            with scope() as session:
                rows = session.scalars(
                    select(PrincipalRoleRecord).where(
                        PrincipalRoleRecord.tenant_id == self.tenant_id
                    )
                ).all()
                out: dict[str, list[Role]] = {}
                for row in rows:
                    # The principal is recorded even when its role string is
                    # unrecognised, so a principal whose only grant is an unknown
                    # role ends up with NO capabilities rather than falling
                    # through to the tenant default — an unreadable grant must
                    # not be more permissive than an absent one.
                    roles = out.setdefault(row.principal_id, [])
                    try:
                        roles.append(Role(row.role))
                    except ValueError:
                        logger.warning(
                            "rbac.unknown_role_ignored", role=row.role, principal=row.principal_id
                        )
                return out
        except SQLAlchemyError as exc:
            logger.error("rbac.assignments_unavailable", tenant=self.tenant_id, error=str(exc))
            raise RolesUnavailableError(str(exc)) from exc

    @property
    def assignments(self) -> dict[str, list[Role]]:
        if self._assignments is None:
            self._assignments = self._load_assignments()
        return self._assignments

    @property
    def default_roles(self) -> list[Role]:
        if self._explicit_default_roles is not None:
            return list(self._explicit_default_roles)
        if not self.assignments:
            # Unconfigured tenant: preserve pre-RBAC behaviour.
            return list(Role)
        return [Role.AGENT_USER]

    def resolve(self, principal_id: str | None) -> Principal:
        pid = (principal_id or "").strip() or "anonymous"
        roles = self.assignments.get(pid)
        if roles is None:
            logger.info("rbac.default_roles_applied", principal=pid, tenant=self.tenant_id)
            roles = list(self.default_roles)
        return Principal(id=pid, tenant_id=self.tenant_id, roles=roles)

    def authorize(self, principal_id: str | None, capability: Capability) -> Principal:
        """Resolve and check in one step. Raises ``AuthorizationError`` on refusal."""
        principal = self.resolve(principal_id)
        principal.require(capability)
        return principal
