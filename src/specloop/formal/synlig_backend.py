"""SynligBackend: SBY orchestration with the Synlig SystemVerilog frontend.

Synlig is a Yosys-equivalent binary with Surelog wired in as a built-in
plugin, parsing the full SystemVerilog 2017 grammar. It is NOT an SBY
replacement — SBY still orchestrates engines, depths, per-task directories,
and VCD generation. This backend invokes `sby` exactly like SBYBackend, but
rewrites PATH so the `yosys` SBY forks resolves to the synlig binary. A
sibling `yosys -> synlig` symlink in the synlig install directory makes
this drop-in.

Compared to SBYBackend, only two things change:
  1. The script template uses `plugin -i systemverilog; read_systemverilog
     -formal ...` instead of `read -sv ...`.
  2. PATH is set so synlig's directory comes first.

All output parsing, stub-retry, preprocessing, and per-assertion result
extraction is reused from sby_backend.
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Literal

from jinja2 import Environment, FileSystemLoader

from specloop.formal.backend import FormalBackend, FormalResult, finalize_verdict
from specloop.formal.sby_backend import (
    _LOG_TAIL_LINES,
    _RC_ERROR,
    _RC_TIMEOUT,
    _build_assertion_results,
    _extract_missing_modules,
    _find_vcd,
    _parse_output,
    _preprocess_rtl,
    _print_raw_output,
    _unique_paths,
    _write_stubs,
)
from specloop.formal.vcd_parser import vcd_to_nl
from specloop.training.schema import AssertionEntry

log = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).parent / "prompts"


class SynligBackend(FormalBackend):
    """SBY-orchestrated formal verification with Synlig as the SV frontend."""

    def __init__(
        self,
        synlig_path: str = "synlig",
        sby_path: str = "sby",
        timeout: int = 300,
        depth: int = 20,
        solver: str = "",
        debug: bool = False,
    ) -> None:
        self._synlig = Path(synlig_path).expanduser().resolve()
        self._sby = sby_path
        self._timeout = timeout
        self._depth = depth
        self._solver = solver
        self._debug = debug
        # Set up the `yosys -> synlig` shim that SBY will find on PATH.
        shim_dir = self._synlig.parent
        yosys_shim = shim_dir / "yosys"
        if not yosys_shim.exists():
            try:
                yosys_shim.symlink_to(self._synlig.name)
            except OSError as exc:
                log.warning("Could not create yosys shim at %s: %s", yosys_shim, exc)

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

        # 1. Preprocess (import-in-header + unpacked array ports). Synlig
        # handles most of these natively, but the preprocessor is cheap and
        # only writes copies when patterns match — safe to keep on.
        raw_rtl_files = _unique_paths([rtl_path] + deps)
        rtl_files = [_preprocess_rtl(p, work_dir) for p in raw_rtl_files]
        bind_abs = bind_path.resolve()

        from specloop.config import SpecloopConfig
        _cfg = SpecloopConfig()
        include_dirs = [d.resolve() for d in _cfg.rtl_include_dirs] if _cfg.rtl_include_dirs else []

        sby_file = work_dir / f"{module_name}.sby"

        def _write_and_run(extra_files: list[Path]) -> tuple[subprocess.CompletedProcess, float, str]:
            sby_content = _render_synlig_config(
                module_name=module_name,
                rtl_files=rtl_files + extra_files,
                bind_path=bind_abs,
                mode=mode,
                depth=self._depth,
                solver=self._solver,
                include_dirs=include_dirs,
            )
            sby_file.write_text(sby_content, encoding="utf-8")
            log.debug("Wrote SBY config (synlig): %s (stubs=%s)", sby_file, [p.name for p in extra_files])
            t0 = time.monotonic()
            p = _run_sby_with_synlig(self._sby, self._synlig, sby_file.name, work_dir, self._timeout)
            wall = time.monotonic() - t0
            return p, wall, (p.stdout or "") + (p.stderr or "")

        proc, wall_seconds, combined = _write_and_run([])

        if proc.returncode == _RC_ERROR:
            missing = _extract_missing_modules(combined)
            if missing:
                log.info(
                    "Compile error references missing modules %s — retrying with stubs",
                    missing,
                )
                stub_files = _write_stubs(missing, work_dir, parent_rtl=rtl_files[0])
                proc2, wall2, combined2 = _write_and_run(stub_files)
                if proc2.returncode != _RC_ERROR:
                    proc, combined = proc2, combined2
                    wall_seconds += wall2
                else:
                    wall_seconds += wall2
                    combined = combined2

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        rc = proc.returncode

        log.debug("SBY (synlig) exit code=%d  wall=%.1fs", rc, wall_seconds)

        if self._debug:
            _print_raw_output(module_name, rc, stdout, stderr)

        task_dir = work_dir / module_name
        status, failed_names, passed_names, vcd_path = _parse_output(combined, rc, task_dir)
        assertions = _build_assertion_results(assertion_index, failed_names, passed_names, status)

        # A proof over zero proven assertions is vacuous and must never count as a
        # PASS@1.00 (see finalize_verdict).
        status, confidence = finalize_verdict(status, assertions)

        cex_nl = ""
        if vcd_path and vcd_path.exists():
            cex_nl = vcd_to_nl(vcd_path, module_name)
        elif status == "fail" and not vcd_path:
            vcd_path = _find_vcd(task_dir)
            if vcd_path:
                cex_nl = vcd_to_nl(vcd_path, module_name)

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

def _render_synlig_config(
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
        trim_blocks=False,
        lstrip_blocks=False,
        keep_trailing_newline=True,
    )
    return env.get_template("synlig_config.j2").render(
        module_name=module_name,
        rtl_files=rtl_files,
        bind_path=bind_path,
        mode=mode,
        depth=depth,
        solver=solver,
        include_dirs=include_dirs or [],
    )


def _run_sby_with_synlig(
    sby_bin: str,
    synlig_path: Path,
    sby_filename: str,
    cwd: Path,
    timeout: int,
) -> subprocess.CompletedProcess:
    """Run sby with PATH set so its `yosys` fork resolves to synlig."""
    cmd = [sby_bin, "--yosys", str(synlig_path), "-f", sby_filename]
    env = os.environ.copy()
    # Prepend synlig's bin directory so the `yosys -> synlig` shim is found
    # first. This is what makes SBY use Synlig's SV frontend.
    env["PATH"] = str(synlig_path.parent) + os.pathsep + env.get("PATH", "")
    log.info("Running: %s  cwd=%s  timeout=%ds  (yosys=synlig)", " ".join(cmd), cwd, timeout)
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
