"""Job store fan-out and registration (src/chandra/api/runtime.py)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from src.chandra.api import runtime
from src.chandra.api.websockets import WebSocketManager


class _FakeSocket:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def accept(self) -> None:
        return None

    async def send_json(self, data: Any) -> None:
        self.sent.append(data)


@pytest.fixture
def clean_store() -> Any:
    runtime.job_store.clear()
    yield runtime.job_store
    runtime.job_store.clear()


class TestRegisterJob:
    def test_creates_the_standard_shape(self, clean_store: Any) -> None:
        job_id = runtime.new_job_id()
        runtime.register_job(job_id, "Queued: something")
        job = runtime.job_store[job_id]
        assert job["status"] == "pending"
        assert job["progress"] == 0
        assert job["message"] == "Queued: something"
        assert job["result"] is None and job["error"] is None
        assert job["started_at"] is None and job["completed_at"] is None

    def test_extra_fields_are_merged(self, clean_store: Any) -> None:
        job_id = runtime.new_job_id()
        runtime.register_job(job_id, "Waiting", sandbox_path="/tmp/x", kind="orchestrate")
        assert runtime.job_store[job_id]["sandbox_path"] == "/tmp/x"
        assert runtime.job_store[job_id]["kind"] == "orchestrate"

    def test_ids_are_unique(self) -> None:
        assert runtime.new_job_id() != runtime.new_job_id()


class TestJobStoreBroadcasts:
    """Every job type gets live updates, not only the Digital Worker jobs that
    used to carry explicit publish calls."""

    def _subscribed(self, job_id: str) -> tuple[WebSocketManager, _FakeSocket]:
        manager = WebSocketManager()
        socket = _FakeSocket()
        asyncio.run(manager.subscribe(job_id, socket))
        return manager, socket

    def test_status_and_message_changes_are_published(
        self, clean_store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        job_id = runtime.new_job_id()
        published: list[tuple[str, dict[str, Any]]] = []
        monkeypatch.setattr(
            runtime.ws_manager,
            "publish_threadsafe",
            lambda jid, msg: published.append((jid, msg)),
        )
        runtime.register_job(job_id, "Queued")
        job = runtime.job_store[job_id]
        job["status"] = "running"
        job["progress"] = 50
        job["message"] = "Halfway"
        job["error"] = None

        assert [m["status"] for _, m in published][-3:] == ["running", "running", "running"]
        assert any(m.get("progress") == 50 for _, m in published)
        assert all(jid == job_id for jid, _ in published)

    def test_result_is_never_broadcast(
        self, clean_store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The live feed stays small; the full result comes from HTTP."""
        job_id = runtime.new_job_id()
        published: list[dict[str, Any]] = []
        monkeypatch.setattr(
            runtime.ws_manager, "publish_threadsafe", lambda jid, msg: published.append(msg)
        )
        runtime.register_job(job_id, "Queued")
        job = runtime.job_store[job_id]
        job["result"] = {"huge": "payload"}
        job["status"] = "completed"
        assert published
        assert all("result" not in msg for msg in published)

    def test_non_broadcast_keys_do_not_publish(
        self, clean_store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        job_id = runtime.new_job_id()
        runtime.register_job(job_id, "Queued")
        published: list[dict[str, Any]] = []
        monkeypatch.setattr(
            runtime.ws_manager, "publish_threadsafe", lambda jid, msg: published.append(msg)
        )
        runtime.job_store[job_id]["thread_id"] = 12345
        assert published == []

    def test_broadcast_does_not_take_the_job_store_lock(self, clean_store: Any) -> None:
        """The snapshot comes from the record itself. Re-reading the store from
        the event-loop thread would make the publisher wait on a lock a worker
        already holds."""
        job_id = runtime.new_job_id()
        runtime.register_job(job_id, "Queued")
        job = runtime.job_store[job_id]
        with runtime.job_store_lock:
            job["status"] = "running"  # must not deadlock or block
        assert runtime.job_store[job_id]["status"] == "running"
