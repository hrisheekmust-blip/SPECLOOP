"""Deterministic protocol-aware wiring for composition wrappers.

For standard hardware interface protocols the correct wiring is not a judgement
call — there is exactly one right way to connect a producer's valid/ready/data to
a consumer's. Handing those ports to the LLM only invites the classic mistakes:
reversed ready direction, dropped backpressure, mis-routed clock. This module
detects known protocols from port-naming patterns and emits the connections
deterministically, so the LLM is left to reason only about the genuinely
ambiguous ports.

Protocols handled:
  * clock / reset  — shared; every clock port joins the top-level clock, every
                     reset port joins the top-level reset (inverting for any
                     active-high port so a single top reset drives them all).
  * valid / ready  — the producer drives `valid` and waits on `ready`; the
                     consumer drives `ready` and waits on `valid`. The `ready`
                     wire therefore runs consumer→producer — the exact direction
                     the LLM most often reverses.
  * AXI-Lite       — the five channels (AW, W, B, AR, R) matched by canonical
                     signal name between master and slave.

Everything is best-effort and conservative: only confident matches are fixed,
and :func:`detect_protocols` never raises — on any trouble it returns an empty
plan and the caller falls back to full-LLM wiring.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from pydantic import BaseModel

from specloop.compose.schema import CompositionPlan, SelectedModule
from specloop.ir.schema import Port

log = logging.getLogger(__name__)

# Suffix patterns (anchored at end of the lowercased port name).
_VALID_RE = re.compile(r"(valid|vld)$")
_READY_RE = re.compile(r"(ready|rdy)$")
_DATA_RE = re.compile(r"(tdata|data|payload|tkeep|tlast|tstrb)$")
# Trailing run stripped to normalize a channel base (handles AXI-stream's shared
# `t` prefix: tvalid/tready/tdata all reduce to the same base).
_BASE_STRIP_RE = re.compile(r"[_\-t]+$")

# Canonical AXI-Lite signal roots (without the master/slave prefix). Matched
# case-insensitively as a suffix so `s_axi_awvalid`, `m_axi_awvalid`, `awvalid`
# all map to the same canonical name.
_AXI_LITE_SIGNALS = [
    "awvalid", "awready", "awaddr", "awprot",
    "wvalid", "wready", "wdata", "wstrb",
    "bvalid", "bready", "bresp",
    "arvalid", "arready", "araddr", "arprot",
    "rvalid", "rready", "rdata", "rresp",
]


def _ident(s: str) -> str:
    """Coerce a string into a safe SystemVerilog identifier fragment."""
    return re.sub(r"[^A-Za-z0-9_]", "_", s)


# ---------------------------------------------------------------------------
# Output models
# ---------------------------------------------------------------------------

class FixedWire(BaseModel):
    name: str
    width: int = 1
    comment: str = ""


class FixedAssign(BaseModel):
    """A continuous assign the wrapper must emit (e.g. reset inversion)."""
    lhs: str
    rhs: str
    comment: str = ""


class PortBinding(BaseModel):
    """Authoritative mapping: instance port -> the signal it connects to."""
    inst_id: str
    port: str
    signal: str
    comment: str = ""


class TopPort(BaseModel):
    name: str
    width: int = 1
    direction: str = "input"
    comment: str = ""


class ProtocolPlan(BaseModel):
    """Deterministic wiring decisions handed to the wrapper generator."""
    top_ports: list[TopPort] = []
    internal_wires: list[FixedWire] = []
    extra_assigns: list[FixedAssign] = []
    bindings: list[PortBinding] = []
    notes: list[str] = []

    def fixed_ports(self) -> set[tuple[str, str]]:
        """Set of (inst_id, port) pairs that are already deterministically wired."""
        return {(b.inst_id, b.port) for b in self.bindings}

    def bindings_by_instance(self) -> dict[str, list[PortBinding]]:
        out: dict[str, list[PortBinding]] = {}
        for b in self.bindings:
            out.setdefault(b.inst_id, []).append(b)
        return out

    @property
    def has_any(self) -> bool:
        return bool(self.bindings)


# ---------------------------------------------------------------------------
# Channel detection
# ---------------------------------------------------------------------------

class _Channel(BaseModel):
    inst_id: str
    base: str
    valid: str            # valid port name
    ready: str            # ready port name
    data_ports: list[str] = []
    is_producer: bool     # True when this module drives valid (source side)


def _channel_base(name: str, role_re: re.Pattern) -> str:
    """Normalize a port name to its channel base by removing the role suffix."""
    stem = role_re.sub("", name.lower())
    return _BASE_STRIP_RE.sub("", stem)


def _detect_channels(inst_id: str, ports: list[Port]) -> list[_Channel]:
    """Detect valid/ready handshake channels on one module instance.

    Ports are grouped by normalized base; a group is a channel when it has both a
    valid and a ready port. The valid port's direction sets producer vs consumer.
    """
    # base -> {role -> [port]}
    groups: dict[str, dict[str, list[Port]]] = {}
    for p in ports:
        if p.is_clock or p.is_reset:
            continue
        low = p.name.lower()
        if _VALID_RE.search(low):
            base = _channel_base(p.name, _VALID_RE)
            role = "valid"
        elif _READY_RE.search(low):
            base = _channel_base(p.name, _READY_RE)
            role = "ready"
        elif _DATA_RE.search(low):
            base = _channel_base(p.name, _DATA_RE)
            role = "data"
        else:
            continue
        groups.setdefault(base, {}).setdefault(role, []).append(p)

    channels: list[_Channel] = []
    for base, roles in groups.items():
        if "valid" not in roles or "ready" not in roles:
            continue  # not a complete handshake
        valid_port = roles["valid"][0]
        ready_port = roles["ready"][0]
        channels.append(_Channel(
            inst_id=inst_id,
            base=base,
            valid=valid_port.name,
            ready=ready_port.name,
            data_ports=[p.name for p in roles.get("data", [])],
            is_producer=valid_port.direction == "output",
        ))
    return channels


def _port_residual(name: str) -> str:
    """Port name minus its data-role suffix and base separators — used to match a
    producer data port to the corresponding consumer data port."""
    low = name.lower()
    stem = _DATA_RE.sub("", low)
    return _BASE_STRIP_RE.sub("", stem)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def detect_protocols(
    selected: list[SelectedModule],
    plan: CompositionPlan,
) -> ProtocolPlan:
    """Detect known protocols across the selected modules and produce fixed wiring.

    Never raises: any unexpected condition yields an empty :class:`ProtocolPlan`,
    so the caller degrades to full-LLM wiring.
    """
    try:
        return _detect_protocols(selected, plan)
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("Protocol detection failed (%s) — full-LLM wiring", exc)
        return ProtocolPlan()


def _detect_protocols(
    selected: list[SelectedModule],
    plan: CompositionPlan,
) -> ProtocolPlan:
    result = ProtocolPlan()
    width_of: dict[tuple[str, str], int] = {}
    ports_of: dict[str, list[Port]] = {}
    for sm in selected:
        ports_of[sm.sub_function_id] = sm.ir.ports
        for p in sm.ir.ports:
            width_of[(sm.sub_function_id, p.name)] = p.width

    _wire_clock_reset(selected, result)
    _wire_axi_lite(selected, plan, result, width_of)

    # Valid/ready: skip any port already claimed by AXI (AXI signals are also
    # valid/ready bundles but were handled canonically above).
    claimed = result.fixed_ports()
    _wire_valid_ready(selected, plan, result, width_of, claimed)

    return result


# ---------------------------------------------------------------------------
# Clock / reset
# ---------------------------------------------------------------------------

def _wire_clock_reset(selected: list[SelectedModule], result: ProtocolPlan) -> None:
    clock_ports = [(sm.sub_function_id, p) for sm in selected for p in sm.ir.ports if p.is_clock]
    reset_ports = [(sm.sub_function_id, p) for sm in selected for p in sm.ir.ports if p.is_reset]

    if clock_ports:
        result.top_ports.append(TopPort(name="clk", width=1, direction="input",
                                        comment="shared clock"))
        for inst, p in clock_ports:
            result.bindings.append(PortBinding(inst_id=inst, port=p.name, signal="clk",
                                               comment="shared clock"))

    if not reset_ports:
        return

    polarities = {p.reset_polarity for _, p in reset_ports}
    # Expose an active-high top reset only when every reset port is active-high;
    # otherwise default to active-low `rst_n` (the project convention) and invert
    # for any active-high ports.
    if polarities == {"high"}:
        result.top_ports.append(TopPort(name="rst", width=1, direction="input",
                                        comment="shared reset (active-high)"))
        for inst, p in reset_ports:
            result.bindings.append(PortBinding(inst_id=inst, port=p.name, signal="rst",
                                               comment="shared reset"))
        return

    result.top_ports.append(TopPort(name="rst_n", width=1, direction="input",
                                    comment="shared reset (active-low)"))
    need_inv = False
    for inst, p in reset_ports:
        if p.reset_polarity == "high":
            need_inv = True
            result.bindings.append(PortBinding(inst_id=inst, port=p.name, signal="rst_inv",
                                               comment="reset inverted for active-high port"))
        else:
            result.bindings.append(PortBinding(inst_id=inst, port=p.name, signal="rst_n",
                                               comment="shared reset"))
    if need_inv:
        result.internal_wires.append(FixedWire(name="rst_inv", width=1,
                                               comment="active-high view of rst_n"))
        result.extra_assigns.append(FixedAssign(lhs="rst_inv", rhs="~rst_n",
                                                comment="invert shared reset"))


# ---------------------------------------------------------------------------
# Valid / ready
# ---------------------------------------------------------------------------

def _wire_valid_ready(
    selected: list[SelectedModule],
    plan: CompositionPlan,
    result: ProtocolPlan,
    width_of: dict[tuple[str, str], int],
    claimed: set[tuple[str, str]],
) -> None:
    # Detect channels per instance, excluding ports already claimed by AXI.
    channels: list[_Channel] = []
    for sm in selected:
        for ch in _detect_channels(sm.sub_function_id, sm.ir.ports):
            if (ch.inst_id, ch.valid) in claimed or (ch.inst_id, ch.ready) in claimed:
                continue
            channels.append(ch)

    if not channels:
        return

    producers = [c for c in channels if c.is_producer]
    consumers = [c for c in channels if not c.is_producer]
    if not producers or not consumers:
        return

    pairs = _pair_channels(producers, consumers, plan)
    for prod, cons in pairs:
        _emit_handshake(prod, cons, result, width_of)


def _pair_channels(
    producers: list[_Channel],
    consumers: list[_Channel],
    plan: CompositionPlan,
) -> list[tuple[_Channel, _Channel]]:
    """Pair producer channels with consumer channels.

    Primary signal is the declared connection list: a connection whose endpoints
    fall on a producer channel and a consumer channel pins that pair. When no
    connection disambiguates and there is exactly one producer and one consumer
    channel (on different instances), they are paired directly. Conservative —
    ambiguous many-to-many cases are left to the LLM.
    """
    pairs: list[tuple[_Channel, _Channel]] = []
    used_prod: set[int] = set()
    used_cons: set[int] = set()

    def ch_ports(c: _Channel) -> set[str]:
        return {c.valid, c.ready, *c.data_ports}

    for conn in plan.connections:
        for pi, prod in enumerate(producers):
            if pi in used_prod or prod.inst_id != conn.from_id:
                continue
            if conn.from_port not in ch_ports(prod):
                continue
            for ci, cons in enumerate(consumers):
                if ci in used_cons or cons.inst_id != conn.to_id:
                    continue
                if conn.to_port not in ch_ports(cons):
                    continue
                pairs.append((prod, cons))
                used_prod.add(pi)
                used_cons.add(ci)
                break

    # Fallback: exactly one unpaired producer and one unpaired consumer.
    rem_prod = [(i, c) for i, c in enumerate(producers) if i not in used_prod]
    rem_cons = [(i, c) for i, c in enumerate(consumers) if i not in used_cons]
    if len(rem_prod) == 1 and len(rem_cons) == 1:
        (_, prod), (_, cons) = rem_prod[0], rem_cons[0]
        if prod.inst_id != cons.inst_id:
            pairs.append((prod, cons))

    return pairs


def _emit_handshake(
    prod: _Channel,
    cons: _Channel,
    result: ProtocolPlan,
    width_of: dict[tuple[str, str], int],
) -> None:
    """Emit the wires + bindings for one producer→consumer handshake.

    valid: producer drives, consumer receives.  ready: consumer drives, producer
    receives (consumer→producer — the backpressure direction).  data: producer
    drives, consumer receives.
    """
    tag = f"{_ident(prod.inst_id)}_{_ident(cons.inst_id)}"

    valid_w = f"vr_{tag}_valid"
    result.internal_wires.append(FixedWire(name=valid_w, width=1,
                                           comment=f"{prod.inst_id}.{prod.valid} -> {cons.inst_id}.{cons.valid}"))
    result.bindings.append(PortBinding(inst_id=prod.inst_id, port=prod.valid, signal=valid_w,
                                       comment="producer drives valid"))
    result.bindings.append(PortBinding(inst_id=cons.inst_id, port=cons.valid, signal=valid_w,
                                       comment="consumer receives valid"))

    ready_w = f"vr_{tag}_ready"
    result.internal_wires.append(FixedWire(name=ready_w, width=1,
                                           comment=f"{cons.inst_id}.{cons.ready} -> {prod.inst_id}.{prod.ready} (backpressure)"))
    result.bindings.append(PortBinding(inst_id=cons.inst_id, port=cons.ready, signal=ready_w,
                                       comment="consumer drives ready"))
    result.bindings.append(PortBinding(inst_id=prod.inst_id, port=prod.ready, signal=ready_w,
                                       comment="producer receives ready (backpressure)"))

    # Data: match producer outputs to consumer inputs by residual name; if each
    # side has exactly one data port, pair them regardless of residual.
    cons_by_residual = {_port_residual(d): d for d in cons.data_ports}
    matched_cons: set[str] = set()
    for d in prod.data_ports:
        res = _port_residual(d)
        cd = cons_by_residual.get(res)
        if cd is None and len(prod.data_ports) == 1 and len(cons.data_ports) == 1:
            cd = cons.data_ports[0]
        if cd is None or cd in matched_cons:
            continue
        matched_cons.add(cd)
        width = width_of.get((prod.inst_id, d), 1)
        data_w = f"vr_{tag}_{_ident(res or 'data')}"
        result.internal_wires.append(FixedWire(name=data_w, width=width,
                                               comment=f"{prod.inst_id}.{d} -> {cons.inst_id}.{cd}"))
        result.bindings.append(PortBinding(inst_id=prod.inst_id, port=d, signal=data_w,
                                           comment="producer drives data"))
        result.bindings.append(PortBinding(inst_id=cons.inst_id, port=cd, signal=data_w,
                                           comment="consumer receives data"))

    result.notes.append(
        f"valid/ready handshake: {prod.inst_id}.{prod.valid}/{prod.ready} → "
        f"{cons.inst_id}.{cons.valid}/{cons.ready} (ready flows consumer→producer)"
    )


# ---------------------------------------------------------------------------
# AXI-Lite
# ---------------------------------------------------------------------------

def _axi_canon(name: str) -> Optional[str]:
    """Return the canonical AXI-Lite signal name a port maps to, or None."""
    low = name.lower()
    for sig in _AXI_LITE_SIGNALS:
        if low.endswith(sig):
            return sig
    return None


def _axi_map(ports: list[Port]) -> dict[str, Port]:
    """Map canonical AXI-Lite signal -> the module's port implementing it."""
    out: dict[str, Port] = {}
    for p in ports:
        canon = _axi_canon(p.name)
        if canon and canon not in out:
            out[canon] = p
    return out


