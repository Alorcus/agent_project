"""SQLite-backed `ConversationStore`: conversations survive process restarts.

Each method offloads its synchronous sqlite3 body to a worker thread via
`asyncio.to_thread`, so storage I/O never blocks the event loop.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from agentchat.core.errors import StorageError
from agentchat.core.models import Conversation, Message

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, group_id TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
  created_at TEXT NOT NULL, model_id TEXT, metadata TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_messages_conversation
  ON messages(conversation_id, ordinal);
"""


class SqliteStore:
    """Durable `ConversationStore`. See `storage.base.ConversationStore`."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with closing(self._connect()) as conn, conn:
                conn.executescript(_SCHEMA)
        except sqlite3.Error as exc:
            raise StorageError(f"could not open database at {path}") from exc

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        # Per-connection setting: without it ON DELETE CASCADE is silently
        # ignored and delete() would orphan message rows.
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    async def list_conversations(
        self, group_id: str | None = None
    ) -> list[Conversation]:
        return await asyncio.to_thread(self._list_conversations, group_id)

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
        return sorted(items, key=lambda c: c.updated_at, reverse=True)

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
