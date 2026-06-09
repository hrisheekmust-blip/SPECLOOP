"""Port compatibility checker for module composition."""
from __future__ import annotations

from specloop.ir.schema import ModuleIR, Port
from specloop.compose.schema import CompatibilityIssue, CompatibilityResult, CompositionPlan

# ---------------------------------------------------------------------------
# AXI-Stream bundle model — lets the checker compare two modules' stream
# interfaces directly (m_axis → s_axis), without a hand-enumerated connection
# list. Core signals must be present and matched on a wired bundle; optional
# signals are ENABLE-gated and may be tied off on one side.
# ---------------------------------------------------------------------------

AXIS_CORE_SIGNALS = ("tvalid", "tready", "tdata", "tlast")
AXIS_OPTIONAL_SIGNALS = ("tkeep", "tstrb", "tid", "tdest", "tuser")
_AXIS_SIGNALS = AXIS_CORE_SIGNALS + AXIS_OPTIONAL_SIGNALS


def _is_axis_port(name: str) -> bool:
    """True if a port name is an AXI-Stream bundle signal (e.g. s_axis_tdata)."""
    low = name.lower()
    return "axis" in low and any(low.endswith(sig) for sig in _AXIS_SIGNALS)


def axis_bundles(ir: ModuleIR) -> dict[str, dict[str, Port]]:
    """Group a module's ports into AXI-Stream bundles, keyed by interface prefix.

    Returns ``{prefix: {signal: Port}}`` — e.g. ``{"s_axis": {"tvalid": Port,
    "tdata": Port, ...}, "m_axis": {...}}``. The prefix is the port name with its
    trailing t-signal removed (``s_axis_tdata`` → ``"s_axis"``), so numbered
    bundles (``s_axis_0_tdata`` → ``"s_axis_0"``) group correctly. Only ports on
    an axis-style interface are grouped; clock/reset/status/config are ignored.
    """
    bundles: dict[str, dict[str, Port]] = {}
    for p in ir.ports:
        low = p.name.lower()
        if "axis" not in low:
            continue
        for sig in _AXIS_SIGNALS:
            if low.endswith(sig):
                prefix = p.name[: len(p.name) - len(sig)].rstrip("_")
                bundles.setdefault(prefix, {})[sig] = p
                break
    return bundles


def _bundle_role(signals: dict[str, Port]) -> str:
    """Classify a bundle as 'master' (drives the stream) or 'slave' (receives).

    Inferred from ``tvalid``'s direction — authoritative regardless of naming
    (``m_axis_``/``s_axis_``/``monitor_axis_``). Falls back to ``tready`` (which
    the slave drives) when ``tvalid`` is absent.
    """
    tv = signals.get("tvalid")
    if tv is not None:
        return "master" if tv.direction == "output" else "slave"
    tr = signals.get("tready")
    if tr is not None:
        return "slave" if tr.direction == "output" else "master"
    return "unknown"


def bundles_by_role(ir: ModuleIR, role: str) -> list[dict[str, Port]]:
    """All of a module's AXI-Stream bundles with the given role ('master'/'slave')."""
    return [sig for sig in axis_bundles(ir).values() if _bundle_role(sig) == role]


def axis_connection_roles(plan: CompositionPlan, sf_id: str) -> set[str]:
    """The AXI-Stream bundle roles a sub-function must expose, inferred from the
    connections it participates in.

    A connection *into* the sub-function via an axis port means it must receive a
    stream → it needs a ``slave`` (s_axis) bundle. A connection *out of* it via an
    axis port means it must drive a stream → it needs a ``master`` (m_axis) bundle.
    Returns ``set()`` for a sub-function whose connections are not AXI-Stream (e.g.
    a req/grant arbiter), so the interface check never demands AXI ports of it.
    """
    roles: set[str] = set()
    for c in plan.connections:
        if c.to_id == sf_id and _is_axis_port(c.to_port):
            roles.add("slave")
        if c.from_id == sf_id and _is_axis_port(c.from_port):
            roles.add("master")
    return roles


def candidate_role_issues(ir: ModuleIR, required_roles: set[str]) -> list[CompatibilityIssue]:
    """Check a candidate module exposes a usable AXI-Stream bundle for each role it
    must fill. Missing bundle, or a bundle lacking core signals, is an error —
    this is what rejects a bare-memory FIFO (no s_axis/m_axis) from an AXIS role.
    """
    issues: list[CompatibilityIssue] = []
    for role in sorted(required_roles):
        candidates = bundles_by_role(ir, role)
        usable = [b for b in candidates if all(s in b for s in AXIS_CORE_SIGNALS)]
        if not usable:
            kind = "slave (s_axis)" if role == "slave" else "master (m_axis)"
            issues.append(CompatibilityIssue(
                severity="error",
                message=(
                    f"{ir.module} exposes no usable AXI-Stream {kind} bundle "
                    f"(needs {', '.join(AXIS_CORE_SIGNALS)})."
                ),
            ))
    return issues


