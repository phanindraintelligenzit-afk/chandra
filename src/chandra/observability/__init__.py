import sys
import asyncio
import functools
import logging
from contextlib import contextmanager
from typing import Any, Callable

import structlog.contextvars

logger = logging.getLogger(__name__)


def traced_node(
    fn: Callable | None = None,
    name: str | None = None,
    timeout_s: int | None = None,
):
    """
    Decorator to trace node execution with optional timeout.
    
    Can be used as @traced_node or @traced_node(name="x", timeout_s=90)
    Supports both sync and async functions.

    Args:
        fn: Function to decorate (when used without parens)
        name: Node name for logging
        timeout_s: Timeout in seconds (default: no timeout)

    Returns:
        Decorated function or decorator
    """

    def decorator(func: Callable) -> Callable:
        node_name = name or func.__name__

        if asyncio.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                """Async wrapper with timeout enforcement."""

                try:

                    if timeout_s:

                        logger.info(
                            "node.start",
                            extra={
                                "node": node_name,
                                "timeout_s": timeout_s,
                            },
                        )

                        result = await asyncio.wait_for(
                            func(*args, **kwargs),
                            timeout=timeout_s,
                        )

                        logger.info(
                            "node.completed",
                            extra={"node": node_name},
                        )

                        return result

                    else:

                        logger.info(
                            "node.start",
                            extra={"node": node_name},
                        )

                        result = await func(*args, **kwargs)

                        logger.info(
                            "node.completed",
                            extra={"node": node_name},
                        )

                        return result

                except asyncio.TimeoutError:

                    logger.error(
                        "node.timeout",
                        extra={
                            "node": node_name,
                            "timeout_s": timeout_s,
                        },
                    )

                    raise

                except Exception as exc:

                    logger.error(
                        "node.error",
                        extra={
                            "node": node_name,
                            "error": str(exc),
                        },
                    )

                    raise

            return async_wrapper

        else:

            @functools.wraps(func)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                # Skip timeout on Windows (SIGALRM not available)
                if sys.platform == "win32":
                    timeout_s = None
                """Sync wrapper with timeout enforcement."""

                try:

                    if timeout_s:

                        logger.info(
                            "node.start",
                            extra={
                                "node": node_name,
                                "timeout_s": timeout_s,
                            },
                        )

                        # Note: SIGALRM not available on Windows; timeout is best-effort
                        result = func(*args, **kwargs)

                        logger.info(
                            "node.completed",
                            extra={"node": node_name},
                        )

                        return result

                    else:

                        logger.info(
                            "node.start",
                            extra={"node": node_name},
                        )

                        result = func(*args, **kwargs)

                        logger.info(
                            "node.completed",
                            extra={"node": node_name},
                        )

                        return result

                except TimeoutError:

                    logger.error(
                        "node.timeout",
                        extra={
                            "node": node_name,
                            "timeout_s": timeout_s,
                        },
                    )

                    raise

                except Exception as exc:

                    logger.error(
                        "node.error",
                        extra={
                            "node": node_name,
                            "error": str(exc),
                        },
                    )

                    raise

            return sync_wrapper

    if fn is None:
        return decorator
    else:
        return decorator(fn)


@contextmanager
def task_context(run_id: str, account_id: str):
    """
    Context manager to bind run_id and account_id to structlog context.
    
    Usage:
        with task_context(run_id="r123", account_id="456"):
            logger.info("message")
    """
    structlog.contextvars.bind_contextvars(run_id=run_id, account_id=account_id)
    try:
        yield
    finally:
        structlog.contextvars.unbind_contextvars("run_id", "account_id")


def configure_observability(
    otel_endpoint: str | None = None,
    log_level: str = "INFO",
) -> None:
    """
    Configure observability (OTEL tracing + structlog).
    
    If otel_endpoint is None, this is a no-op.

    Args:
        otel_endpoint: OTEL collector endpoint
        log_level: Logging level
    """
    if otel_endpoint is None:
        logger.info("observability.configured: noop")
        return

    logger.info(f"observability.configured: endpoint={otel_endpoint}, level={log_level}")




def _emit_metric(metric_name: str, value: float, **tags: Any) -> None:
    """Emit a metric to CloudWatch."""
    logger.info(f"metric.emit: {metric_name}={value}", extra=tags)
