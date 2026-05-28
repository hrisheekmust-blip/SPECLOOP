"""Iterative repair loop: SBY failure → LLM fix → re-verify → repeat.

On success, upgrades the pending ProvenPair in TrainingLogger to a confirmed one
and logs each intermediate RepairStep for DPO/preference training.
"""
from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

from specloop.formal.backend import FormalBackend, FormalResult
from specloop.gen.client import LLMClient
from specloop.gen.pipeline import _parse_json, _sanitize_sv, _WRAPPER_SUFFIX, _SEP
from specloop.gen.schema import BindResult
from specloop.ir.schema import ModuleIR
from specloop.training.schema import AssertionEntry, ProofSummary, ProvenPair, RepairStep

log = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).parent.parent / "gen" / "prompts"


class RepairLoop:
    """Iterative formal verification repair loop."""

    def __init__(
        self,
        client: LLMClient,
        formal: FormalBackend,
        max_iterations: int = 3,
        mode: str = "prove",
    ) -> None:
        self._client = client
        self._formal = formal
        self._max_iter = max_iterations
        self._mode = mode

    def run(
        self,
        ir: ModuleIR,
        rtl_source: str,
        bind_result: BindResult,
        initial_formal: FormalResult,
        work_dir: Path,
        deps: list[Path] | None = None,
    ) -> tuple[BindResult, FormalResult, list[RepairStep]]:
        """Run the repair loop.

        Returns (final_bind_result, final_formal_result, list_of_repair_steps).
        The caller is responsible for logging the repair steps and upgrading the
        ProvenPair once the final result is known.
        """
        deps = deps or []
        rtl_path = Path(ir.file)
        bind_path = work_dir / f"{ir.module}.bind.sv"

        current_bind = bind_result
        current_formal = initial_formal
        repair_steps: list[RepairStep] = []

        for iteration in range(1, self._max_iter + 1):
            if current_formal.status == "pass":
                log.info("No repair needed — all assertions pass.")
                break

            # Fix #10: UNKNOWN means no CEX — the LLM has nothing to work from.
            if current_formal.status == "unknown":
                log.warning(
                    "Repair iter %d/%d: status UNKNOWN — no counterexample available, "
                    "cannot repair. Consider switching to bmc mode.",
                    iteration, self._max_iter,
                )
                break

            log.info(
                "Repair iteration %d/%d for '%s' (status=%s)",
                iteration, self._max_iter, ir.module, current_formal.status,
            )

            # Classify failure type for prompt
            failure_type = _classify_failure(current_formal)

            # Build CEX description
            cex_desc = _build_cex_description(current_formal, failure_type, ir)

            # Call LLM for repair
            system, user = _render_repair_prompt(
                ir=ir,
                rtl_source=rtl_source,
                bind_module_sv=current_bind.bind_module_sv,
                failed_assertions=current_formal.failed_assertions,
                passed_assertions=[
                    a for a in current_formal.assertions if a.status == "pass"
                ],
                failure_type=failure_type,
                cex_description=cex_desc,
                iteration=iteration,
                max_iterations=self._max_iter,
            )
            raw = self._client.generate(system, user)
            data = _parse_json(raw, f"repair_iter_{iteration}")

            bind_sv = _sanitize_sv(data.get("bind_module", ""))  # fix #5
            if not bind_sv:
                log.warning("Repair iter %d: LLM returned no bind_module", iteration)
                repair_steps.append(RepairStep(
                    module_name=ir.module,
                    module_type=ir.module_type or "unknown",
                    file_path=ir.file,
                    rtl_source=rtl_source,
                    module_ir=ir.model_dump(),
                    iteration=iteration,
                    failed_bind_sv=current_bind.bind_module_sv,
                    failure_type=failure_type,
                    cex_nl=cex_desc,
                    repaired_bind_sv="",
                    repair_succeeded=False,
                    model_id=self._client.model_id,
                ))
                continue

            # Fix #6: stuck detector — bail if LLM reproduced the same bind module
            if (hashlib.md5(bind_sv.encode()).hexdigest() ==
                    hashlib.md5(current_bind.bind_module_sv.encode()).hexdigest()):
                log.warning(
                    "Repair iter %d: LLM produced identical bind module — stuck, stopping.",
                    iteration,
                )
                break

            # Build new BindResult
            index_raw = data.get("assertion_index", [])
            new_index = []
            for entry in index_raw:
                try:
                    new_index.append(AssertionEntry.model_validate(entry))
                except Exception:
                    pass

            new_bind = BindResult(
                bind_module_sv=bind_sv,
                assertion_index=new_index or current_bind.assertion_index,
                model_id=self._client.model_id,
                stage1=current_bind.stage1,
                stage2=current_bind.stage2,
            )

            # Write repaired bind module
            bind_path.write_text(bind_sv, encoding="utf-8")

            # Re-run SBY
            new_formal = self._formal.run(
                module_name=ir.module,
                rtl_path=rtl_path,
                bind_path=bind_path,
                deps=deps,
                work_dir=work_dir,
                assertion_index=new_bind.assertion_index,
                mode=self._mode,
            )

            succeeded = new_formal.status == "pass"
            repair_steps.append(RepairStep(
                module_name=ir.module,
                module_type=ir.module_type or "unknown",
                file_path=ir.file,
                rtl_source=rtl_source,
                module_ir=ir.model_dump(),
                iteration=iteration,
                failed_bind_sv=current_bind.bind_module_sv,
                failure_type=failure_type,
                cex_nl=cex_desc,
                repaired_bind_sv=bind_sv,
                repair_succeeded=succeeded,
                model_id=self._client.model_id,
            ))

            current_bind = new_bind
            current_formal = new_formal

            if succeeded:
                log.info("Repair succeeded on iteration %d", iteration)
                break
            else:
                log.info(
                    "Repair iter %d still failing (status=%s, confidence=%.0f%%)",
                    iteration, new_formal.status, new_formal.confidence * 100,
                )

        return current_bind, current_formal, repair_steps


