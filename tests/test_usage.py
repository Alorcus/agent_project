"""The context meter: what each backend reports for a call's prompt and its
completion, how a turn's peak is picked across the calls it makes, and how the
status row renders it."""

from __future__ import annotations

import asyncio
from pathlib import Path

from textual.widgets import Static

from agentchat.core import usage
from agentchat.core.chat import ChatService
from agentchat.core.context import estimate_tokens
from agentchat.core.delegation import DelegationService
from agentchat.core.models import Message
from agentchat.core.usage import CallUsage, TurnUsage
from agentchat.llm.base import ModelInfo
from agentchat.llm.local import TransformersProvider
from agentchat.llm.registry import ModelRegistry
from agentchat.ui.app import ChatApp
from agentchat.ui.widgets import ContextMeter, _bar, _short
from conftest import mock_settings
from factories import scripted_provider

# -- usage.py -----------------------------------------------------------------


def _call(**overrides) -> CallUsage:
    defaults = dict(prompt_tokens=100, completion_tokens=0, context_window=2048)
    defaults.update(overrides)
    return CallUsage(**defaults)


def test_a_calls_size_is_its_prompt_plus_what_it_generated():
    call = _call(prompt_tokens=400, completion_tokens=112)

    assert call.tokens == 512
    assert call.fraction == 0.25


def test_fraction_is_clamped_and_survives_a_zero_window():
    assert _call(prompt_tokens=2000, completion_tokens=1000).fraction == 1.0
    assert _call(prompt_tokens=10, context_window=0).fraction == 0.0


def test_the_collector_keeps_the_largest_call_not_the_last():
    turn = TurnUsage()
    turn.record(_call(prompt_tokens=900, label="subagent.task"))
    turn.record(_call(prompt_tokens=120, label="chat"))

    assert turn.peak.tokens == 900
    assert turn.peak.label == "subagent.task"


def test_a_completion_is_added_to_the_prompt_it_was_generated_from():
    with usage.collecting() as turn:
        call = usage.record(
            label="chat", messages=[], prompt_tokens=400, context_window=2048
        )
        assert turn.peak.tokens == 400  # the prompt alone, until it has written
        call.complete(completion_tokens=112)

    assert turn.peak.prompt_tokens == 400
    assert turn.peak.completion_tokens == 112
    assert turn.peak.tokens == 512


def test_a_completion_does_not_displace_a_larger_call():
    with usage.collecting() as turn:
        big = usage.record(
            label="subagent.task", messages=[], prompt_tokens=900, context_window=2048
        )
        big.complete(completion_tokens=50)
        small = usage.record(
            label="chat", messages=[], prompt_tokens=100, context_window=2048
        )
        small.complete(completion_tokens=10)

    assert turn.peak.tokens == 950
    assert turn.peak.label == "subagent.task"


def test_a_call_that_generated_nothing_stands_at_its_prompt():
    with usage.collecting() as turn:
        call = usage.record(
            label="chat", messages=[], prompt_tokens=400, context_window=2048
        )
        call.complete(completion_tokens=None, output="")

    assert turn.peak.tokens == 400
    assert turn.peak.completion_tokens == 0


def test_a_completion_reported_after_the_turn_lands_in_the_turn_it_belongs_to():
    """The local backend reports its completion from the generation thread,
    which can run after the turn that started it has closed — and while the
    next turn is already collecting."""
    with usage.collecting() as first:
        call = usage.record(
            label="chat", messages=[], prompt_tokens=400, context_window=2048
        )
    with usage.collecting() as second:
        call.complete(completion_tokens=112)

    assert first.peak.tokens == 512
    assert second.peak is None


def test_recording_outside_a_collector_is_a_no_op():
    assert (
        usage.record(
            label="chat",
            messages=[Message(role="user", content="x" * 400)],
            prompt_tokens=100,
            context_window=2048,
        )
        is None
    )
    with usage.collecting() as turn:
        pass
    assert turn.peak is None


