"""Adaptive fact retrieval: a bounded RAG loop over a group's facts.

The corpus is the `facts` table, scoped to one group and nothing else. A fact
is one sentence, so there is no chunking — the fact is the fragment. Each fact
is indexed under two views, its claim and its evidence, and the vectors are
written when the fact is written; `FactIndex.ensure_indexed` is the catch-up
for facts that predate the vector, or an embedder switch.

The second corpus is `document_snippets`, which is **global**: a file the
user dropped into any chat is searchable from every chat. `SnippetIndex`
mirrors `FactIndex` over it, under one view — a snippet is its own claim and
its own evidence.

This is the only module that reads `fact_embeddings` and `snippet_embeddings`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Container, Sequence
from dataclasses import dataclass
from typing import Literal, TypeVar

import numpy as np

from agentchat.core.anchoring import significant
from agentchat.core.errors import AgentChatError, RetrievalError
from agentchat.core.facts import WINDOW_SIZE, countable
from agentchat.core.models import (
    Conversation,
    Document,
    Fact,
    FactEmbedding,
    Message,
    Snippet,
    SnippetEmbedding,
)
from agentchat.core.prompts import (
    GATE_PROMPT,
    GATE_SYSTEM,
    JUDGE_PROMPT,
    JUDGE_SYSTEM,
    MAX_SEEDS,
    REWRITE_PROMPT,
    REWRITE_SYSTEM,
    RESEED_PROMPT,
    RESEED_SYSTEM,
    parse_gate,
    parse_query,
    parse_seeds,
    parse_verdict,
    render_digest,
)
from agentchat.llm.base import GenerationOptions, complete
from agentchat.llm.embedding import Embedder
from agentchat.llm.registry import ModelRegistry
from agentchat.llm import transcript
from agentchat.storage.base import ConversationStore

_log = logging.getLogger(__name__)

VIEWS = ("claim", "evidence")

#: `assemble` runs over both corpora; only `text` differs.
H = TypeVar("H", "Hit", "SnippetHit")


@dataclass(frozen=True)
class Hit:
    fact: Fact
    view: str
    score: float


@dataclass(frozen=True)
class SnippetHit:
    snippet: Snippet
    #: The document header — `Document.text` is empty here; only
    #: `store.document()` fills it.
    document: Document
    score: float

    @property
    def source(self) -> str:
        """`title` or `title p.N` — how a snippet is attributed wherever it is
        shown, in the injected block and in the UI alike."""
        if self.snippet.page is None:
            return self.document.title
        return f"{self.document.title} p.{self.snippet.page}"


def view_text(fact: Fact, view: str) -> str:
    """`claim` → the fact text. `evidence` → `author: quote` per phrase,
    joined. `""` when the view has nothing (pre-plan-010 facts have no
    quotes) — an empty view is not embedded."""
    if view == "claim":
        return fact.text
    parts = [
        f"{phrase.author.label}: {phrase.quote}"
        for phrase in fact.phrases
        if phrase.quote
    ]
    return "\n".join(parts)


def fact_text(hit: Hit) -> str:
    return hit.fact.text


def snippet_text(hit: SnippetHit) -> str:
    return hit.snippet.text


def assemble(
    hits: Sequence[H], *, limit: int, text: Callable[[H], str] = fact_text
) -> tuple[H, ...]:
    """Sort by score descending, drop a hit whose significant-token set is a
    subset of an already-kept hit's (the read-time dedup plan 009 deferred),
    cut at `limit` (R9). One function over both corpora: `text` says which
    field carries the words."""
    kept: list[H] = []
    kept_tokens: list[set[str]] = []
    for hit in sorted(hits, key=lambda h: h.score, reverse=True):
        tokens = significant(text(hit))
        if any(tokens and tokens <= earlier for earlier in kept_tokens):
            continue
        kept.append(hit)
        kept_tokens.append(tokens)
        if len(kept) >= limit:
            break
    return tuple(kept)


def cap_per_document(
    hits: Sequence[SnippetHit], *, per_document: int
) -> tuple[SnippetHit, ...]:
    """Each document's best `per_document` hits. Adjacent snippets overlap by
    construction, so without this one long document fills the digest with
    neighbouring halves of the same passage."""
    if per_document <= 0:
        return tuple(hits)
    kept: list[SnippetHit] = []
    counts: dict[str, int] = {}
    for hit in sorted(hits, key=lambda h: h.score, reverse=True):
        document_id = hit.snippet.document_id
        if counts.get(document_id, 0) >= per_document:
            continue
        counts[document_id] = counts.get(document_id, 0) + 1
        kept.append(hit)
    return tuple(kept)


class FactIndex:
    def __init__(self, store: ConversationStore, embedder: Embedder) -> None:
        self._store = store
        self._embedder = embedder

    @property
    def embedder(self) -> Embedder:
        return self._embedder

    async def index(self, facts: Sequence[Fact]) -> None:
        """Embed `facts` under both views and write the vectors. An empty view
        is skipped, not embedded."""
        jobs: list[tuple[str, str]] = []  # (fact_id, view)
        texts: list[str] = []
        for fact in facts:
            for view in VIEWS:
                text = view_text(fact, view)
                if not text:
                    continue
                jobs.append((fact.id, view))
                texts.append(text)
        if not texts:
            return
        vectors = await self._embedder.embed(texts)
        rows = [
            FactEmbedding(
                fact_id=fact_id,
                view=view,
                model_id=self._embedder.info.id,
                vector=tuple(vector),
            )
            for (fact_id, view), vector in zip(jobs, vectors)
        ]
        await self._store.save_fact_embeddings(rows)

    async def ensure_indexed(self, group_id: str) -> int:
        """Embed the group's facts that have no vector for the active embedder
        id — pre-plan facts, facts written while recall was off, and facts
        from a different embedder (R4, R5). Returns the number embedded so a
        silent per-turn re-run shows up as a non-zero count that should be 0."""
        pending = await self._store.facts_without_embeddings(
            group_id, model_id=self._embedder.info.id
        )
        if not pending:
            return 0
        await self.index(pending)
        return len(pending)

    async def search(
        self,
        queries: Sequence[Sequence[float]],
        group_id: str,
        *,
        k: int,
        min_score: float,
        exclude: Container[str] = frozenset(),
    ) -> list[Hit]:
        """Every query's top `k` per view at or above `min_score`, unioned by
        fact id keeping the best-scoring `Hit` (R8). One matrix product for
        all queries and all views, not a loop per query."""
        if not queries:
            return []
        rows = await self._store.fact_embeddings(
            group_id, model_id=self._embedder.info.id
        )
        if not rows:
            return []
        facts = {fact.id: fact for fact in await self._store.list_facts(group_id)}

        query_matrix = np.asarray(queries, dtype="f4")  # (n_q, dim)
        best: dict[str, Hit] = {}
        for view in VIEWS:
            view_rows = [
                r for r in rows if r.view == view and r.fact_id in facts
            ]
            if not view_rows:
                continue
            matrix = np.asarray([r.vector for r in view_rows], dtype="f4")
            if matrix.shape[1] != query_matrix.shape[1]:
                continue
            scores = query_matrix @ matrix.T  # (n_q, n_v)
            for query_scores in scores:
                order = np.argsort(query_scores)[::-1][:k]
                for col in order:
                    score = float(query_scores[col])
                    if score < min_score:
                        continue
                    fact_id = view_rows[col].fact_id
                    if fact_id in exclude:
                        continue
                    current = best.get(fact_id)
                    if current is None or score > current.score:
                        best[fact_id] = Hit(
                            fact=facts[fact_id], view=view, score=score
                        )
        return list(best.values())

    @property
    def store(self) -> ConversationStore:
        return self._store


class SnippetIndex:
    """`FactIndex`'s counterpart over the document corpus. Two differences,
    both deliberate: it is global — no `group_id` anywhere — and it has one
    view, because a snippet is its own claim and its own evidence."""

    def __init__(self, store: ConversationStore, embedder: Embedder) -> None:
        self._store = store
        self._embedder = embedder

    @property
    def embedder(self) -> Embedder:
        return self._embedder

    @property
    def store(self) -> ConversationStore:
        return self._store

    async def index(self, snippets: Sequence[Snippet]) -> None:
        texts = [snippet.text for snippet in snippets]
        if not texts:
            return
        vectors = await self._embedder.embed(texts)
        await self._store.save_snippet_embeddings(
            [
                SnippetEmbedding(
                    snippet_id=snippet.id,
                    model_id=self._embedder.info.id,
                    vector=tuple(vector),
                )
                for snippet, vector in zip(snippets, vectors)
            ]
        )

    async def ensure_indexed(self) -> int:
        """Embed snippets with no vector for the active embedder id —
        documents ingested while recall was off, and every document after an
        embedder switch. Returns the number embedded, so a silent per-turn
        re-run shows up as a count that should be 0."""
        pending = await self._store.snippets_without_embeddings(
            model_id=self._embedder.info.id
        )
        if not pending:
            return 0
        await self.index(pending)
        return len(pending)

    async def search(
        self,
        queries: Sequence[Sequence[float]],
        *,
        k: int,
        min_score: float,
    ) -> list[SnippetHit]:
        """Every query's top `k` at or above `min_score`, unioned by snippet id
        keeping the best score. Ranks over vectors alone and hydrates only the
        winners — a search must not pull every snippet's text into memory."""
        if not queries:
            return []
        rows = await self._store.snippet_vectors(model_id=self._embedder.info.id)
        if not rows:
            return []

        query_matrix = np.asarray(queries, dtype="f4")  # (n_q, dim)
        matrix = np.asarray([row.vector for row in rows], dtype="f4")
        if matrix.shape[1] != query_matrix.shape[1]:
            # A corpus embedded at another width — `ensure_indexed` replaces it
            # on the next turn rather than this one crashing.
            return []
        scores = query_matrix @ matrix.T  # (n_q, n_s)

        best: dict[str, float] = {}
        for query_scores in scores:
            order = np.argsort(query_scores)[::-1][:k]
            for col in order:
                score = float(query_scores[col])
                if score < min_score:
                    continue
                snippet_id = rows[col].snippet_id
                if score > best.get(snippet_id, float("-inf")):
                    best[snippet_id] = score
        if not best:
            return []

        snippets = await self._store.snippets_by_ids(list(best))
        documents = {
            document.id: document for document in await self._store.list_documents()
        }
        return [
            SnippetHit(
                snippet=snippet,
                document=documents[snippet.document_id],
                score=best[snippet.id],
            )
            for snippet in snippets
            if snippet.document_id in documents
        ]


