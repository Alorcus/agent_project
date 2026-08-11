"""Stage 3 acceptance: recall that can legitimately return nothing.

Detail plan: ``docs/plans/2026-08-11-006-memory-stage-3-read-path.md``. The
invariants themselves (I-2 read side, I-11, I-13) live in ``test_invariants.py``;
this file covers the rest of the read path — `rank.py`'s three pure steps, the
two store reads they compose into, the embedding lifecycle that feeds them, the
startup guard, and the prompt assembly `GroupMemoryStrategy` performs.

Similarity is **scripted**, never inferred: `ScriptedEncoder` maps a text to the
vector the test wants it to have, so every floor and MMR assertion is a claim
about the ranking code rather than about an encoder's opinion. The real encoder
is exercised in ``test_recall_real_encoder.py``, which is skipped without
``AGENTCHAT_TEST_REAL_ENCODER=1`` and asserts mechanics only.

`agentchat` imports sit inside the test bodies, as in the rest of the memory
suite: the modules under test do not exist yet, and a module-level import would
fail collection for the whole file.
"""

from __future__ import annotations

import logging
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from factories import (
    GraphBuilder,
    ScriptedEncoder,
    embedded,
    make_fragment,
    make_group,
    write,
)
from test_extraction import a_claim, a_decision, reply, supplier

#: The query every test that needs one asks, and the vector the scripted
#: encoder returns for it. `OFF_TOPIC` is orthogonal to it: cosine 0, below any
#: positive floor.
QUERY = "which database did we choose for storage"
ON_TOPIC = (1.0, 0.0)
OFF_TOPIC = (0.0, 1.0)
ENCODER_ID = "test-encoder"

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)


def tuning(**overrides):
    from agentchat.core.memory.tuning import Tuning

    return Tuning(**overrides)


def encoder(extra: dict | None = None) -> ScriptedEncoder:
    """The common case: the query is on topic, everything else encodes to the
    zero vector and clears no floor."""
    return ScriptedEncoder({QUERY: ON_TOPIC, **(extra or {})})


def fresh(tmp_path: Path, *, enc=None, **tuning_overrides):
    from agentchat.storage.memory import SqliteMemoryStore

    path = tmp_path / "chat.db"
    return path, SqliteMemoryStore(path, tuning=tuning(**tuning_overrides), encoder=enc)


def rows(path: Path, sql: str, *params) -> list[tuple]:
    with sqlite3.connect(path) as conn:
        return conn.execute(sql, params).fetchall()


def item(**overrides):
    """One `RankItem`, defaulting to "middling on every term"."""
    from agentchat.core.memory.rank import RankItem

    defaults = dict(
        id=1,
        text="The project stores conversations in SQLite.",
        kind="fact",
        importance=5.0,
        last_seen_at=NOW - timedelta(days=1),
        conversation_count=1,
        embedding=ON_TOPIC,
        bm25_rank=1,
    )
    defaults.update(overrides)
    return RankItem(**defaults)


def fused(items, *, floor=0.0, query=ON_TOPIC, **tuning_overrides):
    """floor -> fuse, the two steps whose *order* is I-11."""
    from agentchat.core.memory.rank import admit, fuse

    settings = tuning(**tuning_overrides)
    return fuse(admit(items, query, floor=floor), now=NOW, tuning=settings)


# -- rank.py: the floor ---------------------------------------------------


def test_the_floor_admits_only_what_clears_it():
    from agentchat.core.memory.rank import admit

    above = item(id=1, embedding=ON_TOPIC)
    below = item(id=2, embedding=(0.5, 0.866))  # cosine 0.5

    admitted = admit([above, below], ON_TOPIC, floor=0.6)

    assert [i.id for i, _ in admitted] == [above.id]
    assert admitted[0][1] == 1.0


def test_a_fragment_without_an_embedding_is_never_admitted():
    from agentchat.core.memory.rank import admit

    assert admit([item(embedding=None)], ON_TOPIC, floor=0.0) == []


def test_a_floor_nothing_clears_admits_nothing():
    from agentchat.core.memory.rank import admit

    items = [item(id=1), item(id=2, embedding=OFF_TOPIC)]

    assert admit(items, ON_TOPIC, floor=1.01) == []


