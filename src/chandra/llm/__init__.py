"""Pluggable chat-model factory — the ONE place a provider is chosen.

Every LLM call site in Chandra (composer, verifier, the root observation/
analyzer/execution agents, the copilot) obtains its model here instead of
constructing ``ChatBedrockConverse`` / ``ChatOpenAI`` directly. Which
backend actually serves the request is decided by ``LLM_PROVIDER``:

* ``bedrock``  — Amazon Bedrock via ``langchain_aws.ChatBedrockConverse``
  (the default; fully backward compatible with the existing deployment).
* ``openai``   — any OpenAI-compatible endpoint (vLLM, TGI, LM Studio …)
  at ``OPENAI_API_BASE`` serving ``OPENAI_MODEL_NAME``.
* ``ollama``   — a local Ollama daemon at ``OLLAMA_HOST`` serving
  ``OLLAMA_MODEL`` through Ollama's OpenAI-compatible ``/v1`` API.

This replaces the previous pattern of hand-commented ``ChatOpenAI``
blocks next to each ``ChatBedrockConverse`` construction: swapping the
inference backend is now an environment change, never a code change.
There is deliberately no hardcoded local model — the production model is
selected by benchmarking candidates on Chandra workloads.

Determinism contract unchanged: this module *builds* clients; which
nodes may *call* them is still governed by the graph rules (planning /
analysis / narrative only).
"""

from __future__ import annotations

from typing import Any

from src.chandra.config import settings
from src.chandra.logging import get_logger

logger = get_logger(__name__)

#: Providers accepted in ``LLM_PROVIDER`` (aliases included).
SUPPORTED_PROVIDERS = ("bedrock", "openai", "openai_compatible", "vllm", "ollama")


def build_chat_model(model: str | None = None, **kwargs: Any) -> Any:
    """Return a LangChain chat model for the configured provider.

    Parameters
    ----------
    model:
        Optional model override. When omitted, the provider's configured
        model is used (``BEDROCK_MODEL_ID`` / ``OPENAI_MODEL_NAME`` /
        ``OLLAMA_MODEL``). The root agents pass their legacy
        ``MODEL_NAME`` env var here so their behavior is unchanged under
        the default provider.
    kwargs:
        Passed through to the underlying client (temperature, callbacks…).
    """
    provider = (settings.llm_provider or "bedrock").strip().lower()

    if provider == "bedrock":
        from langchain_aws import ChatBedrockConverse  # noqa: PLC0415  # lazy: provider-specific

        return ChatBedrockConverse(
            model_id=model or settings.bedrock_model_id,
            region_name=settings.aws_default_region,
            **kwargs,
        )

    if provider in ("openai", "openai_compatible", "vllm"):
        from langchain_openai import ChatOpenAI  # noqa: PLC0415  # lazy: provider-specific

        if not settings.openai_api_base:
            raise ValueError(
                "LLM_PROVIDER=openai requires OPENAI_API_BASE (the OpenAI-compatible endpoint URL)"
            )
        resolved = model or settings.openai_model_name
        if not resolved:
            raise ValueError("LLM_PROVIDER=openai requires OPENAI_MODEL_NAME (or a model override)")
        return ChatOpenAI(
            base_url=settings.openai_api_base,
            api_key=settings.openai_api_key,
            model=resolved,
            **kwargs,
        )

    if provider == "ollama":
        from langchain_openai import ChatOpenAI  # noqa: PLC0415  # lazy: provider-specific

        resolved = model or settings.ollama_model
        if not resolved:
            raise ValueError(
                "LLM_PROVIDER=ollama requires OLLAMA_MODEL — the production model is "
                "chosen by benchmark, not hardcoded; set it explicitly."
            )
        base = settings.ollama_host.rstrip("/")
        return ChatOpenAI(
            base_url=f"{base}/v1",
            api_key="ollama",  # Ollama ignores the key but the client requires one
            model=resolved,
            **kwargs,
        )

    raise ValueError(
        f"Unsupported LLM_PROVIDER {provider!r}; expected one of {', '.join(SUPPORTED_PROVIDERS)}"
    )
