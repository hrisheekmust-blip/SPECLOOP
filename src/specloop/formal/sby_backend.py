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

        # 1. Preprocess RTL files (rewrite import-in-header + unpacked array ports).
        # Non-destructive: only writes to work_dir/_preprocessed/ when patterns match.
        raw_rtl_files = _unique_paths([rtl_path] + deps)
        rtl_files = [_preprocess_rtl(p, work_dir) for p in raw_rtl_files]
        bind_abs = bind_path.resolve()

        from specloop.config import SpecloopConfig
        _cfg = SpecloopConfig()
        include_dirs = [d.resolve() for d in _cfg.rtl_include_dirs] if _cfg.rtl_include_dirs else []

        sby_file = work_dir / f"{module_name}.sby"

        def _write_and_run(extra_files: list[Path]) -> tuple[subprocess.CompletedProcess, float, str]:
            sby_content = _render_sby_config(
                module_name=module_name,
                rtl_files=rtl_files + extra_files,
                bind_path=bind_abs,
                mode=mode,
                depth=self._depth,
                solver=self._solver,
                include_dirs=include_dirs,
            )
            sby_file.write_text(sby_content, encoding="utf-8")
            log.debug("Wrote SBY config: %s (stubs=%s)", sby_file, [p.name for p in extra_files])
            t0 = time.monotonic()
            p = _run_sby(self._sby, sby_file.name, work_dir, self._timeout)
            wall = time.monotonic() - t0
            return p, wall, (p.stdout or "") + (p.stderr or "")

        # 2. First SBY run
        proc, wall_seconds, combined = _write_and_run([])

        # 2b. Transparent stub retry: if compile_error, parse missing-module
        # names from the log, generate minimal empty stubs, and re-run once so
        # Yosys treats them as blackboxes.
        if proc.returncode == _RC_ERROR:
            missing = _extract_missing_modules(combined)
            if missing:
                log.info(
                    "Compile error references missing modules %s — retrying with stubs",
                    missing,
                )
                stub_files = _write_stubs(missing, work_dir, parent_rtl=rtl_files[0])
                proc2, wall2, combined2 = _write_and_run(stub_files)
                # Use the retry result only if it improved things (didn't get worse).
                if proc2.returncode != _RC_ERROR:
                    proc, combined = proc2, combined2
                    wall_seconds += wall2
                else:
                    # Surface that we tried — keep original (also error) but
                    # accumulate elapsed time so timing reflects real work done.
                    wall_seconds += wall2
                    combined = combined2  # retry log is more informative

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
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

# Yosys/SBY error format:
#   ERROR: Module `\prim_and2' referenced in module `\prim_blanker' in cell ...
_MISSING_MODULE_RE = re.compile(
    r"Module\s+`\\?([A-Za-z_]\w*)'\s+referenced\s+in\s+module"
)


def _extract_missing_modules(log_output: str) -> list[str]:
    """Find unique module names from Yosys 'Module X referenced ...' errors."""
    seen: dict[str, None] = {}
    for name in _MISSING_MODULE_RE.findall(log_output):
        seen.setdefault(name, None)
    return list(seen)


# Pattern: module header that has an import statement BEFORE the parameter list.
#   module <name>\n  import <pkg>::*;\n#(
# Captures the leading `module <name>` line and the offending import line(s).
_HEADER_IMPORT_RE = re.compile(
    r"(?P<head>\bmodule\s+(?P<name>\w+)\b[^\n;]*\n)"
    r"(?P<imports>(?:[ \t]*import\s+[\w:*, ]+;[ \t]*\n)+)"
    r"(?P<rest>[ \t]*#\s*\()",
)

# Pattern: unpacked array port like `input [DW-1:0] data_i [N]`.
# Captures direction, optional `wire/logic`, packed range, name, unpacked range.
_UNPACKED_PORT_RE = re.compile(
    r"\b(?P<dir>input|output|inout)\b"
    r"(?P<mid>\s+(?:wire|logic|reg)?\s*)"
    r"\[(?P<pack>[^\]]+)\]"
    r"\s+(?P<name>\w+)"
    r"\s*\[(?P<unp>[^\]]+)\]"
)


