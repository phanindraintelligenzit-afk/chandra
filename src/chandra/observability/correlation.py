"""Correlation IDs (PRD §26.11): one id follows a request through
API → LangGraph state → execution → audit/persist → integrations.

Storage is a ``contextvars`` pair plus ``structlog.contextvars`` binding, so every
structured log line emitted while a request/job is active carries
``correlation_id`` and ``tenant_id`` without touching call sites. The values
also travel explicitly in ``DigitalWorkerState`` and are written to Postgres
(``cloud_requests.correlation_id``, ``agent_run_memory.correlation_id``) so the
trail survives across process boundaries and restarts.

Inbound header: ``X-Correlation-ID`` (honoured if present, else generated).
Tenant header: ``X-Tenant-ID`` (defaults to ``"default"``).
"""

from __future__ import annotations

import contextvars
import re
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import uuid4

import structlog

CORRELATION_HEADER = "X-Correlation-ID"
TENANT_HEADER = "X-Tenant-ID"
DEFAULT_TENANT = "default"

_SAFE = re.compile(r"^[A-Za-z0-9._:\-]{1,64}$")

_correlation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "chandra_correlation_id", default=None
)
_tenant_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "chandra_tenant_id", default=DEFAULT_TENANT
)


def new_correlation_id() -> str:
    return uuid4().hex


def sanitize(value: str | None, *, fallback: str) -> str:
    """Accept only header-safe ids (also safe for log/DB columns)."""
    if value and _SAFE.match(value):
        return value
    return fallback


def get_correlation_id() -> str | None:
    return _correlation_id.get()


def get_tenant_id() -> str:
    return _tenant_id.get()


def bind(correlation_id: str | None, tenant_id: str | None = None) -> None:
    """Bind for the current context (thread / task) and for structlog."""
    cid = correlation_id or new_correlation_id()
    tid = tenant_id or DEFAULT_TENANT
    _correlation_id.set(cid)
    _tenant_id.set(tid)
    structlog.contextvars.bind_contextvars(correlation_id=cid, tenant_id=tid)


def clear() -> None:
    _correlation_id.set(None)
    _tenant_id.set(DEFAULT_TENANT)
    structlog.contextvars.unbind_contextvars("correlation_id", "tenant_id")


@contextmanager
def correlation_scope(correlation_id: str | None, tenant_id: str | None = None) -> Iterator[str]:
    """Bind for the duration of a block (background job threads use this)."""
    prev_cid, prev_tid = _correlation_id.get(), _tenant_id.get()
    bind(correlation_id, tenant_id)
    try:
        yield _correlation_id.get() or ""
    finally:
        if prev_cid is None:
            clear()
        else:
            bind(prev_cid, prev_tid)
