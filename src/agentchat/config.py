"""Runtime configuration and app assembly.

Config is externalised via environment variables, optionally loaded from a
``.env`` file. ``build_registry`` is the single wiring point: which backend
the app talks to is decided here and nowhere else.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from agentchat.core.errors import AgentChatError
from agentchat.llm.base import ModelInfo
from agentchat.llm.local import DEFAULT_MODEL_ROOT, TransformersProvider
from agentchat.llm.local import default_models as local_models
from agentchat.llm.mock import MockProvider
from agentchat.llm.mock import default_models as mock_models
from agentchat.llm.registry import ModelRegistry

ENV_PREFIX = "AGENTCHAT_"

# Walks up from the cwd for a `.env` and merges it into os.environ. Real
# environment variables always win — load_dotenv() never overwrites a key
# that's already set.
load_dotenv(find_dotenv(usecwd=True))

#: ``local`` runs the real checkpoints; ``mock`` is the stub backend, kept so the
#: UI can be developed and tested on a machine with no GPU.
BACKENDS = ("local", "mock")


class ConfigurationError(AgentChatError):
    """The environment asks for something that cannot be assembled."""


def _env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(ENV_PREFIX + name, default)


def _env_flag(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str) -> int | None:
    raw = _env(name)
    if raw is None or not raw.strip():
        return None
    try:
        return int(raw)
    except ValueError:
        raise ConfigurationError(
            f"{ENV_PREFIX}{name} must be an integer, got {raw!r}"
        ) from None


@dataclass
class Settings:
    data_dir: Path = field(
        default_factory=lambda: Path(_env("DATA_DIR", "./data") or "./data")
    )
    corpus_dir: Path = field(
        default_factory=lambda: Path(_env("CORPUS_DIR", "./corpus") or "./corpus")
    )
    #: Which backend to assemble — see ``BACKENDS``.
    backend: str = field(
        default_factory=lambda: (_env("BACKEND", "local") or "local").strip().lower()
    )
    #: Root of the checkpoint tree the local backend loads from.
    model_root: Path = field(
        default_factory=lambda: Path(
            _env("MODEL_ROOT", str(DEFAULT_MODEL_ROOT)) or str(DEFAULT_MODEL_ROOT)
        )
    )
    #: Model id selected at startup; falls back to the first registered.
    default_model: str | None = field(default_factory=lambda: _env("MODEL"))
    #: Cap every model's context window — the escape hatch for a smaller GPU.
    max_context: int | None = field(default_factory=lambda: _env_int("MAX_CONTEXT"))
    #: Simulate an unavailable backend, to exercise error handling.
    simulate_failure: bool = field(
        default_factory=lambda: _env_flag("SIMULATE_FAILURE")
    )

    @classmethod
    def from_env(cls) -> "Settings":
        return cls()


def build_registry(settings: Settings) -> ModelRegistry:
    """Populate the registry. The only place backends are named."""
    if settings.backend not in BACKENDS:
        raise ConfigurationError(
            f"Unknown {ENV_PREFIX}BACKEND {settings.backend!r} — "
            f"expected one of {', '.join(BACKENDS)}"
        )

    registry = ModelRegistry()
    if settings.backend == "mock":
        _register_mock(registry, settings)
    else:
        _register_local(registry, settings)

    if settings.default_model:
        registry.set_active(settings.default_model)  # raises if unknown
    return registry


def _register_local(registry: ModelRegistry, settings: Settings) -> None:
    for index, (info, kwargs) in enumerate(local_models(settings.model_root)):
        fail = settings.simulate_failure and index == 1
        # Cap before the factory closes over it: the chat service sizes the
        # context off ``provider.info``, not off the registry's copy.
        info = _capped(info, settings.max_context)
        registry.register(
            info,
            lambda info=info, kwargs=kwargs, fail=fail: TransformersProvider(
                info, fail=fail, **kwargs
            ),
        )


def _register_mock(registry: ModelRegistry, settings: Settings) -> None:
    for index, (info, kwargs) in enumerate(mock_models()):
        fail = settings.simulate_failure and index == 1
        info = _capped(info, settings.max_context)
        registry.register(
            info,
            lambda info=info, kwargs=kwargs, fail=fail: MockProvider(
                info, fail=fail, **kwargs
            ),
        )


def _capped(info: ModelInfo, max_context: int | None) -> ModelInfo:
    if max_context is None or info.context_window <= max_context:
        return info
    return replace(info, context_window=max_context)
