"""End-to-end wiring: a typed request flows stage 1 -> stage 2 -> stage 3.

``run_pipeline(request, ...)`` drives the planner (stage 1, LLM -> ordered roles) ->
``BlockRetriever`` (stage 2, derived retrieval -> proven blocks) -> stage-3
composition ordering, and — for a chain with an existing sound AG-proof artifact —
the assume-guarantee composition proof, returning one ``PipelineTrace``. Honest
failures from any stage (out-of-vocab plan, no proven block at the requested width)
surface as ``trace.error`` with the failing stage, never a crash or a wrong block.

Runnable directly:  ``python -m specloop.compose.e2e "register and buffer an 8-bit AXI-Stream"``
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from specloop.compose.axis_pipeline import emit_pipeline_wrapper, order_axis_pipeline
from specloop.compose.planner import Planner, PlannerError, PlanResult
from specloop.compose.retrieval import BlockRetriever, RetrievalError, Role

log = logging.getLogger(__name__)


# Sound AG-composition proof artifacts stage 3 has already produced, keyed by the
# ordered block names of the composition they prove. Generating AG harnesses for
# novel chains is stage-3's job; stage 1 only needs to reach an existing verdict.
# value = (harness_path, top_module, original_assert, mutated_assert_for_antivacuity)
_AG_HARNESS: dict[tuple[str, ...], tuple[str, str, str, str]] = {
    ("axis_pipeline_register", "axis_fifo", "axis_frame_length_adjust", "axis_rate_limit"): (
        "recheck_axis/ag/chainA_ag_closed.sv", "chainA_ag",
        "ap_bpout_data:assert(out_d==$past(out_d))",
        "ap_bpout_data:assert(out_d!=$past(out_d))",
    ),
}


@dataclass
class ProofResult:
    verdict: Optional[str]        # "PASS" / "FAIL" / None when no proof was run
    antivacuity: Optional[str]    # verdict of the corrupted reference ("FAIL" expected)
    note: str = ""


@dataclass
class PipelineTrace:
    request: str
    roles: Optional[list[Role]] = None        # stage 1 output (ordered)
    blocks: Optional[list[str]] = None        # stage 2 output (ordered block names)
    ordered_ok: bool = False                  # stage 3 accepted the composition order
    proof: Optional[ProofResult] = None       # stage 3 AG proof verdict
    wrapper_path: Optional[str] = None        # composed synthesizable .sv written on PASS
    error: Optional[str] = None               # honest failure message
    stage_failed: Optional[str] = None        # "plan" | "retrieve" | None

    @property
    def ok(self) -> bool:
        return self.error is None


def _find_sby() -> Optional[str]:
    here = Path(__file__).resolve().parents[3]
    sby = shutil.which("sby") or str(here / "oss-cad-suite" / "bin" / "sby")
    return sby if Path(sby).exists() else None


def _sby_verdict(sby: str, td: Path, harness_sv: str, top: str) -> str:
    (td / "h.sv").write_text(harness_sv)
    (td / "t.sby").write_text(
        "[options]\nmode bmc\ndepth 12\n[engines]\nsmtbmc\n"
        f"[script]\nread -sv h.sv\nprep -top {top}\n[files]\nh.sv\n"
    )
    p = subprocess.run([sby, "-f", "t.sby"], cwd=str(td),
                       capture_output=True, text=True, timeout=180)
    m = re.findall(r"DONE \((\w+)", p.stdout + p.stderr)
    return m[-1] if m else "?"


def prove_chain(blocks: list[str], work_dir: Path) -> ProofResult:
    """Run the sound AG composition proof for ``blocks`` if stage 3 has an artifact
    for that exact chain, plus an anti-vacuity check (a corrupted reference must
    fail). Returns a ProofResult with verdict None + a note when no proof applies."""
    entry = _AG_HARNESS.get(tuple(blocks))
    if entry is None:
        return ProofResult(None, None, f"no AG-proof artifact for chain {blocks} "
                                       f"(stage-3 generates AG harnesses for novel chains)")
    rel, top, original, mutated = entry
    harness = Path(work_dir) / rel
    sby = _find_sby()
    if sby is None or not harness.exists():
        return ProofResult(None, None, "sby or AG harness artifact unavailable")

    sv = harness.read_text()
    with tempfile.TemporaryDirectory() as td:
        verdict = _sby_verdict(sby, Path(td), sv, top)
        corrupted = sv.replace(original, mutated)
        anti = _sby_verdict(sby, Path(td), corrupted, top) if corrupted != sv else "?"
    return ProofResult(verdict, anti, "AG composition proof (back-pressure + reset)")


def run_pipeline(
    request: str,
    planner: Planner,
    retriever: BlockRetriever,
    *,
    prove: bool = True,
    work_dir: Path = Path("work"),
    composition_name: str = "composition",
) -> PipelineTrace:
    """request -> stage 1 (plan) -> stage 2 (retrieve) -> stage 3 (order + AG prove)."""
    # Stage 1: plan.
    try:
        plan_result: PlanResult = planner.plan(request)
    except PlannerError as exc:
        return PipelineTrace(request, error=str(exc), stage_failed="plan")
    roles = plan_result.roles

    # Stage 2: derived retrieval -> proven blocks, assembled as the stage-3 input.
    try:
        selected, plan, _ = retriever.retrieve_chain(roles, composition_name=composition_name)
    except RetrievalError as exc:
        return PipelineTrace(request, roles=roles, error=str(exc), stage_failed="retrieve")
    blocks = [s.ir.module for s in selected]

    # Stage 3: deterministic composition ordering (general) + AG proof (existing
    # artifact for a known chain).
    ordered = order_axis_pipeline(plan, selected)
    ordered_ok = ordered is not None
    proof = prove_chain(blocks, work_dir) if (prove and ordered_ok) else None

    # On a PASSing proof, persist the real synthesizable composed wrapper — head
    # s_axis + tail m_axis exposed as top ports, each stage instantiated and wired
    # through internal hop wires. Additive: emit + save only; the proof and retrieval
    # logic above are untouched, and the wrapper is the same generator stage 3 uses.
    wrapper_path: Optional[str] = None
    if proof is not None and proof.verdict == "PASS" and ordered is not None:
        wrapper_sv = emit_pipeline_wrapper(composition_name, ordered)
        if wrapper_sv is not None:
            out = Path(work_dir) / f"{composition_name}.sv"
            out.write_text(wrapper_sv, encoding="utf-8")
            wrapper_path = str(out)

    return PipelineTrace(request, roles=roles, blocks=blocks, ordered_ok=ordered_ok,
                         proof=proof, wrapper_path=wrapper_path)


def format_trace(trace: PipelineTrace) -> str:
    """Human-readable end-to-end trace: request -> roles -> blocks -> proof verdict."""
    lines = [f"request : {trace.request!r}"]
    if trace.roles is not None:
        lines.append("roles   : " + " -> ".join(
            f"{r.function}@{r.data_width}[{r.position}]" for r in trace.roles))
    if trace.error is not None:
        lines.append(f"FAILED  : [{trace.stage_failed}] {trace.error}")
        return "\n".join(lines)
    lines.append("blocks  : " + " -> ".join(trace.blocks or []))
    lines.append(f"stage-3 : order_axis_pipeline accepted = {trace.ordered_ok}")
    if trace.proof is not None:
        if trace.proof.verdict is None:
            lines.append(f"proof   : (skipped) {trace.proof.note}")
        else:
            lines.append(f"proof   : {trace.proof.verdict}  ({trace.proof.note}); "
                         f"anti-vacuity (corrupted ref) = {trace.proof.antivacuity}")
    if trace.wrapper_path:
        lines.append(f"output  : {trace.wrapper_path}  (synthesizable composed top module)")
    return "\n".join(lines)


def _main(argv: list[str]) -> int:
    import os
    logging.basicConfig(level=logging.WARNING)
    if not argv:
        print('usage: python -m specloop.compose.e2e "<request>"')
        return 2
    request = " ".join(argv)

    from specloop.config import SpecloopConfig
    from specloop.gen.client import make_client

    cfg = SpecloopConfig()
    if cfg.llm_backend == "anthropic" and not (cfg.llm_api_key or os.environ.get("ANTHROPIC_API_KEY")):
        print("ANTHROPIC_API_KEY not set")
        return 2
    retriever = BlockRetriever(work_dir=cfg.work_dir)
    planner = Planner.from_retriever(make_client(cfg), retriever)  # library-aware menu
    trace = run_pipeline(request, planner, retriever, work_dir=cfg.work_dir)
    print(format_trace(trace))
    return 0 if trace.ok else 1


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv[1:]))
