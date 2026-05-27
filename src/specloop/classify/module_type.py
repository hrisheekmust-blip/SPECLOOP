"""Heuristic module-type classifier operating purely on ModuleIR."""
from __future__ import annotations

import re
from specloop.ir.schema import ModuleIR

# Protocol signal lexicons for interface detection
_AXI_SIGNALS = re.compile(r"(awvalid|awready|wvalid|wready|bvalid|bready|arvalid|arready|rvalid|rready)", re.I)
_AHB_SIGNALS = re.compile(r"(hsel|haddr|htrans|hwrite|hwdata|hrdata|hready)", re.I)
_APB_SIGNALS = re.compile(r"(psel|penable|pwrite|pwdata|prdata|pready)", re.I)
_HANDSHAKE_SIGNALS = re.compile(r"(valid|ready|req|ack|grant)", re.I)

_FSM_STATE_NAMES = re.compile(r"^(state|fsm_state|cur_state|next_state|.*_state|.*_fsm)$", re.I)
_MEMORY_SIGNALS = re.compile(r"\b(wr_en|we|write_en|rd_en|re|read_en|mem_en)\b", re.I)


def _has_always_ff(ir: ModuleIR) -> bool:
    for b in ir.always_blocks:
        if b.kind == "always_ff":
            return True
        # Verilog-style always @(posedge clk) — pyslang emits kind="always"
        if b.kind == "always" and any("posedge" in s for s in b.sensitivity):
            return True
    return False


def _has_fsm_signals(ir: ModuleIR) -> bool:
    """Heuristic: any port or known state register name matches FSM pattern."""
    for p in ir.ports:
        if _FSM_STATE_NAMES.match(p.name):
            return True
    # also check always blocks — if there are two (ff + comb), likely FSM
    kinds = {b.kind for b in ir.always_blocks}
    return "always_ff" in kinds and "always_comb" in kinds


def _has_memory_signals(ir: ModuleIR) -> bool:
    port_names = " ".join(p.name for p in ir.ports)
    return bool(_MEMORY_SIGNALS.search(port_names))


def _protocol_score(ir: ModuleIR) -> float:
    """Fraction of ports that match a known protocol lexicon."""
    if not ir.ports:
        return 0.0
    port_str = " ".join(p.name for p in ir.ports)
    matches = sum(1 for p in ir.ports if (
        _AXI_SIGNALS.search(p.name) or
        _AHB_SIGNALS.search(p.name) or
        _APB_SIGNALS.search(p.name) or
        _HANDSHAKE_SIGNALS.search(p.name)
    ))
    return matches / len(ir.ports)


def classify(ir: ModuleIR) -> str:
    """
    Return one of: blackbox | interface | fsm | memory | sequential | combinational
    Sets ir.module_type in place and returns it.
    """
    if ir.parse_status != "ok":
        ir.module_type = "blackbox"
        return "blackbox"

    if _protocol_score(ir) >= 0.70:
        ir.module_type = "interface"
        return "interface"

    if _has_always_ff(ir):
        if _has_fsm_signals(ir):
            ir.module_type = "fsm"
            return "fsm"
        if _has_memory_signals(ir):
            ir.module_type = "memory"
            return "memory"
        ir.module_type = "sequential"
        return "sequential"

    ir.module_type = "combinational"
    return "combinational"
