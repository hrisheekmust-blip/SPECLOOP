"""Stage 2: block retrieval — given a role, return the proven block that fills it.

The pipeline's vision is four stages: (1) decompose a request into roles, (2) find
the proven block for each role [this module], (3) compose + formally prove, (4)
cross-boundary fusion. Stage 3 already composes and soundly proves over the 13
``soundly_reproven`` AXIS blocks in ``recheck_axis/sound_results.json``.

MATCHING IS DERIVED FROM WHAT THE BLOCK IS PROVEN TO DO — never from a hand-assigned
label. For each block we read its *proven contracts* (the surviving assertions in
``work/<block>.bind.sv``) and distil a behavioral signature: a buffer's contracts
reference emptiness / occupancy / read+write pointers; a register's reference single-
beat data stability and pass-through with no queue; a rate-limiter's reference an
accumulator / pause; a codec's reference an FSM with code-build (encode) vs zero-
reinsertion (decode); a frame-length adjuster's reference length / pad / truncate.
A role carries the *behavior it needs* (``_ROLE_BEHAVIOR``), and a block fills the
role iff that role's behavior is the block's own best-fit classification, recomputed
from its signature. Structure confirms the one thing contracts can't see — a width
converter's distinct in/out width params (``S_DATA_WIDTH``/``M_DATA_WIDTH``), which
otherwise looks register-like in its proofs.

This replaces an earlier 32-dim structural fingerprint that could not separate
function (an SRL register sat closer to an SRL FIFO than to other registers) — a
representation failure, not proof that derived matching is impossible. The contract
signature *does* separate them: ``axis_srl_register`` proves ``data_stable`` with no
emptiness/occupancy/pointer contracts, ``axis_srl_fifo`` proves emptiness + occupancy
+ pointer increment/decrement with no data-stable contract.

Because matching reads only the derived signature, scrambling any stored function
field leaves every result unchanged (the anti-cheat test) — it is retrieval, not a
lookup. Only ``soundly_reproven`` blocks are ever candidates. No gap-filling, no
generation, no re-parameterization, no PPA ranking — an honest no-match instead.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from specloop.compose.assertions import parse_proven_assertions
from specloop.compose.compatibility import (
    AXIS_CORE_SIGNALS,
    axis_bundles,
    bundles_by_role,
    candidate_role_issues,
)
from specloop.compose.schema import (
    CompositionPlan,
    Connection,
    SelectedModule,
    SubFunction,
)
from specloop.ir.schema import ModuleIR
from specloop.search.searcher import SearchResult
from specloop.search.structural import extract_structural_fingerprint

log = logging.getLogger(__name__)

# The function vocabulary stage 1 emits (educated guess; reshaped at the seam later).
FUNCTIONS = (
    "buffer", "register", "width_convert", "rate_limit",
    "frame_adjust", "encode", "decode",
)

_PROTOCOL = "axi_stream"

# Which AXIS bundle role(s) a chain position requires the block to expose. A head
# drives downstream (needs a master; its slave is the chain input); a tail receives
# upstream (needs a slave; its master is the chain output); a middle needs both.
_POSITION_ROLES: dict[str, set[str]] = {
    "head": {"master"},
    "middle": {"slave", "master"},
    "tail": {"slave"},
}

# ---------------------------------------------------------------------------
# Derived behavioral signature
# ---------------------------------------------------------------------------
# Behavioral concepts, each detected from a block's PROVEN-CONTRACT text (assertion
# labels + asserted expressions + enclosing guards). These are the primitives the
# proofs actually reason about, so they carry function the way generic port/size
# counts cannot. Patterns are intentionally specific (e.g. 'data_stable' not bare
# 'stable', 'tvalid_onehot' for a demux vs a mux's input 'tready_onehot').
_CONTRACT_CONCEPTS: dict[str, list[str]] = {
    "emptiness":     [r"empty"],
    "occupancy":     [r"\bcount\b", r"occupanc", r"depth", r"\bfill"],
    "wr_pointer":    [r"wr_ptr", r"write_xfer", r"increments_on_write", r"increment_on_write"],
    "rd_pointer":    [r"rd_ptr", r"read_xfer", r"decrements_on_read", r"decrement_on_read"],
    "overflow":      [r"overflow"],
    "data_stable":   [r"data_stable", r"stable_until_ready", r"_stable_tdata"],
    "bypass":        [r"bypass", r"passthrough", r"\bskid"],
    "accumulator":   [r"accumulat", r"\bacc_", r"\bpause", r"paused", r"throttle"],
    "frame_length":  [r"\blength", r"\bpad", r"truncate", r"short_counter", r"long_counter"],
    "code_build":    [r"code_fifo", r"alternates", r"code_data"],
    "zero_reinsert": [r"zero_insertion", r"suppress_zero", r"reinsert"],
    "segment":       [r"segment"],
    "fsm":           [r"\bfsm", r"state_machine", r"_valid_states", r"idle_to",
                      r"_to_segment", r"_transition", r"state_reg", r"state_next"],
    "select_route":  [r"select", r"\bsel_", r"_sel\b"],
    "onehot_route":  [r"routing", r"\bdrop", r"tvalid_onehot"],
    "tag_join":      [r"\btag", r"port_increment", r"port_selector"],
}

# The behavior each role function NEEDS, as (concept, weight). 'register' is the
# low-weight default — a pass-through with valid/ready/data stability and NO
# specialised behavior (no queue, no width change, no accumulator, no codec/frame
# logic); any specialised concept (weight 3) outranks it. A block's derived
# function is the argmax of these scores over its signature (None if it scores 0).
_ROLE_BEHAVIOR: dict[str, list[tuple[str, int]]] = {
    "buffer":        [("emptiness", 3), ("occupancy", 3), ("rw_pointers", 3), ("overflow", 1)],
    "register":      [("data_stable", 1), ("bypass", 1)],
    "width_convert": [("width_convert", 3)],
    "rate_limit":    [("accumulator", 3)],
    "frame_adjust":  [("frame_length", 3)],
    "encode":        [("code_build", 3), ("fsm", 1)],
    "decode":        [("zero_reinsert", 3), ("segment", 2), ("fsm", 1)],
    # Proven, retrievable, but outside the stage-1 role vocabulary above (fan / join
    # / route functions) — included so all 13 blocks classify cleanly and a 'mux'
    # role honestly returns axis_mux rather than mis-classifying it as a register.
    "mux":           [("select_route", 3)],
    "demux":         [("select_route", 2), ("onehot_route", 3)],
    "frame_join":    [("tag_join", 3), ("fsm", 1)],
}


def _width_convert_struct(ir: ModuleIR) -> bool:
    """The one feature the contracts can't see: distinct input/output width params.
    Only the width adapter declares an ``S_*``/``M_*`` width-param pair — every other
    block carries a single ``DATA_WIDTH``. (A bare slave!=master tdata width is NOT
    used: fan-in/out blocks concatenate streams into a wide bus, which is not width
    conversion.)"""
    params = {p.name.upper() for p in ir.parameters}
    return {"S_DATA_WIDTH", "M_DATA_WIDTH"} <= params or {"S_BYTE_LANES", "M_BYTE_LANES"} <= params


def derive_signature(asserts, ir: ModuleIR) -> dict[str, int]:
    """Distil a behavioral signature from a block's proven contracts (primary) plus
    the width-param structural feature (confirmation). Pure function of (contracts,
    IR) — no block identity, no name, no hand label."""
    text = " ".join(
        f"{a.label} {a.expr} {' '.join(a.guards)}".lower() for a in asserts
    )
    sig = {
        concept: (1 if any(re.search(p, text) for p in pats) else 0)
        for concept, pats in _CONTRACT_CONCEPTS.items()
    }
    sig["rw_pointers"] = 1 if (sig["wr_pointer"] and sig["rd_pointer"]) else 0
    sig["width_convert"] = 1 if _width_convert_struct(ir) else 0
    return sig


def profile_score(function: str, signature: dict[str, int]) -> int:
    """How strongly ``signature`` exhibits the behavior ``function`` needs."""
    return sum(w * signature.get(c, 0) for c, w in _ROLE_BEHAVIOR.get(function, []))


def classify(signature: dict[str, int]) -> Optional[str]:
    """The block's best-fit function = argmax behavioral profile score (deterministic
    tie-break toward the more specific/shorter name). None when nothing fits (a block
    with no behavioral evidence matches no role — an honest gap, not a guess)."""
    best = max(_ROLE_BEHAVIOR, key=lambda fn: (profile_score(fn, signature), -len(fn)))
    return best if profile_score(best, signature) > 0 else None


@dataclass(frozen=True)
class Role:
    """A structured role descriptor (stage-1 output; the seam is reshaped later)."""
    function: str
    protocol: str = _PROTOCOL
    data_width: int = 8
    position: str = "middle"  # head | middle | tail


@dataclass
class ProvenBlock:
    """A genuinely-proven block, with its derived behavioral signature. ``signature``
    is the only thing matching reads; ``derived_function`` is ``classify(signature)``
    kept for reporting/inspection and is NOT consulted by the matcher (scrambling it
    changes nothing — the anti-cheat property)."""
    name: str
    ir: ModuleIR
    confidence: float           # sound_confidence from sound_results.json
    rtl_path: Path
    signature: dict[str, int]   # derived behavioral concepts (+ width_convert)
    derived_function: Optional[str]
    fingerprint: list[float] = field(default_factory=list)  # carried to stage 3 only

    @property
    def slave_width(self) -> Optional[int]:
        """tdata width of the input (slave) bundle — the width an upstream neighbour
        must drive. None if the block has no slave bundle (a pure source)."""
        slaves = bundles_by_role(self.ir, "slave")
        return slaves[0]["tdata"].width if slaves and "tdata" in slaves[0] else None

    @property
    def active_concepts(self) -> list[str]:
        """The behavioral concepts present in the signature (for reporting)."""
        return sorted(
            c for c, v in self.signature.items()
            if v and c not in ("wr_pointer", "rd_pointer")
        )


@dataclass
class RetrievalResult:
    """The outcome of retrieving one role: the chosen block (or honest no-match),
    every valid candidate ranked, what was excluded for interface, and why."""
    role: Role
    block: Optional[ProvenBlock]
    ranked: list[ProvenBlock] = field(default_factory=list)
    excluded: list[tuple[str, str]] = field(default_factory=list)  # (block, reason)
    reason: str = ""

    @property
    def matched(self) -> bool:
        return self.block is not None


class RetrievalError(Exception):
    """Raised when a chain cannot be assembled because a role has no proven block."""


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

def _proven_confidences(sound_results: Path) -> dict[str, float]:
    """Names + sound_confidence of every ``soundly_reproven`` block — the single
    source of truth for which blocks are eligible. Nothing else can be returned."""
    data = json.loads(Path(sound_results).read_text(encoding="utf-8"))
    return {
        name: float(rec.get("sound_confidence") or 0.0)
        for name, rec in data.items()
        if rec.get("status") == "soundly_reproven"
    }


def _has_axis_bundle(ir: ModuleIR) -> bool:
    """Structural guard: the block exposes at least one usable AXIS bundle (all four
    core signals). Rejects a corrupted/mislabelled IR before it enters the catalog."""
    return any(
        all(sig in bundle for sig in AXIS_CORE_SIGNALS)
        for bundle in axis_bundles(ir).values()
    )


def load_catalog(work_dir: Path, sound_results: Path) -> dict[str, ProvenBlock]:
    """Build the proven-block catalog from sound_results.json + each block's IR and
    proven bind. Only ``soundly_reproven`` blocks with a structurally-valid AXIS IR
    are included; each block's behavioral signature is derived from its proven
    contracts (NOT from its name or any hand label)."""
    catalog: dict[str, ProvenBlock] = {}
    for name, confidence in _proven_confidences(sound_results).items():
        ir_path = Path(work_dir) / f"{name}.ir.json"
        if not ir_path.exists():
            log.warning("proven block '%s' has no IR at %s — skipping", name, ir_path)
            continue
        ir = ModuleIR.model_validate(json.loads(ir_path.read_text(encoding="utf-8")))
        if not _has_axis_bundle(ir):
            log.warning("proven block '%s' exposes no usable AXIS bundle — skipping", name)
            continue

        bind_path = Path(work_dir) / f"{name}.bind.sv"
        asserts = (
            parse_proven_assertions(bind_path.read_text(encoding="utf-8", errors="replace"))
            if bind_path.exists() else []
        )
        if not asserts:
            log.warning("proven block '%s' has no proven contracts — behavioral "
                        "signature will be empty", name)
        signature = derive_signature(asserts, ir)
        catalog[name] = ProvenBlock(
            name=name,
            ir=ir,
            confidence=confidence,
            rtl_path=Path(ir.file),
            signature=signature,
            derived_function=classify(signature),
            fingerprint=extract_structural_fingerprint(ir),
        )
    return catalog


# ---------------------------------------------------------------------------
# Retriever
# ---------------------------------------------------------------------------

def _interface_reject(block: ProvenBlock, role: Role, required: set[str]) -> Optional[str]:
    """Why ``block``'s interface cannot fill ``role`` (None if it fits).

    Reuses change-#1's bundle-aware logic: the block must expose the AXIS bundle
    role(s) its position needs, and its input (slave) tdata width must equal the
    role's stream width (re-parameterization is out of scope — a different default
    width is a genuine mismatch here, not something we silently coerce)."""
    if candidate_role_issues(block.ir, required):
        return f"missing {'/'.join(sorted(required))} bundle for position '{role.position}'"
    sw = block.slave_width
    if sw is not None and sw != role.data_width:
        return f"data width {sw} != role {role.data_width}"
    return None


class BlockRetriever:
    """Stage 2: role -> proven block, matched on derived behavior over the 13
    soundly-reproven AXIS blocks."""

    def __init__(self, work_dir: Path, sound_results: Optional[Path] = None) -> None:
        self.work_dir = Path(work_dir)
        self.sound_results = (
            Path(sound_results) if sound_results is not None
            else self.work_dir / "recheck_axis" / "sound_results.json"
        )
        self.catalog = load_catalog(self.work_dir, self.sound_results)

    def _best_fit(self, block: ProvenBlock) -> Optional[str]:
        """The block's best-fit function, recomputed from its derived signature. This
        is what matching uses — never ``block.derived_function`` (a stored field) —
        so scrambling any stored label cannot change a match."""
        return classify(block.signature)

    def retrieve(self, role: Role) -> RetrievalResult:
        """Return the best proven block for ``role``, or an honest no-match.

        A block fills the role iff the role's function is the block's own best-fit
        behavioral classification (derived from its proven contracts), it speaks the
        protocol, and its interface (position + width) is compatible. Survivors are
        ranked by how strongly they exhibit the role's behavior, then proof
        confidence, then name."""
        if role.protocol != _PROTOCOL:
            return RetrievalResult(
                role, None,
                reason=f"no proven block for protocol '{role.protocol}' "
                       f"(only '{_PROTOCOL}' is proven)",
            )
        if role.function not in _ROLE_BEHAVIOR:
            return RetrievalResult(
                role, None,
                reason=f"no behavioral profile for function '{role.function}'",
            )
        required = _POSITION_ROLES.get(role.position)
        if required is None:
            return RetrievalResult(
                role, None,
                reason=f"unknown position '{role.position}' "
                       f"(expected one of {sorted(_POSITION_ROLES)})",
            )

        # DERIVED match: the role's function must be the block's best-fit behavior.
        by_behavior = [
            b for b in self.catalog.values() if self._best_fit(b) == role.function
        ]
        if not by_behavior:
            return RetrievalResult(
                role, None,
                reason=f"no proven block's behavior matches function '{role.function}'",
            )

        compatible: list[ProvenBlock] = []
        excluded: list[tuple[str, str]] = []
        for block in sorted(by_behavior, key=lambda b: b.name):
            why = _interface_reject(block, role, required)
            if why:
                excluded.append((block.name, why))
            else:
                compatible.append(block)

        if not compatible:
            detail = ", ".join(f"{n} ({w})" for n, w in excluded)
            return RetrievalResult(
                role, None, excluded=excluded,
                reason=f"no proven block fills role function='{role.function}' "
                       f"width={role.data_width} position='{role.position}' "
                       f"(interface-incompatible: {detail})",
            )

        # All survivors are the same derived function with a compatible interface, so
        # that's a tie; break it by proof confidence (prefer the better-proven block),
        # then name for determinism. No PPA optimisation (deferred — no metrics).
        ranked = sorted(compatible, key=lambda b: (-b.confidence, b.name))
        best = ranked[0]
        return RetrievalResult(
            role, best, ranked=ranked, excluded=excluded,
            reason=f"behavior '{role.function}' -> '{best.name}' "
                   f"(derived from {best.active_concepts}; proven conf={best.confidence:.2f}; "
                   f"{len(ranked)} candidate(s), {len(excluded)} excluded)",
        )

    def retrieve_chain(
        self, roles: list[Role], composition_name: str = "composition",
    ) -> tuple[list[SelectedModule], CompositionPlan, list[RetrievalResult]]:
        """Retrieve a block per role (head->tail) and assemble the exact stage-3
        composition input: a list of SelectedModule + a CompositionPlan whose
        connections wire each stage's m_axis to the next stage's s_axis.

        Raises RetrievalError on the first role with no proven block — a chain with
        a missing stage cannot be composed, so this is an honest hard failure."""
        results = [self.retrieve(role) for role in roles]

        selected: list[SelectedModule] = []
        sub_functions: list[SubFunction] = []
        for idx, (role, result) in enumerate(zip(roles, results)):
            if result.block is None:
                raise RetrievalError(f"role {idx} (function='{role.function}'): {result.reason}")
            sf_id = f"s{idx}_{role.function}"
            selected.append(_selected_module(result.block, sf_id))
            sub_functions.append(SubFunction(
                id=sf_id, name=result.block.name,
                search_query=role.function, role=role.function,
            ))

        connections = [
            Connection(
                from_id=selected[i].sub_function_id, from_port="m_axis_tdata",
                to_id=selected[i + 1].sub_function_id, to_port="s_axis_tdata",
            )
            for i in range(len(selected) - 1)
        ]
        plan = CompositionPlan(
            composition_name=composition_name,
            sub_functions=sub_functions,
            connections=connections,
        )
        return selected, plan, results


def _selected_module(block: ProvenBlock, sf_id: str) -> SelectedModule:
    """Wrap a retrieved block as the SelectedModule stage 3 consumes. The structural
    fingerprint and proof confidence are carried on the SearchResult; score is 1.0
    (retrieval, not vector search)."""
    return SelectedModule(
        sub_function_id=sf_id,
        search_result=SearchResult(
            module_name=block.name,
            module_type=block.ir.module_type,
            score=1.0,
            assertion_count=0,
            confidence=block.confidence,
            assertion_summary=[],
            file_path=str(block.rtl_path),
            record_id=block.name,
            structural_fingerprint=block.fingerprint or None,
        ),
        ir=block.ir,
        rtl_path=block.rtl_path,
    )
