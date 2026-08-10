"""Conversation persistence: protocol plus an in-memory implementation."""

from __future__ import annotations

from typing import Protocol

from agentchat.core.models import Conversation


def by_recency(conversations: list[Conversation]) -> list[Conversation]:
    """Most-recently-updated first — the ordering every `ConversationStore`
    implementation's `list_conversations` promises."""
    return sorted(conversations, key=lambda c: c.updated_at, reverse=True)


class ConversationStore(Protocol):
    async def list_conversations(self, group_id: str | None = None) -> list[Conversation]:
        """Most-recently-updated first, optionally scoped to a project folder."""
        ...

    async def load(self, conversation_id: str) -> Conversation | None: ...

    async def save(self, conversation: Conversation) -> None: ...

    async def delete(self, conversation_id: str) -> None:
        """Must also remove any derived state (memory index, caches)."""
        ...


class InMemoryStore:
    """Non-durable store. Correct shape, wrong lifetime — replaced by SQLite."""

    def __init__(self) -> None:
        self._items: dict[str, Conversation] = {}

    async def list_conversations(
        self, group_id: str | None = None
    ) -> list[Conversation]:
        items = [
            c
            for c in self._items.values()
            if group_id is None or c.group_id == group_id
        ]
        return by_recency(items)

    async def load(self, conversation_id: str) -> Conversation | None:
        return self._items.get(conversation_id)

    async def save(self, conversation: Conversation) -> None:
        self._items[conversation.id] = conversation

    async def delete(self, conversation_id: str) -> None:
        self._items.pop(conversation_id, None)