def upgrade_to_proven(
    pending_pair: ProvenPair,
    bind_result: BindResult,
    formal_result: FormalResult,
    repair_iterations: int,
) -> ProvenPair:
    """Return a new ProvenPair with confirmed proof status, ready for TrainingLogger."""
    n_proven = formal_result.n_proven or len(bind_result.assertion_index)
    n_total = len(bind_result.assertion_index) or len(formal_result.assertions)

    status = "all_proven" if formal_result.status == "pass" else "partial"

    return ProvenPair(
        module_name=pending_pair.module_name,
        module_type=pending_pair.module_type,
        file_path=pending_pair.file_path,
        rtl_source=pending_pair.rtl_source,
        module_ir=pending_pair.module_ir,
        bind_module_sv=bind_result.bind_module_sv,
        assertion_index=bind_result.assertion_index,
        proof=ProofSummary(
            status=status,
            proven=n_proven,
            total=n_total,
            depth=0,
            engine="smtbmc",
            wall_seconds=formal_result.wall_seconds,
        ),
        model_id=bind_result.model_id,
        repair_iterations=repair_iterations,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _classify_failure(fr: FormalResult) -> str:
    mapping = {
        "compile_error": "CompileError",
        "timeout": "Timeout",
        "fail": "FormalFail",
        "unknown": "Unknown",
    }
    return mapping.get(fr.status, "FormalFail")


def _diagnose_compile_error(log_tail: str, ir: ModuleIR) -> str:
    """Classify each signal name in compile errors as a real port/signal or hallucinated."""
    error_lines = [ln for ln in log_tail.splitlines() if "error" in ln.lower()][:20]

    # Extract quoted/single-quoted identifiers — covers Yosys and slang error patterns:
    # "Unknown identifier 'foo'", "Wire 'foo' is not defined", "use of undeclared identifier 'foo'"
    candidates: set[str] = set()
    _IDENT_RE = re.compile(r"['\"]([A-Za-z_]\w*)['\"]")
    for ln in error_lines:
        candidates.update(_IDENT_RE.findall(ln))

    known_ports = {p.name: p for p in ir.ports}
    # signals_written/signals_read are populated by Change 2; degrade to empty if absent
    known_signals: set[str] = set()
    for block in ir.always_blocks:
        known_signals.update(getattr(block, "signals_written", []))
        known_signals.update(getattr(block, "signals_read", []))

    diag_lines = ["Compile error diagnosis:"]
    for name in sorted(candidates):
        if name in known_ports:
            p = known_ports[name]
            width_str = f" [{p.width - 1}:0]" if p.width > 1 else ""
            diag_lines.append(
                f"  '{name}' EXISTS as port: {p.direction} logic{width_str} {p.name}"
            )
        elif name in known_signals:
            diag_lines.append(f"  '{name}' EXISTS as internal signal (not a top-level port)")
        elif len(name) > 2 and not name.isdigit():
            diag_lines.append(
                f"  '{name}' NOT FOUND in module IR — likely a hallucinated name, do not use it"
            )

    diag_lines.append("\nRaw error lines:")
    diag_lines.extend(f"  {ln}" for ln in error_lines[:10])
    return "\n".join(diag_lines)


def _build_cex_description(fr: FormalResult, failure_type: str, ir: ModuleIR) -> str:
    if failure_type == "CompileError":
        return _diagnose_compile_error(fr.log_tail, ir)

    if failure_type == "FormalFail":
        parts: list[str] = []
        failed = [a for a in fr.assertions if a.status == "fail"]
        if failed:
            parts.append("Failed assertions:")
            for a in failed[:5]:
                parts.append(f"  - {a.name}: {a.message or 'assertion violated'}")
        if fr.counterexample_nl:
            parts.append("\nCounterexample trace (exact signal values that triggered the violation):")
            parts.append(fr.counterexample_nl)
        return "\n".join(parts) if parts else "Formal verification failed."

    # Timeout / Unknown — return raw log tail
    return fr.log_tail[-1000:] if fr.log_tail else "No diagnostic information available."


def _render_repair_prompt(
    ir: ModuleIR,
    rtl_source: str,
    bind_module_sv: str,
    failed_assertions,
    passed_assertions,
    failure_type: str,
    cex_description: str,
    iteration: int,
    max_iterations: int,
) -> tuple[str, str]:
    from jinja2 import DictLoader, Environment, FileSystemLoader

    file_env = Environment(
        loader=FileSystemLoader(str(_PROMPTS_DIR)),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    src = file_env.loader.get_source(file_env, "repair.j2")[0]
    patched_env = Environment(
        loader=DictLoader({"__tpl__": src + _WRAPPER_SUFFIX}),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    rendered = patched_env.get_template("__tpl__").render(
        ir=ir,
        rtl_source=rtl_source,
        bind_module_sv=bind_module_sv,
        failed_assertions=failed_assertions,
        passed_assertions=passed_assertions,
        failure_type=failure_type,
        cex_description=cex_description,
        iteration=iteration,
        max_iterations=max_iterations,
    )
    parts = rendered.split(_SEP)
    system = parts[1].strip() if len(parts) > 1 else ""
    user = parts[2].strip() if len(parts) > 2 else ""
    return system, user
