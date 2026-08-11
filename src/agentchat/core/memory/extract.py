"""Extraction pipeline: turns evidence into fragments. Structured as GUM's
named stages (observe -> audit -> propose -> retrieve -> resolve -> apply) so
a future persona pipeline can slot a contextual-integrity gate into ``audit``
without restructuring anything else.

Typed against ``EvidenceItem`` and an opaque ``scope_id`` only — never
``Message``, ``Conversation``, or anything under ``agentchat.storage`` — so
this module stays usable by a non-chat evidence source. Enforced by I-6's
lint from stage 0 on.

``Claim``, ``Candidate`` and ``Decision`` are stage 2's contract. Naming them
now, ahead of the extraction logic that would give them shape, is exactly the
drift "never plan ahead of the gate" forbids — so the payloads below are
``Any`` on purpose, not by oversight.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .types import EvidenceItem


def observe(items: Sequence[EvidenceItem]) -> list[EvidenceItem]:
    raise NotImplementedError("stage 2")


def audit(items: Sequence[EvidenceItem]) -> list[EvidenceItem]:
    # Identity by design. GUM gates observations on contextual integrity here,
    # chat memory has no such gate, and the persona pipeline will need one
    # without restructuring the pipeline around it.
    return list(items)


def propose(items: Sequence[EvidenceItem], *, scope_id: str) -> list[Any]:
    raise NotImplementedError("stage 2")


def retrieve(claims: Sequence[Any], *, scope_id: str) -> list[Any]:
    raise NotImplementedError("stage 2")


def resolve(claims: Sequence[Any], candidates: Sequence[Any]) -> list[Any]:
    raise NotImplementedError("stage 2")


def apply(decisions: Sequence[Any]) -> None:
    raise NotImplementedError("stage 2")
