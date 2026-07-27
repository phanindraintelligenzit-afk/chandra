"""LLM provider abstraction layer.

Provides a single ``get_llm()`` factory that returns a LangChain-compatible
chat model backed by one of several providers:

- **Amazon Bedrock** (``langchain_aws.ChatBedrockConverse``) — the original
  Chandra runtime, used when ``LLM_PROVIDER=bedrock`` (default).
- **OpenAI-compatible** (``langchain_openai.ChatOpenAI``) — any server that
  speaks the OpenAI chat completions API, including **vLLM**, **Ollama**,
  **llama.cpp**, **TGI**, and **OpenAI itself**.
- **Ollama native** (``langchain_ollama.ChatOllama``) — direct Ollama
  integration when ``LLM_PROVIDER=ollama``.

Usage::

    from src.chandra.llm import get_llm

    llm = get_llm()                          # respects env vars
    llm = get_llm(temperature=0.0)            # override temperature
    llm = get_llm(model="qwen2.5-coder:32b")  # override model name
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

from src.chandra.config import settings
from src.chandra.logging import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_llm(
    *,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    **kwargs: Any,
) -> Any:
    """Return a LangChain chat model for the configured LLM provider.

    Parameters
    ----------
    model:
        Override the model name. Uses ``settings.llm_model`` when omitted.
    temperature:
        Sampling temperature. Uses ``settings.llm_temperature`` when omitted.
    max_tokens:
        Max output tokens. Uses ``settings.llm_max_tokens`` when omitted.
    **kwargs:
        Additional keyword arguments passed to the provider constructor.

    Returns
    -------
    A LangChain :class:`BaseChatModel` instance — either
    ``ChatBedrockConverse``, ``ChatOpenAI``, or ``ChatOllama`` depending on
    ``settings.llm_provider``.

    Raises
    ------
    ValueError
        When ``LLM_PROVIDER`` is set to an unknown value.
    """
    provider = settings.llm_provider
    model_name = model or settings.llm_model
    temp = temperature if temperature is not None else settings.llm_temperature
    tokens = max_tokens if max_tokens is not None else settings.llm_max_tokens

    logger.info(
        "llm.get_llm",
        provider=provider,
        model=model_name,
        temperature=temp,
        max_tokens=tokens,
    )

    if provider == "bedrock":
        return _build_bedrock_llm(model_name, temp, tokens, **kwargs)
    elif provider == "openai":
        return _build_openai_llm(model_name, temp, tokens, **kwargs)
    elif provider == "ollama":
        return _build_ollama_llm(model_name, temp, tokens, **kwargs)
    else:
        raise ValueError(f"Unknown LLM_PROVIDER={provider!r}. Supported: bedrock, openai, ollama")


def get_llm_with_tools(
    tools: list[Any],
    *,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    **kwargs: Any,
) -> Any:
    """Return a LangChain chat model with ``tools`` bound to it.

    This is a convenience wrapper around ``get_llm().bind_tools(tools)``
    used by the copilot agent and other tool-calling surfaces.
    """
    llm = get_llm(model=model, temperature=temperature, max_tokens=max_tokens, **kwargs)
    return llm.bind_tools(tools)


# ---------------------------------------------------------------------------
# Provider implementations
# ---------------------------------------------------------------------------


def _build_bedrock_llm(
    model: str,
    temperature: float,
    max_tokens: int | None,
    **kwargs: Any,
) -> Any:
    """Build a ``ChatBedrockConverse`` instance."""
    try:
        from langchain_aws import ChatBedrockConverse
    except ImportError as exc:
        raise ImportError(
            "langchain-aws is required for LLM_PROVIDER=bedrock. "
            "Install it with: pip install langchain-aws"
        ) from exc

    logger.info("llm.provider_bedrock", model=model, region=settings.aws_default_region)
    return ChatBedrockConverse(
        model_id=model,
        region_name=settings.aws_default_region,
        temperature=temperature,
        max_tokens=max_tokens or 2048,
        **kwargs,
    )


def _build_openai_llm(
    model: str,
    temperature: float,
    max_tokens: int | None,
    **kwargs: Any,
) -> Any:
    """Build a ``ChatOpenAI`` instance pointed at an OpenAI-compatible server.

    Reads ``OPENAI_API_BASE`` and ``OPENAI_API_KEY`` from the environment
    (via ``settings``), which means it works with:

    * A local **vLLM** server: ``http://<host>:8000/v1``
    * **Ollama** (OpenAI-compatible mode): ``http://<host>:11434/v1``
    * **llama.cpp** server: ``http://<host>:8080/v1``
    * **OpenAI** itself: ``https://api.openai.com/v1``
    * Any other OpenAI-compatible endpoint
    """
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:
        raise ImportError(
            "langchain-openai is required for LLM_PROVIDER=openai. "
            "Install it with: pip install langchain-openai"
        ) from exc

    base_url = settings.openai_api_base or os.getenv("OPENAI_API_BASE", "http://localhost:8000/v1")
    api_key = settings.openai_api_key or os.getenv("OPENAI_API_KEY", "not-needed-local")

    logger.info("llm.provider_openai", model=model, base_url=base_url)
    return ChatOpenAI(
        model=model,
        base_url=base_url,
        api_key=api_key,
        temperature=temperature,
        max_tokens=max_tokens or 4096,
        **kwargs,
    )


def _build_ollama_llm(
    model: str,
    temperature: float,
    max_tokens: int | None,
    **kwargs: Any,
) -> Any:
    """Build a ``ChatOllama`` instance for direct Ollama integration."""
    try:
        from langchain_ollama import ChatOllama
    except ImportError as exc:
        raise ImportError(
            "langchain-ollama is required for LLM_PROVIDER=ollama. "
            "Install it with: pip install langchain-ollama"
        ) from exc

    base_url = settings.ollama_base_url or os.getenv("OLLAMA_HOST", "http://localhost:11434")

    logger.info("llm.provider_ollama", model=model, base_url=base_url)
    return ChatOllama(
        model=model,
        base_url=base_url,
        temperature=temperature,
        num_predict=max_tokens or 4096,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Cached convenience
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def get_cached_llm(**kwargs: Any) -> Any:
    """Return a cached LLM instance (singleton per process).

    Use this for agents that keep one LLM for the lifetime of the process
    (e.g. the copilot agent, the digital worker). The cache is invalidated
    on process restart, so a config change (via env var) requires a restart.
    """
    return get_llm(**kwargs)
