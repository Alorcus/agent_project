"""Chat orchestration: the only thing that knows how a turn is produced.

The UI calls ``stream_reply`` and renders chunks — it knows nothing about
providers, context strategies, or the store.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

from agentchat.core.context import ContextDecision, ContextStrategy, RecencyWindowStrategy
from agentchat.core.errors import StorageError
from agentchat.core.memory.store import ApplyResult
from agentchat.core.memory.strategy import ChatMemory
from agentchat.core.models import DEFAULT_GROUP_ID, Conversation, Message
from agentchat.llm.base import GenerationOptions
from agentchat.llm.registry import ModelRegistry
from agentchat.storage.base import ConversationStore, InMemoryStore

_LOG = logging.getLogger(__name__)


@dataclass
class TurnResult:
    """Handles to what a turn produced, for the UI to inspect after streaming."""

    message: Message
    context: ContextDecision


class ChatService:
    def __init__(
        self,
        registry: ModelRegistry,
        store: ConversationStore | None = None,
        context_strategy: ContextStrategy | None = None,
        memory: ChatMemory | None = None,
    ) -> None:
        self.registry = registry
        self.store = store or InMemoryStore()
        self.context_strategy = context_strategy or RecencyWindowStrategy()
        self.memory = memory
        self.last_turn: TurnResult | None = None
        self._extraction_task: asyncio.Task | None = None

    async def new_conversation(self, group_id: str = DEFAULT_GROUP_ID) -> Conversation:
        return Conversation(group_id=group_id)

    async def list_conversations(self, group_id: str | None = None) -> list[Conversation]:
        """Every conversation when `group_id` is omitted, one group's
        otherwise — the picker is group-blind until stage 6."""
        if group_id is None:
            return await self.store.list_all_conversations()
        return await self.store.list_conversations(group_id)

    async def list_all_conversations(self) -> list[Conversation]:
        return await self.store.list_all_conversations()

    async def switch_conversation(self, conversation_id: str) -> Conversation:
        """Resolve an id to the authoritative `Conversation` from the store.

        The single place id -> object resolution happens, so callers never
        need to guess whether `store.load` returns the same instance they
        already hold (`InMemoryStore` does; `SqliteStore` doesn't).
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
            self.schedule_extraction(conversation, thinking=bool(options and options.thinking))

    # -- memory extraction --------------------------------------------------

    def schedule_extraction(
        self, conversation: Conversation, *, thinking: bool = False
    ) -> asyncio.Task | None:
        """Start a background extraction run when a batch is due and none is
        already in flight. A batch skipped because one is running is not
        lost — `due` reads the watermark, so the next turn asks again."""
        if self.memory is None or not self.memory.due(conversation):
            return None
        if self._extraction_task is not None and not self._extraction_task.done():
            return None

        async def _run() -> None:
            try:
                await self.memory.extract(conversation, thinking=thinking)
            except Exception:
                # Off the reply path by design (§ 2.1): the watermark did not
                # move, so the next run picks the range back up on its own.
                _LOG.exception("background extraction failed")

        task = asyncio.create_task(_run())
        self._extraction_task = task
        return task

    async def wait_for_extraction(self) -> None:
        if self._extraction_task is not None:
            await asyncio.gather(self._extraction_task, return_exceptions=True)

    async def flush_extraction(
        self, conversation: Conversation, *, thinking: bool = False
    ) -> ApplyResult | None:
        """Await any in-flight run, then extract whatever is still pending
        regardless of `EXTRACT_EVERY` — what leaving a conversation owes it."""
        await self.wait_for_extraction()
        if self.memory is None:
            return None
        return await self.memory.extract(conversation, thinking=thinking)

    def cancel_extraction(self) -> None:
        if self._extraction_task is not None and not self._extraction_task.done():
            self._extraction_task.cancel()

    async def backfill(
        self, conversation: Conversation, *, thinking: bool = False
    ) -> ApplyResult | None:
        """The same path as every other run, with the watermark ignored — a
        trigger, not a separate mechanism."""
        if self.memory is None:
            return None
        return await self.memory.extract(conversation, thinking=thinking, ignore_watermark=True)
