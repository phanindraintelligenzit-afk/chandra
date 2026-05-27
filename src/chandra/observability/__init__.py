"""Observability: tracing, timeout, metrics, pricing."""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


def traced_node(
    name: str,
    timeout_s: int | None = None,
):
    """
    Decorator to trace node execution with optional timeout.

    Args:
        name: Node name for logging
        timeout_s: Timeout in seconds (default: no timeout)

    Returns:
        Decorator function
    """

    def decorator(fn: Callable) -> Callable:

        @functools.wraps(fn)
        async def async_wrapper(
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            """Async wrapper with timeout enforcement."""

            try:

                if timeout_s:

                    logger.info(
                        "node.start",
                        extra={
                            "node": name,
                            "timeout_s": timeout_s,
                        },
                    )

                    result = await asyncio.wait_for(
                        fn(*args, **kwargs),
                        timeout=timeout_s,
                    )

                    logger.info(
                        "node.completed",
                        extra={"node": name},
                    )

                    return result

                else:

                    logger.info(
                        "node.start",
                        extra={"node": name},
                    )

                    result = await fn(*args, **kwargs)

                    logger.info(
                        "node.completed",
                        extra={"node": name},
                    )

                    return result

            except asyncio.TimeoutError:

                logger.error(
                    "node.timeout",
                    extra={
                        "node": name,
                        "timeout_s": timeout_s,
                    },
                )

                return {
                    "errors": [
                        {
                            "node": name,
                            "type": "timeout",
                            "timeout_s": timeout_s,
                        }
                    ]
                }

            except Exception as exc:

                logger.error(
                    "node.error",
                    extra={
                        "node": name,
                        "error": str(exc),
                    },
                )

                return {
                    "errors": [
                        {
                            "node": name,
                            "type": "error",
                            "message": str(exc),
                        }
                    ]
                }

        return async_wrapper

    return decorator
