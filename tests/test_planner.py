"""Tests for stage 1 — the library-aware LLM planner (request -> ordered roles,
validated against the proven catalog) and the full request -> plan -> retrieve ->
compose+prove loop.

Deterministic by default: a FakeClient returns canned planner JSON, so parsing /
catalog-validation / position-attachment / honest-failure logic is tested without the
network. The live-LLM demos run only when SPECLOOP_LIVE_LLM=1; the AG-proof step runs
only when sby is present.

    PYTHONPATH=src python3 tests/test_planner.py
    SPECLOOP_LIVE_LLM=1 PYTHONPATH=src python3 tests/test_planner.py   # + live LLM demos
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from specloop.gen.client import LLMClient  # noqa: E402
from specloop.compose.planner import (  # noqa: E402
    Planner, PlannerError, _position, build_planning_catalog,
)
from specloop.compose.retrieval import FUNCTIONS, BlockRetriever  # noqa: E402
from specloop.compose.e2e import run_pipeline, format_trace  # noqa: E402

WORK = ROOT / "work"
SOUND = WORK / "recheck_axis" / "sound_results.json"
CHAIN_A_BLOCKS = ["axis_pipeline_register", "axis_fifo", "axis_frame_length_adjust", "axis_rate_limit"]


class FakeClient(LLMClient):
    """Returns a fixed canned response — exercises planner parsing with no network."""
    def __init__(self, response: str) -> None:
        self._response = response

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        return self._response

    @property
    def model_id(self) -> str:
        return "fake"


def _retriever() -> BlockRetriever:
    return BlockRetriever(work_dir=WORK, sound_results=SOUND)


# The planning menu, derived once from the proven catalog (same source as stage 2).
_CATALOG = build_planning_catalog(_retriever())


def _planner(response: str) -> Planner:
    return Planner(FakeClient(response), _CATALOG)


# --------------------------------------------------------------------------
# Catalog: derived from the proven set, not hardcoded
# --------------------------------------------------------------------------

def test_catalog_is_derived_from_proven_set():
    cat = build_planning_catalog(_retriever())
    # All 7 role functions have a proven block; nothing else leaks in.
    assert set(cat) == set(FUNCTIONS), f"catalog functions != the 7: {set(cat)}"
    assert "mux" not in cat and "demux" not in cat, "non-role functions leaked into the menu"
    # Widths are READ from the proven blocks (every proven block is 8-bit today).
    assert all(cat[f] == [8] for f in FUNCTIONS), cat
    print(f"  OK catalog: derived menu = {cat} (widths read from proven blocks, not hardcoded)")


# --------------------------------------------------------------------------
# Planner: parsing, position, catalog validation, honest failure
# --------------------------------------------------------------------------

def test_parses_roles_and_attaches_position_mechanically():
    res = _planner(
        '{"roles":[{"function":"register","data_width":8},'
        '{"function":"buffer","data_width":8},'
        '{"function":"rate_limit","data_width":8}]}'
    ).plan("...")
    assert [r.function for r in res.roles] == ["register", "buffer", "rate_limit"]
    assert [r.position for r in res.roles] == ["head", "middle", "tail"]
    assert all(r.protocol == "axi_stream" and r.data_width == 8 for r in res.roles)
    assert not res.data_width_defaulted
    print("  OK planner: ordered roles parsed; position attached mechanically (head/middle/tail)")


def test_single_role_is_middle():
    res = _planner('{"roles":[{"function":"buffer","data_width":8}]}').plan("x")
    assert _position(0, 1) == "middle"
    assert res.roles[0].position == "middle", "a lone stage must expose both bundles"
    print("  OK planner: a single-stage pipeline is 'middle' (needs both bundles)")


def test_unspecified_width_defaults_to_available():
    res = _planner('{"roles":[{"function":"register"}]}').plan("x")
    assert res.roles[0].data_width == 8 and res.data_width_defaulted
    print("  OK planner: unspecified width filled from the catalog's available default (8), flagged")


def test_unavailable_width_fails_at_planning():
    # The width case that used to face-plant in stage 2 is now caught in stage 1.
    try:
        _planner('{"roles":[{"function":"register","data_width":16}]}').plan("x")
    except PlannerError as exc:
        msg = str(exc)
        assert "cannot plan" in msg and "only available at width(s) [8]" in msg and "requested 16" in msg
        assert "proceed at 8-bit?" in msg, "should surface the available alternative (not auto-apply it)"
        print(f"  OK planner: unavailable width rejected UP FRONT — {msg!r}")
        return
    raise AssertionError("unavailable width should raise PlannerError at planning time")


def test_out_of_catalog_function_is_hard_rejected():
    try:
        _planner('{"roles":[{"function":"encrypt","data_width":8}]}').plan("x")
    except PlannerError as exc:
        assert str(exc) == "cannot plan: requires 'encrypt', not in the available block set"
        print("  OK planner: out-of-catalog function hard-rejected (no hallucinated stage)")
        return
    raise AssertionError("out-of-catalog function should raise PlannerError")


def test_model_emitted_error_is_surfaced():
    try:
        _planner('{"error":"cannot plan: requires encrypt, not in the available block set"}').plan("x")
    except PlannerError as exc:
        assert str(exc) == "cannot plan: requires encrypt, not in the available block set"
        print("  OK planner: model's honest out-of-catalog error surfaced verbatim")
        return
    raise AssertionError("model error should raise PlannerError")


def test_unparseable_response_fails_honestly():
    try:
        _planner("I'm not able to help with that request.").plan("x")
    except PlannerError as exc:
        assert "no parseable JSON" in str(exc)
        print("  OK planner: unparseable response fails honestly (no silent empty plan)")
        return
    raise AssertionError("unparseable response should raise PlannerError")


def test_menu_in_prompt_lists_functions_and_widths():
    prompt = _planner("")._system_prompt()
    assert len(FUNCTIONS) == 7
    for f in FUNCTIONS:
        assert f in prompt, f"function '{f}' missing from planner prompt"
    assert "available data widths" in prompt and "[8]" in prompt
    print("  OK planner: prompt carries the derived menu (7 functions + available widths)")


# --------------------------------------------------------------------------
# Full loop: request -> plan -> retrieve -> compose (+ prove)
# --------------------------------------------------------------------------

def test_full_loop_provable_request():
    """Demo 1 (deterministic planner): roles -> the 4 Chain A blocks -> stage-3
    accepts -> AG proof PASS (when sby present), anti-vacuity FAIL. Unchanged."""
    planner = _planner(
        '{"roles":[{"function":"register","data_width":8},'
        '{"function":"buffer","data_width":8},'
        '{"function":"frame_adjust","data_width":8},'
        '{"function":"rate_limit","data_width":8}]}'
    )
    trace = run_pipeline("register, buffer, normalize frame length, rate-limit an 8-bit stream",
                         planner, _retriever(), work_dir=WORK)
    assert trace.ok, trace.error
    assert trace.blocks == CHAIN_A_BLOCKS, trace.blocks
    assert trace.ordered_ok, "stage 3 should accept the composed chain"
    if trace.proof and trace.proof.verdict is not None:
        assert trace.proof.verdict == "PASS", trace.proof
        assert trace.proof.antivacuity == "FAIL", "anti-vacuity: corrupted ref must fail"
        # On PASS the composed synthesizable wrapper is persisted.
        assert trace.wrapper_path and Path(trace.wrapper_path).exists(), "composed .sv not written"
        sv = Path(trace.wrapper_path).read_text()
        assert sv.lstrip().startswith("module ") and "endmodule" in sv, "wrapper isn't a module"
        print("  OK full loop: request -> roles -> 4 proven blocks -> AG proof PASS + anti-vacuity; "
              f"composed RTL -> {trace.wrapper_path}")
    else:
        print(f"  OK full loop: request -> roles -> 4 proven blocks -> ordered "
              f"(proof skipped: {trace.proof.note if trace.proof else 'n/a'})")


def test_full_loop_out_of_vocab_is_honest_failure():
    """Demo 2: out-of-vocab request fails at stage 1; no blocks, no crash. Unchanged."""
    planner = _planner('{"error":"cannot plan: requires encrypt, not in the available block set"}')
    trace = run_pipeline("encrypt the stream", planner, _retriever(), work_dir=WORK)
    assert not trace.ok and trace.stage_failed == "plan"
    assert "cannot plan" in trace.error and trace.blocks is None
    print("  OK full loop: out-of-vocab -> honest stage-1 failure, no hallucinated blocks")


def test_full_loop_unavailable_width_fails_at_planning():
    """Demo 3 (the case that used to face-plant): a 16-bit request is now caught at
    PLANNING time (stage 1), not deferred to retrieval (stage 2)."""
    planner = _planner(
        '{"roles":[{"function":"register","data_width":16},{"function":"buffer","data_width":16}]}'
    )
    trace = run_pipeline("register and buffer a 16-bit stream", planner, _retriever(), work_dir=WORK)
    assert not trace.ok and trace.stage_failed == "plan", f"should fail at PLAN, got {trace.stage_failed}"
    assert "cannot plan" in trace.error and "only available at width(s) [8]" in trace.error
    assert trace.roles is None and trace.blocks is None, "never reached retrieval"
    print("  OK full loop: unavailable 16-bit width caught at PLANNING (stage 1), not retrieval")


# --------------------------------------------------------------------------
# Live LLM demos (opt-in: SPECLOOP_LIVE_LLM=1)
# --------------------------------------------------------------------------

def test_live_llm_demos():
    if os.environ.get("SPECLOOP_LIVE_LLM") != "1":
        print("  SKIP live LLM demos (set SPECLOOP_LIVE_LLM=1 to run real Anthropic calls)")
        return
    from specloop.config import SpecloopConfig
    from specloop.gen.client import make_client

    cfg = SpecloopConfig()
    retr = _retriever()
    planner = Planner.from_retriever(make_client(cfg), retr)

    print("\n  --- DEMO 1: provable request ---")
    t1 = run_pipeline(
        "Take an 8-bit AXI-Stream: register the input, buffer it, normalize the frame "
        "length, and rate-limit the output.", planner, retr, work_dir=WORK)
    print("    " + format_trace(t1).replace("\n", "\n    "))
    assert t1.ok and t1.blocks == CHAIN_A_BLOCKS

    print("\n  --- DEMO 2: out-of-vocab request ---")
    t2 = run_pipeline("Encrypt the AXI-Stream payload.", planner, retr, work_dir=WORK)
    print("    " + format_trace(t2).replace("\n", "\n    "))
    assert not t2.ok and t2.stage_failed == "plan"

    print("\n  --- DEMO 3: width not in the catalog (caught at planning) ---")
    t3 = run_pipeline("Register and buffer a 16-bit AXI-Stream.", planner, retr, work_dir=WORK)
    print("    " + format_trace(t3).replace("\n", "\n    "))
    assert not t3.ok and t3.stage_failed == "plan", f"16-bit must fail at PLAN, got {t3.stage_failed}"
    print("  OK live: demo 1 proves end-to-end; demos 2 & 3 fail honestly at planning")


# --------------------------------------------------------------------------

def main() -> int:
    tests = [
        ("catalog derived from proven set", test_catalog_is_derived_from_proven_set),
        ("planner: parse + mechanical position", test_parses_roles_and_attaches_position_mechanically),
        ("planner: single role is middle", test_single_role_is_middle),
        ("planner: unspecified width -> available default", test_unspecified_width_defaults_to_available),
        ("planner: unavailable width fails at planning", test_unavailable_width_fails_at_planning),
        ("planner: out-of-catalog function rejected", test_out_of_catalog_function_is_hard_rejected),
        ("planner: model error surfaced", test_model_emitted_error_is_surfaced),
        ("planner: unparseable fails honestly", test_unparseable_response_fails_honestly),
        ("planner: menu listed in prompt", test_menu_in_prompt_lists_functions_and_widths),
        ("full loop: provable request", test_full_loop_provable_request),
        ("full loop: out-of-vocab honest fail", test_full_loop_out_of_vocab_is_honest_failure),
        ("full loop: unavailable width fails at planning", test_full_loop_unavailable_width_fails_at_planning),
        ("live LLM demos (opt-in)", test_live_llm_demos),
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
