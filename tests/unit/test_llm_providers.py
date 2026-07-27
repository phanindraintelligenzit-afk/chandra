"""Provider interface: uniform BaseLLM surface over the factory.

No real backend is contacted — ``_build`` is patched to return a fake
chat model, so these tests exercise retry/timeout/health-check logic and
provider selection deterministically.
"""

from __future__ import annotations

from typing import Any

import pytest
from src.chandra.llm.providers import (
    BaseLLM,
    BedrockProvider,
    ClaudeProvider,
    GenerationParams,
    OllamaProvider,
    OpenAICompatibleProvider,
    VLLMProvider,
    get_provider,
)


class _FakeMessage:
    def __init__(self, content: Any) -> None:
        self.content = content


class _FakeModel:
    """Records invocations; optionally fails a set number of times first."""

    def __init__(self, content: Any = "OK", fail_times: int = 0) -> None:
        self._content = content
        self._fail_times = fail_times
        self.calls = 0

    def invoke(self, messages: list[tuple[str, str]]) -> _FakeMessage:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise RuntimeError("transient backend error")
        return _FakeMessage(self._content)


class _StubProvider(BaseLLM):
    provider = "stub"

    def __init__(self, model: _FakeModel, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._model = model

    def _build(self) -> Any:
        return self._model


def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.chandra.llm.providers.time.sleep", lambda *_: None)


def test_complete_returns_text() -> None:
    p = _StubProvider(_FakeModel(content="hello"))
    assert p.complete("sys", "user") == "hello"


def test_complete_stringifies_non_str_content() -> None:
    p = _StubProvider(_FakeModel(content=[{"type": "text", "text": "x"}]))
    out = p.complete("sys", "user")
    assert isinstance(out, str)
    assert "text" in out


def test_complete_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_sleep(monkeypatch)
    model = _FakeModel(content="recovered", fail_times=2)
    p = _StubProvider(model, params=GenerationParams(max_retries=2))
    assert p.complete("sys", "user") == "recovered"
    assert model.calls == 3  # 2 failures + 1 success


def test_complete_raises_after_exhausting_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_sleep(monkeypatch)
    model = _FakeModel(fail_times=99)
    p = _StubProvider(model, params=GenerationParams(max_retries=1))
    with pytest.raises(RuntimeError, match="transient backend error"):
        p.complete("sys", "user")
    assert model.calls == 2  # max_retries=1 → 2 attempts total


def test_health_check_true_on_reply() -> None:
    p = _StubProvider(_FakeModel(content="OK"))
    assert p.health_check() is True


def test_health_check_false_and_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_sleep(monkeypatch)
    p = _StubProvider(_FakeModel(fail_times=99), params=GenerationParams(max_retries=0))
    assert p.health_check() is False


def test_generation_params_model_kwargs() -> None:
    kw = GenerationParams(temperature=0.3, max_tokens=512).model_kwargs()
    assert kw == {"temperature": 0.3, "max_tokens": 512}


@pytest.mark.parametrize(
    ("name", "expected_cls", "expected_provider"),
    [
        ("vllm", VLLMProvider, "vllm"),
        ("openai", OpenAICompatibleProvider, "openai"),
        ("openai_compatible", OpenAICompatibleProvider, "openai"),
        ("ollama", OllamaProvider, "ollama"),
        ("bedrock", BedrockProvider, "bedrock"),
        ("claude", BedrockProvider, "bedrock"),
    ],
)
def test_get_provider_resolves(name: str, expected_cls: type, expected_provider: str) -> None:
    p = get_provider(name)
    assert isinstance(p, expected_cls)
    assert p.provider == expected_provider


def test_get_provider_unknown_raises() -> None:
    with pytest.raises(ValueError, match="Unknown LLM provider"):
        get_provider("does-not-exist")


def test_claude_is_bedrock_alias() -> None:
    assert ClaudeProvider is BedrockProvider


def test_vllm_build_calls_factory_with_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_build(model: str | None = None, provider: str | None = None, **kwargs: Any) -> str:
        seen["model"] = model
        seen["provider"] = provider
        seen.update(kwargs)
        return "built"

    monkeypatch.setattr("src.chandra.llm.providers.build_chat_model", fake_build)
    p = VLLMProvider(model="qwen2.5")
    assert p._build() == "built"
    assert seen["provider"] == "vllm"
    assert seen["model"] == "qwen2.5"
    assert "temperature" in seen and "max_tokens" in seen
