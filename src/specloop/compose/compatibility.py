"""Port compatibility checker for module composition."""
from __future__ import annotations

from specloop.ir.schema import ModuleIR
from specloop.compose.schema import CompatibilityIssue, CompatibilityResult, CompositionPlan


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
                    severity="warning",
                    message=f"Port '{conn.from_port}' not found in '{conn.from_id}' IR.",
                ))
            if to_port is None:
                issues.append(CompatibilityIssue(
                    severity="warning",
                    message=f"Port '{conn.to_port}' not found in '{conn.to_id}' IR.",
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
