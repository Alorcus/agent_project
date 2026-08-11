"""Group-scoped chat memory: extraction, ranking, consolidation, and the
domain model they share. See ``docs/plans/memory-and-groups.md``."""

from __future__ import annotations

from .models import FragmentCitation, FragmentSupport, Group, MemoryFragment
from .store import ApplyResult, ConfidenceChange, FragmentWrite, MemoryStore, Watermark
from .tuning import CATALOGUE, Tuning, log_effective_tuning
from .types import KNOWN_KINDS, EvidenceItem, FragmentKind, Tier

__all__ = [
    "ApplyResult",
    "CATALOGUE",
    "ConfidenceChange",
    "EvidenceItem",
    "FragmentCitation",
    "FragmentKind",
    "FragmentSupport",
    "FragmentWrite",
    "Group",
    "KNOWN_KINDS",
    "MemoryFragment",
    "MemoryStore",
    "Tier",
    "Tuning",
    "Watermark",
    "log_effective_tuning",
]
