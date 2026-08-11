"""Stage 2 acceptance: turns become fragments, durably and idempotently.

Detail plan: ``docs/plans/2026-08-11-005-memory-stage-2-extraction-write-path.md``.
The invariants themselves (I-2 write side, I-5, I-12) live in
``test_invariants.py``; this file covers the rest of the write path — the wire
format and its parsers, the four outcomes, `candidates()`, the chat-side
adapters, and the batching/flush scheduling.

Scripted replies stand in for the model everywhere (skeleton rule 7): these
tests assert on what reaches `apply()`, never on output quality. The one place
a real model is exercised is `test_extraction_real_model.py`, which is skipped
unless `AGENTCHAT_TEST_REAL_MODEL=1`.

Store reads go through raw SQL, as in `test_memory_store.py`: asserting through
the store that did the writing lets a bug in the store hide itself. `agentchat`
imports sit inside the test bodies for the same reason they do in
`test_invariants.py` — the modules under test do not exist yet, and a
module-level import would fail collection for the whole file.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import fast_registry
from factories import GraphBuilder, make_group, write
from test_invariants import ONE_CLAIM, ONE_DECISION, ScriptedProvider

# -- the wire format, in one place ---------------------------------------


def reply(*objects) -> str:
    """A model reply carrying `objects` as its JSON array."""
    return json.dumps(list(objects))


def a_claim(
    text: str = "The project stores conversations in SQLite.",
    *,
    kind: str = "decision",
    importance: int = 7,
    confidence: float = 0.8,
    evidence: Sequence[int] = (1,),
    **extra,
) -> dict:
    return {
        "text": text,
        "kind": kind,
        "importance": importance,
        "confidence": confidence,
        "evidence": list(evidence),
        **extra,
    }


def a_decision(
    claim: int = 1,
    *,
    relation: str = "unrelated",
    candidate: int | None = None,
    **extra,
) -> dict:
    body: dict = {"claim": claim, "relation": relation, **extra}
    if candidate is not None:
        body["candidate"] = candidate
    return body


def a_revision(text: str = "The project stores conversations in SQLite (WAL).", confidence: float = 0.6) -> str:
    return json.dumps({"text": text, "confidence": confidence})


# -- doubles --------------------------------------------------------------


class RecordingProvider(ScriptedProvider):
    """`ScriptedProvider` that also keeps what it was asked, so a test can
    check the prompt and the generation options without asserting on output."""

    def __init__(self, *replies: str, error: Exception | None = None) -> None:
        super().__init__(*replies, error=error)
        self.prompts: list[list] = []
        self.options: list = []

    async def generate(self, messages: Sequence[object], options: object = None) -> AsyncIterator[str]:
        self.prompts.append(list(messages))
        self.options.append(options)
        async for chunk in super().generate(messages, options):
            yield chunk

    def prompt_text(self, call: int = 0) -> str:
        return "\n".join(getattr(turn, "content", "") for turn in self.prompts[call])


class BlockingProvider:
    """Never returns from `generate`. The seam for cancelling a run while the
    model is mid-answer, which is the only moment `apply()` has not run yet."""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def load(self) -> None:
        return None

    async def unload(self) -> None:
        return None

    async def generate(self, messages: Sequence[object], options: object = None) -> AsyncIterator[str]:
        self.started.set()
        await asyncio.Event().wait()
        yield ""  # pragma: no cover — unreachable, keeps this an async generator


async def wait_until_generating(provider: BlockingProvider, task: asyncio.Task) -> None:
    """Wait for `task` to reach the model. If the run dies on the way there,
    surface *that* error rather than a bare timeout five seconds later."""
    waiter = asyncio.create_task(provider.started.wait())
    done, _ = await asyncio.wait({task, waiter}, timeout=5, return_when=asyncio.FIRST_COMPLETED)
    waiter.cancel()
    if task in done:
        await task
    assert done, "extraction never reached the model"


def supplier(provider):
    """The `provider_for` seam `ChatMemory` takes: in the app it is
    `ModelRegistry.active_provider`, so extraction rides the resident model."""

    async def _provider():
        return provider

    return _provider


# -- fixtures -------------------------------------------------------------


def fresh(tmp_path: Path):
    from agentchat.storage.memory import SqliteMemoryStore

    path = tmp_path / "chat.db"
    return path, SqliteMemoryStore(path)


def seeded(tmp_path: Path, *contents: str, kind: str = "project"):
    """A project group holding one conversation of `contents`, on disk."""
    path, store = fresh(tmp_path)
    builder = GraphBuilder(make_group(kind=kind))
    chat = builder.conversation()
    for content in contents:
        builder.message(chat, content=content)
    write(store, builder.build())
    return path, store, builder, chat


def rows(path: Path, sql: str, *params) -> list[tuple]:
    with sqlite3.connect(path) as conn:
        return conn.execute(sql, params).fetchall()


def one(path: Path, sql: str, *params) -> tuple | None:
    found = rows(path, sql, *params)
    return found[0] if found else None


def snapshot(path: Path) -> dict:
    """Everything the idempotence tests must find unchanged."""
    return {
        "fragments": rows(
            path, "SELECT id, text, kind, confidence, importance FROM memory_fragments ORDER BY id"
        ),
        "citations": rows(
            path,
            "SELECT fragment_id, source_message_id, source_fragment_id, quote"
            " FROM fragment_citations ORDER BY id",
        ),
        "watermarks": rows(path, "SELECT id, extracted_at, extracted_id FROM conversations ORDER BY id"),
    }


# -- the wire format: claims ---------------------------------------------


def test_claims_survive_a_thinking_block_a_code_fence_and_prose():
    """A local model wraps its JSON in whatever it likes. The parser has to
    find the array anyway, or every reply from a thinking model is lost."""
    from agentchat.core.memory.extract import parse_claims
    from agentchat.core.memory.strategy import MessageEvidence

    from factories import make_message

    items = [MessageEvidence(make_message(content="We chose SQLite."))]
    raw = (
        "<think>The user states a storage decision. That is durable.</think>\n"
        "Here is what I found:\n"
        "```json\n" + reply(a_claim()) + "\n```\n"
        "Let me know if you need more."
    )

    claims = parse_claims(raw, items)

    assert [c.text for c in claims] == ["The project stores conversations in SQLite."]
    assert claims[0].kind == "decision"
    assert claims[0].importance == 7
    assert claims[0].confidence == 0.8
    assert [item.id for item in claims[0].evidence] == [items[0].id]


def test_an_unparseable_reply_yields_no_claims():
    """Garbage is 'nothing worth remembering', not an exception: the run still
    has to commit its watermark or the batch is re-read forever."""
    from agentchat.core.memory.extract import parse_claims
    from agentchat.core.memory.strategy import MessageEvidence

    from factories import make_message

    items = [MessageEvidence(make_message(content="We chose SQLite."))]

    assert parse_claims("I could not find anything.", items) == []
    assert parse_claims("", items) == []
    assert parse_claims('[{"text": "unterminated', items) == []


def test_a_claim_with_no_usable_evidence_index_is_dropped():
    """I-4 at the write side: a fragment with zero citations must not exist,
    so a claim that cites nothing real never becomes one."""
    from agentchat.core.memory.extract import parse_claims
    from agentchat.core.memory.strategy import MessageEvidence

    from factories import make_message

    items = [MessageEvidence(make_message(content="We chose SQLite."))]

    dropped = parse_claims(reply(a_claim(evidence=[4, 9])), items)
    kept = parse_claims(reply(a_claim(evidence=[1, 9])), items)

    assert dropped == []
    assert [item.id for item in kept[0].evidence] == [items[0].id]


def test_an_unknown_kind_survives_parsing():
    """Rule 4: `kind` is open, so a persona kind reaches the store unmangled
    and `Tuning.alpha_for` does the falling back."""
    from agentchat.core.memory.extract import parse_claims
    from agentchat.core.memory.strategy import MessageEvidence

    from factories import make_message

    items = [MessageEvidence(make_message(content="We chose SQLite."))]

    claims = parse_claims(reply(a_claim(kind="preference")), items)

    assert claims[0].kind == "preference"


def test_a_reasoning_trace_is_kept_only_when_thinking_is_on():
    from agentchat.core.memory.extract import parse_claims
    from agentchat.core.memory.strategy import MessageEvidence

    from factories import make_message

    items = [MessageEvidence(make_message(content="We chose SQLite."))]
    raw = "<think>storage decisions outlive the chat</think>" + reply(
        a_claim(why="it is a storage decision")
    )

    on = parse_claims(raw, items, thinking=True)
    off = parse_claims(raw, items, thinking=False)

    assert on[0].reasoning == "it is a storage decision"
    assert off[0].reasoning is None


def test_the_shared_thinking_block_is_the_fallback_trace():
    """`phi-4-mini` has no native reasoning mode and answers the prompted
    instruction in prose; `qwen3-14b` emits `<think>`. Either is a trace."""
    from agentchat.core.memory.extract import parse_claims
    from agentchat.core.memory.strategy import MessageEvidence

    from factories import make_message

    items = [MessageEvidence(make_message(content="We chose SQLite."))]
    raw = "<think>storage decisions outlive the chat</think>" + reply(a_claim())

    claims = parse_claims(raw, items, thinking=True)

    assert claims[0].reasoning == "storage decisions outlive the chat"


# -- the wire format: decisions ------------------------------------------


def a_candidate(text: str = "The project stores conversations in SQLite.", **overrides):
    from agentchat.core.memory.extract import Candidate
    from agentchat.core.memory.models import FragmentSupport

    from factories import make_fragment

    support = FragmentSupport(
        citation_count=overrides.pop("citation_count", 2),
        conversation_count=overrides.pop("conversation_count", 1),
        first_seen_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        last_seen_at=datetime(2026, 8, 9, tzinfo=timezone.utc),
    )
    overrides.setdefault("id", 1)
    return Candidate(fragment=make_fragment(text=text, **overrides), support=support)


def parsed_claim(text: str = "The project stores conversations in SQLite."):
    from agentchat.core.memory.extract import parse_claims
    from agentchat.core.memory.strategy import MessageEvidence

    from factories import make_message

    items = [MessageEvidence(make_message(content="We chose SQLite."))]
    return parse_claims(reply(a_claim(text=text)), items)[0]


def test_the_three_reranker_labels_map_to_the_three_write_outcomes():
    from agentchat.core.memory.extract import Outcome, parse_decisions

    claims = [parsed_claim("a"), parsed_claim("b"), parsed_claim("c")]
    candidates = [a_candidate()]
    raw = reply(
        a_decision(1, relation="unrelated"),
        a_decision(2, relation="identical", candidate=1),
        a_decision(3, relation="similar", candidate=1),
    )

    decisions = parse_decisions(raw, claims, candidates)

    assert [d.outcome for d in decisions] == [Outcome.NEW, Outcome.REINFORCE, Outcome.REVISE]
    assert decisions[0].candidate is None
    assert decisions[1].candidate is candidates[0]


def test_ignore_resolves_to_the_ignore_outcome():
    from agentchat.core.memory.extract import Outcome, parse_decisions

    claims = [parsed_claim()]

    decisions = parse_decisions(reply(a_decision(1, relation="ignore")), claims, [a_candidate()])

    assert [d.outcome for d in decisions] == [Outcome.IGNORE]


def test_an_unclear_relation_routes_to_new():
    """§ 2.1: the unclear edge routes to `new`, because a duplicate is the
    cheapest mistake the four outcomes can make."""
    from agentchat.core.memory.extract import Outcome, parse_decisions

    claims = [parsed_claim("a"), parsed_claim("b")]
    candidates = [a_candidate()]
    raw = reply(a_decision(1, relation="probably related?"), a_decision(2, relation=None))

    decisions = parse_decisions(raw, claims, candidates)

    assert [d.outcome for d in decisions] == [Outcome.NEW, Outcome.NEW]


def test_a_claim_the_resolver_never_mentions_routes_to_new():
    from agentchat.core.memory.extract import Outcome, parse_decisions

    claims = [parsed_claim("a"), parsed_claim("b")]

    decisions = parse_decisions(reply(a_decision(1, relation="identical", candidate=1)), claims, [a_candidate()])

    assert [d.outcome for d in decisions] == [Outcome.REINFORCE, Outcome.NEW]


def test_a_candidate_index_that_does_not_resolve_routes_to_new():
    """`identical` with nothing to be identical to is unclear, not a crash."""
    from agentchat.core.memory.extract import Outcome, parse_decisions

    claims = [parsed_claim("a"), parsed_claim("b")]
    candidates = [a_candidate()]
    raw = reply(a_decision(1, relation="identical", candidate=7), a_decision(2, relation="similar"))

    decisions = parse_decisions(raw, claims, candidates)

    assert [d.outcome for d in decisions] == [Outcome.NEW, Outcome.NEW]


def test_only_the_first_resolution_of_a_claim_counts():
    """§ 2.1's one surviving rule: at most one resolution per claim."""
    from agentchat.core.memory.extract import Outcome, parse_decisions

    claims = [parsed_claim()]
    candidates = [a_candidate(), a_candidate(text="WAL, single writer.", id=2)]
    raw = reply(
        a_decision(1, relation="identical", candidate=1),
        a_decision(1, relation="similar", candidate=2),
    )

    decisions = parse_decisions(raw, claims, candidates)

    assert [d.outcome for d in decisions] == [Outcome.REINFORCE]
    assert decisions[0].candidate is candidates[0]


