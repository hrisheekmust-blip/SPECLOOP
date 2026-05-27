"""Convert a SBY counterexample VCD trace to natural language.

The output is designed to go into the repair prompt so the LLM understands
exactly which signals had unexpected values and when the assertion fired.
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

_MAX_STEPS = 30
_MAX_SIGNALS = 24  # cap before output becomes unreadable


def vcd_to_nl(vcd_path: Path, module_name: str) -> str:
    """Return a human-readable description of the counterexample trace."""
    try:
        from vcdvcd import VCDVCD
    except ImportError:
        return "(vcdvcd not installed — install with: pip install vcdvcd)"

    if not vcd_path.exists():
        return "(no VCD file at expected path)"

    try:
        vcd = VCDVCD(str(vcd_path))
    except Exception as exc:
        log.warning("Could not parse VCD %s: %s", vcd_path, exc)
        return f"(could not parse VCD: {exc})"

    all_sig_names: list[str] = list(vcd.references_to_ids.keys())
    if not all_sig_names:
        return "(VCD contained no signals)"

    # Prefer the DUT's top-level interface signals: exactly one dot (module.port)
    dut_sigs = [s for s in all_sig_names if s.count(".") == 1 and s.startswith(module_name + ".")]
    if not dut_sigs:
        # Fall back to all signals, trimmed to cap
        dut_sigs = all_sig_names

    # Sort by name, cap count
    dut_sigs = sorted(dut_sigs)[:_MAX_SIGNALS]

    # Collect all timestamps across selected signals
    timestamps: set[int] = set()
    for sname in dut_sigs:
        try:
            for t, _ in vcd[sname].tv:
                timestamps.add(int(t))
        except Exception:
            pass

    if not timestamps:
        return "(no signal transitions found in VCD)"

    sorted_ts = sorted(timestamps)

    def val_at(sname: str, t: int) -> str:
        try:
            val = None
            for st, sv in vcd[sname].tv:
                if int(st) <= t:
                    val = sv
                else:
                    break
            return str(val) if val is not None else "x"
        except Exception:
            return "?"

    lines: list[str] = [
        f"Counterexample trace for '{module_name}' "
        f"({len(sorted_ts)} time steps, {len(dut_sigs)} signals shown):"
    ]

    prev: dict[str, str] = {}
    shown = 0

    for step_idx, t in enumerate(sorted_ts):
        current = {sname: val_at(sname, t) for sname in dut_sigs}
        changed = {sname: v for sname, v in current.items() if prev.get(sname) != v}

        if step_idx == 0 or changed:
            short = {s.split(".")[-1]: v for s, v in current.items()}
            if step_idx == 0:
                sig_str = "  ".join(f"{k}={v}" for k, v in sorted(short.items()))
                lines.append(f"  step {step_idx} (t={t}): {sig_str}  [initial state]")
            else:
                changed_short = {s.split(".")[-1]: v for s, v in changed.items()}
                sig_str = "  ".join(f"*{k}={v}" for k, v in sorted(changed_short.items()))
                lines.append(f"  step {step_idx} (t={t}): {sig_str}")
            shown += 1

        prev = current

        if shown >= _MAX_STEPS:
            remaining = len(sorted_ts) - step_idx - 1
            if remaining > 0:
                lines.append(f"  ... ({remaining} more steps not shown)")
            break

    # Annotate the last step as the failure point
    lines.append(
        "\n  [The assertion violation occurred at or just before the last shown step.]"
    )

    return "\n".join(lines)
