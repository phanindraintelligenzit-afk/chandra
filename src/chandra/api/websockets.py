"""WebSocket manager for live job state (PRD L2 stage 2).

Replaces dashboard polling of ``/jobs/status/{job_id}``. A client subscribes to
one job id and receives state transitions as they happen.

Two properties the implementation exists to guarantee:

* **A slow or dead client never blocks a job.** Broadcasting happens under a
  lock only long enough to copy the subscriber list; sends are attempted
  outside it and a failed send unsubscribes that socket rather than propagating.
  The workflow must never be coupled to the health of a browser tab.
* **Publishing is safe from any thread.** Job state changes on the worker thread
  pool, not the event loop. ``publish_threadsafe`` marshals onto the loop, so a
  background worker can announce progress without touching asyncio itself.

The manager holds no job state — it is a fan-out only. ``/jobs/status`` remains
the source of truth, so a client that reconnects reads current state rather than
replaying a stream it missed.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from src.chandra.logging import get_logger

logger = get_logger(__name__)


class WebSocketLike(Protocol):
    """Structural type for the parts of ``fastapi.WebSocket`` used here, so the
    manager is unit-testable without a live socket."""

    async def accept(self) -> None: ...

    async def send_json(self, data: Any) -> None: ...


class WebSocketManager:
    """Fan-out of job state to subscribed clients, keyed by job id."""

    def __init__(self) -> None:
        self._subscribers: dict[str, set[WebSocketLike]] = {}
        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Record the loop that ``publish_threadsafe`` should marshal onto."""
        self._loop = loop

    async def subscribe(self, job_id: str, websocket: WebSocketLike) -> None:
        await websocket.accept()
        async with self._lock:
            self._subscribers.setdefault(job_id, set()).add(websocket)
        logger.info("ws.subscribed", job_id=job_id, subscribers=self.subscriber_count(job_id))

    async def unsubscribe(self, job_id: str, websocket: WebSocketLike) -> None:
        async with self._lock:
            sockets = self._subscribers.get(job_id)
            if sockets:
                sockets.discard(websocket)
                if not sockets:
                    self._subscribers.pop(job_id, None)

    def subscriber_count(self, job_id: str) -> int:
        return len(self._subscribers.get(job_id, ()))

    async def publish(self, job_id: str, message: dict[str, Any]) -> int:
        """Send ``message`` to everyone watching ``job_id``.

        Returns the number of successful deliveries. A socket that raises is
        dropped: a client that has gone away must not keep a job's publisher
        failing forever.
        """
        async with self._lock:
            targets = list(self._subscribers.get(job_id, ()))
        if not targets:
            return 0

        payload = {"job_id": job_id, **message}
        delivered = 0
        dead: list[WebSocketLike] = []
        for socket in targets:
            try:
                await socket.send_json(payload)
                delivered += 1
            except Exception as exc:
                logger.info("ws.dropping_subscriber", job_id=job_id, error=str(exc))
                dead.append(socket)

        if dead:
            async with self._lock:
                sockets = self._subscribers.get(job_id)
                if sockets:
                    for socket in dead:
                        sockets.discard(socket)
                    if not sockets:
                        self._subscribers.pop(job_id, None)
        return delivered

    def publish_threadsafe(self, job_id: str, message: dict[str, Any]) -> None:
        """Publish from a worker thread. Never raises into the caller — a status
        broadcast failing must not fail the job it is reporting on."""
        loop = self._loop
        if loop is None or not self._subscribers.get(job_id):
            return
        try:
            asyncio.run_coroutine_threadsafe(self.publish(job_id, message), loop)
        except Exception as exc:
            logger.warning("ws.publish_failed", job_id=job_id, error=str(exc))

    async def close_all(self) -> None:
        async with self._lock:
            self._subscribers.clear()


async def pump_until_disconnect(
    manager: WebSocketManager,
    job_id: str,
    websocket: WebSocketLike,
    receive: Callable[[], Awaitable[Any]],
) -> None:
    """Hold a subscription open until the client disconnects.

    The receive loop exists only to notice the disconnect; inbound messages are
    ignored. A WebSocket is a status feed here, not a command channel — accepting
    commands over it would route around the authenticated, rate-limited,
    RBAC-checked HTTP edge.
    """
    await manager.subscribe(job_id, websocket)
    try:
        while True:
            await receive()
    except Exception:
        pass
    finally:
        with contextlib.suppress(Exception):
            await manager.unsubscribe(job_id, websocket)