def test_an_unparseable_revision_falls_back_to_a_new_fragment():
    """A rewrite that cannot be read must not blend two claims into one text;
    the cheapest recovery is the duplicate the design already tolerates."""
    from agentchat.core.memory.extract import parse_revision

    assert parse_revision("I have rewritten it for you.") is None
    assert parse_revision(a_revision()) == ("The project stores conversations in SQLite (WAL).", 0.6)


# -- prompts --------------------------------------------------------------


def test_the_candidate_block_carries_support_counts_and_no_quotes(tmp_path: Path):
    """§ 2.1: 'Counts, not rows.' k candidates × several citations × a quote
    each is a large share of the prompt, and the signal worth having compresses
    to three numbers."""
    from agentchat.core.memory.extract import Candidate, render_candidates

    from factories import make_citation

    path, store, builder, chat = seeded(tmp_path, "We chose SQLite.", "WAL, single writer.")
    fragment = builder.extracted(
        builder.messages[0], text="The project stores conversations in SQLite."
    )
    builder.citations.append(
        make_citation(fragment, message=builder.messages[1], quote="WAL, single writer.")
    )
    write(store, builder.build())

    found = store.candidates(builder.group.id, "sqlite storage", 10)
    block = render_candidates([Candidate(fragment=f, support=f.support) for f in found])

    assert "The project stores conversations in SQLite." in block
    assert "WAL, single writer." not in block, "citation quotes must not reach the prompt"
    assert "2" in block, "citation_count"
    assert "0.5" in block, "confidence"


