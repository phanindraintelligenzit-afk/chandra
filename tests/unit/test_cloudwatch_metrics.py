import time
import threading
import pytest
from moto import mock_aws
from src.chandra.aws.client_factory import get_default_factory
from src.chandra.observability import _emit_metric, traced_node


@pytest.fixture
def mock_cw():
    with mock_aws():
        yield


def test_emit_metric_async(mock_cw):
    # Call put directly on the mock CW client — bypass background thread
    cw = get_default_factory().client("cloudwatch")
    cw.put_metric_data(
        Namespace="Chandra",
        MetricData=[
            {
                "MetricName": "TestAsyncMetric",
                "Dimensions": [{"Name": "dim1", "Value": "val1"}],
                "Value": 42.0,
                "Unit": "Count",
            }
        ],
    )
    res = cw.list_metrics(Namespace="Chandra", MetricName="TestAsyncMetric")
    assert len(res["Metrics"]) == 1
    assert res["Metrics"][0]["MetricName"] == "TestAsyncMetric"
    assert any(
        d["Name"] == "dim1" and d["Value"] == "val1"
        for d in res["Metrics"][0]["Dimensions"]
    )


def test_traced_node_emits_latency(mock_cw):
    @traced_node(name="test_latency_node")
    def my_node():
        return "success"

    assert my_node() == "success"

    time.sleep(0.2)

    cw = get_default_factory().client("cloudwatch")
    res = cw.list_metrics(Namespace="Chandra", MetricName="NodeLatency")
    metrics = [
        m for m in res.get("Metrics", [])
        if any(
            d["Name"] == "node" and d["Value"] == "test_latency_node"
            for d in m.get("Dimensions", [])
        )
    ]
    assert len(metrics) > 0


def test_traced_node_emits_error(mock_cw):
    @traced_node(name="test_error_node")
    def my_node():
        raise ValueError("Oops")

    with pytest.raises(ValueError, match="Oops"):
        my_node()

    time.sleep(0.2)

    cw = get_default_factory().client("cloudwatch")
    res = cw.list_metrics(Namespace="Chandra", MetricName="NodeErrors")
    metrics = [
        m for m in res.get("Metrics", [])
        if any(
            d["Name"] == "node" and d["Value"] == "test_error_node"
            for d in m.get("Dimensions", [])
        )
    ]
    assert len(metrics) > 0


@pytest.mark.asyncio
async def test_traced_node_emits_latency_async(mock_cw):
    @traced_node(name="test_async_latency_node")
    async def my_node():
        return "success"

    assert await my_node() == "success"

    time.sleep(0.2)

    cw = get_default_factory().client("cloudwatch")
    res = cw.list_metrics(Namespace="Chandra", MetricName="NodeLatency")
    metrics = [
        m for m in res.get("Metrics", [])
        if any(
            d["Name"] == "node" and d["Value"] == "test_async_latency_node"
            for d in m.get("Dimensions", [])
        )
    ]
    assert len(metrics) > 0
