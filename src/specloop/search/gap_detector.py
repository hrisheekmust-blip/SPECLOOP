"""Application 2: gap detection.

When the best module combination still doesn't sum close to the request vector,
the residual ``R - sum(combination)`` points toward what the library is missing.
Characterize that residual geometrically — no LLM calls.
"""
from __future__ import annotations

import numpy as np
from pydantic import BaseModel

from specloop.search.vector_search import (
    VectorCompositionResult,
    cosine_distance,
    nearest_assertion_descriptions,
    retrieve_all_vectors,
)


class GapReport(BaseModel):
    residual_vector: list[float]            # R - sum(best_combination)
    gap_magnitude: float                    # ||residual||
    nearest_assertions: list[str]           # assertion descriptions near the gap
    gap_description: str                    # natural language description of what's missing
    suggested_modules_to_build: list[str]   # closest existing modules to the gap
    suggested_index_commands: list[str]     # spec/index commands to add them


def detect_gap(
    request_vector: list[float],
    best_combination: VectorCompositionResult,
    qdrant_url: str,
    collection: str,
    embed_model: str,
    top_k_assertions: int = 5,
) -> GapReport:
    """Compute the residual vector (what's missing) and characterize it geometrically."""
    request = np.asarray(request_vector, dtype=np.float64)
    sum_vec = np.asarray(best_combination.sum_vector, dtype=np.float64)
    # Project both onto the unit sphere first: the tip-to-tail sum of several unit
    # vectors has inflated magnitude, which would otherwise dominate the residual
    # and make it point away from the request rather than toward what's missing.
    request_u = request / (np.linalg.norm(request) or 1.0)
    sum_u = sum_vec / (np.linalg.norm(sum_vec) or 1.0)
    residual = request_u - sum_u
    magnitude = float(np.linalg.norm(residual))

    if magnitude < 0.1:
        return GapReport(
            residual_vector=residual.tolist(),
            gap_magnitude=magnitude,
            nearest_assertions=[],
            gap_description="Gap is negligible — the selected combination covers the request.",
            suggested_modules_to_build=[],
            suggested_index_commands=[],
        )

    # Assertion descriptions whose behavioral region is nearest the residual.
    nearest = nearest_assertion_descriptions(
        residual.tolist(), qdrant_url, collection, limit=top_k_assertions
    )
    nearest_assertions = [n["assertion"] for n in nearest]

    if nearest_assertions:
        gap_description = (
            "Your library may be missing a module that: "
            + "; ".join(nearest_assertions)
        )
    else:
        gap_description = "Your library has a behavioral gap with no nearby assertions to describe it."

    # Existing modules whose individual vectors are closest to the residual direction —
    # likely variants/relatives of what's actually needed.
    all_vectors = retrieve_all_vectors(qdrant_url, collection)
    existing = best_combination.modules
    ranked = sorted(
        (m for m in all_vectors if m not in existing),
        key=lambda m: cosine_distance(all_vectors[m], residual.tolist()),
    )
    suggested = ranked[:3]
    suggested_index_commands = [f"specloop spec {m} && specloop index {m}" for m in suggested]

    return GapReport(
        residual_vector=residual.tolist(),
        gap_magnitude=magnitude,
        nearest_assertions=nearest_assertions,
        gap_description=gap_description,
        suggested_modules_to_build=suggested,
        suggested_index_commands=suggested_index_commands,
    )


def format_gap_report(report: GapReport) -> str:
    """Format GapReport as human-readable text for CLI display."""
    lines = [
        f"Gap magnitude: {report.gap_magnitude:.3f}",
        report.gap_description,
    ]
    if report.nearest_assertions:
        lines.append("Nearest behaviors:")
        lines += [f"  - {a}" for a in report.nearest_assertions]
    if report.suggested_modules_to_build:
        lines.append("Closest existing modules (possible variants to build/index):")
        lines += [f"  - {m}" for m in report.suggested_modules_to_build]
    if report.suggested_index_commands:
        lines.append("Suggested commands:")
        lines += [f"  {c}" for c in report.suggested_index_commands]
    return "\n".join(lines)
