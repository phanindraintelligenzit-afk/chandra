"""compose_execution_summary: narrative only, deterministic fallback when the LLM is down."""

from __future__ import annotations

from typing import Any

from src.chandra.briefing import composer


class _FakeLLM:
    def __init__(self, text: str) -> None:
        self._text = text
        self.prompts: list[str] = []

    def invoke(self, prompt: str) -> Any:
        self.prompts.append(prompt)

        class _Resp:
            content = self._text

        return _Resp()


def test_uses_llm_narrative_when_available(monkeypatch: Any) -> None:
    fake = _FakeLLM("- created bucket x\n- enabled versioning")
    monkeypatch.setattr("src.chandra.llm.get_llm", lambda: fake)
    out = composer.compose_execution_summary("Create S3 bucket", "apply complete\nbucket x created")
    assert "created bucket x" in out
    assert "Execution summary" in out
    assert "bucket x created" in fake.prompts[0]


def test_falls_back_to_log_tail_when_llm_unavailable(monkeypatch: Any) -> None:
    def _boom() -> Any:
        raise RuntimeError("no provider")

    monkeypatch.setattr("src.chandra.llm.get_llm", _boom)
    logs = "\n".join(f"line {i}" for i in range(40))
    out = composer.compose_execution_summary("Restart EC2", logs)
    assert "raw tail" in out
    assert "line 39" in out
    assert "line 0" not in out  # only the tail is included


def test_empty_llm_response_falls_back(monkeypatch: Any) -> None:
    monkeypatch.setattr("src.chandra.llm.get_llm", lambda: _FakeLLM("   "))
    out = composer.compose_execution_summary("x", "some log")
    assert "raw tail" in out and "some log" in out
