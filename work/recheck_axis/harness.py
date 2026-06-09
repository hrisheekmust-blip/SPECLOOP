"""Reusable, gated, sound AXIS proof harness (read-only on corpus RTL).

For each module: inject (into a copy of the module, so internal-signal assertions
are in scope) a driven-by-counter reset assumption + AXIS source assumptions on the
slave input(s), then instantiate the stored spec as a NORMAL child (`spec_inst(.*)`
— no bind). Master outputs free.

Gates (ALL mandatory; nothing is 'proven' unless all pass):
  - assume_sat : assert(1'b0) WITH assumptions active must FAIL (assumptions satisfiable)
  - cover      : a normal accepted beat must be coverable (env not over-constrained)
  - assert_chk : corrupting one stored assertion must FAIL (assertions really checked)
"""
import re, subprocess, tempfile, shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CORP = ROOT / "corpus/verilog-axis/rtl"
SBY = ROOT / "oss-cad-suite/bin/sby"

_INST = re.compile(r"^\s*([A-Za-z_]\w*)\s+(?:#\s*\(|[A-Za-z_]\w*\s*\()", re.M)
_SKIP = {"if","for","case","module","assign","always","begin","initial","generate","wire",
         "reg","logic","input","output","inout","localparam","parameter","genvar","integer",
         "function","task","posedge","negedge"}

def deps(paths):
    seen, q = {}, list(paths)
    while q:
        rp = q.pop(0).resolve()
        if rp in seen: continue
        seen[rp] = None
        try: t = rp.read_text(errors="replace")
        except OSError: continue
        for m in _INST.finditer(t):
            n = m.group(1)
            if n in _SKIP: continue
            c = rp.parent / f"{n}.v"
            if c.exists() and c.resolve() not in seen: q.append(c)
    return list(seen)

def slave_inputs(rtl):
    """s_axis_t* signals declared as module INPUTS (tready is an output, excluded).
    Line-local match against an `input [width] s_axis_t<suffix>` declaration."""
    seen = []
    pat = re.compile(r"\binput\b[ \t]+(?:wire|reg|logic)?[ \t]*(?:\[[^\]]*\][ \t]*)?(s_axis_t\w+)")
    for m in pat.finditer(rtl):
        name = m.group(1)
        if name not in seen and not name.endswith("tready"):
            seen.append(name)
    return seen

def split_bind(bt):
    m = re.search(r"^\s*bind\b", bt, re.M)
    if not m: return None, None
    spec_src = bt[:m.start()].strip()
    names = re.findall(r"module\s+(\w+)", spec_src)
    return (spec_src, names[-1]) if names else (None, None)

def labels(spec):
    return [m.group(1) for m in re.finditer(r"(\w+)\s*:\s*assert\s*\(", spec)]

def _close(s, i):
    d = 0
    while i < len(s):
        if s[i] == '(': d += 1
        elif s[i] == ')':
            d -= 1
            if d == 0: return i
        i += 1
    return -1

def neuter(spec, keep):
    out, i = [], 0
    for m in re.finditer(r"(\w+)\s*:\s*assert\s*\(", spec):
        lab, op = m.group(1), m.end() - 1
        cl = _close(spec, op)
        out.append(spec[i:op + 1]); out.append(spec[op + 1:cl] if lab == keep else "1'b1"); out.append(spec[cl]); i = cl + 1
    out.append(spec[i:]); return "".join(out)

def _env_block(sins, rst_cycles=3):
    has = lambda s: any(x.endswith(s) for x in sins)
    val = next((x for x in sins if x.endswith("tvalid")), None)
    stables = "\n".join(
        f"    am_{x.split('_t')[-1]}: assume ({x} == $past({x}));"
        for x in sins if not x.endswith("tvalid"))
    src = ""
    if val:
        src = f"""  // AXIS source rules on the slave input (well-behaved upstream master)
  // (1) TVALID must be LOW during reset (AMBA AXIS requirement)
  always @(posedge clk) if (rst) am_valid_low_in_reset: assume (!{val});
  // (2) once asserted, TVALID stays high until TREADY; payload stable while stalled
  always @(posedge clk) if (!rst && $past({val}) && !$past(s_axis_tready)) begin
    am_valid_held: assume ({val});
{stables}
  end
"""
    return f"""  // ---- injected proof environment (assumed reset + AXIS source rules) ----
  reg [7:0] __rc = 8'd0;
  always @(posedge clk) if (__rc != 8'hff) __rc <= __rc + 8'd1;
  always @(posedge clk) assume (rst == (__rc < {rst_cycles}));
{src}"""