# -- rank.py: fusion ------------------------------------------------------


def test_fusion_is_deterministic_and_independent_of_input_order():
    items = [
        item(id=1, importance=9.0, conversation_count=3, bm25_rank=2),
        item(id=2, importance=4.0, conversation_count=1, bm25_rank=1),
        item(id=3, importance=6.0, conversation_count=2, bm25_rank=3),
    ]

    forward = fused(items)
    backward = fused(list(reversed(items)))
    again = fused(items)

    assert [r.item.id for r in forward] == [r.item.id for r in backward]
    assert [r.score for r in forward] == [r.score for r in again]


def test_fusion_reads_ranks_not_scores():
    """Rescaling a signal without reordering it must not move anything: RRF
    sums rank positions, which is why a weight tuned on one group is not wrong
    for the next."""
    modest = [
        item(id=1, importance=6.0, conversation_count=2, bm25_rank=1),
        item(id=2, importance=5.0, conversation_count=1, bm25_rank=2),
    ]
    inflated = [
        item(id=1, importance=10.0, conversation_count=400, bm25_rank=1),
        item(id=2, importance=1.0, conversation_count=1, bm25_rank=90),
    ]

    assert [r.item.id for r in fused(modest)] == [r.item.id for r in fused(inflated)]
    assert [r.score for r in fused(modest)] == [r.score for r in fused(inflated)]


def test_terms_that_tie_contribute_equally():
    twins = [item(id=1), item(id=2)]

    scores = {r.item.id: r.score for r in fused(twins)}

    assert scores[1] == scores[2]


def test_an_unmatched_fragment_ranks_behind_every_bm25_hit():
    """A fragment the query's words never touch is not excluded — the other
    three terms still speak for it — but it starts behind everything they do."""
    matched = item(id=1, bm25_rank=3, importance=1.0, conversation_count=1)
    unmatched = item(id=2, bm25_rank=None, importance=1.0, conversation_count=1)

    assert [r.item.id for r in fused([unmatched, matched])] == [matched.id, unmatched.id]


def test_an_untouched_open_question_ranks_below_an_equally_old_decision():
    """§ 6.2: `α` is per `kind`, so the same age means different staleness. A
    question nobody has mentioned in three weeks is stale; a decision is not."""
    stale = NOW - timedelta(days=21)
    decision = item(id=1, kind="decision", last_seen_at=stale)
    question = item(id=2, kind="open_question", last_seen_at=stale)

    assert [r.item.id for r in fused([question, decision])] == [decision.id, question.id]


def test_a_kind_nobody_registered_decays_like_a_fact():
    """Rule 4: `kind` is open, and an unknown one must rank, not raise."""
    persona = item(id=1, kind="persona_trait", last_seen_at=NOW - timedelta(days=21))
    fact = item(id=2, kind="fact", last_seen_at=NOW - timedelta(days=21))

    scores = {r.item.id: r.score for r in fused([persona, fact])}

    assert scores[1] == scores[2]


# -- rank.py: diversity and the budget ceiling ----------------------------


def test_mmr_suppresses_a_near_duplicate_of_what_is_already_selected():
    """§ 6.3: two fragments resting on the same turn are textually similar, so
    the diversity term is what keeps one piece of evidence from being shown
    three times."""
    from agentchat.core.memory.rank import diversify
    from agentchat.core.tokens import estimate_tokens

    original = item(id=1, embedding=(1.0, 0.0), importance=9.0, bm25_rank=1)
    near_duplicate = item(id=2, embedding=(0.99, 0.14), importance=8.0, bm25_rank=2)
    different = item(id=3, embedding=(0.0, 1.0), importance=1.0, bm25_rank=3)

    ranked = fused([original, near_duplicate, different], query=(1.0, 0.0))
    assert [r.item.id for r in ranked] == [1, 2, 3], "the premise: fusion prefers the twin"

    budget = estimate_tokens(original.text) * 2
    spread = diversify(ranked, budget=budget, cost=estimate_tokens, tuning=tuning())
    packed = diversify(
        ranked, budget=budget, cost=estimate_tokens, tuning=tuning(mmr_lambda=1.0)
    )

    assert [i.id for i in spread] == [1, 3]
    assert [i.id for i in packed] == [1, 2], "λ=1 is pure relevance — the control"


