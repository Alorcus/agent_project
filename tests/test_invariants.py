"""The cumulative invariant suite: one test per I-1..I-13 of
``docs/plans/memory-and-groups.md`` § 3, keyed to the invariant-to-stage map in
``docs/plans/2026-08-10-002-memory-and-groups-skeleton.md``.

Every ``agentchat`` and ``factories`` import sits inside a test body. A
module-level one would fail collection for the whole file while
``core/memory/`` is unwritten, and then a skipped test would not skip — it
would error.
"""

from __future__ import annotations

import ast
import sqlite3
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest

MEMORY_PACKAGE = (
    Path(__file__).resolve().parent.parent / "src" / "agentchat" / "core" / "memory"
)

#: The reply the scripted provider hands back where a test needs exactly one
#: claim. Its wire format is stage 2's contract, not stage 0's.
ONE_CLAIM = '[{"kind": "fact", "text": "We chose SQLite for storage."}]'


def stage(n: int):
    """Not yet implementable. Activating this test is deleting one line."""
    return pytest.mark.skip(reason=f"stage {n}")


class ScriptedProvider:
    """An ``LLMProvider`` whose output is fixed, so extraction tests assert on
    what reaches ``apply()`` rather than on model output quality."""

    def __init__(self, *replies: str, error: Exception | None = None) -> None:
        self._replies = list(replies)
        self._error = error
        self.calls = 0

    async def load(self) -> None:
        return None

    async def unload(self) -> None:
        return None

    async def generate(self, messages: Sequence[object], options: object = None) -> AsyncIterator[str]:
        self.calls += 1
        if self._error is not None:
            raise self._error
        yield self._replies[min(self.calls - 1, len(self._replies) - 1)]


async def test_i1_conversation_requires_a_group(tmp_path: Path):
    from dataclasses import fields

    from agentchat.core.errors import StorageError
    from agentchat.core.models import Conversation
    from agentchat.storage.sqlite import SqliteStore

    from factories import make_conversation

    typed = {f.name: f.type for f in fields(Conversation)}
    assert typed["group_id"] == "str"

    store = SqliteStore(tmp_path / "chat.db")
    with pytest.raises(StorageError):
        await store.save(make_conversation(group_id=None))


@stage(2)
async def test_i2_default_group_extracts_nothing(tmp_path: Path, monkeypatch):
    from agentchat.core.memory.extract import MemoryExtractor
    from agentchat.storage.memory import SqliteMemoryStore

    from factories import GraphBuilder, make_group, write

    path = tmp_path / "chat.db"
    store = SqliteMemoryStore(path)

    builder = GraphBuilder(make_group(kind="default"))
    conversation = builder.conversation()
    builder.message(conversation, content="Remember that I prefer aisle seats.")
    write(store, builder.build())

    applied: list[object] = []
    monkeypatch.setattr(store, "apply", lambda decisions: applied.append(decisions))

    await MemoryExtractor(store, ScriptedProvider(ONE_CLAIM)).run(conversation)

    assert applied == []
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM memory_fragments").fetchone()[0] == 0


@stage(3)
def test_i2_default_group_recalls_nothing(tmp_path: Path):
    from agentchat.core.context import RecencyWindowStrategy
    from agentchat.core.memory.strategy import GroupMemoryStrategy
    from agentchat.storage.memory import SqliteMemoryStore

    from factories import GraphBuilder, make_group, write

    store = SqliteMemoryStore(tmp_path / "chat.db")

    project = GraphBuilder(make_group(kind="project"))
    project_chat = project.conversation()
    project.extracted(project.message(project_chat, content="We chose SQLite."))
    write(store, project.build())

    default = GraphBuilder(make_group(kind="default"))
    chat = default.conversation()
    default.message(chat, content="What did we pick for storage?")
    write(store, default.build())

    strategy = GroupMemoryStrategy(inner=RecencyWindowStrategy(), store=store)
    decision = strategy.build(chat.messages, context_window=4096, conversation=chat)

    assert decision.recalled == []
    assert [m.content for m in decision.messages] == [m.content for m in chat.messages]


