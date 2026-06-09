"""Deterministic AXI-Stream pipeline composition: ordering, wrapper emission, and
carried-proof bind assembly.

When change #1's interface-aware selection yields a clean linear AXI-Stream
pipeline (each stage's m_axis feeds the next stage's s_axis), the wrapper that
wires it is not a judgement call — there is exactly one correct way to connect
the bundles. Emitting it deterministically (instead of asking an LLM) removes the
broken-glue failure mode entirely and makes the assembled design reproducible.

SOUNDNESS NOTE (discovered in change #3): the open-source Yosys `read_verilog`
front-end used by the `sby` backend silently *ignores* SystemVerilog `bind`
statements (it parses the spec module but never instantiates it), so any property
attached via `bind` is never checked — it passes vacuously. The bind-based
"carried proof" (:func:`build_carried_bind`) is therefore **not sound under the
sby backend** and must not be reported as a real verification. The sound way to
attach composition-level properties under this toolchain is a *closed harness*
that instantiates the wrapper explicitly and inlines its assertions
(:func:`emit_roundtrip_harness`) — inlined assertions ARE checked.

Everything here is general over any linear AXI-Stream pipeline — no module names,
no chain-specific logic. Non-linear / non-AXIS compositions return None from
:func:`order_axis_pipeline`, and the caller falls back to the LLM wrapper path.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from specloop.compose.assertions import parse_proven_assertions
from specloop.compose.compatibility import (
    _is_axis_port,
    axis_bundles,
    bundles_by_role,
    pair_axis_interfaces,
)
from specloop.compose.schema import CompositionPlan, SelectedModule
from specloop.ir.schema import ModuleIR, Port
from specloop.training.schema import AssertionEntry

# Bundle signals the interaction layer reasons about (always core AXI-Stream).
_VALID, _READY, _DATA, _LAST = "tvalid", "tready", "tdata", "tlast"


# ---------------------------------------------------------------------------
# Pipeline ordering
# ---------------------------------------------------------------------------

def _topo_axis_order(plan: CompositionPlan, ids: set[str]) -> Optional[list[str]]:
    """Order sub-function ids into a single chain from the plan's AXI-Stream
    connections, or None if they don't form one simple covering path."""
    succ: dict[str, str] = {}
    indeg: dict[str, int] = {i: 0 for i in ids}
    edges: set[tuple[str, str]] = set()
    for c in plan.connections:
        if (c.from_id in ids and c.to_id in ids and c.from_id != c.to_id
                and _is_axis_port(c.from_port) and _is_axis_port(c.to_port)):
            if (c.from_id, c.to_id) in edges:
                continue
            if c.from_id in succ:        # fan-out → not a simple line
                return None
            edges.add((c.from_id, c.to_id))
            succ[c.from_id] = c.to_id
            indeg[c.to_id] += 1
    if not edges or any(d > 1 for d in indeg.values()):   # fan-in → not a line
        return None
    heads = [i for i in ids if indeg[i] == 0]
    if len(heads) != 1:
        return None
    order, cur, seen = [], heads[0], set()
    while cur is not None:
        if cur in seen:
            return None
        seen.add(cur)
        order.append(cur)
        cur = succ.get(cur)
    return order if len(order) == len(ids) else None


