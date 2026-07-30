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


def build_chat_model(model: str | None = None, provider: str | None = None, **kwargs: Any) -> Any:
    """Return a LangChain chat model for the configured provider.

    Parameters
    ----------
    model:
        Optional model override. When omitted, the provider's configured
        model is used (``BEDROCK_MODEL_ID`` / ``OPENAI_MODEL_NAME`` /
        ``OLLAMA_MODEL``). The root agents pass their legacy
        ``MODEL_NAME`` env var here so their behavior is unchanged under
        the default provider.
    provider:
        Optional backend override. When omitted, ``LLM_PROVIDER`` selects
        it. The provider classes in ``chandra.llm.providers`` pass this so
        a ``VLLMProvider`` builds a vLLM client regardless of the ambient
        ``LLM_PROVIDER`` value.
    kwargs:
        Passed through to the underlying client (temperature, callbacks…).
    """
    provider = (provider or settings.llm_provider or "bedrock").strip().lower()

    if provider == "bedrock":
        from langchain_aws import ChatBedrockConverse  # lazy: provider-specific

        kwargs.setdefault("timeout", 60)
        return ChatBedrockConverse(
            model_id=model or settings.bedrock_model_id,
            region_name=settings.aws_default_region,
            **kwargs,
        )

    if provider in ("openai", "openai_compatible", "vllm"):
        from langchain_openai import ChatOpenAI  # lazy: provider-specific

        kwargs.setdefault("timeout", 60)
        kwargs.setdefault("max_retries", 2)

        # vLLM reads its own VLLM_* vars first, then falls back to the generic
        # OPENAI_* pair so any OpenAI-compatible server keeps working.
        base_url = settings.vllm_api_base or settings.openai_api_base
        api_key = settings.vllm_api_key or settings.openai_api_key or "not-needed"
        if not base_url:
            raise ValueError(
                f"LLM_PROVIDER={provider} requires VLLM_API_BASE (or OPENAI_API_BASE) — "
                "the OpenAI-compatible endpoint URL"
            )
        resolved = model or settings.vllm_model or settings.openai_model_name
        if not resolved:
            raise ValueError(
                f"LLM_PROVIDER={provider} requires VLLM_MODEL (or OPENAI_MODEL_NAME, "
                "or a model override) — the production model is chosen by benchmark, "
                "not hardcoded; set it explicitly."
            )
        return ChatOpenAI(
            base_url=base_url,
            api_key=api_key,
            model=resolved,
            **kwargs,
        )

    if provider == "ollama":
        from langchain_openai import ChatOpenAI  # lazy: provider-specific

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


def build_chat_model_with_fallback(model: str | None = None, provider: str | None = None, **kwargs: Any) -> Any:
    """Build a chat model with automatic fallback to Bedrock when local LLM is unavailable.

    Tries the configured provider first. If the connection fails (timeout,
    connection refused, 5xx), falls back to Bedrock. This ensures zero-downtime
    even when the local vLLM server is down or restarting.

    Returns a tuple of (model, actual_provider) so callers can log which
    provider served the request.
    """
    provider = (provider or settings.llm_provider or "bedrock").strip().lower()
    tried_provider = provider

    try:
        return (build_chat_model(model=model, provider=provider, **kwargs), provider)
    except Exception as exc:
        logger.warning(
            "LLM provider '%s' failed (%s: %s). Falling back to Bedrock.",
            provider, type(exc).__name__, exc,
        )
        if provider != "bedrock":
            try:
                return (build_chat_model(model=settings.bedrock_model_id, provider="bedrock", **kwargs), "bedrock")
            except Exception as fallback_exc:
                logger.error("Bedrock fallback also failed: %s", fallback_exc)
                raise  # Raise original if both fail
        raise  # Already on Bedrock, re-raise original