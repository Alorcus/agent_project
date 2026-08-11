"""Extraction pipeline: turns evidence into fragments. Structured as GUM's
named stages (observe -> audit -> propose -> retrieve -> resolve -> apply) so
a future persona pipeline can slot a contextual-integrity gate into ``audit``
without restructuring anything else.

Typed against ``EvidenceItem``/``EvidenceSource`` and an opaque ``scope_id``
only — never ``Message``, ``Conversation``, or anything under
``agentchat.storage`` — so this module stays usable by a non-chat evidence
source. Enforced by I-6's lint from stage 0 on.

The wire format (what the model is asked for, and how the reply is parsed)
is documented in full in
``docs/plans/2026-08-11-005-memory-stage-2-extraction-write-path.md`` § 2;
the three ``parse_*`` functions below are its pure, falsifiable half.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any

from agentchat.llm.base import GenerationOptions

from .models import FragmentCitation, FragmentSupport, MemoryFragment
from .store import ApplyResult, FragmentWrite, MemoryStore, Watermark
from .tuning import Tuning
from .types import EvidenceItem, EvidenceSource, PromptTurn

_LOG = logging.getLogger(__name__)

EXTRACTION_PROMPT = """\
You read a chat's recent turns and decide what is worth remembering beyond \
this conversation: durable decisions, constraints, facts, open questions, or \
artefacts that would still matter in a later conversation.

The turns are numbered below. Reply with a JSON array, one object per \
durable claim:

[{"text": "...", "kind": "fact", "importance": 5, "confidence": 0.5, "evidence": [1, 2]}]

Example. Turns:
1. user: Let's use SQLite for storage, in WAL mode.
2. assistant: Got it, SQLite in WAL mode.
Reply:
[{"text": "The project stores data in SQLite, in WAL mode.", "kind": "decision", "importance": 7, "confidence": 0.9, "evidence": [1, 2]}]

Rules:
- "text" must be self-contained: understandable on its own, with no pronouns \
or references that only make sense inside this conversation.
- "kind" is free text — fact, decision, constraint, open_question, artefact, \
or anything else that fits.
- "importance" is 1-10: how much this would matter to recall later.
- "confidence" is 0.0-1.0: how sure you are this is correct and durable.
- "evidence" lists the 1-based turn numbers that support the claim. A claim \
with no evidence is dropped, so always cite at least one turn.
- Most turns carry nothing durable. Only write a claim for something that \
would genuinely still matter later. When in doubt, prefer writing the claim \
over leaving it out — a duplicate is cheap to fix later, a claim never \
written is gone.

Reply with the JSON array and nothing else, now for the turns below.
"""

RESOLUTION_PROMPT = """\
You compare newly proposed claims against existing memory fragments \
("candidates") and decide how each claim relates to them.

Claims and candidates are both numbered below. Reply with a JSON array, one \
object per claim:

[{"claim": 1, "relation": "unrelated", "candidate": 2}]

"relation" is one of:
- "unrelated": none of the candidates say this — write a new fragment.
- "identical": a candidate already says exactly this — reinforce it, do not \
rewrite its text. "candidate" is required.
- "similar": a candidate says something close but not identical — its text \
should be rewritten to fold the two together. "candidate" is required.
- "ignore": not worth remembering — nothing is written for this claim.

Give every claim exactly one object. If you are unsure, use "unrelated" —
a duplicate is the cheapest mistake.

Reply with the JSON array and nothing else.
"""

REWRITE_PROMPT = """\
Rewrite the existing fragment's text so it also covers the new claim, \
keeping it self-contained and no longer than it needs to be. Reply with one \
JSON object:

{"text": "...", "confidence": 0.6}

"confidence" is 0.0-1.0: your confidence in the combined statement.

Reply with the JSON object and nothing else.
"""


# -- the wire format's types ------------------------------------------------


@dataclass(frozen=True)
class Claim:
    """One proposed durable claim, already resolved to the evidence items it
    cites — `propose`'s output."""

    text: str
    kind: str = "fact"
    importance: float = 5.0
    confidence: float = 0.5
    evidence: tuple[EvidenceItem, ...] = ()
    reasoning: str | None = None


@dataclass(frozen=True)
class Candidate:
    """A `candidates()` result paired with the support counts § 2.1 sends to
    the resolver instead of citation rows."""

    fragment: MemoryFragment
    support: FragmentSupport | None = None


