"""Memory tiers: exact fingerprint, semantic vector search, cache (PRD §26.6)."""

from src.chandra.memory.embeddings import (
    BedrockEmbeddings,
    EmbeddingProvider,
    FallbackEmbeddings,
    LocalHashingEmbeddings,
    build_embedding_provider,
)
from src.chandra.memory.semantic import (
    DEFAULT_SIMILARITY_THRESHOLD,
    FAISS_AVAILABLE,
    SemanticHit,
    SemanticMemoryIndex,
)

__all__ = [
    "DEFAULT_SIMILARITY_THRESHOLD",
    "FAISS_AVAILABLE",
    "BedrockEmbeddings",
    "EmbeddingProvider",
    "FallbackEmbeddings",
    "LocalHashingEmbeddings",
    "SemanticHit",
    "SemanticMemoryIndex",
    "build_embedding_provider",
]
