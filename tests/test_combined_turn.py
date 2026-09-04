"""Recall and consultation on the same turn: order, both blocks reaching the
model, the staged trimming rollback, independent metadata, and the two
switches still meaning what they say."""

from __future__ import annotations


from agentchat.core.chat import ChatService
from agentchat.core.delegation import DelegationService
from agentchat.core.models import Conversation
from agentchat.core.prompts import (
    CONSULTATION_HEADER,
    RECALL_HEADER,
    SPECIALIST_EVIDENCE_HEADER,
)
from agentchat.core.retrieval import AdaptiveRetriever, FactIndex
from agentchat.llm.base import ModelInfo
from agentchat.llm.embedding import HashingEmbedder
from agentchat.llm.registry import ModelRegistry
from factories import make_indexed_group, scripted_provider


def _registry_with(provider) -> ModelRegistry:
    registry = ModelRegistry()
    registry.register(provider.info, lambda: provider)
    return registry


def _facts(text: str):
    return [dict(text=text, quote="", author=("user", "user"))]


async def _retriever(registry, store, embedder, **kw):
    kw.setdefault("rewrites", 1)
    kw.setdefault("min_score", 0.0)
    return AdaptiveRetriever(registry, FactIndex(store, embedder), **kw)


# gate → rewrite → judge, then route → task → specialist, then chat
_RECALL_SCRIPT = ("SEARCH", "a query", "ENOUGH")
_DELEGATION_SCRIPT = ("ask_bank", "Explain APR.", "an answer")


async def test_a_turn_recalls_then_consults_and_the_reply_sees_both_blocks(store):
    embedder = HashingEmbedder()
    group = await make_indexed_group(
        store, embedder, facts=_facts("The team runs Postgres 14 for billing.")
    )
    provider = scripted_provider(
        *_RECALL_SCRIPT, *_DELEGATION_SCRIPT, "the reply"
    )
    registry = _registry_with(provider)
    chat = ChatService(
        registry,
        store=store,
        delegator=DelegationService(registry),
        retriever=await _retriever(registry, store, embedder),
    )
    conv = Conversation(group_id=group.id)

    async for _ in chat.stream_reply(conv, "what does APR mean for our billing?"):
        pass

    assert len(provider.calls) == 7  # gate, rewrite, judge, route, task, specialist, chat

    # The specialist was handed the digest behind its own header, after the task.
    specialist_prompt = provider.calls[5].messages[-1].content
    assert specialist_prompt.index("Explain APR.") < specialist_prompt.index(
        SPECIALIST_EVIDENCE_HEADER
    )
    assert "Postgres 14" in specialist_prompt
    assert "The user's message, again" not in specialist_prompt  # no recall footer

    # The persisted task never carries the digest.
    assert chat.last_turn.consultation.task == "Explain APR."

    # The reply's prompt: question, then consultation block, then recall block.
    reply_prompt = provider.calls[6].messages[-1].content
    assert reply_prompt.startswith("what does APR mean for our billing?")
    assert (
        reply_prompt.index(CONSULTATION_HEADER)
        < reply_prompt.index(RECALL_HEADER)
        < reply_prompt.rindex("what does APR mean for our billing?")
    )

    meta = chat.last_turn.message.metadata
    assert meta.get("subagent") is not None
    assert meta.get("recall") is not None
    assert chat.last_turn.consultation is not None
    assert chat.last_turn.recall is not None


async def test_a_failed_recall_still_leaves_a_consultation(store):
    embedder = HashingEmbedder()
    group = await make_indexed_group(store, embedder)
    provider = scripted_provider(*_DELEGATION_SCRIPT, "the reply")
    registry = _registry_with(provider)
    chat = ChatService(
        registry,
        store=store,
        delegator=DelegationService(registry),
        # A zero deadline: recall gives up before its first call and returns None.
        retriever=await _retriever(registry, store, embedder, timeout=0.0),
    )
    conv = Conversation(group_id=group.id)

    async for _ in chat.stream_reply(conv, "what does APR mean?"):
        pass

    assert chat.last_turn.recall is None
    assert chat.last_turn.consultation is not None
    assert chat.last_turn.message.metadata.get("subagent") is not None
    assert "recall" not in chat.last_turn.message.metadata


