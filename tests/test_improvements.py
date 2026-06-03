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
