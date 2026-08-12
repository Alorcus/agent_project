"""Conversation persistence: protocol plus an in-memory implementation."""

from __future__ import annotations

from typing import Protocol

from agentchat.core.errors import StorageError
from agentchat.core.models import DEFAULT_GROUP_ID, Conversation, Group
from agentchat.storage.schema import DEFAULT_GROUP_NAME


def by_recency(conversations: list[Conversation]) -> list[Conversation]:
    """Most-recently-updated first — the ordering every `ConversationStore`
    implementation's `list_conversations` promises."""
    return sorted(conversations, key=lambda c: c.updated_at, reverse=True)


def by_default_first(groups: list[Group]) -> list[Group]:
    """The default group first, then the rest by creation order — the ordering
    every `ConversationStore` implementation's `list_groups` promises."""
    return sorted(groups, key=lambda g: (g.is_project(), g.created_at))


def refuse_default_group(group_id: str) -> None:
    """The guard every `delete_group` opens with: the default group is what a
    conversation lands in when no other is chosen, so it always exists."""
    if group_id == DEFAULT_GROUP_ID:
        raise StorageError("the default group cannot be deleted")


class ConversationStore(Protocol):
    async def list_conversations(self, group_id: str = DEFAULT_GROUP_ID) -> list[Conversation]:
        """Most-recently-updated first, scoped to one group."""
        ...

    async def list_all_conversations(self) -> list[Conversation]:
        """Every conversation regardless of group, most-recently-updated first."""
        ...

    async def list_groups(self) -> list[Group]:
        """The default group first, then the rest by creation order."""
        ...

    async def save_group(self, group: Group) -> None: ...

    async def delete_group(self, group_id: str) -> None:
        """Delete the group **and its conversations** — membership is bound
        for life, so they are not re-homed to the default group.

        Raises `StorageError` for the default group, which is not deletable.
        """
        ...

    async def default_group(self) -> Group: ...

    async def load(self, conversation_id: str) -> Conversation | None: ...

    async def save(self, conversation: Conversation) -> None:
        """Raises `StorageError` if `conversation.group_id` names no group (I-1)."""
        ...

    async def delete(self, conversation_id: str) -> None:
        """Must also remove any derived state (memory index, caches)."""
        ...


class InMemoryStore:
    """Non-durable store. Correct shape, wrong lifetime — replaced by SQLite."""

    def __init__(self) -> None:
        self._items: dict[str, Conversation] = {}
        self._groups: dict[str, Group] = {
            DEFAULT_GROUP_ID: Group(id=DEFAULT_GROUP_ID, name=DEFAULT_GROUP_NAME, kind="default")
        }

    async def list_conversations(self, group_id: str = DEFAULT_GROUP_ID) -> list[Conversation]:
        items = [c for c in self._items.values() if c.group_id == group_id]
        return by_recency(items)

    async def list_all_conversations(self) -> list[Conversation]:
        return by_recency(list(self._items.values()))

    async def list_groups(self) -> list[Group]:
        return by_default_first(list(self._groups.values()))

    async def save_group(self, group: Group) -> None:
        self._groups[group.id] = group

    async def delete_group(self, group_id: str) -> None:
        refuse_default_group(group_id)
        self._groups.pop(group_id, None)
        # The cascade SQLite's FK does, by hand.
        for conversation_id in [
            c.id for c in self._items.values() if c.group_id == group_id
        ]:
            del self._items[conversation_id]

    async def default_group(self) -> Group:
        return self._groups[DEFAULT_GROUP_ID]

    async def load(self, conversation_id: str) -> Conversation | None:
        return self._items.get(conversation_id)

    async def save(self, conversation: Conversation) -> None:
        # No foreign key to do it for us — a stub that quietly permits what the
        # real store forbids would let I-1 break in every test using it.
        if conversation.group_id not in self._groups:
            raise StorageError(f"unknown group {conversation.group_id!r}")
        self._items[conversation.id] = conversation

    async def delete(self, conversation_id: str) -> None:
        self._items.pop(conversation_id, None)
