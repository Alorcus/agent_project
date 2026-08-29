"""Shared test fixtures and settings helpers.

Two autouse fixtures keep the suite from depending on — or damaging — the
developer's own machine: the repo's `.env` must not steer what a test thinks
`Settings()` defaults to, and no test may open the real `./data/agentchat.db`.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from agentchat.config import ENV_PREFIX, Settings, build_registry
from agentchat.llm import transcript
from agentchat.llm.registry import ModelRegistry
from agentchat.storage.sqlite import SqliteStore

_REAL_DATA_DIR = (Path(__file__).parent.parent / "data").resolve()


@pytest.fixture(autouse=True)
def _scrubbed_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Strip `AGENTCHAT_*` so `Settings()` reflects the code's own defaults,
    not whatever the developer's `.env` happens to set (config.py loads it at
    import time), and point the sqlite store at a throwaway directory."""
    for key in list(os.environ):
        if key.startswith(ENV_PREFIX):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(f"{ENV_PREFIX}DATA_DIR", str(tmp_path / "data"))


@pytest.fixture(autouse=True)
def _guard_real_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail with the offending test's name if a `SqliteStore` is ever opened
    under the repo's real data directory, instead of silently writing to it."""
    original_init = SqliteStore.__init__

    def guarded_init(self: SqliteStore, path: Path, *args, **kwargs) -> None:
        if Path(path).resolve().is_relative_to(_REAL_DATA_DIR):
            raise AssertionError(
                f"refusing to open the real database at {path!r} — "
                "pass a tmp_path-based path"
            )
        original_init(self, path, *args, **kwargs)

    monkeypatch.setattr(SqliteStore, "__init__", guarded_init)


@pytest.fixture(autouse=True)
def _guard_real_transcript(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail with the offending test's name if the LLM transcript is ever
    opened under the repo's real data directory, mirroring
    `_guard_real_database` above. Belt to `transcript`'s own inert-by-default
    behaviour (KTD9), not a substitute for it."""
    original_attach = transcript.attach_jsonl_handler

    def guarded_attach(logger: logging.Logger, path: Path) -> Path:
        if Path(path).resolve().is_relative_to(_REAL_DATA_DIR):
            raise AssertionError(
                f"refusing to open the real transcript at {path!r} — "
                "pass a tmp_path-based log_dir"
            )
        return original_attach(logger, path)

    monkeypatch.setattr(transcript, "attach_jsonl_handler", guarded_attach)


@pytest.fixture(autouse=True)
def _reset_llm_transcript() -> Iterator[None]:
    """Undo whatever a test's `setup_llm_log` call left behind. The module
    holds enabled-state and a file handler at import scope; leaking either
    into the next test would silently start writing a transcript no one
    asked for, or point one at a path this test session has already torn
    down."""
    yield
    transcript._enabled = False
    for handler in list(transcript._log.handlers):
        if isinstance(handler, logging.FileHandler):
            transcript._log.removeHandler(handler)
            handler.close()


@pytest.fixture
def store(tmp_path: Path) -> SqliteStore:
    """A throwaway durable store — the default for tests that need a
    ChatService and do not care where it persists."""
    return SqliteStore(tmp_path / "chat.db")


def mock_settings(**overrides) -> Settings:
    """The stub backend — the default for tests that are about interface
    behaviour, not real inference.

    Defaults to near-zero mock timing so tests that just drain a stream to
    completion don't pay for realistic load/chunk delays. Tests that assert
    on *mid-generation* behaviour (still streaming, cancel, stop) need a real
    gap to land in — pass ``mock_chunk_delay=None, mock_load_delay=None``
    to opt back into ``mock.default_models()``'s realistic timing.

    Sub-agent consultation defaults to off — otherwise every turn in the
    existing suite would grow up to three silent LLM calls; tests that are
    about consultation pass ``subagents=True`` explicitly. Fact extraction
    defaults to off for the same reason — otherwise every turn would grow up
    to two silent LLM calls per window; tests that are about fact extraction
    pass ``extract_facts=True`` explicitly. Recall defaults to off for the
    same reason again — otherwise every turn would grow a gate call; tests
    that are about recall pass ``recall_facts=True`` explicitly.
    """
    overrides.setdefault("backend", "mock")
    overrides.setdefault("store", "sqlite")
    overrides.setdefault("mock_chunk_delay", 0.0)
    overrides.setdefault("mock_load_delay", 0.0)
    overrides.setdefault("subagents", False)
    overrides.setdefault("extract_facts", False)
    overrides.setdefault("recall_facts", False)
    return Settings(**overrides)


def fast_registry(**overrides) -> ModelRegistry:
    """A registry wired to the stub backend, for tests about orchestration
    (streaming, cancellation, residency) that must not depend on a GPU or on
    the cluster's checkpoints being mounted."""
    return build_registry(mock_settings(**overrides))
