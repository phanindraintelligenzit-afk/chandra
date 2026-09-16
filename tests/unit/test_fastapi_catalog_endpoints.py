"""Catalog endpoints read/write Postgres via ConfigRepository, not JSON files.

The repository is routed to in-memory SQLite.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from src.chandra.catalog import ConfigRepository
from src.chandra.db.models import Base
from src.chandra.governance import RbacEngine

REPO_ROOT = Path(__file__).resolve().parents[2]
LEGACY_FILES = [
    "aws_tasks.json",
    "aws_permissions.json",
    "customKras.json",
    "agent_memory.json",
    "executions.json",
    "digital_worker_config.json",
]
SEEDS_DIR = REPO_ROOT / "src" / "chandra" / "catalog" / "seeds"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    import fastapi_app

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
        finally:
            session.close()

    monkeypatch.setattr(
        fastapi_app, "_config_repo", lambda tenant_id="default": ConfigRepository(tenant_id, _scope)
    )
    # RBAC reads its assignments from the same SQLite scope. None are granted, so
    # the tenant is unconfigured and every caller retains the pre-RBAC capabilities.
    monkeypatch.setattr(
        fastapi_app,
        "_rbac_engine",
        lambda tenant_id="default": RbacEngine(tenant_id=tenant_id, session_factory=_scope),
    )
    # No lifespan context on purpose: these endpoints need no startup, and running
    # a second startup/shutdown cycle would tear down the shared job executor that
    # test_fastapi_intake's module-scoped client relies on.
    yield TestClient(fastapi_app.app)


def _mtimes() -> dict[str, float | None]:
    files = [REPO_ROOT / f for f in LEGACY_FILES] + sorted(SEEDS_DIR.glob("*.json"))
    return {str(p): p.stat().st_mtime if p.exists() else None for p in files}


def test_no_legacy_json_state_files_at_repo_root() -> None:
    assert [f for f in LEGACY_FILES if (REPO_ROOT / f).exists()] == []


def test_custom_kras_round_trip_without_touching_disk(client: TestClient) -> None:
    before = _mtimes()
    r = client.put(
        "/customKras", json={"kras": [{"name": "Cost"}, {"name": "cost"}, {"code": "Sec"}]}
    )
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 2
    got = client.get("/customKras").json()
    assert [k["name"] for k in got["kras"]] == ["Cost", "Sec"]
    assert _mtimes() == before  # no JSON file was written


def test_aws_tasks_put_then_get(client: TestClient) -> None:
    tasks = [{"id": "t1", "name": "Create S3", "category": "S3", "version": 3}]
    assert client.put("/api/aws-tasks", json={"tasks": tasks}).json()["count"] == 1
    body = client.get("/api/aws-tasks").json()
    assert body["count"] == 1
    assert body["tasks"][0]["version"] == 3
    # legacy alias path still works
    assert client.get("/aws-tasks").json()["count"] == 1


def test_permission_sets_put_then_get(client: TestClient) -> None:
    perms = [{"id": "ps1", "name": "S3", "aws_service": "s3", "actions": ["s3:ListBucket"]}]
    assert client.put("/api/permission-sets", json={"permissions": perms}).json()["count"] == 1
    body = client.get("/api/permission-sets").json()
    assert body["permissions"][0]["actions"] == ["s3:ListBucket"]


def test_digital_worker_settings_persist(client: TestClient) -> None:
    assert client.get("/settings/digital-worker").json()["max_iterations"] is not None
    r = client.post("/settings/digital-worker", json={"max_iterations": 7, "command_timeout": 42})
    assert r.status_code == 200
    got = client.get("/settings/digital-worker").json()
    assert got["max_iterations"] == 7 and got["command_timeout"] == 42
