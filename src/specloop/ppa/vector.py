"""Normalized 4-dimensional PPA vector space.

A :class:`PPAVector` places a module (or a composition target) in a normalized
[0, 1] space across four axes:

  latency    — 0.0 = zero latency (combinational), 1.0 = high latency (deep pipeline)
  throughput — 0.0 = low throughput, 1.0 = high throughput (pipelined/parallel)
  area       — 0.0 = minimal area, 1.0 = large area
  power      — 0.0 = low power, 1.0 = high power

Vectors are derived from :class:`specloop.ppa.features.PPAFeatures` via simple
linear-normalization heuristics — no synthesis involved.
"""
from __future__ import annotations

import math

from pydantic import BaseModel

from specloop.ppa.features import PPAFeatures

# Normalization caps: feature values at or above the cap map to 1.0 on their axis.
_FF_CAP = 10.0
_ALWAYS_CAP = 20.0
_SUBMODULE_CAP = 15.0
_PORT_WIDTH_CAP = 256.0


def _clamp(x: float) -> float:
    """Clamp a value into the [0.0, 1.0] range."""
    return max(0.0, min(1.0, x))


class PPAVector(BaseModel):
    """A point in the normalized [0, 1] PPA space."""

    latency: float      # [0, 1]
    throughput: float   # [0, 1]
    area: float         # [0, 1]
    power: float        # [0, 1]


def features_to_vector(features: PPAFeatures) -> PPAVector:
    """Convert raw PPA features to a normalized [0, 1] PPA vector via heuristics.

    Latency is driven by flip-flop and async-reset counts (pure combinational
    logic yields 0.0). Throughput is the inverse of latency for sequential
    logic, with memories penalized. Area scales with the amount of logic and
    port width. Power tracks dynamic switching (flip-flops) plus submodule count.
    """
    # Latency: flip-flops dominate, async resets add a little pipeline depth.
    latency = _clamp(
        0.85 * (features.ff_count / _FF_CAP)
        + 0.15 * (features.async_reset_count / _FF_CAP)
    )

    # Throughput: pure combinational logic streams every cycle (high throughput);
    # sequential logic is inversely related to latency; memories are low.
    if features.is_memory:
        throughput = 0.2
    elif features.ff_count == 0:
        throughput = 1.0
    else:
        throughput = _clamp(1.0 - latency)

    # Area: more always blocks, submodules, and wider ports = more area.
    area = _clamp(
        0.4 * (features.always_block_count / _ALWAYS_CAP)
        + 0.35 * (features.submodule_count / _SUBMODULE_CAP)
        + 0.25 * (features.total_port_width / _PORT_WIDTH_CAP)
    )

    # Power: dynamic switching (flip-flops) plus submodule count.
    power = _clamp(
        0.7 * (features.ff_count / _FF_CAP)
        + 0.3 * (features.submodule_count / _SUBMODULE_CAP)
    )

    return PPAVector(latency=latency, throughput=throughput, area=area, power=power)


def sum_vectors(vectors: list[PPAVector]) -> PPAVector:
    """Combine PPA vectors for a composition via tip-to-tail addition.

    Latency, area, and power accumulate across composed modules (clamped to 1.0).
    Throughput of a pipeline is gated by its slowest stage, so the combined
    throughput is the minimum across the constituent vectors.
    """
    if not vectors:
        return PPAVector(latency=0.0, throughput=0.0, area=0.0, power=0.0)

    return PPAVector(
        latency=_clamp(sum(v.latency for v in vectors)),
        throughput=min(v.throughput for v in vectors),
        area=_clamp(sum(v.area for v in vectors)),
        power=_clamp(sum(v.power for v in vectors)),
    )


def distance(a: PPAVector, b: PPAVector) -> float:
    """Euclidean distance between two PPA vectors."""
    return math.sqrt(
        (a.latency - b.latency) ** 2
        + (a.throughput - b.throughput) ** 2
        + (a.area - b.area) ** 2
        + (a.power - b.power) ** 2
    )
