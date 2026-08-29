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

from agentchat.core.delegation import DelegationService
from agentchat.core.errors import AgentChatError
from agentchat.core.facts import FactExtractor
from agentchat.core.retrieval import AdaptiveRetriever, FactIndex
from agentchat.llm.base import ModelInfo
from agentchat.llm.embedding import (
    DEFAULT_EMBED_MODEL_ID,
    Embedder,
    EmbedderInfo,
    HashingEmbedder,
    LocalEmbedder,
)
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


def _p(raw: str | None) -> Path | None:
    return Path(raw) if raw else None


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
    #: Seconds `action_quit` waits for the fact flush before exiting anyway.
    extraction_timeout: float = field(
        default_factory=lambda: (
            30.0 if (v := _env_float("EXTRACTION_TIMEOUT")) is None else v
        )
    )
    #: On by default — a feature that has to be switched on is not
    #: demonstrable. Off switches fact extraction entirely, with no flag
    #: threaded through call sites (`ChatService.extract_facts` just checks
    #: for `None`) — and off means no LLM calls at all.
    extract_facts: bool = field(default_factory=lambda: _env_flag("EXTRACT_FACTS", True))
    #: On by default, same reasoning. Off switches sub-agent consultation
    #: entirely, with no flag threaded through call sites
    #: (`ChatService.stream_reply` just checks for `None`).
    subagents: bool = field(default_factory=lambda: _env_flag("SUBAGENTS", True))
    #: Seconds each consultation phase (routing+task, then the specialist's
    #: answer) is allowed. Materially larger than `extraction_timeout`'s 30 —
    #: a consulted turn is three generations, not two.
    subagent_timeout: float = field(
        default_factory=lambda: (
            60.0 if (v := _env_float("SUBAGENT_TIMEOUT")) is None else v
        )
    )
    #: On by default, same reasoning as `extract_facts`. Off switches adaptive
    #: recall entirely — no embedder load, no LLM calls, no store reads (R16).
    recall_facts: bool = field(default_factory=lambda: _env_flag("RECALL_FACTS", True))
    #: The embedder id a stored vector is tagged with; a vector from another id
    #: is re-embedded, not searched (R4).
    embed_model: str = field(
        default_factory=lambda: _env("EMBED_MODEL", DEFAULT_EMBED_MODEL_ID)
        or DEFAULT_EMBED_MODEL_ID
    )
    #: Where the embedding checkpoint lives. `None` → under `model_root`.
    embed_model_path: Path | None = field(
        default_factory=lambda: _p(_env("EMBED_MODEL_PATH"))
    )
    recall_rounds: int = field(
        default_factory=lambda: 3 if (v := _env_int("RECALL_ROUNDS")) is None else v
    )
    recall_rewrites: int = field(
        default_factory=lambda: 3 if (v := _env_int("RECALL_REWRITES")) is None else v
    )
    recall_hits: int = field(
        default_factory=lambda: 10 if (v := _env_int("RECALL_HITS")) is None else v
    )
    recall_seeds: int = field(
        default_factory=lambda: 2 if (v := _env_int("RECALL_SEEDS")) is None else v
    )
    recall_min_score: float = field(
        default_factory=lambda: (
            0.25 if (v := _env_float("RECALL_MIN_SCORE")) is None else v
        )
    )
    recall_digest_facts: int = field(
        default_factory=lambda: (
            12 if (v := _env_int("RECALL_DIGEST_FACTS")) is None else v
        )
    )
    recall_timeout: float = field(
        default_factory=lambda: (
            120.0 if (v := _env_float("RECALL_TIMEOUT")) is None else v
        )
    )
    #: On by default: an instrument that has to be switched on is an
    #: instrument nobody has running when the interesting turn happens.
    log_llm_io: bool = field(default_factory=lambda: _env_flag("LOG_LLM_IO", True))
    #: Where both log files land. Defaults under `data_dir` so one directory
    #: holds the database and the transcripts of the calls that filled it.
    log_dir: Path | None = field(default_factory=lambda: _p(_env("LOG_DIR")))

    @classmethod
    def from_env(cls) -> "Settings":
        return cls()

    @property
    def resolved_log_dir(self) -> Path:
        return self.log_dir or self.data_dir / "logs"

    @property
    def resolved_embed_model_path(self) -> Path:
        return self.embed_model_path or (
            self.model_root / "sentence-transformers" / self.embed_model
        )


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


def build_fact_extractor(
    settings: Settings, registry: ModelRegistry
) -> FactExtractor | None:
    """Assemble the fact extractor. The only place fact extraction is
    switched on; `None` when `extract_facts` is off."""
    if not settings.extract_facts:
        return None
    return FactExtractor(registry)


def build_embedder(settings: Settings) -> Embedder | None:
    """`None` when recall is off. `HashingEmbedder` under the mock backend —
    the same switch `build_registry` makes, for the same reason: no test and
    no laptop should need embedding weights on disk."""
    if not settings.recall_facts:
        return None
    if settings.backend == "mock":
        return HashingEmbedder()
    return LocalEmbedder(
        EmbedderInfo(id=settings.embed_model, name=settings.embed_model),
        path=settings.resolved_embed_model_path,
    )


def build_retriever(
    settings: Settings,
    registry: ModelRegistry,
    store: ConversationStore,
    embedder: Embedder | None,
) -> AdaptiveRetriever | None:
    """Assemble the adaptive retriever. `None` when recall is off or no
    embedder was built. `recall_seeds` is clamped to `MAX_SEEDS` inside
    `AdaptiveRetriever`, not trusted from the environment."""
    if not settings.recall_facts or embedder is None:
        return None
    return AdaptiveRetriever(
        registry,
        FactIndex(store, embedder),
        rounds=settings.recall_rounds,
        rewrites=settings.recall_rewrites,
        hits=settings.recall_hits,
        seeds=settings.recall_seeds,
        min_score=settings.recall_min_score,
        digest_facts=settings.recall_digest_facts,
        timeout=settings.recall_timeout,
    )


def build_delegator(
    settings: Settings, registry: ModelRegistry
) -> DelegationService | None:
    """Assemble the delegation service. The only place sub-agent consultation
    is switched on; `None` when `subagents` is off."""
    if not settings.subagents:
        return None
    return DelegationService(registry, timeout=settings.subagent_timeout)


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
