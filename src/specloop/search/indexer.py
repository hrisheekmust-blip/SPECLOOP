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
    payload = {
        "module_name": pair.module_name,
        "module_type": pair.module_type,
        "assertion_count": len(pair.assertion_index),
        "confidence": confidence,
        "file_path": pair.file_path,
        "record_id": pair.record_id,
        "assertion_summary": [f"{a.name}: {a.rationale}" for a in pair.assertion_index],
    }

    client = QdrantClient(url=qdrant_url)
    ensure_collection(client, collection, len(vector))
    client.upsert(
        collection_name=collection,
        points=[PointStruct(id=point_id, vector=vector, payload=payload)],
    )
    return point_id
