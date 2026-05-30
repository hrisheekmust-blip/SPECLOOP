"""4-stage assertion generation pipeline.

Stage 0 — Module Spec:        Structured behavioral spec from IR alone → ModuleSpec JSON
Stage 1 — Behavior Extraction: Analyze RTL semantics → BehaviorExtraction JSON
Stage 2 — Property Synthesis:  Generate candidate SVA properties → PropertySynthesis JSON
Stage 3 — Property Hardening:  Harden candidates into a complete bind module → BindResult JSON
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional

from jinja2 import DictLoader, Environment, FileSystemLoader

from specloop.gen.client import LLMClient
from specloop.gen.schema import (
    BehaviorExtraction,
    BindResult,
    ModuleSpec,
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

    def __init__(self, client: LLMClient, enable_spec: bool = True) -> None:
        self._client = client
        self._enable_spec = enable_spec
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
        """Execute all stages and return a BindResult."""
        spec: Optional[ModuleSpec] = None
        if self._enable_spec:
            log.info("Stage 0: module spec generation for '%s'", ir.module)
            spec = self._stage0_spec(ir)

        log.info("Stage 1: behavior extraction for '%s'", ir.module)
        behavior = self._stage1_behavior(ir, rtl_source, spec=spec)

        log.info("Stage 2: property synthesis for '%s'", ir.module)
        synthesis = self._stage2_synthesis(ir, rtl_source, behavior, spec=spec)

        log.info("Stage 3: property hardening for '%s'", ir.module)
        result = self._stage3_harden(ir, rtl_source, synthesis, spec=spec)
        result.stage0 = spec
        result.stage1 = behavior
        result.stage2 = synthesis
        result.model_id = self._client.model_id
        return result

    # ------------------------------------------------------------------
    # Stage implementations
    # ------------------------------------------------------------------

    def _stage0_spec(self, ir: ModuleIR) -> ModuleSpec:
        system, user = self._get_prompts("module_spec.j2", ir=ir)
        raw = self._client.generate(system, user)
        log.debug("Stage 0 raw response (len=%d):\n%.500s", len(raw), raw)
        data = _parse_json(raw, "module_spec")
        try:
            return ModuleSpec.model_validate(data)
        except Exception as exc:
            log.warning("Stage 0 parse error: %s — using empty spec", exc)
            return ModuleSpec(module_type=ir.module_type or "")

    def _stage1_behavior(
        self, ir: ModuleIR, rtl_source: str, spec: Optional[ModuleSpec] = None
    ) -> BehaviorExtraction:
        system, user = self._get_prompts("behavior_extraction.j2", ir=ir, rtl_source=rtl_source, spec=spec)
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
        self,
        ir: ModuleIR,
        rtl_source: str,
        behavior: BehaviorExtraction,
        spec: Optional[ModuleSpec] = None,
    ) -> PropertySynthesis:
        system, user = self._get_prompts(
            "property_synthesis.j2", ir=ir, rtl_source=rtl_source, behavior=behavior, spec=spec
        )
        raw = self._client.generate(system, user)
        data = _parse_json(raw, "property_synthesis")
        try:
            return PropertySynthesis.model_validate(data)
        except Exception as exc:
            log.warning("Stage 2 parse error: %s — using empty synthesis", exc)
            return PropertySynthesis()

    def _stage3_harden(
        self,
        ir: ModuleIR,
        rtl_source: str,
        synthesis: PropertySynthesis,
        spec: Optional[ModuleSpec] = None,
        few_shot: list[dict] | None = None,
    ) -> BindResult:
        system, user = self._get_prompts(
            "property_hardening.j2",
            ir=ir,
            rtl_source=rtl_source,
            candidates=synthesis.candidates,
            spec=spec,
            few_shot_examples=few_shot or [],
        )
        raw = self._client.generate(system, user)
        data = _parse_json(raw, "property_hardening")
        bind_sv = _sanitize_sv(data.get("bind_module", ""))
        bind_sv = _sanitize_property_assertions(bind_sv)
        bind_sv = _sanitize_bind_ports(bind_sv)
        bind_sv = _hoist_wires_from_always(bind_sv)
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

    def fetch_few_shot_examples(
        self,
        ir: ModuleIR,
        qdrant_url: str,
        collection: str,
        embed_model: str,
        work_dir: Path,
        top_k: int = 3,
    ) -> list[dict]:
        """Query Qdrant for structurally similar proven modules and load their bind SVs.

        Returns [] silently if Qdrant is unavailable, the collection is empty, or no
        bind files are present on disk — Changes 1, 2, and 4 are unaffected.
        """
        try:
            from specloop.search.searcher import search
            query = f"{ir.module} {ir.module_type or ''}"
            results = search(query, qdrant_url, collection, embed_model, top_k=top_k + 1)
        except Exception as exc:
            log.debug("Few-shot Qdrant query skipped: %s", exc)
            return []

        examples: list[dict] = []
        for r in results:
            if r.module_name == ir.module:
                continue
            bind_path = work_dir / f"{r.module_name}.bind.sv"
            if not bind_path.exists():
                continue
            examples.append({
                "module_name": r.module_name,
                "assertion_summary": r.assertion_summary,
                "bind_module_sv": bind_path.read_text(encoding="utf-8", errors="replace")[:2000],
            })
            if len(examples) >= top_k:
                break

        log.debug("Few-shot examples fetched: %d", len(examples))
        return examples

    def run_decomposed(self, ir: ModuleIR, rtl_source: str) -> BindResult:
        """Signal-group decomposition (AssertGen): run one pipeline call per always block.

        Falls through to the standard `run()` when the module has ≤ 2 always blocks,
        preserving existing behavior for simple modules.
        """
        if len(ir.always_blocks) <= 2:
            return self.run(ir, rtl_source)

        log.info(
            "Decomposed mode: %d always blocks for '%s'", len(ir.always_blocks), ir.module
        )

        # Stage 0 runs once over the whole module
        spec: Optional[ModuleSpec] = None
        if self._enable_spec:
            log.info("Stage 0 (decomposed): module spec for '%s'", ir.module)
            spec = self._stage0_spec(ir)

        lines = rtl_source.splitlines()
        # Module header = everything before the first always block with a known start_line
        header_end = _first_block_line(ir.always_blocks, len(lines))
        header_lines = lines[: header_end]

        group_results: list[BindResult] = []
        for i, block in enumerate(ir.always_blocks):
            slice_rtl = _slice_rtl(lines, block, header_lines)
            slice_ir = ir.model_copy(update={"always_blocks": [block]})
            log.info(
                "  Group %d/%d: %s @(%s)",
                i + 1, len(ir.always_blocks), block.kind,
                ", ".join(block.sensitivity) if block.sensitivity else "*",
            )
            behavior = self._stage1_behavior(slice_ir, slice_rtl, spec=spec)
            synthesis = self._stage2_synthesis(slice_ir, slice_rtl, behavior, spec=spec)
            result = self._stage3_harden(slice_ir, slice_rtl, synthesis, spec=spec)
            # Prefix assertion names to avoid collisions when groups are merged
            result = _prefix_assertions(result, f"g{i}_")
            group_results.append(result)

        merged = _merge_bind_results(group_results, ir.module, spec=spec)
        merged.model_id = self._client.model_id
        return merged


# ---------------------------------------------------------------------------
# Decomposition helpers
# ---------------------------------------------------------------------------

def _first_block_line(blocks: list, n_lines: int) -> int:
    """Return the 0-based line index of the first always block, or n_lines if unknown."""
    for b in blocks:
        if b.start_line > 0:
            return b.start_line - 1  # start_line is 1-based
    return n_lines


def _slice_rtl(lines: list[str], block, header_lines: list[str]) -> str:
    """Build a focused RTL slice: module header + this always block's source lines."""
    if block.start_line > 0 and block.end_line >= block.start_line:
        block_lines = lines[block.start_line - 1 : block.end_line]
    else:
        # Fallback: return full source when line numbers are unavailable
        return "\n".join(lines)
    return "\n".join(header_lines + ["  // ... (other always blocks omitted) ..."] + block_lines)


