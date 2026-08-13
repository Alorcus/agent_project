"""Recall across a group: which of the group's other summaries a user message
matches, and the once-per-visit ledger that keeps a summary from being
appended twice in one session.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import lru_cache

from agentchat.core.models import DEFAULT_GROUP_ID, Conversation, ConversationSummary
from agentchat.storage.base import ConversationStore

#: R7 — the most a single message can be enriched by.
MAX_ENRICHMENTS = 3


@lru_cache(maxsize=512)
def _pattern(keyword: str) -> re.Pattern[str]:
    # `\b` fails on keywords like "/upload endpoint" or "S3" that start or end
    # on a non-word character — there is no word boundary between a space and
    # a `/`. `(?<!\w)`/`(?!\w)` matches the same intent without that gap.
    # Internal whitespace becomes `\s+` so a phrase split across a line break
    # in the message still matches.
    body = r"\s+".join(re.escape(part) for part in keyword.split())
    return re.compile(rf"(?<!\w){body}(?!\w)", re.IGNORECASE)


def matches(text: str, summary: ConversationSummary) -> bool:
    """True if any of `summary`'s keywords appears in `text` as a whole
    phrase, ignoring case. A summary with no keywords never matches."""
    return any(_pattern(keyword).search(text) for keyword in summary.keywords)


@dataclass
class EnrichmentSession:
    """Which summaries have already been spent on one uninterrupted visit to
    one conversation."""

    conversation_id: str
    used: set[str] = field(default_factory=set)  # summary conversation ids


class MemoryEnricher:
    """Selects the summaries a user message should be enriched with, and
    tracks which have already been spent this session."""

    def __init__(self, store: ConversationStore) -> None:
        self._store = store
        self._session: EnrichmentSession | None = None

    async def select(
        self, conversation: Conversation, user_text: str
    ) -> tuple[ConversationSummary, ...]:
        """Up to `MAX_ENRICHMENTS` summaries from `conversation`'s group whose
        keywords match `user_text`, excluding the conversation's own summary
        and anything already used this session. `()` in the default group,
        without querying the store at all."""
        if conversation.group_id == DEFAULT_GROUP_ID:
            return ()
        if self._session is None or self._session.conversation_id != conversation.id:
            self._session = EnrichmentSession(conversation.id)

        summaries = await self._store.list_summaries(conversation.group_id)
        selected: list[ConversationSummary] = []
        for summary in summaries:
            if summary.conversation_id == conversation.id:
                continue
            if summary.conversation_id in self._session.used:
                continue
            if not matches(user_text, summary):
                continue
            selected.append(summary)
            if len(selected) >= MAX_ENRICHMENTS:
                break
        return tuple(selected)

    def mark_used(self, summaries: Sequence[ConversationSummary]) -> None:
        """Spend `summaries` against the current session. Separate from
        `select` so a caller can roll back an enrichment that did not survive
        context trimming without ever marking it used."""
        if self._session is None:
            return
        self._session.used.update(summary.conversation_id for summary in summaries)

    def reset(self) -> None:
        """End the current session — the next `select` starts a fresh one
        with every summary available again."""
        self._session = None
