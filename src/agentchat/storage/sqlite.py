"""SQLite-backed `ConversationStore`: conversations survive process restarts.

Each method offloads its synchronous sqlite3 body to a worker thread via
`asyncio.to_thread`, so storage I/O never blocks the event loop. The schema
itself lives in `storage.schema`.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from agentchat.core.errors import StorageError
from agentchat.core.models import DEFAULT_GROUP_ID, Conversation, Group, Message
from agentchat.storage import schema
from agentchat.storage.base import by_default_first, by_recency


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

    async def default_group(self) -> Group:
        groups = await self.list_groups()
        return groups[0]

    async def load(self, conversation_id: str) -> Conversation | None:
        return await asyncio.to_thread(self._load, conversation_id)

    async def save(self, conversation: Conversation) -> None:
        await asyncio.to_thread(self._save, conversation)

    async def delete(self, conversation_id: str) -> None:
        await asyncio.to_thread(self._delete, conversation_id)

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
