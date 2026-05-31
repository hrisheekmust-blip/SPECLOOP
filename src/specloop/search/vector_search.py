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
    structural_diversity: Optional[float] = None  # mean pairwise structural distance


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


_CATEGORIES = ["reset", "functional", "safety", "temporal", "interface", "fsm"]


def retrieve_all_assertion_vectors(qdrant_url: str, collection: str) -> dict[str, list[float]]:
    """Retrieve assertion-centric (centroid) vectors from payload for all modules.

    Returns {module_name: assertion_vector}, omitting modules indexed before this
    field existed (or with no assertions).
    """
    client = _client(qdrant_url)
    if not client.collection_exists(collection):
        return {}

    out: dict[str, list[float]] = {}
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=collection,
            with_vectors=False,
            with_payload=["module_name", "assertion_vector"],
            limit=100,
            offset=offset,
        )
        for p in points:
            payload = p.payload or {}
            name = payload.get("module_name")
            av = payload.get("assertion_vector")
            if name and av is not None:
                out[name] = av
        if offset is None:
            break
    return out


def retrieve_all_category_vectors(
    qdrant_url: str, collection: str
) -> dict[str, dict[str, list[float]]]:
    """Retrieve per-category assertion vectors from payload for all indexed modules.

    Returns {module_name: {category: vector}} containing only the categories that
    actually have a (non-null) vector. Modules indexed before this upgrade simply
    yield an empty inner dict.
    """
    client = _client(qdrant_url)
    if not client.collection_exists(collection):
        return {}

    out: dict[str, dict[str, list[float]]] = {}
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
            cats = {
                cat: payload[f"vec_{cat}"]
                for cat in _CATEGORIES
                if payload.get(f"vec_{cat}") is not None
            }
            out[name] = cats
        if offset is None:
            break
    return out


def retrieve_all_structural_fingerprints(
    qdrant_url: str, collection: str
) -> dict[str, list[float]]:
    """Retrieve 32-dim structural fingerprints from payload for all indexed modules.

    Modules indexed before this upgrade are omitted (no fingerprint payload).
    """
    client = _client(qdrant_url)
    if not client.collection_exists(collection):
        return {}

    out: dict[str, list[float]] = {}
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
            fp = payload.get("structural_fingerprint")
            if name and fp is not None:
                out[name] = fp
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
    structural_weight: float = 0.0,
    use_assertion_vectors: bool = True,
) -> list[VectorCompositionResult]:
    """Find the top-k module combinations whose vector sums are closest to the request.

    See module docstring and the project spec for the full algorithm. Vector math
    is done with numpy (no per-element Python loops).

    ``structural_weight`` (>0) rewards architecturally *diverse* combinations: the
    mean pairwise structural-fingerprint distance is blended into the score so that
    mixing implementation styles (e.g. a pipelined + a combinational module) ranks
    above two near-identical modules — the non-obvious combinations.

    ``use_assertion_vectors`` (default True): when assertion-centric vectors are
    available, the functional distance is computed from those (proven behavior)
    instead of the composite-document vectors, falling back per-module to the main
    vector where an assertion vector is missing.
    """
    from specloop.search._embed import embed_query

    vectors = retrieve_all_vectors(qdrant_url, collection)
    if not vectors:
        return []

    # Choose the matching space: assertion-centric (proven behavior) when available,
    # else composite document vectors. Per-module fallback keeps old points working.
    if use_assertion_vectors:
        assertion_vectors = retrieve_all_assertion_vectors(qdrant_url, collection)
        if assertion_vectors:
            vectors = {name: assertion_vectors.get(name, vec) for name, vec in vectors.items()}

    ppa_vectors = retrieve_all_ppa_vectors(qdrant_url, collection)
    fingerprints = (
        retrieve_all_structural_fingerprints(qdrant_url, collection)
        if structural_weight > 0
        else {}
    )

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

            diversity = None
            if structural_weight > 0:
                diversity = _mean_pairwise_structural_distance(combo_names, fingerprints)

            results.append(VectorCompositionResult(
                modules=combo_names,
                vectors=[vectors[name] for name in combo_names],
                sum_vector=sum_vec.tolist(),
                distance_to_request=float(func_dist),
                ppa_vector=ppa_combined,
                ppa_distance_to_target=float(ppa_dist),
                structural_diversity=diversity,
            ))

    sqrt32 = 32 ** 0.5

    def score(r: VectorCompositionResult) -> float:
        # Generalized blend. With ppa_target=None and structural_weight=0 this
        # reduces exactly to distance_to_request (unchanged default behavior).
        func_w = 1.0
        ppa_term = 0.0
        struct_term = 0.0
        if ppa_target is not None:
            ppa_term = ppa_weight * r.ppa_distance_to_target
            func_w -= ppa_weight
        if structural_weight > 0:
            div_norm = (r.structural_diversity or 0.0) / sqrt32
            struct_term = structural_weight * (1.0 - div_norm)  # more diverse = lower
            func_w -= structural_weight
        return func_w * r.distance_to_request + ppa_term + struct_term

    results.sort(key=score)
    return results[:top_k]


def _mean_pairwise_structural_distance(
    module_names: list[str], fingerprints: dict[str, list[float]]
) -> float:
    """Mean pairwise structural-fingerprint distance across a combination.

    0.0 for fewer than two modules (no pairs) or when fingerprints are missing.
    """
    from specloop.search.structural import structural_distance

    fps = [fingerprints[m] for m in module_names if m in fingerprints]
    if len(fps) < 2:
        return 0.0
    pairs = list(itertools.combinations(fps, 2))
    return sum(structural_distance(a, b) for a, b in pairs) / len(pairs)


