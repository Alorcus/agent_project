"""Builders for test data, shared by the tests that need a group to file a
conversation under."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass

from agentchat.core import usage
from agentchat.core.models import Conversation, ConversationSummary, Group, Message
from agentchat.llm import transcript
from agentchat.llm.base import GenerationOptions, ModelInfo


def make_conversation(**overrides) -> Conversation:
    # `default` is the one group the schema seeds, so a conversation that means
    # nothing in particular by its group still satisfies the FK.
    defaults = dict(
        title="Trip planning",
        group_id="default",
        messages=[
            Message(role="user", content="Where should I go?"),
            Message(role="assistant", content="Try Kyoto.", model_id="qwen"),
        ],
    )
    defaults.update(overrides)
    return Conversation(**defaults)


def make_message(**overrides) -> Message:
    defaults = dict(role="user", content="Let's use SQLite for storage.")
    defaults.update(overrides)
    return Message(**defaults)


def make_group(**overrides) -> Group:
    defaults = dict(name="Picker rewrite", kind="project")
    defaults.update(overrides)
    return Group(**defaults)


@dataclass
class RecordedCall:
    messages: list[Message]
    options: GenerationOptions | None
    started: float
    #: `None` until the call finishes — a cancelled call may never set this.
    finished: float | None = None


class ScriptedProvider:
    """An always-loaded `LLMProvider` that yields each canned reply, whole,
    in turn, and records every call it received. Assertions about *which*
    prompt got *which* input, and about overlapping calls, need that record —
    the mock backend (which echoes the last user turn) cannot supply it.

    Replies run out silently (as `""`) once a test's script is exhausted,
    rather than raising `IndexError`, so a scenario that only cares about the
    first call or two doesn't have to script the rest.
    """

    def __init__(
        self,
        *replies: str,
        info: ModelInfo | None = None,
        chunk_delay: float = 0.0,
    ) -> None:
        self._replies = list(replies)
        self._info = info or ModelInfo(id="scripted", name="Scripted", context_window=4096)
        self._chunk_delay = chunk_delay
        self.calls: list[RecordedCall] = []

    @property
    def info(self) -> ModelInfo:
        return self._info

    @property
    def is_loaded(self) -> bool:
        return True

    async def load(self) -> None:
        return None

    async def unload(self) -> None:
        return None

    async def generate(
        self,
        messages: Sequence[Message],
        options: GenerationOptions | None = None,
    ) -> AsyncIterator[str]:
        call = RecordedCall(messages=list(messages), options=options, started=time.monotonic())
        self.calls.append(call)
        # Reported like a real backend's, so tests that assert on the context
        # meter can drive it with a scripted script rather than a GPU.
        metered = usage.record(
            label=transcript.current_label(),
            messages=messages,
            prompt_tokens=None,
            context_window=self._info.context_window,
        )
        index = len(self.calls) - 1
        reply = self._replies[index] if index < len(self._replies) else ""
        emitted: list[str] = []
        try:
            for chunk in _chunks(reply):
                if self._chunk_delay:
                    await asyncio.sleep(self._chunk_delay)
                emitted.append(chunk)
                yield chunk
        finally:
            if metered is not None:
                metered.complete(completion_tokens=None, output="".join(emitted))
        call.finished = time.monotonic()


def scripted_provider(
    *replies: str, info: ModelInfo | None = None, chunk_delay: float = 0.0
) -> ScriptedProvider:
    return ScriptedProvider(*replies, info=info, chunk_delay=chunk_delay)


def _chunks(text: str) -> list[str]:
    """Split into whitespace-preserving pieces so a joined stream reassembles
    exactly into `text`, and multi-word replies still exercise multi-chunk
    streaming."""
    words = text.split(" ")
    if not words:
        return []
    return [w + " " for w in words[:-1]] + [words[-1]]


def make_summary(**overrides) -> ConversationSummary:
    defaults = dict(
        conversation_id="c1",
        group_id="default",
        summary="A discussion about trip planning in Kyoto.",
        keywords=("kyoto", "travel"),
        covered_messages=2,
        model_id="mock-small",
    )
    defaults.update(overrides)
    return ConversationSummary(**defaults)
