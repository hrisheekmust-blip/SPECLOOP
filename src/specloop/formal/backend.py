"""FormalBackend ABC and FormalResult schema.

Concrete backends: SBYBackend (open-source), JasperGoldBackend (future).
Plug-and-play: change config.formal_backend = "sby" | "jasper".
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel

from specloop.training.schema import AssertionEntry


class AssertionResult(BaseModel):
    name: str
    status: Literal["pass", "fail", "timeout", "vacuous", "unknown"] = "unknown"
    message: str = ""


class FormalResult(BaseModel):
    status: Literal["pass", "fail", "timeout", "compile_error", "unknown"]
    assertions: list[AssertionResult] = []
    counterexample_vcd: Optional[Path] = None
    counterexample_nl: str = ""     # natural-language CEX description
    wall_seconds: float = 0.0
    confidence: float = 0.0         # proven / total assertions
    log_tail: str = ""              # last ~50 lines of tool output

    @property
    def n_proven(self) -> int:
        return sum(1 for a in self.assertions if a.status == "pass")

    @property
    def n_failed(self) -> int:
        return sum(1 for a in self.assertions if a.status == "fail")

    @property
    def failed_assertions(self) -> list[AssertionResult]:
        return [a for a in self.assertions if a.status == "fail"]


def finalize_verdict(
    status: str, assertions: list[AssertionResult]
) -> tuple[str, float]:
    """Compute the reportable (status, confidence) from per-assertion results.

    A proof over zero *proven* assertions is vacuous: SBY/Synlig exit rc=0 on an
    assertion-free design (e.g. truncated/empty generated assertions, or a module
    with no assertions at all), which would otherwise surface as PASS@1.00. Such a
    result is not a verification — force it to not-verified ("unknown") with 0.0
    confidence; never report it as a pass. Results that prove at least one
    assertion are scored exactly as before: (total - failed) / total.
    """
    n_proven = sum(1 for a in assertions if a.status == "pass")
    if n_proven == 0:
        return ("unknown" if status == "pass" else status), 0.0
    n_total = len(assertions)
    n_failed = sum(1 for a in assertions if a.status == "fail")
    return status, max(0.0, (n_total - n_failed) / n_total)


class FormalBackend(ABC):
    """Abstract formal verification runner."""

    @abstractmethod
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
        """Run formal verification; return a structured result."""
