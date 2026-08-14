"""The database schema, its connection settings, and the default-group seed.

Kept out of `SqliteStore` because opening a file is a lifecycle concern of its
own: the seed and the refusal below both run before any store method does.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from agentchat.core.errors import StorageError
from agentchat.core.models import DEFAULT_GROUP_ID, _now

DEFAULT_GROUP_NAME = "Chats"

SCHEMA = """
CREATE TABLE IF NOT EXISTS groups (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
  created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY, title TEXT NOT NULL,
  group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
  created_at TEXT NOT NULL, model_id TEXT, metadata TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_messages_conversation
  ON messages(conversation_id, ordinal);

CREATE TABLE IF NOT EXISTS conversation_summaries (
  conversation_id TEXT PRIMARY KEY
    REFERENCES conversations(id) ON DELETE CASCADE,
  group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  summary TEXT NOT NULL, keywords TEXT NOT NULL,
  covered_messages INTEGER NOT NULL, model_id TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_summaries_group
  ON conversation_summaries(group_id);

CREATE TABLE IF NOT EXISTS facts (
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  text TEXT NOT NULL,
  window_start INTEGER NOT NULL, window_end INTEGER NOT NULL,
  model_id TEXT, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_facts_group ON facts(group_id);
CREATE INDEX IF NOT EXISTS idx_facts_conversation ON facts(conversation_id);

-- No FK to `messages`: `SqliteStore._save` deletes and re-inserts every
-- message row on every turn, so an FK here would cascade every fact in the
-- conversation away on the next turn. The cascade that matters is carried by
-- `facts.conversation_id` above.
CREATE TABLE IF NOT EXISTS fact_phrases (
  fact_id TEXT NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL,
  message_id TEXT NOT NULL,
  start INTEGER NOT NULL, "end" INTEGER NOT NULL,
  author_kind TEXT NOT NULL, author_label TEXT NOT NULL,
  PRIMARY KEY (fact_id, ordinal));

-- `covered_messages` here counts *countable* messages (core.facts.countable),
-- not rows in `messages` — not comparable with
-- `conversation_summaries.covered_messages` despite the shared name.
CREATE TABLE IF NOT EXISTS fact_extraction_state (
  conversation_id TEXT PRIMARY KEY
    REFERENCES conversations(id) ON DELETE CASCADE,
  covered_messages INTEGER NOT NULL, updated_at TEXT NOT NULL);
"""


def connect(path: Path) -> sqlite3.Connection:
    """Open `path` with foreign keys on. Does not create tables."""
    conn = sqlite3.connect(path)
    # Per-connection setting: without it every CASCADE and every FK in the
    # schema above is decorative.
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def ensure_schema(path: Path) -> None:
    """Create the schema if absent and seed the default group. Raises
    `StorageError` if `path` holds a pre-groups database."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with closing(connect(path)) as conn, conn:
            _refuse_pre_groups_database(conn, path)
            conn.executescript(SCHEMA)
            _seed_default_group(conn)
    except sqlite3.Error as exc:
        raise StorageError(f"could not open database at {path}") from exc


def _refuse_pre_groups_database(conn: sqlite3.Connection, path: Path) -> None:
    # CREATE TABLE IF NOT EXISTS would leave such a file's nullable, FK-less
    # `conversations.group_id` in place and report success. Refusing is what
    # keeps first launch from silently running against a schema I-1 forbids —
    # and from rewriting real conversations to get one.
    tables = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if "conversations" in tables and "groups" not in tables:
        raise StorageError(
            f"{path} predates the groups schema and this prototype has no "
            f"migrations — delete the file and restart to recreate it"
        )


def _seed_default_group(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO groups (id, name, kind, created_at)"
        " VALUES (?, ?, 'default', ?) ON CONFLICT(id) DO NOTHING",
        (DEFAULT_GROUP_ID, DEFAULT_GROUP_NAME, _now().isoformat()),
    )
