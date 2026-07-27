"""Shared LangGraph checkpointer factory.

Both the core Chandra observation graph and the Digital Worker request
workflow need durable, restart-surviving human-in-the-loop state. Rather
than duplicate the Postgres-with-in-memory-fallback logic in each graph,
they both call :func:`build_checkpointer` here.

Prefers Postgres (production): a paused approval survives a process
restart and can still be resumed by ``thread_id``. Falls back to an
in-memory saver when the Postgres checkpoint library or a reachable
database is unavailable, so tests and offline runs still work. The
fallback is always logged at WARNING so it never silently regresses.
"""

from __future__ import annotations

from typing import Any, cast

from langgraph.checkpoint.memory import MemorySaver
from src.chandra.config import settings
from src.chandra.logging import get_logger

logger = get_logger(__name__)


def build_checkpointer() -> Any:
    """Return a durable Postgres checkpointer, or an in-memory fallback."""
    import inspect
    from enum import Enum

    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    from pydantic import BaseModel
    from src.chandra.digital_worker import schemas

    # Dynamically allow all Pydantic models and Enums defined in the schemas module
    # This avoids hardcoding the list while still satisfying LangGraph's strict msgpack security.
    allowed_modules = []
    for name, obj in inspect.getmembers(schemas, inspect.isclass):
        if issubclass(obj, BaseModel | Enum) and obj.__module__ == schemas.__name__:
            allowed_modules.append((schemas.__name__, name))

    serde = JsonPlusSerializer(allowed_msgpack_modules=allowed_modules)

    try:
        from langgraph.checkpoint.postgres import (  # lazy: optional dep
            PostgresSaver,
        )
    except ImportError:
        logger.warning("checkpointer.postgres_unavailable_fallback_to_memory")
        return MemorySaver(serde=serde)

    try:
        # Convert SQLAlchemy URL format to psycopg native format
        # postgresql+psycopg://... → postgresql://...
        import psycopg  # lazy: optional dep
        from psycopg.rows import dict_row  # lazy: optional dep
        from psycopg_pool import (  # lazy: optional dep
            ConnectionPool,
        )

        conn_string = settings.postgres_url.replace("postgresql+psycopg://", "postgresql://")

        with psycopg.connect(conn_string, autocommit=True, row_factory=dict_row) as conn:
            PostgresSaver(conn).setup()

        # kwargs row_factory yields dict rows at runtime; ConnectionPool's
        # type parameter cannot express that, hence the cast.
        pool = ConnectionPool(conn_string, max_size=10, kwargs={"row_factory": dict_row})
        checkpointer = PostgresSaver(cast(Any, pool), serde=serde)
        return checkpointer
    except Exception as exc:
        logger.warning(
            "checkpointer.postgres_setup_failed_fallback_to_memory",
            error=str(exc),
        )
        return MemorySaver(serde=serde)
