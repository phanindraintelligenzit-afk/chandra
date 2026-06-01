import sys
import asyncio
import functools
import inspect
import logging
import threading
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
    """

    def decorator(func: Callable) -> Callable:
        node_name = name or func.__name__

        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                try:
                    logger.info("node.start", extra={"node": node_name})
                    if timeout_s:
                        result = await asyncio.wait_for(
                            func(*args, **kwargs), timeout=timeout_s
                        )
                    else:
                        result = await func(*args, **kwargs)
                    logger.info("node.completed", extra={"node": node_name})
                    _emit_metric(
                        "NodeLatency", 1.0, "Count", {"node": node_name}
                    )
                    return result
                except asyncio.TimeoutError:
                    logger.error("node.timeout", extra={"node": node_name, "timeout_s": timeout_s})
                    raise
                except Exception as exc:
                    logger.error("node.error", extra={"node": node_name, "error": str(exc)})
                    _emit_metric(
                        "NodeErrors", 1.0, "Count", {"node": node_name}
                    )
                    raise

            return async_wrapper

        else:

            @functools.wraps(func)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                effective_timeout = None if sys.platform == "win32" else timeout_s
                try:
                    logger.info("node.start", extra={"node": node_name})
                    result = func(*args, **kwargs)
                    logger.info("node.completed", extra={"node": node_name})
                    _emit_metric(
                        "NodeLatency", 1.0, "Count", {"node": node_name}
                    )
                    return result
                except TimeoutError:
                    logger.error("node.timeout", extra={"node": node_name, "timeout_s": effective_timeout})
                    raise
                except Exception as exc:
                    logger.error("node.error", extra={"node": node_name, "error": str(exc)})
                    _emit_metric(
                        "NodeErrors", 1.0, "Count", {"node": node_name}
                    )
                    raise

            return sync_wrapper

    if fn is None:
        return decorator
    else:
        return decorator(fn)


@contextmanager
def task_context(run_id: str, account_id: str):
    """Bind run_id and account_id to structlog context."""
    structlog.contextvars.bind_contextvars(run_id=run_id, account_id=account_id)
    try:
        yield
    finally:
        structlog.contextvars.unbind_contextvars("run_id", "account_id")


def configure_observability(
    otel_endpoint: str | None = None,
    log_level: str = "INFO",
) -> None:
    """Configure observability (OTEL tracing + structlog)."""
    if otel_endpoint is None:
        logger.info("observability.configured: noop")
        return
    logger.info(f"observability.configured: endpoint={otel_endpoint}, level={log_level}")


def _emit_metric(
    metric_name: str,
    value: float,
    unit: str = "None",
    dimensions: dict | None = None,
    **tags: Any,
) -> None:
    """Emit a metric to CloudWatch in a background thread."""
    logger.info(f"metric.emit: {metric_name}={value}", extra=tags)

    def _put() -> None:
        try:
            from chandra.aws.client_factory import get_default_factory
            cw = get_default_factory().client("cloudwatch")
            dim_list = (
                [{"Name": k, "Value": v} for k, v in dimensions.items()]
                if dimensions
                else []
            )
            cw.put_metric_data(
                Namespace="Chandra",
                MetricData=[
                    {
                        "MetricName": metric_name,
                        "Dimensions": dim_list,
                        "Value": value,
                        "Unit": unit,
                    }
                ],
            )
        except Exception as exc:
            logger.warning(f"metric.emit.failed: {metric_name} {exc}")

    threading.Thread(target=_put, daemon=True).start()
