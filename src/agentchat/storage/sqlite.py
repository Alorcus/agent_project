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

from agentchat.core.errors import StorageError
from agentchat.core.models import (
    DEFAULT_GROUP_ID,
    Author,
    Conversation,
    ConversationSummary,
    Fact,
    Group,
    Message,
    Phrase,
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

    async def save_summary(self, summary: ConversationSummary) -> None:
        await asyncio.to_thread(self._save_summary, summary)

    async def summary(self, conversation_id: str) -> ConversationSummary | None:
        return await asyncio.to_thread(self._summary, conversation_id)

    async def list_summaries(self, group_id: str) -> list[ConversationSummary]:
        return await asyncio.to_thread(self._list_summaries, group_id)

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

    def _save_summary(self, summary: ConversationSummary) -> None:
        try:
            with closing(self._connect()) as conn, conn:
                conn.execute(
                    "INSERT INTO conversation_summaries "
                    "(conversation_id, group_id, summary, keywords, "
                    "covered_messages, model_id, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(conversation_id) DO UPDATE SET "
                    "summary = excluded.summary, keywords = excluded.keywords, "
                    "covered_messages = excluded.covered_messages, "
                    "model_id = excluded.model_id, updated_at = excluded.updated_at",
                    (
                        summary.conversation_id,
                        summary.group_id,
                        summary.summary,
                        summary.keywords_text,
                        summary.covered_messages,
                        summary.model_id,
                        summary.created_at.isoformat(),
                        summary.updated_at.isoformat(),
                    ),
                )
        except sqlite3.Error as exc:
            raise StorageError(
                f"failed to save summary for conversation {summary.conversation_id}"
            ) from exc

    def _summary(self, conversation_id: str) -> ConversationSummary | None:
        try:
            with closing(self._connect()) as conn, conn:
                row = conn.execute(
                    "SELECT conversation_id, group_id, summary, keywords, "
                    "covered_messages, model_id, created_at, updated_at "
                    "FROM conversation_summaries WHERE conversation_id = ?",
                    (conversation_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise StorageError(
                f"failed to load summary for conversation {conversation_id}"
            ) from exc
        return None if row is None else _summary_from_row(row)

    def _list_summaries(self, group_id: str) -> list[ConversationSummary]:
        try:
            with closing(self._connect()) as conn, conn:
                rows = conn.execute(
                    "SELECT conversation_id, group_id, summary, keywords, "
                    "covered_messages, model_id, created_at, updated_at "
                    "FROM conversation_summaries WHERE group_id = ?",
                    (group_id,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise StorageError(f"failed to list summaries for group {group_id}") from exc
        summaries = [_summary_from_row(row) for row in rows]
        summaries.sort(key=lambda s: s.updated_at, reverse=True)
        return summaries

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
                        "author_kind, author_label) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        [
                            (
                                fact.id,
                                ordinal,
                                phrase.message_id,
                                phrase.start,
                                phrase.end,
                                phrase.author.kind,
                                phrase.author.label,
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
                    "author_kind, author_label FROM fact_phrases "
                    "WHERE fact_id IN (SELECT id FROM facts WHERE group_id = ?) "
                    "ORDER BY fact_id, ordinal",
                    (group_id,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise StorageError(f"failed to list facts for group {group_id}") from exc

        phrases_by_fact: dict[str, list[Phrase]] = {}
        for fact_id, _ordinal, message_id, start, end, author_kind, author_label in phrase_rows:
            phrases_by_fact.setdefault(fact_id, []).append(
                Phrase(
                    message_id=message_id,
                    start=start,
                    end=end,
                    author=Author(kind=author_kind, label=author_label),
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


def _summary_from_row(row: tuple) -> ConversationSummary:
    (
        conversation_id,
        group_id,
        summary_text,
        keywords,
        covered_messages,
        model_id,
        created_at,
        updated_at,
    ) = row
    return ConversationSummary(
        conversation_id=conversation_id,
        group_id=group_id,
        summary=summary_text,
        keywords=ConversationSummary.split_keywords(keywords),
        covered_messages=covered_messages,
        model_id=model_id,
        created_at=datetime.fromisoformat(created_at),
        updated_at=datetime.fromisoformat(updated_at),
    )


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
