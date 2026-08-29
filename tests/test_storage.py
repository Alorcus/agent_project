from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentchat.core.errors import StorageError
from agentchat.core.models import DEFAULT_GROUP_ID, Author, Fact, Message, Phrase
from agentchat.storage.schema import connect
from agentchat.storage.sqlite import SqliteStore

from factories import make_conversation, make_fact, make_group, make_phrase


async def test_round_trip_title_group_messages_and_model_id(tmp_path: Path):
    path = tmp_path / "chat.db"
    conversation = make_conversation()
    await SqliteStore(path).save(conversation)

    loaded = await SqliteStore(path).load(conversation.id)

    assert loaded is not None
    assert loaded.title == "Trip planning"
    assert loaded.group_id == DEFAULT_GROUP_ID
    assert [m.role for m in loaded.messages] == ["user", "assistant"]
    assert [m.content for m in loaded.messages] == ["Where should I go?", "Try Kyoto."]
    assert loaded.messages[1].model_id == "qwen"


async def test_metadata_round_trips_including_nested_context(tmp_path: Path):
    path = tmp_path / "chat.db"
    message = Message(
        role="assistant",
        content="Kyoto is lovely.",
        metadata={"context": {"window": 4096, "dropped": 2}, "source": "wiki"},
    )
    conversation = make_conversation(messages=[message])
    await SqliteStore(path).save(conversation)

    loaded = await SqliteStore(path).load(conversation.id)

    assert loaded is not None
    assert loaded.messages[0].metadata == {
        "context": {"window": 4096, "dropped": 2},
        "source": "wiki",
    }


async def test_timestamps_round_trip_as_timezone_aware(tmp_path: Path):
    path = tmp_path / "chat.db"
    conversation = make_conversation()
    await SqliteStore(path).save(conversation)

    loaded = await SqliteStore(path).load(conversation.id)

    assert loaded is not None
    assert loaded.created_at.tzinfo is not None
    assert loaded.updated_at.tzinfo is not None
    assert loaded.created_at == conversation.created_at
    assert loaded.updated_at == conversation.updated_at


async def test_messages_come_back_in_insertion_order_after_append(tmp_path: Path):
    path = tmp_path / "chat.db"
    store = SqliteStore(path)
    conversation = make_conversation()
    await store.save(conversation)

    conversation.add(Message(role="user", content="Any food recs?"))
    await store.save(conversation)

    loaded = await SqliteStore(path).load(conversation.id)

    assert loaded is not None
    assert [m.content for m in loaded.messages] == [
        "Where should I go?",
        "Try Kyoto.",
        "Any food recs?",
    ]


async def test_resaving_does_not_duplicate_message_rows(tmp_path: Path):
    path = tmp_path / "chat.db"
    store = SqliteStore(path)
    conversation = make_conversation()

    await store.save(conversation)
    await store.save(conversation)

    loaded = await store.load(conversation.id)
    assert loaded is not None
    assert len(loaded.messages) == 2

    with sqlite3.connect(path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id = ?",
            (conversation.id,),
        ).fetchone()[0]
    assert count == 2


async def test_load_unknown_id_returns_none(tmp_path: Path):
    store = SqliteStore(tmp_path / "chat.db")
    assert await store.load("does-not-exist") is None


async def test_list_conversations_orders_most_recently_updated_first(tmp_path: Path):
    store = SqliteStore(tmp_path / "chat.db")
    now = datetime.now(timezone.utc)

    older = make_conversation(title="older", updated_at=now - timedelta(hours=1))
    newer = make_conversation(title="newer", updated_at=now)
    await store.save(older)
    await store.save(newer)

    items = await store.list_conversations()

    assert [c.title for c in items] == ["newer", "older"]


async def test_list_conversations_filters_by_group_id(tmp_path: Path):
    store = SqliteStore(tmp_path / "chat.db")
    wanted, other = make_group(name="wanted"), make_group(name="other")
    await store.save_group(wanted)
    await store.save_group(other)
    await store.save(make_conversation(title="in-group", group_id=wanted.id))
    await store.save(make_conversation(title="other-group", group_id=other.id))

    items = await store.list_conversations(wanted.id)

    assert [c.title for c in items] == ["in-group"]


async def test_delete_removes_conversation_and_message_rows(tmp_path: Path):
    path = tmp_path / "chat.db"
    store = SqliteStore(path)
    conversation = make_conversation()
    await store.save(conversation)

    await store.delete(conversation.id)

    assert await store.load(conversation.id) is None
    with sqlite3.connect(path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id = ?",
            (conversation.id,),
        ).fetchone()[0]
    assert count == 0


async def test_delete_unknown_id_is_a_no_op(tmp_path: Path):
    store = SqliteStore(tmp_path / "chat.db")
    await store.delete("does-not-exist")  # must not raise