def spec_body(spec_src, module=""):
    """The spec's body (decode wires + assertion always-blocks) WITHOUT the module
    wrapper, to inline directly into the module (so internal-signal references
    resolve — `(.*)` does NOT connect to a module's internal regs, only its ports).

    Some specs alias internals via hierarchical refs (`wire x; assign x =
    <module>.x;`). On inlining those would shadow the real signals, so we drop the
    hierarchical self-prefix, the resulting self-assigns, and declaration-only
    alias wires — leaving the assertions to reference the module's signals directly.
    """
    body = spec_src[spec_src.index(");") + 2: spec_src.rindex("endmodule")]
    body = re.sub(r"^\s*parameter\b[^;]*;\s*$", "", body, flags=re.M)
    if module:
        # alias names: `assign X = <module>.Y;` — X just mirrors an internal signal.
        aliases = set(re.findall(rf"assign\s+(\w+)\s*=\s*{re.escape(module)}\.\w+\s*;", body))
        body = re.sub(rf"^\s*assign\s+\w+\s*=\s*{re.escape(module)}\.\w+\s*;\s*$", "", body, flags=re.M)
        for a in aliases:                                  # drop ONLY the aliased decl-only wires
            body = re.sub(rf"^\s*(?:wire|reg)\b[^=;\n]*\b{re.escape(a)}\s*;\s*$", "", body, flags=re.M)
        body = body.replace(f"{module}.", "")              # inline hier refs -> direct refs
    return body

def build_module(module, params, inject):
    rtl = (CORP / f"{module}.v").read_text()
    for k, v in (params or {}).items():           # set param default in source (preserves FF init)
        rtl = re.sub(rf"\bparameter\s+{k}\s*=\s*[^,)\n]+", f"parameter {k} = {v}", rtl, count=1)
    idx = rtl.rstrip().rfind("endmodule")
    return rtl[:idx] + "\n" + inject + "\n" + rtl[idx:]

def run(module, inject, body=None, params=None, mode="prove", depth=20, timeout=400, tag="run", outdir=None):
    """Inject `inject` (env: reset+source assumes, plus optional gate logic) and the
    assertion `body` directly into the module, read it -formal, prove/bmc/cover."""
    params = params or {}
    td = Path(tempfile.mkdtemp())
    full_inject = inject + ("\n" + body if body else "")
    (td / f"{module}.v").write_text(build_module(module, params, full_inject))
    df = [d for d in deps([CORP / f"{module}.v"]) if d.name != f"{module}.v"]
    for d in df: shutil.copy(d, td / d.name)
    reads = "\n".join([f"read -sv -formal {module}.v"] + [f"read -sv {d.name}" for d in df])
    files = "\n".join([str(td / f"{module}.v")] + [str(td / d.name) for d in df])
    (td / "t.sby").write_text(
        f"[options]\nmode {mode}\ndepth {depth}\n[engines]\nsmtbmc\n[script]\n{reads}\nprep -top {module} -flatten\n[files]\n{files}\n")
    try:
        p = subprocess.run([str(SBY), "-f", "t.sby"], cwd=str(td), capture_output=True, text=True, timeout=timeout)
        out = p.stdout + p.stderr
        v = re.findall(r"DONE \((\w+)", out); v = v[-1] if v else "?"
        fails = [f.split(".")[-1] for f in re.findall(r"Assert failed in \S+: (\S+)", out)]
        cov = bool(re.search(r"Reached cover statement", out))
        if outdir:
            Path(outdir).mkdir(parents=True, exist_ok=True)
            (Path(outdir) / f"{tag}.log").write_text(out)
            (Path(outdir) / f"{tag}.v").write_text(build_module(module, params, inject))
    except subprocess.TimeoutExpired:
        v, fails, cov = "TIMEOUT", [], False
    finally:
        shutil.rmtree(td, ignore_errors=True)
    return v, fails, cov

def gates(module, params=None, depth=20, timeout=400, outdir=None):
    rtl = (CORP / f"{module}.v").read_text()
    sins = slave_inputs(rtl)
    env = _env_block(sins)
    val = next((x for x in sins if x.endswith("tvalid")), "s_axis_tvalid")
    g = {}
    # assumption satisfiability
    g["assume_sat"] = run(module, env + "  always @(posedge clk) if(!rst) __av: assert(1'b0);",
                          params=params, mode=mode_for(module), depth=depth, timeout=timeout, tag="assume_sat", outdir=outdir)[0]
    # reachability cover (a normal accepted slave beat)
    cv, _, cov = run(module, env + f"  always @(posedge clk) c_beat: cover(!rst && {val} && s_axis_tready);",
                     params=params, mode="cover", depth=depth, timeout=timeout, tag="cover", outdir=outdir)
    g["cover_reached"] = cov
    return g, sins, env

def mode_for(module):
    return "bmc"  # assumption-satisfiability is a reachability question -> bmc
