"""Conversation persistence: the protocol and its shared ordering/guard helpers."""

from __future__ import annotations

from typing import Protocol

from collections.abc import Sequence

from agentchat.core.errors import StorageError
from agentchat.core.models import (
    DEFAULT_GROUP_ID,
    Conversation,
    Document,
    Fact,
    FactEmbedding,
    Group,
    Snippet,
    SnippetEmbedding,
)


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

    # -- derived state: facts ----------------------------------------------
    #
    # Append-only — a window's fact, once saved, is never rewritten. Its
    # vectors (see `save_fact_embeddings`) are written alongside it.

    async def save_facts(self, facts: Sequence[Fact]) -> None:
        """Append `facts`. A no-op for `()`."""
        ...

    async def list_facts(self, group_id: str) -> list[Fact]: ...

    async def fact_watermark(self, conversation_id: str) -> int:
        """How many countable messages have been extracted. `0` when
        unknown."""
        ...

    async def set_fact_watermark(self, conversation_id: str, covered: int) -> None: ...

    # -- derived state: fact embeddings -----------------------------------
    #
    # `retrieval.py` is the only module that reads these back.

    async def save_fact_embeddings(self, rows: Sequence[FactEmbedding]) -> None:
        """Upsert on `(fact_id, view, model_id)`. A no-op for `()`."""
        ...

    async def fact_embeddings(
        self, group_id: str, *, model_id: str
    ) -> list[FactEmbedding]:
        """Every vector for the group's facts produced by `model_id`."""
        ...

    async def facts_without_embeddings(
        self, group_id: str, *, model_id: str
    ) -> list[Fact]:
        """The group's facts with no `fact_embeddings` row for `model_id` —
        one `LEFT JOIN`, not a Python set difference over every fact."""
        ...

    # -- derived state: documents ------------------------------------------
    #
    # Global, not group-scoped: a document ingested from any chat is
    # searchable from every chat.

    async def save_document(self, document: Document, snippets: Sequence[Snippet]) -> None:
        """Write the document and its snippets in one transaction.

        Raises `StorageError` when `document.content_hash` is already stored —
        the caller checks `document_by_hash` first.
        """
        ...

    async def document(self, document_id: str) -> Document | None:
        """With `text` filled in — the only loader that fills it."""
        ...

    async def document_by_hash(self, content_hash: str) -> Document | None: ...

    async def document_by_path(self, path: str) -> Document | None: ...

    async def list_documents(self) -> list[Document]:
        """Most-recently-ingested first, each with `text` left empty."""
        ...

    async def list_snippets(self, document_id: str) -> list[Snippet]:
        """In `ordinal` order."""
        ...

    async def snippets_by_ids(self, ids: Sequence[str]) -> list[Snippet]:
        """The named snippets, in no particular order. `[]` for `()`."""
        ...

    async def delete_document(self, document_id: str) -> None:
        """Takes the document's snippets and their vectors with it."""
        ...

    # -- derived state: snippet embeddings ---------------------------------
    #
    # `retrieval.SnippetIndex` is the only reader.

    async def save_snippet_embeddings(self, rows: Sequence[SnippetEmbedding]) -> None:
        """Upsert on `(snippet_id, model_id)`. A no-op for `()`."""
        ...

    async def snippet_vectors(self, *, model_id: str) -> list[SnippetEmbedding]:
        """Every snippet vector produced by `model_id` — ids and vectors, no
        text: search ranks over these and hydrates only the winners."""
        ...

    async def snippets_without_embeddings(self, *, model_id: str) -> list[Snippet]:
        """Snippets with no `snippet_embeddings` row for `model_id` — one
        `LEFT JOIN`, not a Python set difference over every snippet."""
        ...