def order_axis_pipeline(
    plan: CompositionPlan, selected: list[SelectedModule],
) -> Optional[list[SelectedModule]]:
    """Return the selected modules ordered head→tail iff they form a clean linear
    single-stream AXI-Stream pipeline, else None.

    Requirements: every module is single-stream (exactly one s_axis slave + one
    m_axis master bundle), the connections (or, if absent, the plan order) form one
    simple path, and every consecutive pair is bundle-compatible (no errors). A
    single shared reset polarity (no inversion needed). Otherwise None → LLM path.
    """
    by_id = {sm.sub_function_id: sm for sm in selected}
    topo = _topo_axis_order(plan, set(by_id))
    if topo is not None:
        order_ids = topo
    elif any(c.from_id in by_id and c.to_id in by_id
             and _is_axis_port(c.from_port) and _is_axis_port(c.to_port)
             for c in plan.connections):
        # AXI-Stream connections exist but don't form a simple line (fan-in/out,
        # cycle, …) — don't fabricate a linear order from list position.
        return None
    else:
        # No AXI-Stream connections declared — trust the plan's sub-function order.
        order_ids = [sf.id for sf in plan.sub_functions if sf.id in by_id]
    if len(order_ids) != len(selected):
        return None
    ordered = [by_id[i] for i in order_ids]

    for sm in ordered:
        if len(bundles_by_role(sm.ir, "slave")) != 1 or len(bundles_by_role(sm.ir, "master")) != 1:
            return None
    polarities = {
        p.reset_polarity for sm in ordered for p in sm.ir.ports
        if p.is_reset and p.reset_polarity
    }
    if len(polarities) > 1:
        return None
    for up, down in zip(ordered, ordered[1:]):
        if any(i.severity == "error" for i in pair_axis_interfaces(
                up.sub_function_id, up.ir, down.sub_function_id, down.ir)):
            return None
    return ordered


# ---------------------------------------------------------------------------
# Boundaries — the m_axis→s_axis hops + the composition output
# ---------------------------------------------------------------------------

@dataclass
class Boundary:
    """One streaming boundary in the pipeline. Signal names are wrapper-scope
    nets (internal hop wires, or top ports for the composition output)."""
    name: str
    valid: str
    ready: str
    data: str
    last: str
    data_width: int


def _hop_wire(up_id: str, down_id: str, suffix: str) -> str:
    return f"hop_{up_id}_{down_id}_{suffix}"


def pipeline_boundaries(ordered: list[SelectedModule]) -> list[Boundary]:
    """Internal hop boundaries (between consecutive stages) + the composition
    output boundary (the tail's m_axis exposed at the top). Used identically by the
    wrapper emitter (to declare hop wires) and the bind builder (to assert on them).
    """
    boundaries: list[Boundary] = []
    for up, down in zip(ordered, ordered[1:]):
        m = bundles_by_role(up.ir, "master")[0]
        s = bundles_by_role(down.ir, "slave")[0]
        if not ({_VALID, _READY, _DATA, _LAST} <= (set(m) & set(s))):
            continue
        u, d = up.sub_function_id, down.sub_function_id
        boundaries.append(Boundary(
            name=f"{u}_to_{d}",
            valid=_hop_wire(u, d, _VALID), ready=_hop_wire(u, d, _READY),
            data=_hop_wire(u, d, _DATA), last=_hop_wire(u, d, _LAST),
            data_width=m[_DATA].width,
        ))
    tail = ordered[-1]
    tm = bundles_by_role(tail.ir, "master")[0]
    boundaries.append(Boundary(
        name="out",
        valid=tm[_VALID].name, ready=tm[_READY].name,
        data=tm[_DATA].name, last=tm[_LAST].name,
        data_width=tm[_DATA].width,
    ))
    return boundaries


# ---------------------------------------------------------------------------
# Deterministic wrapper emission
# ---------------------------------------------------------------------------

def _decl(direction: str, width: int, name: str) -> str:
    rng = f"[{width - 1}:0] " if width > 1 else ""
    return f"{direction} logic {rng}{name}"


