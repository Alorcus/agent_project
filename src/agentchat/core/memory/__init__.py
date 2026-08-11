"""Group-scoped chat memory: extraction, ranking, consolidation, and the
domain model they share. See ``docs/plans/memory-and-groups.md``."""

from __future__ import annotations

from .extract import (
    EXTRACTION_PROMPT,
    RESOLUTION_PROMPT,
    REWRITE_PROMPT,
    Candidate,
    Claim,
    Decision,
    MemoryExtractor,
    Outcome,
    parse_claims,
    parse_decisions,
    parse_revision,
)
from .models import FragmentCitation, FragmentSupport, Group, MemoryFragment
from .store import ApplyResult, ConfidenceChange, FragmentWrite, MemoryStore, Watermark
from .strategy import ChatMemory, ConversationEvidence, MessageEvidence
from .tuning import CATALOGUE, Tuning, log_effective_tuning
from .types import KNOWN_KINDS, EvidenceItem, EvidenceSource, FragmentKind, PromptTurn, Tier

__all__ = [
    "ApplyResult",
    "CATALOGUE",
    "Candidate",
    "ChatMemory",
    "Claim",
    "ConfidenceChange",
    "ConversationEvidence",
    "Decision",
    "EXTRACTION_PROMPT",
    "EvidenceItem",
    "EvidenceSource",
    "FragmentCitation",
    "FragmentKind",
    "FragmentSupport",
    "FragmentWrite",
    "Group",
    "KNOWN_KINDS",
    "MemoryExtractor",
    "MemoryFragment",
    "MemoryStore",
    "MessageEvidence",
    "Outcome",
    "PromptTurn",
    "RESOLUTION_PROMPT",
    "REWRITE_PROMPT",
    "Tier",
    "Tuning",
    "Watermark",
    "log_effective_tuning",
    "parse_claims",
    "parse_decisions",
    "parse_revision",
]