@stage(4)
def test_i3_citations_stay_within_their_group(tmp_path: Path):
    from agentchat.storage.memory import SqliteMemoryStore

    from factories import GraphBuilder, write

    path = tmp_path / "chat.db"
    store = SqliteMemoryStore(path)

    a = GraphBuilder()
    a_chat = a.conversation()
    a.extracted(a.message(a_chat, content="A: we chose SQLite."))
    write(store, a.build())

    b = GraphBuilder()
    b_chat = b.conversation()
    b.extracted(b.message(b_chat, content="B: the picker is a modal."))
    write(store, b.build())

    cross_group = """
        SELECT COUNT(*) FROM fragment_citations fc
        JOIN memory_fragments f ON f.id = fc.fragment_id
        JOIN messages m ON m.id = fc.source_message_id
        JOIN conversations c ON c.id = m.conversation_id
        WHERE c.group_id != f.group_id
    """
    b_rows = """
        SELECT
          (SELECT COUNT(*) FROM memory_fragments WHERE group_id = ?),
          (SELECT COUNT(*) FROM conversations WHERE group_id = ?),
          (SELECT COUNT(*) FROM fragment_citations fc
             JOIN memory_fragments f ON f.id = fc.fragment_id
            WHERE f.group_id = ?)
    """

    with sqlite3.connect(path) as conn:
        assert conn.execute(cross_group).fetchone()[0] == 0
        before = conn.execute(b_rows, (b.group.id,) * 3).fetchone()

    store.purge_group(a.group.id)

    with sqlite3.connect(path) as conn:
        assert conn.execute(b_rows, (b.group.id,) * 3).fetchone() == before


@stage(4)
def test_i4_sweep_removes_citationless_fragments(tmp_path: Path):
    from agentchat.storage.memory import SqliteMemoryStore

    from factories import GraphBuilder, write

    path = tmp_path / "chat.db"
    store = SqliteMemoryStore(path)

    builder = GraphBuilder()
    doomed = builder.conversation(title="doomed")
    surviving = builder.conversation(title="surviving")
    f1 = builder.extracted(builder.message(doomed, content="We chose SQLite."))
    f2 = builder.extracted(builder.message(surviving, content="WAL, single writer."))
    f3 = builder.consolidated(f1)
    write(store, builder.build())

    # The only deletion entry point on the protocol is per conversation; the
    # doomed one holds the single message f1 rests on.
    store.purge_conversation(doomed.id)

    with sqlite3.connect(path) as conn:
        alive = {row[0] for row in conn.execute("SELECT id FROM memory_fragments")}

    assert f1.id not in alive
    assert f3.id not in alive
    assert f2.id in alive


@stage(5)
async def test_i4_uncited_insights_are_dropped_not_written(tmp_path: Path):
    from agentchat.core.memory.consolidate import Consolidator
    from agentchat.storage.memory import SqliteMemoryStore

    from factories import GraphBuilder, write

    path = tmp_path / "chat.db"
    store = SqliteMemoryStore(path)

    builder = GraphBuilder()
    chat = builder.conversation()
    builder.extracted(builder.message(chat, content="We chose SQLite."))
    builder.extracted(builder.message(chat, content="WAL, single writer."))
    write(store, builder.build())

    questions = '["What is the storage approach?"]'
    unparseable = "the storage approach is settled (because of the earlier turns)"
    provider = ScriptedProvider(questions, unparseable)

    await Consolidator(store, provider).run(builder.group.id)

    with sqlite3.connect(path) as conn:
        consolidated = conn.execute(
            "SELECT COUNT(*) FROM memory_fragments WHERE consolidated = 1"
        ).fetchone()[0]
        citationless = conn.execute(
            """
            SELECT COUNT(*) FROM memory_fragments f
             WHERE NOT EXISTS (
               SELECT 1 FROM fragment_citations fc WHERE fc.fragment_id = f.id)
            """
        ).fetchone()[0]

    assert consolidated == 0
    assert citationless == 0


@stage(2)
def test_i5_extraction_prompt_demands_self_contained_text():
    """Best effort by construction: § 3 concedes I-5 is unenforceable in the
    schema, so all a test can check is that the instruction is still in the
    prompt."""
    from agentchat.core.memory.extract import EXTRACTION_PROMPT

    assert "self-contained" in EXTRACTION_PROMPT.lower()