def _wire_axi_lite(
    selected: list[SelectedModule],
    plan: CompositionPlan,
    result: ProtocolPlan,
    width_of: dict[tuple[str, str], int],
) -> None:
    """Detect a single AXI-Lite master/slave pair and wire all shared channels.

    A module is a master when it drives `awvalid` (output) and a slave when it
    receives it (input). Ports are matched across the pair by canonical signal
    name, so address/data/valid connect like-to-like and `ready` is never crossed.
    """
    masters: list[tuple[str, dict[str, Port]]] = []
    slaves: list[tuple[str, dict[str, Port]]] = []
    for sm in selected:
        amap = _axi_map(sm.ir.ports)
        aw = amap.get("awvalid")
        if aw is None:
            continue
        if aw.direction == "output":
            masters.append((sm.sub_function_id, amap))
        else:
            slaves.append((sm.sub_function_id, amap))

    if len(masters) != 1 or len(slaves) != 1:
        return  # only the unambiguous single-pair case is auto-wired

    (m_id, m_map), (s_id, s_map) = masters[0], slaves[0]
    wired = 0
    for sig in _AXI_LITE_SIGNALS:
        mp, sp = m_map.get(sig), s_map.get(sig)
        if mp is None or sp is None:
            continue
        # Directions must be opposite (one drives, one receives).
        if mp.direction == sp.direction:
            continue
        wire = f"axi_{_ident(m_id)}_{_ident(s_id)}_{sig}"
        result.internal_wires.append(FixedWire(name=wire, width=mp.width,
                                               comment=f"AXI-Lite {sig}"))
        result.bindings.append(PortBinding(inst_id=m_id, port=mp.name, signal=wire,
                                           comment=f"AXI-Lite {sig}"))
        result.bindings.append(PortBinding(inst_id=s_id, port=sp.name, signal=wire,
                                           comment=f"AXI-Lite {sig}"))
        wired += 1

    if wired:
        result.notes.append(
            f"AXI-Lite: master {m_id} ↔ slave {s_id} ({wired} canonical signals wired)"
        )
