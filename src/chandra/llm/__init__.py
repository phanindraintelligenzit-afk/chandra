"""Pluggable chat-model factory — the ONE place a provider is chosen.

``build_chat_model`` returns a LangChain chat model backed by one of
several providers, driven solely by ``LLM_PROVIDER``:

- ``bedrock`` (default) → ``ChatBedrockConverse``
- ``vllm`` / ``openai``  → ``ChatOpenAI`` (any OpenAI-compatible server)
- ``ollama``             → ``ChatOpenAI`` (local Ollama daemon)

Usage::

    from src.chandra.llm import build_chat_model

    llm = build_chat_model()                         # respects env vars
    llm = build_chat_model(model="...")               # override model
    llm = build_chat_model(temperature=0.0)            # override temperature

    # With automatic fallback to Bedrock:
    from src.chandra.llm import build_chat_model_with_fallback
    model, provider = build_chat_model_with_fallback()
"""

from __future__ import annotations

from typing import Any

from src.chandra.config import settings
from src.chandra.logging import get_logger

logger = get_logger(__name__)

SUPPORTED_PROVIDERS = ("bedrock", "openai", "openai_compatible", "vllm", "ollama")


def build_chat_model(model: str | None = None, provider: str | None = None, **kwargs: Any) -> Any:
    """Build a chat model for the given provider."""
    provider = (provider or settings.llm_provider or "bedrock").strip().lower()

    if provider == "bedrock":
        from langchain_aws import ChatBedrockConverse

        kwargs.setdefault("timeout", 60)
        return ChatBedrockConverse(
            model_id=model or settings.bedrock_model_id,
            region_name=settings.aws_default_region,
            **kwargs,
        )

    if provider in ("openai", "openai_compatible", "vllm"):
        from langchain_openai import ChatOpenAI

        # Local providers should fail fast — use 10s timeout, no LangChain retries
        kwargs.setdefault("timeout", 10)
        kwargs.setdefault("max_retries", 0)
        base_url = settings.vllm_api_base or settings.openai_api_base
        api_key = settings.vllm_api_key or settings.openai_api_key or "not-needed"
        if not base_url:
            raise ValueError(f"LLM_PROVIDER={provider} requires VLLM_API_BASE (or OPENAI_API_BASE)")
        resolved = model or settings.vllm_model or settings.openai_model_name
        if not resolved:
            raise ValueError(f"LLM_PROVIDER={provider} requires VLLM_MODEL (or OPENAI_MODEL_NAME)")
        return ChatOpenAI(base_url=base_url, api_key=api_key, model=resolved, **kwargs)

    if provider == "ollama":
        from langchain_openai import ChatOpenAI

        kwargs.setdefault("timeout", 10)
        kwargs.setdefault("max_retries", 0)
        resolved = model or settings.ollama_model
        if not resolved:
            raise ValueError("LLM_PROVIDER=ollama requires OLLAMA_MODEL")
        base = settings.ollama_host.rstrip("/")
        return ChatOpenAI(base_url=f"{base}/v1", api_key="ollama", model=resolved, **kwargs)

    raise ValueError(
        f"Unsupported LLM_PROVIDER {provider!r}; expected one of {', '.join(SUPPORTED_PROVIDERS)}"
    )


def get_llm(model: str | None = None, **kwargs: Any) -> Any:
    """Get the default chat model. Compatible with the old API."""
    return build_chat_model(model=model, **kwargs)


def get_llm_with_tools(
    tools: list[Any] | None = None, model: str | None = None, **kwargs: Any
) -> Any:
    """Get a chat model bound to tools. For tool-calling agents."""
    llm = build_chat_model(model=model, **kwargs)
    if tools:
        return llm.bind_tools(tools)
    return llm


def build_chat_model_with_fallback(
    model: str | None = None,
    provider: str | None = None,
    **kwargs: Any,
) -> tuple[Any, str]:
    """Build a chat model with automatic fallback to Bedrock when local
    LLM is unavailable.

    Unlike ``build_chat_model`` (which returns a model object without
    connecting), this function performs a lightweight connectivity probe
    against the primary provider. If the probe fails, it falls back to
    Bedrock.

    Returns
    -------
    (model, provider_name)
        The working chat model and the provider that ultimately succeeded.
    """
    provider = (provider or settings.llm_provider or "bedrock").strip().lower()

    # Bedrock is the ultimate fallback — no need to probe it.
    if provider == "bedrock":
        return (build_chat_model(model=model, provider=provider, **kwargs), provider)

    # Probe the primary provider with a short timeout.
    try:
        from src.chandra.llm.providers import GenerationParams, get_provider

        test_llm = get_provider(
            provider,
            model=model,
            params=GenerationParams(max_tokens=4, timeout_s=5, max_retries=0),
        )
        if test_llm.health_check():
            logger.info(
                "LLM fallback: primary provider '%s' is healthy, using it.",
                provider,
            )
            return (build_chat_model(model=model, provider=provider, **kwargs), provider)

        logger.warning(
            "LLM fallback: primary provider '%s' health check failed, falling back to Bedrock.",
            provider,
        )
    except Exception as exc:
        logger.warning(
            "LLM fallback: primary provider '%s' probe failed (%s: %s). Falling back to Bedrock.",
            provider,
            type(exc).__name__,
            exc,
        )

    # Fallback to Bedrock
    try:
        return (
            build_chat_model(model=settings.bedrock_model_id, provider="bedrock", **kwargs),
            "bedrock",
        )
    except Exception as fallback_exc:
        logger.error("LLM fallback: Bedrock also failed: %s", fallback_exc)
        raise
