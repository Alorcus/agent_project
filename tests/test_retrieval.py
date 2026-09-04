"""Adaptive retrieval: the gate/rewrite/judge/reseed prompts and parsers, the
two indexes — group-scoped facts and the global document corpus — and the
bounded loop with its three exits."""

from __future__ import annotations

import pytest

from agentchat.core.models import Author, Conversation, Fact, Message, Phrase
from agentchat.core.prompts import (
    RECALL_HEDGE,
    parse_gate,
    parse_query,
    parse_seeds,
    parse_verdict,
    injected_text,
    recall_block,
    render_digest,
    render_documents,
    render_facts,
)
from agentchat.core.retrieval import (
    AdaptiveRetriever,
    FactIndex,
    Hit,
    Recall,
    SnippetHit,
    SnippetIndex,
    assemble,
    cap_per_document,
    snippet_text,
    view_text,
)
from agentchat.llm.embedding import HashingEmbedder
from agentchat.llm.registry import ModelRegistry
from factories import (
    GOLDEN_DOCUMENTS,
    GOLDEN_FACTS,
    make_conversation,
    make_document,
    make_group,
    make_indexed_corpus,
    make_indexed_group,
    make_snippet,
    scripted_provider,
)


def _registry_with(provider) -> ModelRegistry:
    registry = ModelRegistry()
    registry.register(provider.info, lambda: provider)
    return registry


def _hit(text: str, view: str = "claim", score: float = 0.9, quote: str = "q") -> Hit:
    phrases = (
        (Phrase(message_id="m", start=0, end=1, author=Author("user", "user"), quote=quote),)
        if quote
        else ()
    )
    return Hit(fact=Fact(text=text, phrases=phrases), view=view, score=score)


def _snippet_hit(text: str, *, title: str = "handbook.pdf", page=None, score=0.9,
                 document_id: str = "doc") -> SnippetHit:
    return SnippetHit(
        snippet=make_snippet(document_id=document_id, text=text, page=page),
        document=make_document(title=title),
        score=score,
    )


# -- prompts / parsers --------------------------------------------------


@pytest.mark.parametrize(
    "reply, retrieve",
    [
        ("KNOWN", False),
        ("known.", False),
        ("SEARCH", True),
        ("", True),
        ("Hmm, I would probably want to look that up first.", True),
    ],
)
def test_parse_gate(reply, retrieve):
    assert parse_gate(reply) is retrieve


@pytest.mark.parametrize(
    "reply, expected",
    [
        ("ENOUGH", (True, "")),
        ("MISSING: no dates anywhere", (False, "no dates anywhere")),
        ("there is nothing about the version", (False, "there is nothing about the version")),
        ("", (False, "")),
    ],
)
def test_parse_verdict(reply, expected):
    assert parse_verdict(reply) == expected


def test_parse_seeds_strips_decoration_dedupes_and_caps():
    reply = "- postgres version\n1. Postgres Version\n2) upgrade window\n- rollback plan"
    assert parse_seeds(reply, limit=2) == ("postgres version", "upgrade window")


def test_parse_query_takes_one_clean_line():
    assert parse_query("Query: the database version we picked") == "the database version we picked"


def test_no_retrieval_prompt_mentions_summarise():
    from agentchat.core import prompts

    for name in dir(prompts):
        if name.endswith(("SYSTEM", "PROMPT", "HEADER", "FOOTER", "HEDGE")):
            assert "summarise" not in getattr(prompts, name).lower()


# -- render_facts / recall_block --------------------------------------


def test_render_facts_groups_by_view():
    out = render_facts(
        [_hit("Fact A", "claim"), _hit("Fact B", "evidence", quote="B said this")],
        budget=1000,
    )
    assert "Facts:" in out and "Fact A" in out
    assert "Said:" in out and 'B said this' in out


def test_render_facts_drops_whole_hits_under_a_tight_budget():
    hits = [_hit(f"Fact number {i} with some words", "claim", score=1.0 - i / 10) for i in range(6)]
    assert render_facts(hits, budget=10_000).count("Fact number") == 6
    out = render_facts(hits, budget=12)
    assert "Fact number 0" in out  # highest score kept
    assert "Fact number 5" not in out  # lowest score dropped


