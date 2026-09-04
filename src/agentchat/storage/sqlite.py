"""SQLite-backed `ConversationStore`: conversations survive process restarts.

Each method offloads its synchronous sqlite3 body to a worker thread via
`asyncio.to_thread`, so storage I/O never blocks the event loop. The schema
itself lives in `storage.schema`.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from datetime import datetime
from pathlib import Path

import numpy as np

from agentchat.core.errors import StorageError
from agentchat.core.models import (
    DEFAULT_GROUP_ID,
    Author,
    Conversation,
    Document,
    Fact,
    FactEmbedding,
    Group,
    Message,
    Phrase,
    Snippet,
    SnippetEmbedding,
    _now,
)
from agentchat.storage import schema
from agentchat.storage.base import by_default_first, by_recency, refuse_default_group


class SqliteStore:
    """Durable `ConversationStore`. See `storage.base.ConversationStore`."""

    def __init__(self, path: Path) -> None:
        self._path = path
        schema.ensure_schema(path)

    def _connect(self) -> sqlite3.Connection:
        return schema.connect(self._path)

    async def list_conversations(self, group_id: str = DEFAULT_GROUP_ID) -> list[Conversation]:
        return await asyncio.to_thread(self._list_conversations, group_id)

    async def list_all_conversations(self) -> list[Conversation]:
        return await asyncio.to_thread(self._list_conversations, None)

    async def list_groups(self) -> list[Group]:
        return await asyncio.to_thread(self._list_groups)

    async def save_group(self, group: Group) -> None:
        await asyncio.to_thread(self._save_group, group)

    async def delete_group(self, group_id: str) -> None:
        await asyncio.to_thread(self._delete_group, group_id)

    async def default_group(self) -> Group:
        groups = await self.list_groups()
        return groups[0]

    async def load(self, conversation_id: str) -> Conversation | None:
        return await asyncio.to_thread(self._load, conversation_id)

    async def save(self, conversation: Conversation) -> None:
        await asyncio.to_thread(self._save, conversation)

    async def delete(self, conversation_id: str) -> None:
        await asyncio.to_thread(self._delete, conversation_id)

    async def save_facts(self, facts: Sequence[Fact]) -> None:
        if not facts:
            return
        await asyncio.to_thread(self._save_facts, facts)

    async def list_facts(self, group_id: str) -> list[Fact]:
        return await asyncio.to_thread(self._list_facts, group_id)

    async def fact_watermark(self, conversation_id: str) -> int:
        return await asyncio.to_thread(self._fact_watermark, conversation_id)

    async def set_fact_watermark(self, conversation_id: str, covered: int) -> None:
        await asyncio.to_thread(self._set_fact_watermark, conversation_id, covered)

    async def save_fact_embeddings(self, rows: Sequence[FactEmbedding]) -> None:
        if not rows:
            return
        await asyncio.to_thread(self._save_fact_embeddings, rows)

    async def fact_embeddings(
        self, group_id: str, *, model_id: str
    ) -> list[FactEmbedding]:
        return await asyncio.to_thread(self._fact_embeddings, group_id, model_id)

    async def facts_without_embeddings(
        self, group_id: str, *, model_id: str
    ) -> list[Fact]:
        return await asyncio.to_thread(
            self._facts_without_embeddings, group_id, model_id
        )

    async def save_document(self, document: Document, snippets: Sequence[Snippet]) -> None:
        await asyncio.to_thread(self._save_document, document, snippets)

    async def document(self, document_id: str) -> Document | None:
        return await asyncio.to_thread(self._document, "id", document_id, True)

    async def document_by_hash(self, content_hash: str) -> Document | None:
        return await asyncio.to_thread(self._document, "content_hash", content_hash, False)

    async def document_by_path(self, path: str) -> Document | None:
        return await asyncio.to_thread(self._document, "path", path, False)

    async def list_documents(self) -> list[Document]:
        return await asyncio.to_thread(self._list_documents)

    async def list_snippets(self, document_id: str) -> list[Snippet]:
        return await asyncio.to_thread(self._list_snippets, document_id)

    async def snippets_by_ids(self, ids: Sequence[str]) -> list[Snippet]:
        if not ids:
            return []
        return await asyncio.to_thread(self._snippets_by_ids, tuple(ids))

    async def delete_document(self, document_id: str) -> None:
        await asyncio.to_thread(self._delete_document, document_id)

    async def save_snippet_embeddings(self, rows: Sequence[SnippetEmbedding]) -> None:
        if not rows:
            return
        await asyncio.to_thread(self._save_snippet_embeddings, rows)

    async def snippet_vectors(self, *, model_id: str) -> list[SnippetEmbedding]:
        return await asyncio.to_thread(self._snippet_vectors, model_id)

    async def snippets_without_embeddings(self, *, model_id: str) -> list[Snippet]:
        return await asyncio.to_thread(self._snippets_without_embeddings, model_id)

    def _list_conversations(self, group_id: str | None) -> list[Conversation]:
        try:
            with closing(self._connect()) as conn, conn:
                if group_id is None:
                    rows = conn.execute(
                        "SELECT id FROM conversations"
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT id FROM conversations WHERE group_id = ?",
                        (group_id,),
                    ).fetchall()
                items = [self._load_conn(conn, row[0]) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError("failed to list conversations") from exc
        items = [c for c in items if c is not None]
        return by_recency(items)

    def _list_groups(self) -> list[Group]:
        try:
            with closing(self._connect()) as conn, conn:
                rows = conn.execute(
                    "SELECT id, name, kind, created_at FROM groups"
                ).fetchall()
        except sqlite3.Error as exc:
            raise StorageError("failed to list groups") from exc
        groups = [
            Group(id=id_, name=name, kind=kind, created_at=datetime.fromisoformat(created_at))
            for id_, name, kind, created_at in rows
        ]
        return by_default_first(groups)

    def _save_group(self, group: Group) -> None:
        try:
            with closing(self._connect()) as conn, conn:
                conn.execute(
                    "INSERT INTO groups (id, name, kind, created_at) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET "
                    "name = excluded.name, kind = excluded.kind",
                    (group.id, group.name, group.kind, group.created_at.isoformat()),
                )
        except sqlite3.Error as exc:
            raise StorageError(f"failed to save group {group.id}") from exc

    def _delete_group(self, group_id: str) -> None:
        refuse_default_group(group_id)
        try:
            # One statement, one transaction: the FKs carry it from here to the
            # group's conversations and on to their messages.
            with closing(self._connect()) as conn, conn:
                conn.execute("DELETE FROM groups WHERE id = ?", (group_id,))
        except sqlite3.Error as exc:
            raise StorageError(f"failed to delete group {group_id}") from exc

    def _load(self, conversation_id: str) -> Conversation | None:
        try:
            with closing(self._connect()) as conn, conn:
                return self._load_conn(conn, conversation_id)
        except sqlite3.Error as exc:
            raise StorageError(f"failed to load conversation {conversation_id}") from exc

    def _load_conn(self, conn: sqlite3.Connection, conversation_id: str) -> Conversation | None:
        row = conn.execute(
            "SELECT id, title, group_id, created_at, updated_at "
            "FROM conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()
        if row is None:
            return None
        message_rows = conn.execute(
            "SELECT id, role, content, created_at, model_id, metadata "
            "FROM messages WHERE conversation_id = ? ORDER BY ordinal",
            (conversation_id,),
        ).fetchall()
        messages = [
            Message(
                id=m_id,
                role=role,
                content=content,
                created_at=datetime.fromisoformat(created_at),
                model_id=model_id,
                metadata=json.loads(metadata),
            )
            for m_id, role, content, created_at, model_id, metadata in message_rows
        ]
        _, title, group_id, created_at, updated_at = row
        return Conversation(
            id=conversation_id,
            title=title,
            group_id=group_id,
            created_at=datetime.fromisoformat(created_at),
            updated_at=datetime.fromisoformat(updated_at),
            messages=messages,
        )

    def _save(self, conversation: Conversation) -> None:
        try:
            with closing(self._connect()) as conn, conn:
                conn.execute(
                    "INSERT INTO conversations (id, title, group_id, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET "
                    "title = excluded.title, group_id = excluded.group_id, "
                    "created_at = excluded.created_at, updated_at = excluded.updated_at",
                    (
                        conversation.id,
                        conversation.title,
                        conversation.group_id,
                        conversation.created_at.isoformat(),
                        conversation.updated_at.isoformat(),
                    ),
                )
                conn.execute(
                    "DELETE FROM messages WHERE conversation_id = ?",
                    (conversation.id,),
                )
                conn.executemany(
                    "INSERT INTO messages "
                    "(id, conversation_id, ordinal, role, content, created_at, model_id, metadata) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            m.id,
                            conversation.id,
                            ordinal,
                            m.role,
                            m.content,
                            m.created_at.isoformat(),
                            m.model_id,
                            json.dumps(m.metadata),
                        )
                        for ordinal, m in enumerate(conversation.messages)
                    ],
                )
        except sqlite3.Error as exc:
            raise StorageError(f"failed to save conversation {conversation.id}") from exc

    def _delete(self, conversation_id: str) -> None:
        try:
            with closing(self._connect()) as conn, conn:
                conn.execute(
                    "DELETE FROM conversations WHERE id = ?", (conversation_id,)
                )
        except sqlite3.Error as exc:
            raise StorageError(f"failed to delete conversation {conversation_id}") from exc

    def _save_facts(self, facts: Sequence[Fact]) -> None:
        try:
            with closing(self._connect()) as conn, conn:
                for fact in facts:
                    conn.execute(
                        "INSERT INTO facts "
                        "(id, conversation_id, group_id, text, "
                        "window_start, window_end, model_id, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            fact.id,
                            fact.conversation_id,
                            fact.group_id,
                            fact.text,
                            fact.window_start,
                            fact.window_end,
                            fact.model_id,
                            fact.created_at.isoformat(),
                        ),
                    )
                    conn.executemany(
                        "INSERT INTO fact_phrases "
                        '(fact_id, ordinal, message_id, start, "end", '
                        "author_kind, author_label, quote) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        [
                            (
                                fact.id,
                                ordinal,
                                phrase.message_id,
                                phrase.start,
                                phrase.end,
                                phrase.author.kind,
                                phrase.author.label,
                                phrase.quote,
                            )
                            for ordinal, phrase in enumerate(fact.phrases)
                        ],
                    )
        except sqlite3.Error as exc:
            raise StorageError("failed to save facts") from exc

    def _list_facts(self, group_id: str) -> list[Fact]:
        try:
            with closing(self._connect()) as conn, conn:
                fact_rows = conn.execute(
                    "SELECT id, conversation_id, group_id, text, "
                    "window_start, window_end, model_id, created_at "
                    "FROM facts WHERE group_id = ?",
                    (group_id,),
                ).fetchall()
                phrase_rows = conn.execute(
                    "SELECT fact_id, ordinal, message_id, start, \"end\", "
                    "author_kind, author_label, quote FROM fact_phrases "
                    "WHERE fact_id IN (SELECT id FROM facts WHERE group_id = ?) "
                    "ORDER BY fact_id, ordinal",
                    (group_id,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise StorageError(f"failed to list facts for group {group_id}") from exc

        phrases_by_fact: dict[str, list[Phrase]] = {}
        for (
            fact_id,
            _ordinal,
            message_id,
            start,
            end,
            author_kind,
            author_label,
            quote,
        ) in phrase_rows:
            phrases_by_fact.setdefault(fact_id, []).append(
                Phrase(
                    message_id=message_id,
                    start=start,
                    end=end,
                    author=Author(kind=author_kind, label=author_label),
                    quote=quote,
                )
            )
        return [_fact_from_row(row, phrases_by_fact.get(row[0], [])) for row in fact_rows]

    def _fact_watermark(self, conversation_id: str) -> int:
        try:
            with closing(self._connect()) as conn, conn:
                row = conn.execute(
                    "SELECT covered_messages FROM fact_extraction_state "
                    "WHERE conversation_id = ?",
                    (conversation_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise StorageError(
                f"failed to load fact watermark for conversation {conversation_id}"
            ) from exc
        return 0 if row is None else row[0]

    def _set_fact_watermark(self, conversation_id: str, covered: int) -> None:
        try:
            with closing(self._connect()) as conn, conn:
                conn.execute(
                    "INSERT INTO fact_extraction_state "
                    "(conversation_id, covered_messages, updated_at) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(conversation_id) DO UPDATE SET "
                    "covered_messages = excluded.covered_messages, "
                    "updated_at = excluded.updated_at",
                    (conversation_id, covered, _now().isoformat()),
                )
        except sqlite3.Error as exc:
            raise StorageError(
                f"failed to save fact watermark for conversation {conversation_id}"
            ) from exc

    def _save_fact_embeddings(self, rows: Sequence[FactEmbedding]) -> None:
        now = _now().isoformat()
        try:
            with closing(self._connect()) as conn, conn:
                conn.executemany(
                    "INSERT INTO fact_embeddings "
                    "(fact_id, view, model_id, dim, vector, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(fact_id, view, model_id) DO UPDATE SET "
                    "dim = excluded.dim, vector = excluded.vector, "
                    "created_at = excluded.created_at",
                    [
                        (
                            row.fact_id,
                            row.view,
                            row.model_id,
                            len(row.vector),
                            _pack_vector(row.vector),
                            now,
                        )
                        for row in rows
                    ],
                )
        except sqlite3.Error as exc:
            raise StorageError("failed to save fact embeddings") from exc

    def _fact_embeddings(self, group_id: str, model_id: str) -> list[FactEmbedding]:
        try:
            with closing(self._connect()) as conn, conn:
                rows = conn.execute(
                    "SELECT fact_id, view, model_id, vector FROM fact_embeddings "
                    "WHERE model_id = ? AND fact_id IN "
                    "(SELECT id FROM facts WHERE group_id = ?)",
                    (model_id, group_id),
                ).fetchall()
        except sqlite3.Error as exc:
            raise StorageError(
                f"failed to list fact embeddings for group {group_id}"
            ) from exc
        return [
            FactEmbedding(
                fact_id=fact_id,
                view=view,
                model_id=row_model_id,
                vector=_unpack_vector(blob),
            )
            for fact_id, view, row_model_id, blob in rows
        ]

    def _facts_without_embeddings(self, group_id: str, model_id: str) -> list[Fact]:
        try:
            with closing(self._connect()) as conn, conn:
                fact_rows = conn.execute(
                    "SELECT f.id, f.conversation_id, f.group_id, f.text, "
                    "f.window_start, f.window_end, f.model_id, f.created_at "
                    "FROM facts f LEFT JOIN fact_embeddings e "
                    "ON e.fact_id = f.id AND e.model_id = ? "
                    "WHERE f.group_id = ? AND e.fact_id IS NULL",
                    (model_id, group_id),
                ).fetchall()
                if not fact_rows:
                    return []
                ids = {row[0] for row in fact_rows}
                phrase_rows = conn.execute(
                    "SELECT fact_id, ordinal, message_id, start, \"end\", "
                    "author_kind, author_label, quote FROM fact_phrases "
                    "WHERE fact_id IN (SELECT id FROM facts WHERE group_id = ?) "
                    "ORDER BY fact_id, ordinal",
                    (group_id,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise StorageError(
                f"failed to list unembedded facts for group {group_id}"
            ) from exc

        phrases_by_fact: dict[str, list[Phrase]] = {}
        for (
            fact_id,
            _ordinal,
            message_id,
            start,
            end,
            author_kind,
            author_label,
            quote,
        ) in phrase_rows:
            if fact_id not in ids:
                continue
            phrases_by_fact.setdefault(fact_id, []).append(
                Phrase(
                    message_id=message_id,
                    start=start,
                    end=end,
                    author=Author(kind=author_kind, label=author_label),
                    quote=quote,
                )
            )
        return [_fact_from_row(row, phrases_by_fact.get(row[0], [])) for row in fact_rows]

    # -- documents ---------------------------------------------------------

    def _save_document(self, document: Document, snippets: Sequence[Snippet]) -> None:
        try:
            with closing(self._connect()) as conn, conn:
                conn.execute(
                    "INSERT INTO documents (id, title, path, media_type, "
                    "content_hash, text, char_count, snippet_count, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        document.id,
                        document.title,
                        document.path,
                        document.media_type,
                        document.content_hash,
                        document.text,
                        document.char_count,
                        document.snippet_count,
                        document.created_at.isoformat(),
                    ),
                )
                conn.executemany(
                    "INSERT INTO document_snippets "
                    '(id, document_id, ordinal, text, start, "end", page) '
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            snippet.id,
                            document.id,
                            snippet.ordinal,
                            snippet.text,
                            snippet.start,
                            snippet.end,
                            snippet.page,
                        )
                        for snippet in snippets
                    ],
                )
        except sqlite3.Error as exc:
            raise StorageError(f"failed to save document {document.title!r}") from exc

    def _document(self, column: str, value: str, with_text: bool) -> Document | None:
        # `column` is chosen by the three callers above from a fixed set, never
        # by anything the user typed.
        try:
            with closing(self._connect()) as conn, conn:
                row = conn.execute(
                    f"SELECT {_DOCUMENT_COLUMNS} FROM documents WHERE {column} = ? "
                    "ORDER BY created_at DESC",
                    (value,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise StorageError(f"failed to load document by {column}") from exc
        return None if row is None else _document_from_row(row, with_text=with_text)

    def _list_documents(self) -> list[Document]:
        try:
            with closing(self._connect()) as conn, conn:
                rows = conn.execute(
                    f"SELECT {_DOCUMENT_COLUMNS} FROM documents ORDER BY created_at DESC"
                ).fetchall()
        except sqlite3.Error as exc:
            raise StorageError("failed to list documents") from exc
        return [_document_from_row(row, with_text=False) for row in rows]

    def _list_snippets(self, document_id: str) -> list[Snippet]:
        try:
            with closing(self._connect()) as conn, conn:
                rows = conn.execute(
                    f"SELECT {_SNIPPET_COLUMNS} FROM document_snippets "
                    "WHERE document_id = ? ORDER BY ordinal",
                    (document_id,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise StorageError(
                f"failed to list snippets for document {document_id}"
            ) from exc
        return [_snippet_from_row(row) for row in rows]

    def _snippets_by_ids(self, ids: tuple[str, ...]) -> list[Snippet]:
        placeholders = ", ".join("?" * len(ids))
        try:
            with closing(self._connect()) as conn, conn:
                rows = conn.execute(
                    f"SELECT {_SNIPPET_COLUMNS} FROM document_snippets "
                    f"WHERE id IN ({placeholders})",
                    ids,
                ).fetchall()
        except sqlite3.Error as exc:
            raise StorageError("failed to load snippets") from exc
        return [_snippet_from_row(row) for row in rows]

    def _delete_document(self, document_id: str) -> None:
        try:
            with closing(self._connect()) as conn, conn:
                conn.execute("DELETE FROM documents WHERE id = ?", (document_id,))
        except sqlite3.Error as exc:
            raise StorageError(f"failed to delete document {document_id}") from exc

    def _save_snippet_embeddings(self, rows: Sequence[SnippetEmbedding]) -> None:
        now = _now().isoformat()
        try:
            with closing(self._connect()) as conn, conn:
                conn.executemany(
                    "INSERT INTO snippet_embeddings "
                    "(snippet_id, model_id, dim, vector, created_at) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(snippet_id, model_id) DO UPDATE SET "
                    "dim = excluded.dim, vector = excluded.vector, "
                    "created_at = excluded.created_at",
                    [
                        (
                            row.snippet_id,
                            row.model_id,
                            len(row.vector),
                            _pack_vector(row.vector),
                            now,
                        )
                        for row in rows
                    ],
                )
        except sqlite3.Error as exc:
            raise StorageError("failed to save snippet embeddings") from exc

    def _snippet_vectors(self, model_id: str) -> list[SnippetEmbedding]:
        try:
            with closing(self._connect()) as conn, conn:
                rows = conn.execute(
                    "SELECT snippet_id, model_id, vector FROM snippet_embeddings "
                    "WHERE model_id = ?",
                    (model_id,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise StorageError("failed to list snippet embeddings") from exc
        return [
            SnippetEmbedding(
                snippet_id=snippet_id,
                model_id=row_model_id,
                vector=_unpack_vector(blob),
            )
            for snippet_id, row_model_id, blob in rows
        ]

    def _snippets_without_embeddings(self, model_id: str) -> list[Snippet]:
        try:
            with closing(self._connect()) as conn, conn:
                rows = conn.execute(
                    "SELECT s.id, s.document_id, s.ordinal, s.text, s.start, "
                    's."end", s.page FROM document_snippets s '
                    "LEFT JOIN snippet_embeddings e "
                    "ON e.snippet_id = s.id AND e.model_id = ? "
                    "WHERE e.snippet_id IS NULL ORDER BY s.document_id, s.ordinal",
                    (model_id,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise StorageError("failed to list unembedded snippets") from exc
        return [_snippet_from_row(row) for row in rows]



def _pack_vector(vector: Sequence[float]) -> bytes:
    # The explicit `<` is what keeps a database written on one machine
    # readable on another; `dtype="f4"` is native-endian and silently is not.
    return np.asarray(vector, dtype="<f4").tobytes()


def _unpack_vector(blob: bytes) -> tuple[float, ...]:
    return tuple(float(x) for x in np.frombuffer(blob, dtype="<f4"))


def _fact_from_row(row: tuple, phrases: list[Phrase]) -> Fact:
    (
        id_,
        conversation_id,
        group_id,
        text,
        window_start,
        window_end,
        model_id,
        created_at,
    ) = row
    return Fact(
        id=id_,
        conversation_id=conversation_id,
        group_id=group_id,
        text=text,
        phrases=tuple(phrases),
        window_start=window_start,
        window_end=window_end,
        model_id=model_id,
        created_at=datetime.fromisoformat(created_at),
    )


_DOCUMENT_COLUMNS = (
    "id, title, path, media_type, content_hash, text, char_count, "
    "snippet_count, created_at"
)
_SNIPPET_COLUMNS = 'id, document_id, ordinal, text, start, "end", page'


def _document_from_row(row: tuple, *, with_text: bool) -> Document:
    (
        id_,
        title,
        path,
        media_type,
        content_hash,
        text,
        char_count,
        snippet_count,
        created_at,
    ) = row
    return Document(
        id=id_,
        title=title,
        path=path,
        media_type=media_type,
        content_hash=content_hash,
        # A listed document is a header: carrying every document's full text
        # into a picker would load the whole corpus to draw a list.
        text=text if with_text else "",
        char_count=char_count,
        snippet_count=snippet_count,
        created_at=datetime.fromisoformat(created_at),
    )


def _snippet_from_row(row: tuple) -> Snippet:
    id_, document_id, ordinal, text, start, end, page = row
    return Snippet(
        id=id_,
        document_id=document_id,
        ordinal=ordinal,
        text=text,
        start=start,
        end=end,
        page=page,
    )
