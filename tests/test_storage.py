from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentchat.core.errors import StorageError
from agentchat.core.models import Conversation, Message
from agentchat.storage.sqlite import SqliteStore


def make_conversation(**overrides) -> Conversation:
    defaults = dict(
        title="Trip planning",
        group_id="g1",
        messages=[
            Message(role="user", content="Where should I go?"),
            Message(role="assistant", content="Try Kyoto.", model_id="qwen"),
        ],
    )
    defaults.update(overrides)
    return Conversation(**defaults)


async def test_round_trip_title_group_messages_and_model_id(tmp_path: Path):
    path = tmp_path / "chat.db"
    conversation = make_conversation()
    await SqliteStore(path).save(conversation)

    loaded = await SqliteStore(path).load(conversation.id)

    assert loaded is not None
    assert loaded.title == "Trip planning"
    assert loaded.group_id == "g1"
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
    in_group = make_conversation(title="in-group", group_id="g1")
    other_group = make_conversation(title="other-group", group_id="g2")
    await store.save(in_group)
    await store.save(other_group)

    items = await store.list_conversations("g1")

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