def test_injected_text_carries_the_message_twice_and_hedges_only_on_cap():
    recall = Recall(
        hits=(_hit("Fact A"),), digest="Facts:\n- Fact A", queries=("q",),
        rounds=1, exit="sufficient", user_text="what db did we pick?",
    )
    text = injected_text("what db did we pick?", None, recall)
    assert text.count("what db did we pick?") == 2
    assert RECALL_HEDGE not in text

    capped = Recall(**{**recall.__dict__, "exit": "cap"})
    assert RECALL_HEDGE in recall_block(capped)


# -- view_text / assemble --------------------------------------------


def test_view_text_claim_and_evidence():
    fact = Fact(
        text="The team picked Postgres.",
        phrases=(Phrase(message_id="m", start=0, end=1, author=Author("user", "user"), quote="we picked Postgres"),),
    )
    assert view_text(fact, "claim") == "The team picked Postgres."
    assert view_text(fact, "evidence") == "user: we picked Postgres"


def test_view_text_evidence_is_empty_for_a_quoteless_fact():
    fact = Fact(text="x", phrases=(Phrase(message_id="m", start=0, end=1, author=Author("user", "user")),))
    assert view_text(fact, "evidence") == ""


def test_assemble_drops_a_subset_duplicate_and_respects_limit():
    a = _hit("The team chose Postgres 14 for the billing service.", score=0.9)
    b = _hit("The team chose Postgres 14 for billing.", score=0.8)  # subset of a
    c = _hit("Priya is allergic to peanuts.", score=0.7)
    out = assemble([a, b, c], limit=5)
    assert [h.fact.text for h in out] == [a.fact.text, c.fact.text]
    assert len(assemble([a, c], limit=1)) == 1


# -- FactIndex ------------------------------------------------------


async def test_index_writes_two_rows_per_fact_and_one_for_a_quoteless_fact(store):
    embedder = HashingEmbedder()
    group = await make_indexed_group(store, embedder)

    rows = await store.fact_embeddings(group.id, model_id=embedder.info.id)
    quoteless = [f for f in group.facts if not f.phrases]
    assert len(quoteless) == 1
    # 12 facts, one quoteless → 11*2 + 1 = 23 rows
    assert len(rows) == len(GOLDEN_FACTS) * 2 - 1


async def test_ensure_indexed_embeds_the_backlog_then_returns_zero(store):
    embedder = HashingEmbedder()
    group = make_group()
    await store.save_group(group)
    conv = make_conversation(group_id=group.id)
    await store.save(conv)
    await store.save_facts([
        Fact(conversation_id=conv.id, group_id=group.id, text="one fact here", phrases=()),
        Fact(conversation_id=conv.id, group_id=group.id, text="another fact here", phrases=()),
    ])
    index = FactIndex(store, embedder)

    assert await index.ensure_indexed(group.id) == 2
    assert await index.ensure_indexed(group.id) == 0


async def test_changing_the_embedder_id_reembeds_and_keeps_the_old_rows(store):
    group = await make_indexed_group(store, HashingEmbedder())
    before = await store.fact_embeddings(group.id, model_id="hashing-64")

    from agentchat.llm.embedding import EmbedderInfo

    other = HashingEmbedder(EmbedderInfo(id="other-embedder", name="Other"))
    n = await FactIndex(store, other).ensure_indexed(group.id)

    assert n == len(GOLDEN_FACTS)
    assert len(await store.fact_embeddings(group.id, model_id="hashing-64")) == len(before)
    assert await store.fact_embeddings(group.id, model_id="other-embedder")


async def test_search_never_returns_a_fact_from_another_group(store):
    """The single most important test in this file (R1)."""
    embedder = HashingEmbedder()
    mine = await make_indexed_group(store, embedder, name="Mine")
    # A second group using the same vocabulary.
    theirs = make_group(name="Theirs")
    await store.save_group(theirs)
    tconv = make_conversation(group_id=theirs.id)
    await store.save(tconv)
    tfacts = [
        Fact(conversation_id=tconv.id, group_id=theirs.id,
             text="The team chose Postgres 14 for the billing service.", phrases=()),
    ]
    await store.save_facts(tfacts)
    await FactIndex(store, embedder).index(tfacts)

    index = FactIndex(store, embedder)
    vectors = await embedder.embed(["Postgres 14 billing service"])
    hits = await index.search(vectors, mine.id, k=10, min_score=0.0)

    assert hits
    assert all(h.fact.group_id == mine.id for h in hits)