def test_i6_memory_modules_never_import_chat_types():
    # The other half of I-6 — persona code never reading fragments — has no
    # persona package to check yet, so it is deliberately not covered here.
    forbidden_modules = (
        "agentchat.core.models",
        "agentchat.core.chat",
        "agentchat.core.context",
        "agentchat.storage",
        "agentchat.ui",
    )
    forbidden_names = ("Message", "Conversation")

    # strategy.py is licensed to know both worlds and is not checked.
    for filename in ("extract.py", "rank.py", "consolidate.py"):
        path = MEMORY_PACKAGE / filename
        assert path.exists(), f"{path} is missing — a rename must not empty this lint"

        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not _is_forbidden_module(alias.name, forbidden_modules), (
                        f"{filename} imports {alias.name}"
                    )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert not _is_forbidden_module(module, forbidden_modules), (
                    f"{filename} imports from {module}"
                )
                for alias in node.names:
                    assert alias.name not in forbidden_names, (
                        f"{filename} imports {alias.name} from {module or '.'}"
                    )


def _is_forbidden_module(name: str, forbidden: tuple[str, ...]) -> bool:
    return any(name == f or name.startswith(f + ".") for f in forbidden)


def test_i7_repeat_citation_is_a_noop(tmp_path: Path):
    from agentchat.storage.memory import SqliteMemoryStore

    from factories import GraphBuilder, write

    path = tmp_path / "chat.db"
    store = SqliteMemoryStore(path)

    builder = GraphBuilder()
    chat = builder.conversation()
    message = builder.message(chat, content="We chose SQLite.")
    lower = builder.extracted(message)
    upper = builder.consolidated(lower)
    graph = builder.build()

    write(store, graph)
    write(store, graph)

    with sqlite3.connect(path) as conn:
        message_edges = conn.execute(
            "SELECT COUNT(*) FROM fragment_citations WHERE fragment_id = ? AND source_message_id = ?",
            (lower.id, message.id),
        ).fetchone()[0]
        fragment_edges = conn.execute(
            "SELECT COUNT(*) FROM fragment_citations WHERE fragment_id = ? AND source_fragment_id = ?",
            (upper.id, lower.id),
        ).fetchone()[0]

    assert message_edges == 1
    assert fragment_edges == 1


def test_i8_cycle_closing_edge_is_rejected(tmp_path: Path):
    from agentchat.core.errors import StorageError
    from agentchat.storage.memory import SqliteMemoryStore

    from factories import GraphBuilder, make_citation, write

    store = SqliteMemoryStore(tmp_path / "chat.db")

    builder = GraphBuilder()
    chat = builder.conversation()
    f1 = builder.extracted(builder.message(chat, content="We chose SQLite."))
    f2 = builder.consolidated(f1)
    graph = builder.build()

    write(store, graph)

    # I-7 makes the re-applied rows no-ops, so only the back edge is new.
    graph.citations.append(make_citation(f1, source=f2))
    with pytest.raises(StorageError):
        write(store, graph)


def test_i8_forward_edge_in_id_order_is_allowed(tmp_path: Path):
    from agentchat.storage.memory import SqliteMemoryStore

    from factories import GraphBuilder, make_citation, write

    path = tmp_path / "chat.db"
    store = SqliteMemoryStore(path)

    builder = GraphBuilder()
    chat = builder.conversation()
    f1 = builder.extracted(builder.message(chat, content="We chose SQLite."))
    older = builder.consolidated(f1)
    newer = builder.extracted(builder.message(chat, content="WAL, single writer."))
    assert older.id < newer.id

    graph = builder.build()
    graph.citations.append(make_citation(older, source=newer))
    write(store, graph)

    with sqlite3.connect(path) as conn:
        edges = conn.execute(
            "SELECT COUNT(*) FROM fragment_citations WHERE fragment_id = ? AND source_fragment_id = ?",
            (older.id, newer.id),
        ).fetchone()[0]

    assert edges == 1


def test_i9_citation_cites_exactly_one_source(tmp_path: Path):
    from agentchat.storage.memory import SqliteMemoryStore

    from factories import GraphBuilder, write

    path = tmp_path / "chat.db"
    store = SqliteMemoryStore(path)

    builder = GraphBuilder()
    chat = builder.conversation()
    message = builder.message(chat, content="We chose SQLite.")
    fragment = builder.extracted(message)
    write(store, builder.build())

    insert = (
        "INSERT INTO fragment_citations"
        " (fragment_id, source_message_id, source_fragment_id, observed_at)"
        " VALUES (?, ?, ?, ?)"
    )
    with sqlite3.connect(path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert, (fragment.id, None, None, "2026-08-10T00:00:00+00:00"))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                insert,
                (fragment.id, message.id, fragment.id, "2026-08-10T00:00:00+00:00"),
            )


