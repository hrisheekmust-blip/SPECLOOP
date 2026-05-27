"""3-stage assertion generation pipeline.

Stage 1 — Behavior Extraction: Analyze RTL semantics → BehaviorExtraction JSON
Stage 2 — Property Synthesis:  Generate candidate SVA properties → PropertySynthesis JSON
Stage 3 — Property Hardening:  Harden candidates into a complete bind module → BindResult JSON
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from jinja2 import DictLoader, Environment, FileSystemLoader

from specloop.gen.client import LLMClient
from specloop.gen.schema import (
    BehaviorExtraction,
    BindResult,
    PropertySynthesis,
)
from specloop.ir.schema import ModuleIR
from specloop.training.schema import AssertionEntry

log = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).parent / "prompts"

# Wrapper appended to each template to emit system/user separated by a unique marker
_SEP = "<<<SPECLOOP_SEP>>>"
_WRAPPER_SUFFIX = f"\n{_SEP}{{{{ system }}}}{_SEP}{{{{ user }}}}{_SEP}"


class AssertionPipeline:
    """Run the 3-stage LLM pipeline to generate an SVA bind module."""

    def __init__(self, client: LLMClient) -> None:
        self._client = client
        self._file_env = Environment(
            loader=FileSystemLoader(str(_PROMPTS_DIR)),
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
        )

    def _get_prompts(self, template_name: str, **ctx) -> tuple[str, str]:
        """Render a prompt template; return (system_prompt, user_prompt)."""
        src = self._file_env.loader.get_source(self._file_env, template_name)[0]
        patched_env = Environment(
            loader=DictLoader({"__tpl__": src + _WRAPPER_SUFFIX}),
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
        )
        rendered = patched_env.get_template("__tpl__").render(**ctx)
        parts = rendered.split(_SEP)
        # parts[0] = empty (template output before sep), [1] = system, [2] = user
        system = parts[1].strip() if len(parts) > 1 else ""
        user = parts[2].strip() if len(parts) > 2 else ""
        return system, user

    def run(self, ir: ModuleIR, rtl_source: str) -> BindResult:
        """Execute all three stages and return a BindResult."""
        log.info("Stage 1: behavior extraction for '%s'", ir.module)
        behavior = self._stage1_behavior(ir, rtl_source)

        log.info("Stage 2: property synthesis for '%s'", ir.module)
        synthesis = self._stage2_synthesis(ir, rtl_source, behavior)

        log.info("Stage 3: property hardening for '%s'", ir.module)
        result = self._stage3_harden(ir, rtl_source, synthesis)
        result.stage1 = behavior
        result.stage2 = synthesis
        result.model_id = self._client.model_id
        return result

    # ------------------------------------------------------------------
    # Stage implementations
    # ------------------------------------------------------------------

    def _stage1_behavior(self, ir: ModuleIR, rtl_source: str) -> BehaviorExtraction:
        system, user = self._get_prompts("behavior_extraction.j2", ir=ir, rtl_source=rtl_source)
        raw = self._client.generate(system, user)
        data = _parse_json(raw, "behavior_extraction")
        try:
            return BehaviorExtraction.model_validate(data)
        except Exception as exc:
            log.warning("Stage 1 parse error: %s — using fallback", exc)
            return BehaviorExtraction(
                clock_ports=[p.name for p in ir.ports if p.is_clock],
                reset_ports=[p.name for p in ir.ports if p.is_reset],
                reset_active_low=any(
                    p.reset_polarity == "low" for p in ir.ports if p.is_reset
                ),
            )

    def _stage2_synthesis(
        self, ir: ModuleIR, rtl_source: str, behavior: BehaviorExtraction
    ) -> PropertySynthesis:
        system, user = self._get_prompts(
            "property_synthesis.j2", ir=ir, rtl_source=rtl_source, behavior=behavior
        )
        raw = self._client.generate(system, user)
        data = _parse_json(raw, "property_synthesis")
        try:
            return PropertySynthesis.model_validate(data)
        except Exception as exc:
            log.warning("Stage 2 parse error: %s — using empty synthesis", exc)
            return PropertySynthesis()

    def _stage3_harden(
        self, ir: ModuleIR, rtl_source: str, synthesis: PropertySynthesis
    ) -> BindResult:
        system, user = self._get_prompts(
            "property_hardening.j2",
            ir=ir,
            rtl_source=rtl_source,
            candidates=synthesis.candidates,
        )
        raw = self._client.generate(system, user)
        data = _parse_json(raw, "property_hardening")
        bind_sv = _sanitize_sv(data.get("bind_module", ""))
        index_raw = data.get("assertion_index", [])
        index = []
        for entry in index_raw:
            try:
                index.append(AssertionEntry.model_validate(entry))
            except Exception:
                pass
        if not bind_sv:
            log.warning("Stage 3 returned no bind_module — using placeholder")
            bind_sv = _placeholder_bind(ir)
        return BindResult(bind_module_sv=bind_sv, assertion_index=index)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sanitize_sv(sv: str) -> str:
    """Replace non-ASCII characters with underscores (SV identifiers must be ASCII)."""
    return re.sub(r'[^\x00-\x7F]', '_', sv)


def _parse_json(raw: str, stage: str) -> dict:
    """Extract and parse the first JSON object from a model response.

    Two failure modes fixed vs a naive brace-depth scan:
    1. { and } inside JSON string values (e.g. SV replication {8{1'b0}}) would
       corrupt a character-blind depth counter → now skips all chars inside strings.
    2. LLMs sometimes emit literal newline characters inside a JSON string value
       instead of the \\n escape, making json.loads reject an otherwise correct
       payload → retried after escaping control chars inside strings.
    """
    raw = raw.strip()
    # Strip markdown code fences
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"```\s*$", "", raw, flags=re.MULTILINE)
    raw = raw.strip()

    try:
        start = raw.index("{")
    except ValueError:
        log.warning("Could not parse JSON from %s: no '{' found\nRaw: %.300s", stage, raw)
        return {}

    depth = 0
    in_string = False
    escape_next = False
    for i, ch in enumerate(raw[start:], start):
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue  # { and } inside string values don't affect nesting depth
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = raw[start : i + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    # Retry after escaping literal control chars in string values.
                    # json.loads rejects a literal \n inside a string even though
                    # the rest of the JSON is structurally valid.
                    try:
                        return json.loads(_escape_control_chars_in_strings(candidate))
                    except json.JSONDecodeError as exc:
                        log.warning(
                            "Could not parse JSON from %s: %s\nRaw: %.300s",
                            stage, exc, raw,
                        )
                        return {}

    log.warning(
        "Could not parse JSON from %s: unbalanced braces\nRaw: %.300s", stage, raw
    )
    return {}


def _escape_control_chars_in_strings(s: str) -> str:
    """Escape literal newlines/tabs/carriage-returns inside JSON string values."""
    out: list[str] = []
    in_string = False
    escape_next = False
    for ch in s:
        if escape_next:
            out.append(ch)
            escape_next = False
            continue
        if ch == "\\" and in_string:
            out.append(ch)
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            out.append(ch)
            continue
        if in_string:
            if ch == "\n":
                out.append("\\n")
            elif ch == "\r":
                out.append("\\r")
            elif ch == "\t":
                out.append("\\t")
            else:
                out.append(ch)
        else:
            out.append(ch)
    return "".join(out)


def _placeholder_bind(ir: ModuleIR) -> str:
    """Generate a minimal placeholder bind module when Stage 3 fails."""
    clocks = [p.name for p in ir.ports if p.is_clock]
    clk = clocks[0] if clocks else "clk"
    port_decls = "\n".join(
        f"    {'logic' if p.width == 1 else f'logic [{p.width-1}:0]'} {p.name};"
        for p in ir.ports
    )
    return (
        f"module {ir.module}_spec (\n{port_decls}\n);\n"
        f"    // TODO: assertions not generated — check LLM output\n"
        f"endmodule\n\n"
        f"bind {ir.module} {ir.module}_spec spec_inst (.*);\n"
    )