def emit_pipeline_wrapper(
    composition_name: str,
    ordered: list[SelectedModule],
    param_overrides: Optional[dict[str, dict[str, str]]] = None,
) -> Optional[str]:
    """Emit a synthesizable SystemVerilog wrapper wiring a linear AXI-Stream
    pipeline. Returns None if a structural assumption fails (caller falls back).

    Wiring rules (all deterministic): head s_axis + tail m_axis become top ports;
    each hop wires upstream m_axis → downstream s_axis through internal wires;
    clock/reset are shared; sideband *inputs* are exposed as free top ports
    (config like length_max stays free, which only strengthens the proof) while
    sideband *outputs* (status/ack) are left unconnected so a parameter override
    that changes their derived width can't cause a mismatch. Parameter overrides
    are applied verbatim (caller is responsible for passing only safe params).
    """
    param_overrides = param_overrides or {}
    head, tail = ordered[0], ordered[-1]

    top_ports: list[str] = [_decl("input", 1, "clk"), _decl("input", 1, "rst")]
    wires: list[str] = []
    instances: list[str] = []

    # Head slave bundle + tail master bundle → top ports (verbatim names).
    for _suf, p in bundles_by_role(head.ir, "slave")[0].items():
        top_ports.append(_decl(p.direction, p.width, p.name))
    for _suf, p in bundles_by_role(tail.ir, "master")[0].items():
        top_ports.append(_decl(p.direction, p.width, p.name))

    # Internal hop wires for every shared bundle signal.
    for up, down in zip(ordered, ordered[1:]):
        m = bundles_by_role(up.ir, "master")[0]
        s = bundles_by_role(down.ir, "slave")[0]
        for suf in m.keys() & s.keys():
            wires.append(f"  {_wire_decl(m[suf].width, _hop_wire(up.sub_function_id, down.sub_function_id, suf))}")

    for idx, sm in enumerate(ordered):
        sid = sm.sub_function_id
        slave = bundles_by_role(sm.ir, "slave")[0]
        master = bundles_by_role(sm.ir, "master")[0]
        bundle_names = {p.name for p in slave.values()} | {p.name for p in master.values()}

        conns: list[str] = []
        for p in sm.ir.ports:
            if p.is_clock:
                conns.append(f".{p.name}(clk)")
            elif p.is_reset:
                conns.append(f".{p.name}(rst)")
        # slave bundle: top (head) or upstream hop wire
        up_master = bundles_by_role(ordered[idx - 1].ir, "master")[0] if idx > 0 else {}
        for suf, p in slave.items():
            if idx == 0:
                conns.append(f".{p.name}({p.name})")
            elif suf in up_master:
                conns.append(f".{p.name}({_hop_wire(ordered[idx-1].sub_function_id, sid, suf)})")
            elif p.direction == "input":
                conns.append(f".{p.name}('0)")          # downstream-only input: tie off
            else:
                conns.append(f".{p.name}()")             # downstream-only output: leave open
        # master bundle: top (tail) or downstream hop wire
        down_slave = bundles_by_role(ordered[idx + 1].ir, "slave")[0] if idx < len(ordered) - 1 else {}
        for suf, p in master.items():
            if idx == len(ordered) - 1:
                conns.append(f".{p.name}({p.name})")
            elif suf in down_slave:
                conns.append(f".{p.name}({_hop_wire(sid, ordered[idx+1].sub_function_id, suf)})")
            else:
                conns.append(f".{p.name}()")             # upstream-only output: leave open
        # sidebands: inputs → free top ports; outputs → left open
        for p in sm.ir.ports:
            if p.is_clock or p.is_reset or p.name in bundle_names:
                continue
            if p.direction == "input":
                top = f"{sid}_{p.name}"
                top_ports.append(_decl("input", p.width, top))
                conns.append(f".{p.name}({top})")
            else:
                conns.append(f".{p.name}()")

        overrides = {k: v for k, v in param_overrides.get(sm.ir.module, {}).items()}
        param_block = ""
        if overrides:
            params = ", ".join(f".{k}({v})" for k, v in overrides.items())
            param_block = f" #({params})"
        body = ",\n    ".join(conns)
        instances.append(
            f"  {sm.ir.module}{param_block} inst_{sid} (\n    {body}\n  );"
        )

    header = ",\n  ".join(top_ports)
    parts = [
        f"module {composition_name} (\n  {header}\n);",
        "",
        *wires,
        "",
        *instances,
        "",
        "endmodule",
    ]
    return "\n".join(parts) + "\n"


def _wire_decl(width: int, name: str) -> str:
    rng = f"[{width - 1}:0] " if width > 1 else ""
    return f"logic {rng}{name};"


# ---------------------------------------------------------------------------
# Carried-proof + interaction bind
# ---------------------------------------------------------------------------

@dataclass
class CarriedBind:
    bind_sv: str
    assertion_index: list[AssertionEntry]
    n_interaction: int
    n_carried: int
    carried_modules: list[tuple[str, int]] = field(default_factory=list)  # (module, #asserts)
    boundaries: list[Boundary] = field(default_factory=list)


