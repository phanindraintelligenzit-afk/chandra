"""RBAC: roles, capabilities and enforcement (PRD §26.7)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from src.chandra.db.models import Base, PrincipalRoleRecord
from src.chandra.governance import (
    ROLE_CAPABILITIES,
    AuthorizationError,
    Capability,
    Principal,
    RbacEngine,
    Role,
    RolesUnavailableError,
)


@pytest.fixture
def scope() -> Iterator[Any]:
    engine = create_engine(
        "sqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def _scope() -> Iterator[Session]:
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    yield _scope


def _grant(scope: Any, principal: str, role: Role, tenant: str = "default") -> None:
    with scope() as session:
        session.add(PrincipalRoleRecord(tenant_id=tenant, principal_id=principal, role=role.value))


class TestCapabilityModel:
    def test_spinner_configures_but_cannot_approve(self) -> None:
        """Separation of duty: a principal who can widen a permission set must not
        also be able to approve its use."""
        spinner = Principal(id="s", roles=[Role.AGENT_SPINNER])
        assert spinner.has(Capability.CONFIGURE_WORKER)
        assert not spinner.has(Capability.APPROVE_REQUEST)

    def test_user_approves_but_cannot_configure(self) -> None:
        user = Principal(id="u", roles=[Role.AGENT_USER])
        assert user.has(Capability.APPROVE_REQUEST)
        assert not user.has(Capability.CONFIGURE_WORKER)

    def test_neither_role_is_a_superset_of_the_other(self) -> None:
        spinner = ROLE_CAPABILITIES[Role.AGENT_SPINNER]
        user = ROLE_CAPABILITIES[Role.AGENT_USER]
        assert not spinner.issubset(user)
        assert not user.issubset(spinner)

    def test_auditor_is_read_only(self) -> None:
        auditor = Principal(id="a", roles=[Role.AUDITOR])
        assert auditor.capabilities == frozenset({Capability.VIEW_AUDIT})

    def test_multiple_roles_union_their_capabilities(self) -> None:
        both = Principal(id="b", roles=[Role.AGENT_SPINNER, Role.AGENT_USER])
        assert both.has(Capability.CONFIGURE_WORKER)
        assert both.has(Capability.APPROVE_REQUEST)

    def test_no_roles_grants_nothing(self) -> None:
        assert Principal(id="n", roles=[]).capabilities == frozenset()

    def test_require_raises_with_an_explanatory_message(self) -> None:
        with pytest.raises(AuthorizationError) as exc:
            Principal(id="s", roles=[Role.AGENT_SPINNER]).require(Capability.APPROVE_REQUEST)
        assert "approve_request" in str(exc.value)
        assert "agent_spinner" in str(exc.value)


class TestResolution:
    def test_assigned_roles_are_read_from_postgres(self, scope: Any) -> None:
        _grant(scope, "priya", Role.AGENT_SPINNER)
        principal = RbacEngine(session_factory=scope).resolve("priya")
        assert principal.roles == [Role.AGENT_SPINNER]
        assert principal.has(Capability.CONFIGURE_WORKER)

    def test_assignments_are_tenant_scoped(self, scope: Any) -> None:
        _grant(scope, "priya", Role.AGENT_SPINNER, tenant="acme")
        _grant(scope, "someone-else", Role.AGENT_USER, tenant="other")
        other = RbacEngine(tenant_id="other", session_factory=scope).resolve("priya")
        assert other.roles == [Role.AGENT_USER]  # 'other' default, not acme's grant
        assert not other.has(Capability.CONFIGURE_WORKER)

    def test_unconfigured_tenant_preserves_pre_rbac_behaviour(self, scope: Any) -> None:
        """No assignments anywhere => RBAC is not in use => nothing is locked out
        on upgrade. This is the backward-compatibility guarantee."""
        principal = RbacEngine(session_factory=scope).resolve("anyone")
        assert principal.has(Capability.CONFIGURE_WORKER)
        assert principal.has(Capability.APPROVE_REQUEST)

    def test_first_assignment_activates_rbac_for_everyone_else(self, scope: Any) -> None:
        """Granting one role configures the tenant: unassigned principals drop to
        agent_user and can no longer configure the worker. RBAC switches on by
        being used, with no flag day."""
        _grant(scope, "priya", Role.AGENT_SPINNER)
        stranger = RbacEngine(session_factory=scope).resolve("stranger")
        assert stranger.roles == [Role.AGENT_USER]
        assert not stranger.has(Capability.CONFIGURE_WORKER)

    def test_tenant_can_require_explicit_assignment(self, scope: Any) -> None:
        """Once identity is authenticated, a tenant sets default_roles=[] so an
        unassigned principal has no capabilities at all."""
        engine = RbacEngine(session_factory=scope, default_roles=[])
        principal = engine.resolve("stranger")
        assert principal.capabilities == frozenset()
        with pytest.raises(AuthorizationError):
            engine.authorize("stranger", Capability.SUBMIT_REQUEST)

    def test_missing_principal_id_resolves_to_anonymous(self, scope: Any) -> None:
        assert RbacEngine(session_factory=scope).resolve(None).id == "anonymous"
        assert RbacEngine(session_factory=scope).resolve("   ").id == "anonymous"

    def test_unknown_role_in_the_table_is_ignored_not_trusted(self, scope: Any) -> None:
        """A role string the code does not know grants nothing, rather than
        failing open or crashing."""
        with scope() as session:
            session.add(
                PrincipalRoleRecord(tenant_id="default", principal_id="x", role="superadmin")
            )
        principal = RbacEngine(session_factory=scope).resolve("x")
        assert principal.capabilities == frozenset()

    def test_unreadable_assignments_raise_rather_than_granting_defaults(self) -> None:
        @contextmanager
        def _broken() -> Iterator[Session]:
            raise SQLAlchemyError("connection refused")
            yield  # pragma: no cover

        with pytest.raises(RolesUnavailableError):
            RbacEngine(session_factory=_broken).resolve("anyone")

    def test_authorize_returns_the_principal_on_success(self, scope: Any) -> None:
        _grant(scope, "nagendra", Role.AGENT_USER)
        principal = RbacEngine(session_factory=scope).authorize(
            "nagendra", Capability.APPROVE_REQUEST
        )
        assert principal.id == "nagendra"


class TestApiEnforcement:
    """Configuration endpoints require CONFIGURE_WORKER (PRD §26.7)."""

    @pytest.fixture
    def client(self, scope: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
        import fastapi_app
        from fastapi.testclient import TestClient
        from src.chandra.catalog import ConfigRepository

        _grant(scope, "priya", Role.AGENT_SPINNER)
        _grant(scope, "nagendra", Role.AGENT_USER)
        monkeypatch.setattr(
            fastapi_app,
            "_config_repo",
            lambda tenant_id="default": ConfigRepository(tenant_id, scope),
        )
        monkeypatch.setattr(
            fastapi_app,
            "_rbac_engine",
            lambda tenant_id="default": RbacEngine(tenant_id=tenant_id, session_factory=scope),
        )
        yield TestClient(fastapi_app.app)

    def test_spinner_may_write_the_catalogue(self, client: Any) -> None:
        r = client.put(
            "/api/aws-tasks",
            json={"tasks": [{"id": "t1", "name": "Create S3"}]},
            headers={"X-Principal-ID": "priya"},
        )
        assert r.status_code == 200

    def test_agent_user_may_not_write_the_catalogue(self, client: Any) -> None:
        r = client.put(
            "/api/aws-tasks",
            json={"tasks": [{"id": "t1", "name": "Create S3"}]},
            headers={"X-Principal-ID": "nagendra"},
        )
        assert r.status_code == 403
        assert "configure_worker" in r.json()["detail"]

    def test_permission_sets_and_kras_are_equally_protected(self, client: Any) -> None:
        headers = {"X-Principal-ID": "nagendra"}
        assert (
            client.put(
                "/api/permission-sets", json={"permissions": []}, headers=headers
            ).status_code
            == 403
        )
        assert client.put("/customKras", json={"kras": []}, headers=headers).status_code == 403
        assert (
            client.post(
                "/settings/digital-worker", json={"max_iterations": 3}, headers=headers
            ).status_code
            == 403
        )

    def test_reads_remain_open(self, client: Any) -> None:
        """Only writes are gated here; read protection arrives with authentication
        in Phase 3, and gating reads now would break the console for everyone."""
        assert (
            client.get("/api/aws-tasks", headers={"X-Principal-ID": "nagendra"}).status_code == 200
        )