def _preprocess_rtl(src_path: Path, work_dir: Path) -> Path:
    """Rewrite an RTL file in-place into work_dir/_preprocessed if it hits
    Yosys-incompatible patterns: header imports before #( and unpacked array
    ports. Returns the preprocessed path when modified, the original path
    otherwise (zero overhead for clean RTL).
    """
    try:
        src_text = src_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return src_path

    new_text = src_text
    changed = False

    # Fix 1: header import — move imports from between `module X` and `#(`
    # to right after the `;` ending the port list.
    def _move_imports(m: re.Match) -> str:
        nonlocal changed
        changed = True
        # Keep module header + `#(` but drop the imports here.
        # The captured `imports` block is re-inserted after `);` below.
        # We stash the imports via a closure-local list so the second pass
        # can reach them.
        _captured_imports.append(m.group("imports").strip())
        return m.group("head") + m.group("rest")

    _captured_imports: list[str] = []
    new_text = _HEADER_IMPORT_RE.sub(_move_imports, new_text)
    if _captured_imports:
        # Insert each captured import block after the first `);` that ends a
        # port list (matches one per module rewrite). We walk module by
        # module and inject the import block just before the first body
        # statement.
        parts: list[str] = []
        cursor = 0
        for imports in _captured_imports:
            close = new_text.find(");", cursor)
            if close == -1:
                break
            # Insert after the `;` and newline of the port list close
            insert_at = close + len(");")
            # Walk to end-of-line so we put imports on their own line
            nl = new_text.find("\n", insert_at)
            if nl == -1:
                nl = insert_at
            parts.append(new_text[cursor : nl + 1])
            parts.append(f"  {imports}\n")
            cursor = nl + 1
        parts.append(new_text[cursor:])
        new_text = "".join(parts)

    # Fix 2: flatten unpacked array ports.
    # `input [W-1:0] name [N]` → `input [(N)*(W)-1:0] name`
    def _flatten_unpacked(m: re.Match) -> str:
        nonlocal changed
        changed = True
        direction = m.group("dir")
        mid = m.group("mid")
        pack = m.group("pack").strip()
        name = m.group("name")
        unp = m.group("unp").strip()
        # Replace `W-1:0` style with `(N)*W-1:0`. Handle the common
        # `<msb>:0` form by extracting the msb expression and multiplying.
        if ":" in pack:
            msb, lsb = pack.split(":", 1)
            msb = msb.strip()
            lsb = lsb.strip()
            # Width = (msb - lsb + 1) for the inner; outer scaling = N
            # Use ((msb)-(lsb)+1)*N - 1 : 0 as the new packed range
            new_range = f"({unp})*(({msb})-({lsb})+1)-1:0"
        else:
            # Single-bit `[N]` case → just multiply
            new_range = f"({unp})*({pack})-1:0"
        return f"{direction}{mid}[{new_range}] {name}"

    new_text = _UNPACKED_PORT_RE.sub(_flatten_unpacked, new_text)

    if not changed:
        return src_path

    out_dir = work_dir / "_preprocessed"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / src_path.name
    out_path.write_text(new_text, encoding="utf-8")
    log.info("Preprocessed RTL: %s → %s", src_path.name, out_path)
    return out_path


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
        trim_blocks=False,
        lstrip_blocks=False,
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


def _find_matching_paren(s: str, open_idx: int) -> int:
    """Return the index of the close paren matching s[open_idx]='(' or -1."""
    if open_idx >= len(s) or s[open_idx] != "(":
        return -1
    depth = 0
    in_string = False
    i = open_idx
    while i < len(s):
        c = s[i]
        if c == '"':
            bs = 0
            k = i - 1
            while k >= 0 and s[k] == "\\":
                bs += 1
                k -= 1
            if bs % 2 == 0:
                in_string = not in_string
        elif not in_string:
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    return -1


def _extract_signal_widths(parent_text: str) -> dict[str, str]:
    """Map signal_name → packed-range string from port/wire/logic decls.

    Catches the common Yosys-friendly forms:
      input  logic [Width-1:0] in_i;
      wire   [N-1:0]           sig;
      output [3:0]             data;
    """
    widths: dict[str, str] = {}
    pattern = re.compile(
        r"\b(?:input|output|inout|wire|logic|reg)\s+"
        r"(?:wire|logic|reg)?\s*"
        r"\[(?P<range>[^\]]+)\]\s+"
        r"(?P<name>\w+)"
    )
    for m in pattern.finditer(parent_text):
        widths.setdefault(m.group("name"), m.group("range").strip())
    return widths


def _parse_port_mappings(port_text: str) -> list[tuple[str, str]]:
    """Parse `.name(expr), .name(expr), .name` into [(name, expr)].

    Implicit ports (no parens) map the port to a same-named signal.
    """
    out: list[tuple[str, str]] = []
    i = 0
    n = len(port_text)
    while i < n:
        if port_text[i] != ".":
            i += 1
            continue
        j = i + 1
        while j < n and (port_text[j].isalnum() or port_text[j] == "_"):
            j += 1
        name = port_text[i + 1:j]
        if not name:
            i = j + 1
            continue
        # Skip whitespace
        while j < n and port_text[j].isspace():
            j += 1
        if j < n and port_text[j] == "(":
            close = _find_matching_paren(port_text, j)
            if close == -1:
                break
            expr = port_text[j + 1:close].strip()
            out.append((name, expr))
            i = close + 1
        else:
            # Implicit .name → connects to a signal of the same name
            out.append((name, name))
            i = j
    return out


