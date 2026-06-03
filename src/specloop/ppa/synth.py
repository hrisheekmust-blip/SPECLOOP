"""Real PPA statistics from Yosys synthesis.

The structural heuristics in :mod:`specloop.ppa.features` estimate complexity from
IR counts (always-block counts, port widths). This module replaces that estimate
with *measured* synthesis statistics — real cell count (area proxy) and flip-flop
count (sequential-complexity proxy) — by running Tabby CAD's Yosys on the module.

Everything here is best-effort. Synthesis can fail (missing package/submodule
dependencies, unsupported constructs) or time out; in every such case the public
entry points return ``None`` so the caller can fall back to the heuristic. A
synthesis failure must never break indexing.
"""
from __future__ import annotations

import logging
import math
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

from specloop.ppa.vector import PPAVector, _clamp

log = logging.getLogger(__name__)

# Synthesis runs at index time; keep it short so a slow module doesn't stall a
# bulk reindex. Modules that can't synthesize in this budget fall back silently.
_DEFAULT_TIMEOUT = 30

# Log-scale normalization caps, calibrated against the proven library: cell and
# flip-flop counts span orders of magnitude (a counter has ~20 cells, a CPU core
# tens of thousands), so a linear cap would crush the whole library into the
# bottom of the axis. log1p normalization gives a usable spread; a module at the
# cap saturates to 1.0 on that axis.
_CELL_CAP = 4096.0   # ~log1p(4096) ≈ 8.32
_FF_CAP = 512.0      # ~log1p(512)  ≈ 6.24


class SynthStats(BaseModel):
    """Measured synthesis statistics for a single module."""

    cells: int   # total standard cells after `synth -flatten` (area proxy)
    ffs: int     # flip-flop cells (sequential-complexity proxy)


def find_yosys() -> Optional[str]:
    """Return the path to the yosys binary, or None if it is not on PATH."""
    return shutil.which("yosys")


def synthesize_stats(
    rtl_source: str,
    module: str,
    timeout: int = _DEFAULT_TIMEOUT,
) -> Optional[SynthStats]:
    """Synthesize `module` from `rtl_source` and return measured cell/FF counts.

    Best-effort: returns None when yosys is unavailable, synthesis errors out
    (e.g. unresolved dependencies), the run times out, or the stat block can't
    be parsed. The caller falls back to the structural heuristic on None.
    """
    yosys = find_yosys()
    if not yosys or not rtl_source.strip():
        return None

    tmp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".sv", delete=False, encoding="utf-8"
        ) as fh:
            fh.write(rtl_source)
            tmp_path = Path(fh.name)

        # `synth -flatten` already prints one statistics block at the end, so we
        # do NOT append an explicit `stat` (that would print a second block and
        # double-count). -flatten inlines submodules so the counts are the real
        # flattened gate counts rather than per-module subtotals.
        script = f"read_verilog -sv {tmp_path}; synth -top {module} -flatten"
        proc = subprocess.run(
            [yosys, "-p", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            log.debug(
                "yosys synth failed for '%s' (rc=%d) — falling back to heuristic",
                module, proc.returncode,
            )
            return None

        return _parse_stat(proc.stdout)
    except subprocess.TimeoutExpired:
        log.debug("yosys synth timed out for '%s' — falling back to heuristic", module)
        return None
    except Exception as exc:  # never let synthesis break indexing
        log.debug("yosys synth raised for '%s': %s — falling back to heuristic", module, exc)
        return None
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


# Total-cell line, e.g. "   Number of cells:                 24" — the label
# comes first and the integer count last. The per-type lines below it look like
# "     $_DFFE_PN0P_                    8" (cell type first, count last).
_TOTAL_CELLS_RE = re.compile(r"^\s*Number of cells:\s+(\d+)")
# Per-type cell line: "<$CELLTYPE>   <count>".
_CELL_TYPE_RE = re.compile(r"^\s*(\$\S+)\s+(\d+)\s*$")


def _parse_stat(output: str) -> Optional[SynthStats]:
    """Parse one Yosys `stat` block: total cell count and flip-flop subtotal.

    Only the FIRST stat block is consumed so a duplicated stat print can't double
    the counts. Flip-flops are any cell type whose name contains "dff" (covers
    $_DFF_*, $_DFFE_*, $_SDFF_*, $_ADFF_*, $dff, $adff, …).
    """
    lines = output.splitlines()
    total: Optional[int] = None
    ffs = 0
    in_block = False

    for line in lines:
        if total is None:
            m = _TOTAL_CELLS_RE.match(line)
            if m:
                total = int(m.group(1))
                in_block = True
            continue

        # We are inside the first block; accumulate per-type counts until the
        # indented "<n> $type" lines stop.
        m = _CELL_TYPE_RE.match(line)
        if m:
            cell_type, count = m.group(1), int(m.group(2))
            if "dff" in cell_type.lower():
                ffs += count
        elif in_block and line.strip():
            # First non-blank, non-cell-type line ends the block.
            break

    if total is None:
        return None
    return SynthStats(cells=total, ffs=ffs)


def vector_from_synth(stats: SynthStats, is_memory: bool = False) -> PPAVector:
    """Map measured synthesis statistics to a normalized [0, 1] PPA vector.

    Mirrors the axis semantics of :func:`specloop.ppa.vector.features_to_vector`
    but drives them from real numbers:

      latency    — sequential complexity, from flip-flop count (combinational = 0)
      throughput — 1.0 for combinational, inverse of latency for sequential,
                   penalized for memories
      area       — total cell count
      power      — dynamic-switching proxy blending sequential depth and area
    """
    latency = _clamp(math.log1p(stats.ffs) / math.log1p(_FF_CAP))
    area = _clamp(math.log1p(stats.cells) / math.log1p(_CELL_CAP))

    if is_memory:
        throughput = 0.2
    elif stats.ffs == 0:
        throughput = 1.0
    else:
        throughput = _clamp(1.0 - latency)

    power = _clamp(0.6 * latency + 0.4 * area)

    return PPAVector(latency=latency, throughput=throughput, area=area, power=power)
