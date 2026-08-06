"""Conversation persistence.

Only the in-memory implementation exists so far. The protocol is here now
because "resume chats" (NFR-S-01) means a durable store has to slot in without
the chat service or UI noticing, and because deletion must cascade into derived
state (NFR-S-04) — which is a property of the store, not the caller.
"""

from __future__ import annotations

from typing import Protocol

from agentchat.core.models import Conversation


class ConversationStore(Protocol):
    async def list_conversations(self, group_id: str | None = None) -> list[Conversation]:
        """Most-recently-updated first. ``group_id`` scopes to a project folder
        (NFR-S-02)."""
        ...

    async def load(self, conversation_id: str) -> Conversation | None: ...

    async def save(self, conversation: Conversation) -> None: ...

    async def delete(self, conversation_id: str) -> None:
        """Must also remove derived state (memory index, caches) — NFR-S-04."""
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
        return sorted(items, key=lambda c: c.updated_at, reverse=True)

    async def load(self, conversation_id: str) -> Conversation | None:
        return self._items.get(conversation_id)

    async def save(self, conversation: Conversation) -> None:
        self._items[conversation.id] = conversation

    async def delete(self, conversation_id: str) -> None:
        self._items.pop(conversation_id, None)
