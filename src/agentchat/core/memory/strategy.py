"""Chat-side adapters and the batching decision. The one file in
``core/memory/`` licensed to import chat types (I-6 exempts it): everything
else in this package must stay ignorant of ``Message``/``Conversation`` so it
can bind to a future persona pipeline instead.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime

from agentchat.core.memory.extract import MemoryExtractor
from agentchat.core.memory.store import ApplyResult, MemoryStore
from agentchat.core.memory.tuning import Tuning
from agentchat.core.models import Conversation, Message
from agentchat.llm.base import LLMProvider

ProviderSupplier = Callable[[], Awaitable[LLMProvider]]


class MessageEvidence:
    """``Message -> EvidenceItem``. ``EvidenceItem`` has no role field — a
    chat turn's role and an email's sender occupy the same slot — so the role
    rides into ``text`` alongside the content."""

    def __init__(self, message: Message) -> None:
        self.id = message.id
        # Blank stays blank rather than becoming "assistant: " — a cancelled
        # reply's empty turn must still read as blank to `observe`'s filter.
        self.text = f"{message.role}: {message.content}" if message.content.strip() else ""
        self.created_at = message.created_at


class ConversationEvidence:
    """``Conversation -> EvidenceSource``: the messages after the watermark,
    or every message when ``ignore_watermark`` is set (backfill — the same
    path, just a null watermark)."""

    def __init__(self, conversation: Conversation, *, ignore_watermark: bool = False) -> None:
        self.id = conversation.id
        self.scope_id = conversation.group_id

        after: tuple[datetime, str] | None = None
        if not ignore_watermark and conversation.extracted_at is not None:
            after = (conversation.extracted_at, conversation.extracted_id)

        messages = conversation.messages
        if after is not None:
            # The lexicographic range § 2.1 spends a paragraph justifying:
            # `id` breaks ties between messages created in the same instant.
            messages = [m for m in messages if (m.created_at, m.id) > after]

        self.items: tuple[MessageEvidence, ...] = tuple(MessageEvidence(m) for m in messages)
        # What was *read*, blank turns included — not what was claimed, or a
        # cancelled reply at the end of a batch is re-read forever.
        self.read_through: tuple[datetime, str] | None = (
            (messages[-1].created_at, messages[-1].id) if messages else None
        )


class ChatMemory:
    """Owns adapting a conversation into evidence and running extraction over
    it. ``ChatService`` owns the scheduling/batching around that."""

    def __init__(
        self, store: MemoryStore, provider_for: ProviderSupplier, tuning: Tuning | None = None
    ) -> None:
        self._store = store
        self._provider_for = provider_for
        self._tuning = tuning or Tuning.from_env()

    def pending(self, conversation: Conversation) -> int:
        return len(ConversationEvidence(conversation).items)

    def due(self, conversation: Conversation) -> bool:
        return self.pending(conversation) >= self._tuning.extract_every

    async def extract(
        self,
        conversation: Conversation,
        *,
        thinking: bool = False,
        ignore_watermark: bool = False,
    ) -> ApplyResult | None:
        source = ConversationEvidence(conversation, ignore_watermark=ignore_watermark)
        # The resident provider, not a model of extraction's own — a
        # background run must never evict the model the user is talking to.
        provider = await self._provider_for()
        extractor = MemoryExtractor(self._store, provider, self._tuning)
        result = await extractor.run(source, thinking=thinking)
        if result is not None:
            # The store row is authoritative; without this the live object
            # the UI holds stays stale and the next batch re-reads the range.
            conversation.extracted_at, conversation.extracted_id = source.read_through
        return result