# -- the four outcomes, through the store --------------------------------


async def run_extraction(store, chat, provider, **kwargs):
    """One extraction run through the chat-side adapter — the same call the
    scheduler makes, so the watermark is written back onto `chat` too."""
    from agentchat.core.memory.strategy import ChatMemory

    return await ChatMemory(store, supplier(provider)).extract(chat, **kwargs)


async def test_a_new_claim_lands_with_one_citation_per_evidence_turn(tmp_path: Path):
    path, store, builder, chat = seeded(tmp_path, "We chose SQLite.", "WAL, single writer.")
    provider = ScriptedProvider(reply(a_claim(evidence=[1, 2])), ONE_DECISION)

    result = await run_extraction(store, chat, provider)

    fragment = one(path, "SELECT id, text, kind, confidence, importance FROM memory_fragments")
    assert fragment[1] == "The project stores conversations in SQLite."
    assert (fragment[2], fragment[3], fragment[4]) == ("decision", 0.8, 7.0)
    cited = {row[0] for row in rows(path, "SELECT source_message_id FROM fragment_citations")}
    assert cited == {m.id for m in chat.messages}
    assert result.inserted == [fragment[0]]
    assert one(path, "SELECT extracted_id FROM conversations WHERE id = ?", chat.id) == (
        chat.messages[-1].id,
    )


