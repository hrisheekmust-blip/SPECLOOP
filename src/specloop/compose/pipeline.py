"""CompositionPipeline: orchestrates Steps 2-7 of the composition flow.

Step 1 (decomposition) is handled separately so the CLI can display the plan
before proceeding with the remaining steps.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from specloop.formal.backend import FormalBackend
from specloop.gen.client import LLMClient
from specloop.ir.schema import ModuleIR, Port
from specloop.search.searcher import search
from specloop.compose.assertions import CompositionAssertionGenerator
from specloop.compose.compatibility import CompatibilityChecker
from specloop.compose.schema import (
    CompositionPlan,
    CompositionResult,
    SelectedModule,
)
from specloop.compose.wrapper_gen import WrapperGenerator

log = logging.getLogger(__name__)


class CompositionError(Exception):
    pass


class CompositionPipeline:
    def __init__(
        self,
        client: LLMClient,
        qdrant_url: str,
        collection: str,
        embed_model: str,
        top_k: int = 3,
        min_confidence: float = 0.5,
        min_score: float = 0.70,
    ) -> None:
        self._client = client
        self._qdrant_url = qdrant_url
        self._collection = collection
        self._embed_model = embed_model
        self._top_k = top_k
        self._min_confidence = min_confidence
        self._min_score = min_score

    def run(
        self,
        request: str,
        plan: CompositionPlan,
        work_dir: Path,
        out_dir: Path,
        formal: Optional[FormalBackend] = None,
        formal_mode: str = "prove",
        formal_repair_iterations: int = 3,
    ) -> CompositionResult:
        # Step 2: Search + candidate selection
        log.info("Step 2: selecting candidates from Qdrant")
        selected, skipped = self._select_modules(plan, work_dir)

        # Step 3: Compatibility check
        log.info("Step 3: checking port compatibility")
        modules_by_id = {s.sub_function_id: s.ir for s in selected}
        compat = CompatibilityChecker().check(modules_by_id, plan)
        if not compat.ok:
            raise CompositionError(
                "Port compatibility errors (fix or remove these connections):\n"
                + "\n".join(f"  [error] {i.message}" for i in compat.errors)
            )

        # Step 4: Wrapper generation
        log.info("Step 4: generating SystemVerilog wrapper")
        wrapper_sv = WrapperGenerator(self._client).generate(request, plan, selected, compat)

        out_dir.mkdir(parents=True, exist_ok=True)
        wrapper_path = out_dir / f"{plan.composition_name}.sv"
        wrapper_path.write_text(wrapper_sv, encoding="utf-8")
        log.info("Wrapper written to %s", wrapper_path)

        # Step 5: Composition assertions
        log.info("Step 5: generating composition assertions")
        bind_result = CompositionAssertionGenerator(self._client).generate(
            request, plan, selected, wrapper_sv
        )
        bind_path = out_dir / f"{plan.composition_name}.bind.sv"
        bind_path.write_text(bind_result.bind_module_sv, encoding="utf-8")
        log.info("Bind module written to %s", bind_path)

        # Step 6: SBY formal verification (optional)
        formal_result = None
        confidence = 0.0

        if formal is not None:
            log.info("Step 6: running SBY on composition (mode=%s)", formal_mode)
            deps = [s.rtl_path for s in selected if s.rtl_path.exists()]
            formal_result = formal.run(
                module_name=plan.composition_name,
                rtl_path=wrapper_path,
                bind_path=bind_path,
                deps=deps,
                work_dir=out_dir,
                assertion_index=bind_result.assertion_index,
                mode=formal_mode,
            )
            confidence = formal_result.confidence

            # Repair loop if needed
            if formal_result.status != "pass" and formal_repair_iterations > 0:
                from specloop.loop.repair import RepairLoop

                wrapper_ir = _build_wrapper_ir(plan, selected, wrapper_path)
                repair_loop = RepairLoop(
                    client=self._client,
                    formal=formal,
                    max_iterations=formal_repair_iterations,
                    mode=formal_mode,
                )
                bind_result, formal_result, _ = repair_loop.run(
                    ir=wrapper_ir,
                    rtl_source=wrapper_sv,
                    bind_result=bind_result,
                    initial_formal=formal_result,
                    work_dir=out_dir,
                    deps=deps,
                )
                bind_path.write_text(bind_result.bind_module_sv, encoding="utf-8")
                confidence = formal_result.confidence

        return CompositionResult(
            composition_name=plan.composition_name,
            plan=plan,
            selected_modules=selected,
            skipped_sub_functions=skipped,
            compatibility=compat,
            wrapper_sv_path=wrapper_path,
            bind_sv_path=bind_path,
            bind_result=bind_result,
            formal_result=formal_result,
            confidence=confidence,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _select_modules(
        self, plan: CompositionPlan, work_dir: Path
    ) -> tuple[list[SelectedModule], list[str]]:
        """Return (selected, skipped_warnings).

        Skips sub-functions whose best search score falls below self._min_score
        rather than using a semantically wrong module.
        """
        selected: list[SelectedModule] = []
        skipped: list[str] = []

        for sf in plan.sub_functions:
            results = search(
                sf.search_query,
                self._qdrant_url,
                self._collection,
                self._embed_model,
                top_k=self._top_k,
            )

            if not results:
                raise CompositionError(
                    f"No indexed module found for sub-function '{sf.name}' "
                    f"(query: '{sf.search_query}'). "
                    f"Run 'specloop spec <module> && specloop index <module>' first."
                )

            # Score gate: if the best available match is semantically too far away,
            # skip rather than pollute the composition with a wrong module.
            overall_best = max(results, key=lambda r: r.score)
            if overall_best.score < self._min_score:
                msg = (
                    f"No good match found for sub-function '{sf.name}' — "
                    f"best score {overall_best.score:.3f} < {self._min_score} "
                    f"('{overall_best.module_name}') — skipping"
                )
                log.warning(msg)
                skipped.append(msg)
                continue

            eligible = [r for r in results if r.confidence >= self._min_confidence]
            if not eligible:
                best = results[0]
                raise CompositionError(
                    f"No module meets min-confidence {self._min_confidence} for '{sf.name}'. "
                    f"Best candidate: '{best.module_name}' "
                    f"(confidence={best.confidence:.2f}, score={best.score:.4f}). "
                    f"Try --min-confidence {best.confidence:.1f} or index a better-verified module."
                )

            best = max(eligible, key=lambda r: r.score * r.confidence)
            log.info(
                "Selected '%s' for sub-function '%s' (score=%.4f, confidence=%.2f)",
                best.module_name, sf.id, best.score, best.confidence,
            )

            # Load ModuleIR from work dir
            ir_path = work_dir / f"{best.module_name}.ir.json"
            if ir_path.exists():
                ir = ModuleIR.model_validate(json.loads(ir_path.read_text()))
            else:
                log.warning(
                    "IR not found for '%s' at %s — using minimal fallback",
                    best.module_name, ir_path,
                )
                ir = ModuleIR(
                    module=best.module_name,
                    file=best.file_path,
                    module_type=best.module_type,
                )

            rtl_path = Path(ir.file)
            if not rtl_path.exists():
                raise CompositionError(
                    f"RTL for '{best.module_name}' not found at '{ir.file}'. "
                    f"The Qdrant index points to a file that doesn't exist locally. "
                    f"Re-run 'specloop ingest' and 'specloop index' with local RTL files."
                )

            selected.append(SelectedModule(
                sub_function_id=sf.id,
                search_result=best,
                ir=ir,
                rtl_path=rtl_path,
            ))

        if not selected:
            raise CompositionError(
                "All sub-functions were skipped (no search results met the score threshold). "
                "Index more modules or lower --min-score.\n"
                + "\n".join(f"  {w}" for w in skipped)
            )

        return selected, skipped


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_wrapper_ir(
    plan: CompositionPlan,
    selected: list[SelectedModule],
    wrapper_path: Path,
) -> ModuleIR:
    """Build a minimal ModuleIR for the wrapper to drive the repair loop."""
    connected_keys: set[str] = set()
    for conn in plan.connections:
        connected_keys.add(f"{conn.from_id}.{conn.from_port}")
        connected_keys.add(f"{conn.to_id}.{conn.to_port}")

    external_ports: list[Port] = []
    seen_names: set[str] = set()

    for sm in selected:
        for p in sm.ir.ports:
            port_key = f"{sm.sub_function_id}.{p.name}"
            if port_key not in connected_keys and p.name not in seen_names:
                seen_names.add(p.name)
                external_ports.append(p)

    return ModuleIR(
        module=plan.composition_name,
        file=str(wrapper_path),
        ports=external_ports,
        module_type="interface",
    )
