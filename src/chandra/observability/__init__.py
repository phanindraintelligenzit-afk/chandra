"""Observability infrastructure: OTEL tracing + structlog context binding + metrics."""

from __future__ import annotations

import asyncio
import functools
import inspect
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, TypeVar, overload

import structlog.contextvars
from opentelemetry import metrics, trace
from opentelemetry.trace import StatusCode

from chandra.logging import configure_logging, get_logger as _get_logger
from chandra.aws.client_factory import get_default_factory
import concurrent.futures

# Re-export so callers can: from chandra.observability import get_logger
get_logger = _get_logger

# Module-level bounded executor for CloudWatch metrics
_cw_pool = concurrent.futures.ThreadPoolExecutor(max_workers=5, thread_name_prefix="cw_metrics")

def _emit_metric(name: str, value: float, unit: str, dims: dict[str, str]) -> None:
    """Emit a CloudWatch metric asynchronously."""
    def _do_emit() -> None:
        try:
            cw = get_default_factory().client("cloudwatch")
            cw.put_metric_data(
                Namespace="Chandra",
                MetricData=[{
                    "MetricName": name,
                    "Value": value,
                    "Unit": unit,
                    "Dimensions": [{"Name": k, "Value": v} for k, v in dims.items()],
                }],
            )
        except Exception as e:
            _get_logger(__name__).warning("Failed to emit CloudWatch metric", error=str(e), metric_name=name)

    try:
        _cw_pool.submit(_do_emit)
    except Exception:
        pass # Ignore errors if pool is shutting down


# Module-level instruments — point at global no-op providers until
# configure_observability() promotes them to real providers.
_meter = metrics.get_meter("chandra")
node_latency: metrics.Histogram = _meter.create_histogram(
    "Chandra.node_latency", unit="ms", description="Node execution latency in ms"
)
node_errors: metrics.Counter = _meter.create_counter(
    "Chandra.node_errors", description="Node error count"
)


def configure_observability(
    otel_endpoint: str | None = None,
    log_level: str = "INFO",
    environment: str = "production",
) -> None:
    """Configure observability: structlog + OTEL tracing + metrics.

    If otel_endpoint is None, uses OTEL's global no-op providers (zero config).
    """
    configure_logging(log_level)

    if not otel_endpoint:
        return  # OTEL global defaults are already no-op

    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create({
        "service.name": "chandra",
        "deployment.environment": environment,
    })

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=otel_endpoint))
    )
    trace.set_tracer_provider(tracer_provider)

    reader = PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=otel_endpoint))
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[reader]))


@contextmanager
def task_context(run_id: str, account_id: str) -> Iterator[None]:
    """Context manager: bind run_id and account_id to structlog contextvars.

    Any log lines emitted within this context automatically include run_id and
    account_id due to structlog.contextvars.merge_contextvars in the processor chain.
    """
    structlog.contextvars.bind_contextvars(run_id=run_id, account_id=account_id)
    try:
        yield
    finally:
        structlog.contextvars.unbind_contextvars("run_id", "account_id")


_F = TypeVar("_F", bound=Callable[..., Any])


@overload
def traced_node(fn: _F) -> _F:
    ...


@overload
def traced_node(
    fn: None = None, *, name: str | None = None, timeout_s: float | None = None
) -> Callable[[_F], _F]:
    ...


def traced_node(
    fn: _F | None = None,
    *,
    name: str | None = None,
    timeout_s: float | None = None,
) -> _F | Callable[[_F], _F]:
    """Decorate a node function to emit OTEL spans, contextvar bindings, and metrics.

    Supports three call forms:
    - @traced_node
    - @traced_node()
    - @traced_node(name="custom", timeout_s=30.0)
    """

    def decorator(f: _F) -> _F:
        node_name = name or f.__name__
        logger = _get_logger(f.__module__)

        def _run_sync_span(state: Any, call: Callable[[], Any]) -> Any:
            run_id = state.get("run_id", "") if isinstance(state, dict) else ""
            account_id = state.get("account_id", "") if isinstance(state, dict) else ""
            tracer = trace.get_tracer("chandra")
            with tracer.start_as_current_span(
                f"node.{node_name}",
                attributes={
                    "node.name": node_name,
                    "run_id": run_id,
                    "account.id": account_id,
                },
            ) as span:
                structlog.contextvars.bind_contextvars(node=node_name)
                t0 = time.perf_counter()
                logger.info("node.start", node=node_name)
                try:
                    result = call()
                    ms = (time.perf_counter() - t0) * 1_000
                    span.set_attribute("latency_ms", ms)
                    logger.info("node.end", node=node_name, latency_ms=round(ms, 2))
                    node_latency.record(ms, {"node": node_name})
                    _emit_metric("NodeLatency", ms, "Milliseconds", {"node": node_name, "agent": "chandra"})
                    return result
                except Exception as exc:
                    ms = (time.perf_counter() - t0) * 1_000
                    span.set_status(StatusCode.ERROR, str(exc))
                    span.record_exception(exc)
                    logger.error("node.error", node=node_name, error=str(exc))
                    node_errors.add(1, {"node": node_name})
                    _emit_metric("NodeErrors", 1.0, "Count", {"node": node_name, "error_type": type(exc).__name__})
                    raise
                finally:
                    structlog.contextvars.unbind_contextvars("node")

        if inspect.iscoroutinefunction(f):

            @functools.wraps(f)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                state = args[0] if args else {}
                coro = f(*args, **kwargs)
                if timeout_s is not None:
                    coro = asyncio.wait_for(coro, timeout=timeout_s)

                run_id = (
                    state.get("run_id", "") if isinstance(state, dict) else ""
                )
                account_id = (
                    state.get("account_id", "") if isinstance(state, dict) else ""
                )
                tracer = trace.get_tracer("chandra")
                with tracer.start_as_current_span(
                    f"node.{node_name}",
                    attributes={
                        "node.name": node_name,
                        "run_id": run_id,
                        "account.id": account_id,
                    },
                ) as span:
                    structlog.contextvars.bind_contextvars(node=node_name)
                    t0 = time.perf_counter()
                    logger.info("node.start", node=node_name)
                    try:
                        result = await coro
                        ms = (time.perf_counter() - t0) * 1_000
                        span.set_attribute("latency_ms", ms)
                        logger.info(
                            "node.end", node=node_name, latency_ms=round(ms, 2)
                        )
                        node_latency.record(ms, {"node": node_name})
                        _emit_metric("NodeLatency", ms, "Milliseconds", {"node": node_name, "agent": "chandra"})
                        return result
                    except Exception as exc:
                        ms = (time.perf_counter() - t0) * 1_000
                        span.set_status(StatusCode.ERROR, str(exc))
                        span.record_exception(exc)
                        logger.error("node.error", node=node_name, error=str(exc))
                        node_errors.add(1, {"node": node_name})
                        _emit_metric("NodeErrors", 1.0, "Count", {"node": node_name, "error_type": type(exc).__name__})
                        raise
                    finally:
                        structlog.contextvars.unbind_contextvars("node")

            return async_wrapper  # type: ignore[return-value]
        else:

            @functools.wraps(f)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                state = args[0] if args else {}
                if timeout_s is not None:
                    import concurrent.futures

                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                        future = ex.submit(f, *args, **kwargs)
                        return _run_sync_span(state, lambda: future.result(timeout=timeout_s))
                return _run_sync_span(state, lambda: f(*args, **kwargs))

            return sync_wrapper  # type: ignore[return-value]

    if fn is not None:
        return decorator(fn)
    return decorator
