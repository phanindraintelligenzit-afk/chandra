"""Correlation IDs travel API -> LangGraph state -> audit -> persist (PRD 26.11)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from src.chandra.db.models import Base, CloudRequestRecord
from src.chandra.digital_worker import graph as dw_graph
from src.chandra.digital_worker import memory, planner
from src.chandra.digital_worker.graph import build_digital_worker_graph
from src.chandra.observability import correlation


@pytest.fixture(autouse=True)
def _isolated_context() -> Iterator[None]:
    """Context is process-wide; other tests in the session may leave ids bound."""
    correlation.clear()
    yield
    correlation.clear()


class TestContext:
    def test_bind_scope_and_clear(self) -> None:
        assert correlation.get_correlation_id() is None
        with correlation.correlation_scope("abc", "acme") as cid:
            assert cid == "abc"
            assert correlation.get_tenant_id() == "acme"
        assert correlation.get_correlation_id() is None
        assert correlation.get_tenant_id() == correlation.DEFAULT_TENANT

    def test_sanitize_rejects_unsafe_header_values(self) -> None:
        assert correlation.sanitize("ok-1.2:3", fallback="f") == "ok-1.2:3"
        assert correlation.sanitize("bad value\n", fallback="f") == "f"
        assert correlation.sanitize("x" * 65, fallback="f") == "f"
        assert correlation.sanitize(None, fallback="f") == "f"


class TestApiMiddleware:
    @pytest.fixture
    def client(self) -> TestClient:
        import fastapi_app

        return TestClient(fastapi_app.app)

    def test_inbound_header_is_echoed(self, client: TestClient) -> None:
        r = client.get("/health/live", headers={"X-Correlation-ID": "trace-123"})
        assert r.headers["X-Correlation-ID"] == "trace-123"

    def test_missing_header_gets_generated_id(self, client: TestClient) -> None:
        r = client.get("/health/live")
        assert len(r.headers["X-Correlation-ID"]) == 32

    def test_unsafe_header_is_replaced(self, client: TestClient) -> None:
        r = client.get("/health/live", headers={"X-Correlation-ID": "bad value"})
        assert r.headers["X-Correlation-ID"] != "bad value"


class TestGraphPropagation:
    @pytest.fixture
    def sqlite_scope(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
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

        monkeypatch.setattr(dw_graph, "session_scope", _scope)
        monkeypatch.setattr(memory, "lookup_plan", lambda fingerprint: None)
        monkeypatch.setattr(planner, "compose_request_analysis", lambda payload: None)
        for var in ("SLACK_WEBHOOK_URL", "TEAMS_WEBHOOK_URL", "SMTP_HOST", "JIRA_SERVER"):
            monkeypatch.delenv(var, raising=False)
        yield _scope

    def test_ids_flow_from_state_to_audit_and_db(self, sqlite_scope: Any) -> None:
        graph = build_digital_worker_graph(checkpointer=MemorySaver())
        cfg = {"configurable": {"thread_id": "corr-thread"}}
        final = graph.invoke(
            {
                "source": "rest_api",
                "payload": {
                    "title": "Rotate an expired IAM access key for a service account",
                    "description": "Key AKIA... for svc-report is 120 days old. Rotate it.",
                    "priority": "P3",
                    "platform": "aws",
                },
                "dry_run": True,
                "job_id": "job-1",
                "correlation_id": "corr-xyz",
                "tenant_id": "acme",
            },
            config=cfg,
        )
        assert final["correlation_id"] == "corr-xyz"
        assert final["tenant_id"] == "acme"
        trail = final["audit_trail"]
        assert trail and all(e.data.get("correlation_id") == "corr-xyz" for e in trail)
        if final.get("status") not in {"awaiting_approval", "in_progress"}:
            with sqlite_scope() as session:
                row = session.scalars(select(CloudRequestRecord)).one()
                assert row.correlation_id == "corr-xyz"
                assert row.tenant_id == "acme"

    def test_defaults_when_api_did_not_set_ids(self, sqlite_scope: Any) -> None:
        graph = build_digital_worker_graph(checkpointer=MemorySaver())
        final = graph.invoke(
            {
                "source": "rest_api",
                "payload": {
                    "title": "List S3 buckets",
                    "description": "read only",
                    "platform": "aws",
                },
                "dry_run": True,
                "job_id": "job-77",
            },
            config={"configurable": {"thread_id": "corr-thread-2"}},
            interrupt_before=[],
        )
        assert final["correlation_id"] == "job-77"  # falls back to the job id
        assert final["tenant_id"] == correlation.DEFAULT_TENANT
