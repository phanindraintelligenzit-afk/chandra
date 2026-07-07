"""End-to-end Digital Worker workflow tests (LLM + external I/O mocked).

Exercises the full LangGraph topology: intake → understand → classify →
identify_platform → collect_context → RCA → plan → risk → decision →
execute/guidance → validate → tracker → notify → audit → persist.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, ClassVar

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from src.chandra.aws.client_factory import AwsClientFactory
from src.chandra.db.models import Base, CloudRequestRecord, ResolutionMemoryRecord
from src.chandra.digital_worker import graph as dw_graph
from src.chandra.digital_worker import memory, planner
from src.chandra.digital_worker.graph import build_digital_worker_graph

THREAD = {"configurable": {"thread_id": "test-thread"}}


@pytest.fixture
def sqlite_scope(monkeypatch: pytest.MonkeyPatch) -> Iterator[sessionmaker[Session]]:
    """Route the persist node's session_scope to in-memory SQLite."""
    engine = create_engine("sqlite:///:memory:")
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
    yield factory


@pytest.fixture
def workflow(
    monkeypatch: pytest.MonkeyPatch,
    factory: AwsClientFactory,
    sqlite_scope: sessionmaker[Session],
) -> Any:
    """Compiled graph with LLM, memory, Jira, and webhooks neutralized."""
    monkeypatch.setattr(memory, "lookup_plan", lambda fingerprint: None)
    monkeypatch.setattr(planner, "compose_request_analysis", lambda payload: None)
    # AWS clients (context collection + executor) go through moto.
    monkeypatch.setattr(
        "src.chandra.digital_worker.context.get_default_factory", lambda: factory
    )
    monkeypatch.setattr(
        "src.chandra.graphs.action_nodes.action_executor.get_default_factory", lambda: factory
    )
    for var in (
        "JIRA_SERVER", "JIRA_EMAIL", "JIRA_API_TOKEN",
        "SLACK_WEBHOOK_URL", "TEAMS_WEBHOOK_URL", "SMTP_HOST",
    ):
        monkeypatch.delenv(var, raising=False)
    return build_digital_worker_graph(checkpointer=MemorySaver())


class TestGuidancePath:
    def test_unknown_request_yields_engineer_guidance(self, workflow: Any) -> None:
        final = workflow.invoke(
            {
                "source": "rest_api",
                "payload": {
                    "title": "Migrate the reporting warehouse to a new region",
                    "description": "Multi-quarter effort, needs design",
                },
            },
            config=THREAD,
        )
        assert final["decision"].mode.value == "engineer_guidance"
        assert final["execution"].status == "skipped"
        assert "# Engineer Guidance" in final["guidance_md"]
        assert final["validation"].passed
        assert final["status"] == "completed"
        # Every stage left an audit event; intake is first.
        nodes = [event.node for event in final["audit_trail"]]
        assert nodes[0] == "receive_request"
        assert "persist" not in nodes  # persist returns no audit event, only rows
        # No chat channels configured → everything skipped, nothing failed.
        assert {n.status for n in final["notifications"]} == {"skipped"}

    def test_persist_writes_audit_row(
        self, workflow: Any, sqlite_scope: sessionmaker[Session]
    ) -> None:
        workflow.invoke(
            {"source": "rest_api", "payload": {"title": "Question about tagging policy"}},
            config=THREAD,
        )
        with sqlite_scope() as session:
            rows = session.execute(select(CloudRequestRecord)).scalars().all()
            assert len(rows) == 1
            assert rows[0].source == "rest_api"
            assert rows[0].decision_mode == "engineer_guidance"
            memory_rows = session.execute(select(ResolutionMemoryRecord)).scalars().all()
            assert len(memory_rows) == 1  # plan cached for future reuse


class TestAutoExecutePath:
    def test_low_risk_automation_runs_dry_by_default(self, workflow: Any) -> None:
        final = workflow.invoke(
            {
                "source": "rest_api",
                "payload": {
                    "title": "S3 bucket is public — block public access",
                    "description": "Bucket acme-logs flagged as exposed",
                    "resource_id": "acme-logs",
                    "priority": "P3",
                },
            },
            config=THREAD,
        )
        assert final["plan"].automation_available
        assert final["plan"].detector_id == "SEC-001-public-s3"
        assert final["decision"].mode.value == "auto_execute"
        assert final["execution"].status == "dry_run"
        assert final["execution"].dry_run is True
        assert final["validation"].passed


class TestApprovalPath:
    P1_PAYLOAD: ClassVar[dict[str, Any]] = {
        "source": "jira",
        "payload": {
            "issue": {
                "key": "SEC-99",
                "fields": {
                    "summary": "Public s3 bucket in production — block access now",
                    "description": "bucket exposed",
                    "priority": {"name": "Highest"},
                },
            },
            "resource_id": "acme-prod-logs",
        },
    }

    def test_p1_pauses_at_approval_gate(self, workflow: Any) -> None:
        final = workflow.invoke(dict(self.P1_PAYLOAD), config=THREAD)
        assert final["decision"].mode.value == "await_approval"
        snapshot = workflow.get_state(THREAD)
        assert "approval_gate" in snapshot.next
        # Nothing executed while paused.
        assert "execution" not in final or final.get("execution") is None

    def test_approve_resumes_and_executes(self, workflow: Any) -> None:
        workflow.invoke(dict(self.P1_PAYLOAD), config=THREAD)
        final = workflow.invoke(
            Command(resume={"approved": True, "approver": "phani", "comment": "go"}),
            config=THREAD,
        )
        assert final["approval"].approved
        assert final["approval"].approver == "phani"
        assert final["execution"].status == "dry_run"  # dry_run default preserved
        assert final["validation"].passed

    def test_reject_falls_back_to_guidance(self, workflow: Any) -> None:
        workflow.invoke(dict(self.P1_PAYLOAD), config=THREAD)
        final = workflow.invoke(
            Command(resume={"approved": False, "approver": "phani", "comment": "too risky"}),
            config=THREAD,
        )
        assert not final["approval"].approved
        assert final["execution"].status == "skipped"
        assert "# Engineer Guidance" in final["guidance_md"]


class TestDeterminismInvariant:
    def test_llm_is_confined_to_planning(
        self, workflow: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Once planning is done, no other node may reach for the composer."""
        calls: list[str] = []

        def _tracked(payload: dict[str, Any]) -> None:
            calls.append("compose_request_analysis")

        monkeypatch.setattr(planner, "compose_request_analysis", _tracked)
        workflow.invoke(
            {"source": "rest_api", "payload": {"title": "Restart the staging api"}},
            config=THREAD,
        )
        assert calls == ["compose_request_analysis"]  # exactly one planning call
