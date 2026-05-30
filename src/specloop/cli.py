"""SpecLoop CLI."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.syntax import Syntax
from rich.table import Table
from rich.tree import Tree

from specloop.ir.extractor import extract_modules
from specloop.ir.preamble import PreambleCapsule
from specloop.deps.resolver import DependencyResolver
from specloop.deps.blackbox import get_vendor_stubs
from specloop.classify.module_type import classify

app = typer.Typer(help="SpecLoop — RTL formal spec generator", add_completion=False)
console = Console()

_STATUS_STYLE = {"ok": "green", "partial": "yellow", "failed": "red"}
_TYPE_STYLE = {
    "fsm": "cyan", "memory": "blue", "sequential": "magenta",
    "combinational": "white", "interface": "yellow", "blackbox": "red",
}


def _work_dir() -> Path:
    d = Path("work")
    d.mkdir(exist_ok=True)
    return d


def _load_irs(work: Path) -> dict:
    """Load all cached IR JSON files from work dir."""
    irs = {}
    for f in work.glob("*.ir.json"):
        try:
            data = json.loads(f.read_text())
            irs[data["module"]] = data
        except Exception:
            pass
    return irs


@app.command()
def ingest(
    path: Path = typer.Argument(..., help="Directory of .sv/.v files or a .f filelist"),
    top: Optional[str] = typer.Option(None, "--top", "-t", help="Top module name for filelist generation"),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Output directory (default: work/)"),
):
    """Parse RTL files and emit ModuleIR JSON for each module."""
    if not path.exists():
        console.print(f"[red]Path not found: {path}[/red]")
        raise typer.Exit(1)

    work = out or _work_dir()
    work.mkdir(parents=True, exist_ok=True)

    from specloop.config import SpecloopConfig
    cfg = SpecloopConfig()
    inc_dirs = cfg.rtl_include_dirs or None
    def_files = cfg.rtl_define_files or None
    pkg_files = cfg.rtl_package_files or None

    with console.status(f"[bold]Parsing {path}…[/bold]"):
        irs = extract_modules(
            path,
            include_dirs=inc_dirs,
            define_files=def_files,
            package_files=pkg_files,
        )

    if not irs:
        console.print("[yellow]No modules found.[/yellow]")
        raise typer.Exit(0)

    # Classify all modules
    for ir in irs:
        classify(ir)

    # Build resolver
    resolver = DependencyResolver(irs)

    # Write IR JSON files
    for ir in irs:
        out_file = work / f"{ir.module}.ir.json"
        out_file.write_text(ir.model_dump_json(indent=2))

    # Write filelists for each module (or just the named top)
    targets = [top] if top else resolver.roots()
    for t in targets:
        try:
            fl = resolver.write_filelist(t, work / f"{t}.f")
        except KeyError:
            pass

    # Print summary table
    table = Table(title=f"Ingested: {path}", show_lines=False)
    table.add_column("Module", style="bold")
    table.add_column("Type")
    table.add_column("Ports", justify="right")
    table.add_column("Params", justify="right")
    table.add_column("Submodules", justify="right")
    table.add_column("Always", justify="right")
    table.add_column("Status")

    for ir in sorted(irs, key=lambda x: x.module):
        t_style = _TYPE_STYLE.get(ir.module_type or "", "white")
        s_style = _STATUS_STYLE.get(ir.parse_status, "white")
        table.add_row(
            ir.module,
            f"[{t_style}]{ir.module_type or '?'}[/{t_style}]",
            str(len(ir.ports)),
            str(len(ir.parameters)),
            str(len(ir.submodules)),
            str(len(ir.always_blocks)),
            f"[{s_style}]{ir.parse_status}[/{s_style}]",
        )

    console.print(table)
    console.print(f"[dim]IR files written to {work}/[/dim]")

    # Report missing dependencies
    all_missing = set()
    for t in targets:
        try:
            all_missing.update(resolver.missing(t))
        except KeyError:
            pass
    if all_missing:
        console.print(f"[yellow]Unresolved modules: {', '.join(sorted(all_missing))}[/yellow]")
        stubs = get_vendor_stubs(list(all_missing))
        if stubs:
            console.print(f"[dim]Vendor stubs available: {[s.name for s in stubs]}[/dim]")


@app.command()
def show(
    module: str = typer.Argument(..., help="Module name to display"),
    work: Path = typer.Option(Path("work"), "--work", "-w"),
):
    """Pretty-print the cached ModuleIR JSON for a module."""
    ir_file = work / f"{module}.ir.json"
    if not ir_file.exists():
        console.print(f"[red]No IR found for '{module}'. Run 'specloop ingest' first.[/red]")
        raise typer.Exit(1)

    data = json.loads(ir_file.read_text())
    console.print_json(json.dumps(data, indent=2))


@app.command()
def deps(
    module: str = typer.Argument(..., help="Top module name"),
    work: Path = typer.Option(Path("work"), "--work", "-w"),
):
    """Print the dependency tree for a module."""
    # Load all cached IRs and reconstruct resolver
    irs_raw = _load_irs(work)
    if not irs_raw:
        console.print("[red]No IR files found. Run 'specloop ingest' first.[/red]")
        raise typer.Exit(1)

    from specloop.ir.schema import ModuleIR
    ir_objects = [ModuleIR.model_validate(v) for v in irs_raw.values()]
    resolver = DependencyResolver(ir_objects)

    if module not in resolver.all_modules():
        console.print(f"[red]Module '{module}' not found in cached IR.[/red]")
        raise typer.Exit(1)

    def build_tree(name: str, tree_node, visited: set) -> None:
        if name in visited:
            tree_node.add(f"[dim]{name} (circular)[/dim]")
            return
        visited.add(name)
        ir = resolver.get_ir(name)
        label = f"[bold]{name}[/bold]"
        if ir:
            t_style = _TYPE_STYLE.get(ir.module_type or "", "white")
            label += f" [{t_style}]{ir.module_type or '?'}[/{t_style}]"
        child_node = tree_node.add(label)
        if ir:
            for sub in ir.submodules:
                build_tree(sub.module_name, child_node, visited.copy())

    tree = Tree(f":deciduous_tree: [bold]{module}[/bold]")
    ir = resolver.get_ir(module)
    if ir:
        for sub in ir.submodules:
            build_tree(sub.module_name, tree, {module})
    console.print(tree)

    try:
        closure = resolver.closure(module)
        console.print(f"\n[dim]Closure ({len(closure)} modules): {' → '.join(closure)}[/dim]")
        missing = resolver.missing(module)
        if missing:
            console.print(f"[yellow]Missing: {missing}[/yellow]")
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")



@app.command()
def spec(
    module: str = typer.Argument(..., help="Module name to generate assertions for"),
    work: Path = typer.Option(Path("work"), "--work", "-w"),
    backend: Optional[str] = typer.Option(None, "--backend", "-b", help="Override LLM backend: anthropic|vllm|ollama"),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Override LLM model name"),
    mode: Optional[str] = typer.Option(None, "--mode", help="Formal mode: bmc|prove|cover"),
    no_verify: bool = typer.Option(False, "--no-verify", "-n", help="Skip SBY verification"),
    sby_debug: bool = typer.Option(False, "--sby-debug", help="Print raw SBY stdout/stderr"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print prompts without calling LLM"),
    no_rtl_source: bool = typer.Option(False, "--no-rtl-source", help="Omit raw RTL from prompts; send IR JSON only (reduces token usage)"),
):
    """Generate SVA assertions, run formal verification, and repair failures."""
    from specloop.config import SpecloopConfig
    from specloop.gen.client import make_client
    from specloop.gen.pipeline import AssertionPipeline
    from specloop.ir.schema import ModuleIR
    from specloop.training.logger import TrainingLogger
    from specloop.training.schema import ProofSummary, ProvenPair

    ir_file = work / f"{module}.ir.json"
    if not ir_file.exists():
        console.print(f"[red]No IR found for '{module}'. Run 'specloop ingest' first.[/red]")
        raise typer.Exit(1)

    ir = ModuleIR.model_validate(json.loads(ir_file.read_text()))
    rtl_path = Path(ir.file)
    if not rtl_path.exists():
        console.print(f"[red]RTL file not found: {ir.file}[/red]")
        raise typer.Exit(1)
    rtl_source = "" if no_rtl_source else rtl_path.read_text(encoding="utf-8")

    # Config with optional CLI overrides
    cfg = SpecloopConfig()
    updates: dict = {}
    if backend:
        updates["llm_backend"] = backend
    if model:
        updates["llm_model"] = model
    if mode:
        updates["formal_mode"] = mode
    if sby_debug:
        updates["formal_debug"] = True
    if updates:
        cfg = cfg.model_copy(update=updates)

    # --dry-run: render prompts without calling LLM
    if dry_run:
        _dry_run_prompts(ir, rtl_source)
        if no_rtl_source:
            console.print("[dim](--no-rtl-source: RTL omitted from prompts)[/dim]")
        raise typer.Exit(0)

    # ── Stage 1-3: LLM assertion pipeline ──────────────────────────────────
    client = make_client(cfg)
    console.print(f"[dim]LLM: {cfg.llm_backend} / {client.model_id}[/dim]")
    if no_rtl_source:
        console.print("[dim](--no-rtl-source: RTL omitted from prompts)[/dim]")

    with console.status(f"[bold]Generating assertions for [cyan]{module}[/cyan]…[/bold]"):
        try:
            pipeline = AssertionPipeline(client)
            bind_result = pipeline.run(ir, rtl_source)
        except Exception as exc:
            console.print(f"[red]Assertion pipeline failed: {exc}[/red]")
            raise typer.Exit(1)

    bind_path = work / f"{module}.bind.sv"
    bind_path.write_text(bind_result.bind_module_sv, encoding="utf-8")
    console.print(f"[green]Bind module written:[/green] {bind_path}")
    _print_assertion_table(bind_result.assertion_index, module)

    # Load dependency file paths for SBY
    deps = _load_dep_paths(work, module, ir)

    # ── Log pending ProvenPair ──────────────────────────────────────────────
    logger = TrainingLogger(cfg.training_log) if cfg.training_enabled else None
    pending_pair: Optional[ProvenPair] = None

    if logger:
        pending_pair = ProvenPair(
            module_name=ir.module,
            module_type=ir.module_type or "unknown",
            file_path=ir.file,
            rtl_source=rtl_source,
            module_ir=ir.model_dump(),
            bind_module_sv=bind_result.bind_module_sv,
            assertion_index=bind_result.assertion_index,
            proof=ProofSummary(status="pending", total=len(bind_result.assertion_index)),
            model_id=bind_result.model_id,
        )
        logger.log(pending_pair)
        console.print(f"[dim]Pending training record logged → {cfg.training_log}[/dim]")

    # ── Skip verification if requested or sby not on PATH ──────────────────
    sby_bin = _find_sby_binary()
    if no_verify or not sby_bin:
        if not no_verify and not sby_bin:
            console.print(
                "[yellow]sby not found — skipping formal verification.[/yellow]\n"
                "[dim]Install oss-cad-suite or add SymbiYosys to PATH.[/dim]"
            )
        console.print()
        console.print(Syntax(bind_result.bind_module_sv, "systemverilog",
                              theme="monokai", line_numbers=True))
        raise typer.Exit(0)

    # ── Formal verification ─────────────────────────────────────────────────
    formal = _make_formal_backend(cfg, sby_bin)

    console.print(
        f"\n[bold]Running formal[/bold] "
        f"(backend={cfg.formal_backend}, mode={cfg.formal_mode}, depth={cfg.formal_depth})…"
    )
    try:
        formal_result = formal.run(
            module_name=module,
            rtl_path=rtl_path,
            bind_path=bind_path,
            deps=deps,
            work_dir=work,
            assertion_index=bind_result.assertion_index,
            mode=cfg.formal_mode,
        )
    except Exception as exc:
        console.print(f"[red]Formal verification crashed: {exc}[/red]")
        raise typer.Exit(1)

    _print_formal_result(formal_result, module)

    # Fix #8: when prove returns UNKNOWN, retry with bmc before entering repair
    effective_mode = cfg.formal_mode
    if formal_result.status == "unknown" and cfg.formal_mode == "prove":
        console.print("[yellow]prove returned UNKNOWN — retrying with bmc mode[/yellow]")
        try:
            formal_result = formal.run(
                module_name=module,
                rtl_path=rtl_path,
                bind_path=bind_path,
                deps=deps,
                work_dir=work,
                assertion_index=bind_result.assertion_index,
                mode="bmc",
            )
            effective_mode = "bmc"
            _print_formal_result(formal_result, module, title="BMC Fallback")
        except Exception as exc:
            console.print(f"[red]BMC fallback crashed: {exc}[/red]")

    repair_iters_done = 0

    # ── Repair loop ─────────────────────────────────────────────────────────
    if formal_result.status != "pass" and cfg.formal_repair_iterations > 0:
        from specloop.loop.repair import RepairLoop, upgrade_to_proven

        repair_loop = RepairLoop(
            client=client,
            formal=formal,
            max_iterations=cfg.formal_repair_iterations,
            mode=effective_mode,
        )

        console.print(
            f"\n[bold yellow]Starting repair loop[/bold yellow] "
            f"(up to {cfg.formal_repair_iterations} iterations)…"
        )
        bind_result, formal_result, repair_steps = repair_loop.run(
            ir=ir,
            rtl_source=rtl_source,
            bind_result=bind_result,
            initial_formal=formal_result,
            work_dir=work,
            deps=deps,
        )
        repair_iters_done = len(repair_steps)

        if logger:
            for step in repair_steps:
                logger.log(step)

        _print_formal_result(formal_result, module, title="After Repair")

    # ── Upgrade pending → proven ────────────────────────────────────────────
    if logger and pending_pair and formal_result.status in ("pass", "fail"):
        from specloop.loop.repair import upgrade_to_proven
        confirmed = upgrade_to_proven(pending_pair, bind_result, formal_result, repair_iters_done)
        if logger.log(confirmed):
            verb = "confirmed" if formal_result.status == "pass" else "recorded (partial)"
            console.print(
                f"[green]Training record {verb}[/green] "
                f"({confirmed.proof.proven}/{confirmed.proof.total} assertions proven, "
                f"{repair_iters_done} repair iterations)"
            )

    console.print()
    console.print(Syntax(bind_result.bind_module_sv, "systemverilog",
                         theme="monokai", line_numbers=True))


# ---------------------------------------------------------------------------
# spec helpers (private)
# ---------------------------------------------------------------------------

def _find_sby_binary() -> str | None:
    """Return the path to sby, checking PATH and local oss-cad-suite first."""
    import shutil

    on_path = shutil.which("sby")
    if on_path:
        return on_path

    # Walk up from CWD looking for oss-cad-suite/bin/sby
    cwd = Path.cwd()
    for base in [cwd, *cwd.parents]:
        candidate = base / "oss-cad-suite" / "bin" / "sby"
        if candidate.exists():
            return str(candidate)

    return None


def _make_formal_backend(cfg, sby_bin: str):
    """Construct the configured FormalBackend (sby or synlig)."""
    if cfg.formal_backend == "synlig":
        from specloop.formal.synlig_backend import SynligBackend
        return SynligBackend(
            synlig_path=cfg.synlig_path,
            sby_path=sby_bin,
            timeout=cfg.formal_timeout,
            depth=cfg.formal_depth,
            solver=cfg.formal_solver,
            debug=cfg.formal_debug,
        )
    from specloop.formal.sby_backend import SBYBackend
    return SBYBackend(
        sby_path=sby_bin,
        timeout=cfg.formal_timeout,
        depth=cfg.formal_depth,
        solver=cfg.formal_solver,
        debug=cfg.formal_debug,
    )


def _verify_toolchain(sby_bin: str) -> bool:
    """Run sby --version to confirm the toolchain is actually functional."""
    import subprocess
    try:
        r = subprocess.run([sby_bin, "--version"], capture_output=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


def _dry_run_prompts(ir, rtl_source: str) -> None:
    """Print all 3-stage prompts without calling the LLM."""
    from jinja2 import DictLoader, Environment, FileSystemLoader
    from specloop.gen.pipeline import _PROMPTS_DIR, _WRAPPER_SUFFIX, _SEP
    from specloop.gen.schema import BehaviorExtraction

    file_env = Environment(
        loader=FileSystemLoader(str(_PROMPTS_DIR)),
        trim_blocks=True, lstrip_blocks=True,
    )
    for stage in ("behavior_extraction.j2", "property_synthesis.j2", "property_hardening.j2"):
        src = file_env.loader.get_source(file_env, stage)[0]
        patched_env = Environment(
            loader=DictLoader({"__tpl__": src + _WRAPPER_SUFFIX}),
            trim_blocks=True, lstrip_blocks=True,
        )
        ctx = dict(ir=ir, rtl_source=rtl_source,
                   behavior=BehaviorExtraction(), candidates=[])
        rendered = patched_env.get_template("__tpl__").render(**ctx)
        parts = rendered.split(_SEP)
        console.print(f"\n[bold cyan]── {stage} system ──[/bold cyan]")
        console.print(parts[1].strip() if len(parts) > 1 else "(empty)")
        console.print(f"\n[bold cyan]── {stage} user ──[/bold cyan]")
        console.print(parts[2].strip() if len(parts) > 2 else "(empty)")


def _print_assertion_table(assertion_index, module: str) -> None:
    if not assertion_index:
        console.print("[yellow]No assertion index returned from model.[/yellow]")
        return
    table = Table(title=f"Assertions: {module}", show_lines=False)
    table.add_column("Name", style="bold")
    table.add_column("Category")
    table.add_column("Rationale")
    cat_colors = {
        "reset": "cyan", "interface": "yellow", "functional": "green",
        "temporal": "magenta", "safety": "red", "fsm": "blue",
    }
    for a in assertion_index:
        c = cat_colors.get(a.category, "white")
        table.add_row(
            a.name,
            f"[{c}]{a.category}[/{c}]",
            a.rationale[:80] + ("…" if len(a.rationale) > 80 else ""),
        )
    console.print(table)


def _print_formal_result(formal_result, module: str, title: str = "Formal Result") -> None:
    from specloop.formal.backend import FormalResult
    status_style = {"pass": "green", "fail": "red", "timeout": "yellow",
                    "compile_error": "red", "unknown": "dim"}
    s = formal_result.status
    sty = status_style.get(s, "white")
    n = formal_result.n_proven
    total = len(formal_result.assertions) or "?"
    console.print(
        f"[bold]{title}[/bold]: [{sty}]{s.upper()}[/{sty}] "
        f"({n}/{total} assertions proved, {formal_result.wall_seconds:.1f}s)"
    )
    if formal_result.failed_assertions:
        console.print("[red]Failing assertions:[/red]")
        for a in formal_result.failed_assertions:
            console.print(f"  [red]✗[/red] {a.name}  {a.message or ''}")
    if formal_result.counterexample_nl:
        console.print("\n[bold]Counterexample:[/bold]")
        console.print(f"[dim]{formal_result.counterexample_nl}[/dim]")
    if s == "compile_error":
        console.print("\n[bold red]Compiler output (last 30 lines):[/bold red]")
        tail = "\n".join(formal_result.log_tail.splitlines()[-30:])
        console.print(f"[dim]{tail}[/dim]")


def _load_dep_paths(work: Path, module: str, ir) -> list[Path]:
    """Load dependency RTL file paths from cached IR files."""
    from specloop.deps.resolver import DependencyResolver
    from specloop.ir.schema import ModuleIR

    irs_raw: dict[str, dict] = {}
    for f in work.glob("*.ir.json"):
        try:
            data = json.loads(f.read_text())
            irs_raw[data["module"]] = data
        except Exception:
            pass

    if not irs_raw:
        return []

    ir_objects = [ModuleIR.model_validate(v) for v in irs_raw.values()]
    resolver = DependencyResolver(ir_objects)

    try:
        closure = resolver.closure(module)
    except KeyError:
        return []

    paths = []
    for dep_module in closure:
        if dep_module == module:
            continue
        raw = irs_raw.get(dep_module)
        if raw:
            p = Path(raw.get("file", ""))
            if p.exists():
                paths.append(p)
    return paths


@app.command("spec-all")
def spec_all(
    work: Path = typer.Option(Path("work"), "--work", "-w"),
    mode: Optional[str] = typer.Option(None, "--mode", help="Formal mode: bmc|prove|cover"),
    no_verify: bool = typer.Option(False, "--no-verify", "-n", help="Skip SBY verification"),
    no_rtl_source: bool = typer.Option(False, "--no-rtl-source", help="Omit raw RTL from prompts"),
):
    """Generate assertions for all ingested modules, with per-module error isolation."""
    ir_files = sorted(work.glob("*.ir.json"))
    if not ir_files:
        console.print("[red]No IR files found. Run 'specloop ingest' first.[/red]")
        raise typer.Exit(1)

    # Fix #12: pre-flight toolchain check before processing any modules
    if not no_verify:
        sby_bin = _find_sby_binary()
        if not sby_bin:
            console.print(
                "[yellow]sby not found — formal verification will be skipped.[/yellow]\n"
                "[dim]Install oss-cad-suite or add SymbiYosys to PATH.[/dim]"
            )
            no_verify = True
        elif not _verify_toolchain(sby_bin):
            console.print(
                "[yellow]sby found but toolchain check failed — "
                "formal verification may not work correctly.[/yellow]"
            )

    modules = []
    for f in ir_files:
        try:
            data = json.loads(f.read_text())
            modules.append(data["module"])
        except Exception:
            pass

    console.print(f"[bold]spec-all:[/bold] {len(modules)} modules in {work}")
    results: dict[str, str] = {}

    # Fix #11: per-module error isolation
    for name in modules:
        console.rule(f"[bold cyan]{name}[/bold cyan]")
        try:
            spec(
                module=name,
                work=work,
                backend=None,
                model=None,
                mode=mode,
                no_verify=no_verify,
                sby_debug=False,
                dry_run=False,
                no_rtl_source=no_rtl_source,
            )
            results[name] = "ok"
        except typer.Exit as e:
            results[name] = "ok" if e.exit_code == 0 else "failed"
        except Exception as exc:
            console.print(f"[red]{name}: unexpected error — {exc}[/red]")
            results[name] = "error"

    # Summary table
    console.rule("[bold]spec-all Summary[/bold]")
    table = Table(show_lines=False)
    table.add_column("Module", style="bold")
    table.add_column("Result")
    ok = sum(1 for v in results.values() if v == "ok")
    for name, result in results.items():
        style = "green" if result == "ok" else "red"
        table.add_row(name, f"[{style}]{result}[/{style}]")
    console.print(table)
    console.print(f"[bold]{ok}/{len(modules)} modules succeeded.[/bold]")


# ---------------------------------------------------------------------------
# Training sub-app
# ---------------------------------------------------------------------------

training_app = typer.Typer(help="Training data management for QLoRA fine-tuning")
app.add_typer(training_app, name="training")


@training_app.command("stats")
def training_stats(
    log: Path = typer.Option(Path("work/training_data.jsonl"), "--log", "-l"),
):
    """Show a summary of collected training data."""
    from specloop.training.logger import TrainingLogger

    logger = TrainingLogger(log)
    if not log.exists():
        console.print("[yellow]No training log found. Proven assertions will be logged automatically during spec generation.[/yellow]")
        raise typer.Exit(0)

    s = logger.stats()

    table = Table(title="Training Data Summary", show_lines=False)
    table.add_column("Metric")
    table.add_column("Value", justify="right")

    table.add_row("Proven pairs", str(s["proven_pairs"]))
    table.add_row("Repair steps", str(s["repair_steps"]))
    table.add_row("  └─ successful repairs", str(s["repair_steps_successful"]))
    table.add_row("Total assertions proven", str(s["total_assertions_proven"]))
    table.add_row("Total assertions", str(s["total_assertions"]))
    table.add_row("Log size", f"{s['log_size_kb']} KB")

    console.print(table)

    if s["module_type_breakdown"]:
        console.print("\n[bold]Module types:[/bold]")
        for mt, count in sorted(s["module_type_breakdown"].items(), key=lambda x: -x[1]):
            console.print(f"  {mt:<20} {count}")

    if s["models"]:
        console.print(f"\n[dim]Models: {', '.join(s['models'])}[/dim]")

    console.print(f"[dim]Log: {s['log_path']}[/dim]")


@training_app.command("export")
def training_export(
    out: Path = typer.Argument(..., help="Output directory for exported JSONL files"),
    fmt: str = typer.Option("both", "--format", "-f", help="flat | chat | both"),
    log: Path = typer.Option(Path("work/training_data.jsonl"), "--log", "-l"),
    min_confidence: float = typer.Option(0.0, "--min-confidence"),
):
    """Export training data as JSONL for QLoRA fine-tuning.

    flat  = Alpaca-style instruction/input/output (works with most frameworks)\n
    chat  = OpenAI messages format (for Axolotl, TRL, LlamaFactory)
    """
    from specloop.training.logger import TrainingLogger

    logger = TrainingLogger(log)
    out.mkdir(parents=True, exist_ok=True)

    written = {}
    if fmt in ("flat", "both"):
        n = logger.export_flat(out / "train_flat.jsonl", min_confidence=min_confidence)
        written["flat"] = n
    if fmt in ("chat", "both"):
        n = logger.export_chat(out / "train_chat.jsonl", min_confidence=min_confidence)
        written["chat"] = n

    for kind, n in written.items():
        console.print(f"[green]{kind}[/green]: {n} records → {out / f'train_{kind}.jsonl'}")


# ---------------------------------------------------------------------------
# Search commands
# ---------------------------------------------------------------------------

@app.command("index")
def index_module(
    module: str = typer.Argument(..., help="Module name to index into Qdrant"),
    log: Path = typer.Option(Path("work/training_data.jsonl"), "--log", "-l"),
):
    """Embed and upsert the best proven pair for a module into Qdrant."""
    from specloop.config import SpecloopConfig
    from specloop.training.logger import TrainingLogger
    from specloop.search.indexer import index_pair

    cfg = SpecloopConfig()
    logger = TrainingLogger(log)

    pairs = [
        p for p in logger.load_proven_pairs()
        if p.module_name == module and p.proof.status != "pending"
    ]
    if not pairs:
        console.print(
            f"[red]No proven pair found for '{module}'. "
            f"Run 'specloop spec {module}' first.[/red]"
        )
        raise typer.Exit(1)

    best = max(pairs, key=lambda p: (p.proof.proven / max(p.proof.total, 1), p.proof.proven))

    with console.status(f"[bold]Indexing [cyan]{module}[/cyan] (loading embedding model…)[/bold]"):
        point_id = index_pair(best, cfg.qdrant_url, cfg.qdrant_collection, cfg.embed_model)

    confidence = best.proof.proven / max(best.proof.total, 1)
    console.print(
        f"[green]Indexed[/green] '{module}' "
        f"({best.proof.proven}/{best.proof.total} assertions proven, "
        f"confidence={confidence:.2f}) → point {point_id}"
    )
    console.print(f"[dim]Qdrant: {cfg.qdrant_url}  collection: {cfg.qdrant_collection}[/dim]")


@app.command("search")
def search_modules(
    query: str = typer.Argument(..., help="Natural-language search query"),
    top_k: int = typer.Option(3, "--top-k", "-k", help="Number of results"),
):
    """Search indexed modules by semantic similarity."""
    from specloop.config import SpecloopConfig
    from specloop.search.searcher import search

    cfg = SpecloopConfig()

    with console.status("[bold]Embedding query and searching…[/bold]"):
        results = search(query, cfg.qdrant_url, cfg.qdrant_collection, cfg.embed_model, top_k=top_k)

    if not results:
        console.print(
            "[yellow]No results. Is Qdrant running? "
            "Run 'specloop index <module>' to populate the index.[/yellow]"
        )
        raise typer.Exit(0)

    table = Table(title=f"Search: {query!r}", show_lines=False)
    table.add_column("Rank", justify="right", style="dim")
    table.add_column("Module", style="bold")
    table.add_column("Type")
    table.add_column("Score", justify="right")
    table.add_column("Assertions", justify="right")
    table.add_column("Confidence", justify="right")

    for rank, r in enumerate(results, 1):
        t_style = _TYPE_STYLE.get(r.module_type, "white")
        table.add_row(
            str(rank),
            r.module_name,
            f"[{t_style}]{r.module_type}[/{t_style}]",
            f"{r.score:.4f}",
            str(r.assertion_count),
            f"{r.confidence:.2f}",
        )
    console.print(table)

    for rank, r in enumerate(results, 1):
        if r.assertion_summary:
            console.print(f"\n[bold cyan]#{rank} {r.module_name}[/bold cyan] — assertions:")
            for line in r.assertion_summary:
                console.print(f"  [dim]{line}[/dim]")


@app.command()
def compose(
    request: str = typer.Argument(..., help="Natural language description of the module to build"),
    work: Path = typer.Option(Path("work"), "--work", "-w", help="Work directory with IR and RTL files"),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Output directory (default: work/compose/)"),
    top_k: int = typer.Option(3, "--top-k", "-k", help="Qdrant candidates per sub-function"),
    min_confidence: float = typer.Option(0.5, "--min-confidence", help="Min proof confidence for a candidate module"),
    min_score: float = typer.Option(0.70, "--min-score", help="Min search score; sub-functions below this are skipped"),
    no_verify: bool = typer.Option(False, "--no-verify", "-n", help="Skip SBY verification"),
    mode: Optional[str] = typer.Option(None, "--mode", help="Formal mode: bmc|prove|cover"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show decomposition plan only, no generation"),
):
    """Compose a formally-verified design from indexed modules using natural language."""
    from specloop.config import SpecloopConfig
    from specloop.gen.client import make_client
    from specloop.compose.decomposer import Decomposer
    from specloop.compose.pipeline import CompositionPipeline, CompositionError

    cfg = SpecloopConfig()
    if mode:
        cfg = cfg.model_copy(update={"formal_mode": mode})

    client = make_client(cfg)
    console.print(f"[dim]LLM: {cfg.llm_backend} / {client.model_id}[/dim]")

    # Step 1: Decompose
    with console.status("[bold]Decomposing request into sub-functions…[/bold]"):
        try:
            plan = Decomposer(client).decompose(request)
        except Exception as exc:
            console.print(f"[red]Decomposition failed: {exc}[/red]")
            raise typer.Exit(1)

    _print_composition_plan(plan)

    if dry_run:
        raise typer.Exit(0)

    # Setup SBY backend
    formal = None
    if not no_verify:
        sby_bin = _find_sby_binary()
        if sby_bin:
            formal = _make_formal_backend(cfg, sby_bin)
        else:
            console.print(
                "[yellow]sby not found — skipping formal verification.[/yellow]\n"
                "[dim]Install oss-cad-suite or add SymbiYosys to PATH.[/dim]"
            )

    out_dir = (out or work / "compose") / plan.composition_name

    pipeline = CompositionPipeline(
        client=client,
        qdrant_url=cfg.qdrant_url,
        collection=cfg.qdrant_collection,
        embed_model=cfg.embed_model,
        top_k=top_k,
        min_confidence=min_confidence,
        min_score=min_score,
    )

    # Steps 2-7: search, compat, wrapper, assertions, SBY
    with console.status("[bold]Composing modules…[/bold]"):
        try:
            result = pipeline.run(
                request=request,
                plan=plan,
                work_dir=work,
                out_dir=out_dir,
                formal=formal,
                formal_mode=cfg.formal_mode,
                formal_repair_iterations=cfg.formal_repair_iterations,
            )
        except CompositionError as exc:
            console.print(f"[red]Composition error:[/red] {exc}")
            raise typer.Exit(1)
        except Exception as exc:
            console.print(f"[red]Unexpected error: {exc}[/red]")
            raise typer.Exit(1)

    # ── Skipped sub-functions ───────────────────────────────────────────────
    for warning in result.skipped_sub_functions:
        console.print(f"[bold yellow]WARNING:[/bold yellow] {warning}")

    # ── Candidate selection table ───────────────────────────────────────────
    table = Table(title="Selected Modules", show_lines=False)
    table.add_column("Sub-function", style="bold")
    table.add_column("Module")
    table.add_column("Type")
    table.add_column("Score", justify="right")
    table.add_column("Confidence", justify="right")
    for sm in result.selected_modules:
        r = sm.search_result
        t_style = _TYPE_STYLE.get(r.module_type, "white")
        table.add_row(
            sm.sub_function_id,
            r.module_name,
            f"[{t_style}]{r.module_type}[/{t_style}]",
            f"{r.score:.4f}",
            f"{r.confidence:.2f}",
        )
    console.print(table)

    # ── Compatibility table ─────────────────────────────────────────────────
    if result.compatibility.issues:
        compat_table = Table(title="Compatibility Checks", show_lines=False)
        compat_table.add_column("Severity")
        compat_table.add_column("Message")
        for issue in result.compatibility.issues:
            sty = "yellow" if issue.severity == "warning" else "red"
            compat_table.add_row(
                f"[{sty}]{issue.severity}[/{sty}]",
                issue.message,
            )
        console.print(compat_table)
    else:
        console.print("[green]Compatibility: all checks passed.[/green]")

    # ── Wrapper SV ─────────────────────────────────────────────────────────
    console.print(f"\n[green]Wrapper written:[/green] {result.wrapper_sv_path}")
    console.print(Syntax(
        result.wrapper_sv_path.read_text(encoding="utf-8"),
        "systemverilog", theme="monokai", line_numbers=True,
    ))

    # ── Assertion table ─────────────────────────────────────────────────────
    if result.bind_result:
        _print_assertion_table(result.bind_result.assertion_index, result.composition_name)

    # ── Formal result ───────────────────────────────────────────────────────
    if result.formal_result:
        _print_formal_result(result.formal_result, result.composition_name, title="Composition Proof")
    else:
        console.print("[dim](Formal verification skipped)[/dim]")

    # ── Summary ─────────────────────────────────────────────────────────────
    console.print()
    console.print(f"[bold green]Composition complete![/bold green]")
    console.print(f"  Wrapper: [cyan]{result.wrapper_sv_path}[/cyan]")
    console.print(f"  Bind:    [cyan]{result.bind_sv_path}[/cyan]")
    console.print(f"  Confidence: {result.confidence:.2f}")


def _print_composition_plan(plan) -> None:
    table = Table(title=f"Composition Plan: {plan.composition_name}", show_lines=False)
    table.add_column("ID", style="dim")
    table.add_column("Sub-function", style="bold")
    table.add_column("Search query")
    table.add_column("Role")
    for sf in plan.sub_functions:
        table.add_row(sf.id, sf.name, sf.search_query, sf.role)
    console.print(table)

    if plan.connections:
        conn_table = Table(title="Declared connections", show_lines=False)
        conn_table.add_column("From")
        conn_table.add_column("Port")
        conn_table.add_column("")
        conn_table.add_column("To")
        conn_table.add_column("Port")
        for c in plan.connections:
            conn_table.add_row(c.from_id, c.from_port, "→", c.to_id, c.to_port)
        console.print(conn_table)


if __name__ == "__main__":
    app()