def _interaction_spec(composition_name: str, ordered: list[SelectedModule],
                      boundaries: list[Boundary]) -> tuple[str, list[AssertionEntry]]:
    """Deterministic cross-boundary interaction assertions:
      * composition reset cleanliness — output valid and input ready both low in reset
      * per-boundary back-pressure hold — a stalled beat (valid & !ready) is held
        stable (valid stays high, data/last unchanged) on the next cycle
    All are Yosys-immediate assertions ($past allowed) and close by k-induction.
    """
    head = ordered[0]
    s_ready = bundles_by_role(head.ir, "slave")[0][_READY].name        # top input-ready
    out = boundaries[-1]                                               # composition output

    refs: dict[str, int] = {"clk": 1, "rst": 1, s_ready: 1, out.valid: 1}
    for b in boundaries:
        refs[b.valid] = 1
        refs[b.ready] = 1
        refs[b.data] = b.data_width
        refs[b.last] = 1

    body: list[str] = []
    index: list[AssertionEntry] = []

    body.append("    if (rst) begin")
    body.append(f"      ap_comp_reset_out_valid: assert (!{out.valid});")
    body.append(f"      ap_comp_reset_in_ready:  assert (!{s_ready});")
    body.append("    end")
    index += [AssertionEntry(name="ap_comp_reset_out_valid", category="interaction",
                             rationale="composition output valid deasserted during reset"),
              AssertionEntry(name="ap_comp_reset_in_ready", category="interaction",
                             rationale="composition input ready deasserted during reset")]

    for b in boundaries:
        guard = f"!rst && $past(!rst) && $past({b.valid}) && $past(!{b.ready})"
        body.append(f"    if ({guard}) begin")
        body.append(f"      ap_bp_{b.name}_valid: assert ({b.valid});")
        body.append(f"      ap_bp_{b.name}_data:  assert ({b.data} == $past({b.data}));")
        body.append(f"      ap_bp_{b.name}_last:  assert ({b.last} == $past({b.last}));")
        body.append("    end")
        for kind in ("valid", "data", "last"):
            index.append(AssertionEntry(
                name=f"ap_bp_{b.name}_{kind}", category="interaction",
                rationale=f"back-pressure: stalled beat at boundary '{b.name}' holds {kind} stable"))

    ports = ",\n  ".join(
        f"input logic {('[' + str(w - 1) + ':0] ') if w > 1 else ''}{n}" for n, w in refs.items()
    )
    spec = (
        f"module {composition_name}_spec (\n  {ports}\n);\n"
        f"  always @(posedge clk) begin\n"
        + "\n".join(body) + "\n"
        f"  end\n"
        f"endmodule\n"
        f"bind {composition_name} {composition_name}_spec spec_inst (.*);\n"
    )
    return spec, index


def build_carried_bind(
    composition_name: str,
    ordered: list[SelectedModule],
    work_dir: Path,
) -> CarriedBind:
    """Assemble the composition bind: deterministic interaction assertions + each
    distinct component's own proven bind module (carried-proof). The component
    binds re-target their assertions onto the instances via `bind <module> ... (.*)`;
    no LLM, no re-writing. Missing component binds are skipped (best-effort)."""
    boundaries = pipeline_boundaries(ordered)
    interaction_sv, interaction_index = _interaction_spec(composition_name, ordered, boundaries)

    carried_parts: list[str] = []
    carried_modules: list[tuple[str, int]] = []
    inherited_index: list[AssertionEntry] = []
    seen: set[str] = set()
    for sm in ordered:
        mod = sm.ir.module
        if mod in seen:
            continue
        seen.add(mod)
        bind_path = work_dir / f"{mod}.bind.sv"
        if not bind_path.exists():
            continue
        txt = bind_path.read_text(encoding="utf-8", errors="replace")
        labels = [a.label for a in parse_proven_assertions(txt)]
        if not labels:
            continue
        carried_parts.append(f"// ---- carried proof: {mod} ({len(labels)} proven assertions) ----\n{txt}")
        carried_modules.append((mod, len(labels)))
        inherited_index += [AssertionEntry(name=lab, category="inherited",
                                           rationale=f"carried from {mod}") for lab in labels]

    bind_sv = interaction_sv
    if carried_parts:
        bind_sv += "\n\n" + "\n\n".join(carried_parts)

    # Dedup the index by name (component specs can share label names across modules;
    # the overall verdict is unaffected, but a clean index keeps the table honest).
    index: list[AssertionEntry] = []
    seen_names: set[str] = set()
    for e in interaction_index + inherited_index:
        if e.name not in seen_names:
            seen_names.add(e.name)
            index.append(e)

    return CarriedBind(
        bind_sv=bind_sv,
        assertion_index=index,
        n_interaction=len(interaction_index),
        n_carried=sum(n for _, n in carried_modules),
        carried_modules=carried_modules,
        boundaries=boundaries,
    )