async def test_search_min_score_excludes_unrelated_facts_and_exclude_suppresses_by_id(store):
    embedder = HashingEmbedder()
    group = await make_indexed_group(store, embedder)
    index = FactIndex(store, embedder)

    vectors = await embedder.embed(["Postgres 14 billing service database"])
    hits = await index.search(vectors, group.id, k=20, min_score=0.3)
    assert hits
    assert all("Postgres" in h.fact.text or "billing" in h.fact.text or "database" in h.fact.text
               for h in hits)

    top_id = max(hits, key=lambda h: h.score).fact.id
    again = await index.search(vectors, group.id, k=20, min_score=0.3, exclude={top_id})
    assert all(h.fact.id != top_id for h in again)


async def test_search_returns_one_hit_per_fact_at_the_better_view_score(store):
    embedder = HashingEmbedder()
    group = await make_indexed_group(store, embedder)
    index = FactIndex(store, embedder)

    vectors = await embedder.embed(["Priya vegetarian eggs peanut allergy"])
    hits = await index.search(vectors, group.id, k=20, min_score=0.0)
    ids = [h.fact.id for h in hits]
    assert len(ids) == len(set(ids))


async def test_search_on_an_empty_group_is_no_hits_not_an_error(store):
    embedder = HashingEmbedder()
    group = make_group()
    await store.save_group(group)
    index = FactIndex(store, embedder)
    vectors = await embedder.embed(["anything at all"])
    assert await index.search(vectors, group.id, k=5, min_score=0.0) == []


# -- the loop -----------------------------------------------------------


def _retriever(store, provider, **kw) -> AdaptiveRetriever:
    embedder = HashingEmbedder()
    kw.setdefault("timeout", 30.0)
    return AdaptiveRetriever(
        _registry_with(provider), FactIndex(store, embedder), **kw
    )


async def _group_conv(store, embedder):
    group = await make_indexed_group(store, embedder)
    conv = Conversation(group_id=group.id)
    return group, conv


async def test_gate_known_returns_none_after_one_call_and_no_store_read(store):
    provider = scripted_provider("KNOWN")
    embedder = HashingEmbedder()
    group = await make_indexed_group(store, embedder)
    retriever = AdaptiveRetriever(_registry_with(provider), FactIndex(store, embedder))
    conv = Conversation(group_id=group.id)

    result = await retriever.recall(conv, "write me a haiku")

    assert result is None
    assert len(provider.calls) == 1


async def test_first_round_enough_exits_sufficient_with_expected_call_count(store):
    embedder = HashingEmbedder()
    group = await make_indexed_group(store, embedder)
    provider = scripted_provider(
        "SEARCH",                       # gate
        "postgres billing version", "billing database choice", "db we picked",  # 3 rewrites
        "ENOUGH",                       # judge
    )
    retriever = AdaptiveRetriever(
        _registry_with(provider), FactIndex(store, embedder), rewrites=3
    )
    conv = Conversation(group_id=group.id)

    result = await retriever.recall(conv, "what database did we pick for billing?")

    assert result is not None
    assert result.exit == "sufficient"
    assert result.rounds == 1
    assert len(provider.calls) == 1 + 3 + 1


async def test_always_missing_exits_at_the_round_cap(store):
    embedder = HashingEmbedder()
    group = await make_indexed_group(store, embedder)
    provider = scripted_provider(
        "SEARCH",
        "q1", "MISSING: gap one",
        "seed2", "q2", "MISSING: gap two",
        "seed3", "q3", "MISSING: gap three",
    )
    retriever = AdaptiveRetriever(
        _registry_with(provider), FactIndex(store, embedder),
        rounds=3, rewrites=1, seeds=1,
    )
    conv = Conversation(group_id=group.id)

    result = await retriever.recall(conv, "what postgres version in production?")

    assert result.exit == "cap"
    assert result.rounds == 3
    assert result.gap == "gap three"


async def test_the_judge_prompt_carries_the_original_message_and_no_rewrites(store):
    embedder = HashingEmbedder()
    group = await make_indexed_group(store, embedder)
    provider = scripted_provider(
        "SEARCH", "a rewritten query about postgres", "ENOUGH"
    )
    retriever = AdaptiveRetriever(
        _registry_with(provider), FactIndex(store, embedder), rewrites=1
    )
    conv = Conversation(group_id=group.id)

    await retriever.recall(conv, "ORIGINAL QUESTION about the db")

    judge_call = provider.calls[-1]
    prompt = judge_call.messages[-1].content
    assert "ORIGINAL QUESTION about the db" in prompt
    assert "a rewritten query about postgres" not in prompt