def test_a_backend_without_a_tokenizer_gets_an_estimate_that_says_so():
    messages = [Message(role="user", content="x" * 400)]
    with usage.collecting() as turn:
        call = usage.record(
            label="chat", messages=messages, prompt_tokens=None, context_window=2048
        )
        call.complete(completion_tokens=None, output="y" * 40)

    assert turn.peak.prompt_tokens == estimate_tokens("x" * 400)
    assert turn.peak.completion_tokens == estimate_tokens("y" * 40)
    assert turn.peak.estimated is True


def test_a_counted_call_is_taken_verbatim():
    with usage.collecting() as turn:
        call = usage.record(
            label="chat",
            messages=[Message(role="user", content="x" * 400)],
            prompt_tokens=37,
            context_window=2048,
        )
        call.complete(completion_tokens=3)

    assert turn.peak.tokens == 40
    assert turn.peak.estimated is False


def test_collectors_nest_without_stranding_the_outer_one():
    with usage.collecting() as outer:
        with usage.collecting() as inner:
            usage.record(
                label="inner", messages=[], prompt_tokens=500, context_window=2048
            )
        usage.record(label="outer", messages=[], prompt_tokens=10, context_window=2048)

    assert inner.peak.tokens == 500
    assert outer.peak.tokens == 10


# -- the local backend --------------------------------------------------------


class _Ids:
    """Stands in for the generated token ids. Slicing the prompt off returns
    itself, so `shape[-1]` is whatever length the test asked for."""

    def __init__(self, length: int) -> None:
        self.shape = (length,)

    def __getitem__(self, item):
        return self


class _Tokenizer:
    def decode(self, ids, skip_special_tokens: bool = False) -> str:
        return "generated"


def test_the_local_backend_reports_the_tokenizers_own_completion_count():
    """`_record_completion` exercised directly, as the transcript's own
    no-GPU test does — the count it hands the meter is the tokenizer's, not
    an estimate off the decoded text."""
    provider = TransformersProvider(
        ModelInfo(id="t", name="T", context_window=2048), path=Path("nonexistent")
    )
    provider._tokenizer = _Tokenizer()

    with usage.collecting() as turn:
        call = usage.record(
            label="chat", messages=[], prompt_tokens=400, context_window=2048
        )
        provider._record_completion(
            sequences=[_Ids(112)],
            state={},
            prompt_len=400,
            call_id="abc123",
            label="chat",
            metered=call,
        )

    assert turn.peak.prompt_tokens == 400
    assert turn.peak.completion_tokens == 112
    assert turn.peak.estimated is False


# -- ChatService --------------------------------------------------------------


def _registry_with(provider) -> ModelRegistry:
    registry = ModelRegistry()
    registry.register(provider.info, lambda: provider)
    return registry


def _estimate(messages) -> int:
    return sum(estimate_tokens(m.content) for m in messages)


async def test_a_plain_turn_measures_the_prompt_and_the_reply_together(store):
    reply = "a reply long enough to be worth counting on its own"
    provider = scripted_provider(reply)
    chat = ChatService(_registry_with(provider), store=store)
    conversation = await chat.new_conversation()

    async for _ in chat.stream_reply(conversation, "what does APR mean?"):
        pass

    peak = chat.last_turn.usage.peak
    assert peak.label == "chat"
    assert peak.prompt_tokens == _estimate(provider.calls[0].messages)
    assert peak.completion_tokens == estimate_tokens(reply)
    assert peak.tokens == peak.prompt_tokens + peak.completion_tokens
    assert peak.context_window == provider.info.context_window


async def test_a_long_reply_to_a_short_prompt_still_moves_the_figure(store):
    short = scripted_provider("ok")
    chat = ChatService(_registry_with(short), store=store)
    async for _ in chat.stream_reply(await chat.new_conversation(), "hi"):
        pass
    modest = chat.last_turn.usage.peak.tokens

    long = scripted_provider("APR " * 400)
    chat = ChatService(_registry_with(long), store=store)
    async for _ in chat.stream_reply(await chat.new_conversation(), "hi"):
        pass

    assert chat.last_turn.usage.peak.tokens > modest