def test_selection_stops_at_the_budget_ceiling():
    from agentchat.core.memory.rank import diversify
    from agentchat.core.tokens import estimate_tokens

    items = [item(id=i, bm25_rank=i, embedding=(1.0, i / 10)) for i in range(1, 6)]
    ranked = fused(items)
    room_for_two = estimate_tokens(items[0].text) * 2 + 1

    chosen = diversify(ranked, budget=room_for_two, cost=estimate_tokens, tuning=tuning())

    assert len(chosen) == 2


def test_the_core_ordering_folds_importance_into_the_same_decay():
    """§ 2.2's overflow ordering: `importance · exp(−α·k·age)`, no query."""
    from agentchat.core.memory.rank import core_order

    durable = item(id=1, kind="decision", importance=9.0, last_seen_at=NOW - timedelta(days=30))
    fresh_but_minor = item(id=2, kind="fact", importance=2.0, last_seen_at=NOW)
    forgotten = item(id=3, kind="open_question", importance=9.0, last_seen_at=NOW - timedelta(days=30))

    ordered = core_order([forgotten, fresh_but_minor, durable], now=NOW, tuning=tuning())

    assert [i.id for i in ordered] == [durable.id, fresh_but_minor.id, forgotten.id]


# -- select(): the store's composition of the three steps -----------------


def seeded(tmp_path: Path, *, enc=None, **tuning_overrides):
    """A project group with three extracted fragments, all on topic, one
    conversation, plus a consolidated one for the tier tests."""
    path, store = fresh(tmp_path, enc=enc, **tuning_overrides)
    builder = GraphBuilder(make_group(kind="project"))
    chat = builder.conversation()
    fragments = [
        builder.extracted(
            builder.message(chat, content=f"Storage note {n}."),
            id=n,
            text=f"The database we chose for storage, detail {n}.",
            importance=float(10 - n),
            **embedded((1.0, n / 10)),
        )
        for n in (1, 2, 3)
    ]
    write(store, builder.build())
    return path, store, builder, fragments


def test_select_returns_nothing_when_nothing_clears_the_floor(tmp_path: Path):
    """§ 6.1's whole point: the query-selected block can be empty, and that is
    an outcome rather than a shortfall."""
    path, store, builder, _ = seeded(tmp_path, enc=ScriptedEncoder({QUERY: OFF_TOPIC}))

    assert store.select(builder.group.id, QUERY, 4096) == []


def test_select_is_a_ceiling_not_a_target(tmp_path: Path):
    from agentchat.core.tokens import estimate_tokens

    path, store, builder, fragments = seeded(tmp_path, enc=encoder())
    room_for_one = estimate_tokens(fragments[0].text) + 1

    assert len(store.select(builder.group.id, QUERY, room_for_one)) == 1
    # And the other direction: a budget with room for all three is not a quota
    # — a floor only one of them clears returns one.
    picky = fresh(tmp_path, enc=ScriptedEncoder({QUERY: (1.0, 0.05)}), recall_floor=0.998)[1]
    assert len(picky.select(builder.group.id, QUERY, 4096)) == 1


def test_select_skips_the_consolidated_tier_unless_asked(tmp_path: Path):
    from agentchat.core.memory.types import Tier

    path, store, builder, fragments = seeded(tmp_path, enc=encoder())
    higher = builder.consolidated(
        fragments[0], id=9, text="The database we chose is settled.", **embedded(ON_TOPIC)
    )
    write(store, builder.build())

    extracted_only = [f.id for f in store.select(builder.group.id, QUERY, 4096)]
    both = [
        f.id
        for f in store.select(
            builder.group.id, QUERY, 4096, tiers=Tier.EXTRACTED | Tier.CONSOLIDATED
        )
    ]

    assert higher.id not in extracted_only
    assert higher.id in both


