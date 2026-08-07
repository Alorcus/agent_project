"""Context assembly.

Every prompt is built through a ``ContextStrategy``, so trimming, compression,
or relevance selection is a swap of this object rather than a change to the
chat service or the UI. ``ContextDecision`` records what happened so the UI can
show what was dropped and why.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from agentchat.core.models import Message

#: Rough characters-per-token. Replaced by the real tokenizer with the backend.
CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


@dataclass
class ContextDecision:
    """What the strategy did, in a form the UI can render."""

    messages: list[Message] = field(default_factory=list)
    dropped: list[Message] = field(default_factory=list)
    summarised: list[Message] = field(default_factory=list)
    estimated_tokens: int = 0
    budget: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def was_trimmed(self) -> bool:
        return bool(self.dropped or self.summarised)


class ContextStrategy(Protocol):
    def build(
        self,
        messages: Sequence[Message],
        *,
        context_window: int,
        reserve_for_response: int = 512,
    ) -> ContextDecision: ...


class RecencyWindowStrategy:
    """Naive baseline: keep the system prompt, then the most recent turns that
    fit the budget. No relevance, no compression — the bar smarter strategies
    have to clear."""

    name = "recency-window"

    def build(
        self,
        messages: Sequence[Message],
        *,
        context_window: int,
        reserve_for_response: int = 512,
    ) -> ContextDecision:
        budget = max(256, context_window - reserve_for_response)
        system = [m for m in messages if m.role == "system"]
        rest = [m for m in messages if m.role != "system"]

        used = sum(estimate_tokens(m.content) for m in system)
        kept: list[Message] = []
        dropped: list[Message] = []

        for message in reversed(rest):
            cost = estimate_tokens(message.content)
            if used + cost > budget:
                dropped.append(message)
                continue
            used += cost
            kept.append(message)

        kept.reverse()
        dropped.reverse()

        decision = ContextDecision(
            messages=system + kept,
            dropped=dropped,
            estimated_tokens=used,
            budget=budget,
            notes=[f"strategy={self.name}"],
        )
        if dropped:
            decision.notes.append(f"dropped {len(dropped)} older message(s) to fit")
        return decision
