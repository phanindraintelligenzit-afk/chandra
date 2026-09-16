"""Semantic memory tier and optional Redis cache (PRD §26.6)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest
from src.chandra.memory import (
    FAISS_AVAILABLE,
    FallbackEmbeddings,
    LocalHashingEmbeddings,
    SemanticMemoryIndex,
)
from src.chandra.memory.cache import CacheClient


class TestLocalEmbeddings:
    def test_deterministic(self) -> None:
        provider = LocalHashingEmbeddings()
        assert np.allclose(provider.embed("terminate ec2"), provider.embed("terminate ec2"))

    def test_unit_length(self) -> None:
        vector = LocalHashingEmbeddings().embed("block public access on s3 bucket")
        assert pytest.approx(float(np.linalg.norm(vector)), abs=1e-5) == 1.0

    def test_empty_text_is_the_zero_vector(self) -> None:
        assert float(np.linalg.norm(LocalHashingEmbeddings().embed("   "))) == 0.0

    def test_similar_phrasings_score_higher_than_unrelated_ones(self) -> None:
        provider = LocalHashingEmbeddings()
        base = provider.embed("public s3 bucket needs block public access")
        similar = provider.embed("s3 bucket is public, block public access")
        unrelated = provider.embed("increase rds instance storage in eu-west-1")
        assert float(base @ similar) > float(base @ unrelated)

    def test_signed_hashing_keeps_long_unrelated_strings_apart(self) -> None:
        """Unsigned hashing makes every long string resemble every other one,
        because features only ever add."""
        provider = LocalHashingEmbeddings()
        a = provider.embed(" ".join(["alpha beta gamma delta"] * 30))
        b = provider.embed(" ".join(["zulu yankee xray whiskey"] * 30))
        assert float(a @ b) < 0.5


class TestFallbackEmbeddings:
    def test_falls_back_when_primary_raises(self) -> None:
        class Broken:
            dimensions = 8

            def embed(self, text: str) -> np.ndarray:
                raise RuntimeError("bedrock unreachable")

        provider = FallbackEmbeddings(Broken(), LocalHashingEmbeddings())
        vector = provider.embed("anything")
        assert vector.shape[0] == LocalHashingEmbeddings().dimensions
        assert provider.primary_failed is True

    def test_fallback_is_sticky_until_reset(self) -> None:
        calls: list[str] = []

        class Flaky:
            dimensions = 8

            def embed(self, text: str) -> np.ndarray:
                calls.append(text)
                raise RuntimeError("down")

        provider = FallbackEmbeddings(Flaky(), LocalHashingEmbeddings())
        provider.embed("one")
        provider.embed("two")
        assert calls == ["one"]  # the second lookup did not pay the timeout again
        provider.reset()
        provider.embed("three")
        assert calls == ["one", "three"]


class TestSemanticIndex:
    def _index(self, **kw: Any) -> SemanticMemoryIndex:
        index = SemanticMemoryIndex(provider=LocalHashingEmbeddings(), **kw)
        index.add("fp-s3", "public s3 bucket block public access security aws", title="Public S3")
        index.add("fp-rds", "increase rds storage capacity reliability aws", title="RDS storage")
        index.add("fp-cost", "idle ec2 instances cost optimization aws", title="Idle EC2")
        index.build()
        return index

    def test_finds_a_reworded_request(self) -> None:
        index = self._index(threshold=0.3)
        hit = index.best_match("s3 bucket publicly accessible, block public access aws security")
        assert hit is not None
        assert hit.fingerprint == "fp-s3"

    def test_returns_nothing_below_threshold(self) -> None:
        index = self._index(threshold=0.99)
        assert index.best_match("something entirely unrelated to the corpus") is None

    def test_threshold_gate_prefers_a_miss_over_a_bad_match(self) -> None:
        """A wrong plan costs a human review cycle; a miss costs one LLM call."""
        index = self._index()
        assert index.best_match("configure dns zone delegation for the marketing domain") is None

    def test_empty_index_and_empty_query_are_safe(self) -> None:
        empty = SemanticMemoryIndex(provider=LocalHashingEmbeddings())
        assert empty.best_match("anything") is None
        assert self._index().best_match("   ") is None

    def test_duplicate_fingerprints_are_ignored(self) -> None:
        index = self._index()
        before = len(index)
        index.add("fp-s3", "public s3 bucket block public access security aws")
        assert len(index) == before

    def test_blank_entries_are_not_indexed(self) -> None:
        index = SemanticMemoryIndex(provider=LocalHashingEmbeddings())
        index.add("", "text")
        index.add("fp", "   ")
        assert len(index) == 0

    @pytest.mark.skipif(not FAISS_AVAILABLE, reason="faiss not installed")
    def test_faiss_and_numpy_backends_agree(self) -> None:
        """The backend is a speed choice, not a behaviour change."""
        query = "s3 bucket publicly accessible, block public access aws security"

        def build(use_faiss: bool) -> SemanticMemoryIndex:
            index = SemanticMemoryIndex(
                provider=LocalHashingEmbeddings(), threshold=0.3, use_faiss=use_faiss
            )
            for fp, text in (
                ("fp-s3", "public s3 bucket block public access security aws"),
                ("fp-rds", "increase rds storage capacity reliability aws"),
            ):
                index.add(fp, text)
            index.build()
            return index

        faiss_index, numpy_index = build(True), build(False)
        assert faiss_index.backend == "faiss"
        assert numpy_index.backend == "numpy"
        a, b = faiss_index.best_match(query), numpy_index.best_match(query)
        assert a is not None and b is not None
        assert a.fingerprint == b.fingerprint
        assert pytest.approx(a.score, abs=1e-4) == b.score


class TestCacheIsOptional:
    def test_disabled_cache_is_a_silent_no_op(self) -> None:
        """With REDIS_URL unset nothing raises and nothing is cached — Chandra
        must behave identically without Redis."""
        cache = CacheClient(url="")
        assert cache.enabled is False
        assert cache.get_json("default", "k") is None
        assert cache.set_json("default", "k", {"a": 1}) is False
        assert cache.invalidate("default", "k") is False
        assert cache.append_log("default", "job", "line") is False
        assert cache.read_logs("default", "job") == []
        assert cache.health() == "disabled"

    def test_unreachable_redis_degrades_instead_of_raising(self) -> None:
        """An unavailable cache must never fail a request."""
        cache = CacheClient(url="redis://127.0.0.1:1/0")
        assert cache.get_json("default", "k") is None
        assert cache.set_json("default", "k", {"a": 1}) is False
        assert cache.health() == "unavailable"
        # and it stops retrying the dead connection on every call
        assert cache.enabled is False

    def test_namespacing_is_per_tenant(self) -> None:
        cache = CacheClient(url="")
        assert cache._key("acme", "cache", "k") != cache._key("other", "cache", "k")