async def test_an_identical_claim_reinforces_without_rewriting_the_text(tmp_path: Path):
    """§ 2.1: reinforcement touches confidence and citations, never text —
    repeated rewrites of one sentence drift semantically."""
    from agentchat.core.memory.tuning import Tuning

    path, store, builder, chat = seeded(tmp_path, "We chose SQLite.", "Still SQLite, yes.")
    existing = builder.extracted(
        builder.messages[0], text="The project stores conversations in SQLite.", confidence=0.5
    )
    write(store, builder.build())

    provider = ScriptedProvider(
        reply(a_claim(text="Storage is SQLite.", evidence=[2])),
        reply(a_decision(1, relation="identical", candidate=1)),
    )
    result = await run_extraction(store, chat, provider)

    text, confidence = one(path, "SELECT text, confidence FROM memory_fragments WHERE id = ?", existing.id)
    assert text == "The project stores conversations in SQLite."
    assert confidence == pytest.approx(0.5 + Tuning().reinforce_step)
    assert rows(path, "SELECT COUNT(*) FROM memory_fragments") == [(1,)]
    assert [c.fragment_id for c in result.confidence_changes] == [existing.id]


async def test_a_similar_claim_rewrites_the_text_and_its_confidence(tmp_path: Path):
    path, store, builder, chat = seeded(tmp_path, "We chose SQLite.", "Actually, WAL matters.")
    existing = builder.extracted(
        builder.messages[0], text="The project stores conversations in SQLite.", confidence=0.5
    )
    write(store, builder.build())

    provider = ScriptedProvider(
        reply(a_claim(text="SQLite runs in WAL mode.", evidence=[2])),
        reply(a_decision(1, relation="similar", candidate=1)),
        a_revision(),
    )
    result = await run_extraction(store, chat, provider)

    text, confidence, revised_at = one(
        path, "SELECT text, confidence, revised_at FROM memory_fragments WHERE id = ?", existing.id
    )
    assert text == "The project stores conversations in SQLite (WAL)."
    assert confidence == 0.6
    assert revised_at is not None
    assert provider.calls == 3, "only the revise branch pays a second resolution call"
    assert result.revised == [existing.id]


