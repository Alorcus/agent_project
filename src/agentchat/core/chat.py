"""Chat orchestration: the only thing that knows how a turn is produced.

The UI calls ``stream_reply`` and renders chunks — it knows nothing about
providers, context strategies, or the store.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass

from agentchat.core.context import ContextDecision, ContextStrategy, RecencyWindowStrategy
from agentchat.core.errors import StorageError
from agentchat.core.extraction import ExtractionService
from agentchat.core.models import DEFAULT_GROUP_ID, Conversation, ConversationSummary, Group, Message
from agentchat.llm.base import GenerationOptions
from agentchat.llm.registry import ModelRegistry
from agentchat.storage.base import ConversationStore


@dataclass
class TurnResult:
    """Handles to what a turn produced, for the UI to inspect after streaming."""

    message: Message
    context: ContextDecision


class ChatService:
    def __init__(
        self,
        registry: ModelRegistry,
        store: ConversationStore,
        context_strategy: ContextStrategy | None = None,
        extractor: ExtractionService | None = None,
    ) -> None:
        self.registry = registry
        self.store = store
        self.context_strategy = context_strategy or RecencyWindowStrategy()
        self.last_turn: TurnResult | None = None
        #: `None` switches extraction off — the only place `summarise` checks
        #: for that, so no flag needs threading through every call site.
        self.extractor = extractor
        # Held by both `stream_reply` and `summarise`: `TransformersProvider`
        # kills one `generate()` call when a second starts on it (KTD7), so
        # only one of the two may run at a time.
        self._provider_lock = asyncio.Lock()

    async def new_conversation(self, group_id: str = DEFAULT_GROUP_ID) -> Conversation:
        """Membership is chosen here and bound for life — there is no move."""
        return Conversation(group_id=group_id)

    async def list_conversations(self, group_id: str) -> list[Conversation]:
        return await self.store.list_conversations(group_id)

    async def list_all_conversations(self) -> list[Conversation]:
        return await self.store.list_all_conversations()

    async def list_groups(self) -> list[Group]:
        return await self.store.list_groups()

    async def create_group(self, name: str) -> Group:
        """The only way a group comes into existence in the application."""
        group = Group(name=name)
        await self.store.save_group(group)
        return group

    async def delete_group(self, group_id: str) -> None:
        """Takes the group's conversations with it — there is no re-homing,
        because there is no move."""
        await self.store.delete_group(group_id)

    async def switch_conversation(self, conversation_id: str) -> Conversation:
        """Resolve an id to the authoritative `Conversation` from the store.

        `store.load` returns a fresh instance, not the one a caller already
        holds — callers must not compare what they hold against what they
        load by identity.
        """
        conversation = await self.store.load(conversation_id)
        if conversation is None:
            raise StorageError(f"No conversation {conversation_id!r}")
        # last_turn describes the outgoing conversation's context-trimming
        # decision; carrying it over would report one conversation's
        # trimming against another's.
        self.last_turn = None
        return conversation

    async def delete_conversation(self, conversation_id: str) -> None:
        await self.store.delete(conversation_id)

    async def persist(self, conversation: Conversation) -> None:
        """Save `conversation`, skipping ones with no messages so an empty
        "New conversation" is never written to the store."""
        if not conversation.messages:
            return
        await self.store.save(conversation)

    async def stream_reply(
        self,
        conversation: Conversation,
        user_text: str,
        options: GenerationOptions | None = None,
    ) -> AsyncIterator[str]:
        """Append the user turn, then stream the assistant turn, yielding each
        chunk. Cancelling the consumer stops generation but keeps the partial
        reply in history."""
        conversation.add_user(user_text)
        conversation.autotitle()

        # Wraps the whole body, not just the `async for`: a consumer cancelled
        # mid-stream still has to reach the `finally` below and release the
        # lock, or every later `summarise()` on this provider deadlocks.
        async with self._provider_lock:
            provider = await self.registry.active_provider()
            decision = self.context_strategy.build(
                conversation.messages,
                context_window=provider.info.context_window,
            )

            reply = conversation.add(
                Message(role="assistant", content="", model_id=provider.info.id)
            )
            reply.metadata["context"] = {
                "estimated_tokens": decision.estimated_tokens,
                "budget": decision.budget,
                "dropped": len(decision.dropped),
                "notes": decision.notes,
            }
            self.last_turn = TurnResult(message=reply, context=decision)

            parts: list[str] = []
            try:
                async for chunk in provider.generate(decision.messages, options):
                    parts.append(chunk)
                    reply.content = "".join(parts)
                    yield chunk
            finally:
                reply.content = "".join(parts)
                conversation.touch()
                await self.persist(conversation)

    async def summarise(self, conversation: Conversation) -> ConversationSummary | None:
        """Extract and persist a summary for `conversation`, unless there is
        nothing to do: no extractor configured, no messages yet, or no
        messages since the last summary (R7's watermark)."""
        if self.extractor is None or not conversation.messages:
            return None

        existing = await self.store.summary(conversation.id)
        if existing is not None and existing.covered_messages == len(conversation.messages):
            return None

        async with self._provider_lock:
            summary = await self.extractor.run(conversation)
        if existing is not None:
            summary.created_at = existing.created_at

        await self.store.save_summary(summary)
        return summary
