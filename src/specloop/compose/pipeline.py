"""CompositionPipeline: orchestrates Steps 2-7 of the composition flow.

Step 1 (decomposition) is handled separately so the CLI can display the plan
before proceeding with the remaining steps.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional

from specloop.formal.backend import FormalBackend
from specloop.gen.client import LLMClient
from specloop.ir.schema import ModuleIR, Port
from specloop.search.searcher import search
from specloop.compose.assertions import CompositionAssertionGenerator
from specloop.compose.axis_pipeline import (
    build_carried_bind,
    emit_pipeline_wrapper,
    order_axis_pipeline,
)
from specloop.compose.compatibility import (
    CompatibilityChecker,
    _is_axis_port,
    axis_connection_roles,
    candidate_role_issues,
    pair_axis_interfaces,
)
from specloop.gen.schema import BindResult
from specloop.compose.schema import (
    CompositionPlan,
    CompositionResult,
    SelectedModule,
)
from specloop.compose.wrapper_gen import WrapperGenerator
from specloop.ppa.target import PPATarget
from specloop.ppa.vector import PPAVector, distance

log = logging.getLogger(__name__)


class CompositionError(Exception):
    pass


# Penalty applied per interface warning (e.g. ENABLE-flag or clock-domain
# mismatch) when ranking otherwise error-free candidates. Large enough to beat a
# small semantic-score lead, so a clean-clock single-stream block is preferred
# over a same-family but clock-crossing one (axis_fifo over axis_async_fifo).
_INTERFACE_WARNING_PENALTY = 0.1

# Identifier that begins a module instantiation: `<modname> #(` or `<modname> inst (`.
_INST_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s+(?:#\s*\(|[A-Za-z_]\w*\s*\()", re.MULTILINE)
_INST_SKIP = frozenset({
    "if", "for", "case", "module", "assign", "always", "begin", "initial",
    "generate", "wire", "reg", "logic", "input", "output", "inout", "localparam",
    "parameter", "genvar", "integer", "function", "task", "posedge", "negedge",
})


def _resolve_rtl_deps(rtl_paths: list[Path]) -> list[Path]:
    """Return the given RTL files plus their transitive submodule files, found by
    scanning each source for instantiations of a module that exists as a sibling
    ``<name>.v``. The IR does not carry submodule lists, so this source-level
    closure is what lets a composition's sub-modules (e.g. axis_pipeline_register's
    internal axis_register) reach the prover instead of being stubbed out."""
    seen: dict[Path, None] = {}
    queue = list(rtl_paths)
    while queue:
        rp = queue.pop(0).resolve()
        if rp in seen:
            continue
        seen[rp] = None
        try:
            text = rp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in _INST_RE.finditer(text):
            name = m.group(1)
            if name in _INST_SKIP:
                continue
            cand = rp.parent / f"{name}.v"
            if cand.exists() and cand.resolve() not in seen:
                queue.append(cand)
    return [Path(p) for p in seen]


def _protocol_prefilter(plan: CompositionPlan, sf_id: str) -> dict:
    """Coarse Qdrant pre-filter derived from a sub-function's *own* connections.

    When a role is wired with AXI-Stream ports it must be filled by an AXI-Stream
    block, so we pre-filter to ``has_axi`` candidates — this keeps a bare-memory
    FIFO (``fifo_sync``: wr_en/rd_en/full/empty) out of an AXI-Stream buffer role
    and stops it starving the real ``axis_fifo`` out of the top-k. The filter is
    conditional on *this* role's connections, never blanket: a req/grant arbiter
    role yields no AXI requirement, so a valid arbiter with no AXI ports of its
    own is never excluded.
    """
    return {"has_axi": True} if axis_connection_roles(plan, sf_id) else {}


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
        param_overrides: Optional[dict[str, dict[str, str]]] = None,
    ) -> None:
        self._client = client
        self._qdrant_url = qdrant_url
        self._collection = collection
        self._embed_model = embed_model
        self._top_k = top_k
        self._min_confidence = min_confidence
        self._min_score = min_score
        # {module_name: {param: value}} overrides applied to the deterministic
        # pipeline wrapper (e.g. cap a FIFO DEPTH for fast proofs). General — the
        # caller supplies them; nothing here is keyed to a specific module.
        self._param_overrides = param_overrides or {}

    def run(
        self,
        request: str,
        plan: CompositionPlan,
        work_dir: Path,
        out_dir: Path,
        formal: Optional[FormalBackend] = None,
        formal_mode: str = "prove",
        formal_repair_iterations: int = 3,
        ppa_target: Optional[PPATarget] = None,
    ) -> CompositionResult:
        # Step 2: Search + candidate selection
        log.info("Step 2: selecting candidates from Qdrant")
        selected, skipped = self._select_modules(plan, work_dir, ppa_target)

        # Step 3: Compatibility check
        log.info("Step 3: checking port compatibility")
        modules_by_id = {s.sub_function_id: s.ir for s in selected}
        compat = CompatibilityChecker().check(modules_by_id, plan)
        if not compat.ok:
            raise CompositionError(
                "Port compatibility errors (fix or remove these connections):\n"
                + "\n".join(f"  [error] {i.message}" for i in compat.errors)
            )

        # Steps 4-5: wrapper + composition bind. A clean linear AXI-Stream pipeline
        # is wired and proven deterministically — the wrapper is emitted from the
        # bundle structure (no LLM glue), and the bind carries each component's own
        # proven assertions onto its instance plus cross-boundary interaction
        # assertions. Anything else falls back to the LLM wrapper/assertion path.
        out_dir.mkdir(parents=True, exist_ok=True)
        wrapper_path = out_dir / f"{plan.composition_name}.sv"
        bind_path = out_dir / f"{plan.composition_name}.bind.sv"

        ordered = order_axis_pipeline(plan, selected)
        wrapper_sv = (
            emit_pipeline_wrapper(plan.composition_name, ordered, self._param_overrides)
            if ordered is not None else None
        )
        deterministic = wrapper_sv is not None
        carried = None

        if deterministic:
            log.info("Step 4: deterministic AXI-Stream pipeline wrapper (%d stages, no LLM)", len(ordered))
            wrapper_path.write_text(wrapper_sv, encoding="utf-8")
            log.info("Step 5: carried-proof bind (component proofs + interaction assertions)")
            carried = build_carried_bind(plan.composition_name, ordered, work_dir)
            bind_result = BindResult(
                bind_module_sv=carried.bind_sv,
                assertion_index=carried.assertion_index,
                model_id="deterministic",
            )
            log.info(
                "Carried %d proven assertions from %d component(s); %d cross-boundary "
                "interaction assertions", carried.n_carried, len(carried.carried_modules),
                carried.n_interaction,
            )
            # HONESTY: the open-source Yosys front-end (sby backend) silently ignores
            # SystemVerilog `bind`, so these bind-attached assertions are NOT actually
            # checked there — a PASS is vacuous. Only a bind-aware front-end (synlig)
            # or a closed harness (see compose.axis_pipeline.emit_roundtrip_harness)
            # verifies them. Do not report this proof as sound under the sby backend.
            log.warning(
                "Carried/interaction assertions are attached via `bind`; under the "
                "open-source sby backend `bind` is ignored, so this composition proof "
                "is VACUOUS. Use a bind-aware backend or the round-trip harness for a "
                "sound end-to-end result."
            )
        else:
            log.info("Step 4: generating SystemVerilog wrapper (LLM)")
            wrapper_sv = WrapperGenerator(self._client).generate(request, plan, selected, compat)
            wrapper_path.write_text(wrapper_sv, encoding="utf-8")
            log.info("Step 5: generating composition assertions (LLM)")
            bind_result = CompositionAssertionGenerator(self._client).generate(
                request, plan, selected, wrapper_sv, work_dir=work_dir
            )
        bind_path.write_text(bind_result.bind_module_sv, encoding="utf-8")
        log.info("Wrapper -> %s ; bind -> %s", wrapper_path, bind_path)

        # Step 6: SBY formal verification (optional)
        formal_result = None
        confidence = 0.0

        if formal is not None:
            log.info("Step 6: running SBY on composition (mode=%s)", formal_mode)
            deps = _resolve_rtl_deps([s.rtl_path for s in selected if s.rtl_path.exists()])
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

            # Repair loop if needed. Skipped for the deterministic path: its bind
            # carries proven component assertions verbatim, so LLM repair would only
            # discard real proofs rather than fix anything.
            if formal_result.status != "pass" and formal_repair_iterations > 0 and not deterministic:
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
            deterministic=deterministic,
            n_interaction_assertions=carried.n_interaction if carried else 0,
            n_carried_assertions=carried.n_carried if carried else 0,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _select_modules(
        self, plan: CompositionPlan, work_dir: Path,
        ppa_target: Optional[PPATarget] = None,
    ) -> tuple[list[SelectedModule], list[str]]:
        """Return (selected, skipped_warnings).

        Selection is interface-aware: each role is pre-filtered to the protocol
        family its connections require, then candidates are ranked so only blocks
        whose AXI-Stream interface is compatible with the role and its already-
        selected upstream neighbour are chosen — keeping a bare-memory FIFO out of
        an AXI-Stream buffer slot. Sub-functions whose best search score falls
        below self._min_score are skipped rather than filled with a wrong module.
        """
        selected: list[SelectedModule] = []
        skipped: list[str] = []
        selected_by_id: dict[str, ModuleIR] = {}       # sub_function_id -> chosen IR
        ir_cache: dict[str, Optional[ModuleIR]] = {}    # module_name -> real IR (or None)

        for sf in plan.sub_functions:
            required_roles = axis_connection_roles(plan, sf.id)

            # Coarse, role-conditional protocol pre-filter (never a blanket has_axi).
            prefilter = _protocol_prefilter(plan, sf.id)
            results = search(
                sf.search_query, self._qdrant_url, self._collection,
                self._embed_model, top_k=self._top_k, **prefilter,
            )

            # Starvation fallback: a thin index must never block composition.
            if len(results) < 2 and prefilter:
                log.info(
                    "Protocol-filtered search returned %d results for '%s' — falling back to unfiltered",
                    len(results), sf.search_query,
                )
                results = search(
                    sf.search_query, self._qdrant_url, self._collection,
                    self._embed_model, top_k=self._top_k,
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

            best, ir, ppa_used, iface_note = self._select_interface_aware(
                eligible, required_roles, sf.id, plan, selected_by_id, work_dir,
                ir_cache, ppa_target,
            )

            log.info(
                "Selected '%s' for sub-function '%s' (score=%.4f, confidence=%.2f, "
                "ppa_aware=%s, interface=%s)",
                best.module_name, sf.id, best.score, best.confidence, ppa_used, iface_note,
            )

            if ir is None:
                log.warning(
                    "IR not found for '%s' in %s — using minimal fallback",
                    best.module_name, work_dir,
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
            selected_by_id[sf.id] = ir

        if not selected:
            raise CompositionError(
                "All sub-functions were skipped (no search results met the score threshold). "
                "Index more modules or lower --min-score.\n"
                + "\n".join(f"  {w}" for w in skipped)
            )

        return selected, skipped

    def _select_interface_aware(
        self,
        eligible: list,
        required_roles: set,
        sf_id: str,
        plan: CompositionPlan,
        selected_by_id: dict,
        work_dir: Path,
        ir_cache: dict,
        ppa_target: Optional[PPATarget],
    ):
        """Pick the best eligible candidate whose interface fits the role.

        Each candidate is scored for interface errors (missing the AXI-Stream
        bundle its role needs, or a width/direction conflict with the upstream
        module already selected) and warnings (ENABLE-flag / clock-domain
        mismatch). Candidates with errors are dropped whenever a clean one exists;
        among the rest the existing semantic (or PPA-blended) score decides, minus
        a per-warning penalty so a clean single-clock block wins over a same-family
        clock-crossing one. Candidates with no IR on disk are left unchecked rather
        than penalised. Returns (best, best_ir_or_None, ppa_used, note).
        """
        errors: dict[str, int] = {}
        warnings: dict[str, int] = {}
        irs: dict[str, Optional[ModuleIR]] = {}
        for r in eligible:
            ir = self._load_real_ir(r.module_name, work_dir, ir_cache)
            irs[r.module_name] = ir
            if ir is None:
                errors[r.module_name] = warnings[r.module_name] = 0
                continue
            issues = candidate_role_issues(ir, required_roles)
            for conn in plan.connections:
                if (conn.to_id == sf_id and conn.from_id in selected_by_id
                        and _is_axis_port(conn.to_port)):
                    issues += pair_axis_interfaces(
                        conn.from_id, selected_by_id[conn.from_id], sf_id, ir,
                    )
            errors[r.module_name] = sum(1 for i in issues if i.severity == "error")
            warnings[r.module_name] = sum(1 for i in issues if i.severity == "warning")

        compatible = [r for r in eligible if errors[r.module_name] == 0]
        pool = compatible or eligible
        excluded = [r.module_name for r in eligible if errors[r.module_name] > 0]

        base_metric, ppa_used = self._base_metric(pool, ppa_target)
        best = max(
            pool,
            key=lambda r: base_metric(r) - _INTERFACE_WARNING_PENALTY * warnings[r.module_name],
        )

        note = "ok" if warnings[best.module_name] == 0 else f"warnings={warnings[best.module_name]}"
        if excluded:
            note += f"; excluded interface-incompatible={excluded}"
        return best, irs[best.module_name], ppa_used, note

    def _base_metric(self, pool: list, ppa_target: Optional[PPATarget]):
        """Return (metric_fn, ppa_used) — the semantic ranker, optionally PPA-blended.

        With a PPA target and more than one candidate, blends 60% semantic score
        with 40% PPA proximity to the target; otherwise pure score×confidence.
        Modules without PPA payload default to 0.5, degrading gracefully.
        """
        if ppa_target is None or len(pool) <= 1:
            return (lambda r: r.score * r.confidence), False

        target_vec = PPAVector(
            latency=ppa_target.latency, throughput=ppa_target.throughput,
            area=ppa_target.area, power=ppa_target.power,
        )

        def metric(r):
            candidate_vec = PPAVector(
                latency=r.ppa_latency, throughput=r.ppa_throughput,
                area=r.ppa_area, power=r.ppa_power,
            )
            return 0.6 * r.score * r.confidence + 0.4 * (1.0 - distance(candidate_vec, target_vec))

        return metric, True

    @staticmethod
    def _load_real_ir(
        module_name: str, work_dir: Path, cache: dict,
    ) -> Optional[ModuleIR]:
        """Load a candidate's ModuleIR from work_dir, cached. None when absent or
        unparseable — the interface check then skips that candidate rather than
        treating missing data as incompatibility."""
        if module_name in cache:
            return cache[module_name]
        ir_path = work_dir / f"{module_name}.ir.json"
        ir: Optional[ModuleIR] = None
        if ir_path.exists():
            try:
                ir = ModuleIR.model_validate(json.loads(ir_path.read_text()))
            except Exception as exc:  # noqa: BLE001 - defensive; bad IR must not break selection
                log.warning("Failed to parse IR for '%s' (%s) — skipping interface check",
                            module_name, exc)
        cache[module_name] = ir
        return ir


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