async def test_a_failed_consultation_still_leaves_a_recall(store):
    embedder = HashingEmbedder()
    group = await make_indexed_group(
        store, embedder, facts=_facts("The team runs Postgres 14 for billing.")
    )
    provider = scripted_provider(*_RECALL_SCRIPT, "the reply")
    registry = _registry_with(provider)
    chat = ChatService(
        registry,
        store=store,
        delegator=DelegationService(registry, timeout=0.0),
        retriever=await _retriever(registry, store, embedder),
    )
    conv = Conversation(group_id=group.id)

    async for _ in chat.stream_reply(conv, "what does APR mean for our billing?"):
        pass

    assert chat.last_turn.consultation is None
    assert chat.last_turn.recall is not None
    assert chat.last_turn.message.metadata.get("recall") is not None
    assert "subagent" not in chat.last_turn.message.metadata


async def test_a_window_that_fits_the_consultation_but_not_the_digest(store):
    embedder = HashingEmbedder()
    group = await make_indexed_group(
        store, embedder, facts=_facts("Postgres notes. " * 340)  # ~5400 chars
    )
    provider = scripted_provider(
        *_RECALL_SCRIPT,
        "ask_bank",
        "Explain APR.",
        "y" * 400,
        "the reply",
        info=ModelInfo(id="tiny", name="Tiny", context_window=2000),
    )
    registry = _registry_with(provider)
    chat = ChatService(
        registry,
        store=store,
        delegator=DelegationService(registry),
        retriever=await _retriever(registry, store, embedder),
    )
    conv = Conversation(group_id=group.id)

    async for _ in chat.stream_reply(conv, "what does APR mean?"):
        pass

    reply_prompt = provider.calls[-1].messages[-1].content
    assert "what does APR mean?" in reply_prompt
    assert CONSULTATION_HEADER in reply_prompt
    assert RECALL_HEADER not in reply_prompt

    meta = chat.last_turn.message.metadata
    assert meta.get("subagent") is not None
    assert "recall" not in meta
    assert chat.last_turn.consultation is not None
    assert chat.last_turn.recall is None


async def test_a_window_that_fits_neither_still_sends_the_question(store):
    embedder = HashingEmbedder()
    group = await make_indexed_group(store, embedder)
    provider = scripted_provider(
        *_RECALL_SCRIPT,
        "ask_bank",
        "Explain APR.",
        "y" * 4000,
        "the reply",
        info=ModelInfo(id="tiny", name="Tiny", context_window=600),
    )
    registry = _registry_with(provider)
    chat = ChatService(
        registry,
        store=store,
        delegator=DelegationService(registry),
        retriever=await _retriever(registry, store, embedder),
    )
    conv = Conversation(group_id=group.id)

    async for _ in chat.stream_reply(conv, "what does APR mean?"):
        pass

    reply_prompt = provider.calls[-1].messages[-1].content
    assert "what does APR mean?" in reply_prompt
    assert CONSULTATION_HEADER not in reply_prompt
    assert RECALL_HEADER not in reply_prompt

    meta = chat.last_turn.message.metadata
    assert "subagent" not in meta
    assert "recall" not in meta


# -- the switch matrix -----------------------------------------------------


async def test_subagents_off_recall_on(store):
    embedder = HashingEmbedder()
    group = await make_indexed_group(
        store, embedder, facts=_facts("The team runs Postgres 14 for billing.")
    )
    provider = scripted_provider(*_RECALL_SCRIPT, "the reply")
    registry = _registry_with(provider)
    chat = ChatService(
        registry,
        store=store,
        delegator=None,
        retriever=await _retriever(registry, store, embedder),
    )
    conv = Conversation(group_id=group.id)

    async for _ in chat.stream_reply(conv, "what does APR mean for our billing?"):
        pass

    assert len(provider.calls) == 4  # gate, rewrite, judge, chat — no route
    assert chat.last_turn.recall is not None
    assert chat.last_turn.consultation is None


async def test_recall_off_subagents_on(store):
    provider = scripted_provider(*_DELEGATION_SCRIPT, "the reply")
    registry = _registry_with(provider)
    chat = ChatService(
        registry,
        store=store,
        delegator=DelegationService(registry),
        retriever=None,
    )
    conv = Conversation(group_id="default")

    async for _ in chat.stream_reply(conv, "what does APR mean?"):
        pass

    assert len(provider.calls) == 4  # route, task, specialist, chat — no gate
    assert chat.last_turn.consultation is not None
    assert chat.last_turn.recall is None


async def test_both_off_is_a_plain_turn(store):
    provider = scripted_provider("the reply")
    registry = _registry_with(provider)
    chat = ChatService(registry, store=store, delegator=None, retriever=None)
    conv = Conversation(group_id="default")

    chunks = [c async for c in chat.stream_reply(conv, "hello")]

    assert len(provider.calls) == 1
    assert "".join(chunks) == "the reply"