def _prefix_assertions(result: BindResult, prefix: str) -> BindResult:
    """Rename assertion labels in bind_sv and index entries with a group prefix."""
    sv = re.sub(r'\bap_', prefix + "ap_", result.bind_module_sv)
    index = []
    for entry in result.assertion_index:
        index.append(entry.model_copy(update={"name": prefix + entry.name}))
    return result.model_copy(update={"bind_module_sv": sv, "assertion_index": index})


def _merge_bind_results(
    results: list[BindResult], module_name: str, spec: Optional[ModuleSpec] = None
) -> BindResult:
    """Merge per-group bind results into a single bind module."""
    if not results:
        return BindResult(bind_module_sv=f"// no groups\n", assertion_index=[])

    # Collect all always blocks from each group's bind module
    always_block_re = re.compile(
        r"(always\s+@\s*\(posedge\b[^)]*\)\s*begin.*?end)", re.DOTALL
    )
    merged_blocks: list[str] = []
    merged_index: list = []
    for r in results:
        merged_blocks.extend(always_block_re.findall(r.bind_module_sv))
        merged_index.extend(r.assertion_index)

    # Use port list from first result's bind module header
    first_sv = results[0].bind_module_sv
    port_match = re.search(
        r"module\s+\w+\s*\((.*?)\)\s*;", first_sv, re.DOTALL
    )
    port_decl = port_match.group(1).strip() if port_match else "input logic clk"

    blocks_sv = "\n\n  ".join(merged_blocks)
    bind_sv = (
        f"module {module_name}_spec (\n  {port_decl}\n);\n\n"
        f"  {blocks_sv}\n\n"
        f"endmodule\n\n"
        f"bind {module_name} {module_name}_spec spec_inst (.*);\n"
    )
    return BindResult(
        bind_module_sv=_sanitize_sv(bind_sv),
        assertion_index=merged_index,
        stage0=spec,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sanitize_sv(sv: str) -> str:
    """Replace non-ASCII characters with underscores (SV identifiers must be ASCII)."""
    return re.sub(r'[^\x00-\x7F]', '_', sv)


def _hoist_wires_from_always(sv: str) -> str:
    """Move wire/assign declarations from inside always blocks to module level.

    A wire or assign inside a procedural block is illegal SystemVerilog and
    causes Yosys elaboration to silently fail (returning UNKNOWN at ~0s).
    Hoisting them to before the first always block preserves semantics because
    module-level wires are in scope throughout the entire module.
    """
    first_always = re.search(r'\balways\s*@', sv)
    if not first_always:
        return sv

    split_pos = first_always.start()
    before = sv[:split_pos]
    after = sv[split_pos:]

    # Match lines whose first non-whitespace token is `wire` or `assign`
    _WIRE_RE = re.compile(r'^[ \t]*(?:wire|assign)\b[^\n]*\n?', re.MULTILINE)

    hoisted: list[str] = []

    def _extract(m: re.Match) -> str:
        hoisted.append(m.group(0).rstrip('\n') + '\n')
        return ''

    after_cleaned = _WIRE_RE.sub(_extract, after)

    if not hoisted:
        return sv

    return before + ''.join(hoisted) + '\n' + after_cleaned


def _sanitize_property_assertions(sv: str) -> str:
    """Rewrite `assert property(...)` into Yosys-compatible `assert(...)`.

    Yosys with the open-source backend rejects SVA property syntax. This
    sanitizer rewrites the common LLM-generated patterns:

      assert property (<expr>)                       → assert (<expr>)
      assert property (@(posedge clk) <expr>)        → assert (<expr>)
      assert property (@(...) disable iff (...) e)   → assert (e)

    Implication operators (`|->` / `|=>`) inside the body are left intact —
    a follow-up sanitizer or LLM repair iteration can handle those.
    """
    _START = re.compile(r"\bassert\s+property\s*\(")
    out: list[str] = []
    cursor = 0
    while True:
        m = _START.search(sv, cursor)
        if not m:
            out.append(sv[cursor:])
            break
        out.append(sv[cursor:m.start()])

        # Find the matching close paren starting at the '(' position
        open_paren = m.end() - 1
        depth = 0
        in_string = False
        end = -1
        j = open_paren
        while j < len(sv):
            c = sv[j]
            if c == '"':
                bs = 0
                k = j - 1
                while k >= 0 and sv[k] == "\\":
                    bs += 1
                    k -= 1
                if bs % 2 == 0:
                    in_string = not in_string
            elif not in_string:
                if c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                    if depth == 0:
                        end = j
                        break
            j += 1

        if end == -1:
            # Unbalanced — leave the rest of the string as-is
            out.append(sv[m.start():])
            break

        inner = sv[m.end():end]
        # Strip leading clocking event @(...)
        inner = re.sub(r"^\s*@\s*\([^)]*\)\s*", "", inner)
        # Strip leading `disable iff (...)`
        inner = re.sub(r"^\s*disable\s+iff\s*\([^)]*\)\s*", "", inner)
        out.append(f"assert ({inner.strip()})")
        cursor = end + 1

    return "".join(out)


def _sanitize_bind_ports(sv: str) -> str:
    """Force all spec module port declarations to `input`.

    In a bind module the spec only observes DUT signals — it never drives them.
    Any `output` or `inout` in the port list produces undriven-output elaboration
    errors and causes the solver to return UNKNOWN instead of proving assertions.

    The substitution is restricted to the port list (text before the first `);`)
    so that internal wire/reg declarations inside the module body are untouched.
    """
    close = sv.find(");")
    if close == -1:
        return sv
    port_section = sv[:close]
    # Replace output/inout (with optional whitespace) before logic/reg keywords
    port_section = re.sub(r'\b(?:output|inout)(\s+(?:logic|reg)\b)', r'input\1', port_section)
    # Also catch bare `output` / `inout` not followed by logic/reg (e.g. `output [N:0]`)
    port_section = re.sub(r'\b(?:output|inout)(\s)', r'input\1', port_section)
    return port_section + sv[close:]


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

    # Truncation repair: the response ran out before the top-level `}` closed.
    # Try to close the unclosed brackets (or truncate to last complete array
    # element and close from there). Surfacing partial assertions beats
    # returning nothing on every truncated response.
    repaired = _repair_truncated_json(raw, start)
    if repaired is not None:
        try:
            data = json.loads(repaired)
            log.warning(
                "Repaired truncated JSON from %s (recovered %d assertion entries)",
                stage,
                len(data.get("assertion_index", []) if isinstance(data, dict) else []),
            )
            return data
        except json.JSONDecodeError:
            try:
                data = json.loads(_escape_control_chars_in_strings(repaired))
                log.warning(
                    "Repaired truncated JSON from %s after control-char escape", stage,
                )
                return data
            except json.JSONDecodeError:
                pass

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


def _repair_truncated_json(raw: str, start: int) -> str | None:
    """Best-effort repair of JSON cut off mid-response by token limits.

    Two strategies, tried in order:
      1. If we ran out of input outside any string, append the matching
         closers (}/]) in reverse stack order.
      2. If we ran out inside a string, truncate back to the position right
         after the last `}` that completed an element inside an array, then
         close the remaining open brackets.

    Returns repaired JSON text, or None if the input wasn't truncated.
    """
    stack: list[str] = []
    in_string = False
    escape_next = False
    last_array_elem_end = -1  # position right after a `}` closed inside [...]

    for i in range(start, len(raw)):
        ch = raw[i]
        if escape_next:
            escape_next = False
            continue
        if in_string:
            if ch == "\\":
                escape_next = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            stack.append("{")
        elif ch == "[":
            stack.append("[")
        elif ch == "}":
            if stack and stack[-1] == "{":
                stack.pop()
                if stack and stack[-1] == "[":
                    last_array_elem_end = i + 1
        elif ch == "]":
            if stack and stack[-1] == "[":
                stack.pop()

    if not stack and not in_string:
        return None  # not actually truncated

    # Strategy 1: outside any string — just close what's open.
    if not in_string:
        closers = "".join("}" if c == "{" else "]" for c in reversed(stack))
        return raw[start:] + closers

    # Strategy 2: mid-string — truncate to last complete array element.
    if last_array_elem_end <= start:
        return None  # no safe cut point

    # Recompute the bracket stack at last_array_elem_end so we know what
    # still needs closing.
    s2: list[str] = []
    in_str2 = False
    esc2 = False
    for j in range(start, last_array_elem_end):
        c = raw[j]
        if esc2:
            esc2 = False
            continue
        if in_str2:
            if c == "\\":
                esc2 = True
            elif c == '"':
                in_str2 = False
            continue
        if c == '"':
            in_str2 = True
            continue
        if c == "{":
            s2.append("{")
        elif c == "[":
            s2.append("[")
        elif c == "}":
            if s2 and s2[-1] == "{":
                s2.pop()
        elif c == "]":
            if s2 and s2[-1] == "[":
                s2.pop()

    closers = "".join("}" if c == "{" else "]" for c in reversed(s2))
    return raw[start:last_array_elem_end] + closers


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
