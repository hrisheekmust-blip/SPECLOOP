"""LLM decomposition: natural language request → CompositionPlan."""
from __future__ import annotations

import logging
from pathlib import Path

from jinja2 import DictLoader, Environment, FileSystemLoader

from specloop.gen.client import LLMClient
from specloop.gen.pipeline import _parse_json, _SEP, _WRAPPER_SUFFIX
from specloop.compose.schema import CompositionPlan

log = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).parent / "prompts"


class Decomposer:
    def __init__(self, client: LLMClient) -> None:
        self._client = client
        self._env = Environment(
            loader=FileSystemLoader(str(_PROMPTS_DIR)),
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
        )

    def decompose(self, request: str) -> CompositionPlan:
        src = self._env.loader.get_source(self._env, "decompose.j2")[0]
        patched_env = Environment(
            loader=DictLoader({"__tpl__": src + _WRAPPER_SUFFIX}),
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
        )
        rendered = patched_env.get_template("__tpl__").render(request=request)
        parts = rendered.split(_SEP)
        system = parts[1].strip() if len(parts) > 1 else ""
        user = parts[2].strip() if len(parts) > 2 else ""

        raw = self._client.generate(system, user)
        data = _parse_json(raw, "decompose")

        try:
            return CompositionPlan.model_validate(data)
        except Exception as exc:
            log.warning("Decomposition parse error: %s — retrying once", exc)
            raw2 = self._client.generate(system, user)
            data2 = _parse_json(raw2, "decompose_retry")
            try:
                return CompositionPlan.model_validate(data2)
            except Exception as exc2:
                raise ValueError(
                    f"Decomposition failed after retry: {exc2}\n"
                    f"Raw LLM output:\n{raw2[:800]}"
                ) from exc2
