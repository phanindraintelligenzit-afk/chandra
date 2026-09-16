"""Embeddings for semantic memory search (PRD §26.6 step 5).

Two providers, chosen at runtime:

* **Bedrock Titan** when ``CHANDRA_EMBEDDINGS_PROVIDER=bedrock``. Real semantic
  similarity, at the cost of a network call per lookup.
* **A local deterministic hashing vectoriser** otherwise, and whenever Bedrock
  is unreachable.

The local provider is not a toy stand-in that pretends to be semantic: it is
character n-gram hashing, so it generalises over wording, word order, typos and
inflection ("terminate ec2 instance" ≈ "EC2 instances terminated") without
claiming to understand meaning. That is the right default for a cache *fallback*
whose misses cost only an LLM call. It also means the semantic tier works with
no additional infrastructure, offline, and in tests — which matters more for a
governance-critical system than the last few points of recall.

Nothing here is authoritative. A semantic hit returns a *candidate* plan that
still traverses every policy, permission, risk and approval control (§26.6).
"""

from __future__ import annotations

import hashlib
import re
from typing import Protocol

import numpy as np
from src.chandra.config import settings
from src.chandra.logging import get_logger

logger = get_logger(__name__)

LOCAL_DIMENSIONS = 512
_NGRAM = 4
_WORD = re.compile(r"[a-z0-9:_\-]+")


class EmbeddingProvider(Protocol):
    @property
    def dimensions(self) -> int: ...

    def embed(self, text: str) -> np.ndarray: ...


def _normalise(vector: np.ndarray) -> np.ndarray:
    """Unit-length, so an inner product is cosine similarity."""
    norm = float(np.linalg.norm(vector))
    if norm == 0.0:
        return vector
    return (vector / norm).astype(np.float32)


class LocalHashingEmbeddings:
    """Character n-gram hashing vectoriser. Deterministic and dependency-free."""

    def __init__(self, dimensions: int = LOCAL_DIMENSIONS) -> None:
        self._dimensions = dimensions

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed(self, text: str) -> np.ndarray:
        vector = np.zeros(self._dimensions, dtype=np.float32)
        cleaned = " ".join(_WORD.findall(text.lower()))
        if not cleaned:
            return vector

        features: list[str] = _WORD.findall(cleaned)
        padded = f" {cleaned} "
        features += [padded[i : i + _NGRAM] for i in range(len(padded) - _NGRAM + 1)]

        for feature in features:
            digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self._dimensions
            # Signed hashing: the sign bit keeps unrelated features from only ever
            # adding, which would make every long string look similar to every
            # other long string.
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign
        return _normalise(vector)


class BedrockEmbeddings:
    """Amazon Titan text embeddings via boto3."""

    def __init__(self, model_id: str | None = None, dimensions: int = 1024) -> None:
        self.model_id = model_id or settings.embeddings_model_id
        self._dimensions = dimensions

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed(self, text: str) -> np.ndarray:
        import json

        import boto3

        client = boto3.client("bedrock-runtime", region_name=settings.aws_default_region)
        response = client.invoke_model(
            modelId=self.model_id,
            body=json.dumps({"inputText": text, "dimensions": self._dimensions}),
        )
        payload = json.loads(response["body"].read())
        return _normalise(np.asarray(payload["embedding"], dtype=np.float32))


class FallbackEmbeddings:
    """Primary provider with a local fallback.

    A memory lookup must never be a hard dependency on a remote service: the
    worst outcome of an embedding failure is a cache miss and one extra LLM call,
    which is strictly better than failing the request. The fallback is sticky —
    once the primary has failed, later lookups in the same process stop paying
    the timeout until it is reset.
    """

    def __init__(self, primary: EmbeddingProvider, fallback: EmbeddingProvider) -> None:
        self.primary = primary
        self.fallback = fallback
        self.primary_failed = False

    @property
    def dimensions(self) -> int:
        return self.fallback.dimensions if self.primary_failed else self.primary.dimensions

    def embed(self, text: str) -> np.ndarray:
        if not self.primary_failed:
            try:
                return self.primary.embed(text)
            except Exception as exc:
                logger.warning("embeddings.primary_failed_using_local", error=str(exc))
                self.primary_failed = True
        return self.fallback.embed(text)

    def reset(self) -> None:
        self.primary_failed = False


def build_embedding_provider() -> EmbeddingProvider:
    provider = (settings.embeddings_provider or "local").strip().lower()
    if provider == "bedrock":
        return FallbackEmbeddings(BedrockEmbeddings(), LocalHashingEmbeddings())
    return LocalHashingEmbeddings()
