import asyncio
import pytest
from src.chandra.observability import traced_node


def test_timeout_decorator_exists():
    assert callable(traced_node)


@pytest.mark.asyncio
async def test_traced_node_with_timeout():
    @traced_node(timeout_s=5)
    async def fast_node(state):
        await asyncio.sleep(0.1)
        return {"findings": ["finding_1"]}
    result = await fast_node({})
    assert result == {"findings": ["finding_1"]}


@pytest.mark.asyncio
async def test_traced_node_without_timeout():
    @traced_node
    async def simple_node(state):
        return {"findings": []}
    result = await simple_node({})
    assert result == {"findings": []}


@pytest.mark.asyncio
async def test_timeout_structured_error():
    @traced_node(timeout_s=1)
    async def slow_node(state):
        await asyncio.sleep(5)
        return {}
    with pytest.raises(asyncio.TimeoutError):
        await slow_node({})


@pytest.mark.asyncio
async def test_fast_node_completes():
    @traced_node(timeout_s=90)
    async def observer(state):
        await asyncio.sleep(0.05)
        return {"findings": ["cost_anomaly"]}
    result = await observer({})
    assert len(result["findings"]) == 1
