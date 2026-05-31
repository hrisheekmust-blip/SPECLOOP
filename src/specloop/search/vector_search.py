"""Application 1: compositional search via vector arithmetic.

Find combinations of library modules whose functional vectors sum closest to a
request vector — no LLM decomposition. Every module's functional embedding and
PPA vector are already stored in Qdrant (see ``indexer.index_pair``); this module
treats them as geometry: enumerate combinations, add tip-to-tail, rank by distance.

Purely geometric — zero LLM calls.
"""
from __future__ import annotations

import itertools
import logging
import random
from typing import Optional

import numpy as np
from pydantic import BaseModel

from specloop.ppa.vector import PPAVector, distance, sum_vectors

log = logging.getLogger(__name__)

# Enumeration caps to keep runtime under ~5s (see PERFORMANCE NOTE in spec).
_CAP_SIZE3 = 50   # max modules to enumerate for combinations of size >= 3
_CAP_SIZE4 = 30   # max modules to enumerate for combinations of size 4

_DEFAULT_PPA = PPAVector(latency=0.5, throughput=0.5, area=0.5, power=0.5)


class VectorCompositionResult(BaseModel):
    modules: list[str]                  # module names in this combination
    vectors: list[list[float]]          # their individual functional vectors
    sum_vector: list[float]             # tip-to-tail sum
    distance_to_request: float          # cosine distance to request vector
    ppa_vector: PPAVector               # summed PPA vectors of all components
    ppa_distance_to_target: float       # distance to user's PPA target (0.5 default)


def _client(qdrant_url: str):
    from qdrant_client import QdrantClient
    return QdrantClient(url=qdrant_url)


def retrieve_all_vectors(qdrant_url: str, collection: str) -> dict[str, list[float]]:
    """Retrieve all module functional vectors from Qdrant via the scroll API.

    Returns {module_name: embedding_vector}.
    """
    client = _client(qdrant_url)
    if not client.collection_exists(collection):
        return {}

    out: dict[str, list[float]] = {}
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=collection,
            with_vectors=True,
            with_payload=True,
            limit=100,
            offset=offset,
        )
        for p in points:
            name = (p.payload or {}).get("module_name")
            if name and p.vector is not None:
                out[name] = list(p.vector)
        if offset is None:
            break
    return out


def retrieve_all_ppa_vectors(qdrant_url: str, collection: str) -> dict[str, PPAVector]:
    """Retrieve PPA vectors from Qdrant payload for all indexed modules.

    Returns {module_name: PPAVector}. Falls back to PPAVector(0.5, 0.5, 0.5, 0.5)
    for modules indexed before PPA payloads existed.
    """
    client = _client(qdrant_url)
    if not client.collection_exists(collection):
        return {}

    out: dict[str, PPAVector] = {}
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=collection,
            with_vectors=False,
            with_payload=True,
            limit=100,
            offset=offset,
        )
        for p in points:
            payload = p.payload or {}
            name = payload.get("module_name")
            if not name:
                continue
            if "ppa_latency" in payload:
                out[name] = PPAVector(
                    latency=payload.get("ppa_latency", 0.5),
                    throughput=payload.get("ppa_throughput", 0.5),
                    area=payload.get("ppa_area", 0.5),
                    power=payload.get("ppa_power", 0.5),
                )
            else:
                out[name] = _DEFAULT_PPA.model_copy()
        if offset is None:
            break
    return out


def nearest_assertion_descriptions(
    target_vector: list[float],
    qdrant_url: str,
    collection: str,
    limit: int = 5,
) -> list[dict]:
    """Find the module points nearest to ``target_vector`` and harvest their
    assertion descriptions.

    Qdrant points are modules (not individual assertions), so "nearest assertions"
    is realized by querying the nearest module points and unpacking each module's
    stored ``assertion_summary`` list. Returns up to ``limit`` dicts of
    ``{"assertion": str, "module": str, "distance": float}`` ordered by module
    proximity to the target vector.
    """
    client = _client(qdrant_url)
    if not client.collection_exists(collection):
        return []

    # Pull a few extra module points since each contributes several assertions.
    response = client.query_points(
        collection_name=collection,
        query=list(target_vector),
        limit=max(limit, 5),
        with_payload=True,
    )

    out: list[dict] = []
    for hit in response.points:
        payload = hit.payload or {}
        module = payload.get("module_name", "")
        module_dist = 1.0 - float(hit.score)  # query_points returns cosine similarity
        for assertion in payload.get("assertion_summary", []):
            out.append({"assertion": assertion, "module": module, "distance": module_dist})
            if len(out) >= limit:
                return out
    return out


