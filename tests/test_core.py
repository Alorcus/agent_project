from __future__ import annotations

import asyncio

import pytest

from agentchat.config import ConfigurationError, Settings, build_store
from agentchat.core.chat import ChatService
from agentchat.core.context import RecencyWindowStrategy
from agentchat.core.errors import ModelNotFoundError, ProviderError
from agentchat.core.models import Conversation, Message
from agentchat.llm.base import GenerationOptions, ModelInfo
from agentchat.llm.mock import MockProvider
from agentchat.storage.base import InMemoryStore
from agentchat.storage.sqlite import SqliteStore
from conftest import fast_registry


async def test_mock_provider_streams_multiple_chunks():
    info = ModelInfo(id="t", name="T", context_window=2048)
    provider = MockProvider(info, chunk_delay=0.0, load_delay=0.0, seed=0)
    await provider.load()
    chunks = [c async for c in provider.generate([Message(role="user", content="hi")])]
    assert len(chunks) > 5, "must stream, not return one blob"
    assert "hi" in "".join(chunks)


async def test_provider_rejects_generation_before_load():
    provider = MockProvider(ModelInfo(id="t", name="T", context_window=64))
    with pytest.raises(ProviderError):
        [c async for c in provider.generate([])]


async def test_failing_backend_raises_provider_error_not_crash():
    registry = fast_registry(simulate_failure=True)
    registry.cycle()  # move to the second model, which is the failing one
    with pytest.raises(ProviderError):
        await registry.active_provider()


async def test_registry_keeps_one_model_resident():
    registry = fast_registry()
    first = await registry.active_provider()
    assert first.is_loaded
    registry.cycle()
    second = await registry.active_provider()
    assert second.is_loaded
    assert not first.is_loaded, "previous model must be evicted (NFR-P-05)"


async def test_unknown_model_id_is_an_error():
    registry = fast_registry()
    with pytest.raises(ModelNotFoundError):
        registry.set_active("does-not-exist")


def test_context_strategy_drops_oldest_to_fit_window():
    messages = [Message(role="user", content="x" * 400) for _ in range(20)]
    decision = RecencyWindowStrategy().build(
        messages, context_window=512, reserve_for_response=128
    )
    assert decision.was_trimmed
    assert decision.estimated_tokens <= decision.budget
    # the most recent turn always survives
    assert decision.messages[-1] is messages[-1]


def test_context_strategy_keeps_everything_when_it_fits():
    messages = [Message(role="user", content="short") for _ in range(3)]
    decision = RecencyWindowStrategy().build(messages, context_window=8192)
    assert not decision.was_trimmed
    assert decision.messages == messages


async def test_chat_service_records_both_turns_and_provenance():
    registry = fast_registry()
    chat = ChatService(registry)
    conversation = await chat.new_conversation()

    chunks = [c async for c in chat.stream_reply(conversation, "hello there")]

    assert [m.role for m in conversation.messages] == ["user", "assistant"]
    assert conversation.messages[1].content == "".join(chunks)
    assert conversation.messages[1].model_id == registry.active_id
    assert conversation.title.startswith("hello there")


async def test_stopping_mid_stream_keeps_the_partial_reply():
    # Needs a real mid-generation gap to cancel into — fast_registry()'s
    # default near-zero delay would finish before the sleep below.
    registry = fast_registry(mock_chunk_delay=None, mock_load_delay=None)
    chat = ChatService(registry)
    conversation = await chat.new_conversation()

    async def consume():
        async for _ in chat.stream_reply(conversation, "hello"):
            pass

    task = asyncio.create_task(consume())
    # let it load and emit a few chunks, then cancel
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    reply = conversation.messages[1]
    assert reply.role == "assistant"
    assert reply.content, "a stopped reply keeps what it produced"
    stored = await chat.store.load(conversation.id)
    assert stored is conversation


async def test_thinking_option_changes_the_output():
    registry = fast_registry()
    chat = ChatService(registry)
    plain_conv = await chat.new_conversation()
    plain = "".join([c async for c in chat.stream_reply(plain_conv, "q")])

    think_conv = await chat.new_conversation()
    thought = "".join(
        [
            c
            async for c in chat.stream_reply(
                think_conv, "q", GenerationOptions(thinking=True)
            )
        ]
    )
    assert len(thought) > len(plain)


async def test_conversations_do_not_leak_into_each_other():
    registry = fast_registry()
    chat = ChatService(registry)
    a = await chat.new_conversation()
    b = await chat.new_conversation()

    async for _ in chat.stream_reply(a, "secret alpha"):
        pass
    async for _ in chat.stream_reply(b, "beta"):
        pass

    assert all("alpha" not in m.content for m in b.messages)


async def test_store_scopes_listing_by_group():
    from factories import make_group

    chat = ChatService(fast_registry())
    scoped, other = make_group(name="scoped"), make_group(name="other")
    await chat.store.save_group(scoped)
    await chat.store.save_group(other)
    await chat.store.save(Conversation(group_id=scoped.id))
    await chat.store.save(Conversation(group_id=other.id))
    assert len(await chat.store.list_conversations(scoped.id)) == 1
    assert len(await chat.store.list_all_conversations()) == 2


def test_build_store_memory_returns_in_memory_store():
    store = build_store(Settings(backend="mock", store="memory"))
    assert isinstance(store, InMemoryStore)


def test_build_store_sqlite_returns_sqlite_store_and_creates_parent(tmp_path):
    data_dir = tmp_path / "nested"
    store = build_store(Settings(backend="mock", store="sqlite", data_dir=data_dir))
    assert isinstance(store, SqliteStore)
    assert data_dir.exists()


def test_build_store_unknown_raises_configuration_error():
    with pytest.raises(ConfigurationError):
        build_store(Settings(backend="mock", store="nope"))
