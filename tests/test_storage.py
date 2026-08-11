from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentchat.core.errors import StorageError
from agentchat.core.models import Message
from agentchat.storage.sqlite import SqliteStore

from factories import make_conversation


async def test_round_trip_title_group_messages_and_model_id(tmp_path: Path):
    path = tmp_path / "chat.db"
    conversation = make_conversation()
    await SqliteStore(path).save(conversation)

    loaded = await SqliteStore(path).load(conversation.id)

    assert loaded is not None
    assert loaded.title == "Trip planning"
    assert loaded.group_id == "default"
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
    from factories import make_group

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


# -- groups and the watermark (stage 1) ----------------------------------
#
# Every `agentchat` import below sits inside a test body: these describe the
# schema of `2026-08-10-004-memory-stage-1-schema-and-stores.md`, and a
# module-level import of a name that does not exist yet would fail collection
# for the whole file, taking the tests above down with it.


async def test_default_group_is_seeded_and_is_not_a_memory_scope(tmp_path: Path):
    store = SqliteStore(tmp_path / "chat.db")

    groups = await store.list_groups()

    assert [group.kind for group in groups] == ["default"]
    assert (await store.default_group()).is_memory_scope() is False


async def test_groups_round_trip_with_the_default_group_first(tmp_path: Path):
    from factories import make_group

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
    from agentchat.core.models import DEFAULT_GROUP_ID

    from factories import make_group

    store = SqliteStore(tmp_path / "chat.db")
    project = make_group()
    await store.save_group(project)
    await store.save(make_conversation(title="scoped", group_id=project.id))
    await store.save(make_conversation(title="unscoped", group_id=DEFAULT_GROUP_ID))

    scoped = await store.list_conversations(project.id)
    everything = await store.list_all_conversations()

    assert [c.title for c in scoped] == ["scoped"]
    assert {c.title for c in everything} == {"scoped", "unscoped"}


async def test_watermark_round_trips_on_the_conversation(tmp_path: Path):
    from agentchat.core.models import DEFAULT_GROUP_ID

    store = SqliteStore(tmp_path / "chat.db")
    conversation = make_conversation(group_id=DEFAULT_GROUP_ID)
    marker = conversation.messages[-1]
    conversation.extracted_at = marker.created_at
    conversation.extracted_id = marker.id
    await store.save(conversation)

    loaded = await store.load(conversation.id)

    assert loaded is not None
    assert (loaded.extracted_at, loaded.extracted_id) == (marker.created_at, marker.id)


async def test_in_memory_store_holds_i1_too():
    from agentchat.core.models import DEFAULT_GROUP_ID
    from agentchat.storage.base import InMemoryStore

    store = InMemoryStore()

    with pytest.raises(StorageError):
        await store.save(make_conversation(group_id="no-such-group"))
    await store.save(make_conversation(group_id=DEFAULT_GROUP_ID))
