"""Message enrichment: the injected-text wrapper, keyword matching and the
session ledger, `ChatService`'s enrich → build → fallback → mark turn, and the
UI's expand-on-click note under the user bubble."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from textual.widgets import Input

from agentchat.core.chat import ChatService
from agentchat.core.enrichment import MemoryEnricher, matches
from agentchat.core.models import DEFAULT_GROUP_ID
from agentchat.core.prompts import ENRICHMENT_HEADER, enriched_text
from agentchat.llm.base import ModelInfo
from agentchat.llm.registry import ModelRegistry
from agentchat.storage.sqlite import SqliteStore
from agentchat.ui.app import ChatApp
from agentchat.ui.widgets import EnrichmentNote, MessageBubble
from conftest import mock_settings
from factories import make_conversation, make_group, make_summary, scripted_provider

# -- enriched_text ------------------------------------------------------------


def test_enriched_text_with_no_summaries_returns_the_text_unchanged():
    assert enriched_text("hi", []) == "hi"


def test_enriched_text_with_summaries_carries_the_text_the_header_and_both_summaries():
    summaries = [
        make_summary(conversation_id="c1", summary="First summary."),
        make_summary(conversation_id="c2", summary="Second summary."),
    ]

    text = enriched_text("what now?", summaries)

    assert text.startswith("what now?")
    assert "First summary." in text
    assert "Second summary." in text
    assert ENRICHMENT_HEADER in text


def test_enrichment_header_states_the_notes_are_not_the_users_own_words():
    # Pinned so a reword cannot silently turn the injected block into
    # something the model reads as the user's own words.
    assert "earlier conversations" in ENRICHMENT_HEADER
    assert "did not write them" in ENRICHMENT_HEADER


# -- matches --------------------------------------------------------------


def test_matches_is_a_whole_phrase_hit_not_a_substring():
    summary = make_summary(keywords=("S3",))
    assert matches("we deployed to S3 today", summary) is True
    assert matches("the class S3Bucket", summary) is False


def test_matches_handles_a_keyword_that_starts_on_punctuation():
    # `\b` misses this: there is no word boundary between a space and `/`.
    summary = make_summary(keywords=("/upload endpoint",))
    assert matches("the /upload endpoint 500s", summary) is True


def test_matches_is_case_insensitive():
    summary = make_summary(keywords=("Flask",))
    assert matches("debugging flask today", summary) is True


def test_matches_a_keyword_phrase_split_across_a_newline():
    summary = make_summary(keywords=("upload endpoint",))
    assert matches("the upload\nendpoint failed", summary) is True


def test_matches_with_no_keywords_never_matches():
    summary = make_summary(keywords=())
    assert matches("anything at all", summary) is False


# -- MemoryEnricher.select -------------------------------------------------


async def _seed(
    store,
    *,
    group_id: str,
    conversation_id: str,
    keywords: tuple[str, ...] = (),
    summary_text: str = "note",
    updated_at: datetime | None = None,
) -> None:
    """A group, a conversation in it, and a matching summary — the minimum
    the FKs in `conversation_summaries` require."""
    await store.save_group(make_group(id=group_id))
    await store.save(make_conversation(id=conversation_id, group_id=group_id))
    summary = make_summary(
        conversation_id=conversation_id,
        group_id=group_id,
        keywords=keywords,
        summary=summary_text,
    )
    if updated_at is not None:
        summary.updated_at = updated_at
    await store.save_summary(summary)


class _RecordingStore:
    """A `list_summaries` call counter — enough of `ConversationStore` for
    the default-group short-circuit, which must never reach a real store."""

    def __init__(self) -> None:
        self.calls = 0

    async def list_summaries(self, group_id: str):
        self.calls += 1
        return []


async def test_select_in_default_group_returns_nothing_and_queries_no_store():
    recording_store = _RecordingStore()
    enricher = MemoryEnricher(recording_store)
    conversation = make_conversation(group_id=DEFAULT_GROUP_ID)

    result = await enricher.select(conversation, "hello")

    assert result == ()
    assert recording_store.calls == 0


async def test_select_excludes_the_conversations_own_summary(store):
    await _seed(store, group_id="g1", conversation_id="c-self", keywords=("kyoto",))
    enricher = MemoryEnricher(store)
    conversation = make_conversation(id="c-self", group_id="g1")

    result = await enricher.select(conversation, "let's talk about kyoto")

    assert result == ()


async def test_select_returns_only_summaries_of_the_conversations_own_group(store):
    await _seed(store, group_id="g1", conversation_id="c1", keywords=("kyoto",))
    await _seed(store, group_id="g2", conversation_id="c2", keywords=("kyoto",))
    enricher = MemoryEnricher(store)
    conversation = make_conversation(id="c3", group_id="g1")

    result = await enricher.select(conversation, "back to kyoto")

    assert [s.conversation_id for s in result] == ["c1"]


async def test_select_caps_at_three_and_keeps_the_most_recently_updated(store):
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for i in range(5):
        await _seed(
            store,
            group_id="g1",
            conversation_id=f"c{i}",
            keywords=("kyoto",),
            updated_at=base + timedelta(minutes=i),
        )
    enricher = MemoryEnricher(store)
    conversation = make_conversation(id="c-self", group_id="g1")

    result = await enricher.select(conversation, "back to kyoto")

    assert [s.conversation_id for s in result] == ["c4", "c3", "c2"]


async def test_select_after_mark_used_does_not_return_it_again(store):
    await _seed(store, group_id="g1", conversation_id="c1", keywords=("kyoto",))
    enricher = MemoryEnricher(store)
    conversation = make_conversation(id="c-self", group_id="g1")

    first = await enricher.select(conversation, "kyoto")
    assert len(first) == 1
    enricher.mark_used(first)

    second = await enricher.select(conversation, "kyoto")
    assert second == ()


async def test_select_for_a_different_conversation_id_resets_the_ledger(store):
    await _seed(store, group_id="g1", conversation_id="c1", keywords=("kyoto",))
    enricher = MemoryEnricher(store)
    conv_a = make_conversation(id="conv-a", group_id="g1")
    conv_b = make_conversation(id="conv-b", group_id="g1")

    used = await enricher.select(conv_a, "kyoto")
    enricher.mark_used(used)
    assert await enricher.select(conv_a, "kyoto") == ()

    again = await enricher.select(conv_b, "kyoto")
    assert len(again) == 1


async def test_reset_makes_a_used_summary_available_again(store):
    await _seed(store, group_id="g1", conversation_id="c1", keywords=("kyoto",))
    enricher = MemoryEnricher(store)
    conversation = make_conversation(id="c-self", group_id="g1")

    used = await enricher.select(conversation, "kyoto")
    enricher.mark_used(used)
    enricher.reset()

    again = await enricher.select(conversation, "kyoto")
    assert len(again) == 1


# -- ChatService.stream_reply ------------------------------------------------


def _registry_with(provider) -> ModelRegistry:
    registry = ModelRegistry()
    registry.register(provider.info, lambda: provider)
    return registry


def _bare_conversation(conversation_id: str, group_id: str):
    return make_conversation(id=conversation_id, group_id=group_id, messages=[])


async def test_enrichment_never_reaches_the_database(tmp_path: Path):
    path = tmp_path / "chat.db"
    store = SqliteStore(path)
    await _seed(
        store,
        group_id="g1",
        conversation_id="other",
        keywords=("kyoto",),
        summary_text="Notes about Kyoto travel.",
    )
    provider = scripted_provider("a reply")
    chat = ChatService(_registry_with(provider), store=store, enricher=MemoryEnricher(store))
    conversation = _bare_conversation("mine", "g1")

    async for _ in chat.stream_reply(conversation, "let's talk kyoto again"):
        pass

    assert conversation.messages[-2].content == "let's talk kyoto again"

    reloaded = await SqliteStore(path).load(conversation.id)
    assert reloaded is not None
    for message in reloaded.messages:
        assert "Notes about Kyoto travel." not in message.content
        assert "Notes about Kyoto travel." not in json.dumps(message.metadata)


async def test_the_providers_call_carries_both_the_typed_text_and_the_matched_summary(
    store,
):
    await _seed(
        store,
        group_id="g1",
        conversation_id="other",
        keywords=("kyoto",),
        summary_text="Notes about Kyoto travel.",
    )
    provider = scripted_provider("a reply")
    chat = ChatService(_registry_with(provider), store=store, enricher=MemoryEnricher(store))
    conversation = _bare_conversation("mine", "g1")

    async for _ in chat.stream_reply(conversation, "let's talk kyoto again"):
        pass

    sent = provider.calls[0].messages[-1].content
    assert "let's talk kyoto again" in sent
    assert "Notes about Kyoto travel." in sent
    assert len(chat.last_turn.enrichment) == 1
    assert chat.last_turn.enrichment[0].conversation_id == "other"


async def test_no_keyword_hit_sends_plain_text_and_marks_nothing_used(store):
    await _seed(
        store,
        group_id="g1",
        conversation_id="other",
        keywords=("kyoto",),
        summary_text="Notes about Kyoto travel.",
    )
    provider = scripted_provider("a reply")
    chat = ChatService(_registry_with(provider), store=store, enricher=MemoryEnricher(store))
    conversation = _bare_conversation("mine", "g1")

    async for _ in chat.stream_reply(conversation, "totally unrelated text"):
        pass

    assert chat.last_turn.enrichment == ()
    assert provider.calls[0].messages[-1].content == "totally unrelated text"


async def test_chat_service_with_no_enricher_never_enriches(store):
    await _seed(
        store,
        group_id="g1",
        conversation_id="other",
        keywords=("kyoto",),
        summary_text="Notes about Kyoto travel.",
    )
    provider = scripted_provider("a reply")
    chat = ChatService(_registry_with(provider), store=store, enricher=None)
    conversation = _bare_conversation("mine", "g1")

    async for _ in chat.stream_reply(conversation, "kyoto"):
        pass

    assert chat.last_turn.enrichment == ()
    assert provider.calls[0].messages[-1].content == "kyoto"


async def test_a_summary_used_on_turn_one_is_not_appended_on_turn_two(store):
    await _seed(
        store,
        group_id="g1",
        conversation_id="other",
        keywords=("kyoto",),
        summary_text="Notes about Kyoto travel.",
    )
    provider = scripted_provider("reply one", "reply two")
    chat = ChatService(_registry_with(provider), store=store, enricher=MemoryEnricher(store))
    conversation = _bare_conversation("mine", "g1")

    async for _ in chat.stream_reply(conversation, "kyoto plans"):
        pass
    assert len(chat.last_turn.enrichment) == 1

    async for _ in chat.stream_reply(conversation, "more kyoto plans"):
        pass
    assert chat.last_turn.enrichment == ()
    assert "Notes about Kyoto travel." not in provider.calls[1].messages[-1].content


async def test_stream_reply_caps_enrichment_at_three(store):
    for i in range(4):
        await _seed(
            store, group_id="g1", conversation_id=f"c{i}", keywords=("kyoto",), summary_text=f"note {i}"
        )
    provider = scripted_provider("reply")
    chat = ChatService(_registry_with(provider), store=store, enricher=MemoryEnricher(store))
    conversation = _bare_conversation("mine", "g1")

    async for _ in chat.stream_reply(conversation, "kyoto"):
        pass

    assert len(chat.last_turn.enrichment) == 3


async def test_budget_guard_drops_the_enrichment_but_keeps_the_users_own_message(store):
    await _seed(
        store,
        group_id="g1",
        conversation_id="other",
        keywords=("kyoto",),
        summary_text="x" * 2000,
    )
    small = ModelInfo(id="mock-small", name="Mock Small", context_window=600)
    provider = scripted_provider("a reply", info=small)
    enricher = MemoryEnricher(store)
    chat = ChatService(_registry_with(provider), store=store, enricher=enricher)
    conversation = _bare_conversation("mine", "g1")

    async for _ in chat.stream_reply(conversation, "kyoto again?"):
        pass

    sent = provider.calls[0].messages[-1].content
    assert "kyoto again?" in sent
    assert "x" * 2000 not in sent
    assert chat.last_turn.enrichment == ()

    # Nothing was spent: a later turn with room to spare can still use it.
    roomy = ModelInfo(id="mock-large", name="Mock Large", context_window=8192)
    provider2 = scripted_provider("a reply", info=roomy)
    chat2 = ChatService(_registry_with(provider2), store=store, enricher=enricher)
    async for _ in chat2.stream_reply(conversation, "kyoto again?"):
        pass
    assert len(chat2.last_turn.enrichment) == 1


async def test_switch_conversation_resets_the_ledger(store):
    await _seed(
        store,
        group_id="g1",
        conversation_id="other",
        keywords=("kyoto",),
        summary_text="Notes about Kyoto travel.",
    )
    provider = scripted_provider("reply one", "reply two")
    chat = ChatService(_registry_with(provider), store=store, enricher=MemoryEnricher(store))
    mine = _bare_conversation("mine", "g1")
    await store.save(mine)

    async for _ in chat.stream_reply(mine, "kyoto plans"):
        pass
    assert len(chat.last_turn.enrichment) == 1

    reloaded = await chat.switch_conversation("mine")
    async for _ in chat.stream_reply(reloaded, "kyoto plans again"):
        pass
    assert len(chat.last_turn.enrichment) == 1


async def test_cancel_mid_stream_still_spends_the_summary_and_frees_the_lock(store):
    await _seed(
        store,
        group_id="g1",
        conversation_id="other",
        keywords=("kyoto",),
        summary_text="Notes about Kyoto travel.",
    )
    provider = scripted_provider(
        "a longer streaming reply with several words in it", chunk_delay=0.03
    )
    enricher = MemoryEnricher(store)
    chat = ChatService(_registry_with(provider), store=store, enricher=enricher)
    conversation = _bare_conversation("mine", "g1")

    async def consume():
        async for _ in chat.stream_reply(conversation, "kyoto plans"):
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not chat._provider_lock.locked()
    assert len(chat.last_turn.enrichment) == 1
    assert await chat.enricher.select(conversation, "kyoto plans") == ()


# -- the UI note --------------------------------------------------------


async def _submit(pilot, text: str) -> None:
    prompt = pilot.app.query_one("#prompt", Input)
    prompt.value = text
    await pilot.press("enter")


async def _wait_until_done(pilot, app) -> None:
    for _ in range(200):
        await pilot.pause()
        if not app._generating:
            break
        await asyncio.sleep(0.05)


async def _seed_app(app, *, group_id: str, conversation_id: str, keywords, summary_text: str):
    await app.chat.store.save_group(make_group(id=group_id))
    await app.chat.store.save(make_conversation(id=conversation_id, group_id=group_id))
    await app.chat.store.save_summary(
        make_summary(
            conversation_id=conversation_id,
            group_id=group_id,
            keywords=keywords,
            summary=summary_text,
        )
    )


async def test_a_matching_turn_mounts_exactly_one_collapsed_note():
    app = ChatApp(mock_settings(enrich_messages=True))
    async with app.run_test() as pilot:
        await _seed_app(
            app,
            group_id="g1",
            conversation_id="other",
            keywords=("kyoto",),
            summary_text="Notes about Kyoto travel.",
        )
        app.conversation = await app.chat.new_conversation("g1")

        await _submit(pilot, "let's revisit kyoto")
        await _wait_until_done(pilot, app)

        notes = list(app.query(EnrichmentNote))
        assert len(notes) == 1
        assert str(notes[0].content) == "▸ enriched by 1 memory"
        assert "Notes about Kyoto travel." not in str(notes[0].content)


async def test_clicking_the_note_reveals_the_summary_and_hides_it_again():
    app = ChatApp(mock_settings(enrich_messages=True))
    async with app.run_test() as pilot:
        await _seed_app(
            app,
            group_id="g1",
            conversation_id="other",
            keywords=("kyoto",),
            summary_text="Notes about Kyoto travel.",
        )
        app.conversation = await app.chat.new_conversation("g1")
        await _submit(pilot, "let's revisit kyoto")
        await _wait_until_done(pilot, app)

        note = app.query_one(EnrichmentNote)
        # The assistant's reply has since pushed the note out of the
        # viewport; pilot.click only lands on what is actually on screen.
        note.scroll_visible(animate=False)
        await pilot.pause()

        await pilot.click(EnrichmentNote)
        assert "Notes about Kyoto travel." in str(note.content)

        await pilot.click(EnrichmentNote)
        assert "Notes about Kyoto travel." not in str(note.content)


async def test_a_turn_with_no_match_mounts_no_note():
    app = ChatApp(mock_settings(enrich_messages=True))
    async with app.run_test() as pilot:
        await _seed_app(
            app,
            group_id="g1",
            conversation_id="other",
            keywords=("kyoto",),
            summary_text="Notes about Kyoto travel.",
        )
        app.conversation = await app.chat.new_conversation("g1")

        await _submit(pilot, "totally unrelated text")
        await _wait_until_done(pilot, app)

        assert list(app.query(EnrichmentNote)) == []


async def test_switching_away_and_back_shows_no_notes_and_frees_the_summary():
    app = ChatApp(mock_settings(enrich_messages=True))
    async with app.run_test() as pilot:
        await _seed_app(
            app,
            group_id="g1",
            conversation_id="other",
            keywords=("kyoto",),
            summary_text="Notes about Kyoto travel.",
        )
        app.conversation = await app.chat.new_conversation("g1")
        await _submit(pilot, "let's revisit kyoto")
        await _wait_until_done(pilot, app)
        assert len(list(app.query(EnrichmentNote))) == 1

        mine_id = app.conversation.id
        await app._switch_to("other")
        await pilot.pause()
        await app._switch_to(mine_id)
        await pilot.pause()

        # The enrichment was never persisted, so the reloaded log has no note.
        assert list(app.query(EnrichmentNote)) == []

        await _submit(pilot, "kyoto again")
        await _wait_until_done(pilot, app)
        assert len(list(app.query(EnrichmentNote))) == 1


async def test_enrich_messages_off_mounts_no_note_and_sends_the_plain_text():
    app = ChatApp(mock_settings(enrich_messages=False))
    async with app.run_test() as pilot:
        await _seed_app(
            app,
            group_id="g1",
            conversation_id="other",
            keywords=("kyoto",),
            summary_text="Notes about Kyoto travel.",
        )
        app.conversation = await app.chat.new_conversation("g1")

        await _submit(pilot, "let's revisit kyoto")
        await _wait_until_done(pilot, app)

        assert list(app.query(EnrichmentNote)) == []
        bubbles = [b for b in app.query(MessageBubble) if b.message.role == "assistant"]
        assert "Notes about Kyoto travel." not in bubbles[-1].message.content


async def test_a_turn_in_a_second_conversation_is_enriched_from_the_first():
    app = ChatApp(mock_settings(enrich_messages=True))
    async with app.run_test() as pilot:
        group = await app.chat.create_group("Course project")
        app.conversation = await app.chat.new_conversation(group.id)
        await _submit(pilot, "we're deploying to S3 with boto3")
        await _wait_until_done(pilot, app)
        await app.chat.persist(app.conversation)
        # Stands in for what ExtractionService would have produced on leaving
        # this conversation — the mock backend doesn't emit real keywords.
        await app.chat.store.save_summary(
            make_summary(
                conversation_id=app.conversation.id,
                group_id=group.id,
                keywords=("S3",),
                summary="Notes about deploying to S3 with boto3.",
            )
        )

        await app.action_new_conversation()
        await pilot.pause()
        await _submit(pilot, "remind me about S3")
        await _wait_until_done(pilot, app)

        assert len(list(app.query(EnrichmentNote))) == 1