async def test_a_batch_with_nothing_worth_remembering_still_advances_the_watermark(tmp_path: Path):
    """The common case. § 2.1's empty batch: no row is written, and the range
    must not be re-read forever."""
    path, store, builder, chat = seeded(tmp_path, "Morning.", "Morning!")

    result = await run_extraction(store, chat, ScriptedProvider("[]"))

    assert rows(path, "SELECT COUNT(*) FROM memory_fragments") == [(0,)]
    assert result.inserted == [] and result.citations_added == 0
    assert one(path, "SELECT extracted_id FROM conversations WHERE id = ?", chat.id) == (
        chat.messages[-1].id,
    )


# -- idempotence ----------------------------------------------------------


async def test_a_run_cancelled_before_apply_commits_nothing_and_the_retry_is_clean(tmp_path: Path):
    """The gate's kill-before-`apply()` case: one short transaction with the
    LLM outside it means a killed run leaves no partial state at all."""
    path, store, builder, chat = seeded(tmp_path, "We chose SQLite.", "WAL, single writer.")
    blocked = BlockingProvider()

    task = asyncio.create_task(run_extraction(store, chat, blocked))
    await wait_until_generating(blocked, task)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert rows(path, "SELECT COUNT(*) FROM memory_fragments") == [(0,)]
    assert rows(path, "SELECT COUNT(*) FROM fragment_citations") == [(0,)]
    assert one(path, "SELECT extracted_at, extracted_id FROM conversations WHERE id = ?", chat.id) == (
        None,
        None,
    )

    await run_extraction(store, chat, ScriptedProvider(reply(a_claim(evidence=[1, 2])), ONE_DECISION))

    assert rows(path, "SELECT COUNT(*) FROM memory_fragments") == [(1,)]
    assert rows(path, "SELECT COUNT(*) FROM fragment_citations") == [(2,)]


async def test_re_running_an_extracted_conversation_changes_nothing(tmp_path: Path):
    """Kill-after-`apply()`: the watermark moved with the writes, so the retry
    reads an empty range and no second LLM call is even made."""
    path, store, builder, chat = seeded(tmp_path, "We chose SQLite.", "WAL, single writer.")

    await run_extraction(store, chat, ScriptedProvider(reply(a_claim(evidence=[1, 2])), ONE_DECISION))
    before = snapshot(path)

    # The adapter wrote the watermark back onto the live conversation, so the
    # retry sees an empty range without reloading it from the store.
    assert (chat.extracted_at, chat.extracted_id) == (
        chat.messages[-1].created_at,
        chat.messages[-1].id,
    )
    second = ScriptedProvider(reply(a_claim(evidence=[1, 2])), ONE_DECISION)
    assert await run_extraction(store, chat, second) is None
    assert second.calls == 0, "an empty range must not reach the model"
    assert snapshot(path) == before


