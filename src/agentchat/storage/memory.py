"""`SqliteMemoryStore`: the synchronous `MemoryStore` over the same file
`SqliteStore` writes conversations to.

Synchronous by the skeleton's ratified contract: recall sits on the reply
path, `ContextStrategy.build` is sync, and SQLite reads are fast enough not
to earn an async surface.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from datetime import datetime
from pathlib import Path

from agentchat.core.errors import StorageError
from agentchat.core.memory.models import FragmentCitation, FragmentSupport, Group, MemoryFragment
from agentchat.core.memory.store import ApplyResult, ConfidenceChange, FragmentWrite, Watermark
from agentchat.core.memory.tuning import Tuning
from agentchat.core.memory.types import Tier
from agentchat.core.models import _now
from agentchat.storage import schema

#: Words only — a user's own phrasing routed straight into FTS5 MATCH would
#: let a stray quote or the bare token `NEAR` raise a syntax error on the
#: reply path, so every token is quoted and OR-joined instead of passed raw.
_WORD_RE = re.compile(r"\w+", re.UNICODE)

#: `f.`-qualified so a caller can join `memory_fragments` under other aliases
#: without rewriting this list — the order `_row_to_fragment` unpacks by.
_FRAGMENT_SELECT = (
    "f.id, f.uuid, f.group_id, f.origin_conversation_id, f.consolidated, f.kind,"
    " f.text, f.confidence, f.importance, f.decay, f.embedding, f.embedding_model,"
    " f.reasoning, f.created_at, f.revised_at"
)
_FRAGMENT_COLUMN_COUNT = 15


def _row_to_fragment(row: Sequence, support: FragmentSupport | None = None) -> MemoryFragment:
    """The row -> `MemoryFragment` mapper `candidates()` and (stage 3's)
    `select()`/`stable_core()` all need identically — one place that knows
    the column order above."""
    (
        id_, uuid_, group_id, origin_conversation_id, consolidated, kind, text,
        confidence, importance, decay, embedding, embedding_model, reasoning,
        created_at, revised_at,
    ) = row
    return MemoryFragment(
        id=id_,
        uuid=uuid_,
        group_id=group_id,
        origin_conversation_id=origin_conversation_id,
        consolidated=bool(consolidated),
        kind=kind,
        text=text,
        confidence=confidence,
        importance=importance,
        decay=decay,
        embedding=embedding,
        embedding_model=embedding_model,
        reasoning=reasoning,
        created_at=datetime.fromisoformat(created_at),
        revised_at=datetime.fromisoformat(revised_at) if revised_at else None,
        support=support,
    )


def _support_from(row: Sequence) -> FragmentSupport | None:
    """`fragment_support` is a `LEFT JOIN`: a fragment somehow cited by
    nothing (I-4 should already forbid it) reads as `None` rather than a
    `FragmentSupport` of zeroes with no timestamps to put in it."""
    citation_count, conversation_count, first_seen_at, last_seen_at = row
    if citation_count is None:
        return None
    return FragmentSupport(
        citation_count=citation_count,
        conversation_count=conversation_count,
        first_seen_at=datetime.fromisoformat(first_seen_at),
        last_seen_at=datetime.fromisoformat(last_seen_at),
    )


def _fts_match(query: str) -> str | None:
    """`None` when `query` has no usable token, so a caller can skip the
    database entirely rather than run an empty `MATCH`."""
    tokens = dict.fromkeys(_WORD_RE.findall(query))  # dedup, order-preserving
    if not tokens:
        return None
    return " OR ".join(f'"{token}"' for token in tokens)

# The reachability check behind I-8: does `source` already (transitively)
# cite `fragment`? A hit means the proposed edge would close a cycle. Run
# inside the write transaction so it sees the batch's own earlier edges too.
_REACHABLE = """
WITH RECURSIVE reach(id) AS (
  SELECT :source
  UNION
  SELECT fc.source_fragment_id FROM fragment_citations fc
    JOIN reach r ON fc.fragment_id = r.id
   WHERE fc.source_fragment_id IS NOT NULL)
SELECT 1 FROM reach WHERE id = :fragment
"""


class SqliteMemoryStore:
    """Synchronous `MemoryStore` over the same file as `SqliteStore`."""

    def __init__(self, path: Path, tuning: Tuning | None = None) -> None:
        self.path = path  # public: tests and factories write through it
        self._tuning = tuning or Tuning.from_env()
        schema.ensure_schema(path)

    def memory_scope(self, group_id: str) -> str | None:
        with closing(schema.connect(self.path)) as conn:
            row = conn.execute("SELECT kind FROM groups WHERE id = ?", (group_id,)).fetchone()
        if row is None:
            return None
        # The kind comparison stays in Group.is_memory_scope() (I-2's single
        # decision point) rather than being repeated here.
        return group_id if Group(kind=row[0]).is_memory_scope() else None

    def candidates(self, group_id: str, query: str, k: int) -> list[MemoryFragment]:
        match = _fts_match(query)
        if match is None:
            return []
        sql = f"""
            SELECT {_FRAGMENT_SELECT},
                   s.citation_count, s.conversation_count, s.first_seen_at, s.last_seen_at
              FROM memory_fragments f
              JOIN memory_fts ON memory_fts.rowid = f.id
              LEFT JOIN fragment_support s ON s.fragment_id = f.id
             WHERE f.group_id = ? AND f.consolidated = 0 AND memory_fts MATCH ?
             ORDER BY bm25(memory_fts)
             LIMIT ?
        """
        with closing(schema.connect(self.path)) as conn:
            rows = conn.execute(sql, (group_id, match, k)).fetchall()
        return [
            _row_to_fragment(row[:_FRAGMENT_COLUMN_COUNT], _support_from(row[_FRAGMENT_COLUMN_COUNT:]))
            for row in rows
        ]

    def stable_core(self, group_id: str, budget: int) -> list[MemoryFragment]:
        raise NotImplementedError("stage 3")

    def select(
        self,
        group_id: str,
        query: str,
        budget: int,
        *,
        tiers: Tier = Tier.EXTRACTED,
    ) -> list[MemoryFragment]:
        raise NotImplementedError("stage 3")

    def purge_conversation(self, conversation_id: str) -> None:
        raise NotImplementedError("stage 4")

    def purge_group(self, group_id: str) -> None:
        raise NotImplementedError("stage 4")

    def apply(
        self, writes: Sequence[FragmentWrite], *, watermark: Watermark | None = None
    ) -> ApplyResult:
        try:
            with closing(schema.connect(self.path)) as conn:
                with conn:
                    result = self._apply(conn, writes)
                    if watermark is not None:
                        conn.execute(
                            "UPDATE conversations SET extracted_at = ?, extracted_id = ?"
                            " WHERE id = ?",
                            (
                                watermark.extracted_at.isoformat(),
                                watermark.extracted_id,
                                watermark.conversation_id,
                            ),
                        )
        except sqlite3.Error as exc:
            raise StorageError(str(exc)) from exc
        return result

    def _apply(self, conn: sqlite3.Connection, writes: Sequence[FragmentWrite]) -> ApplyResult:
        now = _now()

        # Phase 1: every fragment row in the batch exists before the first
        # citation is inserted — a citation may name a fragment that appears
        # later in the same batch.
        ids: list[int] = []
        newly_inserted: list[bool] = []
        for write in writes:
            fragment_id, is_new = self._upsert_fragment(conn, write, now)
            write.fragment.id = fragment_id
            ids.append(fragment_id)
            newly_inserted.append(is_new)

        inserted = [
            ids[i] for i, w in enumerate(writes) if not w.reinforce and newly_inserted[i]
        ]
        revised = [
            ids[i] for i, w in enumerate(writes) if not w.reinforce and not newly_inserted[i]
        ]

        # Phases 2-3: citations, one at a time, then the reinforcement raise
        # for whatever in this write's batch turned out to be genuinely new.
        citations_added = 0
        confidence_changes: list[ConfidenceChange] = []
        for i, write in enumerate(writes):
            fragment_id = ids[i]
            new_count = 0
            for citation in write.citations:
                if self._insert_citation(conn, fragment_id, citation, now):
                    citations_added += 1
                    new_count += 1
            if write.reinforce and new_count:
                (before,) = conn.execute(
                    "SELECT confidence FROM memory_fragments WHERE id = ?", (fragment_id,)
                ).fetchone()
                after = min(1.0, before + new_count * self._tuning.reinforce_step)
                conn.execute(
                    "UPDATE memory_fragments SET confidence = ? WHERE id = ?",
                    (after, fragment_id),
                )
                confidence_changes.append(ConfidenceChange(fragment_id, before, after))

        return ApplyResult(
            inserted=inserted,
            revised=revised,
            citations_added=citations_added,
            confidence_changes=confidence_changes,
        )

    def _upsert_fragment(
        self, conn: sqlite3.Connection, write: FragmentWrite, now: datetime
    ) -> tuple[int, bool]:
        """Insert or revise `write.fragment`'s row. Returns `(id, is_new)`."""
        fragment = write.fragment
        existing = None
        if fragment.id is not None:
            row = conn.execute(
                "SELECT id FROM memory_fragments WHERE id = ?", (fragment.id,)
            ).fetchone()
            existing = row[0] if row is not None else None

        if write.reinforce:
            if existing is None:
                raise StorageError(
                    "reinforce=True requires an existing fragment row "
                    f"(fragment.id={fragment.id!r})"
                )
            return existing, False

        if existing is not None:
            # The revise path is authoritative: the caller's text and
            # confidence win outright, and revised_at is stamped by the
            # store so no caller can forget it.
            conn.execute(
                "UPDATE memory_fragments SET kind = ?, text = ?, confidence = ?,"
                " importance = ?, decay = ?, embedding = ?, embedding_model = ?,"
                " reasoning = ?, revised_at = ? WHERE id = ?",
                (
                    fragment.kind,
                    fragment.text,
                    fragment.confidence,
                    fragment.importance,
                    fragment.decay,
                    fragment.embedding,
                    fragment.embedding_model,
                    fragment.reasoning,
                    now.isoformat(),
                    existing,
                ),
            )
            return existing, False

        cursor = conn.execute(
            "INSERT INTO memory_fragments (id, uuid, group_id, origin_conversation_id,"
            " consolidated, kind, text, confidence, importance, decay, embedding,"
            " embedding_model, reasoning, created_at, revised_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (
                fragment.id,
                fragment.uuid,
                fragment.group_id,
                fragment.origin_conversation_id,
                int(fragment.consolidated),
                fragment.kind,
                fragment.text,
                fragment.confidence,
                fragment.importance,
                fragment.decay,
                fragment.embedding,
                fragment.embedding_model,
                fragment.reasoning,
                fragment.created_at.isoformat(),
            ),
        )
        new_id = fragment.id if fragment.id is not None else cursor.lastrowid
        return new_id, True

    def _insert_citation(
        self,
        conn: sqlite3.Connection,
        fragment_id: int,
        citation: FragmentCitation,
        now: datetime,
    ) -> bool:
        """Insert `citation` against `fragment_id`. Returns whether the row
        was genuinely new (`False` for a repeat — I-7)."""
        if citation.source_fragment_id is not None:
            self._reject_cycle(conn, fragment_id, citation.source_fragment_id)

        observed_at = (citation.observed_at or now).isoformat()
        row = conn.execute(
            "INSERT INTO fragment_citations"
            " (fragment_id, source_message_id, source_fragment_id, observed_at, quote)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT DO NOTHING RETURNING id",
            (
                fragment_id,
                citation.source_message_id,
                citation.source_fragment_id,
                observed_at,
                citation.quote,
            ),
        ).fetchone()
        citation.fragment_id = fragment_id
        if row is not None:
            citation.id = row[0]
        return row is not None

    def _reject_cycle(self, conn: sqlite3.Connection, fragment_id: int, source_id: int) -> None:
        # Self-citation is the degenerate case: reach seeds at `source_id`,
        # so `fragment_id == source_id` matches on the seed row itself — no
        # separate check needed. Per § 1.1.1, id order is not a fast path.
        hit = conn.execute(_REACHABLE, {"source": source_id, "fragment": fragment_id}).fetchone()
        if hit is not None:
            raise StorageError(
                f"citing fragment {source_id} from {fragment_id} would close a cycle"
            )
