"""Regression tests for stage 2 — DERIVED role -> proven block retrieval.

Matching is driven by each block's behavioral signature (derived from its proven
contracts + the width-param structural feature), never a hand-assigned label. These
tests prove that: derived classification matches ground truth on all 13, the
hard-to-separate srl_register/srl_fifo pair is split by real contract signal, and
scrambling any stored function field leaves matching unchanged (anti-cheat).

Pure-logic (no LLM/Qdrant/API key); the end-to-end AG-proof test skips when sby is
absent. Run directly:

    PYTHONPATH=src python3 tests/test_retrieval.py
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from specloop.ir.schema import ModuleIR, Port  # noqa: E402
import specloop.compose.retrieval as R  # noqa: E402
from specloop.compose.retrieval import (  # noqa: E402
    FUNCTIONS,
    BlockRetriever,
    ProvenBlock,
    Role,
    _interface_reject,
)

WORK = ROOT / "work"
SOUND = WORK / "recheck_axis" / "sound_results.json"

# The hand-picked Chain A blocks the existing AG proof's stubs cite (AG_RESULT.md:
# pipeline_register / axis_fifo / frame_length_adjust / rate_limit).
CHAIN_A_BLOCKS = [
    "axis_pipeline_register", "axis_fifo", "axis_frame_length_adjust", "axis_rate_limit",
]
FLAGGED = ["axis_async_fifo", "axis_ll_bridge", "fifo_sync", "axis_frame_len"]

# GROUND TRUTH — used ONLY to validate that the DERIVED classification is correct.
# It is never read by the retriever (that is the whole point; see the scramble test).
GROUND_TRUTH = {
    "axis_register": "register", "axis_pipeline_register": "register",
    "axis_srl_register": "register", "axis_fifo": "buffer", "axis_srl_fifo": "buffer",
    "axis_adapter": "width_convert", "axis_frame_length_adjust": "frame_adjust",
    "axis_rate_limit": "rate_limit", "axis_cobs_encode": "encode",
    "axis_cobs_decode": "decode", "axis_frame_join": "frame_join",
    "axis_mux": "mux", "axis_demux": "demux",
}


def _retriever() -> BlockRetriever:
    return BlockRetriever(work_dir=WORK, sound_results=SOUND)


def _one_bundle_block(name: str, *, slave: bool, master: bool, width: int = 8) -> ProvenBlock:
    """A synthetic ProvenBlock exposing only the requested AXIS bundle(s) — used to
    exercise the head/middle/tail position requirement (every *real* block has both,
    so a missing-bundle case has to be constructed). Signature is irrelevant here."""
    ports = [Port(name="clk", direction="input", is_clock=True),
             Port(name="rst", direction="input", is_reset=True, reset_polarity="high")]

    def bundle(prefix: str, is_slave: bool) -> list[Port]:
        fwd = "input" if is_slave else "output"
        rev = "output" if is_slave else "input"
        return [Port(name=f"{prefix}_tdata", direction=fwd, width=width),
                Port(name=f"{prefix}_tvalid", direction=fwd),
                Port(name=f"{prefix}_tready", direction=rev),
                Port(name=f"{prefix}_tlast", direction=fwd)]

    if slave:
        ports += bundle("s_axis", True)
    if master:
        ports += bundle("m_axis", False)
    ir = ModuleIR(module=name, file=f"{name}.v", ports=ports)
    return ProvenBlock(name=name, ir=ir, confidence=1.0, rtl_path=Path(f"{name}.v"),
                       signature={}, derived_function=None)


# --------------------------------------------------------------------------
# Derived classification: correct on all 13, label-independent
# --------------------------------------------------------------------------

def test_catalog_is_exactly_the_13_proven():
    proven = {n for n, r in json.loads(SOUND.read_text()).items()
              if r.get("status") == "soundly_reproven"}
    cat = _retriever().catalog
    assert len(proven) == 13, f"expected 13 soundly_reproven, found {len(proven)}"
    assert set(cat) == proven, f"catalog != proven set: {set(cat) ^ proven}"
    for flagged in FLAGGED:
        assert flagged not in cat, f"flagged/unproven '{flagged}' leaked into catalog"
    print(f"  OK catalog: exactly the {len(cat)} soundly_reproven blocks; no flagged module present")


def test_derived_classification_matches_ground_truth():
    cat = _retriever().catalog
    wrong = {n: b.derived_function for n, b in cat.items()
             if b.derived_function != GROUND_TRUTH[n]}
    assert not wrong, f"derived classification disagrees with ground truth: {wrong}"
    print(f"  OK derived: all 13 blocks classify to ground truth FROM their contracts (no label)")


def test_no_hand_label_table_in_module():
    # The old lookup table must be gone from the matching path entirely.
    assert not hasattr(R, "_BLOCK_FUNCTION"), "_BLOCK_FUNCTION hand-label table still present"
    print("  OK no-lookup: _BLOCK_FUNCTION is absent from the module")


def test_each_function_maps_to_correct_block():
    r = _retriever()
    expect_top = {
        "buffer": "axis_fifo", "register": "axis_pipeline_register",
        "width_convert": "axis_adapter", "rate_limit": "axis_rate_limit",
        "frame_adjust": "axis_frame_length_adjust", "encode": "axis_cobs_encode",
        "decode": "axis_cobs_decode",
    }
    for func in FUNCTIONS:
        res = r.retrieve(Role(function=func))
        assert res.matched, f"function '{func}' should match a proven block: {res.reason}"
        assert res.block.name == expect_top[func], \
            f"{func} -> {res.block.name}, expected {expect_top[func]}"
    print("  OK function map: all 7 role functions resolve to the right proven block")


def test_srl_register_vs_srl_fifo_separated():
    """The exact pair the 32-dim fingerprint could not separate, now split by the
    derived CONTRACT signal."""
    r = _retriever()
    reg, fifo = r.catalog["axis_srl_register"], r.catalog["axis_srl_fifo"]
    assert reg.derived_function == "register" and fifo.derived_function == "buffer"
    # WHY: srl_register proves single-beat data stability and NO queue behaviour...
    assert reg.signature["data_stable"]
    assert not (reg.signature["emptiness"] or reg.signature["occupancy"] or reg.signature["rw_pointers"])
    # ...srl_fifo proves emptiness + occupancy + read/write pointer motion, no data-stable.
    assert fifo.signature["emptiness"] and fifo.signature["occupancy"] and fifo.signature["rw_pointers"]
    assert not fifo.signature["data_stable"]
    # So the hard bar holds by real signal, not by label:
    assert "axis_srl_register" not in {b.name for b in r.retrieve(Role(function="buffer")).ranked}
    assert "axis_srl_fifo" not in {b.name for b in r.retrieve(Role(function="register")).ranked}
    print("  OK srl pair: register(data_stable, no queue) vs buffer(empty+occupancy+pointers) — separated by contracts")


def test_anti_cheat_label_scramble_leaves_matching_unchanged():
    """Matching must NOT read a stored function field. Scramble every block's stored
    label to a plausible-but-wrong value: if it were a lookup the results would
    change; because matching re-derives from the signature, they don't."""
    r = _retriever()
    baseline = {f: [b.name for b in r.retrieve(Role(function=f)).ranked] for f in FUNCTIONS}

    wrong = ["buffer", "register", "encode", "decode", "rate_limit", "frame_adjust", "width_convert"]
    for i, b in enumerate(r.catalog.values()):
        b.derived_function = wrong[i % len(wrong)]      # deliberately wrong labels
    after = {f: [b.name for b in r.retrieve(Role(function=f)).ranked] for f in FUNCTIONS}
    assert baseline == after, f"matching changed when labels scrambled -> still a lookup\n{baseline}\n{after}"

    # Positive control: corrupting the DERIVED signature DOES break matching — proof
    # that the signature (not a label) is what actually drives the match.
    r2 = _retriever()
    for b in r2.catalog.values():
        b.signature = {k: 0 for k in b.signature}
    assert not r2.retrieve(Role(function="buffer")).matched, \
        "wiping the derived signature should break matching (it is signature-driven)"
    print("  OK anti-cheat: scrambling stored labels changes nothing; wiping the derived signature breaks it")


