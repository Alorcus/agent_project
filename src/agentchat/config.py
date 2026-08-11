"""Runtime configuration and app assembly.

Config is externalised via environment variables, optionally loaded from a
``.env`` file. ``build_registry`` and ``build_store`` are the single wiring
points: which backend and which store the app uses are decided here and
nowhere else.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from agentchat.core.errors import AgentChatError
from agentchat.core.memory.store import MemoryStore
from agentchat.core.memory.tuning import Tuning
from agentchat.core.memory.types import EmbeddingProvider
from agentchat.llm.base import ModelInfo
from agentchat.llm.embed import LocalEncoder, default_encoder_path
from agentchat.llm.local import DEFAULT_MODEL_ROOT, TransformersProvider
from agentchat.llm.local import default_models as local_models
from agentchat.llm.mock import MockProvider
from agentchat.llm.mock import default_models as mock_models
from agentchat.llm.registry import ModelRegistry
from agentchat.storage.base import ConversationStore, InMemoryStore
from agentchat.storage.memory import SqliteMemoryStore
from agentchat.storage.sqlite import SqliteStore

ENV_PREFIX = "AGENTCHAT_"

_LOG = logging.getLogger(__name__)

# Walks up from the cwd for a `.env` and merges it into os.environ. Real
# environment variables always win — load_dotenv() never overwrites a key
# that's already set.
load_dotenv(find_dotenv(usecwd=True))

#: ``local`` runs the real checkpoints; ``mock`` is the stub backend, kept so the
#: UI can be developed and tested on a machine with no GPU.
BACKENDS = ("local", "mock")

#: ``sqlite`` persists across restarts; ``memory`` is the non-durable stub,
#: kept for tests and for working without touching disk.
STORES = ("sqlite", "memory")


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


def _env_float(name: str) -> float | None:
    raw = _env(name)
    if raw is None or not raw.strip():
        return None
    try:
        return float(raw)
    except ValueError:
        raise ConfigurationError(
            f"{ENV_PREFIX}{name} must be a number, got {raw!r}"
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
    #: Which store to assemble — see ``STORES``.
    store: str = field(
        default_factory=lambda: (_env("STORE", "sqlite") or "sqlite").strip().lower()
    )
    #: Root of the checkpoint tree the local backend loads from.
    model_root: Path = field(
        default_factory=lambda: Path(
            _env("MODEL_ROOT", str(DEFAULT_MODEL_ROOT)) or str(DEFAULT_MODEL_ROOT)
        )
    )
    #: Where the pinned sentence encoder's weights live. Defaults under
    #: ``model_root``, like every other checkpoint.
    encoder_path: Path = field(
        default_factory=lambda: Path(
            _env("ENCODER_PATH")
            or str(
                default_encoder_path(
                    Path(_env("MODEL_ROOT", str(DEFAULT_MODEL_ROOT)) or str(DEFAULT_MODEL_ROOT))
                )
            )
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
    #: Override the mock backend's per-chunk / load delay (only meaningful
    #: when ``backend == "mock"``). ``None`` keeps ``mock.default_models()``'s
    #: realistic per-model timing; tests use this to run near-instantly.
    mock_chunk_delay: float | None = field(
        default_factory=lambda: _env_float("MOCK_CHUNK_DELAY")
    )
    mock_load_delay: float | None = field(
        default_factory=lambda: _env_float("MOCK_LOAD_DELAY")
    )

    @classmethod
    def from_env(cls) -> "Settings":
        return cls()


def _require_choice(var_name: str, value: str, choices: tuple[str, ...]) -> None:
    if value not in choices:
        raise ConfigurationError(
            f"Unknown {ENV_PREFIX}{var_name} {value!r} — "
            f"expected one of {', '.join(choices)}"
        )


def build_registry(settings: Settings) -> ModelRegistry:
    """Populate the registry. The only place backends are named."""
    _require_choice("BACKEND", settings.backend, BACKENDS)

    registry = ModelRegistry()
    if settings.backend == "mock":
        _register_mock(registry, settings)
    else:
        _register_local(registry, settings)

    if settings.default_model:
        registry.set_active(settings.default_model)  # raises if unknown
    return registry


def build_store(settings: Settings) -> ConversationStore:
    """Assemble the conversation store. The only place stores are named."""
    _require_choice("STORE", settings.store, STORES)
    if settings.store == "memory":
        return InMemoryStore()
    return SqliteStore(settings.data_dir / "agentchat.db")


def build_memory_store(
    settings: Settings, encoder: EmbeddingProvider | None = None
) -> MemoryStore | None:
    """`None` when the conversation store is non-durable — memory lives in the
    same file, and there is no in-memory implementation of `apply()`."""
    _require_choice("STORE", settings.store, STORES)
    if settings.store == "memory":
        return None
    return SqliteMemoryStore(settings.data_dir / "agentchat.db", encoder=encoder)


def build_encoder(settings: Settings) -> EmbeddingProvider | None:
    """The single wiring point for the sentence encoder. `None` — with a
    `WARNING` naming the path it looked in — when the weights are absent or
    fail to load, because a missing encoder must degrade recall, not stop the
    app."""
    tuning = Tuning.from_env()
    path = settings.encoder_path
    if not path.is_dir():
        _LOG.warning("no encoder weights at %s — recall will run without a floor", path)
        return None
    encoder = LocalEncoder(tuning.encoder_model, path=path)
    try:
        encoder.load()
    except Exception as error:  # noqa: BLE001 — a bad checkpoint degrades recall, not the app
        _LOG.warning("failed to load the encoder from %s: %s", path, error)
        return None
    return encoder


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
    models = mock_models(
        chunk_delay=settings.mock_chunk_delay, load_delay=settings.mock_load_delay
    )
    for index, (info, kwargs) in enumerate(models):
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
