"""LLM-based composition assertion generator."""
from __future__ import annotations

import logging
import re
from pathlib import Path

from jinja2 import DictLoader, Environment, FileSystemLoader

from specloop.gen.client import LLMClient
from specloop.gen.pipeline import _parse_json, _sanitize_sv, _SEP, _WRAPPER_SUFFIX
from specloop.gen.schema import BindResult
from specloop.training.schema import AssertionEntry
from specloop.compose.schema import CompositionPlan, SelectedModule

log = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).parent / "prompts"


class CompositionAssertionGenerator:
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
        wrapper_sv: str,
    ) -> BindResult:
        src = self._env.loader.get_source(self._env, "compose_assertions.j2")[0]
        patched_env = Environment(
            loader=DictLoader({"__tpl__": src + _WRAPPER_SUFFIX}),
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
        )
        rendered = patched_env.get_template("__tpl__").render(
            request=request,
            plan=plan,
            selected=selected,
            wrapper_sv=wrapper_sv,
        )
        parts = rendered.split(_SEP)
        system = parts[1].strip() if len(parts) > 1 else ""
        user = parts[2].strip() if len(parts) > 2 else ""

        raw = self._client.generate(system, user)
        log.debug("compose_assertions raw LLM response (len=%d):\n%s", len(raw), raw)

        data = _parse_json(raw, "compose_assertions")

        if not data:
            log.warning(
                "compose_assertions: JSON parse returned empty dict.\n"
                "  Raw response (first 2000 chars):\n%.2000s",
                raw,
            )

        # Accept both key spellings the model might use
        bind_sv = _sanitize_sv(
            data.get("bind_module") or data.get("bind_module_sv") or ""
        )

        log.debug(
            "compose_assertions parsed keys=%s  bind_sv_len=%d",
            list(data.keys()),
            len(bind_sv),
        )

        index: list[AssertionEntry] = []
        for entry in data.get("assertion_index", []):
            try:
                index.append(AssertionEntry.model_validate(entry))
            except Exception:
                pass

        if not bind_sv:
            # Fallback: try to extract a module...endmodule block from the raw text
            bind_sv = _extract_sv_fallback(raw, plan)
            if bind_sv:
                log.warning(
                    "compose_assertions: used regex SV fallback (JSON bind_module was empty)"
                )
            else:
                log.warning(
                    "compose_assertions: no bind_module in response and regex fallback failed"
                    " — using placeholder"
                )
                bind_sv = _placeholder_bind(plan)

        return BindResult(
            bind_module_sv=bind_sv,
            assertion_index=index,
            model_id=self._client.model_id,
        )


def _extract_sv_fallback(raw: str, plan: CompositionPlan) -> str:
    """Try to recover a bind module from raw text when JSON parsing fails.

    Handles two common failure modes:
    1. LLM returned SV inside a ```systemverilog fence rather than JSON-encoded
    2. LLM returned the module/bind text as plain text after some preamble
    """
    # Try fenced code block first
    m = re.search(r"```(?:systemverilog|verilog|sv)?\s*(.*?)```", raw, re.DOTALL | re.IGNORECASE)
    if m:
        candidate = m.group(1).strip()
        if "module" in candidate and "endmodule" in candidate:
            return _sanitize_sv(candidate)

    # Try bare module...endmodule block with a bind statement
    m = re.search(
        r"(module\s+\w+.*?endmodule\s*[\r\n]+\s*bind\s+\w[^;]*;)",
        raw,
        re.DOTALL,
    )
    if m:
        return _sanitize_sv(m.group(1).strip())

    return ""


def _placeholder_bind(plan: CompositionPlan) -> str:
    return (
        f"module {plan.composition_name}_spec (input logic clk, input logic rst_n);\n"
        f"  always @(posedge clk) begin\n"
        f"    ap_placeholder: assert(1'b1);\n"
        f"  end\n"
        f"endmodule\n\n"
        f"bind {plan.composition_name} {plan.composition_name}_spec spec_inst (.*);\n"
    )
