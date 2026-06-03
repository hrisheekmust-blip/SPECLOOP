"""Build composite documents from ProvenPairs and upsert them into Qdrant."""
from __future__ import annotations

import uuid

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from specloop.training.schema import ProvenPair

# Deterministic namespace for module point IDs.
_NS = uuid.NAMESPACE_DNS


def build_document(pair: ProvenPair) -> str:
    """Render a ProvenPair as a single composite text document for embedding."""
    ports = pair.module_ir.get("ports", [])
    params = pair.module_ir.get("parameters", [])

    port_lines: list[str] = []
    for p in ports:
        tags: list[str] = []
        if p.get("is_clock"):
            tags.append("clock")
        if p.get("is_reset"):
            polarity = p.get("reset_polarity", "")
            tags.append("reset, active-low" if polarity == "low" else "reset, active-high")
        width = p.get("width", 1)
        if width > 1:
            tags.append(f"{width}-bit")
        tag_str = f"  [{', '.join(tags)}]" if tags else ""
        port_lines.append(f"  {p['name']:<10} {p.get('direction', ''):6}{tag_str}")

    param_lines = [f"  {p['name']}={p.get('default', '?')}" for p in params]

    confidence = pair.proof.proven / max(pair.proof.total, 1)
    depth_note = f" at depth {pair.proof.depth}" if pair.proof.depth else ""
    assertion_header = f"Proven assertions ({pair.proof.proven}/{pair.proof.total}{depth_note}):"
    assertion_lines = [
        f"  {a.name} [{a.category}]: {a.rationale}" for a in pair.assertion_index
    ]

    parts: list[str] = [
        f"Module: {pair.module_name} ({pair.module_type})",
        "",
        "Ports:",
        *port_lines,
    ]
    if param_lines:
        parts += ["", "Parameters:", *param_lines]
    parts += [
        "",
        assertion_header,
        *assertion_lines,
        "",
        "Verified bind module:",
        pair.bind_module_sv,
    ]
    return "\n".join(parts)


_CATEGORIES = ["reset", "functional", "safety", "temporal", "interface", "fsm"]


def build_category_documents(pair: ProvenPair) -> dict[str, str]:
    """Build one focused text document per non-empty assertion category."""
    docs: dict[str, str] = {}
    for cat in _CATEGORIES:
        assertions_in_cat = [a for a in pair.assertion_index if a.category == cat]
        if not assertions_in_cat:
            continue
        lines = [
            f"Module: {pair.module_name} ({pair.module_type})",
            f"Category: {cat} assertions",
            "",
        ]
        for a in assertions_in_cat:
            lines.append(f"  {a.name}: {a.rationale}")
        docs[cat] = "\n".join(lines)
    return docs


def build_assertion_document(pair: ProvenPair) -> str | None:
    """Build a text document from the module's proven assertions only.

    Returns None if the module has no assertions. Format: one assertion per line as
    "category: rationale". This gives BGE natural-language behavioral claims to embed,
    without the port-name / SVA-syntax noise it doesn't understand.
    """
    if not pair.assertion_index:
        return None
    lines = [f"{a.category}: {a.rationale}" for a in pair.assertion_index]
    return "\n".join(lines)


def ensure_collection(client: QdrantClient, collection: str, dim: int) -> None:
    """Create the Qdrant collection if it does not already exist."""
    if not client.collection_exists(collection):
        client.create_collection(
            collection_name=collection,
            vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
        )


