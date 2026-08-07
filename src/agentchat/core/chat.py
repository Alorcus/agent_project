"""Chat orchestration: the only thing that knows how a turn is produced.

The UI calls ``stream_reply`` and renders chunks — it knows nothing about
providers, context strategies, or the store.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

from agentchat.core.context import ContextDecision, ContextStrategy, RecencyWindowStrategy
from agentchat.core.models import Conversation, Message
from agentchat.llm.base import GenerationOptions
from agentchat.llm.registry import ModelRegistry
from agentchat.storage.base import ConversationStore, InMemoryStore


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
    ) -> None:
        self.registry = registry
        self.store = store or InMemoryStore()
        self.context_strategy = context_strategy or RecencyWindowStrategy()
        self.last_turn: TurnResult | None = None

    async def new_conversation(self, group_id: str | None = None) -> Conversation:
        conversation = Conversation(group_id=group_id)
        await self.store.save(conversation)
        return conversation

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
            await self.store.save(conversation)
