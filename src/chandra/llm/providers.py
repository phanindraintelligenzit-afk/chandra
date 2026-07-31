"""Explicit LLM provider interface.

``build_chat_model`` (the factory) chooses a backend from ``LLM_PROVIDER``;
this module wraps it in a small, uniform ``BaseLLM`` surface that the
reasoning layer (planner, self-correction) programs against — so business
logic depends on ``BaseLLM``, never on Bedrock vs vLLM vs Ollama.

The contract is deliberately tiny: ``complete()`` returns text, with
retries + timeout + generation params (temperature / top_p / max_tokens),
and ``health_check()`` probes reachability. Each concrete provider only
sets which backend + model to build; the completion/retry logic lives once
in the base, so there is no per-provider business-logic duplication.

The provider never touches AWS, boto3, Terraform, or kubectl — it only
produces text/JSON. Execution stays in the deterministic executor.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from src.chandra.config import settings
from src.chandra.llm import build_chat_model
from src.chandra.logging import get_logger

logger = get_logger(__name__)


@dataclass
class GenerationParams:
    """Backend-agnostic generation knobs (vLLM/OpenAI-compatible superset)."""

    temperature: float = 0.0
    top_p: float = 1.0
    max_tokens: int = 2048
    timeout_s: float = 60.0
    max_retries: int = 2

    def model_kwargs(self) -> dict[str, Any]:
        return {"temperature": self.temperature, "max_tokens": self.max_tokens}


class BaseLLM(ABC):
    """Uniform reasoning surface. Text/JSON only — never executes anything."""

    provider: str = "bedrock"

    def __init__(self, model: str | None = None, params: GenerationParams | None = None) -> None:
        self.model = model
        self.params = params or GenerationParams()

    @abstractmethod
    def _build(self) -> Any:
        """Construct the underlying LangChain chat model for this provider."""

    def _build_with_timeout(self, **overrides: Any) -> Any:
        """Build the model with the BaseLLM's timeout, falling back to _build().

        Ensures the LangChain constructor uses the BaseLLM's configured
        timeout (via request_timeout) instead of the factory default, so the
        retry loop controls the actual wall-clock timeout rather than stacking
        on top of LangChain's own long timeout.

        Falls back to ``_build()`` for test/mock providers that aren't
        registered in the ``build_chat_model`` factory.
        """
        params = self.params
        kwargs = params.model_kwargs()
        kwargs["timeout"] = overrides.get("timeout", params.timeout_s)
        kwargs["max_retries"] = 0  # retries handled by BaseLLM loop
        try:
            return build_chat_model(self.model, provider=self.provider, **kwargs)
        except ValueError:
            # Provider not registered in the factory (e.g. test stubs).
            return self._build()

    def complete(self, system: str, user: str, **overrides: Any) -> str:
        """Return the model's text completion, with retries + timeout.

        ``overrides`` may carry ``temperature`` / ``max_tokens`` etc. for a
        single call. Raises the last exception if every attempt fails.
        """
        params = self.params
        attempts = params.max_retries + 1
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                llm = self._build_with_timeout(**overrides)
                response = llm.invoke([("system", system), ("user", user)])
                content = response.content
                return content if isinstance(content, str) else str(content)
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "llm.complete_attempt_failed",
                    provider=self.provider,
                    attempt=attempt,
                    error=str(exc),
                )
                if attempt < attempts:
                    time.sleep(min(2**attempt, 8))
        assert last_exc is not None
        raise last_exc

    def health_check(self) -> bool:
        """Cheap reachability probe. True when the backend answers.

        Uses a 5s timeout with no retries so it never blocks the health
        endpoint for more than a few seconds.
        """
        try:
            llm = self._build_with_timeout(timeout=5)
            reply = llm.invoke([("system", "You are a health probe."), ("user", "Reply with OK.")])
            return bool(reply.content)
        except Exception as exc:
            logger.warning("llm.health_check_failed", provider=self.provider, error=str(exc))
            return False


class VLLMProvider(BaseLLM):
    """vLLM (or any OpenAI-compatible) endpoint — the local-LLM target."""

    provider = "vllm"

    def _build(self) -> Any:
        return build_chat_model(
            model=self.model, provider=self.provider, **self.params.model_kwargs()
        )


class OpenAICompatibleProvider(VLLMProvider):
    """Alias for any OpenAI-compatible server (TGI, LM Studio, ...)."""

    provider = "openai"


class OllamaProvider(BaseLLM):
    """Local Ollama daemon (its OpenAI-compatible /v1 API)."""

    provider = "ollama"

    def _build(self) -> Any:
        return build_chat_model(
            model=self.model, provider=self.provider, **self.params.model_kwargs()
        )


class BedrockProvider(BaseLLM):
    """Amazon Bedrock (the legacy default; kept for parity + fallback)."""

    provider = "bedrock"

    def _build(self) -> Any:
        return build_chat_model(
            model=self.model, provider=self.provider, **self.params.model_kwargs()
        )


ClaudeProvider = BedrockProvider

_PROVIDERS: dict[str, type[BaseLLM]] = {
    "vllm": VLLMProvider,
    "openai": OpenAICompatibleProvider,
    "openai_compatible": OpenAICompatibleProvider,
    "ollama": OllamaProvider,
    "bedrock": BedrockProvider,
    "claude": BedrockProvider,
}


def get_provider(name: str | None = None, **kwargs: Any) -> BaseLLM:
    """Instantiate the configured provider (``LLM_PROVIDER`` when unset)."""
    key = (name or settings.llm_provider or "bedrock").strip().lower()
    cls = _PROVIDERS.get(key)
    if cls is None:
        raise ValueError(
            f"Unknown LLM provider {key!r}; expected one of {', '.join(sorted(_PROVIDERS))}"
        )
    return cls(**kwargs)