def _check_signal(
    sig: str, up_id: str, mp: Port, down_id: str, sp: Port,
    issues: list[CompatibilityIssue],
) -> None:
    """Width-equality and opposed-direction check for one paired bundle signal.

    Every signal in a master↔slave pair has opposite direction (master drives
    tdata/tvalid/tlast…, slave drives tready), so a single direction-opposition
    rule covers the whole bundle, including the back-pressure tready.
    """
    if mp.width != sp.width:
        issues.append(CompatibilityIssue(
            severity="error",
            message=(
                f"AXIS width mismatch on '{sig}': {up_id}.{mp.name}[{mp.width}] → "
                f"{down_id}.{sp.name}[{sp.width}]."
            ),
        ))
    if mp.direction == sp.direction:
        issues.append(CompatibilityIssue(
            severity="error",
            message=(
                f"AXIS direction conflict on '{sig}': {up_id}.{mp.name} and "
                f"{down_id}.{sp.name} are both '{mp.direction}'."
            ),
        ))


def pair_axis_interfaces(
    up_id: str, up_ir: ModuleIR,
    down_id: str, down_ir: ModuleIR,
) -> list[CompatibilityIssue]:
    """Auto-pair ``up``'s master (m_axis) bundle to ``down``'s slave (s_axis)
    bundle and report incompatibilities — no hand-enumerated connection list.

    - Missing master/slave interface → error (e.g. a bare-memory FIFO has neither).
    - Core signal present on only one side → error.
    - Per shared signal: width equality + opposed direction → error on mismatch.
    - Optional signal present on only one side → warning (ENABLE-flag mismatch).
    - No shared clock between the two modules → warning (clock-domain crossing).

    Returns ``[]`` when the two interfaces are fully compatible.
    """
    issues: list[CompatibilityIssue] = []
    up_masters = bundles_by_role(up_ir, "master")
    down_slaves = bundles_by_role(down_ir, "slave")

    if not up_masters:
        return [CompatibilityIssue(
            severity="error",
            message=(
                f"{up_id} ({up_ir.module}) has no AXI-Stream master (m_axis) "
                f"interface to drive {down_id} ({down_ir.module})."
            ),
        )]
    if not down_slaves:
        return [CompatibilityIssue(
            severity="error",
            message=(
                f"{down_id} ({down_ir.module}) has no AXI-Stream slave (s_axis) "
                f"interface to receive from {up_id} ({up_ir.module})."
            ),
        )]

    # Single-stream pipeline: pair the first master bundle with the first slave
    # bundle. Fan-in/out (multiple bundles) is out of scope for this check.
    up_sig = up_masters[0]
    down_sig = down_slaves[0]

    for sig in AXIS_CORE_SIGNALS:
        mp, sp = up_sig.get(sig), down_sig.get(sig)
        if mp is None and sp is None:
            continue
        if mp is None or sp is None:
            issues.append(CompatibilityIssue(
                severity="error",
                message=(
                    f"AXIS {up_id}.m_axis → {down_id}.s_axis: core signal "
                    f"'{sig}' present on only one side."
                ),
            ))
            continue
        _check_signal(sig, up_id, mp, down_id, sp, issues)

    for sig in AXIS_OPTIONAL_SIGNALS:
        mp, sp = up_sig.get(sig), down_sig.get(sig)
        if (mp is None) != (sp is None):
            issues.append(CompatibilityIssue(
                severity="warning",
                message=(
                    f"AXIS {up_id}.m_axis → {down_id}.s_axis: optional signal "
                    f"'{sig}' present on only one side (ENABLE mismatch — tie off "
                    f"or enable to match)."
                ),
            ))
        elif mp is not None and sp is not None:
            _check_signal(sig, up_id, mp, down_id, sp, issues)

    up_clocks = {p.name for p in up_ir.ports if p.is_clock}
    down_clocks = {p.name for p in down_ir.ports if p.is_clock}
    if up_clocks and down_clocks and up_clocks.isdisjoint(down_clocks):
        issues.append(CompatibilityIssue(
            severity="warning",
            message=(
                f"AXIS {up_id} → {down_id}: no shared clock "
                f"({sorted(up_clocks)} vs {sorted(down_clocks)}) — clock-domain "
                f"crossing; wrapper must route clocks explicitly."
            ),
        ))
    elif up_clocks and not down_clocks:
        issues.append(CompatibilityIssue(
            severity="warning",
            message=(
                f"AXIS {up_id} → {down_id}: {down_id} ({down_ir.module}) exposes "
                f"no clock to share with {up_id}'s {sorted(up_clocks)}."
            ),
        ))
    return issues


