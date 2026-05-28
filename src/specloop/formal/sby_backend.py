"""SBYBackend: SymbiYosys formal verification runner.

Generates a .sby config from a Jinja2 template, executes `sby` as a subprocess,
parses stdout/stderr for per-assertion results, finds counterexample VCDs, and
converts them to natural language via vcd_parser.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Literal

from jinja2 import Environment, FileSystemLoader

from specloop.formal.backend import AssertionResult, FormalBackend, FormalResult
from specloop.formal.vcd_parser import vcd_to_nl
from specloop.training.schema import AssertionEntry

log = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).parent / "prompts"

# SBY exit codes (from sby source: sby line ~1108)
_RC_PASS = 0
_RC_FAIL = 2
_RC_UNKNOWN = 4
_RC_TIMEOUT = 8
_RC_ERROR = 16

_LOG_TAIL_LINES = 60


class SBYBackend(FormalBackend):
    """Concrete SBY backend."""

    def __init__(
        self,
        sby_path: str = "sby",
        timeout: int = 300,
        depth: int = 20,
        solver: str = "",
        debug: bool = False,
    ) -> None:
        self._sby = sby_path
        self._timeout = timeout
        self._depth = depth
        self._solver = solver
        self._debug = debug

    def run(
        self,
        module_name: str,
        rtl_path: Path,
        bind_path: Path,
        deps: list[Path],
        work_dir: Path,
        assertion_index: list[AssertionEntry] | None = None,
        mode: Literal["bmc", "prove", "cover"] = "prove",
    ) -> FormalResult:
        assertion_index = assertion_index or []

        # 1. Write .sby config
        # RTL files are read without -formal so `ifdef FORMAL blocks are skipped,
        # preventing pre-existing RTL asserts from becoming untracked formal properties.
        rtl_files = _unique_paths([rtl_path] + deps)
        bind_abs = bind_path.resolve()

        from specloop.config import SpecloopConfig
        _cfg = SpecloopConfig()
        include_dirs = [d.resolve() for d in _cfg.rtl_include_dirs] if _cfg.rtl_include_dirs else []

        sby_content = _render_sby_config(
            module_name=module_name,
            rtl_files=rtl_files,
            bind_path=bind_abs,
            mode=mode,
            depth=self._depth,
            solver=self._solver,
            include_dirs=include_dirs,
        )
        sby_file = work_dir / f"{module_name}.sby"
        sby_file.write_text(sby_content, encoding="utf-8")
        log.debug("Wrote SBY config: %s", sby_file)

        # 2. Run SBY
        t0 = time.monotonic()
        proc = _run_sby(self._sby, sby_file.name, work_dir, self._timeout)
        wall_seconds = time.monotonic() - t0

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        combined = stdout + stderr
        rc = proc.returncode

        log.debug("SBY exit code=%d  wall=%.1fs", rc, wall_seconds)

        if self._debug:
            _print_raw_output(module_name, rc, stdout, stderr)

        # 3. Parse output
        task_dir = work_dir / module_name
        status, failed_names, passed_names, vcd_path = _parse_output(combined, rc, task_dir)

        # 4. Build assertion result list
        assertions = _build_assertion_results(assertion_index, failed_names, passed_names, status)

        # 5. Confidence = proven / total
        n_total = len(assertions) if assertions else max(len(assertion_index), 1)
        n_failed = sum(1 for a in assertions if a.status == "fail")
        confidence = max(0.0, (n_total - n_failed) / n_total)

        # 6. Parse VCD → natural language
        cex_nl = ""
        if vcd_path and vcd_path.exists():
            cex_nl = vcd_to_nl(vcd_path, module_name)
        elif status == "fail" and not vcd_path:
            vcd_path = _find_vcd(task_dir)
            if vcd_path:
                cex_nl = vcd_to_nl(vcd_path, module_name)

        # 7. Log tail (last N lines of combined output)
        log_tail = "\n".join(combined.splitlines()[-_LOG_TAIL_LINES:])

        return FormalResult(
            status=status,
            assertions=assertions,
            counterexample_vcd=vcd_path,
            counterexample_nl=cex_nl,
            wall_seconds=wall_seconds,
            confidence=confidence,
            log_tail=log_tail,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _unique_paths(paths: list[Path]) -> list[Path]:
    """Deduplicate paths, preserving order, resolving to absolute."""
    seen: set[Path] = set()
    result = []
    for p in paths:
        abs_p = p.resolve()
        if abs_p not in seen:
            seen.add(abs_p)
            result.append(abs_p)
    return result


def _render_sby_config(
    module_name: str,
    rtl_files: list[Path],
    bind_path: Path,
    mode: str,
    depth: int,
    solver: str,
    include_dirs: list[Path] | None = None,
) -> str:
    env = Environment(
        loader=FileSystemLoader(str(_PROMPTS_DIR)),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    return env.get_template("sby_config.j2").render(
        module_name=module_name,
        rtl_files=rtl_files,
        bind_path=bind_path,
        mode=mode,
        depth=depth,
        solver=solver,
        include_dirs=include_dirs or [],
    )


def _run_sby(
    sby_bin: str,
    sby_filename: str,
    cwd: Path,
    timeout: int,
) -> subprocess.CompletedProcess:
    cmd = [sby_bin, "-f", sby_filename]

    # If sby_bin is an absolute path (oss-cad-suite), prepend its bin dir to
    # PATH so that the Yosys it forks is the oss-cad-suite Yosys, not the
    # system one at /usr/bin/yosys which lacks SVA/slang support.
    env = os.environ.copy()
    sby_path_obj = Path(sby_bin)
    if sby_path_obj.is_absolute():
        env["PATH"] = str(sby_path_obj.parent) + os.pathsep + env.get("PATH", "")

    log.info("Running: %s  cwd=%s  timeout=%ds", " ".join(cmd), cwd, timeout)
    try:
        return subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        # Return a synthetic result for timeout
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=_RC_TIMEOUT,
            stdout=exc.stdout or "",
            stderr=exc.stderr or f"Timeout after {timeout}s",
        )
    except FileNotFoundError:
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=_RC_ERROR,
            stdout="",
            stderr=f"'{sby_bin}' not found on PATH",
        )


def _parse_output(
    output: str, returncode: int, task_dir: Path
) -> tuple[str, list[str], list[str], Path | None]:
    """Parse combined SBY output.

    Returns: (overall_status, failed_names, passed_names, vcd_path_or_None)

    Status classification rules (in priority order):
      1. rc=0  → pass (exit code is authoritative for PASS)
      2. DONE line: "SBY HH:MM:SS [task] DONE (PASS, rc=0)"
      3. rc=2→fail, rc=4→unknown, rc=8→timeout, rc=16→compile_error
    """
    lines = output.splitlines()

    # ── Step 1: set status from exit code ────────────────────────────────
    _rc_status = {
        _RC_PASS: "pass",
        _RC_FAIL: "fail",
        _RC_UNKNOWN: "unknown",
        _RC_TIMEOUT: "timeout",
        _RC_ERROR: "compile_error",
    }
    status = _rc_status.get(returncode, "unknown")

    # ── Step 2: refine with the DONE summary line ─────────────────────────
    # SBY final status: "SBY HH:MM:SS [task] DONE (PASS, rc=0)"
    # or "DONE (FAIL, rc=2)", "DONE (TIMEOUT, rc=8)", etc.
    # Scan in reverse so the very last DONE line wins.
    _done_map = {
        "PASS": "pass", "FAIL": "fail",
        "TIMEOUT": "timeout", "UNKNOWN": "unknown", "ERROR": "compile_error",
    }
    for line in reversed(lines):
        m = re.search(r'DONE \((\w+),', line)
        if m:
            status = _done_map.get(m.group(1).upper(), status)
            break

    # ── Step 3: rc=0 sanity guard — exit code is authoritative for PASS ──
    if returncode == _RC_PASS and status != "pass":
        log.warning(
            "rc=0 (PASS) but text parser produced status=%r — "
            "trusting exit code. Run with debug=True to inspect raw output.",
            status,
        )
        status = "pass"

    # ── Per-assertion results from summary lines ──────────────────────────
    # "SBY HH:MM:SS [task] summary: PASS: assert ap_reset [prove_induct]"
    # "SBY HH:MM:SS [task] summary: FAIL: assert ap_value [prove_induct]"
    failed: list[str] = []
    passed: list[str] = []

    for line in lines:
        m = re.search(r'summary:\s+(PASS|FAIL):\s+(?:assert|cover|assume)\s+(\S+)', line)
        if m:
            verdict, name = m.group(1), m.group(2)
            # Strip trailing brackets like "[prove_induct]"
            name = re.sub(r'\[.*?\]', '', name).strip()
            if verdict == "FAIL":
                if name not in failed:
                    failed.append(name)
            else:
                if name not in passed:
                    passed.append(name)

        # Fallback: engine-level assert failure line
        # "engine_0: ## 2 0:00:00 Assert failed in counter.spec_inst: ap_reset"
        m = re.search(r'Assert failed in \S+:\s+(\S+)', line)
        if m:
            name = m.group(1).strip()
            if name not in failed:
                failed.append(name)

    # ── VCD path ─────────────────────────────────────────────────────────
    vcd_path: Path | None = None
    for line in lines:
        m = re.search(r'Writing trace to VCD file:\s+(\S+)', line)
        if m:
            raw_path = m.group(1).strip()
            candidate = task_dir / raw_path
            vcd_path = candidate if candidate.exists() else task_dir / Path(raw_path).name

    return status, failed, passed, vcd_path


def _build_assertion_results(
    index: list[AssertionEntry],
    failed_names: list[str],
    passed_names: list[str],
    overall: str,
) -> list[AssertionResult]:
    """Build a per-assertion result list from the known index and SBY summary names."""
    if overall == "compile_error":
        return [
            AssertionResult(name=e.name, status="unknown",
                            message="compile error — assertion not run")
            for e in index
        ]

    if overall == "timeout":
        return [
            AssertionResult(name=e.name, status="timeout") for e in index
        ]

    failed_set = set(failed_names)
    passed_set = set(passed_names)

    def _matches(entry_name: str, sby_name: str) -> bool:
        return (entry_name == sby_name
                or sby_name.endswith("." + entry_name)
                or sby_name.endswith(":" + entry_name))

    results: list[AssertionResult] = []
    for entry in index:
        is_failed = any(_matches(entry.name, fn) for fn in failed_set)
        is_passed = any(_matches(entry.name, pn) for pn in passed_set)

        if is_failed:
            verdict = "fail"
        elif is_passed or overall == "pass":
            # Explicit pass from summary, or overall PASS means all proved
            verdict = "pass"
        else:
            verdict = "unknown"

        results.append(AssertionResult(name=entry.name, status=verdict))

    # Append any failed names that didn't match anything in the index
    indexed_names = {e.name for e in index}
    for fn in failed_names:
        if not any(_matches(n, fn) for n in indexed_names):
            results.append(AssertionResult(
                name=fn, status="fail",
                message="not in assertion index",
            ))

    return results


def _print_raw_output(module_name: str, rc: int, stdout: str, stderr: str) -> None:
    """Print raw SBY output for debugging."""
    sep = "─" * 60
    print(f"\n{sep}")
    print(f"[SBY DEBUG] module={module_name}  rc={rc}")
    print(f"{sep}")
    if stdout.strip():
        print("── stdout ──")
        print(stdout)
    if stderr.strip():
        print("── stderr ──")
        print(stderr)
    print(sep)


def _find_vcd(task_dir: Path) -> Path | None:
    """Search the SBY task directory for a counterexample VCD."""
    if not task_dir.exists():
        return None
    matches = sorted(task_dir.rglob("*.vcd"))
    return matches[0] if matches else None
