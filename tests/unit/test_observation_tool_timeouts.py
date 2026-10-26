from __future__ import annotations

import asyncio

import pytest
from tools.aws_cloud_tools import langchain_tools


def test_aws_observation_tool_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    async def slow_tool() -> str:
        await asyncio.sleep(0.1)
        return "finished"

    monkeypatch.setattr(langchain_tools, "_AWS_TOOL_TIMEOUT_SECONDS", 0.01)

    with pytest.raises(TimeoutError):
        langchain_tools._run(slow_tool())
