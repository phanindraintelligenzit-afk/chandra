"""ConfigRepository — Postgres system of record for DFTE configuration (PRD §26.8).

Runs against in-memory SQLite via the injectable session factory.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from src.chandra.catalog import ConfigRepository, normalize_custom_kras
from src.chandra.db.models import Base

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def scope() -> Iterator[object]:
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


@pytest.fixture
def repo(scope: object) -> ConfigRepository:
    return ConfigRepository(tenant_id="t1", session_factory=scope)  # type: ignore[arg-type]


def _load_seed(name: str) -> object:
    path = REPO_ROOT / "src" / "chandra" / "catalog" / "seeds" / name
    if not path.exists():  # during the migration window the file is still at root
        path = REPO_ROOT / name
    return json.loads(path.read_text(encoding="utf-8"))


class TestAwsTasks:
    def test_replace_and_list_round_trip(self, repo: ConfigRepository) -> None:
        tasks = [
            {
                "id": "t-1",
                "name": "Create S3 bucket",
                "description": "d",
                "category": "S3",
                "ownership": "platform",
                "version": 2,
                "is_preset": True,
                "is_predefined": False,
                "somethingExtra": {"k": 1},
            }
        ]
        assert repo.replace_aws_tasks(tasks) == 1
        out = repo.list_aws_tasks()
        assert out[0]["id"] == "t-1"
        assert out[0]["version"] == 2
        assert out[0]["is_preset"] is True
        assert out[0]["somethingExtra"] == {"k": 1}  # unknown fields survive

    def test_replace_is_full_overwrite(self, repo: ConfigRepository) -> None:
        repo.replace_aws_tasks([{"id": "a", "name": "A"}, {"id": "b", "name": "B"}])
        repo.replace_aws_tasks([{"id": "c", "name": "C"}])
        assert [t["id"] for t in repo.list_aws_tasks()] == ["c"]

    def test_rows_without_id_or_name_are_dropped(self, repo: ConfigRepository) -> None:
        assert repo.replace_aws_tasks([{"name": "x"}, {"id": "y"}, {"id": "z", "name": "Z"}]) == 1


class TestPermissionSets:
    def test_round_trip_preserves_wire_shape(self, repo: ConfigRepository) -> None:
        perms = [
            {
                "id": "ps-1",
                "name": "S3 admin",
                "description": "",
                "aws_service": "s3",
                "actions": ["s3:CreateBucket", "s3:PutBucketPolicy"],
                "resource_arn": "arn:aws:s3:::*",
                "is_predefined": True,
                "created_by": "system",
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-02T00:00:00+00:00",
            }
        ]
        repo.replace_permission_sets(perms)
        out = repo.list_permission_sets()[0]
        assert out["actions"] == ["s3:CreateBucket", "s3:PutBucketPolicy"]
        assert out["created_at"].startswith("2026-01-01")
        assert repo.get_permission_set("ps-1")["name"] == "S3 admin"  # type: ignore[index]
        assert repo.get_permission_set("nope") is None


class TestCustomKras:
    def test_normalize_accepts_strings_and_dedupes(self) -> None:
        out = normalize_custom_kras(["Cost", {"name": "cost", "desc": "dup"}, {"code": "Sec"}, ""])
        assert [k["name"] for k in out] == ["Cost", "Sec"]
        assert out[0]["description"] == "Cost"
        assert out[1]["selected"] is True

    def test_round_trip_keeps_order(self, repo: ConfigRepository) -> None:
        repo.replace_custom_kras([{"name": "Zeta"}, {"name": "Alpha", "selected": False}])
        assert [k["name"] for k in repo.list_custom_kras()] == ["Zeta", "Alpha"]
        assert repo.list_custom_kras()[1]["selected"] is False


class TestTenantIsolation:
    def test_tenants_do_not_see_each_other(self, scope: object) -> None:
        a = ConfigRepository(tenant_id="a", session_factory=scope)  # type: ignore[arg-type]
        b = ConfigRepository(tenant_id="b", session_factory=scope)  # type: ignore[arg-type]
        a.replace_aws_tasks([{"id": "same-id", "name": "A's"}])
        b.replace_aws_tasks([{"id": "same-id", "name": "B's"}])
        assert a.list_aws_tasks()[0]["name"] == "A's"
        assert b.list_aws_tasks()[0]["name"] == "B's"
        b.replace_aws_tasks([])
        assert a.list_aws_tasks()  # a untouched by b's overwrite


class TestAgentRunMemory:
    def test_record_and_recent(self, repo: ConfigRepository) -> None:
        repo.record_agent_run(
            action_name="Create S3 bucket",
            final_status="success",
            iterations_used=2,
            errors=["x"],
            fixes=["y"],
            lesson="use versioning",
            correlation_id="corr-1",
        )
        runs = repo.recent_agent_runs(action_name="Create S3 bucket")
        assert runs[0]["lesson"] == "use versioning"
        assert runs[0]["correlation_id"] == "corr-1"
        assert repo.recent_agent_runs(action_name="other") == []


class TestLegacyImport:
    def test_imports_real_seed_files_and_is_idempotent(self, repo: ConfigRepository) -> None:
        first = repo.import_legacy_json(
            aws_tasks=_load_seed("aws_tasks.json"),  # type: ignore[arg-type]
            permission_sets=_load_seed("aws_permissions.json"),  # type: ignore[arg-type]
            custom_kras=_load_seed("customKras.json"),  # type: ignore[arg-type]
            agent_memory=_load_seed("agent_memory.json"),  # type: ignore[arg-type]
        )
        assert first["aws_tasks"] == 4
        assert first["permission_sets"] == 6
        assert first["custom_kras"] == 4
        assert first["agent_run_memory"] == 32
        second = repo.import_legacy_json(aws_tasks=[{"id": "x", "name": "X"}])
        assert second == {}  # table already populated -> skipped
        assert len(repo.list_aws_tasks()) == 4