class Outcome(Enum):
    NEW = "new"
    REINFORCE = "reinforce"
    REVISE = "revise"
    IGNORE = "ignore"


@dataclass(frozen=True)
class Decision:
    """One claim's resolution. `revised_text`/`revised_confidence` are set
    only for a `REVISE` that survived the rewrite call."""

    claim: Claim
    outcome: Outcome
    candidate: Candidate | None = None
    revised_text: str | None = None
    revised_confidence: float | None = None


_RELATION_TO_OUTCOME = {
    "unrelated": Outcome.NEW,
    "identical": Outcome.REINFORCE,
    "similar": Outcome.REVISE,
    "ignore": Outcome.IGNORE,
}

_THINK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"```(?:json)?", re.IGNORECASE)
_WORD_RE = re.compile(r"\w+")


# -- parsing: never raises on model output -----------------------------------


def parse_claims(
    reply: str, items: Sequence[EvidenceItem], *, thinking: bool = False
) -> list[Claim]:
    body, trace = _split_thinking(reply)
    raw = _load_json_array(body)
    by_index = {i: item for i, item in enumerate(items, start=1)}

    claims: list[Claim] = []
    for obj in raw:
        if not isinstance(obj, dict):
            continue
        text = obj.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        evidence = _resolve_indices(obj.get("evidence"), by_index)
        if not evidence:  # I-4 at the write side: no evidence, no claim.
            continue
        claims.append(
            Claim(
                text=text.strip(),
                kind=_as_str(obj.get("kind"), default="fact"),
                importance=_clamp(obj.get("importance"), 1.0, 10.0, default=5.0),
                confidence=_clamp(obj.get("confidence"), 0.0, 1.0, default=0.5),
                evidence=tuple(evidence),
                reasoning=_reasoning(obj.get("why"), trace, thinking=thinking),
            )
        )
    return claims


def parse_decisions(
    reply: str, claims: Sequence[Claim], candidates: Sequence[Candidate]
) -> list[Decision]:
    body, _ = _split_thinking(reply)
    raw = _load_json_array(body)

    by_claim: dict[int, dict] = {}
    for obj in raw:
        if not isinstance(obj, dict):
            continue
        index = obj.get("claim")
        if not isinstance(index, int) or isinstance(index, bool):
            continue
        by_claim.setdefault(index, obj)  # first resolution wins (§ 2.1)

    decisions: list[Decision] = []
    for position, claim in enumerate(claims, start=1):
        obj = by_claim.get(position)
        outcome = _RELATION_TO_OUTCOME.get(obj.get("relation") if obj else None, Outcome.NEW)
        candidate = None
        if outcome in (Outcome.REINFORCE, Outcome.REVISE):
            candidate = _resolve_index(obj.get("candidate"), candidates)
            if candidate is None:  # nothing to be identical/similar to: unclear
                outcome = Outcome.NEW
        decisions.append(Decision(claim=claim, outcome=outcome, candidate=candidate))
    return decisions


def parse_revision(reply: str) -> tuple[str, float] | None:
    body, _ = _split_thinking(reply)
    obj = _load_json_object(body)
    if obj is None:
        return None
    text = obj.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    confidence = _clamp(obj.get("confidence"), 0.0, 1.0, default=0.5)
    return text.strip(), confidence


def _split_thinking(reply: str) -> tuple[str, str | None]:
    """Strip a `<think>...</think>` block, returning the rest of the reply
    plus the block's content as the fallback reasoning trace."""
    match = _THINK_RE.search(reply)
    if match is None:
        return reply, None
    trace = match.group(1).strip() or None
    return reply[: match.start()] + reply[match.end() :], trace


