"""Memory domain dataclasses: pure data, no persistence or UI knowledge.
Faithful to §§ 1.1/1.3 of ``docs/plans/memory-and-groups.md``, including the
nullable fields Mermaid cannot express."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from agentchat.core.models import _new_id, _now

from .types import FragmentKind, Tier


@dataclass
class Group:
    id: str = field(default_factory=_new_id)
    name: str = "New group"
    kind: Literal["default", "project"] = "project"
    last_consolidated_at: datetime | None = None
    created_at: datetime = field(default_factory=_now)

    def is_memory_scope(self) -> bool:
        """False for the default group — I-2's single decision point."""
        return self.kind != "default"


@dataclass
class MemoryFragment:
    group_id: str
    text: str
    id: int | None = None  # SQLite rowid, unset until written
    uuid: str = field(default_factory=_new_id)
    origin_conversation_id: str | None = None
    consolidated: bool = False
    kind: FragmentKind = "fact"
    confidence: float = 0.5
    importance: float = 5.0
    decay: float = 0.03
    embedding: bytes | None = None
    embedding_model: str | None = None
    reasoning: str | None = None
    created_at: datetime = field(default_factory=_now)
    revised_at: datetime | None = None
    #: Populated by reads that join `fragment_support` (§ 1.3's derived
    #: association); never written back — `apply()` ignores it outright.
    support: FragmentSupport | None = None

    @property
    def tier(self) -> Tier:
        return Tier.CONSOLIDATED if self.consolidated else Tier.EXTRACTED

    @property
    def is_dormant(self) -> bool:  # confidence == 0 (I-10, I-13)
        return self.confidence == 0


@dataclass
class FragmentCitation:
    #: `None` until `apply()` mints the fragment's id and fills this in — the
    #: state a citation built before its fragment's insert is always in.
    fragment_id: int | None
    id: int | None = None
    source_message_id: str | None = None
    source_fragment_id: int | None = None
    observed_at: datetime = field(default_factory=_now)
    quote: str | None = None

    def __post_init__(self) -> None:
        """Mirrors the schema CHECK (I-9) so factories cannot build a row
        SQLite would reject at stage 1."""
        has_message = self.source_message_id is not None
        has_fragment = self.source_fragment_id is not None
        if has_message == has_fragment:
            raise ValueError(
                "FragmentCitation must cite exactly one of "
                "source_message_id or source_fragment_id"
            )


@dataclass(frozen=True)
class FragmentSupport:
    """The derived view of § 1.1.1 — never written, only read."""

    citation_count: int
    conversation_count: int
    first_seen_at: datetime
    last_seen_at: datetime