def test_wrong_category_never_returned():
    r = _retriever()
    from specloop.compose.retrieval import classify
    for func in FUNCTIONS:
        for cand in r.retrieve(Role(function=func)).ranked:
            assert classify(cand.signature) == func, \
                f"{func} ranked a {classify(cand.signature)} block ({cand.name})"
    buffer_names = {c.name for c in r.retrieve(Role(function="buffer")).ranked}
    assert not (buffer_names & {"axis_register", "axis_pipeline_register", "axis_srl_register"}), \
        "a register was returned for a buffer role"
    register_names = {c.name for c in r.retrieve(Role(function="register")).ranked}
    assert not (register_names & {"axis_cobs_encode", "axis_cobs_decode"}), \
        "a codec was returned for a register role"
    print("  OK no cross-category: every ranked block's DERIVED best-fit equals the requested function")


def test_multiple_variants_ranked_by_confidence():
    r = _retriever()
    reg = [b.name for b in r.retrieve(Role(function="register")).ranked]
    assert reg == ["axis_pipeline_register", "axis_srl_register", "axis_register"], reg
    buf = [b.name for b in r.retrieve(Role(function="buffer")).ranked]
    assert buf == ["axis_fifo", "axis_srl_fifo"], buf
    print("  OK ranking: register (3) and buffer (2) variants returned best-proven first")


