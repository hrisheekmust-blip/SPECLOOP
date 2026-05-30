"""Infer a PPA target from a natural-language hardware design request.

A single focused LLM call maps phrasing like "high throughput" or "low power"
onto a normalized PPA target. If nothing PPA-relevant is stated — or the call
fails — a balanced default is returned with confidence 0.0.
"""
from __future__ import annotations

import logging

from pydantic import BaseModel

from specloop.gen.client import LLMClient
from specloop.gen.pipeline import _parse_json

log = logging.getLogger(__name__)

_SYSTEM = (
    "You are a hardware design advisor. Given a natural language hardware design "
    "request, extract the implied PPA (power, performance, area) targets as "
    "normalized values between 0 and 1. Return JSON only."
)


def _clamp(x: float) -> float:
    """Clamp a value into the [0.0, 1.0] range."""
    return max(0.0, min(1.0, float(x)))


class PPATarget(BaseModel):
    """A desired point in PPA space, plus how confident the inference was."""

    latency: float = 0.3
    throughput: float = 0.5
    area: float = 0.3
    power: float = 0.3
    confidence: float = 0.0   # how confident we are in this inference


def infer_target(request: str, client: LLMClient) -> PPATarget:
    """Call the LLM to extract PPA preferences from a request.

    Returns a :class:`PPATarget` with normalized [0, 1] axes. On any parse or
    call failure, returns the balanced default ``PPATarget()`` (confidence 0.0).
    """
    user = (
        f"Request: {request}\n\n"
        "Return JSON: {latency, throughput, area, power} where 0=minimize/low "
        "and 1=maximize/high. Also return confidence (0-1) indicating how "
        "explicitly the request stated PPA preferences."
    )

    try:
        raw = client.generate(_SYSTEM, user)
        data = _parse_json(raw, "ppa_target")
        if not data:
            return PPATarget()
        return PPATarget(
            latency=_clamp(data.get("latency", 0.3)),
            throughput=_clamp(data.get("throughput", 0.5)),
            area=_clamp(data.get("area", 0.3)),
            power=_clamp(data.get("power", 0.3)),
            confidence=_clamp(data.get("confidence", 0.0)),
        )
    except Exception as exc:  # noqa: BLE001 — degrade gracefully on any failure
        log.warning("PPA target inference failed (%s) — using balanced default", exc)
        return PPATarget()
