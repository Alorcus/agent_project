"""The `MemoryStore` protocol and `apply()`'s payload types.

Lives in `core/`, not `storage/`, so stage 2's `extract.py` can import it
without importing `agentchat.storage` and tripping I-6's lint.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from .models import FragmentCitation, MemoryFragment
from .types import Tier


@dataclass(frozen=True)
class FragmentWrite:
    """One fragment's worth of write. ``citations`` are attached to
    ``fragment`` — their ``fragment_id`` is filled in from the row id, so a
    caller can build them before the fragment has one."""

    fragment: MemoryFragment
    citations: Sequence[FragmentCitation] = ()
    #: Reinforcement (§ 2.1): the row's text and confidence are left alone,
    #: and confidence rises by `Tuning.reinforce_step` per citation that was
    #: genuinely new. False means the fragment's own values are
    #: authoritative — new and revise.
    reinforce: bool = False
    #: Refresh `embedding`/`embedding_model` on an existing row and nothing
    #: else — not a revision, so no `revised_at` and no text change. Mutually
    #: exclusive with `reinforce`.
    embedding_only: bool = False


@dataclass(frozen=True)
class Watermark:
    conversation_id: str
    extracted_at: datetime
    extracted_id: str


@dataclass(frozen=True)
class ConfidenceChange:
    fragment_id: int
    before: float
    after: float


@dataclass(frozen=True)
class ApplyResult:
    inserted: list[int] = field(default_factory=list)
    revised: list[int] = field(default_factory=list)
    citations_added: int = 0
    confidence_changes: list[ConfidenceChange] = field(default_factory=list)
    #: `embedding_only` writes — never in `inserted` or `revised`.
    reembedded: list[int] = field(default_factory=list)


class MemoryStore(Protocol):
    def memory_scope(self, group_id: str) -> str | None: ...

    def candidates(self, group_id: str, query: str, k: int) -> list[MemoryFragment]: ...

    def stable_core(self, group_id: str, budget: int) -> list[MemoryFragment]: ...

    def select(
        self,
        group_id: str,
        query: str,
        budget: int,
        *,
        tiers: Tier = Tier.EXTRACTED,
    ) -> list[MemoryFragment]: ...

    def apply(
        self, writes: Sequence[FragmentWrite], *, watermark: Watermark | None = None
    ) -> ApplyResult: ...

    def purge_conversation(self, conversation_id: str) -> None: ...

    def purge_group(self, group_id: str) -> None: ...