async def test_an_overlapping_backfill_double_counts_no_confidence(tmp_path: Path):
    """§ 2.1's harder failure: two runs that both succeed over evidence counted
    twice. No transaction discipline helps — only tying the raise to a citation
    that was genuinely new."""
    path, store, builder, chat = seeded(tmp_path, "We chose SQLite.", "WAL, single writer.")

    await run_extraction(store, chat, ScriptedProvider(reply(a_claim(evidence=[1, 2])), ONE_DECISION))
    before = snapshot(path)

    # The backfill trigger: the same range again, ignoring the watermark. The
    # claim now matches the fragment the first run wrote.
    backfill = ScriptedProvider(
        reply(a_claim(evidence=[1, 2])),
        reply(a_decision(1, relation="identical", candidate=1)),
    )
    await run_extraction(store, chat, backfill, ignore_watermark=True)

    assert snapshot(path) == before
    assert rows(path, "SELECT COUNT(*) FROM memory_fragments") == [(1,)]


# -- reasoning traces -----------------------------------------------------


async def test_reasoning_is_stored_when_thinking_is_on_and_null_when_off(tmp_path: Path):
    path, store, builder, chat = seeded(tmp_path, "We chose SQLite.")
    thinking_reply = reply(a_claim(why="a storage decision outlives the chat"))

    await run_extraction(
        store, chat, ScriptedProvider(thinking_reply, ONE_DECISION), thinking=True
    )
    assert one(path, "SELECT reasoning FROM memory_fragments") == (
        "a storage decision outlives the chat",
    )

    other_path, other_store, other_builder, other_chat = seeded(
        tmp_path / "off", "We chose SQLite."
    )
    await run_extraction(
        other_store, other_chat, ScriptedProvider(thinking_reply, ONE_DECISION), thinking=False
    )
    assert one(other_path, "SELECT reasoning FROM memory_fragments") == (None,)


async def test_extraction_asks_the_provider_for_the_thinking_mode_it_was_given(tmp_path: Path):
    """§ 2.1 reuses the generation path's own mechanism — native for
    `qwen3-14b`, prompted for `phi-4-mini` — rather than inventing a second."""
    path, store, builder, chat = seeded(tmp_path, "We chose SQLite.")
    provider = RecordingProvider(reply(a_claim()), ONE_DECISION)

    await run_extraction(store, chat, provider, thinking=True)

    assert all(options.thinking for options in provider.options)
    assert "self-contained" in provider.prompt_text().lower()


# -- candidates() ---------------------------------------------------------


def test_candidates_are_scoped_to_their_group_and_ranked_by_bm25(tmp_path: Path):
    path, store = fresh(tmp_path)

    mine = GraphBuilder()
    chat = mine.conversation()
    message = mine.message(chat, content="We chose SQLite.")
    mine.extracted(message, text="The project stores conversations in SQLite.")
    mine.extracted(message, text="The conversation picker is a modal.")
    write(store, mine.build())

    theirs = GraphBuilder()
    other_chat = theirs.conversation()
    # Explicit ids: two GraphBuilders both mint from 1, and identical ids in
    # one database are one fragment.
    theirs.extracted(
        theirs.message(other_chat, content="SQLite there too."),
        id=10,
        text="The other group also stores conversations in SQLite.",
    )
    write(store, theirs.build())

    found = store.candidates(mine.group.id, "which database stores conversations", 10)

    assert [f.group_id for f in found] == [mine.group.id] * len(found)
    assert found[0].text == "The project stores conversations in SQLite."