async def test_unwritable_path_raises_storage_error(tmp_path: Path):
    readonly_dir = tmp_path / "readonly"
    readonly_dir.mkdir()
    readonly_dir.chmod(0o500)  # r-x: directory exists, but sqlite can't create a file in it
    try:
        with pytest.raises(StorageError):
            SqliteStore(readonly_dir / "chat.db")
    finally:
        readonly_dir.chmod(0o700)


# -- groups ---------------------------------------------------------------


async def test_default_group_is_seeded_and_is_not_a_project(tmp_path: Path):
    store = SqliteStore(tmp_path / "chat.db")

    groups = await store.list_groups()

    assert [group.kind for group in groups] == ["default"]
    assert (await store.default_group()).is_project() is False


async def test_groups_round_trip_with_the_default_group_first(tmp_path: Path):
    store = SqliteStore(tmp_path / "chat.db")
    project = make_group(name="Picker rewrite")

    await store.save_group(project)

    groups = await store.list_groups()
    assert [group.kind for group in groups] == ["default", "project"]
    assert groups[1].name == "Picker rewrite"


async def test_saving_a_conversation_into_an_unknown_group_raises(tmp_path: Path):
    store = SqliteStore(tmp_path / "chat.db")

    with pytest.raises(StorageError):
        await store.save(make_conversation(group_id="no-such-group"))


async def test_list_all_conversations_spans_groups_and_scoped_listing_does_not(tmp_path: Path):
    store = SqliteStore(tmp_path / "chat.db")
    project = make_group()
    await store.save_group(project)
    await store.save(make_conversation(title="scoped", group_id=project.id))
    await store.save(make_conversation(title="unscoped", group_id=DEFAULT_GROUP_ID))

    scoped = await store.list_conversations(project.id)
    everything = await store.list_all_conversations()

    assert [c.title for c in scoped] == ["scoped"]
    assert {c.title for c in everything} == {"scoped", "unscoped"}


async def test_deleting_a_group_cascades_to_its_conversations(tmp_path: Path):
    """Deleting a group deletes its conversations — they are not re-homed to
    the default group, because that would be a move and there are none."""
    path = tmp_path / "chat.db"
    store = SqliteStore(path)
    project = make_group()
    await store.save_group(project)
    doomed = make_conversation(group_id=project.id)
    kept = make_conversation(group_id=DEFAULT_GROUP_ID)
    await store.save(doomed)
    await store.save(kept)

    await store.delete_group(project.id)

    assert await store.load(doomed.id) is None
    assert await store.load(kept.id) is not None
    assert [group.id for group in await store.list_groups()] == [DEFAULT_GROUP_ID]


