"""Gated per-assertion analysis for one AXIS module. Refuses to proceed unless all
three anti-vacuity gates pass. Categorizes each stored assertion as EXERCISED&PROVEN
/ GUARD-DORMANT (true but switched off at this config) / WRONG (real counterexample)."""
import re, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from harness import (CORP, slave_inputs, split_bind, labels, neuter, _close,
                     _env_block, run, gates, spec_body)

ROOT = Path(__file__).resolve().parents[2]

def neuter_zero(spec, keep):
    """Keep `keep`'s GUARD but force its asserted expr to 1'b0 (others -> 1'b1).
    PASS => the guard never fires at this config (assertion is guard-dormant)."""
    out, i = [], 0
    for m in re.finditer(r"(\w+)\s*:\s*assert\s*\(", spec):
        lab, op = m.group(1), m.end() - 1
        cl = _close(spec, op)
        out.append(spec[i:op + 1])
        out.append("1'b0" if lab == keep else "1'b1")
        out.append(spec[cl]); i = cl + 1
    out.append(spec[i:]); return "".join(out)

def analyze(module, params=None, depth=18, timeout=400):
    params = params or {}
    out = ROOT / "work/recheck_axis" / module
    rtl = (CORP / f"{module}.v").read_text()
    spec, sn = split_bind((ROOT / f"work/{module}.bind.sv").read_text())
    sins = slave_inputs(rtl)
    env = _env_block(sins)
    val = next((x for x in sins if x.endswith("tvalid")), "s_axis_tvalid")
    labs = labels(spec)
    cfg = f"{module} @ params={params or 'default'}"
    print(f"\n{'='*70}\n{cfg}  ({len(labs)} stored assertions; slave inputs: {sins})")

    # ---- GATE 1: assumption satisfiability (assert(1'b0) WITH assumes -> FAIL) ----
    g1 = run(module, env + "  always @(posedge clk) if(!rst) __av: assert(1'b0);",
             params=params, mode="bmc", depth=depth, timeout=timeout, tag="gate_assume_sat", outdir=out)[0]
    # ---- GATE 2: reachability cover (normal accepted slave beat) ----
    _, _, cov = run(module, env + f"  always @(posedge clk) c_beat: cover(!rst && {val} && s_axis_tready);",
                    params=params, mode="cover", depth=depth, timeout=timeout, tag="gate_cover", outdir=out)
    # ---- GATE 3: assertion really checked (corrupt one's expr to false -> FAIL) ----
    g3 = run(module, env, body=spec_body(neuter_zero(spec, labs[0]), module), params=params, mode="bmc", depth=depth, timeout=timeout, tag="gate_assert_chk", outdir=out)[0]
    print(f"  GATES: assume_sat={g1}(want FAIL)  cover_reached={cov}(want True)  assert_chk(corrupt {labs[0]})={g3}")
    ok = (g1 == "FAIL") and cov and (g3 == "FAIL")
    if not ok and cov and g1 == "FAIL" and len(labs) > 1:
        # the first assertion's guard may be dormant; try others until one fires
        for alt in labs[1:]:
            g3b = run(module, env, body=spec_body(neuter_zero(spec, alt), module), params=params, mode="bmc", depth=depth, timeout=timeout, tag="gate_assert_chk2", outdir=out)[0]
            if g3b == "FAIL":
                print(f"  GATE3 satisfied via corrupt {alt} -> FAIL")
                ok = True; break
    if not ok:
        print(f"  !!! GATES FAILED -> REFUSE to record proven for {cfg}")
        return

    # ---- per-assertion categorization (gates passed) ----
    exercised, dormant, wrong, nonind, other = [], [], [], [], []
    for lab in labs:
        pv = run(module, env, body=spec_body(neuter(spec, lab), module), params=params, mode="prove", depth=depth, timeout=timeout, tag=f"pa_{lab}", outdir=out)[0]
        if pv == "PASS":
            lv = run(module, env, body=spec_body(neuter_zero(spec, lab), module), params=params, mode="bmc", depth=depth, timeout=timeout)[0]
            (exercised if lv == "FAIL" else dormant).append(lab)
        else:
            # FAIL or UNKNOWN (k-induction inconclusive) -> classify by real-CEX BMC
            bv = run(module, env, body=spec_body(neuter(spec, lab), module), params=params, mode="bmc", depth=depth, timeout=timeout)[0]
            if bv == "FAIL":
                wrong.append(lab)            # real reachable counterexample
            elif bv == "PASS":
                nonind.append(lab)           # holds in BMC; just not k-inductive at this depth
            else:
                other.append((lab, pv, bv))
    print(f"  RESULT: total={len(labs)}  EXERCISED&PROVEN={len(exercised)}  GUARD-DORMANT={len(dormant)}  "
          f"WRONG={len(wrong)}  non-inductive={len(nonind)}  other={len(other)}")
    if wrong:   print("   WRONG (real CEX):", ", ".join(wrong))
    if nonind:  print("   non-inductive (BMC-ok, not k-inductive):", ", ".join(nonind))
    if other:   print("   other:", other)
    if dormant: print(f"   guard-dormant ({len(dormant)}):", ", ".join(dormant))
    return dict(total=len(labs), exercised=exercised, dormant=dormant, wrong=wrong, nonind=nonind, other=other)

if __name__ == "__main__":
    mod = sys.argv[1]
    params = {}
    for kv in sys.argv[2:]:
        k, v = kv.split("="); params[k] = v
    t0 = time.time()
    analyze(mod, params)
    print(f"  ({time.time()-t0:.0f}s)")
