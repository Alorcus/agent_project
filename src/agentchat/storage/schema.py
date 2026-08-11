"""The database schema shared by `SqliteStore` and `SqliteMemoryStore` —
either may open a file first, so both call `ensure_schema` and neither owns
its own `CREATE TABLE` statements.
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
  last_consolidated_at TEXT, created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY, title TEXT NOT NULL,
  group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  extracted_at TEXT, extracted_id TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
  created_at TEXT NOT NULL, model_id TEXT, metadata TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_messages_conversation
  ON messages(conversation_id, ordinal);

CREATE TABLE IF NOT EXISTS memory_fragments (
  id INTEGER PRIMARY KEY, uuid TEXT NOT NULL UNIQUE,
  group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  origin_conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
  consolidated INTEGER NOT NULL DEFAULT 0,
  kind TEXT NOT NULL, text TEXT NOT NULL,
  confidence REAL NOT NULL, importance REAL NOT NULL, decay REAL NOT NULL,
  embedding BLOB, embedding_model TEXT, reasoning TEXT,
  created_at TEXT NOT NULL, revised_at TEXT);

CREATE TABLE IF NOT EXISTS fragment_citations (
  id INTEGER PRIMARY KEY,
  fragment_id INTEGER NOT NULL REFERENCES memory_fragments(id) ON DELETE CASCADE,
  source_message_id TEXT REFERENCES messages(id) ON DELETE CASCADE,
  source_fragment_id INTEGER REFERENCES memory_fragments(id) ON DELETE CASCADE,
  observed_at TEXT NOT NULL, quote TEXT,
  CHECK ((source_message_id IS NULL) <> (source_fragment_id IS NULL)));

CREATE UNIQUE INDEX IF NOT EXISTS idx_citation_message
  ON fragment_citations(fragment_id, source_message_id)
  WHERE source_message_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_citation_fragment
  ON fragment_citations(fragment_id, source_fragment_id)
  WHERE source_fragment_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_citation_source_fragment
  ON fragment_citations(source_fragment_id);

CREATE VIEW IF NOT EXISTS fragment_support AS
SELECT fc.fragment_id AS fragment_id,
       COUNT(*) AS citation_count,
       COUNT(DISTINCT m.conversation_id) AS conversation_count,
       MIN(fc.observed_at) AS first_seen_at,
       MAX(fc.observed_at) AS last_seen_at
  FROM fragment_citations fc
  LEFT JOIN messages m ON m.id = fc.source_message_id
 GROUP BY fc.fragment_id;

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts
  USING fts5(text, content='memory_fragments', content_rowid='id');

CREATE TRIGGER IF NOT EXISTS memory_fragments_ai AFTER INSERT ON memory_fragments BEGIN
  INSERT INTO memory_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS memory_fragments_ad AFTER DELETE ON memory_fragments BEGIN
  INSERT INTO memory_fts(memory_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS memory_fragments_au AFTER UPDATE ON memory_fragments BEGIN
  INSERT INTO memory_fts(memory_fts, rowid, text) VALUES ('delete', old.id, old.text);
  INSERT INTO memory_fts(rowid, text) VALUES (new.id, new.text);
END;
"""


def connect(path: Path) -> sqlite3.Connection:
    """Open `path` with foreign keys on and WAL set. Does not create tables."""
    conn = sqlite3.connect(path)
    # Without this, every CASCADE and every FK in the schema is decorative.
    conn.execute("PRAGMA foreign_keys = ON")
    # apply()'s single-writer transaction argument (§ 2.1) is about WAL.
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def ensure_schema(path: Path) -> None:
    """Create the schema if absent and seed the default group. Raises
    `StorageError` if `path` holds a pre-memory database."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with closing(connect(path)) as conn, conn:
            _refuse_pre_memory_database(conn, path)
            conn.executescript(SCHEMA)
            _seed_default_group(conn)
    except sqlite3.Error as exc:
        raise StorageError(f"could not open database at {path}") from exc


def _refuse_pre_memory_database(conn: sqlite3.Connection, path: Path) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(conversations)")}
    if columns and "extracted_at" not in columns:
        raise StorageError(
            f"{path} predates the memory schema and this prototype has no "
            f"migrations — delete the file and restart to recreate it"
        )


def _seed_default_group(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO groups (id, name, kind, last_consolidated_at, created_at)"
        " VALUES (?, ?, 'default', NULL, ?) ON CONFLICT(id) DO NOTHING",
        (DEFAULT_GROUP_ID, DEFAULT_GROUP_NAME, _now().isoformat()),
    )
