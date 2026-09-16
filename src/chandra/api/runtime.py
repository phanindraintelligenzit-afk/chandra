"""Shared API runtime: job store, worker pool, background loop, live fan-out.

Extracted from ``fastapi_app`` so routers can reach this state without importing
the module they are included in. This is the piece that made the remaining
endpoints hard to split — they all touch the job store, its lock, the thread
pool or the event loop — so moving it here is the enabling step rather than a
tidy-up.

Process-global by necessity, not by preference: the job store is in-memory, so
every replica sees only its own jobs. That is the existing design and is not
changed here; a shared store belongs with the Redis work, and the interface
below would not change.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from src.chandra.api.websockets import WebSocketManager

logger = logging.getLogger("fastapi_app")


class JobStoreDict(dict[str, Any]):
    """A job record that logs its own status and message transitions."""

    def __init__(self, job_id: str, *args: Any, **kwargs: Any) -> None:
        self.job_id = job_id
        super().__init__(*args, **kwargs)
        if "message" in self:
            logger.info(
                f"Job {self.job_id} | Status: {self.get('status', 'unknown')} | "
                f"Progress: {self.get('progress', 0)}% | Message: {self['message']}"
            )

    def __setitem__(self, key: str, value: Any) -> None:
        super().__setitem__(key, value)
        if key == "message":
            logger.info(
                f"Job {self.job_id} | Status: {self.get('status', 'unknown')} | "
                f"Progress: {self.get('progress', 0)}% | Message: {value}"
            )
        elif key == "status":
            logger.info(f"Job {self.job_id} | Status changed to: {value}")


class JobStoreManager(dict[str, Any]):
    def __setitem__(self, key: str, value: Any) -> None:
        if isinstance(value, dict) and not isinstance(value, JobStoreDict):
            value = JobStoreDict(key, value)
        super().__setitem__(key, value)


job_store: dict[str, dict[str, Any]] = JobStoreManager()

# RLock (reentrant) so a background worker that already holds the lock can
# re-enter it without deadlocking the HTTP threads serving GET /requests and
# GET /jobs/status while that job runs.
job_store_lock = threading.RLock()

thread_pool = ThreadPoolExecutor(max_workers=8)

# Per-thread request scratch space (job id, principal).
thread_local = threading.local()


def submit_with_context(fn: Any, *args: Any, **kwargs: Any) -> Future[Any]:
    """Submit to the pool, carrying contextvars into the worker thread.

    Correlation id, tenant id and the structlog context all live in contextvars,
    so a bare ``submit`` would start every background job untraceable.
    """
    ctx = contextvars.copy_context()
    return thread_pool.submit(ctx.run, fn, *args, **kwargs)


# ── Shared background event loop ─────────────────────────────────────────────
# asyncio.run() in several background threads creates competing event loops and
# crashes uvicorn. One persistent loop on a daemon thread, with all async work
# submitted via run_coroutine_threadsafe, avoids that.
bg_loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()


def _start_bg_loop(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)
    loop.run_forever()


bg_loop_thread = threading.Thread(
    target=_start_bg_loop, args=(bg_loop,), daemon=True, name="bg-async-loop"
)
bg_loop_thread.start()

# Live job state fan-out, bound to the background loop so worker threads can
# publish without touching asyncio themselves.
ws_manager = WebSocketManager()
ws_manager.bind_loop(bg_loop)


def publish_job_state(job_id: str) -> None:
    """Announce a job's current state to WebSocket subscribers.

    Best-effort: the job store remains the source of truth and
    ``/jobs/status/{job_id}`` still answers, so a dropped broadcast costs a
    client latency and nothing else.
    """
    with job_store_lock:
        job = dict(job_store.get(job_id, {}))
    if job:
        job.pop("result", None)  # the live feed stays small; full result via HTTP
        ws_manager.publish_threadsafe(job_id, job)
