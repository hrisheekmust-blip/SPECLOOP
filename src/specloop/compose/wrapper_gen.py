"""LLM-based SystemVerilog wrapper generator."""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from jinja2 import DictLoader, Environment, FileSystemLoader

from specloop.gen.client import LLMClient
from specloop.gen.pipeline import _sanitize_sv, _SEP, _WRAPPER_SUFFIX
from specloop.compose.schema import CompatibilityResult, CompositionPlan, SelectedModule
from specloop.compose.protocol_templates import ProtocolPlan, detect_protocols
from specloop.ir.schema import Port

log = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).parent / "prompts"

# Characters that indicate a parameter default is a derived expression rather
# than a simple literal.  A defparam containing these causes Yosys to error:
# "Module name in defparam contains non-constant expressions."
_EXPR_CHARS = frozenset("$()+-*/|&^?:")


def is_derived_param(default: Optional[str]) -> bool:
    """Return True if `default` is an expression that cannot be used in #()."""
    if not default:
        return False
    return bool(_EXPR_CHARS & set(default))


class WrapperGenerator:
    def __init__(self, client: LLMClient) -> None:
        self._client = client
        self._env = Environment(
            loader=FileSystemLoader(str(_PROMPTS_DIR)),
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
        )

    def generate(
        self,
        request: str,
        plan: CompositionPlan,
        selected: list[SelectedModule],
        compat: CompatibilityResult,
    ) -> str:
        """Generate SystemVerilog wrapper text. Returns raw SV source."""
        # Deterministically wire known protocols (clock/reset, valid/ready, AXI-Lite)
        # so the LLM only reasons about the genuinely ambiguous ports. Best-effort:
        # detect_protocols never raises, and an empty plan reproduces full-LLM wiring.
        protocol = detect_protocols(selected, plan)
        leftovers = _leftover_ports(selected, protocol)
        if protocol.has_any:
            log.info(
                "Protocol templates fixed %d connection(s); %d port(s) left to the LLM",
                len(protocol.bindings), sum(len(v) for v in leftovers.values()),
            )

        src = self._env.loader.get_source(self._env, "wrapper_gen.j2")[0]
        patched_env = Environment(
            loader=DictLoader({"__tpl__": src + _WRAPPER_SUFFIX}),
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
        )
        patched_env.globals["is_derived_param"] = is_derived_param
        rendered = patched_env.get_template("__tpl__").render(
            request=request,
            plan=plan,
            selected=selected,
            warnings=[i.message for i in compat.warnings],
            protocol=protocol,
            fixed_by_instance=protocol.bindings_by_instance(),
            leftovers=leftovers,
        )
        parts = rendered.split(_SEP)
        system = parts[1].strip() if len(parts) > 1 else ""
        user = parts[2].strip() if len(parts) > 2 else ""

        sv_text = self._client.generate(system, user)

        # Strip markdown fences if the LLM added them
        sv_text = re.sub(r"^```(?:systemverilog|verilog|sv)?\s*", "", sv_text.strip(), flags=re.MULTILINE)
        sv_text = re.sub(r"```\s*$", "", sv_text, flags=re.MULTILINE)

        return _sanitize_sv(sv_text.strip())


def _leftover_ports(
    selected: list[SelectedModule],
    protocol: ProtocolPlan,
) -> dict[str, list[Port]]:
    """Ports per instance NOT covered by a deterministic protocol binding.

    These are the only ports the LLM must reason about; everything else is fixed.
    """
    fixed = protocol.fixed_ports()
    out: dict[str, list[Port]] = {}
    for sm in selected:
        remaining = [p for p in sm.ir.ports if (sm.sub_function_id, p.name) not in fixed]
        if remaining:
            out[sm.sub_function_id] = remaining
    return out
