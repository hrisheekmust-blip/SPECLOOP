"""LLM client abstraction with plug-and-play backends.

Backend is selected by specloop.toml `[llm] backend = "anthropic"|"vllm"|"ollama"`.
Zero code changes required to switch models.
"""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod

log = logging.getLogger(__name__)


class LLMClient(ABC):
    """Single-method interface for all LLM backends."""

    @abstractmethod
    def generate(self, system_prompt: str, user_prompt: str) -> str:
        """Send system + user prompt; return the model's text response."""

    @property
    @abstractmethod
    def model_id(self) -> str:
        """Identifier string for logging and training records."""


# ---------------------------------------------------------------------------
# Anthropic backend
# ---------------------------------------------------------------------------

class AnthropicClient(LLMClient):
    """Claude via the official Anthropic SDK with system-prompt caching."""

    def __init__(
        self,
        model: str = "claude-sonnet-4-5",
        api_key: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.2,
    ) -> None:
        import anthropic as _anthropic
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature
        kwargs: dict = {"max_retries": 0}  # we handle retries ourselves with 60s wait
        if api_key:
            kwargs["api_key"] = api_key
        self._client = _anthropic.Anthropic(**kwargs)

    @property
    def model_id(self) -> str:
        return self._model

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        import anthropic as _anthropic
        # RateLimitError = 429; InternalServerError covers 529 (Overloaded)
        _RETRY_ERRORS = (_anthropic.RateLimitError, _anthropic.InternalServerError)
        _MAX_RETRIES = 3
        _RETRY_WAIT = 60

        for attempt in range(_MAX_RETRIES + 1):
            try:
                response = self._client.messages.create(
                    model=self._model,
                    max_tokens=self._max_tokens,
                    temperature=self._temperature,
                    system=[
                        {
                            "type": "text",
                            "text": system_prompt,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    messages=[{"role": "user", "content": user_prompt}],
                )
                cache_hits = getattr(response.usage, "cache_read_input_tokens", 0)
                if cache_hits:
                    log.debug("Anthropic prompt cache hit: %d tokens saved", cache_hits)
                return next(b.text for b in response.content if b.type == "text")
            except _RETRY_ERRORS as exc:
                if attempt == _MAX_RETRIES:
                    raise
                log.warning(
                    "%s (attempt %d/%d) — waiting %ds before retry",
                    type(exc).__name__, attempt + 1, _MAX_RETRIES, _RETRY_WAIT,
                )
                time.sleep(_RETRY_WAIT)


# ---------------------------------------------------------------------------
# vLLM backend (OpenAI-compatible)
# ---------------------------------------------------------------------------

class VLLMClient(LLMClient):
    """Local vLLM endpoint via the OpenAI-compatible API."""

    def __init__(
        self,
        base_url: str = "http://localhost:8000/v1",
        model: str = "mistralai/Mistral-7B-Instruct-v0.3",
        api_key: str = "EMPTY",
        max_tokens: int = 4096,
        temperature: float = 0.2,
    ) -> None:
        from openai import OpenAI as _OpenAI
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature
        # Normalize to "https://host/v1/" — the OpenAI SDK requires /v1 in the path
        # and a trailing slash so URL joining doesn't drop the path segment.
        _url = base_url.rstrip("/")
        if not _url.endswith("/v1"):
            _url += "/v1"
        self._client = _OpenAI(api_key=api_key, base_url=_url + "/")

    @property
    def model_id(self) -> str:
        return self._model

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        print(f"[VLLMClient] base_url={self._client.base_url!r}", flush=True)
        response = self._client.chat.completions.create(
            model=self._model,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        return response.choices[0].message.content or ""


# ---------------------------------------------------------------------------
# Ollama backend (OpenAI-compatible endpoint)
# ---------------------------------------------------------------------------

class OllamaClient(LLMClient):
    """Local Ollama via its OpenAI-compatible /v1 endpoint."""

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "mistral",
        max_tokens: int = 4096,
        temperature: float = 0.2,
    ) -> None:
        from openai import OpenAI as _OpenAI
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature
        v1_url = base_url.rstrip("/") + "/v1"
        self._client = _OpenAI(api_key="ollama", base_url=v1_url)

    @property
    def model_id(self) -> str:
        return f"ollama/{self._model}"

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        response = self._client.chat.completions.create(
            model=self._model,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        return response.choices[0].message.content or ""


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def make_client(config) -> LLMClient:
    """Instantiate the backend specified in SpecloopConfig."""
    backend = config.llm_backend
    if backend == "anthropic":
        return AnthropicClient(
            model=config.llm_model,
            api_key=config.llm_api_key or None,
            max_tokens=config.llm_max_tokens,
            temperature=config.llm_temperature,
        )
    if backend == "vllm":
        return VLLMClient(
            base_url=config.llm_base_url,
            model=config.llm_model,
            api_key=config.llm_api_key or "EMPTY",
            max_tokens=config.llm_max_tokens,
            temperature=config.llm_temperature,
        )
    if backend == "ollama":
        return OllamaClient(
            base_url=config.llm_ollama_url,
            model=config.llm_model,
            max_tokens=config.llm_max_tokens,
            temperature=config.llm_temperature,
        )
    raise ValueError(f"Unknown LLM backend: {backend!r}. Choose anthropic, vllm, or ollama.")