# ---------------------------------------------------------------------------
# End-to-end equivalence: lossless round-trip / pass-through harness
# ---------------------------------------------------------------------------

@dataclass
class RoundTripHarness:
    harness_sv: str
    top_module: str
    assertion_index: list[AssertionEntry]
    max_len: int
    deadline: int
    depth: int
    data_width: int


def _harness_connections(ordered: list[SelectedModule]) -> list[str]:
    """Port connections from the harness to the composition wrapper instance.

    Drives the head slave bundle from the scoreboard wires, reads the tail master
    bundle into scoreboard wires, ties the wrapper's exposed sideband inputs to 0.
    Mirrors :func:`emit_pipeline_wrapper`'s top-port shape exactly, so the names
    line up with the generated wrapper.
    """
    head, tail = ordered[0], ordered[-1]
    hs = bundles_by_role(head.ir, "slave")[0]
    tm = bundles_by_role(tail.ir, "master")[0]
    conns = [".clk(clk)", ".rst(rst)"]

    drive = {_DATA: "s_tdata", _VALID: "s_tvalid", _LAST: "s_tlast", "tuser": "1'b0"}
    for suf, p in hs.items():
        if suf == _READY:
            conns.append(f".{p.name}(s_tready)")
        elif suf in drive:
            conns.append(f".{p.name}({drive[suf]})")
        elif suf == "tkeep":
            conns.append(f".{p.name}({{{p.width}{{1'b1}}}})")   # KEEP_ENABLE=0 → don't-care
        else:
            conns.append(f".{p.name}('0)")

    read = {_DATA: "m_tdata", _VALID: "m_tvalid", _LAST: "m_tlast", "tuser": "m_tuser"}
    for suf, p in tm.items():
        if suf == _READY:
            conns.append(f".{p.name}(1'b1)")               # no output back-pressure
        elif suf in read:
            conns.append(f".{p.name}({read[suf]})")
        else:
            conns.append(f".{p.name}()")

    for sm in ordered:
        sid = sm.sub_function_id
        bnames = ({pp.name for pp in bundles_by_role(sm.ir, "slave")[0].values()}
                  | {pp.name for pp in bundles_by_role(sm.ir, "master")[0].values()})
        for p in sm.ir.ports:
            if p.is_clock or p.is_reset or p.name in bnames:
                continue
            if p.direction == "input":                      # wrapper exposes sideband inputs
                conns.append(f".{sid}_{p.name}('0)")
    return conns


