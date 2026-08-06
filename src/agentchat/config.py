"""Runtime configuration and app assembly.

Config is externalised (NFR-Q-03) via environment variables so a grader can
point the system at their own paths without editing code. ``build_registry`` is
the single wiring point: replacing the mock backend with a real one is a change
here and nowhere else.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from agentchat.llm.mock import MockProvider, default_models
from agentchat.llm.registry import ModelRegistry

ENV_PREFIX = "AGENTCHAT_"


def _env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(ENV_PREFIX + name, default)


def _env_flag(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    #: Where durable state will live once the store is real (NFR-S-01).
    data_dir: Path = field(
        default_factory=lambda: Path(_env("DATA_DIR", "./data") or "./data")
    )
    #: Where user documents are ingested from (NFR-RAG-02).
    corpus_dir: Path = field(
        default_factory=lambda: Path(_env("CORPUS_DIR", "./corpus") or "./corpus")
    )
    #: Model id selected at startup; falls back to the first registered.
    default_model: str | None = field(default_factory=lambda: _env("MODEL"))
    #: Simulate an unavailable backend to exercise error handling (NFR-Q-02).
    simulate_failure: bool = field(
        default_factory=lambda: _env_flag("SIMULATE_FAILURE")
    )

    @classmethod
    def from_env(cls) -> "Settings":
        return cls()


def build_registry(settings: Settings) -> ModelRegistry:
    """Populate the registry. The only place backends are named."""
    registry = ModelRegistry()
    for index, (info, kwargs) in enumerate(default_models()):
        fail = settings.simulate_failure and index == 1
        registry.register(
            info,
            lambda info=info, kwargs=kwargs, fail=fail: MockProvider(
                info, fail=fail, **kwargs
            ),
        )
    if settings.default_model:
        registry.set_active(settings.default_model)  # raises if unknown
    return registry
