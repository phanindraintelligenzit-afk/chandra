import asyncio
import functools
import inspect
import json
import logging
import signal
import sys
import threading
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

import structlog.contextvars

logger = logging.getLogger(__name__)


def _pretty_format(obj: Any) -> str:
    def _default(o: Any) -> Any:
        if hasattr(o, "model_dump"):
            return o.model_dump()
        return str(o)

    try:
        return json.dumps(obj, indent=2, default=_default)
    except Exception:
        import pprint

        return pprint.pformat(obj)


def traced_node(  # noqa: PLR0915  # sync+async wrappers inline
    fn: Callable[..., Any] | None = None,
    name: str | None = None,
    timeout_s: float | None = None,
) -> Callable[..., Any]:
    """
    Decorator to trace node execution with optional timeout.

    Can be used as @traced_node or @traced_node(name="x", timeout_s=90)
    Supports both sync and async functions.
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:  # noqa: PLR0915
        node_name = name or func.__name__

        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                try:
                    logger.info("node.start", extra={"node": node_name})
                    state_in = args[0] if args else kwargs.get("state", {})
                    logger.info(
                        f"Input received for node ({node_name}):\n{_pretty_format(state_in)}"
                    )

                    if timeout_s:
                        result = await asyncio.wait_for(func(*args, **kwargs), timeout=timeout_s)
                    else:
                        result = await func(*args, **kwargs)

                    logger.info(f"Output for node ({node_name}):\n{_pretty_format(result)}")
                    logger.info("node.completed", extra={"node": node_name})
                    _emit_metric("NodeLatency", 1.0, "Count", {"node": node_name})
                    return result
                except TimeoutError:
                    logger.error("node.timeout", extra={"node": node_name, "timeout_s": timeout_s})
                    raise
                except Exception as exc:
                    logger.error("node.error", extra={"node": node_name, "error": str(exc)})
                    _emit_metric("NodeErrors", 1.0, "Count", {"node": node_name})
                    raise

            return async_wrapper

        else:

            @functools.wraps(func)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                # SIGALRM-based enforcement: POSIX + main thread only.
                # Elsewhere (Windows, worker threads) run without a timeout.
                effective_timeout = None if sys.platform == "win32" else timeout_s
                alarm_armed = False
                previous_handler: Any = None
                if effective_timeout and threading.current_thread() is threading.main_thread():

                    def _on_alarm(signum: int, frame: Any) -> None:
                        raise TimeoutError(
                            f"node {node_name} exceeded timeout of {effective_timeout}s"
                        )

                    previous_handler = signal.signal(signal.SIGALRM, _on_alarm)  # type: ignore[attr-defined]
                    signal.setitimer(signal.ITIMER_REAL, float(effective_timeout))  # type: ignore[attr-defined]
                    alarm_armed = True
                try:
                    logger.info("node.start", extra={"node": node_name})
                    state_in = args[0] if args else kwargs.get("state", {})
                    logger.info(
                        f"Input received for node ({node_name}):\n{_pretty_format(state_in)}"
                    )

                    result = func(*args, **kwargs)

                    logger.info(f"Output for node ({node_name}):\n{_pretty_format(result)}")
                    logger.info("node.completed", extra={"node": node_name})
                    _emit_metric("NodeLatency", 1.0, "Count", {"node": node_name})
                    return result
                except TimeoutError:
                    logger.error(
                        "node.timeout", extra={"node": node_name, "timeout_s": effective_timeout}
                    )
                    raise
                except Exception as exc:
                    logger.error("node.error", extra={"node": node_name, "error": str(exc)})
                    _emit_metric("NodeErrors", 1.0, "Count", {"node": node_name})
                    raise
                finally:
                    if alarm_armed:
                        signal.setitimer(signal.ITIMER_REAL, 0)  # type: ignore[attr-defined]
                        signal.signal(signal.SIGALRM, previous_handler)  # type: ignore[attr-defined]

            return sync_wrapper

    if fn is None:
        return decorator
    else:
        return decorator(fn)


@contextmanager
def task_context(run_id: str, account_id: str) -> Any:
    """Bind run_id and account_id to structlog context."""
    structlog.contextvars.bind_contextvars(run_id=run_id, account_id=account_id)
    try:
        yield
    finally:
        structlog.contextvars.unbind_contextvars("run_id", "account_id")


def configure_observability(
    otel_endpoint: str | None = None,
    log_level: str = "INFO",
    environment: str = "production",
) -> None:
    """Configure observability (OTEL tracing + structlog)."""
    if otel_endpoint is None:
        logger.info("observability.configured: noop (environment=%s)", environment)
        return
    logger.info(
        "observability.configured: endpoint=%s, level=%s, environment=%s",
        otel_endpoint,
        log_level,
        environment,
    )


def _emit_metric(
    metric_name: str,
    value: float,
    unit: str = "None",
    dimensions: dict[str, str] | None = None,
    **tags: Any,
) -> None:
    """Emit a metric to CloudWatch in a background thread."""
    logger.info(f"metric.emit: {metric_name}={value}", extra=tags)

    def _put() -> None:
        try:
            from src.chandra.aws.client_factory import (  # lazy: avoid cycle
                get_default_factory,
            )

            cw = get_default_factory().client("cloudwatch")
            dim_list = (
                [{"Name": k, "Value": v} for k, v in dimensions.items()] if dimensions else []
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
            if "InvalidClientTokenId" in str(exc):
                logger.debug(f"metric.emit.failed (auth): {metric_name} {exc}")
            else:
                logger.warning(f"metric.emit.failed: {metric_name} {exc}")

    threading.Thread(target=_put, daemon=True).start()
