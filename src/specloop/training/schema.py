"""Training record schemas for QLoRA fine-tuning data collection.

Two record types are captured:

  ProvenPair   — a formally-verified (RTL, assertions) pair. Primary training
                 signal. One record per module per proof run.

  RepairStep   — one iteration of the repair loop: (failed_assertions, CEX,
                 repaired_assertions). Used for DPO / preference tuning later.

The instruction templates here define what the fine-tuned model learns to do,
so they must match the inference prompts exactly.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, computed_field


# ---------------------------------------------------------------------------
# Shared sub-models
# ---------------------------------------------------------------------------

class AssertionEntry(BaseModel):
    name: str
    category: str       # reset | interface | functional | temporal | safety | fsm
    rationale: str = ""


class ProofSummary(BaseModel):
    status: Literal["pending", "all_proven", "partial", "failed", "timeout", "error"]
    proven: int = 0
    total: int = 0
    depth: int = 0
    engine: str = ""
    wall_seconds: float = 0.0


# ---------------------------------------------------------------------------
# System prompt — shared across all record types
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are an expert formal verification engineer writing assertions for SymbiYosys "
    "with the open-source Yosys backend. Yosys does NOT support SVA property syntax. "
    "Use Yosys-compatible immediate assertions inside clocked always blocks ONLY. "
    "Use `assert(<boolean_expr>)` — NOT `assert property(...)`. "
    "No `property`, `sequence`, `default clocking`, `|->`, `|=>`, `##N`, `disable iff`. "
    "No `function`, `task`, or `class` — use `wire`/`assign` for decode logic. "
    "Name every assert label with the `ap_` prefix. "
    "Emit a JSON object with fields: "
    '{"bind_module": "<full SV bind module source>", '
    '"assertion_index": [{"name": "...", "category": "...", "rationale": "..."}]}'
)

REPAIR_SYSTEM_PROMPT = (
    "You are an expert formal verification engineer writing assertions for SymbiYosys "
    "with the open-source Yosys backend. Yosys does NOT support SVA property syntax. "
    "Use `assert(<expr>)` only inside always @(posedge clk) blocks — "
    "never `assert property(...)`, never `property`, never `sequence`. "
    "No `function`, `task`, or `class` — use `wire`/`assign` for decode logic. "
    "Fix only the failing assertions; preserve passing ones verbatim. "
    "Emit the same JSON format: "
    '{"bind_module": "<full SV bind module source>", '
    '"assertion_index": [{"name": "...", "category": "...", "rationale": "..."}]}'
)


# ---------------------------------------------------------------------------
# ProvenPair record
# ---------------------------------------------------------------------------

class ProvenPair(BaseModel):
    """A formally-proven (RTL + assertions) pair — primary fine-tuning signal."""

    record_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    record_type: Literal["proven_pair"] = "proven_pair"
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    # Module identity
    module_name: str
    module_type: str                     # fsm | sequential | memory | combinational | interface
    file_path: str
    rtl_source: str                      # raw SV source of the module
    module_ir: dict[str, Any]            # ModuleIR.model_dump()

    # What the model produced
    bind_module_sv: str                  # the full bind module SV
    assertion_index: list[AssertionEntry] = []

    # Proof outcome
    proof: ProofSummary

    # Generation metadata
    model_id: str = ""                   # e.g. "CodeV-CodeQwen-7B-AWQ"
    repair_iterations: int = 0           # 0 = proven on first attempt

    @computed_field  # type: ignore[misc]
    @property
    def rtl_hash(self) -> str:
        return hashlib.sha256(self.rtl_source.encode()).hexdigest()[:16]

    # ------------------------------------------------------------------
    # Instruction-tuning format builders
    # ------------------------------------------------------------------

    def to_flat(self) -> dict[str, str]:
        """Alpaca-style flat format: instruction / input / output."""
        instruction = (
            f"Generate SVA formal properties for the following SystemVerilog module.\n\n"
            f"Backend profile: open_source_yosys\n"
            f"Module type: {self.module_type}"
        )
        ports = self.module_ir.get("ports", [])
        params = self.module_ir.get("parameters", [])
        port_lines = "\n".join(
            f"  {p['direction']:6s}  {p['name']}"
            + (f"  [width={p['width']}]" if p.get("width", 1) > 1 else "")
            + (" [clock]" if p.get("is_clock") else "")
            + (" [reset/" + p["reset_polarity"] + "]" if p.get("is_reset") else "")
            for p in ports
        )
        param_lines = "\n".join(
            f"  {p['name']} = {p.get('default', '?')}" for p in params
        )
        input_block = (
            f"## Module: {self.module_name}\n"
            + (f"\n### Parameters\n{param_lines}\n" if param_lines else "")
            + f"\n### Ports\n{port_lines}\n"
            + f"\n### RTL Source\n```systemverilog\n{self.rtl_source.strip()}\n```"
        )
        import json
        output_block = json.dumps(
            {
                "bind_module": self.bind_module_sv,
                "assertion_index": [a.model_dump() for a in self.assertion_index],
            },
            ensure_ascii=False,
        )
        return {"instruction": instruction, "input": input_block, "output": output_block}

    def to_chat(self) -> dict[str, list[dict[str, str]]]:
        """OpenAI messages format for chat-style fine-tuning (Axolotl, TRL)."""
        flat = self.to_flat()
        return {
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": flat["instruction"] + "\n\n" + flat["input"]},
                {"role": "assistant", "content": flat["output"]},
            ]
        }


# ---------------------------------------------------------------------------
# RepairStep record
# ---------------------------------------------------------------------------

class RepairStep(BaseModel):
    """One repair-loop iteration — valuable for preference / DPO tuning."""

    record_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    record_type: Literal["repair_step"] = "repair_step"
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    # Links back to the module
    module_name: str
    module_type: str
    file_path: str
    rtl_source: str
    module_ir: dict[str, Any]

    # The bad → good trajectory
    iteration: int                       # which repair round (1-based)
    failed_bind_sv: str                  # assertions that failed
    failure_type: str                    # FormalFail | VacuousProof | CompileError | ...
    cex_nl: str                          # natural-language counterexample description
    repaired_bind_sv: str                # assertions after repair
    repair_succeeded: bool               # did the repaired version pass?

    # Generation metadata
    model_id: str = ""

    @computed_field  # type: ignore[misc]
    @property
    def rtl_hash(self) -> str:
        return hashlib.sha256(self.rtl_source.encode()).hexdigest()[:16]

    def to_flat(self) -> dict[str, str]:
        import json
        instruction = (
            f"The following SVA assertions for module '{self.module_name}' "
            f"(type: {self.module_type}) failed formal verification. "
            f"Fix the failing assertions.\n\n"
            f"Failure type: {self.failure_type}"
        )
        ports = self.module_ir.get("ports", [])
        port_lines = "\n".join(
            f"  {p['direction']:6s}  {p['name']}" for p in ports
        )
        input_block = (
            f"## Module: {self.module_name}\n"
            f"\n### Ports\n{port_lines}\n"
            f"\n### RTL Source\n```systemverilog\n{self.rtl_source.strip()}\n```"
            f"\n### Failed Assertions\n```systemverilog\n{self.failed_bind_sv.strip()}\n```"
            f"\n### Counterexample\n{self.cex_nl}"
        )
        output_block = json.dumps(
            {"bind_module": self.repaired_bind_sv},
            ensure_ascii=False,
        )
        return {"instruction": instruction, "input": input_block, "output": output_block}

    def to_chat(self) -> dict[str, list[dict[str, str]]]:
        flat = self.to_flat()
        return {
            "messages": [
                {"role": "system", "content": REPAIR_SYSTEM_PROMPT},
                {"role": "user", "content": flat["instruction"] + "\n\n" + flat["input"]},
                {"role": "assistant", "content": flat["output"]},
            ]
        }
