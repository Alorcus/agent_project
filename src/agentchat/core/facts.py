"""Grounded fact extraction: a sliding window over a conversation, one LLM
call for a fact and one for its supporting quotes, each quote anchored back
into a real message by code before the fact is trusted.

Knows nothing about storage or the watermark — `extract` returns a `Fact` (or
`None`) for one window and the caller (`ChatService.extract_facts`) persists
it and advances the cursor, which keeps every LLM call outside any
transaction and this class testable against a scripted provider alone.
"""

from __future__ import annotations

from collections.abc import Sequence

from agentchat.core.anchoring import MIN_COVERAGE, anchor_in, coverage
from agentchat.core.errors import FactExtractionError, ProviderError
from agentchat.core.models import Author, Fact, Message, Phrase
from agentchat.core.prompts import (
    FACT_PROMPT,
    FACT_SYSTEM,
    QUOTES_PROMPT,
    QUOTES_SYSTEM,
    parse_quotes,
    render_window,
)
from agentchat.llm import transcript as llm_transcript
from agentchat.llm.base import GenerationOptions, complete
from agentchat.llm.registry import ModelRegistry

WINDOW_SIZE = 6
WINDOW_STEP = 4
#: How many trailing messages of a completed window open the next one — also
#: how far a reopened conversation's watermark is rewound (R8).
WINDOW_CARRY = WINDOW_SIZE - WINDOW_STEP

FACT_MAX_TOKENS = 96
QUOTES_MAX_TOKENS = 128
#: Head-room for the instruction text around the window — `FACT_SYSTEM` and
#: `QUOTES_SYSTEM` each carry a worked example.
PROMPT_OVERHEAD_TOKENS = 384
#: A floor under the window budget so a tiny context window still leaves room
#: for something to extract from.
MIN_WINDOW_BUDGET = 256

#: Both calls run at temperature 0 with thinking off, same reasoning as
#: `extraction.py`: the same window must extract the same way twice, and a
#: reasoning preamble would otherwise be stored as the fact itself.
_EXTRACTION_OPTIONS_BASE = dict(temperature=0.0, thinking=False)


def countable(messages: Sequence[Message]) -> list[Message]:
    """The turns the window and the summary are built from: the user's and
    the assistant's, in order, dropping `system` and empty ones.

    A specialist's answer is never a `Message` of its own — it lives in the
    default reply's `metadata["subagent"]` — so nothing here has to filter it
    out; there is no data a predicate for it could match (R9)."""
    return [m for m in messages if m.role != "system" and not m.is_empty]


def windows(covered: int, total: int, *, flush: bool = False) -> tuple[tuple[int, int], ...]:
    """Half-open ranges of countable indices still to extract, starting
    `WINDOW_CARRY` before `covered` so the last carried messages reopen the
    next window (R8). Full windows only, unless `flush` — then a final short
    window covers whatever is left past `covered`, which is what stops
    leaving a conversation twice from re-extracting the same carried
    messages."""
    result: list[tuple[int, int]] = []
    start = max(0, covered - WINDOW_CARRY)
    while start + WINDOW_SIZE <= total:
        result.append((start, start + WINDOW_SIZE))
        start += WINDOW_STEP
    if flush and total > covered:
        result.append((start, total))
    return tuple(result)


class FactExtractor:
    def __init__(self, registry: ModelRegistry) -> None:
        self._registry = registry

    async def extract(self, messages: Sequence[Message]) -> Fact | None:
        """One window's extraction: a fact call, a quotes call, each quote
        anchored back into `messages`. `conversation_id`, `group_id`,
        `window_start` and `window_end` are left at their defaults — the
        caller, which owns the watermark, fills those in."""
        provider = await self._registry.active_provider()
        budget = max(
            MIN_WINDOW_BUDGET,
            provider.info.context_window
            - max(FACT_MAX_TOKENS, QUOTES_MAX_TOKENS)
            - PROMPT_OVERHEAD_TOKENS,
        )
        text = render_window(messages, budget=budget)

        try:
            with llm_transcript.label("facts.fact"):
                fact_text = await complete(
                    provider,
                    [
                        Message(role="system", content=FACT_SYSTEM),
                        Message(role="user", content=FACT_PROMPT.format(text=text)),
                    ],
                    GenerationOptions(max_tokens=FACT_MAX_TOKENS, **_EXTRACTION_OPTIONS_BASE),
                )
        except ProviderError as exc:
            raise FactExtractionError(str(exc)) from exc
        fact_text = fact_text.strip()
        if not fact_text:
            return None

        try:
            with llm_transcript.label("facts.quotes"):
                quotes_reply = await complete(
                    provider,
                    [
                        Message(role="system", content=QUOTES_SYSTEM),
                        Message(
                            role="user",
                            content=QUOTES_PROMPT.format(text=text, fact=fact_text),
                        ),
                    ],
                    GenerationOptions(max_tokens=QUOTES_MAX_TOKENS, **_EXTRACTION_OPTIONS_BASE),
                )
        except ProviderError as exc:
            raise FactExtractionError(str(exc)) from exc

        phrases: list[Phrase] = []
        anchored_quotes: list[str] = []
        for quote in parse_quotes(quotes_reply):
            located = anchor_in(quote, messages)
            if located is None:
                continue
            message, (start, end) = located
            phrases.append(
                Phrase(message_id=message.id, start=start, end=end, author=Author.of(message))
            )
            anchored_quotes.append(quote)

        if not phrases:
            # A quote that cannot be located is not evidence (R2).
            return None
        if coverage(fact_text, anchored_quotes) < MIN_COVERAGE:
            # A long claim quoting only a fragment is unsupported (R10).
            return None

        return Fact(text=fact_text, phrases=tuple(phrases), model_id=provider.info.id)
