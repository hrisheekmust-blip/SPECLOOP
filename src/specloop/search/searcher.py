"""Query Qdrant for semantically similar modules."""
from __future__ import annotations

from typing import Optional

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
    ppa_latency: float = 0.5
    ppa_throughput: float = 0.5
    ppa_area: float = 0.5
    ppa_power: float = 0.5
    # Hierarchical per-category assertion vectors (None when the module has no
    # assertions in that category, or was indexed before this upgrade).
    vec_reset: Optional[list[float]] = None
    vec_functional: Optional[list[float]] = None
    vec_safety: Optional[list[float]] = None
    vec_temporal: Optional[list[float]] = None
    vec_interface: Optional[list[float]] = None
    vec_fsm: Optional[list[float]] = None
    # 32-dim structural fingerprint (None for pre-upgrade points).
    structural_fingerprint: Optional[list[float]] = None


def _result_from_payload(p: dict, score: float) -> SearchResult:
    """Build a SearchResult from a Qdrant payload dict and a score."""
    return SearchResult(
        module_name=p.get("module_name", ""),
        module_type=p.get("module_type", ""),
        score=score,
        assertion_count=p.get("assertion_count", 0),
        confidence=p.get("confidence", 0.0),
        assertion_summary=p.get("assertion_summary", []),
        file_path=p.get("file_path", ""),
        record_id=p.get("record_id", ""),
        ppa_latency=p.get("ppa_latency", 0.5),
        ppa_throughput=p.get("ppa_throughput", 0.5),
        ppa_area=p.get("ppa_area", 0.5),
        ppa_power=p.get("ppa_power", 0.5),
        vec_reset=p.get("vec_reset"),
        vec_functional=p.get("vec_functional"),
        vec_safety=p.get("vec_safety"),
        vec_temporal=p.get("vec_temporal"),
        vec_interface=p.get("vec_interface"),
        vec_fsm=p.get("vec_fsm"),
        structural_fingerprint=p.get("structural_fingerprint"),
    )


def search(
    query: str,
    qdrant_url: str,
    collection: str,
    model_name: str,
    top_k: int = 3,
    # Optional structural pre-filters — all default to None (no filtering).
    module_type: Optional[str] = None,           # sequential|combinational|fsm|memory
    has_axi: Optional[bool] = None,
    has_valid_ready: Optional[bool] = None,
    has_wide_ports: Optional[bool] = None,
    min_assertion_count: Optional[int] = None,
    require_reset_assertions: Optional[bool] = None,
) -> list[SearchResult]:
    """Embed query and return top_k matching modules by cosine similarity.

    When any filter is provided, a Qdrant payload filter is applied *before* vector
    similarity so structurally-incompatible modules are excluded. With no filters,
    behavior is identical to before.
    """
    from qdrant_client import QdrantClient
    from qdrant_client.models import Filter, FieldCondition, MatchValue, Range
    from specloop.search._embed import embed_query

    client = QdrantClient(url=qdrant_url)
    if not client.collection_exists(collection):
        return []

    conditions = []
    if module_type is not None:
        conditions.append(FieldCondition(key="module_type", match=MatchValue(value=module_type)))
    if has_axi is not None:
        conditions.append(FieldCondition(key="has_axi", match=MatchValue(value=has_axi)))
    if has_valid_ready is not None:
        conditions.append(FieldCondition(key="has_valid_ready", match=MatchValue(value=has_valid_ready)))
    if has_wide_ports is not None:
        conditions.append(FieldCondition(key="has_wide_ports", match=MatchValue(value=has_wide_ports)))
    if min_assertion_count is not None:
        conditions.append(FieldCondition(key="assertion_count", range=Range(gte=min_assertion_count)))
    if require_reset_assertions is not None:
        conditions.append(FieldCondition(key="has_reset_assertions", match=MatchValue(value=require_reset_assertions)))
    qdrant_filter = Filter(must=conditions) if conditions else None

    query_vec = embed_query(query, model_name)
    response = client.query_points(
        collection_name=collection,
        query=query_vec,
        query_filter=qdrant_filter,
        limit=top_k,
        with_payload=True,
    )

    return [_result_from_payload(hit.payload or {}, hit.score) for hit in response.points]


def _cosine(a, b) -> float:
    """Cosine similarity between two stored vectors (both already L2-normalized)."""
    import numpy as np
    av = np.asarray(a, dtype=np.float64)
    bv = np.asarray(b, dtype=np.float64)
    denom = float(np.linalg.norm(av) * np.linalg.norm(bv))
    if denom == 0.0:
        return 0.0
    return float(np.dot(av, bv) / denom)


# Payload fields needed to build a SearchResult + the assertion centroid. Deliberately
# excludes the bulky per-assertion `assertion_vectors` and `vec_*` / fingerprint lists.
_SCORE_PAYLOAD_FIELDS = [
    "module_name", "module_type", "assertion_count", "confidence",
    "assertion_summary", "file_path", "record_id",
    "ppa_latency", "ppa_throughput", "ppa_area", "ppa_power",
    "assertion_vector",
]


def _scroll_scored(
    query: str,
    qdrant_url: str,
    collection: str,
    model_name: str,
    assertion_weight: float,
    top_k: int,
) -> list[SearchResult]:
    """Score every module by blending composite-doc and assertion-centroid similarity.

    blended = (1-w)*cosine(q, main_vector) + w*cosine(q, assertion_vector); modules
    without an assertion_vector fall back to the composite similarity for both terms.
    """
    from qdrant_client import QdrantClient
    from specloop.search._embed import embed_query

    client = QdrantClient(url=qdrant_url)
    if not client.collection_exists(collection):
        return []

    query_vec = embed_query(query, model_name)

    scored: list[SearchResult] = []
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=collection,
            with_vectors=True,
            with_payload=_SCORE_PAYLOAD_FIELDS,
            limit=100,
            offset=offset,
        )
        for pt in points:
            p = pt.payload or {}
            if pt.vector is None:
                continue
            composite = _cosine(query_vec, pt.vector)
            av = p.get("assertion_vector")
            assertion = _cosine(query_vec, av) if av else composite
            blended = (1.0 - assertion_weight) * composite + assertion_weight * assertion
            scored.append(_result_from_payload(p, blended))
        if offset is None:
            break

    scored.sort(key=lambda r: r.score, reverse=True)
    return scored[:top_k]


def search_by_assertions(
    query: str,
    qdrant_url: str,
    collection: str,
    model_name: str,
    top_k: int = 5,
) -> list[SearchResult]:
    """Search using assertion-centric vectors — finds modules whose *proven behaviors*
    are closest to the query, not whose descriptions are. Falls back to composite
    similarity for modules indexed before assertion vectors existed."""
    return _scroll_scored(query, qdrant_url, collection, model_name, 1.0, top_k)


def search_blended(
    query: str,
    qdrant_url: str,
    collection: str,
    model_name: str,
    top_k: int = 5,
    assertion_weight: float = 0.6,
) -> list[SearchResult]:
    """Blend composite-document and assertion-centric similarity. assertion_weight
    (default 0.6) lets proven behavior drive the ranking more than descriptions."""
    return _scroll_scored(query, qdrant_url, collection, model_name, assertion_weight, top_k)
