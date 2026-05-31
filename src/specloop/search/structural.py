"""Structural fingerprint: a 32-dim implementation signature derived from ModuleIR.

Captures *how* a module is built — port shape, always-block style, submodule and
parameter structure, size — independently of *what* it does. Two modules with the
same behavioral description but different implementations (shift register vs
counter, pipelined vs iterative) get different fingerprints, which lets composition
search favor architecturally diverse combinations.

Deterministic and synthesis-free: the same ModuleIR always yields the same vector.
"""
from __future__ import annotations

import numpy as np

from specloop.ir.schema import ModuleIR


def _clamp(x: float) -> float:
    """Clamp a value into the [0.0, 1.0] range."""
    return max(0.0, min(1.0, float(x)))


def extract_structural_fingerprint(ir: ModuleIR) -> list[float]:
    """Extract a 32-dimensional normalized structural fingerprint from a ModuleIR.

    All dimensions are normalized to [0, 1]. See module docstring; dimension layout
    follows the project spec exactly.
    """
    fp = [0.0] * 32

    # ── Port structure (dims 0-7) ──────────────────────────────────────────
    ports = ir.ports
    port_count = len(ports)
    input_count = sum(1 for p in ports if p.direction == "input")
    output_count = sum(1 for p in ports if p.direction == "output")
    clock_count = sum(1 for p in ports if p.is_clock)
    reset_count = sum(1 for p in ports if p.is_reset)
    widths = [p.width for p in ports]
    max_width = max(widths) if widths else 0
    mean_width = (sum(widths) / port_count) if port_count else 0.0
    has_inout = any(p.direction == "inout" for p in ports)

    fp[0] = _clamp(port_count / 64)
    fp[1] = _clamp(input_count / 32)
    fp[2] = _clamp(output_count / 32)
    fp[3] = _clamp(clock_count / 4)
    fp[4] = _clamp(reset_count / 4)
    fp[5] = _clamp(max_width / 128)
    fp[6] = _clamp(mean_width / 64)
    fp[7] = 1.0 if has_inout else 0.0

    # ── Always block structure (dims 8-15) ─────────────────────────────────
    blocks = ir.always_blocks
    ff_count = sum(1 for b in blocks if b.kind == "always_ff")
    comb_count = sum(1 for b in blocks if b.kind == "always_comb")
    latch_count = sum(1 for b in blocks if b.kind == "always_latch")
    async_reset_count = sum(1 for b in blocks if b.has_async_reset)
    has_sensitivity = any(b.sensitivity for b in blocks)
    signals_written = sum(len(b.signals_written) for b in blocks)
    signals_read = sum(len(b.signals_read) for b in blocks)
    rw_ratio = signals_read / max(signals_written, 1)

    fp[8] = _clamp(ff_count / 10)
    fp[9] = _clamp(comb_count / 10)
    fp[10] = _clamp(latch_count / 4)
    fp[11] = _clamp(async_reset_count / 4)
    fp[12] = 1.0 if has_sensitivity else 0.0
    fp[13] = _clamp(signals_written / 50)
    fp[14] = _clamp(signals_read / 100)
    fp[15] = _clamp(rw_ratio)

    # ── Module type one-hot (dims 16-20) ───────────────────────────────────
    mt = ir.module_type
    fp[16] = 1.0 if mt == "sequential" else 0.0
    fp[17] = 1.0 if mt == "combinational" else 0.0
    fp[18] = 1.0 if mt == "fsm" else 0.0
    fp[19] = 1.0 if mt == "memory" else 0.0
    fp[20] = 1.0 if mt not in ("sequential", "combinational", "fsm", "memory") else 0.0

    # ── Submodule structure (dims 21-24) ───────────────────────────────────
    submodules = ir.submodules
    submodule_count = len(submodules)
    unique_types = len({s.module_name for s in submodules})

    fp[21] = _clamp(submodule_count / 10)
    fp[22] = _clamp(unique_types / 5)
    fp[23] = 1.0 if submodule_count else 0.0
    fp[24] = _clamp(submodule_count / max(port_count, 1))

    # ── Parameter structure (dims 25-27) ───────────────────────────────────
    params = ir.parameters
    param_count = len(params)
    has_local = any(p.is_local for p in params)

    fp[25] = _clamp(param_count / 10)
    fp[26] = 1.0 if has_local else 0.0
    fp[27] = _clamp(param_count / max(port_count, 1))

    # ── Size indicators (dims 28-31) ───────────────────────────────────────
    line_count = max(0, ir.lines[1] - ir.lines[0])
    always_density = len(blocks) / max(line_count / 50, 1)
    complexity = ff_count * 2 + comb_count + submodule_count * 3

    fp[28] = _clamp(line_count / 500)
    fp[29] = _clamp(always_density)
    fp[30] = 1.0 if line_count > 200 else 0.0
    fp[31] = _clamp(complexity / 30)

    return [_clamp(x) for x in fp]


def structural_distance(a: list[float], b: list[float]) -> float:
    """Euclidean distance between two structural fingerprints. Range [0, sqrt(32)]."""
    return float(np.linalg.norm(np.array(a) - np.array(b)))


def structural_similarity(a: list[float], b: list[float]) -> float:
    """1 - normalized_distance. Range [0, 1]. Higher = more similar structure."""
    max_dist = 32 ** 0.5
    return 1.0 - structural_distance(a, b) / max_dist