def test_select_is_scoped_to_its_group(tmp_path: Path):
    path, store, builder, _ = seeded(tmp_path, enc=encoder())

    other = GraphBuilder(make_group(kind="project"))
    chat = other.conversation()
    other.extracted(
        other.message(chat, content="Another group's turn."),
        id=50,
        text="The database we chose for storage, in another group.",
        **embedded(ON_TOPIC),
    )
    write(store, other.build())

    assert [f.id for f in store.select(other.group.id, QUERY, 4096)] == [50]


def test_select_without_an_encoder_recalls_nothing(tmp_path: Path):
    """Fail closed. Without an encoder there is no floor, and recall without a
    floor is § 6.1's failure mode — every group contributing its top-k to every
    prompt. The startup guard is what makes this visible."""
    path, store, builder, _ = seeded(tmp_path, enc=None)

    assert store.select(builder.group.id, QUERY, 4096) == []


def test_select_ignores_a_fragment_embedded_by_another_model(tmp_path: Path):
    """A vector from another encoder is not comparable to this one's query
    vector — the re-embed path is the remedy, not a silent cosine."""
    path, store, builder, fragments = seeded(tmp_path, enc=encoder())
    stale = builder.extracted(
        builder.message(builder.conversations[0], content="Older note."),
        id=20,
        text="The database we chose for storage, embedded elsewhere.",
        **embedded(ON_TOPIC, model_id="some-other-encoder"),
    )
    write(store, builder.build())

    assert stale.id not in [f.id for f in store.select(builder.group.id, QUERY, 4096)]


def test_select_tolerates_fts_syntax_in_the_query(tmp_path: Path):
    path, store, builder, _ = seeded(tmp_path, enc=ScriptedEncoder({'NEAR "db" OR': ON_TOPIC}))

    assert store.select(builder.group.id, 'NEAR "db" OR', 4096) != []


def test_select_writes_nothing(tmp_path: Path):
    """§ 7 #15 is parked: decay runs from `last_seen_at`, and recall does not
    touch it. Recall is a read."""
    path, store, builder, _ = seeded(tmp_path, enc=encoder())
    dump = "SELECT * FROM memory_fragments"
    citations = "SELECT * FROM fragment_citations"
    before = (rows(path, dump), rows(path, citations))

    store.select(builder.group.id, QUERY, 4096)

    assert (rows(path, dump), rows(path, citations)) == before


# -- stable_core() --------------------------------------------------------


def test_stable_core_is_empty_before_the_first_consolidation(tmp_path: Path):
    """§ 2.2's cold start: a group with no consolidated tier has an empty core,
    and that is correct rather than an error."""
    path, store, builder, _ = seeded(tmp_path, enc=encoder())

    assert store.stable_core(builder.group.id, 4096) == []


def test_stable_core_needs_no_encoder(tmp_path: Path):
    """There is no query for the core to be relevant to, so no floor applies
    and no vector is needed."""
    path, store, builder, fragments = seeded(tmp_path, enc=None)
    higher = builder.consolidated(fragments[0], id=9, text="Storage is settled.")
    write(store, builder.build())

    assert [f.id for f in store.stable_core(builder.group.id, 4096)] == [higher.id]


def test_stable_core_truncates_by_core_rank_when_it_overflows(tmp_path: Path):
    from agentchat.core.tokens import estimate_tokens

    path, store, builder, fragments = seeded(tmp_path, enc=None)
    minor = builder.consolidated(
        fragments[0], id=10, text="Storage has a settled shape.", importance=2.0
    )
    major = builder.consolidated(
        fragments[1], id=11, text="Storage is settled: SQLite in WAL.", importance=9.0
    )
    write(store, builder.build())

    room_for_one = estimate_tokens(major.text) + 1

    assert [f.id for f in store.stable_core(builder.group.id, room_for_one)] == [major.id]
    assert {f.id for f in store.stable_core(builder.group.id, 4096)} == {minor.id, major.id}


