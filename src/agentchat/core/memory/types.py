"""Narrow protocols and vocabulary shared by the memory pipeline. Written
against these, not chat types, so the same modules can later bind to a
persona pipeline (I-6)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Flag, auto
from typing import Protocol, runtime_checkable


@runtime_checkable
class EmbeddingProvider(Protocol):
    """What `rank.py` and the store need from an encoder — never the model
    itself, so no torch object crosses this seam."""

    model_id: str

    def encode(self, texts: Sequence[str]) -> list[tuple[float, ...]]: ...


@runtime_checkable
class EvidenceItem(Protocol):
    """What extraction consumes. The chat pipeline binds it to a message, the
    future persona pipeline to an email."""

    id: str
    text: str
    created_at: datetime


@runtime_checkable
class EvidenceSource(Protocol):
    """One batch of evidence for `MemoryExtractor.run`: the adapter has
    already sliced it to the unread range and knows how far it reaches.
    `ConversationEvidence` binds this to a chat; an email thread binds it
    later."""

    id: str
    scope_id: str
    items: Sequence[EvidenceItem]
    #: `(created_at, id)` of the last item in `items`, watermark-comparable
    #: (§ 2.1's lexicographic range) — `None` when there is nothing new.
    read_through: tuple[datetime, str] | None


@dataclass(frozen=True)
class PromptTurn:
    """What a provider's `generate` actually reads off a `Message`: role and
    content, nothing else. Lets `extract.py` (and later `consolidate.py`)
    build prompts without importing `Message` (I-6)."""

    role: str
    content: str


class Tier(Flag):
    EXTRACTED = auto()  # memory_fragments.consolidated = 0
    CONSOLIDATED = auto()  # = 1
    ALL = EXTRACTED | CONSOLIDATED


#: Open by rule 4 — deliberately not an Enum, so persona kinds plug in
#: without a schema change.
FragmentKind = str

KNOWN_KINDS: tuple[str, ...] = (
    "fact",
    "decision",
    "constraint",
    "open_question",
    "artefact",
)