async def test_deleting_a_group_takes_its_messages_with_it(tmp_path: Path):
    """The second half of the cascade, which nothing else can observe once the
    conversation rows it hangs off are gone."""
    path = tmp_path / "chat.db"
    store = SqliteStore(path)
    project = make_group()
    await store.save_group(project)
    await store.save(make_conversation(group_id=project.id))

    await store.delete_group(project.id)

    with closing(connect(path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


async def test_the_default_group_cannot_be_deleted(tmp_path: Path):
    store = SqliteStore(tmp_path / "chat.db")
    kept = make_conversation(group_id=DEFAULT_GROUP_ID)
    await store.save(kept)

    with pytest.raises(StorageError):
        await store.delete_group(DEFAULT_GROUP_ID)

    assert await store.default_group() is not None
    assert await store.load(kept.id) is not None


async def test_a_pre_groups_database_is_refused_not_rewritten(tmp_path: Path):
    path = tmp_path / "chat.db"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT NOT NULL,"
            " group_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO conversations VALUES ('c1', 'Real work', NULL, '2026-01-01', '2026-01-01')"
        )

    with pytest.raises(StorageError):
        SqliteStore(path)

    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1


# -- facts ------------------------------------------------------------------


async def test_facts_round_trip_two_facts_with_phrases_and_reconstructed_author(
    tmp_path: Path,
):
    path = tmp_path / "chat.db"
    store = SqliteStore(path)
    conversation = make_conversation(
        messages=[
            Message(id="m1", role="user", content="Where should I go?"),
            Message(id="m2", role="assistant", content="Try Kyoto.", model_id="qwen"),
        ]
    )
    await store.save(conversation)
    fact_a = make_fact(
        conversation_id=conversation.id,
        text="The user wants a trip suggestion.",
        phrases=(
            make_phrase(
                message_id="m1", start=0, end=5, author=Author(kind="user", label="user")
            ),
        ),
    )
    fact_b = make_fact(
        conversation_id=conversation.id,
        text="Kyoto was suggested.",
        phrases=(
            make_phrase(
                message_id="m2", start=4, end=9, author=Author(kind="model", label="qwen")
            ),
        ),
    )
    await store.save_facts([fact_a, fact_b])

    loaded = {f.text: f for f in await store.list_facts(conversation.group_id)}

    assert set(loaded) == {fact_a.text, fact_b.text}
    reloaded_a = loaded[fact_a.text]
    assert reloaded_a.window_start == fact_a.window_start
    assert reloaded_a.window_end == fact_a.window_end
    assert reloaded_a.model_id == fact_a.model_id
    assert len(reloaded_a.phrases) == 1
    assert reloaded_a.phrases[0] == Phrase(
        message_id="m1", start=0, end=5, author=Author(kind="user", label="user")
    )
    reloaded_b = loaded[fact_b.text]
    assert reloaded_b.phrases[0].author == Author(kind="model", label="qwen")


async def test_list_facts_scopes_to_group(tmp_path: Path):
    path = tmp_path / "chat.db"
    store = SqliteStore(path)
    other_group = make_group(name="other")
    await store.save_group(other_group)
    in_group = make_conversation()
    in_other = make_conversation(group_id=other_group.id)
    await store.save(in_group)
    await store.save(in_other)
    await store.save_facts([make_fact(conversation_id=in_group.id, group_id=DEFAULT_GROUP_ID)])
    await store.save_facts(
        [make_fact(conversation_id=in_other.id, group_id=other_group.id, text="other fact")]
    )

    scoped = await store.list_facts(DEFAULT_GROUP_ID)

    assert [f.group_id for f in scoped] == [DEFAULT_GROUP_ID]


async def test_deleting_the_conversation_removes_facts_and_phrases(tmp_path: Path):
    path = tmp_path / "chat.db"
    store = SqliteStore(path)
    conversation = make_conversation()
    await store.save(conversation)
    await store.save_facts([make_fact(conversation_id=conversation.id)])

    await store.delete(conversation.id)

    with closing(connect(path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM fact_phrases").fetchone()[0] == 0


async def test_deleting_the_group_removes_its_conversations_facts(tmp_path: Path):
    path = tmp_path / "chat.db"
    store = SqliteStore(path)
    project = make_group()
    await store.save_group(project)
    conversation = make_conversation(group_id=project.id)
    await store.save(conversation)
    await store.save_facts([make_fact(conversation_id=conversation.id, group_id=project.id)])

    await store.delete_group(project.id)

    with closing(connect(path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0


async def test_resaving_the_conversation_leaves_its_facts_intact(tmp_path: Path):
    """The regression the missing FK to `messages` exists to prevent:
    `SqliteStore._save` deletes and re-inserts every message row on every
    turn, and a real FK there would cascade every fact away with it."""
    path = tmp_path / "chat.db"
    store = SqliteStore(path)
    conversation = make_conversation()
    await store.save(conversation)
    await store.save_facts([make_fact(conversation_id=conversation.id)])

    conversation.add(Message(role="user", content="one more turn"))
    await store.save(conversation)

    assert len(await store.list_facts(conversation.group_id)) == 1
    with closing(connect(path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM fact_phrases").fetchone()[0] == 1


async def test_save_facts_twice_appends(tmp_path: Path):
    path = tmp_path / "chat.db"
    store = SqliteStore(path)
    conversation = make_conversation()
    await store.save(conversation)

    await store.save_facts([make_fact(conversation_id=conversation.id, text="first")])
    await store.save_facts([make_fact(conversation_id=conversation.id, text="second")])

    facts = await store.list_facts(conversation.group_id)
    assert {f.text for f in facts} == {"first", "second"}


async def test_save_facts_empty_is_a_no_op(tmp_path: Path):
    store = SqliteStore(tmp_path / "chat.db")
    await store.save_facts([])  # must not raise
    assert await store.list_facts(DEFAULT_GROUP_ID) == []


async def test_fact_watermark_is_zero_when_unknown_and_round_trips(tmp_path: Path):
    path = tmp_path / "chat.db"
    store = SqliteStore(path)
    conversation = make_conversation()
    await store.save(conversation)

    assert await store.fact_watermark(conversation.id) == 0

    await store.set_fact_watermark(conversation.id, 6)
    assert await store.fact_watermark(conversation.id) == 6

    await store.set_fact_watermark(conversation.id, 10)
    assert await store.fact_watermark(conversation.id) == 10


# -- fact embeddings ------------------------------------------------------


from agentchat.core.models import FactEmbedding  # noqa: E402


def _emb(fact_id: str, view: str = "claim", model_id: str = "hashing-64", vector=(0.1, 0.2, 0.3)):
    return FactEmbedding(fact_id=fact_id, view=view, model_id=model_id, vector=tuple(vector))


async def test_fact_embedding_round_trips_the_same_floats(tmp_path: Path):
    store = SqliteStore(tmp_path / "chat.db")
    conversation = make_conversation()
    await store.save(conversation)
    fact = make_fact(conversation_id=conversation.id)
    await store.save_facts([fact])

    await store.save_fact_embeddings([_emb(fact.id, vector=(0.5, -0.25, 0.125, 0.0))])

    rows = await store.fact_embeddings(conversation.group_id, model_id="hashing-64")
    assert len(rows) == 1
    assert rows[0].vector == pytest.approx((0.5, -0.25, 0.125, 0.0), abs=1e-6)
    assert rows[0].view == "claim"


async def test_saving_the_same_view_and_model_upserts(tmp_path: Path):
    store = SqliteStore(tmp_path / "chat.db")
    conversation = make_conversation()
    await store.save(conversation)
    fact = make_fact(conversation_id=conversation.id)
    await store.save_facts([fact])

    await store.save_fact_embeddings([_emb(fact.id, vector=(1.0, 0.0))])
    await store.save_fact_embeddings([_emb(fact.id, vector=(0.0, 1.0))])

    rows = await store.fact_embeddings(conversation.group_id, model_id="hashing-64")
    assert len(rows) == 1
    assert rows[0].vector == pytest.approx((0.0, 1.0), abs=1e-6)


async def test_facts_without_embeddings_tracks_the_backlog_and_the_embedder_id(tmp_path: Path):
    store = SqliteStore(tmp_path / "chat.db")
    conversation = make_conversation()
    await store.save(conversation)
    f1 = make_fact(conversation_id=conversation.id, text="one")
    f2 = make_fact(conversation_id=conversation.id, text="two")
    await store.save_facts([f1, f2])

    pending = await store.facts_without_embeddings(conversation.group_id, model_id="m1")
    assert {f.text for f in pending} == {"one", "two"}

    await store.save_fact_embeddings([_emb(f1.id, model_id="m1"), _emb(f2.id, model_id="m1")])
    assert await store.facts_without_embeddings(conversation.group_id, model_id="m1") == []

    # A different embedder id sees every fact again (R4).
    other = await store.facts_without_embeddings(conversation.group_id, model_id="m2")
    assert {f.text for f in other} == {"one", "two"}


async def test_deleting_the_conversation_removes_its_vectors(tmp_path: Path):
    path = tmp_path / "chat.db"
    store = SqliteStore(path)
    conversation = make_conversation()
    await store.save(conversation)
    fact = make_fact(conversation_id=conversation.id)
    await store.save_facts([fact])
    await store.save_fact_embeddings([_emb(fact.id)])

    await store.delete(conversation.id)

    with closing(connect(path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM fact_embeddings").fetchone()[0] == 0


async def test_deleting_the_group_removes_its_vectors(tmp_path: Path):
    path = tmp_path / "chat.db"
    store = SqliteStore(path)
    project = make_group()
    await store.save_group(project)
    conversation = make_conversation(group_id=project.id)
    await store.save(conversation)
    fact = make_fact(conversation_id=conversation.id, group_id=project.id)
    await store.save_facts([fact])
    await store.save_fact_embeddings([_emb(fact.id)])

    await store.delete_group(project.id)

    with closing(connect(path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM fact_embeddings").fetchone()[0] == 0


async def test_resaving_the_conversation_leaves_vectors_intact(tmp_path: Path):
    store = SqliteStore(tmp_path / "chat.db")
    conversation = make_conversation()
    await store.save(conversation)
    fact = make_fact(conversation_id=conversation.id)
    await store.save_facts([fact])
    await store.save_fact_embeddings([_emb(fact.id)])

    conversation.add(Message(role="user", content="one more turn"))
    await store.save(conversation)

    rows = await store.fact_embeddings(conversation.group_id, model_id="hashing-64")
    assert len(rows) == 1


async def test_phrase_quote_round_trips_and_matches_the_source_message(tmp_path: Path):
    from agentchat.core.facts import FactExtractor
    from agentchat.llm.registry import ModelRegistry
    from factories import scripted_provider

    store = SqliteStore(tmp_path / "chat.db")
    messages = [
        Message(id="u1", role="user", content="We're moving staging to GKE next month."),
    ]
    conversation = make_conversation(messages=messages)
    await store.save(conversation)

    provider = scripted_provider(
        "The team is moving staging to GKE next month.",
        "moving staging to GKE next month",
    )
    registry = ModelRegistry()
    registry.register(provider.info, lambda: provider)
    fact = await FactExtractor(registry).extract(messages)
    fact.conversation_id = conversation.id
    fact.group_id = conversation.group_id
    await store.save_facts([fact])

    loaded = (await store.list_facts(conversation.group_id))[0]
    phrase = loaded.phrases[0]
    assert phrase.quote == "moving staging to GKE next month"
    assert phrase.quote == phrase.text(messages[0])