def test_stable_core_excludes_dormant_fragments(tmp_path: Path):
    path, store, builder, fragments = seeded(tmp_path, enc=None)
    dormant = builder.consolidated(
        fragments[0], id=12, text="Storage was settled once.", confidence=0.0
    )
    write(store, builder.build())

    assert dormant.id not in [f.id for f in store.stable_core(builder.group.id, 4096)]


# -- the embedding lifecycle ---------------------------------------------


def test_the_byte_format_round_trips():
    from agentchat.core.memory.embed import pack, unpack

    vector = (0.5, -0.25, 0.125)

    assert unpack(pack(vector)) == vector
    assert len(pack(vector)) == 12, "float32, four bytes a dimension"
    assert unpack(None) is None
    assert unpack(b"\x00\x00\x00") is None, "a misaligned blob is not a vector"


async def test_extraction_embeds_the_fragment_it_writes(tmp_path: Path):
    """Where embeddings come from: extraction, at write time, with the encoder
    that is configured — never the recall path, which must not write."""
    from agentchat.core.memory.embed import unpack
    from agentchat.core.memory.strategy import ChatMemory
    from test_invariants import ScriptedProvider

    claim = "The project stores conversations in SQLite."
    enc = ScriptedEncoder({claim: ON_TOPIC})
    path, store = fresh(tmp_path, enc=enc)
    builder = GraphBuilder(make_group(kind="project"))
    chat = builder.conversation()
    builder.message(chat, content="We chose SQLite.")
    write(store, builder.build())

    provider = ScriptedProvider(reply(a_claim(text=claim)), reply(a_decision()))
    await ChatMemory(store, supplier(provider), encoder=enc).extract(chat)

    stored = rows(path, "SELECT embedding, embedding_model FROM memory_fragments")
    assert [unpack(blob) for blob, _ in stored] == [ON_TOPIC]
    assert [model for _, model in stored] == [ENCODER_ID]


def test_reembedding_fills_in_what_stage_2_left_behind(tmp_path: Path):
    """Stage-2 fragments carry neither an embedding nor an `embedding_model`;
    § 9's remedy is incremental, which is what the per-fragment column buys."""
    from agentchat.core.memory.embed import reembed, unpack

    text = "The database we chose for storage is SQLite."
    enc = ScriptedEncoder({QUERY: ON_TOPIC, text: (0.5, 0.25)})
    path, store = fresh(tmp_path, enc=enc)
    builder = GraphBuilder(make_group(kind="project"))
    chat = builder.conversation()
    unembedded = builder.extracted(builder.message(chat, content="We chose SQLite."), text=text)
    write(store, builder.build())
    assert rows(path, "SELECT embedding_model FROM memory_fragments") == [(None,)]

    assert reembed(store, enc) == 1

    stored = rows(path, "SELECT embedding, embedding_model, revised_at FROM memory_fragments")
    assert [unpack(blob) for blob, _, _ in stored] == [(0.5, 0.25)]
    assert [model for _, model, _ in stored] == [ENCODER_ID]
    assert [revised for *_, revised in stored] == [None], "a re-embed is not a revision"
    assert unembedded.id in [f.id for f in store.select(builder.group.id, QUERY, 4096)]


def test_reembedding_replaces_a_vector_from_another_encoder(tmp_path: Path):
    from agentchat.core.memory.embed import reembed, unpack

    text = "The database we chose for storage is SQLite."
    enc = ScriptedEncoder({text: (0.5, 0.25)})
    path, store = fresh(tmp_path, enc=enc)
    builder = GraphBuilder(make_group(kind="project"))
    chat = builder.conversation()
    builder.extracted(
        builder.message(chat, content="We chose SQLite."),
        text=text,
        **embedded(OFF_TOPIC, model_id="some-other-encoder"),
    )
    write(store, builder.build())

    assert reembed(store, enc) == 1
    assert [unpack(blob) for (blob,) in rows(path, "SELECT embedding FROM memory_fragments")] == [
        (0.5, 0.25)
    ]


