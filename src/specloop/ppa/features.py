"""Lightweight PPA feature extraction from a ModuleIR.

These features are structural counts derived purely from the IR — no synthesis
is required, so extraction is free and runs at ingest time. They act as proxies
for power, performance, and area, and feed :func:`specloop.ppa.vector.features_to_vector`.
"""
from __future__ import annotations

from pydantic import BaseModel

from specloop.ir.schema import ModuleIR


class PPAFeatures(BaseModel):
    """Structural PPA proxy counts extracted from a ModuleIR."""

    # Latency proxy: more always_ff blocks and deeper sensitivity = more pipeline stages
    ff_count: int           # number of always_ff blocks
    async_reset_count: int  # always blocks with has_async_reset=True
    comb_depth: int         # number of always_comb blocks

    # Area proxy: more logic = more area
    always_block_count: int
    submodule_count: int
    port_count: int
    total_port_width: int   # sum of all port widths
    param_count: int

    # Throughput proxy: combinational modules have higher throughput
    is_sequential: bool     # module_type == "sequential" or "fsm"
    is_memory: bool         # module_type == "memory"

    # Signal complexity
    signals_written_count: int  # total across all always blocks
    signals_read_count: int     # total across all always blocks


def extract_features(ir: ModuleIR) -> PPAFeatures:
    """Extract PPA features from a ModuleIR. Zero cost, runs at ingest time."""
    ff_count = sum(1 for b in ir.always_blocks if b.kind == "always_ff")
    async_reset_count = sum(1 for b in ir.always_blocks if b.has_async_reset)
    comb_depth = sum(1 for b in ir.always_blocks if b.kind == "always_comb")

    total_port_width = sum(p.width for p in ir.ports)
    signals_written_count = sum(len(b.signals_written) for b in ir.always_blocks)
    signals_read_count = sum(len(b.signals_read) for b in ir.always_blocks)

    return PPAFeatures(
        ff_count=ff_count,
        async_reset_count=async_reset_count,
        comb_depth=comb_depth,
        always_block_count=len(ir.always_blocks),
        submodule_count=len(ir.submodules),
        port_count=len(ir.ports),
        total_port_width=total_port_width,
        param_count=len(ir.parameters),
        is_sequential=ir.module_type in ("sequential", "fsm"),
        is_memory=ir.module_type == "memory",
        signals_written_count=signals_written_count,
        signals_read_count=signals_read_count,
    )
