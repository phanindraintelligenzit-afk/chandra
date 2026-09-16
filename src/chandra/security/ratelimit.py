"""Edge rate limiting (PRD L2 stage 2: "rate limiting" in request validation).

A fixed-capacity token bucket per key, refilled continuously. Chosen over a
fixed window because a fixed window lets a caller spend a full window's budget
in the last instant of one window and again in the first instant of the next —
double the intended rate at the boundary, which is exactly when a retry storm
arrives.

In-process and therefore per-replica: with N replicas behind a load balancer the
effective limit is N x the configured rate. That is a deliberate stopgap, not an
oversight — a correct distributed limiter needs the shared Redis introduced in
Phase 4, and a per-replica limit still sheds the load it is there to shed. The
interface does not change when the backend does.

No performance or capacity claims are made here; the limit is whatever the
operator configures.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class _Bucket:
    tokens: float
    updated_at: float


@dataclass
class RateLimiter:
    """``requests_per_minute`` of 0 (the default) disables limiting entirely."""

    requests_per_minute: int
    burst: int | None = None
    _buckets: dict[str, _Bucket] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def enabled(self) -> bool:
        return self.requests_per_minute > 0

    @property
    def capacity(self) -> float:
        return float(self.burst if self.burst is not None else self.requests_per_minute)

    def check(self, key: str, now: float | None = None) -> tuple[bool, int]:
        """Consume one token for ``key``. Returns ``(allowed, retry_after_seconds)``."""
        if not self.enabled:
            return True, 0

        now = time.monotonic() if now is None else now
        refill_per_second = self.requests_per_minute / 60.0

        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                self._buckets[key] = _Bucket(tokens=self.capacity - 1.0, updated_at=now)
                return True, 0

            elapsed = max(0.0, now - bucket.updated_at)
            bucket.tokens = min(self.capacity, bucket.tokens + elapsed * refill_per_second)
            bucket.updated_at = now

            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return True, 0

            deficit = 1.0 - bucket.tokens
            return False, max(1, int(deficit / refill_per_second) + 1)

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._buckets.clear()
            else:
                self._buckets.pop(key, None)
