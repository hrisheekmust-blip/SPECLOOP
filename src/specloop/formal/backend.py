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