# -- the loop --------------------------------------------------------------

GATE_MAX_TOKENS = 4
REWRITE_MAX_TOKENS = 48
JUDGE_MAX_TOKENS = 64
RESEED_MAX_TOKENS = 96
DIGEST_PROMPT_OVERHEAD_TOKENS = 512
MIN_DIGEST_BUDGET = 256
_RECALL_OPTIONS_BASE = dict(temperature=0.0, thinking=False)


@dataclass(frozen=True)
class Recall:
    hits: tuple[Hit, ...]
    digest: str
    queries: tuple[str, ...]
    rounds: int
    exit: Literal["sufficient", "cap", "deadline"]
    #: The global document corpus's contribution — `()` when documents are
    #: off, or when nothing matched.
    snippets: tuple[SnippetHit, ...] = ()
    gap: str = ""
    #: The original user message, carried so `prompts.recall_block` can render
    #: the injected block from a `Recall` alone.
    user_text: str = ""


def _sys(content: str) -> Message:
    return Message(role="system", content=content)


def _user(content: str) -> Message:
    return Message(role="user", content=content)


def _tried_block(tried: Sequence[str]) -> str:
    return "\n".join(f"- {t}" for t in tried) if tried else "(none)"


class AdaptiveRetriever:
    """The single entry point and the single place failure is handled: every
    provider, storage, embedder or timeout failure inside `recall` is logged
    and returned as `None` — answer normally (R15). `None` also means the gate
    said the message is self-contained; both mean the same thing to the
    caller."""

    def __init__(
        self,
        registry: ModelRegistry,
        index: FactIndex,
        snippets: SnippetIndex | None = None,
        *,
        rounds: int = 3,
        rewrites: int = 3,
        hits: int = 10,
        seeds: int = 2,
        min_score: float = 0.25,
        digest_facts: int = 12,
        snippet_hits: int = 8,
        snippet_min_score: float = 0.25,
        digest_snippets: int = 5,
        snippets_per_document: int = 2,
        timeout: float = 120.0,
    ) -> None:
        self._registry = registry
        self.index = index
        #: `None` switches the document corpus off — no `snippet_embeddings`
        #: read, the fact path unchanged.
        self.snippets = snippets
        self._rounds = max(1, rounds)
        self._rewrites = max(1, rewrites)
        self._hits = hits
        self._seeds = max(1, min(seeds, MAX_SEEDS))
        self._min_score = min_score
        self._digest_facts = digest_facts
        self._snippet_hits = snippet_hits
        self._snippet_min_score = snippet_min_score
        self._digest_snippets = digest_snippets
        self._snippets_per_document = snippets_per_document
        self._timeout = timeout

    async def recall(
        self,
        conversation: Conversation,
        user_text: str,
        on_progress: Callable[[str], None] | None = None,
    ) -> Recall | None:
        try:
            return await asyncio.wait_for(
                self._run(conversation, user_text, on_progress), self._timeout
            )
        except (AgentChatError, TimeoutError, asyncio.TimeoutError) as error:
            _log.warning("recall failed: %s", error)
            return None

    async def _run(
        self,
        conversation: Conversation,
        user_text: str,
        on_progress: Callable[[str], None] | None,
    ) -> Recall | None:
        provider = await self._registry.active_provider()

        with transcript.label("recall.gate"):
            gate_reply = await complete(
                provider,
                [_sys(GATE_SYSTEM), _user(GATE_PROMPT.format(message=user_text))],
                GenerationOptions(max_tokens=GATE_MAX_TOKENS, **_RECALL_OPTIONS_BASE),
            )
        if not parse_gate(gate_reply):
            return None

        group_id = conversation.group_id
        exclude = await self._recent_fact_ids(conversation)
        budget = max(
            MIN_DIGEST_BUDGET,
            provider.info.context_window
            - max(
                GATE_MAX_TOKENS,
                REWRITE_MAX_TOKENS,
                JUDGE_MAX_TOKENS,
                RESEED_MAX_TOKENS,
            )
            - DIGEST_PROMPT_OVERHEAD_TOKENS,
        )

        deadline = time.monotonic() + self._timeout
        seeds: tuple[str, ...] = (user_text,)
        tried: list[str] = []
        pool: dict[str, Hit] = {}
        snippet_pool: dict[str, SnippetHit] = {}
        digest_hits: tuple[Hit, ...] = ()
        digest_snippets: tuple[SnippetHit, ...] = ()
        digest = ""
        gap = ""
        round_no = 1

        if on_progress is not None:
            on_progress("searching…")

        while True:
            if round_no > 1 and time.monotonic() > deadline:
                return Recall(
                    hits=digest_hits,
                    snippets=digest_snippets,
                    digest=digest,
                    queries=tuple(tried),
                    rounds=round_no - 1,
                    exit="deadline",
                    gap=gap,
                    user_text=user_text,
                )

            round_queries = await self._rewrite_round(
                provider, user_text, seeds, tried
            )
            if round_queries:
                try:
                    vectors = await self.index.embedder.embed(round_queries)
                except AgentChatError:
                    raise
                except Exception as error:  # noqa: BLE001 — one boundary (R15)
                    raise RetrievalError(f"embedding queries failed: {error}") from error
                try:
                    hits = await self.index.search(
                        vectors,
                        group_id,
                        k=self._hits,
                        min_score=self._min_score,
                        exclude=exclude,
                    )
                except AgentChatError:
                    raise
                except Exception as error:  # noqa: BLE001 — one boundary (R15)
                    raise RetrievalError(f"vector search failed: {error}") from error
                for hit in hits:
                    current = pool.get(hit.fact.id)
                    if current is None or hit.score > current.score:
                        pool[hit.fact.id] = hit
                for snippet_hit in await self._search_snippets(vectors):
                    current = snippet_pool.get(snippet_hit.snippet.id)
                    if current is None or snippet_hit.score > current.score:
                        snippet_pool[snippet_hit.snippet.id] = snippet_hit
            tried.extend(round_queries)

            digest_hits = assemble(tuple(pool.values()), limit=self._digest_facts)
            digest_snippets = assemble(
                cap_per_document(
                    tuple(snippet_pool.values()),
                    per_document=self._snippets_per_document,
                ),
                limit=self._digest_snippets,
                text=snippet_text,
            )
            digest = render_digest(digest_hits, digest_snippets, budget=budget)

            if on_progress is not None:
                on_progress("judging…")
            enough, gap = await self._judge(
                provider, user_text, digest_hits, digest_snippets
            )

            if enough:
                return Recall(
                    hits=digest_hits,
                    snippets=digest_snippets,
                    digest=digest,
                    queries=tuple(tried),
                    rounds=round_no,
                    exit="sufficient",
                    gap="",
                    user_text=user_text,
                )
            if round_no >= self._rounds:
                return Recall(
                    hits=digest_hits,
                    snippets=digest_snippets,
                    digest=digest,
                    queries=tuple(tried),
                    rounds=round_no,
                    exit="cap",
                    gap=gap,
                    user_text=user_text,
                )

            seeds = await self._reseed(provider, user_text, gap, tried)
            round_no += 1
            if on_progress is not None:
                on_progress(f"round {round_no} of {self._rounds}…")

    async def _recent_fact_ids(self, conversation: Conversation) -> frozenset[str]:
        """Facts of *this* conversation covering messages the recency window
        is almost certainly still carrying verbatim — injecting them would
        spend budget restating the prompt. Older facts of the same
        conversation are still retrieved."""
        cutoff = len(countable(conversation.messages)) - WINDOW_SIZE
        try:
            facts = await self.index.store.list_facts(conversation.group_id)
        except AgentChatError:
            raise
        except Exception as error:  # noqa: BLE001 — one boundary (R15)
            raise RetrievalError(f"loading recent facts failed: {error}") from error
        return frozenset(
            fact.id
            for fact in facts
            if fact.conversation_id == conversation.id and fact.window_end > cutoff
        )

    async def _rewrite_round(
        self,
        provider,
        user_text: str,
        seeds: Sequence[str],
        tried: Sequence[str],
    ) -> list[str]:
        written: list[str] = []
        seen = {t.lower() for t in tried}
        for seed in seeds:
            for _ in range(self._rewrites):
                with transcript.label("recall.rewrite"):
                    reply = await complete(
                        provider,
                        [
                            _sys(REWRITE_SYSTEM),
                            _user(
                                REWRITE_PROMPT.format(
                                    message=user_text,
                                    seed=seed,
                                    tried=_tried_block(list(tried) + written),
                                )
                            ),
                        ],
                        GenerationOptions(
                            max_tokens=REWRITE_MAX_TOKENS, **_RECALL_OPTIONS_BASE
                        ),
                    )
                query = parse_query(reply)
                if query and query.lower() not in seen:
                    seen.add(query.lower())
                    written.append(query)
        return written

    async def _search_snippets(
        self, vectors: Sequence[Sequence[float]]
    ) -> list[SnippetHit]:
        if self.snippets is None:
            return []
        try:
            return await self.snippets.search(
                vectors, k=self._snippet_hits, min_score=self._snippet_min_score
            )
        except AgentChatError:
            raise
        except Exception as error:  # noqa: BLE001 — one boundary (R15)
            raise RetrievalError(f"snippet search failed: {error}") from error

    async def _judge(
        self,
        provider,
        user_text: str,
        digest_hits: Sequence[Hit],
        digest_snippets: Sequence[SnippetHit] = (),
    ) -> tuple[bool, str]:
        lines = [f"- {h.fact.text}" for h in digest_hits]
        lines += [
            f'- [{h.source}] "{" ".join(h.snippet.text.split())}"'
            for h in digest_snippets
        ]
        evidence = "\n".join(lines) if lines else "(nothing retrieved)"
        with transcript.label("recall.judge"):
            reply = await complete(
                provider,
                [
                    _sys(JUDGE_SYSTEM),
                    _user(JUDGE_PROMPT.format(message=user_text, evidence=evidence)),
                ],
                GenerationOptions(max_tokens=JUDGE_MAX_TOKENS, **_RECALL_OPTIONS_BASE),
            )
        return parse_verdict(reply)

    async def _reseed(
        self, provider, user_text: str, gap: str, tried: Sequence[str]
    ) -> tuple[str, ...]:
        with transcript.label("recall.reseed"):
            reply = await complete(
                provider,
                [
                    _sys(RESEED_SYSTEM),
                    _user(
                        RESEED_PROMPT.format(
                            message=user_text,
                            gap=gap,
                            tried=_tried_block(tried),
                            limit=self._seeds,
                        )
                    ),
                ],
                GenerationOptions(max_tokens=RESEED_MAX_TOKENS, **_RECALL_OPTIONS_BASE),
            )
        seeds = parse_seeds(reply, limit=self._seeds)
        return seeds or (user_text,)