def cosine_distance(a: list[float], b: list[float]) -> float:
    """1 - cosine_similarity. Range [0, 2]. Lower = more similar."""
    av = np.asarray(a, dtype=np.float64)
    bv = np.asarray(b, dtype=np.float64)
    denom = float(np.linalg.norm(av) * np.linalg.norm(bv))
    if denom == 0.0:
        return 1.0
    return 1.0 - float(np.dot(av, bv) / denom)


def vector_sum(vectors: list[list[float]]) -> list[float]:
    """Element-wise sum of a list of vectors."""
    if not vectors:
        return []
    return np.sum(np.asarray(vectors, dtype=np.float64), axis=0).tolist()


def _cosine_distance_batch(matrix: np.ndarray, query: np.ndarray) -> np.ndarray:
    """Cosine distance from every row of ``matrix`` (M x D) to ``query`` (D,)."""
    row_norms = np.linalg.norm(matrix, axis=1)
    q_norm = np.linalg.norm(query)
    denom = row_norms * q_norm
    sims = np.where(denom == 0.0, 0.0, matrix @ query / np.where(denom == 0.0, 1.0, denom))
    return 1.0 - sims


def _enumeration_cap(size: int, n: int, names: list[str]) -> list[int]:
    """Return the module indices eligible for combinations of the given size.

    Applies the size-dependent caps and samples (with a warning) when the
    library exceeds them, so enumeration stays bounded.
    """
    cap = None
    if size == 4:
        cap = _CAP_SIZE4
    elif size >= 3:
        cap = _CAP_SIZE3

    if cap is not None and n > cap:
        log.warning(
            "Library has %d modules; capping combinations of size %d to a random "
            "sample of %d to bound runtime.",
            n, size, cap,
        )
        return random.sample(range(n), cap)
    return list(range(n))


def search_compositions(
    request: str,
    qdrant_url: str,
    collection: str,
    embed_model: str,
    max_components: int = 4,
    top_k: int = 10,
    ppa_target: Optional[PPAVector] = None,
    ppa_weight: float = 0.4,
) -> list[VectorCompositionResult]:
    """Find the top-k module combinations whose vector sums are closest to the request.

    See module docstring and the project spec for the full algorithm. Vector math
    is done with numpy (no per-element Python loops).
    """
    from specloop.search._embed import embed_query

    vectors = retrieve_all_vectors(qdrant_url, collection)
    if not vectors:
        return []

    ppa_vectors = retrieve_all_ppa_vectors(qdrant_url, collection)

    names = list(vectors.keys())
    n = len(names)
    library = np.asarray([vectors[name] for name in names], dtype=np.float64)  # N x D
    request_vec = np.asarray(embed_query(request, embed_model), dtype=np.float64)

    results: list[VectorCompositionResult] = []

    for size in range(1, min(max_components, n) + 1):
        eligible = _enumeration_cap(size, n, names)
        combos = list(itertools.combinations(eligible, size))
        if not combos:
            continue

        # Batch every combination's tip-to-tail sum into one (M x D) array, then
        # compute all cosine distances to the request in a single vectorized call.
        sum_matrix = np.asarray(
            [library[list(combo)].sum(axis=0) for combo in combos], dtype=np.float64
        )
        func_dists = _cosine_distance_batch(sum_matrix, request_vec)

        for combo, sum_vec, func_dist in zip(combos, sum_matrix, func_dists):
            combo_names = [names[i] for i in combo]
            ppa_combined = sum_vectors(
                [ppa_vectors.get(name, _DEFAULT_PPA) for name in combo_names]
            )
            ppa_dist = distance(ppa_combined, ppa_target) if ppa_target else 0.0

            results.append(VectorCompositionResult(
                modules=combo_names,
                vectors=[vectors[name] for name in combo_names],
                sum_vector=sum_vec.tolist(),
                distance_to_request=float(func_dist),
                ppa_vector=ppa_combined,
                ppa_distance_to_target=float(ppa_dist),
            ))

    def score(r: VectorCompositionResult) -> float:
        if ppa_target is None:
            return r.distance_to_request
        return (1.0 - ppa_weight) * r.distance_to_request + ppa_weight * r.ppa_distance_to_target

    results.sort(key=score)
    return results[:top_k]