async def test_the_reseed_prompt_gets_the_judges_gap_verbatim(store):
    embedder = HashingEmbedder()
    group = await make_indexed_group(store, embedder)
    provider = scripted_provider(
        "SEARCH",
        "q1", "MISSING: the exact version number is absent",
        "postgres version number", "ENOUGH",
    )
    retriever = AdaptiveRetriever(
        _registry_with(provider), FactIndex(store, embedder),
        rounds=3, rewrites=1, seeds=1,
    )
    conv = Conversation(group_id=group.id)

    await retriever.recall(conv, "what version?")

    reseed_call = provider.calls[3]
    assert "the exact version number is absent" in reseed_call.messages[-1].content


async def test_a_provider_error_mid_loop_returns_none(store):
    embedder = HashingEmbedder()
    group = await make_indexed_group(store, embedder)

    class _Boom:
        info = scripted_provider().info
        is_loaded = True

        async def load(self): ...
        async def unload(self): ...

        async def generate(self, messages, options=None):
            raise __import__("agentchat.core.errors", fromlist=["ProviderError"]).ProviderError("boom")
            yield ""

    retriever = AdaptiveRetriever(_registry_with(_Boom()), FactIndex(store, embedder))
    conv = Conversation(group_id=group.id)

    assert await retriever.recall(conv, "anything") is None


async def test_timeout_of_zero_returns_none(store):
    embedder = HashingEmbedder()
    group = await make_indexed_group(store, embedder)
    provider = scripted_provider("SEARCH", "q", "ENOUGH")
    retriever = AdaptiveRetriever(
        _registry_with(provider), FactIndex(store, embedder), timeout=0.0
    )
    conv = Conversation(group_id=group.id)

    assert await retriever.recall(conv, "anything") is None


async def test_recall_on_a_group_with_no_facts_still_runs_and_returns_a_recall(store):
    embedder = HashingEmbedder()
    group = make_group()
    await store.save_group(group)
    provider = scripted_provider("SEARCH", "q1", "q2", "q3", "ENOUGH")
    retriever = AdaptiveRetriever(
        _registry_with(provider), FactIndex(store, embedder), rewrites=3
    )
    conv = Conversation(group_id=group.id)

    result = await retriever.recall(conv, "what did we decide?")

    assert isinstance(result, Recall)
    assert result.hits == ()


# -- ChatService wiring -----------------------------------------------


from agentchat.core.chat import ChatService  # noqa: E402


class _CountingStore:
    """Wraps a real store, counting reads that recall must not make when it is
    switched off."""

    def __init__(self, inner):
        self._inner = inner
        self.reads = 0

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if name in {"fact_embeddings", "facts_without_embeddings", "list_facts"}:
            async def counted(*a, **kw):
                self.reads += 1
                return await attr(*a, **kw)
            return counted
        return attr


async def test_retriever_none_touches_no_store_read_and_makes_one_call(store):
    counting = _CountingStore(store)
    provider = scripted_provider("a reply")
    chat = ChatService(_registry_with(provider), store=counting, retriever=None)
    conv = Conversation(group_id="default")

    async for _ in chat.stream_reply(conv, "what did we pick for the db?"):
        pass

    assert counting.reads == 0
    assert len(provider.calls) == 1


async def test_a_recalled_turn_persists_the_matched_fact_and_its_source_conversation(store):
    embedder = HashingEmbedder()
    group = await make_indexed_group(store, embedder)
    provider = scripted_provider(
        "SEARCH", "postgres billing version chosen", "ENOUGH", "the reply"
    )
    retriever = AdaptiveRetriever(
        _registry_with(provider), FactIndex(store, embedder),
        rewrites=1, min_score=0.0,
    )
    chat = ChatService(_registry_with(provider), store=store, retriever=retriever)
    # The chat turn and the loop share one provider; script has the reply last.
    conv = Conversation(group_id=group.id)

    async for _ in chat.stream_reply(conv, "which Postgres version for billing?"):
        pass

    recall_meta = chat.last_turn.message.metadata.get("recall")
    assert recall_meta is not None
    assert recall_meta["facts"]
    matched = recall_meta["facts"][0]
    assert "Postgres" in matched["text"]
    assert matched["conversation_id"].startswith(group.id)


