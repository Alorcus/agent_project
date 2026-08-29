from __future__ import annotations

import asyncio

import pytest

from agentchat.config import (
    ConfigurationError,
    Settings,
    build_fact_extractor,
    build_store,
)
from agentchat.core.chat import ChatService
from agentchat.core.context import RecencyWindowStrategy
from agentchat.core.errors import ModelNotFoundError, ProviderError
from agentchat.core.facts import FactExtractor
from agentchat.core.models import DEFAULT_GROUP_ID, Conversation, Message
from agentchat.core.prompts import DEFAULT_SYSTEM
from agentchat.llm.base import GenerationOptions, ModelInfo
from agentchat.llm.mock import MockProvider
from agentchat.llm.registry import ModelRegistry
from agentchat.storage.sqlite import SqliteStore
from conftest import fast_registry, mock_settings
from factories import make_group, scripted_provider


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


async def test_chat_service_records_both_turns_and_provenance(store):
    registry = fast_registry()
    chat = ChatService(registry, store=store)
    conversation = await chat.new_conversation()

    chunks = [c async for c in chat.stream_reply(conversation, "hello there")]

    assert [m.role for m in conversation.messages] == ["user", "assistant"]
    assert conversation.messages[1].content == "".join(chunks)
    assert conversation.messages[1].model_id == registry.active_id
    assert conversation.title.startswith("hello there")


async def test_every_turn_is_sent_with_the_system_prompt_and_stores_none(store):
    provider = scripted_provider("a reply", "another reply")
    registry = ModelRegistry()
    registry.register(provider.info, lambda: provider)
    chat = ChatService(registry, store=store)
    conversation = await chat.new_conversation()

    async for _ in chat.stream_reply(conversation, "hello"):
        pass
    async for _ in chat.stream_reply(conversation, "and again"):
        pass

    for call in provider.calls:
        assert call.messages[0].role == "system"
        assert call.messages[0].content == DEFAULT_SYSTEM
    assert [m.role for m in conversation.messages] == ["user", "assistant"] * 2
    stored = await chat.store.load(conversation.id)
    assert all(m.role != "system" for m in stored.messages)


def test_system_prompt_asks_for_short_answers_unless_length_was_asked_for():
    prompt = DEFAULT_SYSTEM.lower()
    assert "short answer" in prompt
    assert "asks" in prompt, "the long-answer exception has to be stated"


async def test_stopping_mid_stream_keeps_the_partial_reply(store):
    # Needs a real mid-generation gap to cancel into — fast_registry()'s
    # default near-zero delay would finish before the sleep below.
    registry = fast_registry(mock_chunk_delay=None, mock_load_delay=None)
    chat = ChatService(registry, store=store)
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
    assert stored is not None
    assert stored.messages[1].content == reply.content


async def test_thinking_option_changes_the_output(store):
    registry = fast_registry()
    chat = ChatService(registry, store=store)
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


async def test_conversations_do_not_leak_into_each_other(store):
    registry = fast_registry()
    chat = ChatService(registry, store=store)
    a = await chat.new_conversation()
    b = await chat.new_conversation()

    async for _ in chat.stream_reply(a, "secret alpha"):
        pass
    async for _ in chat.stream_reply(b, "beta"):
        pass

    assert all("alpha" not in m.content for m in b.messages)


async def test_store_scopes_listing_by_group(store):
    chat = ChatService(fast_registry(), store=store)
    scoped, other = make_group(name="scoped"), make_group(name="other")
    await chat.store.save_group(scoped)
    await chat.store.save_group(other)
    await chat.store.save(Conversation(group_id=scoped.id))
    await chat.store.save(Conversation(group_id=other.id))
    assert len(await chat.store.list_conversations(scoped.id)) == 1
    assert len(await chat.store.list_all_conversations()) == 2


async def test_new_conversation_lands_in_the_group_it_was_given(store):
    """Membership is set here or not at all — § 2.5 allows no move later."""
    chat = ChatService(fast_registry(), store=store)
    project = make_group()
    await chat.store.save_group(project)

    assert (await chat.new_conversation()).group_id == DEFAULT_GROUP_ID
    assert (await chat.new_conversation(project.id)).group_id == project.id


def test_build_store_memory_raises_configuration_error():
    with pytest.raises(ConfigurationError):
        build_store(Settings(backend="mock", store="memory"))


def test_build_store_sqlite_returns_sqlite_store_and_creates_parent(tmp_path):
    data_dir = tmp_path / "nested"
    store = build_store(Settings(backend="mock", store="sqlite", data_dir=data_dir))
    assert isinstance(store, SqliteStore)
    assert data_dir.exists()


def test_build_store_unknown_raises_configuration_error():
    with pytest.raises(ConfigurationError):
        build_store(Settings(backend="mock", store="nope"))


def test_agentchat_extraction_timeout_bad_value_raises_configuration_error(monkeypatch):
    monkeypatch.setenv("AGENTCHAT_EXTRACTION_TIMEOUT", "abc")
    with pytest.raises(ConfigurationError):
        Settings()


def test_build_fact_extractor_is_switched_by_extract_facts():
    registry = fast_registry()
    assert build_fact_extractor(mock_settings(extract_facts=False), registry) is None
    extractor = build_fact_extractor(mock_settings(extract_facts=True), registry)
    assert isinstance(extractor, FactExtractor)


def test_agentchat_extract_facts_env_flag_is_honoured(monkeypatch):
    monkeypatch.setenv("AGENTCHAT_EXTRACT_FACTS", "0")
    assert Settings().extract_facts is False


def test_build_embedder_and_retriever_are_switched_by_recall_facts(store):
    from agentchat.config import build_embedder, build_retriever
    from agentchat.core.retrieval import AdaptiveRetriever
    from agentchat.llm.embedding import HashingEmbedder

    registry = fast_registry()
    assert build_embedder(mock_settings(recall_facts=False)) is None
    assert build_retriever(mock_settings(recall_facts=False), registry, store, None) is None

    embedder = build_embedder(mock_settings(recall_facts=True))
    assert isinstance(embedder, HashingEmbedder)  # mock backend → hashing
    retriever = build_retriever(
        mock_settings(recall_facts=True), registry, store, embedder
    )
    assert isinstance(retriever, AdaptiveRetriever)


def test_agentchat_recall_facts_env_flag_is_honoured(monkeypatch):
    monkeypatch.setenv("AGENTCHAT_RECALL_FACTS", "0")
    assert Settings().recall_facts is False


def test_recall_seeds_is_clamped_to_max_seeds(store):
    from agentchat.config import build_embedder, build_retriever
    from agentchat.core.prompts import MAX_SEEDS

    settings = mock_settings(recall_facts=True, recall_seeds=99)
    retriever = build_retriever(
        settings, fast_registry(), store, build_embedder(settings)
    )
    assert retriever._seeds == MAX_SEEDS
