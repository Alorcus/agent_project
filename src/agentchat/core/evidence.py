"""The projection between a fact and the messages it was extracted from.

A `Fact` points at spans of messages by id; the evidence view needs the
messages themselves, with those spans located in place. This module is the
pure function between the two — no store, no widget — the same contract
`core/anchoring.py` holds.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from agentchat.core.facts import countable
from agentchat.core.models import Conversation, Fact, Message


@dataclass(frozen=True)
class Excerpt:
    message: Message
    #: Sorted, non-overlapping, in `message.content` coordinates.
    spans: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class Evidence:
    excerpts: tuple[Excerpt, ...]
    #: Quotes whose message is not in the window, in phrase order.
    orphans: tuple[str, ...]


def merge_spans(spans: Iterable[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
    """Sort by start and fold overlapping *and* touching pairs into one, so two
    quotes that anchored to adjoining regions draw as one highlight with no
    seam and the output is canonical enough to assert on."""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return tuple(merged)


def evidence_for(fact: Fact, conversation: Conversation) -> Evidence:
    """The window `fact` came from, as excerpts with its phrases located in
    place, plus the phrases whose message fell outside that window as their
    stored quote text."""
    items = countable(conversation.messages)
    start = fact.window_start
    end = min(fact.window_end, len(items))
    window = items[start:end] if start < end else []
    window_ids = {message.id for message in window}

    spans_by_message: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for phrase in fact.phrases:
        spans_by_message[phrase.message_id].append((phrase.start, phrase.end))

    excerpts: list[Excerpt] = []
    for message in window:
        limit = len(message.content)
        clamped: list[tuple[int, int]] = []
        for span_start, span_end in spans_by_message.get(message.id, ()):
            lo = max(0, min(span_start, limit))
            hi = max(0, min(span_end, limit))
            if hi > lo:
                clamped.append((lo, hi))
        excerpts.append(Excerpt(message=message, spans=merge_spans(clamped)))

    orphans = tuple(
        phrase.quote
        for phrase in fact.phrases
        if phrase.message_id not in window_ids and phrase.quote
    )
    return Evidence(excerpts=tuple(excerpts), orphans=orphans)