def find_structurally_diverse_compositions(
    request: str,
    qdrant_url: str,
    collection: str,
    embed_model: str,
    max_components: int = 4,
    top_k: int = 10,
) -> list[VectorCompositionResult]:
    """Find compositions that are both functionally relevant AND structurally diverse.

    Surfaces non-obvious combinations — modules that implement things in different
    ways but together cover the requested behavior. Thin wrapper over
    ``search_compositions`` with ``structural_weight=0.3``.
    """
    return search_compositions(
        request=request,
        qdrant_url=qdrant_url,
        collection=collection,
        embed_model=embed_model,
        max_components=max_components,
        top_k=top_k,
        structural_weight=0.3,
    )


# Category-specific query prefixes steer the request embedding toward each facet.
_CATEGORY_PREFIX = {
    "reset": "reset behavior: ",
    "functional": "functional behavior: ",
    "safety": "safety properties: ",
    "temporal": "temporal properties: ",
    "interface": "interface protocol: ",
    "fsm": "FSM states: ",
}


def search_compositions_hierarchical(
    request: str,
    qdrant_url: str,
    collection: str,
    embed_model: str,
    category_weights: Optional[dict[str, float]] = None,
    max_components: int = 4,
    top_k: int = 10,
    ppa_target: Optional[PPAVector] = None,
    ppa_weight: float = 0.4,
) -> list[VectorCompositionResult]:
    """Composition search using hierarchical category vectors for finer matching.

    ``category_weights`` controls each assertion category's contribution (default:
    all six equal at 1.0). For a category to count toward a combination, *every*
    member module must have that category vector; the summed category vectors are
    compared to the request's category-specific embedding. The top-level functional
    distance is always included (implicit weight 1.0), so a combination with no
    shared categories degrades gracefully to the flat functional score.

    Falls back to :func:`search_compositions` when no module has category vectors
    (e.g. a library indexed before this upgrade).
    """
    from specloop.search._embed import embed_query

    category_vectors = retrieve_all_category_vectors(qdrant_url, collection)
    if not any(category_vectors.values()):
        return search_compositions(
            request, qdrant_url, collection, embed_model,
            max_components=max_components, top_k=top_k,
            ppa_target=ppa_target, ppa_weight=ppa_weight,
        )

    vectors = retrieve_all_vectors(qdrant_url, collection)
    if not vectors:
        return []
    ppa_vectors = retrieve_all_ppa_vectors(qdrant_url, collection)

    if category_weights is None:
        category_weights = {cat: 1.0 for cat in _CATEGORIES}

    names = list(vectors.keys())
    n = len(names)
    library = np.asarray([vectors[name] for name in names], dtype=np.float64)
    request_vec = np.asarray(embed_query(request, embed_model), dtype=np.float64)
    # Pre-embed the request once per category facet.
    request_cat_vecs = {
        cat: np.asarray(embed_query(_CATEGORY_PREFIX[cat] + request, embed_model), dtype=np.float64)
        for cat in _CATEGORIES
    }

    scored: list[tuple[float, VectorCompositionResult]] = []

    for size in range(1, min(max_components, n) + 1):
        eligible = _enumeration_cap(size, n, names)
        combos = list(itertools.combinations(eligible, size))
        if not combos:
            continue

        sum_matrix = np.asarray(
            [library[list(combo)].sum(axis=0) for combo in combos], dtype=np.float64
        )
        func_dists = _cosine_distance_batch(sum_matrix, request_vec)

        for combo, sum_vec, func_dist in zip(combos, sum_matrix, func_dists):
            combo_names = [names[i] for i in combo]

            # Top level always contributes (weight 1.0); add categories shared by all.
            weighted_sum = float(func_dist)
            weight_total = 1.0
            for cat in _CATEGORIES:
                cat_vecs = [category_vectors.get(m, {}).get(cat) for m in combo_names]
                if any(v is None for v in cat_vecs):
                    continue
                cat_sum = np.sum(np.asarray(cat_vecs, dtype=np.float64), axis=0)
                cat_dist = float(_cosine_distance_batch(
                    cat_sum.reshape(1, -1), request_cat_vecs[cat]
                )[0])
                w = category_weights.get(cat, 1.0)
                weighted_sum += w * cat_dist
                weight_total += w
            weighted_category_score = weighted_sum / weight_total

            ppa_combined = sum_vectors(
                [ppa_vectors.get(name, _DEFAULT_PPA) for name in combo_names]
            )
            ppa_dist = distance(ppa_combined, ppa_target) if ppa_target else 0.0

            if ppa_target is not None:
                final = (1.0 - ppa_weight) * weighted_category_score + ppa_weight * ppa_dist
            else:
                final = weighted_category_score

            scored.append((final, VectorCompositionResult(
                modules=combo_names,
                vectors=[vectors[name] for name in combo_names],
                sum_vector=sum_vec.tolist(),
                distance_to_request=float(func_dist),
                ppa_vector=ppa_combined,
                ppa_distance_to_target=float(ppa_dist),
            )))

    scored.sort(key=lambda x: x[0])
    return [r for _, r in scored[:top_k]]