def test_reembedding_is_bounded_and_resumable(tmp_path: Path):
    """One pass takes `REEMBED_LIMIT` fragments, so a large group is caught up
    over several background runs rather than one long stall."""
    from agentchat.core.memory.embed import reembed

    path, store = fresh(tmp_path)
    two_at_a_time = tuning(reembed_limit=2)
    builder = GraphBuilder(make_group(kind="project"))
    chat = builder.conversation()
    texts = [f"Fragment number {n} about storage." for n in range(5)]
    for n, text in enumerate(texts, start=1):
        builder.extracted(builder.message(chat, content=f"Turn {n}."), id=n, text=text)
    write(store, builder.build())
    enc = ScriptedEncoder({text: (1.0, 0.0) for text in texts})

    assert reembed(store, enc, tuning=two_at_a_time) == 2
    assert reembed(store, enc, tuning=two_at_a_time) == 2
    assert reembed(store, enc, tuning=two_at_a_time) == 1
    assert reembed(store, enc, tuning=two_at_a_time) == 0
    assert rows(path, "SELECT COUNT(*) FROM memory_fragments WHERE embedding_model IS NULL") == [
        (0,)
    ]


def test_reembedding_leaves_text_confidence_and_citations_alone(tmp_path: Path):
    from agentchat.core.memory.embed import reembed

    text = "The database we chose for storage is SQLite."
    enc = ScriptedEncoder({text: ON_TOPIC})
    path, store = fresh(tmp_path, enc=enc)
    builder = GraphBuilder(make_group(kind="project"))
    chat = builder.conversation()
    builder.extracted(builder.message(chat, content="We chose SQLite."), text=text, confidence=0.4)
    write(store, builder.build())
    before = rows(path, "SELECT text, confidence, kind, importance FROM memory_fragments")
    citations = rows(path, "SELECT * FROM fragment_citations")

    reembed(store, enc)

    assert rows(path, "SELECT text, confidence, kind, importance FROM memory_fragments") == before
    assert rows(path, "SELECT * FROM fragment_citations") == citations


# -- the startup guard ----------------------------------------------------


def audit(**overrides):
    from agentchat.core.memory.embed import EmbeddingAudit

    defaults = dict(total=3, usable=3, missing=0, stale=0)
    defaults.update(overrides)
    return EmbeddingAudit(**defaults)


def test_the_guard_warns_when_the_encoder_is_unpinned():
    from agentchat.core.memory.embed import check_encoder

    warnings = check_encoder(
        audit(), encoder_id=ENCODER_ID, tuning=tuning(recall_floor_model="")
    )

    assert len(warnings) == 1
    assert "RECALL_FLOOR_MODEL" in warnings[0]


def test_the_guard_warns_when_the_encoder_is_not_the_one_the_floor_was_set_for():
    from agentchat.core.memory.embed import check_encoder

    warnings = check_encoder(
        audit(), encoder_id="another-encoder", tuning=tuning(recall_floor_model=ENCODER_ID)
    )

    assert len(warnings) == 1
    assert "another-encoder" in warnings[0] and ENCODER_ID in warnings[0]


def test_the_guard_counts_the_fragments_a_swap_invalidated(caplog):
    from agentchat.core.memory.embed import check_encoder

    logger = logging.getLogger("test.memory.guard")
    with caplog.at_level(logging.WARNING, logger=logger.name):
        warnings = check_encoder(
            audit(total=10, usable=3, missing=2, stale=5),
            encoder_id=ENCODER_ID,
            tuning=tuning(recall_floor_model=ENCODER_ID),
            log=logger,
        )

    assert len(warnings) == 1
    assert "5" in warnings[0] and "2" in warnings[0]
    assert [r.getMessage() for r in caplog.records if r.name == logger.name] == warnings


def test_the_guard_is_silent_when_the_encoder_and_the_corpus_agree():
    from agentchat.core.memory.embed import check_encoder

    assert (
        check_encoder(audit(), encoder_id=ENCODER_ID, tuning=tuning(recall_floor_model=ENCODER_ID))
        == []
    )


def test_the_guard_says_so_when_there_is_no_encoder_at_all():
    from agentchat.core.memory.embed import check_encoder

    warnings = check_encoder(
        audit(), encoder_id=None, tuning=tuning(recall_floor_model=ENCODER_ID)
    )

    assert len(warnings) == 1
    assert "recall" in warnings[0].lower()


