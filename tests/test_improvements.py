"""Standalone regression tests for the four quality improvements.

Pure-logic coverage only — no LLM, no Qdrant, no API key. The synthesis test is
skipped automatically when yosys is not on PATH. Run directly:

    PYTHONPATH=src python3 tests/test_improvements.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from specloop.ir.schema import ModuleIR, Port  # noqa: E402
from specloop.search.searcher import SearchResult  # noqa: E402
from specloop.compose.schema import (  # noqa: E402
    CompositionPlan, SelectedModule, SubFunction, Connection,
)

WORK = ROOT / "work"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _sr() -> SearchResult:
    return SearchResult(module_name="m", module_type="x", score=1.0, assertion_count=0,
                        confidence=1.0, assertion_summary=[], file_path="", record_id="")


def _sm(sfid: str, ports: list[Port], module: str | None = None) -> SelectedModule:
    return SelectedModule(sub_function_id=sfid, search_result=_sr(),
                          ir=ModuleIR(module=module or sfid, file="x.sv", ports=ports),
                          rtl_path=Path("x.sv"))


def _plan(*ids: str, conns: list[Connection] | None = None) -> CompositionPlan:
    return CompositionPlan(
        composition_name="top",
        sub_functions=[SubFunction(id=i, name=i, search_query="", role="") for i in ids],
        connections=conns or [],
    )


# --------------------------------------------------------------------------
# Improvement 1 — functional-unit grouping
# --------------------------------------------------------------------------

def test_grouping_on_ibex_alu():
    from specloop.gen.pipeline import _group_blocks, _MAX_GROUP_SIZE

    ir_file = WORK / "ibex_alu.ir.json"
    if not ir_file.exists():
        print("  SKIP grouping (work/ibex_alu.ir.json absent)")
        return
    ir = ModuleIR.model_validate(json.loads(ir_file.read_text()))
    groups = _group_blocks(ir.always_blocks)

    # Every group within the cap.
    for g in groups:
        assert len(g) <= _MAX_GROUP_SIZE, "group exceeds cap"
    # Every multi-block group has an internal producer->consumer link.
    for g in groups:
        if len(g) > 1:
            linked = any(
                set(a.signals_written) & set(b.signals_read)
                for a in g for b in g if a is not b
            )
            assert linked, "multi-block group has no signal sharing"
    # The fan-in output mux must NOT have collapsed everything into one group.
    assert len(groups) >= 4, "fan-in mux collapsed the decomposition"
    print(f"  OK grouping: {len(ir.always_blocks)} blocks -> {len(groups)} units, all coherent & capped")


def test_grouping_determinism():
    from specloop.ir.schema import AlwaysBlock
    from specloop.gen.pipeline import _group_blocks

    blocks = [
        AlwaysBlock(kind="always_comb", start_line=1, end_line=2, signals_written=["a"], signals_read=["x"]),
        AlwaysBlock(kind="always_comb", start_line=3, end_line=4, signals_written=["b"], signals_read=["a"]),
        AlwaysBlock(kind="always_comb", start_line=5, end_line=6, signals_written=["c"], signals_read=["z"]),
    ]
    g1 = [[id(b) for b in grp] for grp in _group_blocks(list(blocks))]
    g2 = [[id(b) for b in grp] for grp in _group_blocks(list(blocks))]
    assert g1 == g2, "grouping is non-deterministic"
    # a and b share a signal (a) -> same unit; c is independent -> separate.
    sizes = sorted(len(grp) for grp in _group_blocks(list(blocks)))
    assert sizes == [1, 2], f"expected one pair + one singleton, got {sizes}"
    print("  OK grouping determinism + signal-sharing pairs / independent singletons")


def test_merge_preserves_combinational_and_dedups():
    from specloop.gen.schema import BindResult
    from specloop.gen.pipeline import _merge_bind_results
    from specloop.training.schema import AssertionEntry

    g0 = BindResult(bind_module_sv="""module s ( input logic [6:0] op );
  wire is_add = (op == 7'd0);
  always @(*) begin
    if (is_add) begin
      g0_ap_add: assert(1'b1);
    end
  end
endmodule
bind d s spec_inst(.*);""", assertion_index=[AssertionEntry(name="g0_ap_add", category="functional")])
    g1 = BindResult(bind_module_sv="""module s ( input logic [6:0] op );
  wire is_add = (op == 7'd0);
  wire is_sub = (op == 7'd1);
  always @(*) begin
    if (is_sub) begin
      g1_ap_sub: assert(1'b1);
    end
  end
endmodule
bind d s spec_inst(.*);""", assertion_index=[AssertionEntry(name="g1_ap_sub", category="functional")])

    sv = _merge_bind_results([g0, g1], "d").bind_module_sv
    assert "g0_ap_add" in sv and "g1_ap_sub" in sv, "combinational blocks lost in merge"
    assert sv.count("wire is_add") == 1, "shared decode wire not deduped"
    assert sv.count("wire is_sub") == 1
    assert sv.count("always @(*)") == 2
    print("  OK merge: @(*) blocks preserved, shared wires deduped")


# --------------------------------------------------------------------------
# Improvement 2 — protocol templates
# --------------------------------------------------------------------------

def test_valid_ready_direction():
    from specloop.compose.protocol_templates import detect_protocols

    prod = _sm("src", [
        Port(name="clk", direction="input", is_clock=True),
        Port(name="rst_n", direction="input", is_reset=True, reset_polarity="low"),
        Port(name="out_valid", direction="output"),
        Port(name="out_ready", direction="input"),
        Port(name="out_data", direction="output", width=8),
    ])
    cons = _sm("snk", [
        Port(name="clk", direction="input", is_clock=True),
        Port(name="rst_n", direction="input", is_reset=True, reset_polarity="low"),
        Port(name="in_valid", direction="input"),
        Port(name="in_ready", direction="output"),
        Port(name="in_data", direction="input", width=8),
    ])
    pp = detect_protocols([prod, cons], _plan("src", "snk"))
    b = {(x.inst_id, x.port): x.signal for x in pp.bindings}

    vw = b[("src", "out_valid")]
    rw = b[("src", "out_ready")]
    dw = b[("src", "out_data")]
    assert vw == b[("snk", "in_valid")], "valid not shared producer->consumer"
    assert rw == b[("snk", "in_ready")], "ready not shared consumer->producer"
    assert vw != rw != dw and vw != dw, "valid/ready/data collapsed (direction bug)"
    assert b[("src", "clk")] == "clk" and b[("snk", "clk")] == "clk"
    assert b[("src", "rst_n")] == "rst_n" and b[("snk", "rst_n")] == "rst_n"
    # data wire keeps width
    dwire = next(w for w in pp.internal_wires if w.name == dw)
    assert dwire.width == 8
    print("  OK valid/ready: producer drives valid, consumer drives ready (backpressure), data 8-bit")


def test_axi_lite_canonical_match():
    from specloop.compose.protocol_templates import detect_protocols

    master = _sm("cpu", [
        Port(name="clk", direction="input", is_clock=True),
        Port(name="s_axi_awvalid", direction="output"),
        Port(name="s_axi_awready", direction="input"),
        Port(name="s_axi_awaddr", direction="output", width=32),
        Port(name="s_axi_bvalid", direction="input"),
        Port(name="s_axi_bready", direction="output"),
    ])
    slave = _sm("mem", [
        Port(name="clk", direction="input", is_clock=True),
        Port(name="axi_awvalid", direction="input"),
        Port(name="axi_awready", direction="output"),
        Port(name="axi_awaddr", direction="input", width=32),
        Port(name="axi_bvalid", direction="output"),
        Port(name="axi_bready", direction="input"),
    ])
    pp = detect_protocols([master, slave], _plan("cpu", "mem"))
    b = {(x.inst_id, x.port): x.signal for x in pp.bindings}
    assert b[("cpu", "s_axi_awvalid")] == b[("mem", "axi_awvalid")], "awvalid not matched canonically"
    assert b[("cpu", "s_axi_awready")] == b[("mem", "axi_awready")], "awready not matched"
    assert b[("cpu", "s_axi_awvalid")] != b[("cpu", "s_axi_awready")], "aw valid/ready crossed"
    assert b[("cpu", "s_axi_bvalid")] == b[("mem", "axi_bvalid")], "bvalid (slave-driven) not matched"
    print("  OK AXI-Lite: canonical name matching across s_axi_/axi_ prefixes, valid!=ready")


def test_reset_polarity_inversion():
    from specloop.compose.protocol_templates import detect_protocols

    a = _sm("a", [Port(name="clk", direction="input", is_clock=True),
                  Port(name="rst", direction="input", is_reset=True, reset_polarity="high")])
    b = _sm("b", [Port(name="clk", direction="input", is_clock=True),
                  Port(name="rst_n", direction="input", is_reset=True, reset_polarity="low")])
    pp = detect_protocols([a, b], _plan("a", "b"))
    bind = {(x.inst_id, x.port): x.signal for x in pp.bindings}
    assert bind[("b", "rst_n")] == "rst_n"
    assert bind[("a", "rst")] == "rst_inv", "active-high reset not inverted"
    assert any(asn.lhs == "rst_inv" and asn.rhs == "~rst_n" for asn in pp.extra_assigns)
    print("  OK reset: active-high port inverted from shared rst_n")


def test_no_protocol_empty_plan():
    from specloop.compose.protocol_templates import detect_protocols

    comb = _sm("alu", [Port(name="a", direction="input", width=8),
                       Port(name="y", direction="output", width=8)])
    pp = detect_protocols([comb], _plan("alu"))
    assert not pp.has_any, "spurious protocol match on pure combinational module"
    print("  OK no-protocol: pure combinational module yields empty plan (full-LLM fallback)")


# --------------------------------------------------------------------------
# Improvement 3 — inherited proven assertions
# --------------------------------------------------------------------------

def test_inheritance_guard_aware():
    from specloop.compose.assertions import parse_proven_assertions

    bind = WORK / "counter.bind.sv"
    if not bind.exists():
        print("  SKIP inheritance (work/counter.bind.sv absent)")
        return
    asserts = {a.label: a for a in parse_proven_assertions(bind.read_text())}

    # Safety bound + reset-state: inherit.
    assert not asserts["ap_count_in_valid_range"].temporal, "safety bound should inherit"
    assert not asserts["ap_reset_clears_count_async"].temporal, "reset-state should inherit"
    # $rose in the GUARD (clean expr) must still be classified temporal.
    assert asserts["ap_reset_to_zero_on_release"].temporal, "guard-only $rose not caught"
    # References a $past decode wire -> temporal.
    assert asserts["ap_count_increments_when_enabled"].temporal, "$past decode wire not caught"
    inh = sum(1 for a in asserts.values() if not a.temporal)
    tmp = sum(1 for a in asserts.values() if a.temporal)
    print(f"  OK inheritance: counter -> {inh} inheritable, {tmp} temporal (guard- & wire-aware)")


def test_inheritance_missing_bind_graceful():
    from specloop.compose.assertions import load_inherited_properties

    sm = _sm("x", [Port(name="clk", direction="input", is_clock=True)], module="does_not_exist_zzz")
    props = load_inherited_properties([sm], WORK)
    assert props == [], "missing bind should yield no inherited props, not raise"
    print("  OK inheritance: missing component bind degrades to empty (from-scratch)")


# --------------------------------------------------------------------------
# Improvement 4 — synthesis-based PPA
# --------------------------------------------------------------------------

def test_synth_ppa():
    from specloop.ppa.synth import synthesize_stats, vector_from_synth, find_yosys

    if not find_yosys():
        print("  SKIP synth (yosys not on PATH)")
        return
    counter = ROOT / "tests" / "fixtures" / "counter.sv"
    if not counter.exists():
        print("  SKIP synth (counter fixture absent)")
        return
    stats = synthesize_stats(counter.read_text(), "counter")
    assert stats is not None, "counter should synthesize"
    assert stats.cells > 0 and stats.ffs > 0, "counter has cells and flip-flops"
    v = vector_from_synth(stats)
    assert 0.0 <= v.latency <= 1.0 and 0.0 <= v.area <= 1.0
    # Graceful failure: garbage and empty must return None, never raise.
    assert synthesize_stats("not verilog ;;;", "nope") is None
    assert synthesize_stats("", "nope") is None
    # Combinational (0 FF) => latency 0, throughput 1.
    from specloop.ppa.synth import SynthStats
    comb = vector_from_synth(SynthStats(cells=10, ffs=0))
    assert comb.latency == 0.0 and comb.throughput == 1.0
    print(f"  OK synth: counter cells={stats.cells} ffs={stats.ffs}; garbage->None; combinational lat=0/thr=1")


# --------------------------------------------------------------------------
# Improvement 5 — interface-aware selection (bundle-aware compatibility)
# --------------------------------------------------------------------------

def _axis_module(name: str, width: int = 8, clk: str | None = "clk",
                 rst: str | None = "rst", keep: bool = True, user: bool = True) -> ModuleIR:
    """A single-stream AXI-Stream module: one s_axis slave + one m_axis master."""
    ports: list[Port] = []
    if clk is not None:
        ports.append(Port(name=clk, direction="input", is_clock=True))
    if rst is not None:
        ports.append(Port(name=rst, direction="input", is_reset=True, reset_polarity="high"))

    def bundle(prefix: str, slave: bool) -> list[Port]:
        fwd = "input" if slave else "output"   # data/valid/last flow toward a slave
        rev = "output" if slave else "input"   # ready flows back to the master
        b = [Port(name=f"{prefix}_tdata", direction=fwd, width=width),
             Port(name=f"{prefix}_tvalid", direction=fwd),
             Port(name=f"{prefix}_tready", direction=rev),
             Port(name=f"{prefix}_tlast", direction=fwd)]
        if keep:
            b.append(Port(name=f"{prefix}_tkeep", direction=fwd, width=max(1, width // 8)))
        if user:
            b.append(Port(name=f"{prefix}_tuser", direction=fwd))
        return b

    ports += bundle("s_axis", slave=True) + bundle("m_axis", slave=False)
    return ModuleIR(module=name, file=f"{name}.v", ports=ports)


def _mem_fifo(name: str = "fifo_sync") -> ModuleIR:
    """A bare-memory FIFO — the wrong-interface block the discovery run wired in."""
    return ModuleIR(module=name, file=f"{name}.v", ports=[
        Port(name="clk", direction="input", is_clock=True),
        Port(name="rst_n", direction="input", is_reset=True, reset_polarity="low"),
        Port(name="wr_en", direction="input"), Port(name="wr_data", direction="input", width=8),
        Port(name="rd_en", direction="input"), Port(name="rd_data", direction="output", width=8),
        Port(name="full", direction="output"), Port(name="empty", direction="output"),
    ])


def test_axis_bundle_extraction_and_roles():
    from specloop.compose.compatibility import axis_bundles, bundles_by_role

    ir = _axis_module("axis_fifo")
    bundles = axis_bundles(ir)
    assert set(bundles) == {"s_axis", "m_axis"}, f"bundles: {set(bundles)}"
    assert {"tdata", "tvalid", "tready", "tlast"} <= set(bundles["s_axis"])
    assert len(bundles_by_role(ir, "slave")) == 1 and len(bundles_by_role(ir, "master")) == 1
    # a bare-memory FIFO exposes no axis bundles at all
    assert axis_bundles(_mem_fifo()) == {}
    print("  OK bundles: s_axis(slave)/m_axis(master) grouped by tvalid direction; mem-FIFO has none")


def test_pair_axis_chain_and_reject_memfifo():
    from specloop.compose.compatibility import pair_axis_interfaces, candidate_role_issues

    up, down = _axis_module("axis_register"), _axis_module("axis_fifo")
    assert not [i for i in pair_axis_interfaces("reg", up, "buf", down) if i.severity == "error"]
    # the discovery bug: a bare-memory FIFO in the buffer slot -> error (no s_axis slave bundle)
    bad = pair_axis_interfaces("reg", up, "buf", _mem_fifo())
    assert any(i.severity == "error" for i in bad), "mem-FIFO wrongly accepted as AXIS slave"
    assert candidate_role_issues(_mem_fifo(), {"slave", "master"}), "mem-FIFO should fail role check"
    assert not candidate_role_issues(_axis_module("x"), {"slave", "master"})
    print("  OK pairing: axis->axis clean; bare-memory FIFO rejected from the AXIS buffer slot")


def test_connection_roles_not_over_excluded():
    from specloop.compose.compatibility import axis_connection_roles

    plan = _plan("reg", "buf", "sink", conns=[
        Connection(from_id="reg", from_port="m_axis_tdata", to_id="buf", to_port="s_axis_tdata"),
        Connection(from_id="buf", from_port="m_axis_tdata", to_id="sink", to_port="s_axis_tdata"),
    ])
    assert axis_connection_roles(plan, "buf") == {"slave", "master"}
    assert axis_connection_roles(plan, "reg") == {"master"}
    # a req/grant arbiter role must NOT be forced to carry AXIS bundles (the arbiter bug)
    aplan = _plan("arb", "client", conns=[
        Connection(from_id="client", from_port="request", to_id="arb", to_port="request"),
        Connection(from_id="arb", from_port="grant", to_id="client", to_port="grant"),
    ])
    assert axis_connection_roles(aplan, "arb") == set(), "non-AXIS role wrongly required AXIS bundles"
    print("  OK roles: AXIS roles derive slave/master; req/grant arbiter demands no AXIS ports")


def test_pair_width_clock_enable_diagnostics():
    from specloop.compose.compatibility import pair_axis_interfaces

    up = _axis_module("up", width=8)
    # width mismatch -> error
    assert any(i.severity == "error" and "width" in i.message.lower()
               for i in pair_axis_interfaces("up", up, "wide", _axis_module("wide", width=16)))
    # async-like neighbour with no shared clock -> warning, never an error
    iss = pair_axis_interfaces("up", up, "async", _axis_module("async", clk=None))
    assert not [i for i in iss if i.severity == "error"], "clock-domain diff must not be an error"
    assert any(i.severity == "warning" and "clock" in i.message.lower() for i in iss)
    # ENABLE mismatch: neighbour drops tkeep -> warning, not error
    iss2 = pair_axis_interfaces("up", up, "nokeep", _axis_module("nokeep", keep=False))
    assert any(i.severity == "warning" and "tkeep" in i.message for i in iss2)
    assert not [i for i in iss2 if i.severity == "error"], "optional-signal diff must not be an error"
    print("  OK diagnostics: width=error, clock-domain=warning, ENABLE(tkeep)=warning")


def test_check_interfaces_chain_a_end_to_end():
    from specloop.compose.compatibility import CompatibilityChecker

    chain = [(n, _axis_module(n)) for n in
             ["axis_register", "axis_fifo", "axis_frame_length_adjust", "axis_rate_limit"]]
    res = CompatibilityChecker().check_interfaces(chain)
    assert res.ok, f"Chain A end-to-end had errors: {[i.message for i in res.errors]}"
    assert not res.warnings, f"uniform Chain A should have no warnings: {[i.message for i in res.warnings]}"
    print("  OK end-to-end: 4-stage uniform AXIS pipeline passes bundle check (0 errors, 0 warnings)")


def test_select_interface_aware_picks_axis_fifo():
    """Headline: with fifo_sync out-scoring axis_fifo (the live ranking), interface-
    aware selection still picks axis_fifo and reports fifo_sync excluded."""
    import tempfile
    from specloop.compose.pipeline import CompositionPipeline
    from specloop.search.searcher import SearchResult

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        (work / "axis_fifo.ir.json").write_text(_axis_module("axis_fifo").model_dump_json())
        (work / "axis_async_fifo.ir.json").write_text(
            _axis_module("axis_async_fifo", clk=None).model_dump_json())
        (work / "fifo_sync.ir.json").write_text(_mem_fifo().model_dump_json())
        reg_ir = _axis_module("axis_register")

        def sr(name: str, score: float) -> SearchResult:
            return SearchResult(module_name=name, module_type="x", score=score,
                                assertion_count=10, confidence=1.0, assertion_summary=[],
                                file_path=f"{name}.v", record_id="")

        # fifo_sync out-scores the AXIS blocks, exactly like the live buffer query.
        eligible = [sr("fifo_sync", 0.663), sr("axis_async_fifo", 0.659), sr("axis_fifo", 0.645)]
        plan = _plan("reg", "buf", "sink", conns=[
            Connection(from_id="reg", from_port="m_axis_tdata", to_id="buf", to_port="s_axis_tdata"),
            Connection(from_id="buf", from_port="m_axis_tdata", to_id="sink", to_port="s_axis_tdata"),
        ])
        pipe = CompositionPipeline(client=None, qdrant_url="", collection="", embed_model="")
        best, ir, ppa_used, note = pipe._select_interface_aware(
            eligible, {"slave", "master"}, "buf", plan, {"reg": reg_ir}, work, {}, None,
        )
        assert best.module_name == "axis_fifo", f"picked {best.module_name}, expected axis_fifo ({note})"
        assert "fifo_sync" in note, f"fifo_sync should be reported excluded: {note}"
        print(f"  OK selection: axis_fifo chosen over higher-scoring fifo_sync/async_fifo ({note})")


# --------------------------------------------------------------------------
# Improvement 6 — carried-proof spine (deterministic wrapper + carried + interaction)
# --------------------------------------------------------------------------

def _axis_sm(sfid: str, module: str, **kw) -> SelectedModule:
    ir = _axis_module(module, **kw)
    return SelectedModule(sub_function_id=sfid, search_result=_sr(), ir=ir, rtl_path=Path(ir.file))


def _conn(x: str, y: str) -> Connection:
    return Connection(from_id=x, from_port="m_axis_tdata", to_id=y, to_port="s_axis_tdata")


def test_order_axis_pipeline():
    from specloop.compose.axis_pipeline import order_axis_pipeline

    sel = [_axis_sm("a", "m_a"), _axis_sm("b", "m_b"), _axis_sm("c", "m_c")]
    ordered = order_axis_pipeline(_plan("a", "b", "c", conns=[_conn("a", "b"), _conn("b", "c")]), sel)
    assert ordered and [s.sub_function_id for s in ordered] == ["a", "b", "c"]
    # connection topology, not list order, decides the chain
    rev = order_axis_pipeline(_plan("c", "a", "b", conns=[_conn("a", "b"), _conn("b", "c")]), sel)
    assert [s.sub_function_id for s in rev] == ["a", "b", "c"]
    # fan-out is not a clean line
    assert order_axis_pipeline(_plan("a", "b", "c", conns=[_conn("a", "b"), _conn("a", "c")]), sel) is None
    # a bare-memory FIFO in the chain (no s_axis/m_axis bundle) → not a pipeline
    bad = [_axis_sm("a", "m_a"),
           SelectedModule(sub_function_id="b", search_result=_sr(), ir=_mem_fifo(), rtl_path=Path("x.v")),
           _axis_sm("c", "m_c")]
    assert order_axis_pipeline(_plan("a", "b", "c", conns=[_conn("a", "b"), _conn("b", "c")]), bad) is None
    print("  OK order: linear AXIS chain ordered head→tail; fan-out & non-AXIS member rejected")


def test_emit_pipeline_wrapper_structure():
    from specloop.compose.axis_pipeline import order_axis_pipeline, emit_pipeline_wrapper

    sel = [_axis_sm("a", "modA"), _axis_sm("b", "modB")]
    ordered = order_axis_pipeline(_plan("a", "b", conns=[_conn("a", "b")]), sel)
    sv = emit_pipeline_wrapper("topcomp", ordered, {"modB": {"DEPTH": "32"}})
    assert "module topcomp (" in sv and sv.rstrip().endswith("endmodule")
    assert "modA inst_a" in sv and "modB #(.DEPTH(32)) inst_b" in sv
    assert "hop_a_b_tdata" in sv and "hop_a_b_tready" in sv          # internal boundary wires
    assert ".s_axis_tdata(s_axis_tdata)" in sv                       # head slave at top
    assert ".m_axis_tdata(m_axis_tdata)" in sv                       # tail master at top
    assert ".s_axis_tdata(hop_a_b_tdata)" in sv                      # b consumes the hop wire
    assert "assert" not in sv                                        # wrapper is pure RTL
    print("  OK wrapper: top ports + per-hop bundle wires + safe param override, no assertions")


def test_build_carried_bind_counts():
    import tempfile
    from specloop.compose.axis_pipeline import order_axis_pipeline, build_carried_bind

    sel = [_axis_sm("a", "modA"), _axis_sm("b", "modB"), _axis_sm("c", "modC")]
    ordered = order_axis_pipeline(_plan("a", "b", "c", conns=[_conn("a", "b"), _conn("b", "c")]), sel)
    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        for mod, n in [("modA", 3), ("modB", 4), ("modC", 2)]:
            asserts = "\n".join(f"    ap_{mod}_{i}: assert(1'b1);" for i in range(n))
            (work / f"{mod}.bind.sv").write_text(
                f"module {mod}_spec(input logic clk);\n  always @(posedge clk) begin\n"
                f"{asserts}\n  end\nendmodule\nbind {mod} {mod}_spec s (.*);\n")
        cb = build_carried_bind("topc", ordered, work)
    # 2 internal hops + 1 output boundary = 3 boundaries × 3 + 2 reset = 11 interaction
    assert cb.n_interaction == 3 * 3 + 2, cb.n_interaction
    assert cb.n_carried == 3 + 4 + 2, cb.n_carried
    assert "bind topc topc_spec" in cb.bind_sv and "carried proof: modA" in cb.bind_sv
    assert any(e.category == "interaction" for e in cb.assertion_index)
    assert any(e.category == "inherited" for e in cb.assertion_index)
    print(f"  OK carried bind: {cb.n_interaction} interaction + {cb.n_carried} carried; categorized index")


def _find_sby() -> str | None:
    import shutil
    sby = shutil.which("sby") or str(ROOT / "oss-cad-suite" / "bin" / "sby")
    return sby if Path(sby).exists() else None


def _sby_verdict(sby: str, td: Path, files: dict[str, str], top: str, formal: str) -> str:
    """Write `files` into td, run a depth-3 BMC with `formal` read -formal, return
    the final DONE verdict (PASS/FAIL/...)."""
    import re
    import subprocess
    for name, text in files.items():
        (td / name).write_text(text)
    reads = "\n".join(f"read -sv {n}" for n in files if n != formal)
    cfg = (f"[options]\nmode bmc\ndepth 3\n[engines]\nsmtbmc\n[script]\n{reads}\n"
           f"read -sv -formal {formal}\nprep -top {top}\n[files]\n" + "\n".join(files) + "\n")
    (td / "t.sby").write_text(cfg)
    p = subprocess.run([sby, "-f", "t.sby"], cwd=str(td), capture_output=True, text=True, timeout=120)
    m = re.findall(r"DONE \((\w+)", p.stdout + p.stderr)
    return m[-1] if m else "?"


def test_emit_roundtrip_harness_structure():
    from specloop.compose.axis_pipeline import order_axis_pipeline, emit_roundtrip_harness

    sel = [_axis_sm("a", "modA"), _axis_sm("b", "modB")]
    ordered = order_axis_pipeline(_plan("a", "b", conns=[_conn("a", "b")]), sel)
    h = emit_roundtrip_harness("topc", ordered, max_len=4)
    assert h is not None and h.top_module == "topc_rt_harness"
    assert "(* anyconst *)" in h.harness_sv                     # symbolic frame
    assert "topc dut (" in h.harness_sv                         # instantiates the wrapper, no bind
    assert not any(l.strip().startswith("bind ") for l in h.harness_sv.splitlines())  # closed harness
    assert "ap_rt_data" in h.harness_sv and "ap_rt_complete" in h.harness_sv
    assert {e.name for e in h.assertion_index} >= {"ap_rt_data", "ap_rt_complete"}
    # width-changing pipeline → identity can't hold → None
    mismatched = [_axis_sm("a", "modA", width=8), _axis_sm("b", "modB", width=16)]
    assert emit_roundtrip_harness("x", mismatched, max_len=4) is None
    print("  OK harness: closed (no bind), symbolic frame + completeness assert; width-change → None")


def test_sby_checks_inlined_not_bind():
    """Toolchain guard for the change-#3 discovery: the sby backend's Yosys
    front-end CHECKS inlined assertions but SILENTLY IGNORES SystemVerilog `bind`.
    Encodes this so a bind-attached 'proof' can never again pass as sound."""
    import tempfile
    sby = _find_sby()
    if sby is None:
        print("  SKIP sby inline/bind guard (sby not found)")
        return
    inl = ("module d(input clk, input [7:0] x);\n"
           "  always @(posedge clk) ap_fail: assert(1'b0);\nendmodule\n")
    dut = "module d(input clk, input [7:0] x, output [7:0] y); assign y=x; endmodule\n"
    spec = ("module s(input clk, input [7:0] x);\n"
            "  always @(posedge clk) ap_bind_fail: assert(1'b0);\nendmodule\n"
            "bind d s si(.*);\n")
    with tempfile.TemporaryDirectory() as td:
        a = Path(td) / "a"; a.mkdir()
        b = Path(td) / "b"; b.mkdir()
        inlined = _sby_verdict(sby, a, {"d.sv": inl}, "d", "d.sv")
        bound = _sby_verdict(sby, b, {"dut.sv": dut, "spec.sv": spec}, "d", "spec.sv")
    assert inlined == "FAIL", f"sby must CHECK inlined assertions, got {inlined}"
    assert bound == "PASS", f"sby silently IGNORES bind (assert never checked), got {bound}"
    print("  OK toolchain: sby checks inlined assertions (FAIL); silently ignores bind (PASS) — guard locked")


def test_roundtrip_harness_proves_passthrough():
    """Sound end-to-end: a 2-stage register pass-through round-trips identically
    (BMC), and a deliberately-corrupted check produces a counterexample (proving
    the harness genuinely verifies). Gated on sby + axis_register artifacts."""
    import tempfile
    from specloop.compose.axis_pipeline import order_axis_pipeline, emit_pipeline_wrapper, emit_roundtrip_harness
    from specloop.compose.pipeline import _resolve_rtl_deps
    from specloop.formal.sby_backend import SBYBackend

    sby = _find_sby()
    ir_file = WORK / "axis_register.ir.json"
    if sby is None or not ir_file.exists():
        print("  SKIP round-trip passthrough (sby / axis_register.ir.json absent)")
        return
    ir = ModuleIR.model_validate(json.loads(ir_file.read_text()))
    if not Path(ir.file).exists():
        print("  SKIP round-trip passthrough (corpus RTL absent)")
        return
    sel = [SelectedModule(sub_function_id=s, search_result=_sr(), ir=ir, rtl_path=Path(ir.file))
           for s in ("r0", "r1")]
    ordered = order_axis_pipeline(_plan("r0", "r1", conns=[_conn("r0", "r1")]), sel)
    assert ordered is not None
    with tempfile.TemporaryDirectory() as td:
        out = Path(td)
        (out / "w.sv").write_text(emit_pipeline_wrapper("w", ordered))
        h = emit_roundtrip_harness("w", ordered, max_len=2)
        (out / "h.sv").write_text(h.harness_sv)
        deps = _resolve_rtl_deps([Path(ir.file)])
        be = SBYBackend(sby_path=sby, timeout=200, depth=h.depth, flatten=True)
        good = be.run(module_name=h.top_module, rtl_path=out / "w.sv", bind_path=out / "h.sv",
                      deps=deps, work_dir=out, assertion_index=h.assertion_index, mode="bmc")
        # corrupt the data check: must now produce a counterexample
        bad_sv = h.harness_sv.replace("m_tdata == sym[out_idx*W +: W]",
                                      "m_tdata == (sym[out_idx*W +: W] ^ 8'h01)")
        (out / "hb.sv").write_text(bad_sv)
        bad = be.run(module_name=h.top_module, rtl_path=out / "w.sv", bind_path=out / "hb.sv",
                     deps=deps, work_dir=out, assertion_index=h.assertion_index, mode="bmc")
    assert good.status == "pass", f"pass-through round-trip should hold, got {good.status}"
    assert bad.status == "fail", f"corrupted check must fail (anti-vacuity), got {bad.status}"
    print(f"  OK round-trip: 2-stage pass-through proves (N=2); corrupted reference → counterexample")


# --------------------------------------------------------------------------

def main() -> int:
    tests = [
        ("Improvement 1: grouping on ibex_alu", test_grouping_on_ibex_alu),
        ("Improvement 1: grouping determinism", test_grouping_determinism),
        ("Improvement 1: merge preserves @(*) + dedup", test_merge_preserves_combinational_and_dedups),
        ("Improvement 2: valid/ready direction", test_valid_ready_direction),
        ("Improvement 2: AXI-Lite canonical match", test_axi_lite_canonical_match),
        ("Improvement 2: reset polarity inversion", test_reset_polarity_inversion),
        ("Improvement 2: no-protocol fallback", test_no_protocol_empty_plan),
        ("Improvement 3: inheritance guard-aware", test_inheritance_guard_aware),
        ("Improvement 3: missing bind graceful", test_inheritance_missing_bind_graceful),
        ("Improvement 4: synthesis PPA", test_synth_ppa),
        ("Improvement 5: axis bundle extraction + roles", test_axis_bundle_extraction_and_roles),
        ("Improvement 5: pair chain + reject mem-FIFO", test_pair_axis_chain_and_reject_memfifo),
        ("Improvement 5: connection roles not over-excluded", test_connection_roles_not_over_excluded),
        ("Improvement 5: width/clock/ENABLE diagnostics", test_pair_width_clock_enable_diagnostics),
        ("Improvement 5: Chain A end-to-end bundle check", test_check_interfaces_chain_a_end_to_end),
        ("Improvement 5: interface-aware selection picks axis_fifo", test_select_interface_aware_picks_axis_fifo),
        ("Improvement 6: order linear AXIS pipeline", test_order_axis_pipeline),
        ("Improvement 6: deterministic wrapper structure", test_emit_pipeline_wrapper_structure),
        ("Improvement 6: carried-bind structure/counts", test_build_carried_bind_counts),
        ("Improvement 7: round-trip harness structure", test_emit_roundtrip_harness_structure),
        ("Improvement 7: sby checks inlined, ignores bind (guard)", test_sby_checks_inlined_not_bind),
        ("Improvement 7: round-trip pass-through proves + anti-vacuity", test_roundtrip_harness_proves_passthrough),
    ]
    failures = 0
    for name, fn in tests:
        print(f"[{name}]")
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"  FAIL: {exc}")
    print()
    print(f"{len(tests) - failures}/{len(tests)} test groups passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
