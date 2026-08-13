"""Runtime configuration and app assembly.

Config is externalised via environment variables, optionally loaded from a
``.env`` file. ``build_registry`` and ``build_store`` are the single wiring
points: which backend and which store the app uses are decided here and
nowhere else.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from agentchat.core.enrichment import MemoryEnricher
from agentchat.core.errors import AgentChatError
from agentchat.core.extraction import ExtractionService
from agentchat.llm.base import ModelInfo
from agentchat.llm.local import DEFAULT_MODEL_ROOT, TransformersProvider
from agentchat.llm.local import default_models as local_models
from agentchat.llm.mock import MockProvider
from agentchat.llm.mock import default_models as mock_models
from agentchat.llm.registry import ModelRegistry
from agentchat.storage.base import ConversationStore
from agentchat.storage.sqlite import SqliteStore

ENV_PREFIX = "AGENTCHAT_"

# Walks up from the cwd for a `.env` and merges it into os.environ. Real
# environment variables always win — load_dotenv() never overwrites a key
# that's already set.
load_dotenv(find_dotenv(usecwd=True))

#: ``local`` runs the real checkpoints; ``mock`` is the stub backend, kept so the
#: UI can be developed and tested on a machine with no GPU.
BACKENDS = ("local", "mock")

#: ``sqlite`` is the only store. Kept as a tuple (not deleted outright) so a
#: stale ``AGENTCHAT_STORE=memory`` in a ``.env`` fails loudly via
#: ``_require_choice`` rather than being silently ignored.
STORES = ("sqlite",)


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
    #: On by default — a feature that has to be switched on is not
    #: demonstrable. Off switches extraction entirely, with no flag threaded
    #: through call sites (`ChatService.summarise` just checks for `None`).
    extract_summaries: bool = field(
        default_factory=lambda: _env_flag("EXTRACT_SUMMARIES", True)
    )
    #: Seconds `action_quit` waits for extraction before exiting anyway.
    extraction_timeout: float = field(
        default_factory=lambda: (
            30.0 if (v := _env_float("EXTRACTION_TIMEOUT")) is None else v
        )
    )
    #: On by default, same reasoning as `extract_summaries`. Off switches
    #: enrichment entirely, with no flag threaded through call sites
    #: (`ChatService.stream_reply` just checks for `None`).
    enrich_messages: bool = field(
        default_factory=lambda: _env_flag("ENRICH_MESSAGES", True)
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
    return SqliteStore(settings.data_dir / "agentchat.db")


def build_extractor(
    settings: Settings, registry: ModelRegistry
) -> ExtractionService | None:
    """Assemble the extraction service. The only place extraction is switched
    on; `None` when `extract_summaries` is off."""
    if not settings.extract_summaries:
        return None
    return ExtractionService(registry)


def build_enricher(settings: Settings, store: ConversationStore) -> MemoryEnricher | None:
    """Assemble the enrichment service. The only place enrichment is switched
    on; `None` when `enrich_messages` is off. Takes `store` rather than
    building its own, so the caller can hand it the same instance `ChatService`
    uses."""
    if not settings.enrich_messages:
        return None
    return MemoryEnricher(store)


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
