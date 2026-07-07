"""Unit tests for observability infrastructure (OTEL tracing + structlog + metrics)."""

from __future__ import annotations

import asyncio
import sys

import pytest
import structlog.contextvars
from moto import mock_aws
from src.chandra.observability import (
    configure_observability,
    task_context,
    traced_node,
)


@pytest.fixture(autouse=True)
def mock_aws_for_metrics():
    """Mock AWS for all tests in this module so _emit_metric doesn't hit real AWS."""
    with mock_aws():
        yield


class TestTracedNode:
    def test_traced_node_bare(self) -> None:
        """@traced_node without call wraps sync fn; returns normally."""

        @traced_node
        def sync_func(state: dict) -> str:
            return "result"

        result = sync_func({"run_id": "test-run", "account_id": "123"})
        assert result == "result"

    def test_traced_node_with_name(self) -> None:
        """@traced_node(name="x") uses custom name."""

        @traced_node(name="custom_node")
        def sync_func(state: dict) -> str:
            return "custom_result"

        result = sync_func({"run_id": "test-run", "account_id": "123"})
        assert result == "custom_result"

    def test_traced_node_reraises(self) -> None:
        """Decorated fn that raises; exception propagates."""

        @traced_node
        def failing_func(state: dict) -> None:
            raise ValueError("test error")

        with pytest.raises(ValueError, match="test error"):
            failing_func({"run_id": "test-run", "account_id": "123"})

    def test_traced_node_noop_no_endpoint(self) -> None:
        """configure_observability(None) is a no-op; decorated fn works."""
        configure_observability(otel_endpoint=None)

        @traced_node
        def sync_func(state: dict) -> str:
            return "still_works"

        result = sync_func({"run_id": "test-run", "account_id": "123"})
        assert result == "still_works"

    @pytest.mark.asyncio
    async def test_traced_node_async_support(self) -> None:
        """Async function decorated with @traced_node; awaited result correct."""

        @traced_node
        async def async_func(state: dict) -> str:
            await asyncio.sleep(0.01)
            return "async_result"

        result = await async_func({"run_id": "test-run", "account_id": "123"})
        assert result == "async_result"

    @pytest.mark.skipif(sys.platform == "win32", reason="SIGALRM not available on Windows")
    def test_traced_node_timeout_sync(self) -> None:
        """Sync function with timeout raises TimeoutError on slow execution."""

        @traced_node(timeout_s=0.01)
        def slow_func(state: dict) -> str:
            import time

            time.sleep(0.5)
            return "too_slow"

        with pytest.raises(TimeoutError):
            slow_func({"run_id": "test-run", "account_id": "123"})

    @pytest.mark.asyncio
    async def test_traced_node_timeout_async(self) -> None:
        """Async function with timeout raises TimeoutError on slow execution."""

        @traced_node(timeout_s=0.01)
        async def slow_async_func(state: dict) -> str:
            await asyncio.sleep(0.5)
            return "too_slow"

        with pytest.raises(asyncio.TimeoutError):
            await slow_async_func({"run_id": "test-run", "account_id": "123"})


class TestTaskContext:
    def test_task_context_binds_vars(self) -> None:
        """Within task_context(run_id, account_id), structlog binds those vars."""
        # Clear any prior bindings
        structlog.contextvars.clear_contextvars()

        with task_context(run_id="run-123", account_id="456789"):
            context_vars = structlog.contextvars.get_contextvars()
            assert context_vars.get("run_id") == "run-123"
            assert context_vars.get("account_id") == "456789"

        # After exiting, vars should be unbound
        context_vars = structlog.contextvars.get_contextvars()
        assert context_vars.get("run_id") is None
        assert context_vars.get("account_id") is None


class TestConfigureObservability:
    def test_configure_observability_noop(self) -> None:
        """configure_observability() with no endpoint is a no-op."""
        # Should not raise any errors
        configure_observability(otel_endpoint=None, log_level="INFO")

    def test_configure_observability_with_invalid_endpoint(self) -> None:
        """configure_observability() with invalid endpoint doesn't crash on decorator use."""
        # Should not raise during setup (import happens lazily)
        configure_observability(
            otel_endpoint="http://invalid-endpoint-that-does-not-exist:4317",
            log_level="INFO",
        )

        # Decorated functions should still work (they're just no-op metrics)
        @traced_node
        def func_with_bad_endpoint(state: dict) -> str:
            return "still_works"

        result = func_with_bad_endpoint({"run_id": "test", "account_id": "123"})
        assert result == "still_works"
