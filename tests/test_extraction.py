"""Conversation summarisation: prompts, transcript rendering, keyword
parsing, and the two-call extraction pipeline."""

from __future__ import annotations

import asyncio
import string
from collections.abc import AsyncIterator, Sequence

import pytest

from agentchat.core.chat import ChatService
from agentchat.core.errors import ExtractionError, ProviderError
from agentchat.core.extraction import ExtractionService
from agentchat.core.models import Conversation, Message
from agentchat.core.prompts import (
    ELISION,
    KEYWORDS_PROMPT,
    KEYWORDS_SYSTEM,
    SUMMARY_PROMPT,
    SUMMARY_SYSTEM,
    parse_keywords,
    render_transcript,
)
from agentchat.llm.base import GenerationOptions, ModelInfo, complete
from agentchat.llm.registry import ModelRegistry
from factories import make_conversation, scripted_provider

# -- prompts ----------------------------------------------------------------


def _format_fields(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def test_summary_prompt_has_exactly_the_transcript_field():
    assert _format_fields(SUMMARY_PROMPT) == {"transcript"}


def test_keywords_prompt_has_exactly_the_summary_field():
    assert _format_fields(KEYWORDS_PROMPT) == {"summary"}


def test_summary_system_prompt_weights_user_turns_over_assistant_turns():
    # R1's one load-bearing instruction, pinned so a later reword cannot
    # silently drop it.
    assert "weight" in SUMMARY_SYSTEM.lower()
    assert "user" in SUMMARY_SYSTEM.lower()


def test_keywords_prompt_asks_for_at_most_five():
    assert "5" in KEYWORDS_SYSTEM


# -- render_transcript --------------------------------------------------


def test_render_transcript_labels_turns_and_omits_system_messages():
    messages = [
        Message(role="system", content="be nice"),
        Message(role="user", content="hello"),
        Message(role="assistant", content="hi there"),
    ]
    rendered = render_transcript(messages, budget=1000)
    assert "be nice" not in rendered
    assert "User: hello" in rendered
    assert "Assistant: hi there" in rendered


def test_render_transcript_caps_long_assistant_turns_but_not_user_turns():
    long_reply = "x" * 5000
    long_question = "y" * 5000
    messages = [
        Message(role="user", content=long_question),
        Message(role="assistant", content=long_reply),
    ]
    rendered = render_transcript(messages, budget=100_000)
    assert long_question in rendered
    assert long_reply not in rendered
    assert "x" * 800 in rendered
    assert "…" in rendered


def test_render_transcript_over_budget_drops_assistant_before_user_and_elides_once():
    messages = [
        Message(role="user", content="q" * 100),
        Message(role="assistant", content="a" * 100),
        Message(role="user", content="q" * 100),
        Message(role="assistant", content="a" * 100),
        Message(role="user", content="the latest question"),
    ]
    rendered = render_transcript(messages, budget=30)
    assert rendered.count(ELISION) == 1
    assert "the latest question" in rendered


def test_render_transcript_truncates_rather_than_drops_the_last_user_turn():
    messages = [Message(role="user", content="q" * 10_000)]
    rendered = render_transcript(messages, budget=10)
    assert rendered.strip()
    assert "User:" in rendered
    assert len(rendered) < 10_000


# -- parse_keywords -------------------------------------------------------


def test_parse_keywords_splits_on_semicolons():
    assert parse_keywords("a; b; c") == ("a", "b", "c")


def test_parse_keywords_drops_a_leading_label():
    assert parse_keywords("Keywords: alpha; beta") == ("alpha", "beta")


def test_parse_keywords_without_semicolons_takes_the_first_line_only():
    assert parse_keywords("- a\n- b") == ("a",)


def test_parse_keywords_caps_at_five():
    assert parse_keywords("a; b; c; d; e; f; g") == ("a", "b", "c", "d", "e")


def test_parse_keywords_dedupes_case_insensitively_keeping_first_spelling():
    assert parse_keywords("Alpha; alpha; ALPHA") == ("Alpha",)


def test_parse_keywords_on_empty_and_unparsable_text_does_not_raise():
    assert parse_keywords("") == ()
    assert parse_keywords("I'm sorry, I can't help.") == ("I'm sorry, I can't help",)


# -- complete() -------------------------------------------------------------


async def test_complete_joins_a_multi_chunk_stream_into_one_string():
    provider = scripted_provider("one two three")
    text = await complete(provider, [Message(role="user", content="hi")])
    assert len(provider.calls[0].messages) == 1
    assert text == "one two three"


# -- ExtractionService --------------------------------------------------


class _FailingProvider:
    """Always loaded; raises `ProviderError` from every `generate` call."""

    def __init__(self) -> None:
        self.info = ModelInfo(id="failing", name="Failing", context_window=4096)
        self.is_loaded = True

    async def load(self) -> None:
        return None

    async def unload(self) -> None:
        return None

    async def generate(
        self, messages: Sequence[Message], options: GenerationOptions | None = None
    ) -> AsyncIterator[str]:
        raise ProviderError("boom")
        yield ""  # pragma: no cover — makes this an async generator


def _registry_with(provider) -> ModelRegistry:
    registry = ModelRegistry()
    registry.register(provider.info, lambda: provider)
    return registry


def _conversation() -> Conversation:
    return make_conversation(
        messages=[
            Message(role="user", content="Where should I go on holiday?"),
            Message(role="assistant", content="Try Kyoto in the spring."),
        ]
    )


async def test_run_makes_exactly_two_calls_in_order():
    provider = scripted_provider("A dense summary.", "kyoto; travel")
    service = ExtractionService(_registry_with(provider))

    await service.run(_conversation())

    assert len(provider.calls) == 2


async def test_call_one_carries_the_transcript_call_two_carries_only_the_summary():
    provider = scripted_provider("A dense summary about Kyoto.", "kyoto; travel")
    service = ExtractionService(_registry_with(provider))

    await service.run(_conversation())

    first_user = provider.calls[0].messages[-1].content
    assert "Where should I go on holiday?" in first_user

    second_user = provider.calls[1].messages[-1].content
    assert "A dense summary about Kyoto." in second_user
    assert "Where should I go on holiday?" not in second_user


async def test_both_calls_use_temperature_zero_and_thinking_off():
    provider = scripted_provider("A dense summary.", "kyoto; travel")
    service = ExtractionService(_registry_with(provider))

    await service.run(_conversation())

    for call in provider.calls:
        assert call.options is not None
        assert call.options.temperature == 0.0
        assert call.options.thinking is False


async def test_returned_summary_carries_group_id_watermark_and_model_id():
    provider = scripted_provider("A dense summary.", "kyoto; travel")
    service = ExtractionService(_registry_with(provider))
    conversation = _conversation()

    summary = await service.run(conversation)

    assert summary.group_id == conversation.group_id
    assert summary.covered_messages == len(conversation.messages)
    assert summary.model_id == provider.info.id


async def test_a_second_reply_of_six_keywords_is_capped_at_five():
    provider = scripted_provider("A dense summary.", "Keywords: a; b; c; d; e; f")
    service = ExtractionService(_registry_with(provider))

    summary = await service.run(_conversation())

    assert summary.keywords == ("a", "b", "c", "d", "e")


async def test_a_second_reply_the_parser_rejects_still_yields_a_summary():
    provider = scripted_provider("A dense summary.", "")
    service = ExtractionService(_registry_with(provider))

    summary = await service.run(_conversation())

    assert summary.summary == "A dense summary."
    assert summary.keywords == ()


async def test_a_whitespace_only_first_reply_raises_extraction_error():
    provider = scripted_provider("   \n  ", "kyoto")
    service = ExtractionService(_registry_with(provider))

    with pytest.raises(ExtractionError):
        await service.run(_conversation())


async def test_a_provider_error_propagates_and_nothing_is_returned():
    service = ExtractionService(_registry_with(_FailingProvider()))

    with pytest.raises(ProviderError):
        await service.run(_conversation())


async def test_a_long_conversation_still_fits_a_narrow_context_window():
    narrow = ModelInfo(id="mock-small", name="Mock Small", context_window=2048)
    provider = scripted_provider("A dense summary.", "kyoto", info=narrow)
    service = ExtractionService(_registry_with(provider))
    conversation = make_conversation(
        messages=[
            Message(role="user" if i % 2 == 0 else "assistant", content="word " * 200)
            for i in range(40)
        ]
    )

    await service.run(conversation)

    from agentchat.core.context import estimate_tokens

    first_call_transcript = provider.calls[0].messages[-1].content
    assert estimate_tokens(first_call_transcript) < provider.info.context_window


# -- ChatService.summarise() -------------------------------------------------


async def test_summarise_with_no_messages_returns_none_and_stores_nothing(store):
    provider = scripted_provider("A dense summary.", "kyoto")
    registry = _registry_with(provider)
    chat = ChatService(registry, store=store, extractor=ExtractionService(registry))
    conversation = await chat.new_conversation()

    result = await chat.summarise(conversation)

    assert result is None
    assert await store.summary(conversation.id) is None


async def test_summarise_stores_a_row_readable_through_the_store(store):
    provider = scripted_provider("A dense summary.", "kyoto; travel")
    registry = _registry_with(provider)
    chat = ChatService(registry, store=store, extractor=ExtractionService(registry))
    conversation = _conversation()
    await store.save(conversation)

    result = await chat.summarise(conversation)

    assert result is not None
    loaded = await store.summary(conversation.id)
    assert loaded is not None
    assert loaded.summary == "A dense summary."
    assert loaded.keywords == ("kyoto", "travel")


async def test_summarise_called_twice_with_no_new_messages_runs_the_extractor_once(store):
    provider = scripted_provider("A dense summary.", "kyoto; travel")
    registry = _registry_with(provider)
    chat = ChatService(registry, store=store, extractor=ExtractionService(registry))
    conversation = _conversation()
    await store.save(conversation)

    first = await chat.summarise(conversation)
    assert first is not None
    assert len(provider.calls) == 2  # summary + keywords, once

    second = await chat.summarise(conversation)
    assert second is None
    assert len(provider.calls) == 2  # unchanged: the watermark skipped it


async def test_summarise_after_a_new_message_overwrites_and_keeps_created_at(store):
    provider = scripted_provider(
        "A dense summary.", "kyoto; travel", "An updated summary.", "kyoto; onsen"
    )
    registry = _registry_with(provider)
    chat = ChatService(registry, store=store, extractor=ExtractionService(registry))
    conversation = _conversation()
    await store.save(conversation)

    first = await chat.summarise(conversation)
    assert first is not None

    conversation.add(Message(role="user", content="Any food recs?"))
    await store.save(conversation)
    second = await chat.summarise(conversation)

    assert second is not None
    assert second.covered_messages == 3
    assert second.created_at == first.created_at
    loaded = await store.summary(conversation.id)
    assert loaded is not None
    assert loaded.summary == "An updated summary."


async def test_summarise_provider_error_leaves_no_row_and_propagates(store):
    registry = _registry_with(_FailingProvider())
    chat = ChatService(registry, store=store, extractor=ExtractionService(registry))
    conversation = _conversation()
    await store.save(conversation)

    with pytest.raises(ProviderError):
        await chat.summarise(conversation)

    assert await store.summary(conversation.id) is None


async def test_summarise_with_no_extractor_returns_none(store):
    registry = _registry_with(scripted_provider("A dense summary.", "kyoto"))
    chat = ChatService(registry, store=store, extractor=None)
    conversation = _conversation()
    await store.save(conversation)

    result = await chat.summarise(conversation)

    assert result is None
    assert await store.summary(conversation.id) is None


async def test_cancelling_stream_reply_mid_stream_releases_the_provider_lock(store):
    provider = scripted_provider(
        "a longer streaming reply with several words in it",
        "A dense summary.",
        "kyoto",
        chunk_delay=0.03,
    )
    registry = _registry_with(provider)
    chat = ChatService(registry, store=store, extractor=ExtractionService(registry))
    conversation = await chat.new_conversation()

    async def consume():
        async for _ in chat.stream_reply(conversation, "hi"):
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not chat._provider_lock.locked()

    summarising = _conversation()
    await store.save(summarising)
    result = await chat.summarise(summarising)
    assert result is not None


async def test_summarise_and_stream_reply_do_not_interleave_on_the_provider(store):
    provider = scripted_provider(
        "a streaming reply with several words",
        "A dense summary.",
        "kyoto",
        chunk_delay=0.02,
    )
    registry = _registry_with(provider)
    chat = ChatService(registry, store=store, extractor=ExtractionService(registry))
    talking = await chat.new_conversation()
    summarising = _conversation()
    await store.save(summarising)

    async def consume():
        async for _ in chat.stream_reply(talking, "hi"):
            pass

    stream_task = asyncio.create_task(consume())
    await asyncio.sleep(0.005)
    summarise_task = asyncio.create_task(chat.summarise(summarising))

    await asyncio.gather(stream_task, summarise_task)

    intervals = sorted(
        (call.started, call.finished) for call in provider.calls if call.finished is not None
    )
    for (_start1, end1), (start2, _end2) in zip(intervals, intervals[1:]):
        assert end1 <= start2, "two provider calls overlapped"
