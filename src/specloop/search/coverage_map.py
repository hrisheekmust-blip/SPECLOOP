"""Application 4: library coverage map.

Cluster the library's functional vectors to reveal which behavioral domains are
densely covered versus sparse. Purely geometric — zero LLM calls.
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
from pydantic import BaseModel

from specloop.search.vector_search import (
    nearest_assertion_descriptions,
    retrieve_all_vectors,
)


class CoverageRegion(BaseModel):
    center_description: str      # natural language description of this cluster
    module_count: int
    module_names: list[str]
    density: float               # relative density (0-1), higher = more covered


class CoverageReport(BaseModel):
    total_modules: int
    dense_regions: list[CoverageRegion]
    sparse_regions: list[CoverageRegion]
    coverage_score: float        # overall library breadth (0-1)
    generated_at: str            # ISO timestamp


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def compute_coverage_map(
    qdrant_url: str,
    collection: str,
    embed_model: str,
    n_clusters: int = 8,
) -> CoverageReport:
    """Analyze the library's behavioral coverage by clustering module vectors."""
    vectors = retrieve_all_vectors(qdrant_url, collection)
    total = len(vectors)

    if total < 5:
        return CoverageReport(
            total_modules=total,
            dense_regions=[],
            sparse_regions=[],
            coverage_score=0.0,
            generated_at=_now(),
        )

    from sklearn.cluster import KMeans

    names = list(vectors.keys())
    matrix = np.asarray([vectors[name] for name in names], dtype=np.float64)

    k = min(n_clusters, total // 2)
    kmeans = KMeans(n_clusters=k, n_init=10, random_state=0)
    labels = kmeans.fit_predict(matrix)

    regions: list[CoverageRegion] = []
    cluster_sizes: list[int] = []
    for cluster_id in range(k):
        members = [names[i] for i in range(total) if labels[i] == cluster_id]
        if not members:
            continue
        cluster_sizes.append(len(members))

        centroid = kmeans.cluster_centers_[cluster_id]
        nearest = nearest_assertion_descriptions(
            centroid.tolist(), qdrant_url, collection, limit=3
        )
        label = "; ".join(n["assertion"] for n in nearest) or "(no nearby assertions)"

        regions.append(CoverageRegion(
            center_description=label,
            module_count=len(members),
            module_names=members,
            density=len(members) / total,
        ))

    # Sort by density; top half = dense, bottom half = sparse.
    regions.sort(key=lambda r: r.density, reverse=True)
    split = len(regions) // 2 or 1
    dense_regions = regions[:split]
    sparse_regions = regions[split:]

    sizes = np.asarray(cluster_sizes, dtype=np.float64)
    mean = float(sizes.mean())
    coverage_score = 1.0 - (float(sizes.std()) / mean) if mean > 0 else 0.0
    coverage_score = max(0.0, min(1.0, coverage_score))

    return CoverageReport(
        total_modules=total,
        dense_regions=dense_regions,
        sparse_regions=sparse_regions,
        coverage_score=coverage_score,
        generated_at=_now(),
    )


def format_coverage_report(report: CoverageReport) -> str:
    """Format as human-readable text for CLI display."""
    if report.total_modules < 5:
        return (
            f"Library has only {report.total_modules} indexed module(s) — "
            "need at least 5 for a meaningful coverage map. Index more modules."
        )

    lines = [
        f"Library coverage map ({report.total_modules} modules, "
        f"overall coverage score {report.coverage_score:.2f})",
        f"Generated: {report.generated_at}",
        "",
        "Dense regions (well-covered behavioral domains):",
    ]
    for r in report.dense_regions:
        lines.append(f"  [{r.module_count} modules, density {r.density:.2f}] {r.center_description}")
        lines.append(f"    modules: {', '.join(r.module_names)}")

    lines += ["", "Sparse regions (thin coverage):"]
    for r in report.sparse_regions:
        lines.append(f"  [{r.module_count} modules, density {r.density:.2f}] {r.center_description}")
        lines.append(f"    modules: {', '.join(r.module_names)}")

    return "\n".join(lines)