async def test_a_consulted_turn_also_recalls(store):
    from agentchat.core.delegation import DelegationService

    embedder = HashingEmbedder()
    group = await make_indexed_group(store, embedder)
    provider = scripted_provider(
        # recall first: gate, one rewrite, judge
        "SEARCH", "postgres billing version chosen", "ENOUGH",
        # then delegation: route, task, specialist
        "ask_bank", "Explain APR.", "an answer",
        # then the reply
        "a reply",
    )
    registry = _registry_with(provider)
    retriever = AdaptiveRetriever(
        registry, FactIndex(store, embedder), rewrites=1, min_score=0.0
    )
    chat = ChatService(
        registry, store=store, delegator=DelegationService(registry), retriever=retriever
    )
    conv = Conversation(group_id=group.id)

    async for _ in chat.stream_reply(conv, "what does APR mean for our billing?"):
        pass

    assert chat.last_turn.consultation is not None
    assert chat.last_turn.recall is not None
    # gate → rewrite → judge → route → task → specialist → chat
    assert len(provider.calls) == 7
    reply = chat.last_turn.message
    assert reply.metadata.get("subagent") is not None
    assert reply.metadata.get("recall") is not None


async def test_extract_facts_writes_both_the_fact_and_its_vectors_in_one_pass(store):
    from agentchat.core.facts import FactExtractor

    embedder = HashingEmbedder()
    provider = scripted_provider(
        *(["The team runs Postgres 14 for billing.", "Postgres 14 for billing"] * 4)
    )
    registry = _registry_with(provider)
    retriever = AdaptiveRetriever(registry, FactIndex(store, embedder))
    chat = ChatService(
        registry, store=store,
        fact_extractor=FactExtractor(registry), retriever=retriever,
    )
    conv = make_conversation(messages=[
        Message(id=f"m{i}", role="user" if i % 2 == 0 else "assistant",
                content="We run Postgres 14 for billing and deploy on Tuesdays.")
        for i in range(6)
    ])
    await store.save(conv)

    facts = await chat.extract_facts(conv)

    assert len(facts) == 1
    rows = await store.fact_embeddings(conv.group_id, model_id=embedder.info.id)
    assert rows  # vectors written in the same pass


# -- the document corpus ------------------------------------------------


def test_render_digest_names_every_snippets_source():
    text = render_digest(
        [_hit("The team chose Postgres 14.")],
        [
            _snippet_hit("Expenses over two hundred euro need approval.", page=4),
            _snippet_hit("New joiners get a laptop.", title="onboarding.md"),
        ],
        budget=4000,
    )

    assert "Facts:\n- The team chose Postgres 14." in text
    assert '- [handbook.pdf p.4] "Expenses over two hundred euro need approval."' in text
    assert '- [onboarding.md] "New joiners get a laptop."' in text


def test_render_digest_gives_documents_the_whole_budget_when_there_are_no_facts():
    hits = [_snippet_hit("a " * 200, score=0.9), _snippet_hit("b " * 200, score=0.8)]

    with_facts = render_digest([_hit("f " * 200)], hits, budget=200)
    alone = render_digest([], hits, budget=200)

    assert alone.count("- [") > with_facts.count("- [")


def test_render_documents_drops_whole_hits_lowest_scored_first():
    hits = [
        _snippet_hit("the best match here", score=0.9),
        _snippet_hit("a weaker match here", score=0.4),
    ]

    text = render_documents(hits, budget=12)

    assert "the best match here" in text
    assert "a weaker match here" not in text


def test_a_snippet_that_is_all_whitespace_and_newlines_renders_on_one_line():
    text = render_documents([_snippet_hit("first line\n\nsecond line")], budget=400)
    assert '- [handbook.pdf] "first line second line"' in text


def test_cap_per_document_keeps_each_documents_best():
    hits = [
        _snippet_hit(f"snippet {n}", document_id="a", score=0.9 - n / 10)
        for n in range(5)
    ] + [_snippet_hit("from b", document_id="b", score=0.5)]

    kept = cap_per_document(hits, per_document=2)

    assert [h.snippet.text for h in kept if h.snippet.document_id == "a"] == [
        "snippet 0",
        "snippet 1",
    ]
    assert any(h.snippet.document_id == "b" for h in kept)
    assert cap_per_document(hits, per_document=0) == tuple(hits)


def test_assemble_dedupes_snippets_by_their_own_text():
    long_hit = _snippet_hit("expenses over two hundred euro need approval", score=0.9)
    subset = _snippet_hit("expenses approval", score=0.8)

    kept = assemble([long_hit, subset], limit=5, text=snippet_text)

    assert [h.snippet.text for h in kept] == [long_hit.snippet.text]


