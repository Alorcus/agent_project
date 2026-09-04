"""Sub-agent dispatch: the roster, the router/task/consultation prompts and
their parsers, `DelegationService`'s routing → task → specialist pipeline,
`ChatService`'s inject → stream → persist turn, wiring, and the UI's progress
and consultation note."""

from __future__ import annotations

import asyncio
import string

import pytest

from agentchat.config import Settings, build_delegator
from agentchat.core.agents import DEFAULT_AGENT_ID, SubAgent, default_agents, find_mention
from agentchat.core.chat import ChatService
from agentchat.core.delegation import ANSWER_MAX_TOKENS, Consultation, DelegationService
from agentchat.core.errors import ProviderError
from agentchat.core.models import Message
from agentchat.core.prompts import (
    CONSULTATION_HEADER,
    DEFAULT_SYSTEM,
    ROUTER_PROMPT,
    ROUTER_SYSTEM,
    TASK_PROMPT,
    TASK_SYSTEM,
    injected_text,
    parse_agent_id,
    parse_task,
    roster_text,
)
from agentchat.llm.base import ModelInfo
from agentchat.llm.registry import ModelRegistry
from agentchat.storage.sqlite import SqliteStore
from agentchat.ui.app import ChatApp
from agentchat.ui.widgets import ConsultationNote
from conftest import mock_settings
from factories import make_conversation, make_group, scripted_provider

# -- agents.py ----------------------------------------------------------------


