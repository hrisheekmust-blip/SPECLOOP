"""Application 3: interaction-term assertion targeting.

When two modules are composed, their individual proofs are free — but interaction
properties (behavior that only emerges from the connection) are covered by neither.
The gap between the composition's behavioral vector and the sum of component
assertion vectors points toward exactly that under-covered region.

Purely geometric — zero LLM calls. The output is meant to *guide* (as few-shot
examples) the existing LLM assertion generator, not replace it.
"""
from __future__ import annotations

import numpy as np
from pydantic import BaseModel

from specloop.search.vector_search import nearest_assertion_descriptions


class InteractionGap(BaseModel):
    gap_vector: list[float]          # composition_vector - sum(component_assertion_vectors)
    gap_magnitude: float
    nearest_examples: list[dict]     # {"assertion": str, "module": str, "distance": float}
    coverage_percentage: float       # fraction of composition behavior covered by components


def _module_payloads(qdrant_url: str, collection: str) -> dict[str, list[str]]:
    """Return {module_name: assertion_summary} for all indexed modules."""
    from qdrant_client import QdrantClient

    client = QdrantClient(url=qdrant_url, check_compatibility=False)
    if not client.collection_exists(collection):
        return {}

    out: dict[str, list[str]] = {}
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
            if name:
                out[name] = payload.get("assertion_summary", [])
        if offset is None:
            break
    return out


def compute_assertion_vectors(
    module_names: list[str],
    qdrant_url: str,
    collection: str,
    embed_model: str,
) -> dict[str, list[list[float]]]:
    """For each module, embed its stored assertion descriptions.

    Returns {module_name: [vector_per_assertion]}.
    """
    from specloop.search._embed import embed_document

    summaries = _module_payloads(qdrant_url, collection)
    out: dict[str, list[list[float]]] = {}
    for name in module_names:
        out[name] = [embed_document(a, embed_model) for a in summaries.get(name, [])]
    return out


def detect_interaction_gap(
    composition_request: str,
    component_modules: list[str],
    qdrant_url: str,
    collection: str,
    embed_model: str,
) -> InteractionGap:
    """Identify behavioral properties that emerge from the composition but aren't
    covered by either component module's assertions."""
    from specloop.search._embed import embed_query

    composition_vec = np.asarray(embed_query(composition_request, embed_model), dtype=np.float64)

    assertion_vectors = compute_assertion_vectors(
        component_modules, qdrant_url, collection, embed_model
    )
    all_vecs = [v for vecs in assertion_vectors.values() for v in vecs]

    if all_vecs:
        covered_sum = np.sum(np.asarray(all_vecs, dtype=np.float64), axis=0)
        # Project onto the unit sphere: summing many unit assertion vectors inflates
        # the magnitude, which would otherwise force coverage to clamp at 0 whenever
        # a module has more than one assertion. We care about direction here.
        covered = covered_sum / (np.linalg.norm(covered_sum) or 1.0)
    else:
        covered = np.zeros_like(composition_vec)

    composition_unit = composition_vec / (np.linalg.norm(composition_vec) or 1.0)
    gap = composition_unit - covered
    gap_magnitude = float(np.linalg.norm(gap))
    coverage = max(0.0, min(1.0, 1.0 - gap_magnitude))

    nearest_examples = nearest_assertion_descriptions(
        gap.tolist(), qdrant_url, collection, limit=5
    )

    return InteractionGap(
        gap_vector=gap.tolist(),
        gap_magnitude=gap_magnitude,
        nearest_examples=nearest_examples,
        coverage_percentage=coverage,
    )


def get_interaction_few_shot_examples(gap: InteractionGap, top_k: int = 3) -> list[dict]:
    """Extract the top_k most relevant assertion examples as few-shot dicts."""
    return [
        {"assertion_text": ex["assertion"], "module": ex["module"]}
        for ex in gap.nearest_examples[:top_k]
    ]
