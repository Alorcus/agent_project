"""Grounded fact extraction: the fact/quote prompts, the sliding window's pure
arithmetic, one window's extraction, and the cadence that ties it to a
conversation's watermark."""

from __future__ import annotations

import string
from collections.abc import AsyncIterator, Sequence

import pytest

from agentchat.core.anchoring import anchor_in
from agentchat.core.chat import ChatService
from agentchat.core.errors import FactExtractionError, ProviderError
from agentchat.core.facts import FactExtractor, countable, windows
from agentchat.core.models import Author, Fact, Message, Phrase
from agentchat.core.prompts import (
    FACT_PROMPT,
    FACT_SYSTEM,
    MAX_QUOTES,
    QUOTES_PROMPT,
    QUOTES_SYSTEM,
    parse_quotes,
    render_window,
)
from agentchat.llm.base import GenerationOptions, ModelInfo
from agentchat.llm.registry import ModelRegistry
from factories import make_conversation, scripted_provider

# -- prompts ------------------------------------------------------------


def _format_fields(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def test_fact_prompt_has_exactly_the_text_field():
    assert _format_fields(FACT_PROMPT) == {"text"}


def test_quotes_prompt_has_exactly_text_and_fact_fields():
    assert _format_fields(QUOTES_PROMPT) == {"text", "fact"}


def test_fact_system_has_no_sentinel_or_example_answering_one():
    lower = FACT_SYSTEM.lower()
    for banned in ("none", "n/a", "not stated", "no fact"):
        assert banned not in lower


def test_neither_quote_prompt_asks_for_a_position():
    for text in (QUOTES_SYSTEM, QUOTES_PROMPT):
        lower = text.lower()
        for banned in ("index", "offset", "position", "character", "line number"):
            assert banned not in lower


def test_parse_quotes_on_a_clean_two_line_reply():
    assert parse_quotes("first quote here\nsecond quote here") == (
        "first quote here",
        "second quote here",
    )


def test_parse_quotes_strips_bullets():
    assert parse_quotes("- first quote\n- second quote") == ("first quote", "second quote")


def test_parse_quotes_strips_a_quotes_label():
    assert parse_quotes("Quotes:\nfirst quote\nsecond quote") == (
        "first quote",
        "second quote",
    )


def test_parse_quotes_on_empty_input():
    assert parse_quotes("") == ()


def test_parse_quotes_caps_at_the_limit():
    text = "\n".join(f"quote number {i}" for i in range(10))
    assert len(parse_quotes(text)) == MAX_QUOTES


def test_parse_quotes_keeps_a_trailing_period_unlike_parse_keywords():
    assert parse_quotes("It's already done.") == ("It's already done.",)


def test_parse_quotes_drops_duplicates():
    assert parse_quotes("same quote\nsame quote") == ("same quote",)


def test_render_window_does_not_cap_a_long_assistant_turn_within_budget():
    long_reply = "x" * 5000
    rendered = render_window([Message(role="assistant", content=long_reply)], budget=100_000)
    assert long_reply in rendered
    assert "…" not in rendered


def test_render_window_drops_a_long_turn_whole_instead_of_truncating():
    long_reply = "x" * 5000
    messages = [
        Message(role="user", content="short question"),
        Message(role="assistant", content=long_reply),
        Message(role="user", content="the latest question"),
    ]
    rendered = render_window(messages, budget=50)
    assert long_reply not in rendered
    assert "x" * 800 not in rendered
    assert "the latest question" in rendered


# -- countable ------------------------------------------------------------


def test_countable_drops_system_and_empty_messages():
    messages = [
        Message(role="system", content="be nice"),
        Message(role="user", content="hi"),
        Message(role="assistant", content=""),
        Message(role="assistant", content="hello"),
    ]
    result = countable(messages)
    assert [(m.role, m.content) for m in result] == [("user", "hi"), ("assistant", "hello")]


def test_countable_keeps_an_assistant_reply_carrying_subagent_metadata():
    reply = Message(
        role="assistant",
        content="Sear the steak for two minutes per side.",
        metadata={
            "subagent": {
                "id": "ask_chef",
                "name": "Chef",
                "task": "t",
                "answer": "a",
                "trigger": "@ask_chef",
            }
        },
    )
    messages = [Message(role="user", content="How do I sear a steak?"), reply]
    assert countable(messages) == messages


# -- windows ----------------------------------------------------------------


def test_windows_0_5_is_empty():
    assert windows(0, 5) == ()


def test_windows_0_6_is_one_window():
    assert windows(0, 6) == ((0, 6),)


def test_windows_0_9_is_one_window():
    assert windows(0, 9) == ((0, 6),)


def test_windows_6_10_resumes_two_before_covered_r8():
    assert windows(6, 10) == ((4, 10),)


def test_windows_6_7_is_empty_without_flush_and_short_with_flush_r7():
    assert windows(6, 7) == ()
    assert windows(6, 7, flush=True) == ((4, 7),)


def test_windows_6_6_flush_is_empty():
    assert windows(6, 6, flush=True) == ()


def test_windows_0_13_flush_drains_a_backlog_oldest_first():
    assert windows(0, 13, flush=True) == ((0, 6), (4, 10), (8, 13))


# -- FactExtractor.extract, ScriptedProvider ---------------------------------


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


def _window() -> list[Message]:
    return [
        Message(
            role="user",
            id="u1",
            content="We're moving staging to a managed Kubernetes offering next month.",
        ),
        Message(role="assistant", id="a1", content="Which provider are you leaning toward?"),
        Message(
            role="user",
            id="u2",
            content="Probably GKE, since we already use BigQuery.",
        ),
    ]


async def test_extract_happy_path_phrases_text_matches_the_real_messages():
    provider = scripted_provider(
        "The team is moving staging to a managed Kubernetes offering, likely GKE.",
        "moving staging to a managed Kubernetes offering\nProbably GKE, since we already use BigQuery",
    )
    extractor = FactExtractor(_registry_with(provider))
    messages = _window()

    fact = await extractor.extract(messages)

    assert fact is not None
    assert len(provider.calls) == 2
    assert fact.phrases
    for phrase in fact.phrases:
        message = next(m for m in messages if m.id == phrase.message_id)
        assert phrase.text(message) in message.content


async def test_empty_fact_reply_yields_none_after_exactly_one_call():
    provider = scripted_provider("   ")
    extractor = FactExtractor(_registry_with(provider))

    fact = await extractor.extract(_window())

    assert fact is None
    assert len(provider.calls) == 1


async def test_invented_quotes_yield_none_r2():
    provider = scripted_provider(
        "The team is adopting Rust for the backend.",
        "We are completely rewriting everything in Rust from scratch next quarter",
    )
    extractor = FactExtractor(_registry_with(provider))

    fact = await extractor.extract(_window())

    assert fact is None


async def test_a_long_fact_quoting_only_a_thin_fragment_fails_coverage_r10():
    provider = scripted_provider(
        "The team is moving its staging environment to a managed Kubernetes "
        "offering, likely GKE, because they already rely heavily on BigQuery "
        "for analytics and want tight integration.",
        "GKE",
    )
    extractor = FactExtractor(_registry_with(provider))

    fact = await extractor.extract(_window())

    assert fact is None


async def test_quote_differing_by_case_and_whitespace_still_anchors_with_original_casing():
    messages = [
        Message(
            role="user",
            id="u1",
            content="Our STAGING\nenvironment moves to GKE next month.",
        )
    ]
    provider = scripted_provider(
        "Staging moves to GKE next month.",
        "staging environment moves to gke",
    )
    extractor = FactExtractor(_registry_with(provider))

    fact = await extractor.extract(messages)

    assert fact is not None
    assert len(fact.phrases) == 1
    assert fact.phrases[0].text(messages[0]) == "STAGING\nenvironment moves to GKE"


async def test_a_window_of_only_questions_yields_a_thin_but_grounded_fact():
    messages = [
        Message(
            role="user",
            id="u1",
            content="Should we use Postgres or MySQL for the new service?",
        ),
        Message(
            role="assistant",
            id="a1",
            content="Do you have a preference already, or is this fully open?",
        ),
    ]
    provider = scripted_provider(
        "The team is deciding whether to use Postgres or MySQL for a new service.",
        "Should we use Postgres or MySQL for the new service",
    )
    extractor = FactExtractor(_registry_with(provider))

    fact = await extractor.extract(messages)

    assert fact is not None


async def test_fact_quoting_user_and_assistant_carries_both_author_kinds():
    messages = [
        Message(
            role="user", id="u1", content="We're moving staging to GKE next month."
        ),
        Message(
            role="assistant",
            id="a1",
            model_id="mock-small",
            content="Sounds reasonable given your BigQuery usage.",
        ),
    ]
    provider = scripted_provider(
        "The team is moving staging to GKE next month, which fits their BigQuery usage.",
        "moving staging to GKE next month\nSounds reasonable given your BigQuery usage",
    )
    extractor = FactExtractor(_registry_with(provider))

    fact = await extractor.extract(messages)

    assert fact is not None
    kinds = {phrase.author.kind for phrase in fact.phrases}
    assert kinds == {"user", "model"}
    labels = {phrase.author.label for phrase in fact.phrases}
    assert "user" in labels
    assert "mock-small" in labels


async def test_backend_error_surfaces_as_fact_extraction_error():
    extractor = FactExtractor(_registry_with(_FailingProvider()))

    with pytest.raises(FactExtractionError):
        await extractor.extract(_window())


# -- R9: a specialist's answer is never rendered or anchorable --------------


async def test_a_subagent_answer_is_in_no_rendered_window_and_anchors_nowhere():
    secret = "The secret ingredient is smoked paprika, never mentioned elsewhere."
    reply = Message(
        role="assistant",
        content="Try smoking it low and slow.",
        metadata={
            "subagent": {
                "id": "ask_chef",
                "name": "Chef",
                "task": "t",
                "answer": secret,
                "trigger": "@ask_chef",
            }
        },
    )
    messages = [Message(role="user", content="How do I smoke a brisket?"), reply]
    window = countable(messages)

    rendered = render_window(window, budget=10_000)

    assert secret not in rendered
    assert anchor_in(secret, window) is None


# -- ChatService.extract_facts: the cadence ----------------------------------


def _kyoto_message(i: int) -> Message:
    role = "user" if i % 2 == 0 else "assistant"
    return Message(
        role=role,
        content=f"Message {i} about the Kyoto trip and Kyoto trip planning.",
    )


def _kyoto_provider(n_windows: int = 10) -> "object":
    return scripted_provider(*(["The trip involves Kyoto.", "Kyoto trip"] * n_windows))


async def test_six_messages_yield_one_fact_and_watermark_six(store):
    provider = _kyoto_provider()
    registry = _registry_with(provider)
    chat = ChatService(registry, store=store, fact_extractor=FactExtractor(registry))
    conversation = make_conversation(messages=[_kyoto_message(i) for i in range(6)])
    await store.save(conversation)

    facts = await chat.extract_facts(conversation)

    assert len(facts) == 1
    assert await store.fact_watermark(conversation.id) == 6
    assert len(provider.calls) == 2


async def test_five_messages_make_no_provider_call(store):
    provider = _kyoto_provider()
    registry = _registry_with(provider)
    chat = ChatService(registry, store=store, fact_extractor=FactExtractor(registry))
    conversation = make_conversation(messages=[_kyoto_message(i) for i in range(5)])
    await store.save(conversation)

    facts = await chat.extract_facts(conversation)

    assert facts == ()
    assert len(provider.calls) == 0


async def test_ten_messages_yield_two_facts_from_the_two_windows(store):
    provider = _kyoto_provider()
    registry = _registry_with(provider)
    chat = ChatService(registry, store=store, fact_extractor=FactExtractor(registry))
    conversation = make_conversation(messages=[_kyoto_message(i) for i in range(10)])
    await store.save(conversation)

    facts = await chat.extract_facts(conversation)

    assert [(f.window_start, f.window_end) for f in facts] == [(0, 6), (4, 10)]
    assert await store.fact_watermark(conversation.id) == 10


async def test_a_barren_window_advances_the_watermark_and_a_rerun_makes_no_call(store):
    # Invented text never anchors in the Kyoto messages, so the window is
    # barren — but the watermark still moves past it.
    provider = scripted_provider(
        "Nothing to see here about unrelated matters.", "completely invented text"
    )
    registry = _registry_with(provider)
    chat = ChatService(registry, store=store, fact_extractor=FactExtractor(registry))
    conversation = make_conversation(messages=[_kyoto_message(i) for i in range(6)])
    await store.save(conversation)

    facts = await chat.extract_facts(conversation)
    assert facts == ()
    assert await store.fact_watermark(conversation.id) == 6

    facts_again = await chat.extract_facts(conversation)
    assert facts_again == ()
    assert len(provider.calls) == 2  # unchanged: the watermark skipped the rerun


async def test_flush_true_extracts_the_partial_window_left_after_a_full_one(store):
    provider = _kyoto_provider()
    registry = _registry_with(provider)
    chat = ChatService(registry, store=store, fact_extractor=FactExtractor(registry))
    conversation = make_conversation(messages=[_kyoto_message(i) for i in range(6)])
    await store.save(conversation)
    await chat.extract_facts(conversation)
    assert await store.fact_watermark(conversation.id) == 6

    conversation.add(
        Message(role="user", content="One more note about the Kyoto trip.")
    )
    await store.save(conversation)

    facts = await chat.extract_facts(conversation, flush=True)

    assert [(f.window_start, f.window_end) for f in facts] == [(4, 7)]
    assert await store.fact_watermark(conversation.id) == 7

    facts_again = await chat.extract_facts(conversation, flush=True)
    assert facts_again == ()
    assert len(provider.calls) == 4  # unchanged by the second, empty flush


async def test_reopening_after_watermark_six_resumes_at_four_r8(store):
    provider = _kyoto_provider()
    registry = _registry_with(provider)
    chat = ChatService(registry, store=store, fact_extractor=FactExtractor(registry))
    conversation = make_conversation(messages=[_kyoto_message(i) for i in range(6)])
    await store.save(conversation)
    await chat.extract_facts(conversation)
    assert len(provider.calls) == 2

    for i in range(6, 10):
        conversation.add(_kyoto_message(i))
    await store.save(conversation)

    facts = await chat.extract_facts(conversation)

    assert [(f.window_start, f.window_end) for f in facts] == [(4, 10)]
    assert len(provider.calls) == 4


async def test_a_second_run_never_rewrites_the_first_run_s_facts_r11(store):
    provider = _kyoto_provider()
    registry = _registry_with(provider)
    chat = ChatService(registry, store=store, fact_extractor=FactExtractor(registry))
    conversation = make_conversation(messages=[_kyoto_message(i) for i in range(10)])
    await store.save(conversation)

    first = await chat.extract_facts(conversation)
    assert len(first) == 2

    second = await chat.extract_facts(conversation)
    assert second == ()

    stored = await store.list_facts(conversation.group_id)
    assert len(stored) == 2


async def test_a_consulted_conversation_advances_by_visible_turns_only_r9(store):
    provider = _kyoto_provider()
    registry = _registry_with(provider)
    chat = ChatService(registry, store=store, fact_extractor=FactExtractor(registry))
    messages = [_kyoto_message(i) for i in range(5)]
    messages.append(
        Message(
            role="assistant",
            content=_kyoto_message(5).content,
            metadata={
                "subagent": {
                    "id": "ask_chef",
                    "name": "Chef",
                    "task": "t",
                    "answer": "A secret answer never shown to the extractor.",
                    "trigger": "@ask_chef",
                }
            },
        )
    )
    conversation = make_conversation(messages=messages)
    await store.save(conversation)

    facts = await chat.extract_facts(conversation)

    assert len(facts) == 1
    assert await store.fact_watermark(conversation.id) == 6
    for fact in await store.list_facts(conversation.group_id):
        assert "secret answer" not in fact.text.lower()


async def test_fact_extractor_none_touches_neither_provider_nor_store_r14(store):
    provider = _kyoto_provider()
    registry = _registry_with(provider)
    chat = ChatService(registry, store=store, fact_extractor=None)
    conversation = make_conversation(messages=[_kyoto_message(i) for i in range(6)])
    await store.save(conversation)

    facts = await chat.extract_facts(conversation)

    assert facts == ()
    assert len(provider.calls) == 0
    assert await store.fact_watermark(conversation.id) == 0


async def test_pending_fact_windows_is_a_provider_free_probe(store):
    provider = _kyoto_provider()
    registry = _registry_with(provider)
    chat = ChatService(registry, store=store, fact_extractor=FactExtractor(registry))
    conversation = make_conversation(messages=[_kyoto_message(i) for i in range(6)])
    await store.save(conversation)

    pending = await chat.pending_fact_windows(conversation)

    assert pending == ((0, 6),)
    assert len(provider.calls) == 0
