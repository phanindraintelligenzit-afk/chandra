"""Pluggable LLM provider factory: env-driven selection, no hardcoding.

No network involved — LangChain chat-model constructors are lazy.
"""

from __future__ import annotations

import pytest
from src.chandra.config import settings
from src.chandra.llm import build_chat_model


class TestProviderSelection:
    def test_default_is_bedrock_with_configured_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "llm_provider", "bedrock")
        llm = build_chat_model()
        assert type(llm).__name__ == "ChatBedrockConverse"
        assert llm.model_id == settings.bedrock_model_id

    def test_bedrock_model_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "llm_provider", "bedrock")
        llm = build_chat_model(model="moonshotai.kimi-k2.5")
        assert llm.model_id == "moonshotai.kimi-k2.5"

    def test_openai_compatible_endpoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "llm_provider", "openai")
        monkeypatch.setattr(settings, "openai_api_base", "http://vllm.internal:8000/v1")
        monkeypatch.setattr(settings, "openai_model_name", "some/quantized-model")
        llm = build_chat_model()
        assert type(llm).__name__ == "ChatOpenAI"
        assert llm.model_name == "some/quantized-model"

    def test_openai_requires_base_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "llm_provider", "openai")
        monkeypatch.setattr(settings, "openai_api_base", None)
        with pytest.raises(ValueError, match="OPENAI_API_BASE"):
            build_chat_model()

    def test_ollama_uses_openai_compatible_v1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "llm_provider", "ollama")
        monkeypatch.setattr(settings, "ollama_host", "http://gpu-box:11434/")
        monkeypatch.setattr(settings, "ollama_model", "llama3.1:8b")
        llm = build_chat_model()
        assert type(llm).__name__ == "ChatOpenAI"
        assert llm.model_name == "llama3.1:8b"
        assert str(llm.openai_api_base).rstrip("/") == "http://gpu-box:11434/v1"

    def test_ollama_refuses_hardcoded_default_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The production model is a benchmark decision — never an implicit default."""
        monkeypatch.setattr(settings, "llm_provider", "ollama")
        monkeypatch.setattr(settings, "ollama_model", None)
        with pytest.raises(ValueError, match="OLLAMA_MODEL"):
            build_chat_model()

    def test_unknown_provider_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "llm_provider", "skynet")
        with pytest.raises(ValueError, match="Unsupported LLM_PROVIDER"):
            build_chat_model()