def _format_fields(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def test_default_agents_have_unique_ids_none_named_default():
    agents = default_agents()
    ids = [a.id for a in agents]
    assert len(ids) == len(set(ids))
    assert DEFAULT_AGENT_ID not in ids
    for agent in agents:
        assert agent.purpose.strip()
        assert agent.system_prompt.strip()


def test_every_system_prompt_states_isolation_and_who_its_for():
    for agent in default_agents():
        prompt = agent.system_prompt.lower()
        assert "cannot see" in prompt
        assert "another assistant" in prompt


def test_find_mention_matches_a_leading_id_case_insensitively():
    agents = default_agents()
    assert find_mention("@ask_bank what's my rate?", agents) == "ask_bank"
    assert find_mention("@AsK_BaNk hi", agents) == "ask_bank"
    assert find_mention("@default hi", agents) == "default"
    assert find_mention("@nobody hi", agents) is None
    assert find_mention("mail me @ask_bank", agents) is None
    assert find_mention("@ask_bankruptcy hi", agents) is None


def test_find_mention_does_not_alter_the_input_text():
    agents = default_agents()
    text = "@ask_bank what's my rate?"
    find_mention(text, agents)
    assert text == "@ask_bank what's my rate?"


# -- prompts.py -----------------------------------------------------------


def test_router_prompt_has_exactly_roster_and_message_fields():
    assert _format_fields(ROUTER_PROMPT) == {"roster", "message"}


def test_task_prompt_has_exactly_name_purpose_and_transcript_fields():
    assert _format_fields(TASK_PROMPT) == {"name", "purpose", "transcript"}


def test_roster_text_puts_default_first_and_one_line_per_agent():
    agents = default_agents()
    lines = roster_text(agents).splitlines()
    assert lines[0].startswith(f"{DEFAULT_AGENT_ID} — ")
    assert len(lines) == len(agents) + 1
    for agent, line in zip(agents, lines[1:]):
        assert line == f"{agent.id} — {agent.purpose}"


def test_router_system_example_names_no_shipped_agent():
    ids = {a.id for a in default_agents()}
    assert not any(agent_id in ROUTER_SYSTEM for agent_id in ids)
    assert "ask_vet" in ROUTER_SYSTEM


def test_task_system_states_the_specialist_cannot_see_the_conversation():
    assert "cannot see" in TASK_SYSTEM.lower()
    assert "conversation" in TASK_SYSTEM.lower()


def test_task_system_example_is_a_multi_turn_transcript():
    example = TASK_SYSTEM.split("Example:", 1)[1]
    assert example.count("User:") == 2
    assert example.count("Assistant:") == 1


def test_consultation_header_states_isolation_and_permission_to_correct():
    assert "could not see this conversation" in CONSULTATION_HEADER
    assert "correct it" in CONSULTATION_HEADER


def test_injected_text_carries_user_text_task_and_answer_in_order():
    agent = SubAgent(id="ask_bank", name="Banking", purpose="p", system_prompt="s")
    consultation = Consultation(
        agent=agent, task="the task", answer="the answer", trigger="router"
    )
    text = injected_text("what now?", consultation, None)

    assert text.startswith("what now?")
    header_at = text.index(CONSULTATION_HEADER)
    task_at = text.index("the task")
    answer_at = text.index("the answer")
    assert header_at < task_at < answer_at


@pytest.mark.parametrize(
    "reply",
    [
        "ask_bank",
        "ask_bank.",
        "`ask_bank`",
        "Name: ask_bank",
        "- ask_bank",
        "ask_bank\nBecause the user asked about rates.",
        "I would route this to ask_bank",
        "bank",
    ],
)
def test_parse_agent_id_accepts_decorated_replies(reply):
    ids = [a.id for a in default_agents()]
    assert parse_agent_id(reply, agent_ids=ids) == "ask_bank"


@pytest.mark.parametrize(
    "reply", ["default", "Default.", "", "none of these", "ask_vet"]
)
def test_parse_agent_id_rejects_default_empty_and_unlisted(reply):
    ids = [a.id for a in default_agents()]
    assert parse_agent_id(reply, agent_ids=ids) is None


def test_parse_agent_id_on_two_names_returns_the_first_deterministically():
    ids = [a.id for a in default_agents()]
    assert parse_agent_id("ask_chef or maybe ask_bank", agent_ids=ids) == "ask_chef"
    assert parse_agent_id("ask_bank or maybe ask_chef", agent_ids=ids) == "ask_bank"


def test_parse_task_drops_a_leading_label():
    assert parse_task("Task: Explain X.", fallback="…") == "Explain X."


def test_parse_task_on_whitespace_returns_the_fallback():
    assert parse_task("   ", fallback="original") == "original"


def test_parse_task_caps_a_long_reply_without_cutting_mid_word():
    long_reply = "word " * 3000
    result = parse_task(long_reply, fallback="x")
    assert len(result) < len(long_reply)
    assert not result.rstrip("…").endswith("wor")
    assert result.endswith("…")


# -- DelegationService ------------------------------------------------------


def _registry_with(provider) -> ModelRegistry:
    registry = ModelRegistry()
    registry.register(provider.info, lambda: provider)
    return registry


async def test_router_path_makes_three_calls_in_order_and_returns_a_consultation():
    provider = scripted_provider(
        "ask_bank", "Explain variable rate mortgages.", "A variable rate moves with the base rate."
    )
    service = DelegationService(_registry_with(provider))

    result = await service.consult("what does a variable rate mortgage mean?")

    assert isinstance(result, Consultation)
    assert result.agent.id == "ask_bank"
    assert result.task == "Explain variable rate mortgages."
    assert result.answer == "A variable rate moves with the base rate."
    assert result.trigger == "router"
    assert len(provider.calls) == 3


async def test_call_one_carries_every_roster_id_and_the_message():
    provider = scripted_provider("ask_bank", "a task", "an answer")
    service = DelegationService(_registry_with(provider))

    await service.consult("what does APR mean?")

    sent = provider.calls[0].messages[-1].content
    assert "what does APR mean?" in sent
    for agent in service.agents:
        assert agent.id in sent


async def test_call_two_carries_the_chosen_agents_purpose():
    provider = scripted_provider("ask_bank", "a task", "an answer")
    service = DelegationService(_registry_with(provider))

    await service.consult("what does APR mean?")

    sent = provider.calls[1].messages[-1].content
    bank = next(a for a in service.agents if a.id == "ask_bank")
    assert bank.purpose in sent


async def test_call_two_carries_the_earlier_turns_not_only_the_last_message():
    provider = scripted_provider("ask_bank", "a task", "an answer")
    service = DelegationService(_registry_with(provider))
    history = [
        Message(role="user", content="I'm comparing two mortgage offers."),
        Message(role="assistant", content="The headline rate is only half of it."),
        Message(role="user", content="and what does APR mean here?"),
    ]

    await service.consult("and what does APR mean here?", history=history)

    sent = provider.calls[1].messages[-1].content
    assert "I'm comparing two mortgage offers." in sent
    assert "The headline rate is only half of it." in sent
    assert "and what does APR mean here?" in sent


async def test_call_two_with_no_history_still_carries_the_message():
    provider = scripted_provider("ask_bank", "a task", "an answer")
    service = DelegationService(_registry_with(provider))

    await service.consult("what does APR mean?")

    assert "what does APR mean?" in provider.calls[1].messages[-1].content


async def test_call_two_keeps_the_last_turn_when_the_history_overflows():
    provider = scripted_provider(
        "ask_bank", "a task", "an answer",
        info=ModelInfo(id="tiny", name="Tiny", context_window=600),
    )
    service = DelegationService(_registry_with(provider))
    history = [
        Message(role="user", content="x" * 8000),
        Message(role="assistant", content="y" * 8000),
        Message(role="user", content="and what does APR mean here?"),
    ]

    await service.consult("and what does APR mean here?", history=history)

    sent = provider.calls[1].messages[-1].content
    assert "and what does APR mean here?" in sent
    assert "x" * 8000 not in sent


async def test_call_three_is_isolated_to_the_agents_system_prompt_and_task():
    provider = scripted_provider("ask_bank", "Explain APR.", "an answer")
    service = DelegationService(_registry_with(provider))

    await service.consult("what does APR mean, given my 30-year loan?")

    call = provider.calls[2]
    assert len(call.messages) == 2
    assert call.messages[0].content == next(
        a.system_prompt for a in service.agents if a.id == "ask_bank"
    )
    assert call.messages[1].content == "Explain APR."
    for agent in service.agents:
        if agent.id != "ask_bank":
            assert agent.id not in call.messages[0].content
            assert agent.id not in call.messages[1].content
    assert "30-year loan" not in call.messages[0].content
    assert "30-year loan" not in call.messages[1].content


def _recall(digest: str) -> "Recall":
    from agentchat.core.retrieval import Recall

    return Recall(
        hits=(), digest=digest, queries=("q",), rounds=1,
        exit="sufficient", user_text="what does APR mean, given my 30-year loan?",
    )


async def test_call_three_carries_the_recall_digest_after_the_task():
    from agentchat.core.prompts import SPECIALIST_EVIDENCE_HEADER

    provider = scripted_provider("ask_bank", "Explain APR.", "an answer")
    service = DelegationService(_registry_with(provider))

    result = await service.consult(
        "what does APR mean, given my 30-year loan?",
        recall=_recall("Facts:\n- The user has a 30-year fixed mortgage."),
    )

    call = provider.calls[2]
    assert len(call.messages) == 2
    user = call.messages[1].content
    assert user.index("Explain APR.") < user.index(SPECIALIST_EVIDENCE_HEADER)
    assert user.index(SPECIALIST_EVIDENCE_HEADER) < user.index("30-year fixed mortgage")
    # The footer restating the user's message is deliberately absent.
    assert "The user's message, again" not in user
    # The digest never leaks into the persisted task.
    assert "30-year fixed mortgage" not in result.task


async def test_call_three_with_no_recall_is_just_the_task():
    provider = scripted_provider("ask_bank", "Explain APR.", "an answer")

    await DelegationService(_registry_with(provider)).consult(
        "what does APR mean?", recall=None
    )

    assert provider.calls[2].messages[1].content == "Explain APR."


async def test_router_replying_default_returns_none_after_one_call():
    provider = scripted_provider("default")
    service = DelegationService(_registry_with(provider))

    result = await service.consult("random text")

    assert result is None
    assert len(provider.calls) == 1


async def test_mention_skips_the_router_call():
    provider = scripted_provider("a task", "an answer")
    service = DelegationService(_registry_with(provider))

    result = await service.consult("@ask_chef how long do I boil this")

    assert result is not None
    assert result.trigger == "mention"
    assert len(provider.calls) == 2


async def test_mention_default_returns_none_with_zero_calls():
    provider = scripted_provider("should not be called")
    service = DelegationService(_registry_with(provider))

    result = await service.consult("@default hello")

    assert result is None
    assert len(provider.calls) == 0


async def test_router_garbage_reply_returns_none():
    provider = scripted_provider("I'm not sure who should handle this!!")
    service = DelegationService(_registry_with(provider))

    assert await service.consult("hello") is None


async def test_router_naming_an_unlisted_agent_returns_none():
    provider = scripted_provider("ask_vet")
    service = DelegationService(_registry_with(provider))

    assert await service.consult("my cat is sick") is None


async def test_empty_task_reply_falls_back_to_the_user_text_and_still_consults():
    provider = scripted_provider("ask_bank", "", "an answer")
    service = DelegationService(_registry_with(provider))

    result = await service.consult("what does APR mean?")

    assert result is not None
    assert result.task == "what does APR mean?"
    assert result.answer == "an answer"


async def test_empty_specialist_answer_returns_none():
    provider = scripted_provider("ask_bank", "a task", "   ")
    service = DelegationService(_registry_with(provider))

    assert await service.consult("what does APR mean?") is None


class _FailingProvider:
    def __init__(self) -> None:
        self.info = ModelInfo(id="failing", name="Failing", context_window=4096)
        self.is_loaded = True

    async def load(self) -> None:
        return None

    async def unload(self) -> None:
        return None

    async def generate(self, messages, options=None):
        raise ProviderError("boom")
        yield ""  # pragma: no cover


async def test_provider_error_returns_none_no_exception():
    service = DelegationService(_registry_with(_FailingProvider()))
    assert await service.consult("hello") is None


async def test_a_slow_provider_times_out_and_returns_none():
    provider = scripted_provider("ask_bank", "a task", "an answer", chunk_delay=1.0)
    service = DelegationService(_registry_with(provider), timeout=0.05)

    result = await service.consult("what does APR mean?")

    assert result is None


async def test_cancellation_propagates_out_of_consult():
    provider = scripted_provider("ask_bank", "a task", "an answer", chunk_delay=1.0)
    service = DelegationService(_registry_with(provider), timeout=30.0)

    task = asyncio.create_task(service.consult("hello"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_every_call_runs_at_temperature_zero_with_thinking_off():
    provider = scripted_provider("ask_bank", "a task", "an answer")
    service = DelegationService(_registry_with(provider))

    await service.consult("what does APR mean?")

    for call in provider.calls:
        assert call.options.temperature == 0.0
        assert call.options.thinking is False


async def test_answer_max_tokens_bounds_the_specialist_call():
    provider = scripted_provider("ask_bank", "a task", "an answer")
    service = DelegationService(_registry_with(provider))

    await service.consult("what does APR mean?")

    assert provider.calls[2].options.max_tokens == ANSWER_MAX_TOKENS


# -- ChatService --------------------------------------------------------


def _chat_registry(provider) -> ModelRegistry:
    return _registry_with(provider)


async def test_a_consulted_turn_makes_four_calls_and_the_fourth_is_the_reply(store):
    provider = scripted_provider(
        "ask_bank", "Explain APR.", "APR is the annualised cost.", "Here's the answer, in short."
    )
    registry = _chat_registry(provider)
    chat = ChatService(registry, store=store, delegator=DelegationService(registry))
    conversation = await chat.new_conversation()

    chunks = [c async for c in chat.stream_reply(conversation, "what does APR mean?")]

    assert len(provider.calls) == 4
    assert "".join(chunks) == "Here's the answer, in short."
    fourth = provider.calls[3].messages[-1].content
    assert CONSULTATION_HEADER in fourth
    assert "APR is the annualised cost." in fourth


async def test_the_streamed_reply_is_call_a_not_the_specialists_answer(store):
    provider = scripted_provider(
        "ask_bank", "Explain APR.", "the specialist's raw answer", "the assistant's own reply"
    )
    registry = _chat_registry(provider)
    chat = ChatService(registry, store=store, delegator=DelegationService(registry))
    conversation = await chat.new_conversation()

    chunks = [c async for c in chat.stream_reply(conversation, "what does APR mean?")]

    assert "".join(chunks) == "the assistant's own reply"
    assert "the specialist's raw answer" not in "".join(chunks)


async def test_the_task_call_sees_the_earlier_turns_of_the_conversation(store):
    provider = scripted_provider(
        "ask_bank", "Explain APR.", "an answer", "a reply",
        "ask_bank", "Explain APR on that loan.", "an answer", "a second reply",
    )
    registry = _chat_registry(provider)
    chat = ChatService(registry, store=store, delegator=DelegationService(registry))
    conversation = await chat.new_conversation()

    async for _ in chat.stream_reply(conversation, "I have a 30-year loan."):
        pass
    async for _ in chat.stream_reply(conversation, "so what does APR mean for it?"):
        pass

    second_task_call = provider.calls[5].messages[-1].content
    assert "I have a 30-year loan." in second_task_call
    assert "so what does APR mean for it?" in second_task_call


async def test_the_user_turn_stored_is_byte_identical_to_what_was_typed(store):
    provider = scripted_provider("ask_bank", "Explain APR.", "an answer", "a reply")
    registry = _chat_registry(provider)
    chat = ChatService(registry, store=store, delegator=DelegationService(registry))
    conversation = await chat.new_conversation()

    async for _ in chat.stream_reply(conversation, "what does APR mean?"):
        pass

    assert conversation.messages[-2].content == "what does APR mean?"


async def test_last_turn_records_the_consultation(store):
    provider = scripted_provider("ask_bank", "Explain APR.", "an answer", "a reply")
    registry = _chat_registry(provider)
    chat = ChatService(registry, store=store, delegator=DelegationService(registry))
    conversation = await chat.new_conversation()

    async for _ in chat.stream_reply(conversation, "what does APR mean?"):
        pass

    assert chat.last_turn.consultation.agent.id == "ask_bank"
    assert chat.last_turn.consultation.answer == "an answer"


async def test_consultation_metadata_survives_a_reload(tmp_path):
    path = tmp_path / "chat.db"
    store = SqliteStore(path)
    provider = scripted_provider("ask_bank", "Explain APR.", "an answer", "a reply")
    registry = _chat_registry(provider)
    chat = ChatService(registry, store=store, delegator=DelegationService(registry))
    conversation = await chat.new_conversation()

    async for _ in chat.stream_reply(conversation, "what does APR mean?"):
        pass

    reloaded = await SqliteStore(path).load(conversation.id)
    block = reloaded.messages[-1].metadata["subagent"]
    assert block["task"] == "Explain APR."
    assert block["answer"] == "an answer"
    assert block["id"] == "ask_bank"


async def test_router_says_default_behaves_exactly_like_no_delegator(store):
    provider = scripted_provider("default", "a plain reply")
    registry = _chat_registry(provider)
    chat = ChatService(registry, store=store, delegator=DelegationService(registry))
    conversation = await chat.new_conversation()

    chunks = [c async for c in chat.stream_reply(conversation, "hello")]

    assert len(provider.calls) == 2
    assert "".join(chunks) == "a plain reply"
    assert chat.last_turn.consultation is None
    assert "subagent" not in conversation.messages[-1].metadata


async def test_chat_service_with_no_delegator_makes_no_routing_call(store):
    provider = scripted_provider("a plain reply")
    registry = _chat_registry(provider)
    chat = ChatService(registry, store=store, delegator=None)
    conversation = await chat.new_conversation()

    chunks = [c async for c in chat.stream_reply(conversation, "hello")]

    assert len(provider.calls) == 1
    assert "".join(chunks) == "a plain reply"


async def test_budget_guard_drops_the_consultation_but_keeps_the_users_message(store):
    provider = scripted_provider(
        "ask_bank", "Explain APR.", "x" * 4000, "a reply", info=ModelInfo(
            id="tiny", name="Tiny", context_window=600
        )
    )
    registry = _chat_registry(provider)
    chat = ChatService(registry, store=store, delegator=DelegationService(registry))
    conversation = await chat.new_conversation()

    chunks = [c async for c in chat.stream_reply(conversation, "what does APR mean?")]

    sent = provider.calls[3].messages[-1].content
    assert "what does APR mean?" in sent
    assert "x" * 4000 not in sent
    # The rebuilt prompt is still the assistant's own prompt — dropping the
    # consultation must not drop the system turn with it.
    assert provider.calls[3].messages[0].content == DEFAULT_SYSTEM
    assert chat.last_turn.consultation is None
    assert "subagent" not in conversation.messages[-1].metadata
    assert "".join(chunks) == "a reply"


async def test_on_progress_order_on_a_consulted_and_a_plain_turn(store):
    consulted_provider = scripted_provider(
        "ask_bank", "Explain APR.", "an answer", "a reply"
    )
    registry = _chat_registry(consulted_provider)
    chat = ChatService(registry, store=store, delegator=DelegationService(registry))
    conversation = await chat.new_conversation()
    seen: list[str] = []

    async for _ in chat.stream_reply(conversation, "what does APR mean?", on_progress=seen.append):
        pass

    assert seen == ["routing…", "consulting Banking…", "writing the reply…"]

    plain_provider = scripted_provider("default", "a reply")
    registry2 = _chat_registry(plain_provider)
    chat2 = ChatService(registry2, store=store, delegator=DelegationService(registry2))
    conversation2 = await chat2.new_conversation()
    seen2: list[str] = []

    async for _ in chat2.stream_reply(conversation2, "hello", on_progress=seen2.append):
        pass

    assert seen2 == ["routing…", "writing the reply…"]


async def test_cancel_mid_stream_on_a_consulted_turn_frees_the_lock(store):
    provider = scripted_provider(
        "ask_bank", "Explain APR.", "an answer", "a long streaming reply that takes a while",
        chunk_delay=0.03,
    )
    registry = _chat_registry(provider)
    chat = ChatService(registry, store=store, delegator=DelegationService(registry))
    conversation = await chat.new_conversation()

    async def consume():
        async for _ in chat.stream_reply(conversation, "what does APR mean?"):
            pass

    task = asyncio.create_task(consume())
    # Long enough to clear routing + task + specialist (5 short chunks) and
    # land mid-stream of the reply itself.
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not chat._provider_lock.locked()
    assert conversation.messages[-1].role == "assistant"
    assert conversation.messages[-1].metadata.get("subagent") is not None


# -- config.py ------------------------------------------------------------


def test_build_delegator_is_switched_by_subagents(store):
    registry = ModelRegistry()
    assert build_delegator(mock_settings(subagents=False), registry) is None
    delegator = build_delegator(mock_settings(subagents=True), registry)
    assert isinstance(delegator, DelegationService)


def test_agentchat_subagents_env_flag_is_honoured(monkeypatch):
    monkeypatch.setenv("AGENTCHAT_SUBAGENTS", "0")
    assert Settings().subagents is False


def test_delegator_carries_the_configured_timeout():
    registry = ModelRegistry()
    delegator = build_delegator(mock_settings(subagents=True, subagent_timeout=12.5), registry)
    assert delegator._timeout == 12.5


# -- the UI: progress and the consultation note ----------------------------


def _scripted_app(*replies, **settings_overrides) -> tuple[ChatApp, object]:
    """A `ChatApp` whose registry and delegator both point at one
    `ScriptedProvider` — the mock backend cannot exercise real routing
    (it echoes, per the plan), so UI-level consultation tests need a
    provider that actually answers the router/task/specialist prompts."""
    app = ChatApp(mock_settings(subagents=True, **settings_overrides))
    provider = scripted_provider(*replies)
    registry = _registry_with(provider)
    app.registry = registry
    app.chat.registry = registry
    app.chat.delegator = DelegationService(registry)
    return app, provider


async def _submit(pilot, text: str) -> None:
    from textual.widgets import Input

    prompt = pilot.app.query_one("#prompt", Input)
    prompt.value = text
    await pilot.press("enter")


async def _wait_until_done(pilot, app) -> None:
    for _ in range(200):
        await pilot.pause()
        if not app._generating:
            break
        await asyncio.sleep(0.02)


async def test_a_consulted_turn_mounts_exactly_one_collapsed_note():
    app, _ = _scripted_app("ask_bank", "Explain APR.", "an answer", "a reply")
    async with app.run_test() as pilot:
        await _submit(pilot, "what does APR mean?")
        await _wait_until_done(pilot, app)

        notes = list(app.query(ConsultationNote))
        assert len(notes) == 1
        assert str(notes[0].content) == "▸ answered with help from Banking"

        notes[0].scroll_visible(animate=False)
        await pilot.pause()
        await pilot.click(ConsultationNote)
        assert "Explain APR." in str(notes[0].content)
        assert "an answer" in str(notes[0].content)


async def test_an_unconsulted_turn_mounts_no_note():
    app, _ = _scripted_app("default", "a reply")
    async with app.run_test() as pilot:
        await _submit(pilot, "hello")
        await _wait_until_done(pilot, app)

        assert list(app.query(ConsultationNote)) == []


async def test_mention_consults_without_a_routing_call():
    # Task, specialist, then the reply (call A) — three calls, none of them
    # a router call.
    app, provider = _scripted_app("a task", "an answer", "the assistant's reply")
    async with app.run_test() as pilot:
        await _submit(pilot, "@ask_chef how long do I boil this")
        await _wait_until_done(pilot, app)

        assert len(provider.calls) == 3
        assert list(app.query(ConsultationNote))


async def test_subagents_disabled_mounts_no_note_and_makes_one_generation():
    app = ChatApp(mock_settings(subagents=False))
    async with app.run_test() as pilot:
        await _submit(pilot, "what does APR mean?")
        await _wait_until_done(pilot, app)

        assert list(app.query(ConsultationNote)) == []


async def test_a_turn_that_recalled_and_consulted_shows_both_notes():
    from agentchat.core.retrieval import AdaptiveRetriever, FactIndex
    from agentchat.llm.embedding import HashingEmbedder
    from agentchat.ui.widgets import RecallNote
    from factories import make_indexed_group

    app, provider = _scripted_app(
        # recall first: gate, rewrite, judge — then route, task, specialist, reply
        "SEARCH", "a query", "ENOUGH",
        "ask_bank", "Explain APR.", "an answer", "a reply",
        recall_facts=True,
    )
    embedder = HashingEmbedder()
    app.chat.retriever = AdaptiveRetriever(
        app.chat.registry, FactIndex(app.chat.store, embedder),
        rewrites=1, min_score=0.0,
    )
    async with app.run_test() as pilot:
        group = await make_indexed_group(app.chat.store, embedder)
        app.conversation = await app.chat.new_conversation(group.id)

        await _submit(pilot, "what does APR mean for our billing?")
        await _wait_until_done(pilot, app)

        assert len(list(app.query(ConsultationNote))) == 1
        assert len(list(app.query(RecallNote))) == 1
        meta = app.chat.last_turn.message.metadata
        assert meta.get("subagent") is not None
        assert meta.get("recall") is not None


async def test_switching_away_and_back_still_shows_the_note():
    app, _ = _scripted_app("ask_bank", "Explain APR.", "an answer", "a reply")
    async with app.run_test() as pilot:
        await _submit(pilot, "what does APR mean?")
        await _wait_until_done(pilot, app)
        assert len(list(app.query(ConsultationNote))) == 1
        mine_id = app.conversation.id

        await app.action_new_conversation()
        await pilot.pause()
        assert list(app.query(ConsultationNote)) == []

        await app._switch_to(mine_id)
        await pilot.pause()

        assert len(list(app.query(ConsultationNote))) == 1


async def test_status_bar_shows_the_consultation_phases_in_order():
    provider = scripted_provider("ask_bank", "Explain APR.", "an answer", "a reply")
    service = DelegationService(_registry_with(provider))
    seen: list[str] = []
    await service.consult("what does APR mean?", seen.append)
    assert seen == ["consulting Banking…"]