class CompatibilityChecker:
    def check(
        self,
        modules: dict[str, ModuleIR],
        plan: CompositionPlan,
    ) -> CompatibilityResult:
        issues: list[CompatibilityIssue] = []

        # 1. Clock name consistency across all modules
        clock_names: dict[str, list[str]] = {}
        for sid, ir in modules.items():
            clocks = [p.name for p in ir.ports if p.is_clock]
            if clocks:
                clock_names[sid] = clocks
        all_clock_names = {c for clocks in clock_names.values() for c in clocks}
        if len(all_clock_names) > 1:
            issues.append(CompatibilityIssue(
                severity="warning",
                message=(
                    f"Clock name mismatch across modules: {dict(clock_names)}. "
                    f"Wrapper must route clocks explicitly."
                ),
            ))

        # 2. Reset polarity consistency
        polarities: dict[str, str] = {}
        for sid, ir in modules.items():
            for p in ir.ports:
                if p.is_reset and p.reset_polarity:
                    polarities[f"{sid}.{p.name}"] = p.reset_polarity
        polarity_values = set(polarities.values())
        if len(polarity_values) > 1:
            issues.append(CompatibilityIssue(
                severity="warning",
                message=(
                    f"Mixed reset polarities ({polarity_values}) across modules. "
                    f"Wrapper must invert reset for mismatched instances."
                ),
            ))

        # 3. Per-connection checks
        for conn in plan.connections:
            from_ir = modules.get(conn.from_id)
            to_ir = modules.get(conn.to_id)

            if from_ir is None or to_ir is None:
                missing = conn.from_id if from_ir is None else conn.to_id
                issues.append(CompatibilityIssue(
                    severity="warning",
                    message=(
                        f"Connection {conn.from_id}.{conn.from_port} → "
                        f"{conn.to_id}.{conn.to_port}: IR not loaded for '{missing}'."
                    ),
                ))
                continue

            from_port = next((p for p in from_ir.ports if p.name == conn.from_port), None)
            to_port = next((p for p in to_ir.ports if p.name == conn.to_port), None)

            if from_port is None:
                issues.append(CompatibilityIssue(
                    severity="error",
                    message=(
                        f"Port '{conn.from_port}' not found on module '{from_ir.module}' "
                        f"(sub-function '{conn.from_id}') — connection "
                        f"{conn.from_id}.{conn.from_port} → {conn.to_id}.{conn.to_port} is invalid."
                    ),
                ))
            if to_port is None:
                issues.append(CompatibilityIssue(
                    severity="error",
                    message=(
                        f"Port '{conn.to_port}' not found on module '{to_ir.module}' "
                        f"(sub-function '{conn.to_id}') — connection "
                        f"{conn.from_id}.{conn.from_port} → {conn.to_id}.{conn.to_port} is invalid."
                    ),
                ))

            if from_port and to_port:
                if from_port.width != to_port.width:
                    issues.append(CompatibilityIssue(
                        severity="error",
                        message=(
                            f"Width mismatch: {conn.from_id}.{conn.from_port}[{from_port.width}] → "
                            f"{conn.to_id}.{conn.to_port}[{to_port.width}]."
                        ),
                    ))
                if from_port.direction == to_port.direction:
                    issues.append(CompatibilityIssue(
                        severity="error",
                        message=(
                            f"Direction conflict: {conn.from_id}.{conn.from_port} and "
                            f"{conn.to_id}.{conn.to_port} both have direction "
                            f"'{from_port.direction}'."
                        ),
                    ))

        return CompatibilityResult(issues=issues)

    def check_interfaces(
        self,
        ordered: list[tuple[str, ModuleIR]],
    ) -> CompatibilityResult:
        """Validate an ordered AXI-Stream pipeline by auto-pairing bundles.

        ``ordered`` is the pipeline head→tail as ``(sub_function_id, ir)`` pairs.
        Each consecutive pair is checked with :func:`pair_axis_interfaces`
        (m_axis → s_axis, widths, directions, ENABLE flags, shared clock), and
        clock-name / reset-polarity consistency is checked across the whole chain.
        Unlike :meth:`check`, this needs no hand-enumerated connection list.
        """
        issues: list[CompatibilityIssue] = []

        clock_names = {p.name for _, ir in ordered for p in ir.ports if p.is_clock}
        if len(clock_names) > 1:
            issues.append(CompatibilityIssue(
                severity="warning",
                message=(
                    f"Clock name mismatch across pipeline: {sorted(clock_names)}. "
                    f"Wrapper must route clocks explicitly."
                ),
            ))
        polarities = {
            p.reset_polarity for _, ir in ordered for p in ir.ports
            if p.is_reset and p.reset_polarity
        }
        if len(polarities) > 1:
            issues.append(CompatibilityIssue(
                severity="warning",
                message=(
                    f"Mixed reset polarities ({sorted(polarities)}) across pipeline. "
                    f"Wrapper must invert reset for mismatched instances."
                ),
            ))

        for (up_id, up_ir), (down_id, down_ir) in zip(ordered, ordered[1:]):
            issues.extend(pair_axis_interfaces(up_id, up_ir, down_id, down_ir))

        return CompatibilityResult(issues=issues)
