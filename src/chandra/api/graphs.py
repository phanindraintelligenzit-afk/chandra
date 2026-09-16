"""Compiled graph singletons shared by the app and its routers.

Both graphs are built once per process. That is not an optimisation: the Digital
Worker's checkpointer keys paused approvals by ``thread_id == job_id``, so
rebuilding the graph between a submission and its approval would orphan every
in-flight interrupt. The Copilot agent holds conversation state on the same
terms.

Construction failures are recorded rather than raised. A missing Copilot agent
should not stop the Digital Worker from serving requests, and vice versa —
``/health/ready`` reports each independently so a degraded component is visible
without taking down the service.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("fastapi_app")

digital_worker: Any = None
copilot_agent: Any = None


def init_copilot_agent() -> Any:
    """Build the Copilot agent once. Returns ``None`` if it cannot be built."""
    global copilot_agent  # noqa: PLW0603 - one instance per process, by design
    if copilot_agent is not None:
        return copilot_agent
    try:
        from copilot_agents.graph import build_graph

        copilot_agent = build_graph()
        logger.info("Copilot agent initialized successfully")
    except Exception as exc:
        logger.error("Failed to initialize copilot agent: %s", exc)
        copilot_agent = None
    return copilot_agent


def init_digital_worker() -> Any:
    """Build the Digital Worker graph once. Returns ``None`` if it cannot be built."""
    global digital_worker  # noqa: PLW0603 - see module docstring
    if digital_worker is not None:
        return digital_worker
    try:
        from src.chandra.digital_worker.graph import build_digital_worker_graph

        digital_worker = build_digital_worker_graph()
        logger.info("Digital Worker graph initialized successfully")
    except Exception as exc:
        logger.error("Failed to initialize Digital Worker graph: %s", exc)
        digital_worker = None
    return digital_worker


def require_digital_worker() -> Any:
    """Return the graph or raise. Callers that cannot proceed without it should
    use this rather than a ``None`` check, so the failure names itself."""
    if digital_worker is None:
        raise RuntimeError("Digital Worker graph is not initialized")
    return digital_worker


def component_status() -> dict[str, str]:
    """Per-graph readiness, for ``/health/ready``."""
    return {
        "copilot_agent": "ok" if copilot_agent is not None else "unavailable",
        "digital_worker": "ok" if digital_worker is not None else "unavailable",
    }