def index_pair(
    pair: ProvenPair,
    qdrant_url: str,
    collection: str,
    model_name: str,
) -> str:
    """Embed a ProvenPair and upsert it into Qdrant. Returns the point UUID."""
    from specloop.search._embed import embed_document

    doc = build_document(pair)
    vector = embed_document(doc, model_name)

    # Deterministic point ID — re-indexing the same module name always upserts the same point.
    point_id = str(uuid.uuid5(_NS, f"specloop.module.{pair.module_name}"))

    confidence = pair.proof.proven / max(pair.proof.total, 1)

    # PPA vector — measured from real Yosys synthesis when possible (cell count =
    # area proxy, flip-flop count = sequential-complexity proxy), falling back to
    # the structural heuristic when synthesis is unavailable, errors, or times out.
    # Synthesis is best-effort and must never break indexing.
    from specloop.ir.schema import ModuleIR
    from specloop.ppa.features import extract_features
    from specloop.ppa.vector import features_to_vector

    module_ir = ModuleIR.model_validate(pair.module_ir)

    synth_cells: int | None = None
    synth_ffs: int | None = None
    ppa_source = "heuristic"
    try:
        from specloop.ppa.synth import synthesize_stats, vector_from_synth

        stats = synthesize_stats(pair.rtl_source, pair.module_name)
        if stats is not None:
            ppa_vector = vector_from_synth(stats, is_memory=pair.module_type == "memory")
            synth_cells, synth_ffs = stats.cells, stats.ffs
            ppa_source = "synthesis"
        else:
            ppa_vector = features_to_vector(extract_features(module_ir))
    except Exception:
        # Any unexpected failure in the synthesis path degrades to the heuristic.
        ppa_vector = features_to_vector(extract_features(module_ir))

    # Hierarchical per-category assertion vectors (None when no assertions in a
    # category) and a structural fingerprint — both stored in the payload so the
    # main point vector (and the collection schema) stay unchanged.
    from specloop.search.structural import extract_structural_fingerprint

    category_docs = build_category_documents(pair)
    category_vectors = {
        f"vec_{cat}": (embed_document(category_docs[cat], model_name) if cat in category_docs else None)
        for cat in _CATEGORIES
    }
    struct_fp = extract_structural_fingerprint(module_ir)

    # Assertion-centric vectors: embed each proven assertion individually, then store
    # the L2-normalized centroid as the module's behavioral search vector. Proven
    # behavior is a more reliable signal than the English description.
    import numpy as np

    assertion_vectors = [
        embed_document(f"{a.category}: {a.rationale}", model_name)
        for a in pair.assertion_index
    ]
    if assertion_vectors:
        mean = np.asarray(assertion_vectors, dtype=np.float64).mean(axis=0)
        norm = np.linalg.norm(mean)
        assertion_centroid = (mean / norm).tolist() if norm > 0 else mean.tolist()
    else:
        assertion_centroid = None

    # Hard structural filters — heuristics over port names / assertion categories that
    # let search reject linguistically-similar but structurally-incompatible modules.
    port_names = [p["name"] for p in pair.module_ir.get("ports", [])]
    has_axi = any("axi" in n.lower() or "awvalid" in n.lower() or
                  "wvalid" in n.lower() or "arvalid" in n.lower()
                  for n in port_names)
    has_wishbone = any("wb_" in n.lower() or n.lower() in
                       ["cyc", "stb", "we", "ack"] for n in port_names)
    has_valid_ready = any("valid" in n.lower() for n in port_names) and \
                      any("ready" in n.lower() for n in port_names)
    has_apb = any("psel" in n.lower() or "penable" in n.lower() or
                  "pwrite" in n.lower() for n in port_names)

    clock_count = sum(1 for p in pair.module_ir.get("ports", [])
                      if p.get("is_clock"))
    port_widths = [p.get("width", 1) for p in pair.module_ir.get("ports", [])]
    max_port_width = max(port_widths) if port_widths else 0
    has_wide_ports = max_port_width >= 32

    assertion_cats = {a.category for a in pair.assertion_index}

    payload = {
        "module_name": pair.module_name,
        "module_type": pair.module_type,
        "assertion_count": len(pair.assertion_index),
        "confidence": confidence,
        "file_path": pair.file_path,
        "record_id": pair.record_id,
        "assertion_summary": [f"{a.name}: {a.rationale}" for a in pair.assertion_index],
        "ppa_latency": ppa_vector.latency,
        "ppa_throughput": ppa_vector.throughput,
        "ppa_area": ppa_vector.area,
        "ppa_power": ppa_vector.power,
        # Raw synthesis numbers for inspection; None when the heuristic was used.
        "synth_cells": synth_cells,
        "synth_ffs": synth_ffs,
        "ppa_source": ppa_source,
        "structural_fingerprint": struct_fp,
        "assertion_vector": assertion_centroid,
        "assertion_vectors": assertion_vectors,
        "has_axi": has_axi,
        "has_wishbone": has_wishbone,
        "has_valid_ready": has_valid_ready,
        "has_apb": has_apb,
        "clock_count": clock_count,
        "max_port_width": max_port_width,
        "has_wide_ports": has_wide_ports,
        "has_reset_assertions": "reset" in assertion_cats,
        "has_fsm_assertions": "fsm" in assertion_cats,
        "has_safety_assertions": "safety" in assertion_cats,
        **category_vectors,
    }

    client = QdrantClient(url=qdrant_url, check_compatibility=False)
    ensure_collection(client, collection, len(vector))
    client.upsert(
        collection_name=collection,
        points=[PointStruct(id=point_id, vector=vector, payload=payload)],
    )
    return point_id
