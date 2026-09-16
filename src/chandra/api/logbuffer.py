"""In-memory ring buffer of backend log records, served by ``GET /logs``.

Extracted so the logs endpoint can move into a router while the logging handler
that fills it stays with the app. Bounded and process-local: this is an
operator convenience for the dashboard, never the audit trail — that lives in
Postgres (PRD §26.11).
"""

from __future__ import annotations

import threading
from typing import Any

MAX_ENTRIES = 2000


class LogBuffer:
    """Thread-safe bounded buffer. Writes come from a logging handler on
    arbitrary threads, reads from HTTP handlers."""

    def __init__(self, max_entries: int = MAX_ENTRIES) -> None:
        self.max_entries = max_entries
        self._entries: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def append(self, entry: dict[str, Any]) -> None:
        with self._lock:
            self._entries.append(entry)
            if len(self._entries) > self.max_entries:
                del self._entries[: len(self._entries) - self.max_entries]

    def read(self, limit: int = 500, offset: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            total = len(self._entries)
            start = max(0, total - limit - offset)
            end = max(0, total - offset)
            return list(self._entries[start:end])

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


log_buffer = LogBuffer()
