"""Narrow protocols and vocabulary shared by the memory pipeline. Written
against these, not chat types, so the same modules can later bind to a
persona pipeline (I-6)."""

from __future__ import annotations

from datetime import datetime
from enum import Flag, auto
from typing import Protocol, runtime_checkable


@runtime_checkable
class EvidenceItem(Protocol):
    """What extraction consumes. The chat pipeline binds it to a message, the
    future persona pipeline to an email."""

    id: str
    text: str
    created_at: datetime


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
