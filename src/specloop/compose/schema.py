"""Data models for the composition layer."""
from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict

from specloop.ir.schema import ModuleIR
from specloop.search.searcher import SearchResult
from specloop.formal.backend import FormalResult
from specloop.gen.schema import BindResult


class SubFunction(BaseModel):
    id: str
    name: str
    search_query: str
    role: str


class Connection(BaseModel):
    from_id: str
    from_port: str
    to_id: str
    to_port: str


class CompositionPlan(BaseModel):
    composition_name: str
    sub_functions: list[SubFunction]
    connections: list[Connection] = []


class SelectedModule(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    sub_function_id: str
    search_result: SearchResult
    ir: ModuleIR
    rtl_path: Path


class CompatibilityIssue(BaseModel):
    severity: Literal["error", "warning"]
    message: str


class CompatibilityResult(BaseModel):
    issues: list[CompatibilityIssue] = []

    @property
    def ok(self) -> bool:
        return not any(i.severity == "error" for i in self.issues)

    @property
    def errors(self) -> list[CompatibilityIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[CompatibilityIssue]:
        return [i for i in self.issues if i.severity == "warning"]


class CompositionResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    composition_name: str
    plan: CompositionPlan
    selected_modules: list[SelectedModule]
    skipped_sub_functions: list[str] = []  # human-readable warning per skipped sub-function
    compatibility: CompatibilityResult
    wrapper_sv_path: Path
    bind_sv_path: Path
    bind_result: Optional[BindResult] = None
    formal_result: Optional[FormalResult] = None
    confidence: float = 0.0