async def test_snippet_index_writes_one_vector_per_snippet(store):
    embedder = HashingEmbedder()
    await make_indexed_corpus(store, embedder)

    rows = await store.snippet_vectors(model_id=embedder.info.id)
    expected = sum(len(spec["snippets"]) for spec in GOLDEN_DOCUMENTS)
    assert len(rows) == expected


async def test_snippet_ensure_indexed_embeds_the_backlog_then_returns_zero(store):
    document, snippets = make_document(), None
    snippets = [
        make_snippet(document_id=document.id, ordinal=n, text=f"snippet {n} of text")
        for n in range(3)
    ]
    await store.save_document(document, snippets)
    index = SnippetIndex(store, HashingEmbedder())

    assert await index.ensure_indexed() == 3
    assert await index.ensure_indexed() == 0


async def test_changing_the_embedder_id_reembeds_snippets_and_keeps_the_old_rows(store):
    embedder = HashingEmbedder()
    await make_indexed_corpus(store, embedder)
    before = await store.snippet_vectors(model_id="hashing-64")

    from agentchat.llm.embedding import EmbedderInfo

    other = HashingEmbedder(EmbedderInfo(id="other-embedder", name="Other"))
    embedded = await SnippetIndex(store, other).ensure_indexed()

    assert embedded == len(before)
    assert len(await store.snippet_vectors(model_id="hashing-64")) == len(before)
    assert await store.snippet_vectors(model_id="other-embedder")


async def test_snippet_search_is_global_and_finds_a_document_from_any_group(store):
    """The counterpart of the fact suite's group-scoping test: documents are
    deliberately *not* scoped, so a group with no facts of its own still
    retrieves them."""
    embedder = HashingEmbedder()
    await make_indexed_corpus(store, embedder)
    index = SnippetIndex(store, embedder)

    vectors = await embedder.embed(["expenses approval finance partner"])
    hits = await index.search(vectors, k=5, min_score=0.2)

    assert hits
    # `search` unions by snippet id and does not promise an order, exactly as
    # `FactIndex.search` does not.
    assert max(hits, key=lambda h: h.score).document.title == "handbook.pdf"
    assert all(isinstance(h, SnippetHit) for h in hits)


async def test_snippet_search_attributes_a_hit_to_its_document_and_page(store):
    embedder = HashingEmbedder()
    await make_indexed_corpus(store, embedder)

    vectors = await embedder.embed(["reimbursed within thirty days claim filed"])
    hits = await SnippetIndex(store, embedder).search(vectors, k=3, min_score=0.2)

    best = max(hits, key=lambda h: h.score)
    assert best.snippet.page == 2
    assert best.source == "handbook.pdf p.2"


async def test_snippet_search_on_an_empty_corpus_is_no_hits_not_an_error(store):
    embedder = HashingEmbedder()
    index = SnippetIndex(store, embedder)
    vectors = await embedder.embed(["anything at all"])

    assert await index.search(vectors, k=5, min_score=0.0) == []
    assert await index.search([], k=5, min_score=0.0) == []


async def test_deleting_a_document_removes_it_from_search(store):
    embedder = HashingEmbedder()
    documents = await make_indexed_corpus(store, embedder)
    index = SnippetIndex(store, embedder)
    vectors = await embedder.embed(["expenses approval finance partner"])
    assert await index.search(vectors, k=5, min_score=0.2)

    await store.delete_document(documents[0].id)

    hits = await index.search(vectors, k=5, min_score=0.2)
    assert all(h.document.id != documents[0].id for h in hits)


# -- the loop over both corpora -----------------------------------------


async def test_a_round_searches_both_corpora_with_the_same_queries(store):
    embedder = HashingEmbedder()
    group = await make_indexed_group(store, embedder)
    await make_indexed_corpus(store, embedder)
    provider = scripted_provider("SEARCH", "expenses approval finance", "ENOUGH")
    retriever = AdaptiveRetriever(
        _registry_with(provider),
        FactIndex(store, embedder),
        SnippetIndex(store, embedder),
        rewrites=1,
        min_score=0.0,
        snippet_min_score=0.2,
    )

    result = await retriever.recall(
        Conversation(group_id=group.id), "who approves an expense?"
    )

    assert result is not None
    assert result.snippets
    assert "Documents:" in result.digest
    # No extra generation: gate, one rewrite, judge — the fact-only count.
    assert len(provider.calls) == 3


