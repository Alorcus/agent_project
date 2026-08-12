from __future__ import annotations

import pytest

from agentchat.core.chat import ChatService
from agentchat.core.errors import StorageError
from agentchat.core.models import Conversation
from agentchat.storage.base import InMemoryStore
from agentchat.storage.sqlite import SqliteStore
from conftest import fast_registry


async def test_list_all_conversations_on_fresh_service_is_empty():
    chat = ChatService(fast_registry())
    assert await chat.list_all_conversations() == []


async def test_list_all_conversations_returns_most_recently_updated_first():
    chat = ChatService(fast_registry())
    a = await chat.new_conversation()
    b = await chat.new_conversation()

    async for _ in chat.stream_reply(a, "first"):
        pass
    async for _ in chat.stream_reply(b, "second"):
        pass

    listed = await chat.list_all_conversations()
    assert [c.id for c in listed] == [b.id, a.id]


async def test_new_conversation_alone_does_not_appear_in_listing():
    chat = ChatService(fast_registry())
    conversation = await chat.new_conversation()

    assert await chat.list_all_conversations() == []

    async for _ in chat.stream_reply(conversation, "hello"):
        pass

    listed = await chat.list_all_conversations()
    assert [c.id for c in listed] == [conversation.id]


async def test_persist_skips_empty_conversation():
    chat = ChatService(fast_registry())
    conversation = Conversation()

    await chat.persist(conversation)

    assert await chat.store.load(conversation.id) is None


async def test_persist_stores_conversation_with_messages():
    chat = ChatService(fast_registry())
    conversation = Conversation()
    conversation.add_user("hi")

    await chat.persist(conversation)

    stored = await chat.store.load(conversation.id)
    assert stored is not None
    assert stored.id == conversation.id


async def test_switch_conversation_returns_stored_messages():
    chat = ChatService(fast_registry())
    conversation = await chat.new_conversation()
    async for _ in chat.stream_reply(conversation, "hello"):
        pass

    switched = await chat.switch_conversation(conversation.id)

    assert [m.content for m in switched.messages] == [
        m.content for m in conversation.messages
    ]


async def test_switch_conversation_unknown_id_raises_storage_error():
    chat = ChatService(fast_registry())
    with pytest.raises(StorageError):
        await chat.switch_conversation("does-not-exist")


async def test_switch_conversation_resets_last_turn():
    chat = ChatService(fast_registry())
    a = await chat.new_conversation()
    b = await chat.new_conversation()
    async for _ in chat.stream_reply(a, "hello"):
        pass
    assert chat.last_turn is not None
    # b must exist in the store before it can be switched to — empty
    # conversations are never persisted.
    async for _ in chat.stream_reply(b, "hi"):
        pass

    await chat.switch_conversation(b.id)

    assert chat.last_turn is None


async def test_delete_conversation_removes_it_from_listing():
    chat = ChatService(fast_registry())
    conversation = await chat.new_conversation()
    async for _ in chat.stream_reply(conversation, "hello"):
        pass
    assert len(await chat.list_all_conversations()) == 1

    await chat.delete_conversation(conversation.id)

    assert await chat.list_all_conversations() == []


async def test_switch_round_trip_does_not_corrupt_conversations_in_memory():
    await _assert_round_trip_no_corruption(InMemoryStore())


async def test_switch_round_trip_does_not_corrupt_conversations_sqlite(tmp_path):
    await _assert_round_trip_no_corruption(SqliteStore(tmp_path / "t.db"))


async def _assert_round_trip_no_corruption(store):
    chat = ChatService(fast_registry(), store=store)
    a = await chat.new_conversation()
    a_id = a.id
    async for _ in chat.stream_reply(a, "secret alpha"):
        pass

    # Switching to a brand-new conversation is just holding a new object —
    # switch_conversation only resolves ids that already exist in the store.
    b = await chat.new_conversation()
    b_id = b.id
    async for _ in chat.stream_reply(b, "secret beta"):
        pass

    a = await chat.switch_conversation(a_id)

    assert len(a.messages) == 2
    assert a.messages[0].content == "secret alpha"
    assert all("beta" not in m.content for m in a.messages)

    b = await chat.switch_conversation(b_id)
    assert len(b.messages) == 2
    assert b.messages[0].content == "secret beta"
    assert all("alpha" not in m.content for m in b.messages)