@stage(4)
def test_i10_dormant_fragments_survive_the_sweep(tmp_path: Path):
    from agentchat.storage.memory import SqliteMemoryStore

    from factories import GraphBuilder, write

    path = tmp_path / "chat.db"
    store = SqliteMemoryStore(path)

    builder = GraphBuilder()
    doomed = builder.conversation(title="doomed")
    keeper = builder.conversation(title="keeper")
    builder.extracted(builder.message(doomed, content="Unrelated evidence."))
    dormant = builder.extracted(
        builder.message(keeper, content="Actually we ruled Postgres out."),
        confidence=0.0,
    )
    write(store, builder.build())

    store.purge_conversation(doomed.id)

    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT confidence FROM memory_fragments WHERE id = ?", (dormant.id,)
        ).fetchone()

    assert row is not None
    assert row[0] == 0.0


@stage(3)
def test_i11_floor_is_applied_before_fusion(tmp_path: Path, monkeypatch):
    from agentchat.storage.memory import SqliteMemoryStore

    from factories import GraphBuilder, write

    # A floor no fragment can clear: whatever fusion would rank first is still
    # never admitted, which is the ordering the invariant is about.
    monkeypatch.setenv("AGENTCHAT_MEMORY_RECALL_FLOOR", "1.0")

    store = SqliteMemoryStore(tmp_path / "chat.db")
    builder = GraphBuilder()
    chat = builder.conversation()
    builder.extracted(builder.message(chat, content="We chose SQLite."))
    write(store, builder.build())

    query = "which database did we choose"
    assert store.candidates(builder.group.id, query, 10) != []
    assert store.select(builder.group.id, query, 512) == []


@stage(2)
async def test_i12_watermark_advances_only_with_its_writes(tmp_path: Path):
    from agentchat.core.errors import ProviderError
    from agentchat.core.memory.extract import MemoryExtractor
    from agentchat.storage.memory import SqliteMemoryStore

    from factories import GraphBuilder, write

    path = tmp_path / "chat.db"
    store = SqliteMemoryStore(path)

    builder = GraphBuilder()
    chat = builder.conversation()
    for content in ("We chose SQLite.", "WAL, single writer.", "Ship it."):
        builder.message(chat, content=content)
    write(store, builder.build())

    watermark = "SELECT extracted_at, extracted_id FROM conversations WHERE id = ?"
    failing = ScriptedProvider(error=ProviderError("simulated"))

    with pytest.raises(ProviderError):
        await MemoryExtractor(store, failing).run(chat)

    with sqlite3.connect(path) as conn:
        assert conn.execute(watermark, (chat.id,)).fetchone() == (None, None)

    await MemoryExtractor(store, ScriptedProvider(ONE_CLAIM)).run(chat)

    with sqlite3.connect(path) as conn:
        extracted_at, extracted_id = conn.execute(watermark, (chat.id,)).fetchone()
        cited = {
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT source_message_id FROM fragment_citations"
                " WHERE source_message_id IS NOT NULL"
            )
        }

    for message in chat.messages:
        past = (message.created_at.isoformat(), message.id) > (extracted_at, extracted_id)
        assert message.id in cited or past


@stage(3)
def test_i13_dormant_is_reachable_by_candidates_not_select(tmp_path: Path):
    from agentchat.storage.memory import SqliteMemoryStore

    from factories import GraphBuilder, write

    store = SqliteMemoryStore(tmp_path / "chat.db")
    builder = GraphBuilder()
    chat = builder.conversation()
    dormant = builder.extracted(
        builder.message(chat, content="We chose SQLite."),
        confidence=0.0,
    )
    write(store, builder.build())

    query = "which database did we choose"
    assert dormant.id in [f.id for f in store.candidates(builder.group.id, query, 10)]
    assert dormant.id not in [f.id for f in store.select(builder.group.id, query, 512)]


def test_every_invariant_has_a_test():
    """The drift detector for the drift detector: the suite is cumulative and
    never shrinks, and this makes that mechanical rather than a matter of
    discipline."""
    names = [name for name in globals() if name.startswith("test_i")]

    missing = [n for n in range(1, 14) if not any(x.startswith(f"test_i{n}_") for x in names)]

    assert missing == [], f"invariants without a test: {missing}"