async def test_a_consulted_turn_reports_the_largest_of_its_four_calls(store):
    replies = ("ask_bank", "Explain APR.", "an answer", "a reply")
    provider = scripted_provider(*replies)
    registry = _registry_with(provider)
    chat = ChatService(registry, store=store, delegator=DelegationService(registry))
    conversation = await chat.new_conversation()

    async for _ in chat.stream_reply(conversation, "what does APR mean?"):
        pass

    assert len(provider.calls) == 4
    # Each call costs its prompt plus what it generated — the cache held both.
    sizes = [
        _estimate(call.messages) + estimate_tokens(generated)
        for call, generated in zip(provider.calls, replies)
    ]
    labels = ["route", "subagent.task", "subagent.ask_bank", "chat"]
    peak = chat.last_turn.usage.peak
    assert peak.tokens == max(sizes)
    assert peak.label == labels[sizes.index(max(sizes))]


async def test_each_turn_is_measured_on_its_own(store):
    provider = scripted_provider("a reply", "another reply")
    chat = ChatService(_registry_with(provider), store=store)

    long_conversation = await chat.new_conversation()
    async for _ in chat.stream_reply(long_conversation, "APR " * 500):
        pass
    after_the_long_turn = chat.last_turn.usage.peak.tokens

    short_conversation = await chat.new_conversation()
    async for _ in chat.stream_reply(short_conversation, "and VAT?"):
        pass

    assert chat.last_turn.usage.peak.tokens < after_the_long_turn


# -- the meter ----------------------------------------------------------------


def test_the_bar_fills_from_empty_to_full():
    assert _bar(0.0, 8) == "░" * 8
    assert _bar(1.0, 8) == "█" * 8
    assert _bar(0.5, 8) == "████" + "░" * 4
    # A prompt too small to fill a cell still shows an edge — "something is in
    # there" is the one thing an empty bar must not say.
    assert _bar(0.01, 8) != "░" * 8
    assert len(_bar(0.37, 14)) == 14


def test_token_counts_read_as_magnitudes():
    assert _short(0) == "0"
    assert _short(999) == "999"
    assert _short(2048) == "2.0k"
    assert _short(16384) == "16.4k"


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


def _meter_text(app) -> str:
    return str(app.query_one("#context-meter", ContextMeter).content)


async def test_the_meter_starts_empty_against_the_active_models_window():
    app = ChatApp(mock_settings())
    async with app.run_test():
        # Mock Small's window is 2048.
        assert "0/2.0k" in _meter_text(app)


async def test_the_meter_reports_the_turn_once_it_has_been_sent():
    app = ChatApp(mock_settings())
    async with app.run_test() as pilot:
        await _submit(pilot, "what does APR mean?")
        await _wait_until_done(pilot, app)

        peak = app.chat.last_turn.usage.peak
        assert peak is not None
        # The streamed reply is counted alongside the prompt that produced it.
        assert peak.completion_tokens > 0
        # The mock backend has no tokenizer, so the figure is flagged a guess.
        assert f"~{_short(peak.tokens)}/2.0k" in _meter_text(app)


async def test_a_prompt_near_the_window_colours_the_meter():
    app = ChatApp(mock_settings())
    async with app.run_test():
        meter = app.query_one("#context-meter", ContextMeter)
        assert not meter.has_class("-tight")
        assert not meter.has_class("-full")

        meter.show(_call(prompt_tokens=1600, completion_tokens=100), 2048)
        assert meter.has_class("-tight")
        assert not meter.has_class("-full")

        meter.show(_call(prompt_tokens=1600, completion_tokens=400), 2048)
        assert meter.has_class("-full")
        assert not meter.has_class("-tight")


async def test_a_long_message_moves_the_bar_and_the_trimming_keeps_it_honest():
    """A message far past the window is dropped by the context strategy rather
    than sent, and the meter reports what was sent — not what was typed."""
    app = ChatApp(mock_settings())
    async with app.run_test() as pilot:
        await _submit(pilot, "APR " * 3000)
        await _wait_until_done(pilot, app)

        peak = app.chat.last_turn.usage.peak
        assert peak.prompt_tokens <= peak.context_window
        assert app.chat.last_turn.context.dropped


async def test_the_status_row_keeps_the_status_text_alongside_the_meter():
    app = ChatApp(mock_settings())
    async with app.run_test():
        row = app.query_one("#statusrow")
        assert row.query_one("#statusbar", Static) is not None
        assert row.query_one("#context-meter", ContextMeter) is not None
