from __future__ import annotations

from typing import Literal, Optional
from pydantic import BaseModel


class Port(BaseModel):
    name: str
    direction: Literal["input", "output", "inout"]
    width: int = 1
    is_clock: bool = False
    is_reset: bool = False
    reset_polarity: Optional[Literal["high", "low"]] = None


class Parameter(BaseModel):
    name: str
    type: str = "int"
    default: Optional[str] = None
    is_local: bool = False  # localparam — cannot be overridden in instantiations


class SubmoduleInstance(BaseModel):
    instance_name: str
    module_name: str
    params: dict[str, str] = {}


class AlwaysBlock(BaseModel):
    kind: Literal["always_ff", "always_comb", "always_latch", "always"]
    sensitivity: list[str] = []
    has_async_reset: bool = False
    start_line: int = 0          # source line where this block begins (1-based)
    end_line: int = 0            # source line where this block ends (inclusive)
    signals_written: list[str] = []  # signal names on LHS of assignments
    signals_read: list[str] = []     # signal names on RHS / in conditions


class ModuleIR(BaseModel):
    schema_version: str = "specloop.ir/v1"
    module: str
    file: str
    lines: tuple[int, int] = (0, 0)
    parameters: list[Parameter] = []
    ports: list[Port] = []
    always_blocks: list[AlwaysBlock] = []
    submodules: list[SubmoduleInstance] = []
    imports: list[str] = []
    module_type: Optional[str] = None
    parse_status: Literal["ok", "partial", "failed"] = "ok"
    confidence: float = 1.0
