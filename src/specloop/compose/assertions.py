"""LLM-based composition assertion generator."""
from __future__ import annotations

import logging
import re
from pathlib import Path

from jinja2 import DictLoader, Environment, FileSystemLoader
from pydantic import BaseModel

from specloop.gen.client import LLMClient
from specloop.gen.pipeline import _parse_json, _sanitize_sv, _SEP, _WRAPPER_SUFFIX
from specloop.gen.schema import BindResult
from specloop.training.schema import AssertionEntry
from specloop.compose.schema import CompositionPlan, SelectedModule

log = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).parent / "prompts"

# Temporal SVA builtins: an assertion using any of these depends on the timing
# context of the module it was proven in, so it does NOT transfer cleanly into a
# composition wrapper (where the clocking and reset sequencing differ).
_TEMPORAL_RE = re.compile(r"\$(past|rose|fell|stable|changed)\b")

# Cap inherited assertions per component so the prompt stays focused.
_MAX_INHERITED_PER_MODULE = 12


class ProvenAssertion(BaseModel):
    """One assertion lifted from a component's proven bind module."""
    label: str
    guards: list[str] = []   # enclosing `if (...)` conditions, outermost first
    expr: str                # the asserted boolean expression
    temporal: bool           # True → depends on timing, do not inherit


class InheritedProperties(BaseModel):
    """The proven properties of one selected component, partitioned for the prompt."""
    sub_function_id: str
    module: str
    inheritable: list[ProvenAssertion] = []   # transfer cleanly (reset/safety/...)
    temporal_skipped: list[str] = []          # labels skipped because timing-dependent


def _has_temporal(text: str) -> bool:
    return bool(_TEMPORAL_RE.search(text))


def _strip_comments(sv: str) -> str:
    sv = re.sub(r"//[^\n]*", "", sv)
    sv = re.sub(r"/\*.*?\*/", "", sv, flags=re.DOTALL)
    return sv


def _balanced_close(text: str, open_idx: int) -> int:
    """Index of the ')' matching the '(' at open_idx, or -1 if unbalanced."""
    depth = 0
    for i in range(open_idx, len(text)):
        c = text[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


_TEMPORAL_WIRE_RE = re.compile(r"\bwire\b\s+(\w+)\s*=\s*(.*?);", re.DOTALL)
_WALK_RE = re.compile(r"\b(if|begin|end)\b|(ap_\w+)\s*:\s*assert\b")
_IDENT_RE = re.compile(r"\b\w+\b")


def parse_proven_assertions(bind_sv: str) -> list[ProvenAssertion]:
    """Extract proven assertions from a bind module with guard awareness.

    Walks the bind text tracking the stack of enclosing `if (...)` guards (via
    begin/end nesting) so each assertion carries the conditions under which it was
    proven. Classifies an assertion as temporal when its expression, any of its
    guards, or any decode wire it transitively references uses a temporal builtin
    ($past/$rose/$fell/…) — those don't transfer to a composition's timing context.
    """
    text = _strip_comments(bind_sv)

    # Decode wires, with a fixpoint to mark wires that reference temporal wires.
    wires = {n: e for n, e in _TEMPORAL_WIRE_RE.findall(text)}
    temporal_wires = {n for n, e in wires.items() if _has_temporal(e)}
    changed = True
    while changed:
        changed = False
        for n, e in wires.items():
            if n in temporal_wires:
                continue
            if set(_IDENT_RE.findall(e)) & temporal_wires:
                temporal_wires.add(n)
                changed = True

    def is_temporal(guards: list[str], expr: str) -> bool:
        blob = expr + " " + " ".join(guards)
        if _has_temporal(blob):
            return True
        return bool(set(_IDENT_RE.findall(blob)) & temporal_wires)

    out: list[ProvenAssertion] = []
    stack: list[str | None] = []
    pending: str | None = None
    i = 0
    while True:
        m = _WALK_RE.search(text, i)
        if not m:
            break
        kw = m.group(1)
        if kw == "if":
            paren = text.find("(", m.end())
            if paren == -1:
                i = m.end()
                continue
            close = _balanced_close(text, paren)
            if close == -1:
                break
            pending = text[paren + 1 : close].strip()
            i = close + 1
        elif kw == "begin":
            stack.append(pending)
            pending = None
            i = m.end()
        elif kw == "end":
            if stack:
                stack.pop()
            i = m.end()
        else:  # assert
            label = m.group(2)
            paren = text.find("(", m.end())
            if paren == -1:
                i = m.end()
                continue
            close = _balanced_close(text, paren)
            if close == -1:
                break
            expr = text[paren + 1 : close].strip()
            guards = [g for g in stack if g]
            if pending:  # bare `if (cond) ap: assert(...)` with no begin
                guards = guards + [pending]
                pending = None
            out.append(ProvenAssertion(
                label=label, guards=guards, expr=expr,
                temporal=is_temporal(guards, expr),
            ))
            i = close + 1
    return out


def load_inherited_properties(
    selected: list[SelectedModule],
    work_dir: Path,
) -> list[InheritedProperties]:
    """Load each component's proven bind module from work_dir and partition its
    assertions into inheritable (transfer cleanly) vs temporal (skipped).

    Best-effort: a component with no bind file, or one that fails to parse,
    contributes no inherited properties — the composition falls back to
    from-scratch generation for that module.
    """
    props: list[InheritedProperties] = []
    for sm in selected:
        bind_path = work_dir / f"{sm.ir.module}.bind.sv"
        if not bind_path.exists():
            continue
        try:
            asserts = parse_proven_assertions(
                bind_path.read_text(encoding="utf-8", errors="replace")
            )
        except Exception as exc:
            log.debug("Could not parse proven bind for '%s': %s", sm.ir.module, exc)
            continue
        inheritable = [a for a in asserts if not a.temporal][:_MAX_INHERITED_PER_MODULE]
        temporal = [a.label for a in asserts if a.temporal]
        if inheritable or temporal:
            props.append(InheritedProperties(
                sub_function_id=sm.sub_function_id,
                module=sm.ir.module,
                inheritable=inheritable,
                temporal_skipped=temporal,
            ))
    return props


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
        work_dir: Path | None = None,
    ) -> BindResult:
        # Inherit each component's formally-proven properties instead of
        # regenerating them from scratch. The LLM re-emits these (rewritten to
        # hierarchical instance refs) and spends its reasoning on the interaction
        # layer. Best-effort: no work_dir / no bind files → empty list → the
        # existing from-scratch behavior.
        inherited: list[InheritedProperties] = []
        if work_dir is not None:
            inherited = load_inherited_properties(selected, work_dir)
            if inherited:
                n = sum(len(p.inheritable) for p in inherited)
                log.info(
                    "Inheriting %d proven propert%s from %d component(s); "
                    "LLM focuses on interaction assertions",
                    n, "y" if n == 1 else "ies", len(inherited),
                )

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
            inherited=inherited,
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