def test_candidates_carry_support_counts(tmp_path: Path):
    """The extractor resolves against 'how well established is this already',
    which is three numbers from the § 1.1 view."""
    path, store, builder, chat = seeded(tmp_path, "We chose SQLite.")
    second = builder.conversation(title="second")
    builder.message(second, content="Still SQLite.")
    fragment = builder.extracted(
        builder.messages[0], builder.messages[1], text="The project stores conversations in SQLite."
    )
    write(store, builder.build())

    found = store.candidates(builder.group.id, "sqlite", 10)

    assert [f.id for f in found] == [fragment.id]
    assert found[0].support.citation_count == 2
    assert found[0].support.conversation_count == 2
    assert found[0].support.last_seen_at is not None


def test_candidates_include_dormant_fragments_and_skip_the_consolidated_tier(tmp_path: Path):
    """I-13's write half: `candidates()` carries no confidence predicate, or
    dormancy is irreversible. The consolidated layer is § 2.6's to write, and
    a raw claim must not revise a compressed insight."""
    path, store, builder, chat = seeded(tmp_path, "We chose SQLite.")
    dormant = builder.extracted(
        builder.messages[0], text="Postgres was ruled out for this project.", confidence=0.0
    )
    insight = builder.consolidated(dormant, text="Storage for the project is settled.")
    write(store, builder.build())

    found = [f.id for f in store.candidates(builder.group.id, "postgres project storage settled", 10)]

    assert dormant.id in found
    assert insight.id not in found


def test_candidates_tolerate_fts_syntax_in_the_query(tmp_path: Path):
    """The query is a user's own words routed into an FTS5 MATCH; unescaped,
    a stray quote or `NEAR` is a syntax error on the reply path."""
    path, store, builder, chat = seeded(tmp_path, "We chose SQLite.")
    builder.extracted(builder.messages[0], text="The project stores conversations in SQLite.")
    write(store, builder.build())

    assert store.candidates(builder.group.id, 'why "SQLite" AND NOT postgres NEAR* (x', 10) != []
    assert store.candidates(builder.group.id, "!!! ???", 10) == []


# -- the chat-side adapters ----------------------------------------------


def test_message_evidence_renders_the_role_with_the_content():
    """`EvidenceItem` has no role: an email's sender and a chat turn's role are
    the same slot, and rendering them into `text` is the adapter's job."""
    from agentchat.core.memory.strategy import MessageEvidence

    from factories import make_message

    message = make_message(role="assistant", content="Try Kyoto.")
    item = MessageEvidence(message)

    assert item.id == message.id
    assert item.created_at == message.created_at
    assert "Try Kyoto." in item.text
    assert "assistant" in item.text


def test_conversation_evidence_starts_after_the_watermark():
    from agentchat.core.memory.strategy import ConversationEvidence

    from factories import make_conversation, make_message

    base = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    messages = [
        make_message(content=f"turn {n}", created_at=base + timedelta(minutes=n)) for n in range(4)
    ]
    conversation = make_conversation(messages=messages)
    conversation.extracted_at = messages[1].created_at
    conversation.extracted_id = messages[1].id

    source = ConversationEvidence(conversation)

    assert [item.id for item in source.items] == [messages[2].id, messages[3].id]
    assert source.read_through == (messages[3].created_at, messages[3].id)
    assert source.scope_id == conversation.group_id
    assert ConversationEvidence(conversation, ignore_watermark=True).items[0].id == messages[0].id


def test_observe_drops_empty_turns_but_the_watermark_still_covers_them():
    """A cancelled reply leaves an empty assistant turn. It is not evidence,
    and it must still fall behind the watermark or it is read forever."""
    from agentchat.core.memory.extract import MemoryExtractor
    from agentchat.core.memory.strategy import ConversationEvidence

    from factories import make_conversation, make_message

    base = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    messages = [
        make_message(content="We chose SQLite.", created_at=base),
        make_message(role="assistant", content="   ", created_at=base + timedelta(minutes=1)),
    ]
    source = ConversationEvidence(make_conversation(messages=messages))

    kept = MemoryExtractor.observe(source.items)

    assert [item.id for item in kept] == [messages[0].id]
    assert source.read_through == (messages[1].created_at, messages[1].id)


# -- batching, flush, cancellation ---------------------------------------