def _load_json_array(text: str) -> list[Any]:
    block = _balanced(_FENCE_RE.sub("", text), "[", "]")
    if block is None:
        return []
    try:
        parsed = json.loads(block)
    except (json.JSONDecodeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _load_json_object(text: str) -> dict | None:
    block = _balanced(_FENCE_RE.sub("", text), "{", "}")
    if block is None:
        return None
    try:
        parsed = json.loads(block)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _balanced(text: str, open_char: str, close_char: str) -> str | None:
    """The first `open_char...close_char` span whose brackets balance,
    ignoring anything inside a JSON string — so a stray `]` in claim text
    can't truncate the array around it."""
    start = text.find(open_char)
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == open_char:
            depth += 1
        elif ch == close_char:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _resolve_indices(raw: Any, by_index: dict[int, EvidenceItem]) -> list[EvidenceItem]:
    if not isinstance(raw, list):
        return []
    return [by_index[i] for i in raw if isinstance(i, int) and not isinstance(i, bool) and i in by_index]


def _resolve_index(raw: Any, options: Sequence[Any]) -> Any | None:
    if not isinstance(raw, int) or isinstance(raw, bool):
        return None
    position = raw - 1
    return options[position] if 0 <= position < len(options) else None


def _as_str(value: Any, *, default: str) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else default


def _clamp(value: Any, lo: float, hi: float, *, default: float) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return default
    return min(hi, max(lo, float(value)))


def _reasoning(why: Any, trace: str | None, *, thinking: bool) -> str | None:
    """`why` if the claim carried one, else the shared `<think>` block, else
    `None` — and unconditionally `None` when thinking is off, so the column
    answers "was this claim reasoned about", not "did the model volunteer
    prose"."""
    if not thinking:
        return None
    if isinstance(why, str) and why.strip():
        return why.strip()
    return trace


# -- prompt rendering ---------------------------------------------------------


def render_evidence(items: Sequence[EvidenceItem]) -> str:
    return "\n".join(f"{i}. {item.text}" for i, item in enumerate(items, start=1))


def render_claims(claims: Sequence[Claim]) -> str:
    return "\n".join(
        f"{i}. [{claim.kind}] {claim.text}"
        f" (confidence {claim.confidence:.2f}, importance {claim.importance:.0f})"
        for i, claim in enumerate(claims, start=1)
    )


def render_candidates(candidates: Sequence[Candidate]) -> str:
    """§ 2.1's 'counts, not rows': citation *counts*, never citation quotes —
    a `Candidate` has no field to carry one."""
    lines = []
    for i, candidate in enumerate(candidates, start=1):
        support = candidate.support
        citations = support.citation_count if support else 0
        conversations = support.conversation_count if support else 0
        lines.append(
            f"{i}. [{candidate.fragment.kind}] {candidate.fragment.text}"
            f" (confidence {candidate.fragment.confidence:.2f},"
            f" citations {citations}, conversations {conversations})"
        )
    return "\n".join(lines)


# -- the pipeline -------------------------------------------------------------


class MemoryExtractor:
    """The six stages of rule 3, in order; `run()` composes them into one
    watermarked write."""

    def __init__(self, store: MemoryStore, provider: Any, tuning: Tuning | None = None) -> None:
        self._store = store
        self._provider = provider
        self._tuning = tuning or Tuning.from_env()

    async def run(self, source: EvidenceSource, *, thinking: bool = False) -> ApplyResult | None:
        scope_id = self._store.memory_scope(source.scope_id)
        if scope_id is None or source.read_through is None:
            return None  # I-2 (not a memory scope), or nothing new to read
        items = self.audit(self.observe(source.items))
        claims = await self.propose(items, thinking=thinking) if items else []
        candidates = self.retrieve(claims, scope_id=scope_id)
        decisions = await self.resolve(claims, candidates, thinking=thinking)
        return self.apply(decisions, scope_id=scope_id, source=source)

    @staticmethod
    def observe(items: Sequence[EvidenceItem]) -> list[EvidenceItem]:
        """Drop blank items (a cancelled reply's empty turn — not evidence,
        but the watermark must still cover it) and sort by `(created_at,
        id)`."""
        kept = [item for item in items if item.text.strip()]
        return sorted(kept, key=lambda item: (item.created_at, item.id))

    @staticmethod
    def audit(items: Sequence[EvidenceItem]) -> list[EvidenceItem]:
        # Identity by design. GUM gates observations on contextual integrity
        # here; chat memory has no such gate, and the persona pipeline will
        # need one without restructuring the pipeline around it.
        return list(items)

    async def propose(self, items: Sequence[EvidenceItem], *, thinking: bool = False) -> list[Claim]:
        reply = await self._generate(EXTRACTION_PROMPT, render_evidence(items), thinking=thinking)
        _LOG.debug("propose reply: %s", reply)
        return parse_claims(reply, items, thinking=thinking)

    def retrieve(self, claims: Sequence[Claim], *, scope_id: str) -> list[Candidate]:
        if not claims:
            return []
        query = " ".join(claim.text for claim in claims)
        found = self._store.candidates(scope_id, query, self._tuning.candidate_pool_k)
        return [Candidate(fragment=fragment, support=fragment.support) for fragment in found]

    async def resolve(
        self, claims: Sequence[Claim], candidates: Sequence[Candidate], *, thinking: bool = False
    ) -> list[Decision]:
        if not claims:
            return []
        if not candidates:  # every group's first run: nothing to compare against
            return [Decision(claim=claim, outcome=Outcome.NEW) for claim in claims]

        prompt = render_claims(claims) + "\n\nCandidates:\n" + render_candidates(candidates)
        reply = await self._generate(RESOLUTION_PROMPT, prompt, thinking=thinking)
        _LOG.debug("resolve reply: %s", reply)
        decisions = parse_decisions(reply, claims, candidates)

        resolved = []
        for decision in decisions:
            if decision.outcome is Outcome.REVISE:
                decision = await self._revise(decision, thinking=thinking)
            resolved.append(decision)
        return resolved

    def apply(
        self, decisions: Sequence[Decision], *, scope_id: str, source: EvidenceSource
    ) -> ApplyResult:
        writes = [
            write
            for write in (self._write_for(d, scope_id=scope_id, source=source) for d in decisions)
            if write is not None
        ]
        watermark = None
        if source.read_through is not None:
            extracted_at, extracted_id = source.read_through
            watermark = Watermark(
                conversation_id=source.id, extracted_at=extracted_at, extracted_id=extracted_id
            )
        return self._store.apply(writes, watermark=watermark)

    # -- helpers ---------------------------------------------------------

    async def _revise(self, decision: Decision, *, thinking: bool) -> Decision:
        assert decision.candidate is not None
        prompt = render_claims([decision.claim]) + "\n\nCandidate:\n" + render_candidates(
            [decision.candidate]
        )
        reply = await self._generate(REWRITE_PROMPT, prompt, thinking=thinking)
        _LOG.debug("rewrite reply: %s", reply)
        parsed = parse_revision(reply)
        if parsed is None:  # the one revise error that loses information — fall back
            return Decision(claim=decision.claim, outcome=Outcome.NEW)
        text, confidence = parsed
        return replace(decision, revised_text=text, revised_confidence=confidence)

    def _write_for(
        self, decision: Decision, *, scope_id: str, source: EvidenceSource
    ) -> FragmentWrite | None:
        if decision.outcome is Outcome.IGNORE:
            return None

        claim = decision.claim
        citations = [
            FragmentCitation(
                fragment_id=None,
                source_message_id=item.id,
                quote=item.text[: self._tuning.quote_max_chars],
            )
            for item in claim.evidence
        ]

        if decision.outcome is Outcome.NEW:
            fragment = MemoryFragment(
                group_id=scope_id,
                text=claim.text,
                kind=claim.kind,
                confidence=claim.confidence,
                importance=claim.importance,
                reasoning=claim.reasoning,
                origin_conversation_id=source.id,
            )
            return FragmentWrite(fragment=fragment, citations=citations)

        candidate = decision.candidate
        if candidate is None:  # unreachable via parse_decisions; defensive
            return None

        if decision.outcome is Outcome.REINFORCE:
            return FragmentWrite(fragment=candidate.fragment, citations=citations, reinforce=True)

        # REVISE: the rewrite call already produced the new text/confidence;
        # every other field carries over from the fragment being revised.
        fragment = replace(
            candidate.fragment,
            text=decision.revised_text,
            confidence=decision.revised_confidence,
        )
        return FragmentWrite(fragment=fragment, citations=citations)

    async def _generate(self, system: str, user: str, *, thinking: bool) -> str:
        turns = (PromptTurn(role="system", content=system), PromptTurn(role="user", content=user))
        options = GenerationOptions(
            temperature=self._tuning.extract_temperature,
            max_tokens=self._tuning.extract_max_tokens,
            thinking=thinking,
        )
        parts: list[str] = []
        async for chunk in self._provider.generate(turns, options):
            parts.append(chunk)
        return "".join(parts)
