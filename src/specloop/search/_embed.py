"""Lazy-cached sentence-transformers embedding helpers."""
from __future__ import annotations

from sentence_transformers import SentenceTransformer

_MODELS: dict[str, SentenceTransformer] = {}

# BGE-large requires this prefix on queries (not on documents) for asymmetric retrieval.
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


def _get_model(model_name: str) -> SentenceTransformer:
    if model_name not in _MODELS:
        _MODELS[model_name] = SentenceTransformer(model_name)
    return _MODELS[model_name]


def embed_document(text: str, model_name: str) -> list[float]:
    """Embed a document passage. No query prefix."""
    return _get_model(model_name).encode(text, normalize_embeddings=True).tolist()


def embed_query(text: str, model_name: str) -> list[float]:
    """Embed a search query with the BGE retrieval prefix."""
    return _get_model(model_name).encode(QUERY_PREFIX + text, normalize_embeddings=True).tolist()
