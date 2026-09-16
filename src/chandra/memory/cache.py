"""Redis cache and live log stream (PRD §26.6 step 5, §26.0.2 step 9).

Redis is **optional by construction**. With ``REDIS_URL`` unset every call here
is a no-op that returns "miss", and Chandra behaves exactly as it did before —
because a cache that can take the system down when it is unavailable is worse
than no cache. The same reasoning as the memory tiers: an accelerator must never
become a dependency.

What it is used for:

* caching the result of expensive read-only lookups, keyed per tenant;
* fan-out of execution log lines to live viewers, where the alternative is
  either polling or coupling the workflow to a WebSocket connection.

What it is deliberately *not* used for: anything authoritative. No policy rule,
role assignment, approval or permission decision is read from Redis. Postgres is
the system of record (§26.8), and a cache that could answer an authorisation
question would be a way to answer it with stale data.
"""

from __future__ import annotations

import json
from typing import Any

from src.chandra.config import settings
from src.chandra.logging import get_logger

logger = get_logger(__name__)

LOG_STREAM_MAX_ENTRIES = 1000


class CacheClient:
    """Thin wrapper with a hard rule: never raise into the caller.

    Every operation degrades to a miss or a silent no-op. Callers therefore need
    no try/except and cannot accidentally make Redis load-bearing.
    """

    def __init__(self, url: str | None = None, namespace: str = "chandra") -> None:
        self.url = url if url is not None else settings.redis_url
        self.namespace = namespace
        self._client: Any = None
        self._unavailable = False

    @property
    def enabled(self) -> bool:
        return bool(self.url) and not self._unavailable

    def _connect(self) -> Any:
        if self._client is not None or not self.enabled:
            return self._client
        try:
            import redis

            self._client = redis.Redis.from_url(self.url, decode_responses=True)
            self._client.ping()
            logger.info("redis.connected")
        except Exception as exc:
            logger.warning("redis.unavailable_running_without_cache", error=str(exc))
            self._client = None
            self._unavailable = True
        return self._client

    def _key(self, tenant_id: str, *parts: str) -> str:
        return ":".join((self.namespace, tenant_id, *parts))

    # -- cache ------------------------------------------------------------

    def get_json(self, tenant_id: str, key: str) -> Any | None:
        client = self._connect()
        if client is None:
            return None
        try:
            raw = client.get(self._key(tenant_id, "cache", key))
            return json.loads(raw) if raw else None
        except Exception as exc:
            logger.warning("redis.get_failed", key=key, error=str(exc))
            return None

    def set_json(self, tenant_id: str, key: str, value: Any, ttl_seconds: int = 300) -> bool:
        client = self._connect()
        if client is None:
            return False
        try:
            client.setex(self._key(tenant_id, "cache", key), ttl_seconds, json.dumps(value))
            return True
        except Exception as exc:
            logger.warning("redis.set_failed", key=key, error=str(exc))
            return False

    def invalidate(self, tenant_id: str, key: str) -> bool:
        client = self._connect()
        if client is None:
            return False
        try:
            client.delete(self._key(tenant_id, "cache", key))
            return True
        except Exception as exc:
            logger.warning("redis.invalidate_failed", key=key, error=str(exc))
            return False

    # -- live log stream --------------------------------------------------

    def append_log(self, tenant_id: str, job_id: str, line: str) -> bool:
        """Append one execution log line and publish it to live subscribers.

        The list is capped so a runaway job cannot consume unbounded memory; the
        full log always remains in the execution record in Postgres, so trimming
        here loses nothing durable.
        """
        client = self._connect()
        if client is None:
            return False
        key = self._key(tenant_id, "logs", job_id)
        try:
            pipe = client.pipeline()
            pipe.rpush(key, line)
            pipe.ltrim(key, -LOG_STREAM_MAX_ENTRIES, -1)
            pipe.expire(key, 86400)
            pipe.publish(self._key(tenant_id, "logstream", job_id), line)
            pipe.execute()
            return True
        except Exception as exc:
            logger.warning("redis.log_append_failed", job_id=job_id, error=str(exc))
            return False

    def read_logs(self, tenant_id: str, job_id: str, limit: int = 200) -> list[str]:
        client = self._connect()
        if client is None:
            return []
        try:
            entries = client.lrange(self._key(tenant_id, "logs", job_id), -limit, -1)
            return [str(e) for e in entries]
        except Exception as exc:
            logger.warning("redis.log_read_failed", job_id=job_id, error=str(exc))
            return []

    def health(self) -> str:
        if not self.url:
            return "disabled"
        client = self._connect()
        return "ok" if client is not None else "unavailable"


_cache: CacheClient | None = None


def get_cache() -> CacheClient:
    global _cache  # noqa: PLW0603 - one client per process, matching the DB engine
    if _cache is None:
        _cache = CacheClient()
    return _cache
