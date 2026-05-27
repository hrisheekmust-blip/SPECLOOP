"""Query Qdrant for semantically similar modules."""
from __future__ import annotations

from pydantic import BaseModel


class SearchResult(BaseModel):
    module_name: str
    module_type: str
    score: float
    assertion_count: int
    confidence: float
    assertion_summary: list[str]
    file_path: str
    record_id: str


def search(
    query: str,
    qdrant_url: str,
    collection: str,
    model_name: str,
    top_k: int = 3,
) -> list[SearchResult]:
    """Embed query and return top_k matching modules by cosine similarity."""
    from qdrant_client import QdrantClient
    from specloop.search._embed import embed_query

    client = QdrantClient(url=qdrant_url)
    if not client.collection_exists(collection):
        return []

    query_vec = embed_query(query, model_name)
    response = client.query_points(
        collection_name=collection,
        query=query_vec,
        limit=top_k,
        with_payload=True,
    )

    results: list[SearchResult] = []
    for hit in response.points:
        p = hit.payload or {}
        results.append(SearchResult(
            module_name=p.get("module_name", ""),
            module_type=p.get("module_type", ""),
            score=hit.score,
            assertion_count=p.get("assertion_count", 0),
            confidence=p.get("confidence", 0.0),
            assertion_summary=p.get("assertion_summary", []),
            file_path=p.get("file_path", ""),
            record_id=p.get("record_id", ""),
        ))
    return results
