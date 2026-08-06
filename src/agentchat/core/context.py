"""Context assembly — the seam for the intelligent-context-management elective.

Nothing intelligent happens here yet. What matters is that *every* prompt is
built through a ``ContextStrategy``, so compression (NFR-CTX-01), selection
(NFR-CTX-03) and window safety (NFR-CTX-04) become a swap of this object rather
than a change to the chat service or the UI.

``ContextDecision`` exists because the elective has to be *demonstrable*
(NFR-CTX-05): the UI must be able to show what was dropped and why.
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
    """Placeholder strategy: keep the system prompt, then the most recent turns
    that fit the budget.

    This is explicitly the naive baseline the elective has to beat — recency
    only, no relevance, no compression. Keeping it as a named class means the
    comparison in the demo is a one-line swap.
    """

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
