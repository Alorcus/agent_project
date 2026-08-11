"""Stage 1 acceptance: the database cannot represent a forbidden state.

Detail plan: ``docs/plans/2026-08-10-004-memory-stage-1-schema-and-stores.md``.
The invariants themselves (I-1, I-7, I-8, I-9) are tested in
``test_invariants.py``; this file covers the rest of the store's surface —
``apply``'s four behaviours, the derived view, the FTS triggers, and the
methods that belong to later stages.

Reads go through raw SQL on purpose: the point of stage 1 is what is *in the
database*, and asserting it through the same store that wrote it would let a
bug in the store hide itself.

`SqliteMemoryStore` and the `apply` payloads are imported inside the bodies
that need them, as in `test_invariants.py`: until stage 1 lands, a module-level
import would fail collection and take the rest of the suite down with it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agentchat.core.errors import StorageError
from agentchat.core.memory.tuning import Tuning
from agentchat.storage.sqlite import SqliteStore

from factories import GraphBuilder, make_citation, make_fragment, make_group, write


def fresh(tmp_path: Path):
    from agentchat.storage.memory import SqliteMemoryStore

    path = tmp_path / "chat.db"
    return path, SqliteMemoryStore(path)


def rows(path: Path, sql: str, *params) -> list[tuple]:
    with sqlite3.connect(path) as conn:
        return conn.execute(sql, params).fetchall()


def one(path: Path, sql: str, *params) -> tuple | None:
    found = rows(path, sql, *params)
    return found[0] if found else None


# -- apply: identity and citations ---------------------------------------


def test_apply_mints_ids_and_writes_citations_against_them(tmp_path: Path):
    from agentchat.core.memory.store import FragmentWrite

    path, store = fresh(tmp_path)
    builder = GraphBuilder()
    chat = builder.conversation()
    message = builder.message(chat, content="We chose SQLite.")
    write(store, builder.build())

    fragment = make_fragment(group_id=builder.group.id, id=None)
    # Built before the row exists, which is the case extraction is always in.
    citation = make_citation(fragment, message=message)
    assert citation.fragment_id is None

    result = store.apply([FragmentWrite(fragment, [citation])])

    assert fragment.id is not None
    assert result.inserted == [fragment.id]
    assert result.citations_added == 1
    assert one(path, "SELECT fragment_id, source_message_id FROM fragment_citations") == (
        fragment.id,
        message.id,
    )


def test_apply_honours_an_explicit_fragment_id(tmp_path: Path):
    # The factories mint provisional ids and build citation rows against them;
    # if apply renumbered, every hand-built hierarchy would land mis-wired.
    path, store = fresh(tmp_path)
    builder = GraphBuilder()
    chat = builder.conversation()
    fragment = builder.extracted(builder.message(chat, content="We chose SQLite."))
    provisional = fragment.id

    write(store, builder.build())

    assert fragment.id == provisional
    assert one(path, "SELECT id FROM memory_fragments") == (provisional,)


# -- apply: the four outcomes --------------------------------------------


def test_double_apply_of_one_batch_moves_confidence_once(tmp_path: Path):
    from agentchat.core.memory.store import FragmentWrite

    path, store = fresh(tmp_path)
    builder = GraphBuilder()
    chat = builder.conversation()
    first = builder.message(chat, content="We chose SQLite.")
    again = builder.message(chat, content="SQLite it is, then.")
    fragment = builder.extracted(first, confidence=0.5)
    write(store, builder.build())

    batch = [FragmentWrite(fragment, [make_citation(fragment, message=again)], reinforce=True)]
    store.apply(batch)
    store.apply(batch)

    step = Tuning.from_env().reinforce_step
    (confidence,) = one(path, "SELECT confidence FROM memory_fragments WHERE id = ?", fragment.id)
    (citations,) = one(
        path, "SELECT COUNT(*) FROM fragment_citations WHERE fragment_id = ?", fragment.id
    )
    assert confidence == pytest.approx(0.5 + step)
    assert citations == 2


def test_reinforce_step_is_paid_per_new_citation(tmp_path: Path):
    from agentchat.core.memory.store import FragmentWrite

    path, store = fresh(tmp_path)
    builder = GraphBuilder()
    chat = builder.conversation()
    first = builder.message(chat, content="We chose SQLite.")
    fragment = builder.extracted(first, confidence=0.5)
    second = builder.message(chat, content="SQLite it is.")
    third = builder.message(chat, content="Confirmed: SQLite.")
    write(store, builder.build())

    store.apply(
        [
            FragmentWrite(
                fragment,
                [
                    make_citation(fragment, message=second),
                    make_citation(fragment, message=third),
                ],
                reinforce=True,
            )
        ]
    )

    step = Tuning.from_env().reinforce_step
    (confidence,) = one(path, "SELECT confidence FROM memory_fragments WHERE id = ?", fragment.id)
    assert confidence == pytest.approx(min(1.0, 0.5 + 2 * step))


def test_reinforce_without_a_new_citation_leaves_confidence_alone(tmp_path: Path):
    # § 2.1's backfill-overlap case: two successful runs over the same
    # evidence must not inflate the claim.
    from agentchat.core.memory.store import FragmentWrite

    path, store = fresh(tmp_path)
    builder = GraphBuilder()
    chat = builder.conversation()
    message = builder.message(chat, content="We chose SQLite.")
    fragment = builder.extracted(message, confidence=0.5)
    write(store, builder.build())

    result = store.apply(
        [FragmentWrite(fragment, [make_citation(fragment, message=message)], reinforce=True)]
    )

    assert result.confidence_changes == []
    assert result.citations_added == 0
    (confidence,) = one(path, "SELECT confidence FROM memory_fragments WHERE id = ?", fragment.id)
    assert confidence == pytest.approx(0.5)


def test_reinforce_never_rewrites_the_text(tmp_path: Path):
    from agentchat.core.memory.store import FragmentWrite

    path, store = fresh(tmp_path)
    builder = GraphBuilder()
    chat = builder.conversation()
    first = builder.message(chat, content="We chose SQLite.")
    fragment = builder.extracted(first, text="We chose SQLite for storage.")
    again = builder.message(chat, content="SQLite it is.")
    write(store, builder.build())

    fragment.text = "Something a caller should not be able to smuggle in."
    store.apply([FragmentWrite(fragment, [make_citation(fragment, message=again)], reinforce=True)])

    assert one(path, "SELECT text FROM memory_fragments WHERE id = ?", fragment.id) == (
        "We chose SQLite for storage.",
    )


def test_revise_rewrites_text_and_sets_confidence_outright(tmp_path: Path):
    from agentchat.core.memory.store import FragmentWrite

    path, store = fresh(tmp_path)
    builder = GraphBuilder()
    chat = builder.conversation()
    first = builder.message(chat, content="We chose SQLite.")
    fragment = builder.extracted(first, text="Storage is undecided.", confidence=0.6)
    again = builder.message(chat, content="Actually, WAL mode too.")
    write(store, builder.build())

    fragment.text = "We chose SQLite in WAL mode."
    fragment.confidence = 0.2
    result = store.apply([FragmentWrite(fragment, [make_citation(fragment, message=again)])])

    assert result.inserted == []
    assert result.revised == [fragment.id]
    text, confidence, revised_at = one(
        path,
        "SELECT text, confidence, revised_at FROM memory_fragments WHERE id = ?",
        fragment.id,
    )
    assert text == "We chose SQLite in WAL mode."
    assert confidence == pytest.approx(0.2)
    assert revised_at is not None
    assert one(path, "SELECT rowid FROM memory_fts WHERE memory_fts MATCH 'WAL'") == (fragment.id,)


def test_apply_result_reports_new_revised_and_actual_confidence_changes(tmp_path: Path):
    # § 8.2's three counters. The ↑ count follows citations that were
    # genuinely new, not reinforce decisions that were returned.
    from agentchat.core.memory.store import FragmentWrite

    path, store = fresh(tmp_path)
    builder = GraphBuilder()
    chat = builder.conversation()
    first = builder.message(chat, content="We chose SQLite.")
    second = builder.message(chat, content="Storage is still open.")
    third = builder.message(chat, content="Settled: SQLite, WAL mode.")
    reinforced = builder.extracted(first, confidence=0.5)
    revised = builder.extracted(second, text="Storage is undecided.")
    write(store, builder.build())

    minted = make_fragment(group_id=builder.group.id, id=None, text="The picker is a modal.")
    revised.text = "Storage is settled: SQLite."
    result = store.apply(
        [
            FragmentWrite(minted, [make_citation(minted, message=third)]),
            FragmentWrite(revised, [make_citation(revised, message=third)]),
            FragmentWrite(
                reinforced,
                [
                    make_citation(reinforced, message=third),
                    make_citation(reinforced, message=first),  # already cited
                ],
                reinforce=True,
            ),
        ]
    )

    step = Tuning.from_env().reinforce_step
    assert result.inserted == [minted.id]
    assert result.revised == [revised.id]
    assert result.citations_added == 3
    (change,) = result.confidence_changes
    assert change.fragment_id == reinforced.id
    assert change.before == pytest.approx(0.5)
    assert change.after == pytest.approx(0.5 + step)


# -- apply: rejection is atomic ------------------------------------------


def test_cycle_closing_edge_rolls_the_whole_batch_back(tmp_path: Path):
    from agentchat.core.memory.store import FragmentWrite

    path, store = fresh(tmp_path)
    builder = GraphBuilder()
    chat = builder.conversation()
    message = builder.message(chat, content="We chose SQLite.")
    lower = builder.extracted(message)
    upper = builder.consolidated(lower)
    write(store, builder.build())

    unrelated = make_fragment(group_id=builder.group.id, id=None, text="The picker is a modal.")
    with pytest.raises(StorageError):
        store.apply(
            [
                FragmentWrite(unrelated, [make_citation(unrelated, message=message)]),
                FragmentWrite(lower, [make_citation(lower, source=upper)]),
            ]
        )

    texts = {row[0] for row in rows(path, "SELECT text FROM memory_fragments")}
    assert "The picker is a modal." not in texts
    assert one(path, "SELECT COUNT(*) FROM fragment_citations") == (2,)


def test_self_citation_is_rejected(tmp_path: Path):
    from agentchat.core.memory.store import FragmentWrite

    _, store = fresh(tmp_path)
    builder = GraphBuilder()
    chat = builder.conversation()
    fragment = builder.extracted(builder.message(chat, content="We chose SQLite."))
    write(store, builder.build())

    with pytest.raises(StorageError):
        store.apply([FragmentWrite(fragment, [make_citation(fragment, source=fragment)])])


# -- the watermark -------------------------------------------------------


def test_watermark_advances_even_when_every_claim_was_ignored(tmp_path: Path):
    # `ignore` is the common outcome (§ 2.1). A run that writes no fragment
    # still has to move the watermark, or those turns are re-read forever.
    from agentchat.core.memory.store import Watermark

    path, store = fresh(tmp_path)
    builder = GraphBuilder()
    chat = builder.conversation()
    message = builder.message(chat, content="Morning!")
    write(store, builder.build())

    store.apply([], watermark=Watermark(chat.id, message.created_at, message.id))

    assert one(
        path, "SELECT extracted_at, extracted_id FROM conversations WHERE id = ?", chat.id
    ) == (message.created_at.isoformat(), message.id)


def test_a_rejected_batch_leaves_the_watermark_unmoved(tmp_path: Path):
    from agentchat.core.memory.store import FragmentWrite, Watermark

    path, store = fresh(tmp_path)
    builder = GraphBuilder()
    chat = builder.conversation()
    first = builder.message(chat, content="We chose SQLite.")
    later = builder.message(chat, content="WAL, single writer.")
    lower = builder.extracted(first)
    upper = builder.consolidated(lower)
    write(store, builder.build())
    store.apply([], watermark=Watermark(chat.id, first.created_at, first.id))

    with pytest.raises(StorageError):
        store.apply(
            [FragmentWrite(lower, [make_citation(lower, source=upper)])],
            watermark=Watermark(chat.id, later.created_at, later.id),
        )

    assert one(
        path, "SELECT extracted_at, extracted_id FROM conversations WHERE id = ?", chat.id
    ) == (first.created_at.isoformat(), first.id)


# -- scope, the derived view, the index ----------------------------------


def test_memory_scope_is_none_for_the_default_group(tmp_path: Path):
    _, store = fresh(tmp_path)
    builder = GraphBuilder(make_group(kind="default"))
    builder.conversation()
    write(store, builder.build())

    assert store.memory_scope(builder.group.id) is None
    assert store.memory_scope("no-such-group") is None


def test_memory_scope_returns_the_group_id_for_a_project_group(tmp_path: Path):
    _, store = fresh(tmp_path)
    builder = GraphBuilder(make_group(kind="project"))
    builder.conversation()
    write(store, builder.build())

    assert store.memory_scope(builder.group.id) == builder.group.id


def test_fragment_support_counts_citations_and_conversations(tmp_path: Path):
    path, store = fresh(tmp_path)
    builder = GraphBuilder()
    first = builder.conversation(title="first")
    second = builder.conversation(title="second")
    corroborated = builder.extracted(
        builder.message(first, content="We chose SQLite."),
        builder.message(second, content="Still SQLite, then."),
    )
    higher = builder.consolidated(corroborated)
    write(store, builder.build())

    support = (
        "SELECT citation_count, conversation_count, first_seen_at, last_seen_at"
        " FROM fragment_support WHERE fragment_id = ?"
    )
    citations, conversations, first_seen, last_seen = one(path, support, corroborated.id)
    assert (citations, conversations) == (2, 2)
    assert first_seen <= last_seen

    # A fragment→fragment edge is evidence but not a second conversation —
    # § 6's rank_support orders on conversation_count.
    citations, conversations, _, _ = one(path, support, higher.id)
    assert (citations, conversations) == (1, 0)


def test_memory_fts_follows_inserts_revisions_and_deletes(tmp_path: Path):
    from agentchat.core.memory.store import FragmentWrite

    path, store = fresh(tmp_path)
    builder = GraphBuilder()
    chat = builder.conversation()
    message = builder.message(chat, content="We chose SQLite.")
    fragment = builder.extracted(message, text="We chose SQLite for storage.")
    write(store, builder.build())

    assert one(path, "SELECT rowid FROM memory_fts WHERE memory_fts MATCH 'SQLite'") == (
        fragment.id,
    )

    fragment.text = "The group picker is a modal."
    store.apply([FragmentWrite(fragment, [])])

    assert one(path, "SELECT rowid FROM memory_fts WHERE memory_fts MATCH 'SQLite'") is None
    assert one(path, "SELECT rowid FROM memory_fts WHERE memory_fts MATCH 'picker'") == (
        fragment.id,
    )

    # Stage 4 owns the sweep; what stage 1 owes it is an index that follows a
    # DELETE without anyone remembering to say so.
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM memory_fragments WHERE id = ?", (fragment.id,))

    assert one(path, "SELECT rowid FROM memory_fts WHERE memory_fts MATCH 'picker'") is None


# -- schema rules --------------------------------------------------------


def test_an_unknown_kind_is_accepted(tmp_path: Path):
    # Rule 4: no CHECK on `kind`, so a persona kind plugs in without a schema
    # change and without a migration.
    from agentchat.core.memory.store import FragmentWrite

    path, store = fresh(tmp_path)
    builder = GraphBuilder()
    chat = builder.conversation()
    message = builder.message(chat, content="Ada always replies within the hour.")
    write(store, builder.build())

    fragment = make_fragment(group_id=builder.group.id, id=None, kind="persona_trait")
    store.apply([FragmentWrite(fragment, [make_citation(fragment, message=message)])])

    assert one(path, "SELECT kind FROM memory_fragments WHERE id = ?", fragment.id) == (
        "persona_trait",
    )


def test_a_fragment_in_an_unknown_group_is_rejected(tmp_path: Path):
    # Scope is NOT NULL at every tier (§ 1.1). The citation-less write is fine
    # here: I-4 is the sweep's job (§ 2.4), not apply's.
    from agentchat.core.memory.store import FragmentWrite

    _, store = fresh(tmp_path)
    orphan = make_fragment(group_id="no-such-group", id=None)

    with pytest.raises(StorageError):
        store.apply([FragmentWrite(orphan, [])])


def test_opening_a_pre_memory_database_raises_naming_the_file(tmp_path: Path):
    from agentchat.storage.memory import SqliteMemoryStore

    path = tmp_path / "chat.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT NOT NULL,"
            " group_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )

    for store_type in (SqliteMemoryStore, SqliteStore):
        with pytest.raises(StorageError) as error:
            store_type(path)
        assert str(path) in str(error.value)


def test_unimplemented_methods_name_the_stage_that_owns_them(tmp_path: Path):
    # An empty-list stub would let the skipped I-11 and I-13 tests pass for the
    # wrong reason the day someone activates them early.
    _, store = fresh(tmp_path)

    with pytest.raises(NotImplementedError, match="stage 2"):
        store.candidates("g", "which database did we choose", 10)
    with pytest.raises(NotImplementedError, match="stage 3"):
        store.select("g", "which database did we choose", 512)
    with pytest.raises(NotImplementedError, match="stage 3"):
        store.stable_core("g", 512)
    with pytest.raises(NotImplementedError, match="stage 4"):
        store.purge_conversation("c")
    with pytest.raises(NotImplementedError, match="stage 4"):
        store.purge_group("g")


# -- the factories' half of the contract ---------------------------------


def test_write_round_trips_the_section_111_hierarchy(tmp_path: Path):
    path, store = fresh(tmp_path)
    b = GraphBuilder()
    c = b.conversation()
    m1, m2, m3 = (b.message(c) for _ in range(3))
    f1, f2 = b.extracted(m1, m2), b.extracted(m2, m3)
    f3 = b.consolidated(f1, f2)

    write(store, b.build())

    assert one(path, "SELECT COUNT(*) FROM messages") == (3,)
    assert one(path, "SELECT COUNT(*) FROM memory_fragments") == (3,)
    assert one(path, "SELECT COUNT(*) FROM fragment_citations") == (6,)
    assert sorted(
        row[0]
        for row in rows(
            path, "SELECT source_fragment_id FROM fragment_citations WHERE fragment_id = ?", f3.id
        )
    ) == sorted([f1.id, f2.id])
    assert one(
        path,
        "SELECT COUNT(*) FROM fragment_citations"
        " WHERE fragment_id = ? AND source_message_id IS NOT NULL",
        f3.id,
    ) == (0,)
