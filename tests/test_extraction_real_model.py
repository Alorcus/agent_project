"""The cluster check for stage 2's wire format: real weights, one run each.

Everything else about extraction is scripted (skeleton rule 7), which proves
the pipeline and proves nothing about whether a 3.8B or a 14B model can
actually produce the JSON `parse_claims` expects. That question needs the
weights, so it lives here, behind `AGENTCHAT_TEST_REAL_MODEL=1`:

    AGENTCHAT_TEST_REAL_MODEL=1 uv run pytest -k real_model

These assert **mechanics only** — that parseable claims reached `apply()` and
that a reasoning trace was stored when thinking was on. Never output quality:
which claims a model finds is not this suite's business. A failure here means
the format is wrong for that model, not that the model is bad, and the raw
reply is in the captured log to say which.

The flag is read at import, like `test_local.py`'s: `conftest` scrubs every
`AGENTCHAT_*` variable per test, so reading it in a body would always see it
unset.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

import pytest

from factories import GraphBuilder, make_group, write

REAL_MODEL_TESTS = os.environ.get("AGENTCHAT_TEST_REAL_MODEL", "").strip().lower() in {
    "1",
    "true",
    "yes",
}

pytestmark = pytest.mark.skipif(
    not REAL_MODEL_TESTS,
    reason="set AGENTCHAT_TEST_REAL_MODEL=1 to load real weights on a GPU node",
)

#: Short, fixed, and carrying one obvious durable decision. Fixed so a failure
#: is about the format rather than about what was said this time.
TURNS = (
    "For this project we decided to keep every conversation in SQLite, in WAL "
    "mode. Please remember that.",
    "Noted: this project stores its conversations in SQLite in WAL mode.",
)


async def extract_with(model_id: str, tmp_path: Path, *, thinking: bool):
    """Load `model_id`, run one extraction over `TURNS`, return the db path."""
    from agentchat.core.memory.strategy import ChatMemory
    from agentchat.llm.local import TransformersProvider, default_models
    from agentchat.storage.memory import SqliteMemoryStore

    info, kwargs = next((i, k) for i, k in default_models() if i.id == model_id)
    provider = TransformersProvider(info, **kwargs)

    path = tmp_path / "chat.db"
    store = SqliteMemoryStore(path)
    builder = GraphBuilder(make_group(kind="project"))
    chat = builder.conversation()
    for role, content in zip(("user", "assistant"), TURNS):
        builder.message(chat, role=role, content=content)
    write(store, builder.build())

    await provider.load()
    try:
        async def supply():
            return provider

        await ChatMemory(store, supply).extract(chat, thinking=thinking)
    finally:
        await provider.unload()
    return path


def fragments(path: Path) -> list[tuple]:
    with sqlite3.connect(path) as conn:
        return conn.execute(
            "SELECT id, text, kind, confidence, reasoning FROM memory_fragments"
        ).fetchall()


@pytest.mark.parametrize("model_id", ["phi-4-mini", "qwen3-14b"])
async def test_real_model_extraction_reaches_apply(model_id: str, tmp_path: Path, caplog):
    with caplog.at_level(logging.DEBUG, logger="agentchat.core.memory.extract"):
        path = await extract_with(model_id, tmp_path, thinking=False)

    written = fragments(path)
    assert written, (
        f"{model_id} produced no parseable claim — the wire format is not "
        f"reaching this model. Raw replies:\n{caplog.text}"
    )
    for fragment_id, text, kind, confidence, _ in written:
        assert text.strip()
        assert isinstance(kind, str) and kind
        assert 0.0 <= confidence <= 1.0
        with sqlite3.connect(path) as conn:
            citations = conn.execute(
                "SELECT COUNT(*) FROM fragment_citations WHERE fragment_id = ?", (fragment_id,)
            ).fetchone()[0]
        assert citations >= 1, "I-4: a fragment with zero citations must not exist"

    with sqlite3.connect(path) as conn:
        watermark = conn.execute("SELECT extracted_id FROM conversations").fetchone()
    assert watermark[0] is not None


async def test_real_model_extraction_stores_a_reasoning_trace(tmp_path: Path, caplog):
    """`qwen3-14b` only: it is the model with a native thinking mode, and § 2.1
    binds the trace to that same toggle."""
    with caplog.at_level(logging.DEBUG, logger="agentchat.core.memory.extract"):
        path = await extract_with("qwen3-14b", tmp_path, thinking=True)

    written = fragments(path)
    assert written, f"no parseable claim with thinking on. Raw replies:\n{caplog.text}"
    assert any(reasoning for *_, reasoning in written), (
        f"thinking was on and no fragment carried a trace. Raw replies:\n{caplog.text}"
    )
