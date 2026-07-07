"""Structured logging setup. Use ``get_logger(__name__)`` everywhere instead of stdlib logging."""

from __future__ import annotations

import json
import logging
import sys

import structlog
from src.chandra.config import settings

_configured = False


def configure_logging(log_level: str | None = None) -> None:
    """Idempotent global structlog configuration.

    Called automatically the first time ``get_logger`` is invoked.
    """
    global _configured
    if _configured:
        return

    level_str = log_level or settings.log_level
    level = getattr(logging, level_str, logging.INFO)
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=level,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a configured structlog bound logger."""
    configure_logging()
    return structlog.get_logger(name)  # type: ignore[no-any-return]


def audit_log(agent, action, output):

    log = {
        "timestamp": datetime.utcnow().isoformat(),
        "agent_id": agent,
        "action": action,
        "output": output,
    }

    logger.info(json.dumps(log))