def test_the_audit_counts_what_the_store_actually_holds(tmp_path: Path):
    path, store = fresh(tmp_path)
    builder = GraphBuilder(make_group(kind="project"))
    chat = builder.conversation()
    message = builder.message(chat, content="We chose SQLite.")
    builder.extracted(message, id=1, text="Usable.", **embedded(ON_TOPIC))
    builder.extracted(message, id=2, text="From another encoder.", **embedded(ON_TOPIC, model_id="other"))
    builder.extracted(message, id=3, text="Never embedded.")
    write(store, builder.build())

    found = store.embedding_audit(ENCODER_ID)

    assert (found.total, found.usable, found.stale, found.missing) == (3, 1, 1, 1)


# -- GroupMemoryStrategy: the two blocks and the budget -------------------


class SpyStore:
    """A `MemoryStore` that answers with fixed fragments and records the
    budgets and query it was handed — the seam for asserting on assembly
    arithmetic without a database."""

    def __init__(self, *, scope: str | None = "g1", core=(), selected=()):
        self._scope = scope
        self._core = list(core)
        self._selected = list(selected)
        self.core_budget: int | None = None
        self.sel_budget: int | None = None
        self.query: str | None = None

    def memory_scope(self, group_id: str) -> str | None:
        return self._scope

    def stable_core(self, group_id: str, budget: int):
        self.core_budget = budget
        return list(self._core)

    def select(self, group_id: str, query: str, budget: int, **kwargs):
        self.sel_budget = budget
        self.query = query
        return list(self._selected)

    def candidates(self, group_id: str, query: str, k: int):  # pragma: no cover
        return []


def conversation_of(*contents: str):
    from agentchat.core.models import Conversation, Message

    chat = Conversation(group_id="g1")
    for index, content in enumerate(contents):
        chat.add(Message(role="user" if index % 2 == 0 else "assistant", content=content))
    return chat


def strategy_over(store, **kwargs):
    from agentchat.core.context import RecencyWindowStrategy
    from agentchat.core.memory.strategy import GroupMemoryStrategy

    return GroupMemoryStrategy(inner=RecencyWindowStrategy(), store=store, **kwargs)


def test_the_two_blocks_are_spliced_in_the_section_22_order():
    """Cacheable prefix first (system, stable core), volatile tail last
    (query-selected, then the turn being answered) — the layout the KV cache
    argument rests on."""
    from agentchat.core.models import Message

    core = make_fragment(text="CORE-FRAGMENT")
    picked = make_fragment(text="SELECTED-FRAGMENT")
    chat = conversation_of("older question", "older answer", "the latest question")
    chat.messages.insert(0, Message(role="system", content="SYSTEM-PROMPT"))

    decision = strategy_over(SpyStore(core=[core], selected=[picked])).build(
        chat.messages, context_window=4096, conversation=chat
    )

    blob = [m.content for m in decision.messages]
    positions = [next(i for i, c in enumerate(blob) if needle in c) for needle in (
        "SYSTEM-PROMPT", "CORE-FRAGMENT", "older question", "SELECTED-FRAGMENT",
        "the latest question",
    )]
    assert positions == sorted(positions)
    assert blob[-1] == "the latest question", "the latest turn stays last"


def test_recalled_carries_exactly_the_query_selected_block():
    picked = make_fragment(text="SELECTED-FRAGMENT")
    core = make_fragment(text="CORE-FRAGMENT")
    chat = conversation_of("the latest question")

    decision = strategy_over(SpyStore(core=[core], selected=[picked])).build(
        chat.messages, context_window=4096, conversation=chat
    )

    assert [f.text for f in decision.recalled] == ["SELECTED-FRAGMENT"]
    assert [f.text for f in decision.core] == ["CORE-FRAGMENT"]
    spliced = "\n".join(m.content for m in decision.messages)
    assert spliced.count("SELECTED-FRAGMENT") == 1
    assert spliced.count("CORE-FRAGMENT") == 1


