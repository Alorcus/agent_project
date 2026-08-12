"""Conversation persistence: the protocol and its shared ordering/guard helpers."""

from __future__ import annotations

from typing import Protocol

from agentchat.core.errors import StorageError
from agentchat.core.models import DEFAULT_GROUP_ID, Conversation, ConversationSummary, Group


def by_recency(conversations: list[Conversation]) -> list[Conversation]:
    """Most-recently-updated first — the ordering `ConversationStore.list_conversations`
    promises."""
    return sorted(conversations, key=lambda c: c.updated_at, reverse=True)


def by_default_first(groups: list[Group]) -> list[Group]:
    """The default group first, then the rest by creation order — the ordering
    `ConversationStore.list_groups` promises."""
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

    # -- derived state: conversation summaries ----------------------------
    #
    # No consumer yet: this is the read half of NFR-S-02 (summaries scoped to
    # a group), shipped so a later group overview can be additive.

    async def save_summary(self, summary: ConversationSummary) -> None:
        """Upsert on `conversation_id`, preserving the original `created_at`."""
        ...

    async def summary(self, conversation_id: str) -> ConversationSummary | None: ...

    async def list_summaries(self, group_id: str) -> list[ConversationSummary]:
        """Most-recently-updated first."""
        ...