# --------------------------------------------------------------------------
# Interface compatibility (unchanged): width / position / protocol
# --------------------------------------------------------------------------

def test_wrong_width_excluded_honest_no_match():
    r = _retriever()
    res = r.retrieve(Role(function="buffer", data_width=16))
    assert not res.matched, "16-bit buffer should not match an 8-bit-default block"
    excluded = dict(res.excluded)
    assert "axis_fifo" in excluded and "width" in excluded["axis_fifo"], res.excluded
    assert "no proven block fills role" in res.reason
    print("  OK width: 16-bit buffer role excluded (blocks are 8-bit) -> honest no-match")


def test_position_requires_the_right_bundle():
    source = _one_bundle_block("src", slave=False, master=True)   # no s_axis
    sink = _one_bundle_block("snk", slave=True, master=False)     # no m_axis
    assert _interface_reject(source, Role(function="register", position="tail"), {"slave"})
    assert _interface_reject(source, Role(function="register", position="head"), {"master"}) is None
    assert _interface_reject(sink, Role(function="register", position="head"), {"master"})
    assert _interface_reject(sink, Role(function="register", position="tail"), {"slave"}) is None
    print("  OK position: source-only fails tail, sink-only fails head (bundle-role requirement)")


def test_real_blocks_satisfy_all_positions():
    r = _retriever()
    for pos in ("head", "middle", "tail"):
        res = r.retrieve(Role(function="buffer", position=pos))
        assert res.matched and res.block.name == "axis_fifo", f"buffer@{pos}: {res.reason}"
    print("  OK position: real single-stream blocks fill head/middle/tail")


def test_protocol_and_unknown_function_no_match():
    r = _retriever()
    bad_proto = r.retrieve(Role(function="buffer", protocol="wishbone"))
    assert not bad_proto.matched and "protocol" in bad_proto.reason
    unknown = r.retrieve(Role(function="cpu_core"))
    assert not unknown.matched and "function 'cpu_core'" in unknown.reason
    bad_pos = r.retrieve(Role(function="buffer", position="sideways"))
    assert not bad_pos.matched and "position" in bad_pos.reason
    print("  OK no-match: wrong protocol, unknown function, and bad position each fail honestly")


def test_unproven_blocks_never_returned():
    r = _retriever()
    returned: set[str] = set()
    for func in list(FUNCTIONS) + ["frame_join", "mux", "demux"]:
        for pos in ("head", "middle", "tail"):
            returned |= {b.name for b in r.retrieve(Role(function=func, position=pos)).ranked}
    proven = {n for n, rec in json.loads(SOUND.read_text()).items()
              if rec.get("status") == "soundly_reproven"}
    assert returned <= proven, f"retrieval returned non-proven blocks: {returned - proven}"
    assert not (returned & set(FLAGGED)), "a flagged module was returned"
    print(f"  OK soundness: every block ever returned ({len(returned)}) is soundly_reproven")


# --------------------------------------------------------------------------
# Wiring to stage 3
# --------------------------------------------------------------------------

def test_retrieve_chain_assembles_and_orders():
    from specloop.compose.axis_pipeline import order_axis_pipeline

    r = _retriever()
    roles = [Role(function="register", position="head"),
             Role(function="buffer", position="middle"),
             Role(function="frame_adjust", position="middle"),
             Role(function="rate_limit", position="tail")]
    selected, plan, results = r.retrieve_chain(roles, composition_name="chainA")

    names = [s.search_result.module_name for s in selected]
    assert names == CHAIN_A_BLOCKS, f"retrieved {names}, expected {CHAIN_A_BLOCKS}"
    assert len(plan.connections) == 3
    assert [c.from_id for c in plan.connections] == [s.sub_function_id for s in selected[:3]]
    ordered = order_axis_pipeline(plan, selected)
    assert ordered is not None, "stage-3 order_axis_pipeline rejected the retrieved chain"
    assert [s.ir.module for s in ordered] == CHAIN_A_BLOCKS
    print("  OK wiring: Chain A roles -> derived retrieval -> stage-3 ordered pipeline (head->tail)")


