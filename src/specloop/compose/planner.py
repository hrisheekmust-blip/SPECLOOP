"""Stage 1: planner — decompose a natural-language request into an ordered role list.

The pipeline is (1) plan a request into roles [this module], (2) retrieve a proven
block per role, (3) compose + formally prove. A request comes in; an LLM proposes an
ordered pipeline of roles drawn ONLY from the proven block catalog. This module
attaches ``position`` MECHANICALLY from list order (first=head, last=tail, else
middle) — the LLM never chooses position. The result is exactly the ``list[Role]``
stage 2 (``BlockRetriever.retrieve_chain``) consumes.

LIBRARY-AWARE, CONSTRAINT-CHECK ONLY (Job A): the planner knows the catalog — which
functions exist and the data widths actually proven for each — derived from the SAME
source of truth stage 2 uses (``sound_results.json`` + the derived classifier). It is
told the menu in its prompt and plans within it, and a hard post-parse validation pass
rejects anything off-menu UP FRONT with an informative message, instead of letting
stage 2 discover it later. This is validation/selection, NOT decomposition: the
decomposition stays exactly as naive as before (request -> ordered role list). It does
NOT choose boundaries, reason about block-internal overlap, or optimise the plan
(that is the deferred Job B), and it never auto-rewrites the user's spec.

CONSTRAINED VOCABULARY: only catalogued functions/widths are allowed; anything else is
an honest ``PlannerError`` at planning time.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from specloop.compose.retrieval import FUNCTIONS, BlockRetriever, Role, classify
from specloop.gen.client import LLMClient
from specloop.gen.pipeline import _parse_json

log = logging.getLogger(__name__)

_PROTOCOL = "axi_stream"
_DEFAULT_WIDTH = 8

# One-line behavioral gloss per function so the LLM maps intent to the right stage.
_FUNCTION_GLOSS: dict[str, str] = {
    "register":      "insert a pipeline/skid register stage (decouple timing; data unchanged)",
    "buffer":        "FIFO buffer: queue/absorb bursts with occupancy (data unchanged)",
    "width_convert": "data-width adapter: convert the stream between two tdata widths",
    "rate_limit":    "rate limiter: throttle / pace the stream's throughput",
    "frame_adjust":  "frame length adjust: pad / truncate / normalise frame length",
    "encode":        "COBS encode: byte-stuff / encode the byte stream",
    "decode":        "COBS decode: un-stuff / decode the byte stream",
}


def build_planning_catalog(retriever: BlockRetriever) -> dict[str, list[int]]:
    """Derive the planning menu from the proven catalog stage 2 uses: for each role
    function, the data widths actually present in the soundly-proven blocks that
    derive to it. Nothing is hardcoded — if the proven library changes, so does this
    (a new 16-bit FIFO would make ``buffer -> [8, 16]``)."""
    widths: dict[str, set[int]] = {}
    for block in retriever.catalog.values():
        function = classify(block.signature)          # derived, same as stage-2 matching
        if function not in FUNCTIONS:                 # mux/demux/etc. are outside the role vocab
            continue
        w = block.slave_width
        if w is not None:
            widths.setdefault(function, set()).add(w)
    return {fn: sorted(ws) for fn, ws in widths.items()}


class PlannerError(Exception):
    """An honest planning failure surfaced UP FRONT: an out-of-catalog function, an
    unavailable width, or an unparseable response. Never a silently-substituted plan."""


@dataclass
class PlanResult:
    """The ordered roles for a request, plus trace metadata."""
    request: str
    roles: list[Role]
    data_width: int
    data_width_defaulted: bool   # True when the request gave no width (defaulted)
    raw: str                     # raw LLM response, for the end-to-end trace


def _position(idx: int, n: int) -> str:
    """Mechanical position from list order: first=head, last=tail, else middle. A
    lone stage is both head and tail, so it must expose both bundles -> 'middle'."""
    if n == 1:
        return "middle"
    if idx == 0:
        return "head"
    if idx == n - 1:
        return "tail"
    return "middle"


class Planner:
    """Stage 1: natural-language request -> ordered list[Role], constrained to and
    validated against the proven catalog."""

    def __init__(
        self,
        client: LLMClient,
        catalog: dict[str, list[int]],
        default_data_width: int = _DEFAULT_WIDTH,
    ) -> None:
        self._client = client
        self._catalog = catalog
        self._default_width = default_data_width

    @classmethod
    def from_retriever(
        cls, client: LLMClient, retriever: BlockRetriever,
        default_data_width: int = _DEFAULT_WIDTH,
    ) -> "Planner":
        """Build a planner whose menu is derived from the retriever's proven catalog."""
        return cls(client, build_planning_catalog(retriever), default_data_width)

    def _system_prompt(self) -> str:
        menu = "\n".join(
            f"  - {f}: {_FUNCTION_GLOSS[f]}  [available data widths: {self._catalog.get(f, [])}]"
            for f in FUNCTIONS if f in self._catalog
        )
        return (
            "You are a hardware pipeline planner for an AXI-Stream block library.\n"
            "Decompose the user's request into an ORDERED pipeline of roles, in dataflow\n"
            "order (input side first, output side last).\n\n"
            "AVAILABLE BLOCKS — you may plan ONLY within this menu (protocol: axi_stream):\n"
            f"{menu}\n\n"
            "Rules:\n"
            "- Respond with STRICT JSON and nothing else.\n"
            "- Each role is {\"function\": <one of the functions above>, \"data_width\": <int bits>}.\n"
            "- DATA WIDTH: use the EXACT width the user states, and do NOT substitute a\n"
            "  different one even if it is not in the available list — the system checks\n"
            "  width availability and will surface options. If the user gives no width, omit\n"
            "  data_width (the system fills an available default).\n"
            "- Do NOT output 'position' — the system assigns it from order.\n"
            "- These functions are the ONLY blocks available. If the request needs a function\n"
            "  NOT in the menu (e.g. encrypt, decrypt, arbitrate, CRC, checksum, multiplex,\n"
            "  route), do NOT invent a stage. Instead return exactly:\n"
            "  {\"error\": \"cannot plan: requires <function>, not in the available block set\"}\n\n"
            "Respond with exactly one of:\n"
            "  {\"roles\": [{\"function\": \"...\", \"data_width\": N}, ...]}\n"
            "  {\"error\": \"cannot plan: requires <function>, not in the available block set\"}"
        )

    def plan(self, request: str) -> PlanResult:
        """Decompose ``request`` into an ordered role list, or raise PlannerError."""
        raw = self._client.generate(self._system_prompt(), request)
        data = _parse_json(raw, "plan")
        return self._build(request, data, raw)

    def _build(self, request: str, data: dict, raw: str) -> PlanResult:
        if not data:
            raise PlannerError(
                f"planner produced no parseable JSON for request: {request!r}"
            )
        if "error" in data:                      # honest failure surfaced by the model
            raise PlannerError(str(data["error"]))

        raw_roles = data.get("roles")
        if not isinstance(raw_roles, list) or not raw_roles:
            raise PlannerError(
                f"planner returned no roles for request: {request!r} (got: {data})"
            )

        defaulted = False
        parsed: list[tuple[str, int]] = []
        for entry in raw_roles:
            if not isinstance(entry, dict) or "function" not in entry:
                raise PlannerError(f"malformed role entry from planner: {entry!r}")
            function = str(entry["function"]).strip().lower()

            # HARD VALIDATION PASS (don't trust the LLM), against the DERIVED catalog:
            # (1) the function must be an available block...
            available = self._catalog.get(function)
            if not available:
                raise PlannerError(
                    f"cannot plan: requires '{function}', not in the available block set"
                )
            # ...(2) and the requested width must actually be available for it.
            raw_w = entry.get("data_width")
            if raw_w in (None, "", 0):
                width = min(available)               # derived default, not hardcoded
                defaulted = True
            else:
                width = int(raw_w)
            if width not in available:
                msg = (f"cannot plan: {function} is only available at width(s) "
                       f"{available}; requested {width}")
                if len(available) == 1:
                    msg += f" — available at {available[0]}-bit, proceed at {available[0]}-bit?"
                raise PlannerError(msg)
            parsed.append((function, width))

        n = len(parsed)
        roles = [
            Role(function=fn, protocol=_PROTOCOL, data_width=w, position=_position(i, n))
            for i, (fn, w) in enumerate(parsed)
        ]
        return PlanResult(
            request=request,
            roles=roles,
            data_width=roles[0].data_width,
            data_width_defaulted=defaulted,
            raw=raw,
        )