def test_an_empty_recall_splices_no_block_at_all():
    """§ 8.1 renders `▸ 0` from `recalled`; the prompt itself must not carry an
    empty header for the model to interpret."""
    chat = conversation_of("the latest question")

    decision = strategy_over(SpyStore()).build(
        chat.messages, context_window=4096, conversation=chat
    )

    assert decision.recalled == [] and decision.core == []
    assert [m.content for m in decision.messages] == ["the latest question"]


def test_the_query_is_the_latest_turn():
    store = SpyStore()
    chat = conversation_of("first question", "an answer", "what did we pick for storage?")

    strategy_over(store).build(chat.messages, context_window=4096, conversation=chat)

    assert store.query == "what did we pick for storage?"


def test_the_budgets_are_fractions_of_the_context_window():
    store = SpyStore()
    chat = conversation_of("the latest question")

    strategy_over(store).build(chat.messages, context_window=8192, conversation=chat)

    assert store.core_budget == int(8192 * 0.10)
    assert store.sel_budget == int(8192 * 0.10)

    smaller = SpyStore()
    strategy_over(smaller).build(chat.messages, context_window=2048, conversation=chat)

    assert smaller.core_budget == int(2048 * 0.10)


def test_a_conversation_the_strategy_is_not_given_is_chat_local():
    """`ContextStrategy.build` carries only messages; without the conversation
    there is no group to scope memory to, and the answer is the inner
    strategy's."""
    store = SpyStore()
    chat = conversation_of("the latest question")

    decision = strategy_over(store).build(chat.messages, context_window=4096)

    assert decision.recalled == []
    assert store.query is None


def test_the_recency_window_strategy_ignores_the_conversation_keyword():
    """The § 1.3 fold-back: the protocol carries `conversation`, and the
    strategy that has no use for it still accepts it."""
    from agentchat.core.context import RecencyWindowStrategy

    chat = conversation_of("hello")

    decision = RecencyWindowStrategy().build(
        chat.messages, context_window=4096, conversation=chat
    )

    assert [m.content for m in decision.messages] == ["hello"]


# -- the property: assembly never exceeds the window ----------------------


def a_long_conversation(seed: int = 7, turns: int = 60):
    from agentchat.core.models import Message

    rng = random.Random(seed)
    chat = conversation_of()
    chat.add(Message(role="system", content="You are a terse assistant."))
    for turn in range(turns):
        role = "user" if turn % 2 == 0 else "assistant"
        chat.add(Message(role=role, content=" ".join(["word"] * rng.randint(3, 90))))
    chat.add(Message(role="user", content="and what did we decide about storage?"))
    return chat


def memory_store_with(n: int = 8):
    fragments = [make_fragment(text=f"Fragment {i}: " + "detail " * 12) for i in range(n)]
    return SpyStore(core=fragments[: n // 2], selected=fragments[n // 2 :])


def test_the_assembled_context_never_exceeds_the_window():
    from agentchat.core.tokens import estimate_tokens

    chat = a_long_conversation()

    for window in (32768, 8192, 4096, 2048, 1024, 512, 256):
        decision = strategy_over(memory_store_with()).build(
            chat.messages, context_window=window, conversation=chat
        )
        spent = sum(estimate_tokens(m.content) for m in decision.messages)

        assert spent <= decision.budget, f"window {window}"
        assert decision.estimated_tokens == spent
        assert decision.budget <= window
        assert decision.messages[-1].content.endswith("about storage?"), (
            f"window {window} dropped the turn being answered"
        )


def test_switching_to_a_smaller_model_shrinks_the_assembly_to_fit():
    """NFR-CTX-04, the reason § 9's budgets are fractions: the same
    conversation, assembled again against a smaller window."""
    from agentchat.core.tokens import estimate_tokens

    chat = a_long_conversation()
    strategy = strategy_over(memory_store_with())

    large = strategy.build(chat.messages, context_window=32768, conversation=chat)
    small = strategy.build(chat.messages, context_window=2048, conversation=chat)

    assert sum(estimate_tokens(m.content) for m in small.messages) <= small.budget
    assert small.estimated_tokens < large.estimated_tokens
    assert len(small.dropped) > len(large.dropped)