async def chat_service(tmp_path: Path, provider, *, extract_every: int = 4):
    from agentchat.core.chat import ChatService
    from agentchat.core.memory.strategy import ChatMemory
    from agentchat.core.memory.tuning import Tuning
    from agentchat.storage.memory import SqliteMemoryStore
    from agentchat.storage.sqlite import SqliteStore

    path = tmp_path / "chat.db"
    conversations = SqliteStore(path)
    group = make_group(kind="project")
    await conversations.save_group(group)

    memory = ChatMemory(
        SqliteMemoryStore(path), supplier(provider), tuning=Tuning(extract_every=extract_every)
    )
    service = ChatService(fast_registry(), store=conversations, memory=memory)
    return path, service, await service.new_conversation(group.id)


async def turn(service, conversation, text: str = "hello") -> None:
    async for _ in service.stream_reply(conversation, text):
        pass


async def test_extraction_waits_until_extract_every_messages_are_pending(tmp_path: Path):
    """Batched, not per-turn: per-turn extraction pays an LLM call for turns
    that usually carry no durable claim."""
    provider = ScriptedProvider(reply(a_claim()), ONE_DECISION)
    path, service, conversation = await chat_service(tmp_path, provider, extract_every=4)

    await turn(service, conversation)  # two messages: user + assistant
    await service.wait_for_extraction()

    assert provider.calls == 0
    assert one(path, "SELECT extracted_id FROM conversations WHERE id = ?", conversation.id) == (None,)

    await turn(service, conversation)
    await service.wait_for_extraction()

    assert provider.calls > 0
    assert one(path, "SELECT extracted_id FROM conversations WHERE id = ?", conversation.id) == (
        conversation.messages[-1].id,
    )


async def test_a_flush_extracts_a_partial_batch(tmp_path: Path):
    """What conversation switch and app close call: without it the last N-1
    turns before you navigate away wait indefinitely."""
    provider = ScriptedProvider(reply(a_claim()), ONE_DECISION)
    path, service, conversation = await chat_service(tmp_path, provider, extract_every=4)

    await turn(service, conversation)
    await service.wait_for_extraction()
    assert provider.calls == 0

    await service.flush_extraction(conversation)

    assert one(path, "SELECT extracted_id FROM conversations WHERE id = ?", conversation.id) == (
        conversation.messages[-1].id,
    )
    assert rows(path, "SELECT COUNT(*) FROM memory_fragments") == [(1,)]


async def test_a_flush_with_nothing_pending_does_not_reach_the_model(tmp_path: Path):
    provider = ScriptedProvider(reply(a_claim()), ONE_DECISION)
    path, service, conversation = await chat_service(tmp_path, provider)

    assert await service.flush_extraction(conversation) is None
    assert provider.calls == 0


async def test_extraction_is_cancellable(tmp_path: Path):
    """NFR-U-04: fire-and-forget, off the reply path, and stoppable — a run
    that never returns must not keep the app from closing."""
    blocked = BlockingProvider()
    path, service, conversation = await chat_service(tmp_path, blocked, extract_every=2)

    await turn(service, conversation)
    # The scheduler swallows a failed run by design, so a timeout here is the
    # only signal that the worker died before reaching the model.
    await asyncio.wait_for(blocked.started.wait(), timeout=5)

    service.cancel_extraction()
    await service.wait_for_extraction()

    assert one(path, "SELECT extracted_id FROM conversations WHERE id = ?", conversation.id) == (None,)


async def test_backfill_re_reads_a_conversation_that_is_already_watermarked(tmp_path: Path):
    """Backfill is the same path with a null watermark — a trigger, not a
    mechanism. Stage 6 gives it a button."""
    provider = ScriptedProvider(reply(a_claim()), ONE_DECISION)
    path, service, conversation = await chat_service(tmp_path, provider, extract_every=2)

    await turn(service, conversation)
    await service.wait_for_extraction()
    assert rows(path, "SELECT COUNT(*) FROM memory_fragments") == [(1,)]

    from agentchat.core.memory.strategy import ChatMemory
    from agentchat.storage.memory import SqliteMemoryStore

    reinforcing = ScriptedProvider(
        reply(a_claim()), reply(a_decision(1, relation="identical", candidate=1))
    )
    service.memory = ChatMemory(SqliteMemoryStore(path), supplier(reinforcing))
    await service.backfill(conversation)

    assert reinforcing.calls > 0
    assert rows(path, "SELECT COUNT(*) FROM memory_fragments") == [(1,)]
