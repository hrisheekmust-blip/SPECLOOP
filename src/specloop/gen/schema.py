"""Data models for the 3-stage assertion generation pipeline."""
from __future__ import annotations

from typing import Optional
from pydantic import BaseModel

from specloop.training.schema import AssertionEntry


class BehaviorExtraction(BaseModel):
    """Stage 1 output: structured behavioral description of a module."""
    clock_ports: list[str] = []
    reset_ports: list[str] = []
    reset_synchronous: bool = False
    reset_active_low: bool = False
    state_machines: list[dict] = []       # [{name, states: [str], transitions: str}]
    functional_behaviors: list[str] = []  # plain-English descriptions
    invariants: list[str] = []            # always-true conditions
    interface_protocols: list[str] = []   # e.g. "AXI-S valid/ready handshake"


class PropertyCandidate(BaseModel):
    """One candidate SVA property from Stage 2."""
    name: str
    category: str   # reset | interface | functional | temporal | safety | fsm
    description: str
    sva_sketch: str  # rough SVA text; may need syntactic fixes in Stage 3


class PropertySynthesis(BaseModel):
    """Stage 2 output: a list of property candidates."""
    candidates: list[PropertyCandidate] = []


class BindResult(BaseModel):
    """Stage 3 output: the final bind module ready for SBY."""
    bind_module_sv: str
    assertion_index: list[AssertionEntry] = []
    model_id: str = ""
    stage1: Optional[BehaviorExtraction] = None
    stage2: Optional[PropertySynthesis] = None
