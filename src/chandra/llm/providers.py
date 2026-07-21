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
        # Only pass params the LangChain constructors accept directly; the
        # rest (timeout/retries) are handled by the retry loop below.
        return {"temperature": self.temperature, "max_tokens": self.max_tokens}


class BaseLLM(ABC):
    """Uniform reasoning surface. Text/JSON only — never executes anything."""

    #: Provider key understood by ``build_chat_model`` (LLM_PROVIDER value).
    provider: str = "bedrock"

    def __init__(self, model: str | None = None, params: GenerationParams | None = None) -> None:
        self.model = model
        self.params = params or GenerationParams()

    @abstractmethod
    def _build(self) -> Any:
        """Construct the underlying LangChain chat model for this provider."""

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
                llm = self._build()
                response = llm.invoke([("system", system), ("user", user)])
                content = response.content
                return content if isinstance(content, str) else str(content)
            except Exception as exc:  # provider-agnostic retry boundary
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
        """Cheap reachability probe. True when the backend answers."""
        try:
            reply = self.complete("You are a health probe.", "Reply with the single word OK.")
            return bool(reply)
        except Exception as exc:  # health check must never raise
            logger.warning("llm.health_check_failed", provider=self.provider, error=str(exc))
            return False


class VLLMProvider(BaseLLM):
    """vLLM (or any OpenAI-compatible) endpoint — the local-LLM target.

    Uses ``OPENAI_API_BASE`` + ``OPENAI_MODEL_NAME`` via the factory's
    ``openai``/``vllm`` path.
    """

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


#: Legacy name — Claude on Bedrock is the same backend path.
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
    """Instantiate the configured provider (``LLM_PROVIDER`` when unset).

    A single seam: business logic calls ``get_provider()`` and receives a
    ``BaseLLM`` — swapping Claude→vLLM is an env change (``LLM_PROVIDER``),
    exactly like the factory, but now behind the richer provider surface.
    """
    key = (name or settings.llm_provider or "bedrock").strip().lower()
    cls = _PROVIDERS.get(key)
    if cls is None:
        raise ValueError(
            f"Unknown LLM provider {key!r}; expected one of {', '.join(sorted(_PROVIDERS))}"
        )
    # Each class already carries the correct ``build_chat_model`` factory key
    # in its ``provider`` attribute (e.g. OpenAICompatibleProvider→"openai",
    # BedrockProvider→"bedrock"), so no post-hoc override is needed.
    return cls(**kwargs)
