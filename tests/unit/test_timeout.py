"""Tests for per-node timeout enforcement."""

import asyncio

from chandra.observability import traced_node


def test_timeout_decorator_exists():
    """Test that traced_node decorator exists."""
    assert callable(traced_node)


def test_traced_node_with_timeout():
    """Test traced_node with timeout parameter."""
    
    @traced_node("test_node", timeout_s=1)
    async def slow_func():
        await asyncio.sleep(5)
        return {"status": "ok"}
    
    result = asyncio.run(slow_func())
    assert "errors" in result
    assert result["errors"][0]["type"] == "timeout"


def test_traced_node_without_timeout():
    """Test traced_node without timeout."""
    
    @traced_node("test_node")
    async def fast_func():
        await asyncio.sleep(0.1)
        return {"status": "ok"}
    
    result = asyncio.run(fast_func())
    assert result["status"] == "ok"
    assert "errors" not in result


def test_timeout_structured_error():
    """Test timeout error structure."""
    
    @traced_node("slow_observer", timeout_s=1)
    async def observer():
        await asyncio.sleep(5)
        return {}
    
    result = asyncio.run(observer())
    error = result["errors"][0]
    assert error["node"] == "slow_observer"
    assert error["type"] == "timeout"
    assert error["timeout_s"] == 1


def test_fast_node_completes():
    """Test fast node completes."""
    
    @traced_node("fast_observer", timeout_s=5)
    async def observer():
        await asyncio.sleep(0.1)
        return {"findings": 5}
    
    result = asyncio.run(observer())
    assert "errors" not in result
    assert result["findings"] == 5