def test_retrieve_chain_raises_on_missing_role():
    from specloop.compose.retrieval import RetrievalError

    r = _retriever()
    try:
        r.retrieve_chain([Role(function="register"), Role(function="cpu_core")])
    except RetrievalError as exc:
        assert "cpu_core" in str(exc)
        print("  OK wiring: a role with no proven block fails the chain honestly (no wrong fill)")
        return
    raise AssertionError("retrieve_chain should raise on an unfillable role")


# --------------------------------------------------------------------------
# End-to-end: derived Chain A -> existing AG proof passes (sound), anti-vacuity holds
# --------------------------------------------------------------------------

def _find_sby() -> str | None:
    sby = shutil.which("sby") or str(ROOT / "oss-cad-suite" / "bin" / "sby")
    return sby if Path(sby).exists() else None


def _ag_verdict(sby: str, td: Path, harness_sv: str) -> str:
    (td / "h.sv").write_text(harness_sv)
    (td / "t.sby").write_text(
        "[options]\nmode bmc\ndepth 12\n[engines]\nsmtbmc\n"
        "[script]\nread -sv h.sv\nprep -top chainA_ag\n[files]\nh.sv\n"
    )
    p = subprocess.run([sby, "-f", "t.sby"], cwd=str(td),
                       capture_output=True, text=True, timeout=180)
    m = re.findall(r"DONE \((\w+)", p.stdout + p.stderr)
    return m[-1] if m else "?"


def test_chain_a_ag_proof_end_to_end():
    """Derived retrieval selects the Chain A blocks the existing AG proof is built
    from, that proof passes soundly, and a corrupted reference fails (anti-vacuity).
    Gated on sby + the chainA_ag harness artifact."""
    from specloop.compose.axis_pipeline import order_axis_pipeline

    sby = _find_sby()
    harness = WORK / "recheck_axis" / "ag" / "chainA_ag_closed.sv"
    if sby is None or not harness.exists():
        print("  SKIP Chain A AG end-to-end (sby / chainA_ag_closed.sv absent)")
        return

    r = _retriever()
    roles = [Role(function="register", position="head"),
             Role(function="buffer", position="middle"),
             Role(function="frame_adjust", position="middle"),
             Role(function="rate_limit", position="tail")]
    selected, plan, _ = r.retrieve_chain(roles, composition_name="chainA")
    assert [s.ir.module for s in selected] == CHAIN_A_BLOCKS
    assert order_axis_pipeline(plan, selected) is not None

    sv = harness.read_text()
    with tempfile.TemporaryDirectory() as td:
        good = _ag_verdict(sby, Path(td), sv)
        corrupted = sv.replace("ap_bpout_data:assert(out_d==$past(out_d))",
                               "ap_bpout_data:assert(out_d!=$past(out_d))")
        assert corrupted != sv, "anti-vacuity mutation did not apply"
        bad = _ag_verdict(sby, Path(td), corrupted)

    assert good == "PASS", f"Chain A AG proof should pass, got {good}"
    assert bad == "FAIL", f"corrupted AG reference must fail (anti-vacuity), got {bad}"
    print("  OK end-to-end: derived Chain A -> AG proof PASS (7/7); corrupted reference -> FAIL")


# --------------------------------------------------------------------------

def main() -> int:
    tests = [
        ("catalog is exactly the 13 proven", test_catalog_is_exactly_the_13_proven),
        ("derived classification == ground truth", test_derived_classification_matches_ground_truth),
        ("no hand-label table in module", test_no_hand_label_table_in_module),
        ("each function -> correct block", test_each_function_maps_to_correct_block),
        ("srl_register vs srl_fifo separated", test_srl_register_vs_srl_fifo_separated),
        ("anti-cheat: label scramble unchanged", test_anti_cheat_label_scramble_leaves_matching_unchanged),
        ("wrong category never returned", test_wrong_category_never_returned),
        ("multiple variants ranked by confidence", test_multiple_variants_ranked_by_confidence),
        ("wrong width -> honest no-match", test_wrong_width_excluded_honest_no_match),
        ("position requires right bundle", test_position_requires_the_right_bundle),
        ("real blocks satisfy all positions", test_real_blocks_satisfy_all_positions),
        ("protocol / unknown function no-match", test_protocol_and_unknown_function_no_match),
        ("unproven blocks never returned", test_unproven_blocks_never_returned),
        ("retrieve_chain assembles + orders", test_retrieve_chain_assembles_and_orders),
        ("retrieve_chain raises on missing role", test_retrieve_chain_raises_on_missing_role),
        ("Chain A AG proof end-to-end", test_chain_a_ag_proof_end_to_end),
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
