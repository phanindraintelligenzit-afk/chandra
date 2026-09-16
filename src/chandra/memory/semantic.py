"""Semantic memory search (PRD §26.6 step 5: "PostgreSQL, Redis and FAISS").

The lookup order the PRD specifies, and the reason for each tier:

1. **Exact fingerprint match** — an identical classified request. Free, certain.
2. **Semantic match** (this module) — a request that means the same thing in
   different words. Cheap, probabilistic, threshold-gated.
3. **LLM planning** — everything else.

A semantic hit is a *candidate*, never an authorisation. It returns a plan that
then traverses policy, RBAC, risk, Gate 1 and Gate 2 exactly as a freshly
planned one does (§26.6). The threshold therefore trades an unnecessary LLM call
against a plan a human will review anyway — which is why it is deliberately
conservative rather than tuned for recall.

FAISS is used when installed; without it the same search runs as a plain numpy
inner product over the same normalised vectors. Identical results, and the
difference is only speed at scale, so the semantic tier is never silently absent
because of a missing wheel.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from src.chandra.logging import get_logger
from src.chandra.memory.embeddings import EmbeddingProvider, build_embedding_provider

logger = get_logger(__name__)

try:  # pragma: no cover - import-time branch
    import faiss

    FAISS_AVAILABLE = True
except ImportError:  # pragma: no cover
    faiss = None  # type: ignore[assignment]
    FAISS_AVAILABLE = False

DEFAULT_SIMILARITY_THRESHOLD = 0.82


@dataclass(frozen=True)
class SemanticHit:
    fingerprint: str
    score: float
    title: str


class SemanticMemoryIndex:
    """In-memory vector index over resolution memory entries.

    Rebuilt from Postgres rather than persisted: the corpus is small, the vectors
    are cheap to recompute, and a stale index file that disagrees with the
    database is a worse failure than a rebuild. ``add`` and ``search`` are
    deliberately simple so the index can be built in a test without a service.
    """

    def __init__(
        self,
        provider: EmbeddingProvider | None = None,
        threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
        use_faiss: bool | None = None,
    ) -> None:
        self.provider = provider or build_embedding_provider()
        self.threshold = threshold
        self._use_faiss = FAISS_AVAILABLE if use_faiss is None else (use_faiss and FAISS_AVAILABLE)
        self._fingerprints: list[str] = []
        self._titles: list[str] = []
        self._vectors: list[np.ndarray] = []
        self._index: object | None = None
        self._dirty = True

    def __len__(self) -> int:
        return len(self._fingerprints)

    @property
    def backend(self) -> str:
        return "faiss" if self._use_faiss else "numpy"

    def add(self, fingerprint: str, text: str, title: str = "") -> None:
        if not fingerprint or not text.strip():
            return
        if fingerprint in self._fingerprints:
            return
        self._fingerprints.append(fingerprint)
        self._titles.append(title or text[:120])
        self._vectors.append(self.provider.embed(text))
        self._dirty = True

    def build(self) -> None:
        """Materialise the FAISS index. Idempotent and safe to call repeatedly."""
        if not self._dirty or not self._vectors:
            self._dirty = False
            return
        if self._use_faiss:
            matrix = np.vstack(self._vectors).astype(np.float32)
            # Inner product over unit-normalised vectors == cosine similarity.
            index = faiss.IndexFlatIP(matrix.shape[1])
            index.add(matrix)
            self._index = index
        self._dirty = False

    def search(self, text: str, limit: int = 3) -> list[SemanticHit]:
        """Return hits at or above the threshold, best first."""
        if not self._vectors or not text.strip():
            return []
        self.build()
        query = self.provider.embed(text).reshape(1, -1).astype(np.float32)

        if self._use_faiss and self._index is not None:
            scores, indices = self._index.search(query, min(limit, len(self._vectors)))  # type: ignore[attr-defined]
            pairs = [
                (int(i), float(s))
                for s, i in zip(scores[0], indices[0], strict=False)
                if int(i) >= 0
            ]
        else:
            matrix = np.vstack(self._vectors).astype(np.float32)
            similarities = (matrix @ query.T).ravel()
            order = np.argsort(-similarities)[:limit]
            pairs = [(int(i), float(similarities[int(i)])) for i in order]

        return [
            SemanticHit(fingerprint=self._fingerprints[i], score=score, title=self._titles[i])
            for i, score in pairs
            if score >= self.threshold
        ]

    def best_match(self, text: str) -> SemanticHit | None:
        hits = self.search(text, limit=1)
        return hits[0] if hits else None