def emit_roundtrip_harness(
    composition_name: str,
    ordered: list[SelectedModule],
    max_len: int = 8,
    deadline: Optional[int] = None,
) -> Optional[RoundTripHarness]:
    """Emit a closed BMC harness proving the tail output frame is word-identical to
    the head input frame (lossless round-trip / pass-through) for any single frame
    of ``L <= max_len`` data words.

    General over any linear AXI-Stream pipeline whose end-to-end spec is stream
    equivalence — driven only by the head slave / tail master bundles. The harness
    instantiates the wrapper, drives ONE symbolic frame (``$anyconst`` data words +
    symbolic length L) into the head with ``tlast`` on the last word and
    ``tuser=0``, holds the tail ready high, and on every output beat asserts the
    word equals the symbolic frame in order; a completeness deadline forces the
    whole frame to round-trip within ``deadline`` cycles. No ``bind``, no
    hierarchical refs — every assertion is in harness scope, so the open-source
    flow genuinely checks it.

    Returns None when the spec cannot apply (head/tail data widths differ, i.e. the
    pipeline changes word width so output != input by construction).
    """
    head, tail = ordered[0], ordered[-1]
    hs = bundles_by_role(head.ir, "slave")[0]
    tm = bundles_by_role(tail.ir, "master")[0]
    if not ({_DATA, _VALID} <= set(hs)) or not ({_DATA, _VALID} <= set(tm)):
        return None
    w = hs[_DATA].width
    if tm[_DATA].width != w:
        return None

    deadline = deadline or (2 * max_len + 16 + 4 * len(ordered))
    depth = deadline + 6
    top = f"{composition_name}_rt_harness"
    conns = ",\n    ".join(_harness_connections(ordered))

    names = ["ap_rt_overrun", "ap_rt_data", "ap_rt_user", "ap_rt_last", "ap_rt_complete"]
    index = [
        AssertionEntry(name="ap_rt_overrun", category="equivalence",
                       rationale="tail never emits more words than the injected frame"),
        AssertionEntry(name="ap_rt_data", category="equivalence",
                       rationale="each output word equals the injected frame word in order"),
        AssertionEntry(name="ap_rt_user", category="equivalence",
                       rationale="no error (tuser) on a clean round-trip"),
        AssertionEntry(name="ap_rt_last", category="equivalence",
                       rationale="output tlast lands exactly on the last frame word"),
        AssertionEntry(name="ap_rt_complete", category="equivalence",
                       rationale="the whole frame round-trips within the deadline (non-vacuity)"),
    ]

    sv = f"""\
// Closed end-to-end round-trip harness for '{composition_name}' (lossless pass-through).
// Proves: the word stream out of the tail equals the word stream into the head, for
// any single symbolic frame of L <= {max_len} words. No bind — assertions are inlined.
module {top} (input clk);
  localparam integer N = {max_len};
  localparam integer W = {w};
  localparam integer DEADLINE = {deadline};

  (* anyconst *) reg [N*W-1:0] sym;     // symbolic frame words, concatenated
  (* anyconst *) reg [15:0]    Lr;      // symbolic length selector
  wire [15:0] L = (Lr % N) + 1;         // frame length in [1, N]

  reg        rst = 1'b1;
  reg [15:0] cyc = 0;
  reg [15:0] in_idx = 0, out_idx = 0;

  // head drive (combinational from the scoreboard)
  wire [W-1:0] s_tdata  = sym[in_idx*W +: W];
  wire         s_tvalid = !rst && (in_idx < L);
  wire         s_tlast  = (in_idx == L-1);
  wire         s_tready;
  // tail observe
  wire [W-1:0] m_tdata;
  wire         m_tvalid, m_tlast, m_tuser;

  {composition_name} dut (
    {conns}
  );

  always @(posedge clk) begin
    cyc <= cyc + 1;
    rst <= 1'b0;                          // one-cycle reset, then run
    if (rst) begin
      in_idx <= 0; out_idx <= 0;
    end else begin
      if (s_tvalid && s_tready) in_idx <= in_idx + 1;
      if (m_tvalid) begin
        ap_rt_overrun: assert (out_idx < L);
        ap_rt_data:    assert (m_tdata == sym[out_idx*W +: W]);
        ap_rt_user:    assert (!m_tuser);
        ap_rt_last:    assert (m_tlast == (out_idx == L-1));
        out_idx <= out_idx + 1;
      end
    end
  end

  // completeness: every frame fully round-trips by DEADLINE cycles (forces the
  // bounded proof to witness the entire output frame, not just a prefix).
  always @(posedge clk)
    if (!rst && cyc >= DEADLINE) ap_rt_complete: assert (out_idx == L);
endmodule
"""
    return RoundTripHarness(
        harness_sv=sv, top_module=top, assertion_index=index,
        max_len=max_len, deadline=deadline, depth=depth, data_width=w,
    )
