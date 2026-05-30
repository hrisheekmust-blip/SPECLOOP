from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar, Literal, Tuple, Type

from pydantic import Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)


class SpecloopConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SPECLOOP_",
        toml_file="specloop.toml",
        env_nested_delimiter="__",
    )

    work_dir: Path = Field(default=Path("work"))
    vendor_blackboxes_dir: Path = Field(default=Path("vendor_blackboxes"))
    max_file_size_kb: int = Field(default=2048)

    # Cross-file RTL dependencies (industrial designs with packages / header macros)
    rtl_include_dirs: list[Path] = Field(default_factory=list)
    rtl_define_files: list[Path] = Field(default_factory=list)
    rtl_package_files: list[Path] = Field(default_factory=list)

    # Training data collection
    training_log: Path = Field(default=Path("work/training_data.jsonl"))
    training_min_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    training_enabled: bool = Field(default=True)

    # LLM backend — switch by changing one line in specloop.toml
    llm_backend: Literal["anthropic", "vllm", "ollama"] = Field(default="anthropic")
    llm_model: str = Field(default="claude-sonnet-4-5")
    llm_api_key: str = Field(default="")          # overrides ANTHROPIC_API_KEY if set
    llm_base_url: str = Field(default="http://localhost:8000/v1")   # vLLM endpoint
    llm_ollama_url: str = Field(default="http://localhost:11434")    # Ollama endpoint
    llm_max_tokens: int = Field(default=4096)
    llm_temperature: float = Field(default=0.2, ge=0.0, le=2.0)

    # Formal verification
    formal_backend: Literal["sby", "synlig"] = Field(default="sby")
    formal_mode: Literal["bmc", "prove", "cover"] = Field(default="prove")
    formal_depth: int = Field(default=20)
    formal_timeout: int = Field(default=300)      # seconds per SBY run
    formal_solver: str = Field(default="")        # "" = SBY default; "bitwuzla", "yices", etc.
    formal_repair_iterations: int = Field(default=3)
    formal_debug: bool = Field(default=False)    # print raw SBY stdout/stderr
    synlig_path: str = Field(default="~/projects/specloop2/synlig/bin/synlig")

    # Semantic search
    qdrant_url: str = Field(default="http://localhost:6333")
    qdrant_collection: str = Field(default="specloop_modules")
    embed_model: str = Field(default="BAAI/bge-large-en-v1.5")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: Type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> Tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            TomlConfigSettingsSource(settings_cls),
        )