def _extract_instantiation(
    parent_text: str, module_name: str
) -> tuple[list[str], list[tuple[str, str]]] | None:
    """Find first instantiation of `module_name` in parent_text.

    Returns (param_names, [(port_name, connected_expr), ...]) or None.
    """
    pattern = re.compile(rf"\b{re.escape(module_name)}\b\s*(#\s*\(|\w+\s*\()")
    m = pattern.search(parent_text)
    if not m:
        return None

    params: list[str] = []
    cursor = m.end() - len(m.group(1))

    # Optional `#(...)` parameter override list
    if parent_text[cursor] == "#":
        hash_open = parent_text.find("(", cursor)
        if hash_open == -1:
            return None
        param_close = _find_matching_paren(parent_text, hash_open)
        if param_close == -1:
            return None
        param_text = parent_text[hash_open + 1:param_close]
        params = re.findall(r"\.(\w+)\s*\(", param_text)
        cursor = param_close + 1

    # Instance name + port list `inst_name (...)`
    inst_match = re.match(r"\s*\w+\s*\(", parent_text[cursor:])
    if not inst_match:
        return None
    port_open = cursor + inst_match.end() - 1
    port_close = _find_matching_paren(parent_text, port_open)
    if port_close == -1:
        return None
    ports = _parse_port_mappings(parent_text[port_open + 1:port_close])
    return params, ports


def _infer_port_width(expr: str, parent_widths: dict[str, str]) -> str:
    """Best-effort width inference for a port-connection expression.

      bare identifier → look up parent declaration
      {N{...}}        → N-1:0 (replication operator)
    """
    if re.fullmatch(r"\w+", expr):
        return parent_widths.get(expr, "")
    m = re.match(r"\{\s*([^{}]+?)\s*\{", expr)
    if m:
        return f"{m.group(1).strip()}-1:0"
    return ""


def _write_stubs(
    missing: list[str], work_dir: Path, parent_rtl: Path
) -> list[Path]:
    """Write `(* blackbox *)` stub .sv files for each missing module.

    Parses `parent_rtl` to recover the actual instantiation: the parameter
    names from `#(...)` and the port names from `(.name(expr), ...)`. Port
    direction is inferred from the OpenTitan `_i` / `_o` suffix convention
    (default input). Port width is inferred from the connected expression
    when it's a bare signal declared in the parent.

    Falls back to a permissive `(.*)` wildcard stub if the instantiation
    can't be located.
    """
    stub_dir = work_dir / "_stubs"
    stub_dir.mkdir(parents=True, exist_ok=True)

    parent_text = parent_rtl.read_text(encoding="utf-8", errors="replace")
    parent_widths = _extract_signal_widths(parent_text)

    out: list[Path] = []
    for name in missing:
        path = stub_dir / f"{name}.sv"
        spec = _extract_instantiation(parent_text, name)

        if spec is None:
            # No instantiation found — fall back to wildcard.
            path.write_text(
                f"// Auto-generated permissive blackbox stub for {name}.\n"
                f"(* blackbox *)\nmodule {name} (.*);\nendmodule\n",
                encoding="utf-8",
            )
            out.append(path.resolve())
            continue

        params, ports = spec

        # Parameter declarations: we know names but not types/defaults — use
        # `parameter <name> = 1` which Yosys accepts and which the parent's
        # override will replace at elaboration.
        if params:
            param_lines = ",\n  ".join(f"parameter {p} = 1" for p in params)
            param_block = f" #(\n  {param_lines}\n)"
        else:
            param_block = ""

        port_lines: list[str] = []
        for pname, expr in ports:
            direction = "output" if pname.endswith("_o") else "input"
            width = _infer_port_width(expr.strip(), parent_widths)
            width_str = f"[{width}] " if width else ""
            port_lines.append(f"  {direction} logic {width_str}{pname}")
        port_block = ",\n".join(port_lines) if port_lines else ""

        stub = (
            f"// Auto-generated blackbox stub for {name} "
            f"(ports/params recovered from {parent_rtl.name}).\n"
            f"(* blackbox *)\n"
            f"module {name}{param_block}"
            f"{' (' + chr(10) + port_block + chr(10) + ')' if port_block else ''};\n"
            f"endmodule\n"
        )
        path.write_text(stub, encoding="utf-8")
        out.append(path.resolve())
    return out


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
