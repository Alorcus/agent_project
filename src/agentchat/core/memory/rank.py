"""Rank fusion, decay and MMR diversity over memory fragments (§ 6).

Pure functions over data passed in — no store, no `Message`, no `Conversation`,
no SQL (I-6). `select()` and `stable_core()` in `storage/memory.py` are the
only callers that turn rows into `RankItem` and back.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from .embed import cosine
from .tuning import Tuning


@dataclass(frozen=True)
class RankItem:
    id: int
    text: str
    kind: str
    importance: float
    last_seen_at: datetime
    conversation_count: int
    embedding: tuple[float, ...] | None = None
    #: 1-based position in the BM25 hit list; `None` means the query's words
    #: never matched this fragment.
    bm25_rank: int | None = None


@dataclass(frozen=True)
class Ranked:
    item: RankItem
    similarity: float
    score: float


def decay_factor(kind: str, last_seen_at: datetime, *, now: datetime, tuning: Tuning) -> float:
    age_days = (now - last_seen_at).total_seconds() / 86400.0
    return math.exp(-tuning.alpha_for(kind) * tuning.decay_k * age_days)


def admit(
    items: Sequence[RankItem], query_vector: Sequence[float], *, floor: float
) -> list[tuple[RankItem, float]]:
    """The floor (§ 6.1): cosine on the query embedding, run before fusion
    (I-11). An item with no embedding never clears it — the same fail-closed
    rule as a missing encoder."""
    admitted = []
    for item in items:
        if item.embedding is None:
            continue
        similarity = cosine(query_vector, item.embedding)
        if similarity >= floor:
            admitted.append((item, similarity))
    return admitted


def _competition_ranks(keys: Sequence) -> list[int]:
    """Standard competition ranking (1, 1, 3, 4, ...): equal keys share the
    best position among them, and `keys` is already oriented so ascending
    means "better"."""
    order = sorted(range(len(keys)), key=lambda i: keys[i])
    ranks = [0] * len(keys)
    for position, index in enumerate(order):
        previous = order[position - 1]
        ranks[index] = ranks[previous] if position and keys[previous] == keys[index] else position + 1
    return ranks


def fuse(admitted: Sequence[tuple[RankItem, float]], *, now: datetime, tuning: Tuning) -> list[Ranked]:
    """RRF over four rank orderings — never over scores, so a weight tuned on
    one group's BM25 magnitudes is not wrong for the next."""
    if not admitted:
        return []
    items = [item for item, _ in admitted]
    similarities = {item.id: similarity for item, similarity in admitted}

    # Ascending "better first" keys. A missing BM25 hit sorts after every
    # real one, however large or small the real values happen to be.
    bm25_keys = [
        (item.bm25_rank is None, item.bm25_rank if item.bm25_rank is not None else 0)
        for item in items
    ]
    recency_keys = [-decay_factor(item.kind, item.last_seen_at, now=now, tuning=tuning) for item in items]
    support_keys = [-math.log1p(item.conversation_count) for item in items]
    importance_keys = [-item.importance for item in items]

    term_ranks = [
        _competition_ranks(bm25_keys),
        _competition_ranks(recency_keys),
        _competition_ranks(support_keys),
        _competition_ranks(importance_keys),
    ]

    ranked = [
        Ranked(
            item=item,
            similarity=similarities[item.id],
            score=sum(1.0 / (tuning.rrf_k + ranks[i]) for ranks in term_ranks),
        )
        for i, item in enumerate(items)
    ]
    ranked.sort(key=lambda r: (-r.score, r.item.id))
    return ranked


def diversify(
    ranked: Sequence[Ranked],
    *,
    budget: int,
    cost: Callable[[str], int],
    tuning: Tuning,
) -> list[RankItem]:
    """MMR (§ 6.3). The relevance term is the fused score normalised by the
    top score, so it lands in `(0, 1]` alongside the cosine similarity it is
    traded against — un-normalised RRF scores cluster too tightly to compete.
    The budget is a ceiling: a fragment too large for what is left is
    skipped, not a stopping point."""
    if not ranked:
        return []
    top_score = ranked[0].score

    remaining = list(ranked)
    selected: list[Ranked] = []
    spent = 0
    while remaining:
        best, best_value = None, None
        for candidate in remaining:
            relevance = candidate.score / top_score if top_score else 0.0
            diversity = max(
                (
                    cosine(candidate.item.embedding, chosen.item.embedding)
                    for chosen in selected
                    if candidate.item.embedding is not None and chosen.item.embedding is not None
                ),
                default=0.0,
            )
            value = tuning.mmr_lambda * relevance - (1 - tuning.mmr_lambda) * diversity
            if best_value is None or value > best_value:
                best, best_value = candidate, value
        remaining.remove(best)
        item_cost = cost(best.item.text)
        if spent + item_cost <= budget:
            selected.append(best)
            spent += item_cost

    return [r.item for r in selected]


def core_order(items: Sequence[RankItem], *, now: datetime, tuning: Tuning) -> list[RankItem]:
    """§ 2.2's query-free ordering: importance folded into the same decay,
    with no floor — there is no query for the core to be relevant to."""

    def rank(item: RankItem) -> tuple[float, int]:
        return (-(item.importance * decay_factor(item.kind, item.last_seen_at, now=now, tuning=tuning)), item.id)

    return sorted(items, key=rank)


def fill(items: Sequence[RankItem], *, budget: int, cost: Callable[[str], int]) -> list[RankItem]:
    """Apply `budget` as a ceiling over an already-ordered sequence: an item
    too large for what is left is skipped, not a stopping point."""
    filled: list[RankItem] = []
    spent = 0
    for item in items:
        item_cost = cost(item.text)
        if spent + item_cost <= budget:
            filled.append(item)
            spent += item_cost
    return filled


def rank_and_select(
    items: Sequence[RankItem],
    query_vector: Sequence[float],
    *,
    budget: int,
    cost: Callable[[str], int],
    now: datetime,
    tuning: Tuning,
) -> list[RankItem]:
    """Floor → fusion → diversity, composed — the whole of `select()`'s
    ranking rule."""
    admitted = admit(items, query_vector, floor=tuning.recall_floor)
    ranked = fuse(admitted, now=now, tuning=tuning)
    return diversify(ranked, budget=budget, cost=cost, tuning=tuning)