async def test_the_judge_is_shown_the_documents_as_well_as_the_facts(store):
    embedder = HashingEmbedder()
    group = await make_indexed_group(store, embedder)
    await make_indexed_corpus(store, embedder)
    provider = scripted_provider("SEARCH", "expenses approval finance", "ENOUGH")
    retriever = AdaptiveRetriever(
        _registry_with(provider),
        FactIndex(store, embedder),
        SnippetIndex(store, embedder),
        rewrites=1,
        min_score=0.0,
        snippet_min_score=0.2,
    )

    await retriever.recall(Conversation(group_id=group.id), "who approves an expense?")

    judge_prompt = provider.calls[-1].messages[-1].content
    assert "Evidence:" in judge_prompt
    assert "[handbook.pdf" in judge_prompt


async def test_the_digest_holds_no_more_than_the_cap_per_document(store):
    embedder = HashingEmbedder()
    group = await make_indexed_group(store, embedder)
    await make_indexed_corpus(store, embedder)
    provider = scripted_provider("SEARCH", "expenses approval receipts euro", "ENOUGH")
    retriever = AdaptiveRetriever(
        _registry_with(provider),
        FactIndex(store, embedder),
        SnippetIndex(store, embedder),
        rewrites=1,
        min_score=0.0,
        snippet_min_score=0.0,
        snippets_per_document=1,
    )

    result = await retriever.recall(
        Conversation(group_id=group.id), "what do I do about expenses?"
    )

    titles = [h.document.title for h in result.snippets]
    assert len(titles) == len(set(titles))


async def test_a_snippet_search_failure_still_answers_from_the_facts(store):
    embedder = HashingEmbedder()
    group = await make_indexed_group(store, embedder)
    provider = scripted_provider("SEARCH", "postgres billing", "ENOUGH")

    class _BrokenSnippetIndex(SnippetIndex):
        async def search(self, *a, **kw):
            raise RuntimeError("corpus is on fire")

    retriever = AdaptiveRetriever(
        _registry_with(provider),
        FactIndex(store, embedder),
        _BrokenSnippetIndex(store, embedder),
        rewrites=1,
        min_score=0.0,
    )

    result = await retriever.recall(
        Conversation(group_id=group.id), "which database for billing?"
    )

    # The whole loop degrades to a normal reply — the same boundary a fact
    # failure crosses.
    assert result is None


async def test_documents_off_reads_no_snippet_vectors(store):
    embedder = HashingEmbedder()
    group = await make_indexed_group(store, embedder)
    await make_indexed_corpus(store, embedder)

    reads = 0
    inner = store.snippet_vectors

    async def counted(*a, **kw):
        nonlocal reads
        reads += 1
        return await inner(*a, **kw)

    store.snippet_vectors = counted
    provider = scripted_provider("SEARCH", "postgres billing", "ENOUGH")
    retriever = AdaptiveRetriever(
        _registry_with(provider), FactIndex(store, embedder), None,
        rewrites=1, min_score=0.0,
    )

    result = await retriever.recall(
        Conversation(group_id=group.id), "which database for billing?"
    )

    assert result is not None
    assert result.snippets == ()
    assert reads == 0


async def test_a_recalled_turn_persists_the_document_it_used(store):
    from agentchat.core.chat import ChatService

    embedder = HashingEmbedder()
    group = await make_indexed_group(store, embedder)
    await make_indexed_corpus(store, embedder)
    provider = scripted_provider(
        "SEARCH", "expenses approval finance partner", "ENOUGH", "the reply"
    )
    registry = _registry_with(provider)
    retriever = AdaptiveRetriever(
        registry,
        FactIndex(store, embedder),
        SnippetIndex(store, embedder),
        rewrites=1,
        min_score=0.0,
        snippet_min_score=0.2,
    )
    chat = ChatService(registry, store=store, retriever=retriever)

    async for _ in chat.stream_reply(
        Conversation(group_id=group.id), "who approves an expense?"
    ):
        pass

    documents = chat.last_turn.message.metadata["recall"]["documents"]
    assert documents
    assert documents[0]["title"] == "handbook.pdf"
    assert documents[0]["source"].startswith("handbook.pdf")
    assert documents[0]["text"] in chat.last_turn.message.metadata["recall"]["block"]
