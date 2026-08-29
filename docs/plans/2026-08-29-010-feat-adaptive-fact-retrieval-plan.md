---
artifact_contract: ce-unified-plan/v1
artifact_readiness: proposed
execution: code
product_contract_source: ce-plan-bootstrap
origin: docs/requirements.md
title: "feat: Adaptive fact retrieval — a bounded RAG loop over the group's facts"
date: 2026-08-29
depth: standard
---

# feat: Adaptive fact retrieval — a bounded RAG loop over the group's facts

**Origin requirements:** `docs/requirements.md` (NFR-RAG-01, NFR-RAG-03,
NFR-RAG-04, NFR-RAG-05, NFR-S-02, NFR-S-04, NFR-CTX-02, NFR-CTX-03, NFR-CTX-05,
NFR-U-07, NFR-Q-03)
**Target repo:** this repo (`agentchat`), branch `feat/RAG-pipeline-fact-extraction`
off `main`
**Builds on:** plan 009 (`facts`, `fact_phrases`, `fact_extraction_state`,
`core/anchoring.py`). This is the consumer plan 009 deferred.
**Replaces:** plans 007 and 008. `ConversationSummary`, `ExtractionService`,
`MemoryEnricher`, the `conversation_summaries` table and the summary/keyword
prompts are deleted, not left dormant beside the new path. Plan 008's note under
the user turn is the one part that survives — adapted, and given a great deal
more to say (U8).

---

## What this builds

Recall stops being a keyword match against conversation summaries and becomes an
**adaptive retrieval loop over the group's facts**, adapted from *The Adaptive
Retrieval Loop*. Every user message crosses a gate. Self-contained messages are
answered as they are today. The rest enter a bounded loop: rewrite, retrieve,
assemble, judge — and the judge's named gap becomes the next round's searches.

```
question ──▶ gate ──self-contained──────────────────────────────▶ reply
               │
          needs evidence
               ▼
        ┌──▶ expand (seed → n queries, sequential, anti-duplicate)
        │      ▼
        │    retrieve (embed batch → top-k per view, union by fact id)
        │      ▼
        │    assemble (dedupe, rank, cap → digest)
        │      ▼
        └─gap─ adjudicate ──sufficient / round cap──▶ reply with digest
```

The corpus is the `facts` table, scoped to the conversation's group and to
nothing else (NFR-S-02, NFR-CTX-02). A fact is already one sentence, so there is
no chunking stage: **the fact is the fragment**. Each fact is indexed under two
views — its claim and its evidence — embedded by a small model running on the
CPU, and the vectors are written when the fact is written.

The note under the user turn stays and grows: where it read *"enriched by 2
memories"*, it now accounts for the whole enhancement — the queries the loop
ran, the judge's verdicts, and the block that was appended to the prompt,
verbatim. Retrieval that cannot be inspected cannot be trusted or tuned, so the
prompt the model actually received is visible per turn and survives reopening
the conversation.

## Requirements

| ID | Requirement |
|---|---|
| R1 | Retrieval reads only facts belonging to the conversation's group. |
| R2 | Each fact is embedded under two views — `claim` (the fact text) and `evidence` (its authored quotes) — at fact-creation time. |
| R3 | Embeddings are produced by a local model on the CPU, loaded from a path, never downloaded. |
| R4 | A vector is stored with the embedder id that produced it; a vector from another embedder is not searched, it is replaced. |
| R5 | Facts that exist without a current vector are embedded before the first search that would need them, incrementally and restartably. |
| R6 | A gate decides per message whether to retrieve at all; an unparseable verdict means retrieve. |
| R7 | Each seed query is expanded into `n` distinct queries, generated one at a time, each shown the queries already written. |
| R8 | Every query retrieves its own top `k`; results are unioned by fact id, keeping the best score. |
| R9 | The digest is bounded: deduplicated, ranked, and capped, whatever the round. |
| R10 | The judge scores the digest against the **original** user message, never against a rewritten query. |
| R11 | An insufficient verdict names the gap in one sentence, and that sentence is what the next round's seeds are written from. |
| R12 | The loop terminates: a hard round cap and a wall-clock deadline, either of which releases the digest as it stands. |
| R13 | Which exit the loop took is carried to the generator and to the user. |
| R14 | Every retrieved fact is attributable — its quote, its author, and the conversation it came from are inspectable in the UI. |
| R15 | Retrieval failure degrades to a normal reply, never to an error or a hang. |
| R16 | Retrieval is switchable off, and off means no embedder load, no LLM calls, and no store reads. |
| R17 | Deleting a conversation or a group removes its facts' vectors with it. |
| R18 | The retrieval loop's knobs are externalised as environment variables. |
| R19 | No consultation and no recall in the same turn (KTD12 unchanged). |
| R20 | The UI shows, per turn, how the prompt was enhanced: the queries and verdicts the loop produced, and the injected block exactly as the model received it. It survives reopening the conversation. |

---

## Design

### Where the reference's stages land

| Reference stage | Here |
|---|---|
| Indexed corpus | `facts` + `fact_phrases`, embedded into `fact_embeddings` on write |
| Fragment | One fact. No chunker — plan 009 already fragments the conversation |
| Multiple views | `claim` and `evidence`, both stored, searched independently, re-joined by fact id |
| Gate | `GATE_SYSTEM`, one call, one token, biased to retrieve |
| Rewriter | `REWRITE_SYSTEM`, `n` sequential calls per seed |
| Retriever | Batched CPU embedding, brute-force cosine over the group's vectors |
| Compressor | **Deterministic** — dedupe, rank, cap. No LLM call |
| Adjudicator | `JUDGE_SYSTEM` then `RESEED_SYSTEM`, two calls, as the reference has them |
| Generator | The existing reply, with the digest appended to the user turn |

**The compressor is the one stage that is not a model call**, because a fact is
already the output of a summarisation call: re-summarising a list of
one-sentence facts would strip the phrase attribution R14 and NFR-RAG-05 need,
and buy no tokens back. NFR-RAG-01's summarisation limb is discharged at write
time by `FactExtractor`, not at read time. Compression drift, the reference's
first listed cost, therefore does not exist here.

### The two views

```
fact.text                      → claim view
"user: we run Postgres 14 in   → evidence view
 production; qwen3-14b: …"
```

The evidence view is built from the fact's phrases alone — author label and
verbatim quote, joined — so it needs no message load and no join to
`conversations`. It is what makes *"who said this, and in what words"* match
independently of *"what was established"*, and the two are re-joined when hits
are unioned by fact id.

This requires the quote itself, which plan 009 stored only as a span. Add
`fact_phrases.quote`: the verbatim substring, denormalised. Deriving it needs
the source message, and a group's facts span many conversations — one load per
conversation on every turn, to recover text the extractor already held.

### Indexing

Vectors are written by the same call that writes the fact
(`ChatService.extract_facts`), so the common path never embeds at read time.
`FactIndex.ensure_indexed(group_id)` is the catch-up: it selects facts with no
row in `fact_embeddings` for the **active embedder id** and embeds them in
batches. That single query covers three cases with no extra state — facts from
before this plan, facts written while retrieval was switched off, and facts
whose vectors were produced by a different embedder (R4, R5, NFR-RAG-04).

Vectors are L2-normalised at write time, so search is a dot product and no
runtime normalisation is needed. Stored as `float32` little-endian bytes with
their length in a `dim` column.

Scoring is brute force over the group's vectors: a prototype group holds
hundreds to low thousands of facts, and one `numpy` matrix product over
`(n, 384)` is faster than the index that would replace it is to build.

### The loop

```
gate(user_text)  ──▶ KNOWN → return None (a normal turn, no digest)

seeds = [user_text]; tried = []; pool = {}; round = 1
loop:
    queries = [rewrite(seed, user_text, tried + queries) for seed in seeds] × n
    vectors = embed(queries)                       # one batched forward
    for each query vector, for each view: top k by cosine, above MIN_SCORE
    pool  = union by fact id, keeping the best score
    tried += queries
    digest = assemble(pool)                        # dedupe, rank, cap
    verdict, gap = adjudicate(user_text, digest)   # ORIGINAL message (R10)
    if verdict is ENOUGH or round == cap or deadline passed:
        return Recall(...)
    seeds = reseed(user_text, gap, tried)          # ≤ s queries
    round += 1
```

`rewrite` is one call per query, sequentially, each shown every query already
written this recall — a single call asked for `n` queries returns `n`
paraphrases. The rewrite also translates register: the user asks in the
vocabulary of a question, the facts are written in the vocabulary of the
conversation they were extracted from.

**Dedup at read time**, the deduplication plan 009 deferred: consecutive windows
overlap by `WINDOW_CARRY`, so near-duplicate facts are expected. A fact whose
`anchoring.significant()` token set is a subset of a higher-scored fact's is
dropped from the digest. Nothing is deleted from the store.

### Cost

| Path | Model calls |
|---|---|
| Gate says self-contained | 1 gate + the reply |
| Round 1 | `n` rewrites + 1 judge + 1 reseed |
| Each later round | `s·n` rewrites + 1 judge (+ 1 reseed unless at the cap) |
| Worst case, defaults `n=3, s=2, cap=3` | 1 + 5 + 8 + 7 = **21** before the reply |

The multiplication is the thing to watch: cost grows with the product of the
knobs, not their sum. `s` is capped in `parse_seeds`, not merely requested in
the prompt, and `RECALL_TIMEOUT` bounds the wall clock regardless (R12).

### Where it runs

Inside `ChatService.stream_reply`, in place of the enrichment selection, with
one difference in placement: `ensure_indexed` touches the store and the CPU
embedder but no provider, so it runs **before** `_provider_lock`; the loop
itself makes provider calls and runs inside it, beside delegation.

The order is unchanged — consult first, and a consultation suppresses recall
entirely (R19). The gate call is not spent in that case.

`AdaptiveRetriever.recall` has `DelegationService`'s failure contract: one entry
point, every failure — provider, storage, embedder, timeout — logged and
returned as `None`, meaning answer normally (R15).

---

## Output Structure

```
src/agentchat/
  llm/
    embedding.py     NEW — Embedder protocol, LocalEmbedder, HashingEmbedder
  core/
    retrieval.py     NEW — Hit, Recall, FactIndex, AdaptiveRetriever
tests/
  test_embedding.py  NEW — both embedders, the real one behind the env flag
  test_retrieval.py  NEW — index, search, the loop, the gate, the exits
```

Deleted: `src/agentchat/core/extraction.py`, `src/agentchat/core/enrichment.py`,
`tests/test_extraction.py`, `tests/test_extraction_real_model.py`,
`tests/test_enrichment.py`.

Modified: `core/models.py`, `core/prompts.py`, `core/chat.py`, `core/errors.py`,
`storage/schema.py`, `storage/base.py`, `storage/sqlite.py`, `config.py`,
`ui/app.py`, `ui/widgets.py`, `pyproject.toml`, `tests/conftest.py`,
`tests/factories.py`, `tests/test_storage.py`, `tests/test_core.py`,
`tests/test_app.py`, `tests/test_usage.py`, `tests/test_llm_log.py`,
`README.md`, `AGENTS.md`.

---

## Implementation Units

### U1. Removal — summaries and keyword enrichment

**Requirements:** R19 · **Depends on:** nothing

Delete, in this order so the suite stays runnable between steps:

1. `tests/test_enrichment.py`, `tests/test_extraction.py`,
   `tests/test_extraction_real_model.py`.
2. `core/enrichment.py`, `core/extraction.py`.
3. `core/models.py`: `ConversationSummary`, `_KEYWORD_SEPARATOR`.
4. `core/prompts.py`: `SUMMARY_SYSTEM`, `SUMMARY_PROMPT`, `KEYWORDS_SYSTEM`,
   `KEYWORDS_PROMPT`, `MAX_KEYWORDS`, `KEYWORD_SEPARATOR`, `parse_keywords`,
   `_clean_keyword`, `ENRICHMENT_HEADER`, `ENRICHMENT_BULLET`, `enriched_text`.
5. `core/errors.py`: `ExtractionError`.
6. `storage/schema.py`: the `conversation_summaries` table and its index.
7. `storage/base.py` and `storage/sqlite.py`: `save_summary`, `summary`,
   `list_summaries`, `_summary_from_row`, and the summary half of `delete`.
8. `core/chat.py`: `extractor`, `summarise`, `enricher`, the `selected` branch
   of `stream_reply`, `TurnResult.enrichment`, the `enricher.reset()` call in
   `switch_conversation`.
9. `config.py`: `extract_summaries`, `enrich_messages`, `build_extractor`,
   `build_enricher`.
10. `ui/app.py`: `_maybe_summarise` → `_maybe_flush_facts`, `_summarise` →
    `_flush_facts`, `_flush_facts_then_summarise` folded into it, the
    `build_extractor`/`build_enricher` wiring, `show_enrichment` call sites.
11. `ui/widgets.py`: `EnrichmentNote` is **kept and adapted** into `RecallNote`
    (U8) — the note under the user turn is the feature, not part of the removal.
    Only its `ConversationSummary` import and `_detail_lines` body change here;
    U8 does the rest.
12. `tests/factories.py`: `make_summary`; `tests/conftest.py`: the
    `extract_summaries` / `enrich_messages` defaults.

**Pitfalls**

- **`render_transcript`, `ASSISTANT_CHAR_CAP` and `ELISION` stay.**
  `delegation.py` builds `TASK_PROMPT` with them; they only *look* like summary
  machinery. `render_window` stays too — `facts.py` uses it.
- `_SUMMARISING_STATUS` becomes `_EXTRACTING_STATUS` ("extracting facts…"), and
  `extraction_timeout` keeps its name and its meaning: seconds `action_quit`
  waits before exiting anyway. It now covers the fact flush alone.
- Dropping the table from `SCHEMA` does not drop it from a database that already
  has it, and this prototype has no migrations. That is harmless — nothing reads
  it. Do not add a `DROP TABLE`: it would destroy data on an accidental
  downgrade.
- `test_storage.py`, `test_app.py`, `test_core.py`, `test_usage.py` and
  `test_llm_log.py` all assert on summary behaviour. Delete those cases; do not
  retarget them at facts, which have their own suites.

**Tests:** `uv run pytest` passes with the summary suites gone; `grep -rin
"summar\|enrich\|keyword" src/` returns only `render_transcript`'s own comments
and `ContextDecision.summarised`, which is the context strategy's own field and
has never had anything to do with `ExtractionService`.

---

### U2. `llm/embedding.py` — a CPU embedder

**Requirements:** R3 · **Depends on:** nothing

```python
DEFAULT_EMBED_MODEL_ID = "all-MiniLM-L6-v2"
EMBED_BATCH = 32
#: Past this a fact is not a fact; the cap is a guard against a pathological
#: row, not a truncation policy.
MAX_EMBED_CHARS = 2000

@dataclass(frozen=True)
class EmbedderInfo:
    id: str
    name: str

@runtime_checkable
class Embedder(Protocol):
    @property
    def info(self) -> EmbedderInfo: ...
    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """One L2-normalised vector per input, in order. `[]` for `[]`."""
        ...

class LocalEmbedder:
    def __init__(self, info: EmbedderInfo, *, path: Path) -> None: ...

class HashingEmbedder:
    """Deterministic, weightless, and similar for texts that share words —
    what the retrieval suite searches against."""
    def __init__(self, info: EmbedderInfo | None = None, *, dim: int = 64) -> None: ...
```

`LocalEmbedder` loads `AutoTokenizer` / `AutoModel` from `path` with
`local_files_only=True` on first `embed`, mirroring `TransformersProvider`'s
lazy load and its `ProviderError` wrapping. Each batch: tokenize with
`padding=True, truncation=True`, forward under `torch.inference_mode()` on
`torch.device("cpu")`, **mean-pool over the attention mask** (not the `[CLS]`
token, and not an unmasked mean — padding would drag every short fact toward the
same vector), then `torch.nn.functional.normalize(p=2, dim=1)`. The whole batch
runs in `asyncio.to_thread`.

`HashingEmbedder` folds each token of `anchoring.significant(text)` into
`hash_bucket = blake2b(token) % dim`, sums, and L2-normalises. Use `hashlib`,
not `hash()` — `hash()` is salted per process, and a vector written by one test
run would not match a query in the next.

**Pitfalls**

- Normalise on the way in, once. A cosine computed at read time over unnormalised
  vectors is the same arithmetic paid on every query.
- `embed([])` must return `[]` without touching the model — `ensure_indexed`
  calls it on an up-to-date group every turn.
- The model is a `Bert`-family encoder, not a generator: it takes no
  `GenerationOptions`, records no transcript, and reports no usage. Do not wire
  it into `core.usage` — the context meter measures the reply's window, and an
  encoder's forward pass has nothing to do with it.

**Tests** (`tests/test_embedding.py`): `HashingEmbedder` is deterministic across
instances and process-independent (assert against a hard-coded vector prefix);
every vector has unit norm; `embed([])` is `[]`; two texts sharing significant
tokens score above two that share none; the batch boundary (`EMBED_BATCH + 1`
inputs) returns that many vectors in order. `LocalEmbedder`, marked
`@pytest.mark.skipif` on `AGENTCHAT_TEST_REAL_MODEL`: 384 dimensions, unit norm,
and `cos("we run Postgres in production", "the database is Postgres") >
cos(same, "the cat sat on the mat")`.

---

### U3. Storage — vectors and the quote column

**Requirements:** R2, R4, R5, R17 · **Depends on:** U2

```sql
ALTER TABLE fact_phrases ADD COLUMN quote TEXT NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS fact_embeddings (
  fact_id TEXT NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
  view TEXT NOT NULL,
  model_id TEXT NOT NULL,
  dim INTEGER NOT NULL,
  vector BLOB NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (fact_id, view, model_id));
CREATE INDEX IF NOT EXISTS idx_fact_embeddings_fact ON fact_embeddings(fact_id);
```

```python
async def save_fact_embeddings(self, rows: Sequence[FactEmbedding]) -> None: ...
async def fact_embeddings(self, group_id: str, *, model_id: str) -> list[FactEmbedding]: ...
async def facts_without_embeddings(self, group_id: str, *, model_id: str) -> list[Fact]: ...
```

`FactEmbedding` (`core/models.py`): `fact_id`, `view: Literal["claim",
"evidence"]`, `model_id`, `vector: tuple[float, ...]`.

`facts_without_embeddings` is one `LEFT JOIN` on `(fact_id, model_id)` filtered
to `fact_embeddings.fact_id IS NULL`, not a Python set difference over every
fact in the group.

Bodies match the existing fact methods: `asyncio.to_thread`-offloaded, wrapped in
`StorageError`, `closing(self._connect()) as conn, conn`, a module-level row
mapper beside `_fact_from_row`.

**Pitfalls**

- The `ALTER TABLE` is the only migration this schema has ever carried and it
  works only because of the `DEFAULT ''`. Run it inside `ensure_schema`, guarded
  by a `PRAGMA table_info(fact_phrases)` check, *after* `executescript(SCHEMA)`
  so a fresh database takes the column from `SCHEMA` itself and the guard is a
  no-op. Facts written before this plan keep an empty `quote` and are re-indexed
  under the claim view alone — an empty evidence view is skipped, not embedded.
- `model_id` is part of the primary key, not a plain column: switching embedders
  and switching back must not have thrown the first embedder's vectors away.
- Pack with `numpy.asarray(vector, dtype="<f4").tobytes()` and unpack with
  `numpy.frombuffer(blob, dtype="<f4")`. The explicit `<` is what keeps a
  database written on one machine readable on another; `dtype="f4"` is
  native-endian and silently is not.
- No FK from `fact_embeddings` to anything but `facts` — R17 rides on that
  cascade, exactly as `fact_phrases` does.

**Tests** (`tests/test_storage.py`): round-trip a vector and get the same floats
back within `1e-6`; `PRIMARY KEY` conflict on the same `(fact, view, model)`
upserts rather than duplicating; `facts_without_embeddings` returns only the
unembedded, is empty once all are embedded, and returns **every** fact again for
a different `model_id`; deleting the conversation removes the vectors (query
`fact_embeddings` directly); deleting the group does too; `quote` round-trips and
`phrase.quote == phrase.text(message)` for a fact built by `FactExtractor`;
saving the conversation again leaves vectors intact.

---

### U4. `core/prompts.py` — gate, rewrite, judge, reseed, injection

**Requirements:** R6, R7, R10, R11, R13 · **Depends on:** U1

```python
GATE_KNOWN = "KNOWN"
GATE_SEARCH = "SEARCH"
VERDICT_ENOUGH = "ENOUGH"
VERDICT_MISSING = "MISSING"
MAX_SEEDS = 4          # hard ceiling; `s` is configured below it

GATE_SYSTEM / GATE_PROMPT       → parse_gate(text) -> bool          # True = retrieve
REWRITE_SYSTEM / REWRITE_PROMPT → parse_query(text) -> str
JUDGE_SYSTEM / JUDGE_PROMPT     → parse_verdict(text) -> tuple[bool, str]
RESEED_SYSTEM / RESEED_PROMPT   → parse_seeds(text, *, limit) -> tuple[str, ...]
RECALL_HEADER / RECALL_FOOTER / RECALL_HEDGE
render_facts(hits, *, budget) -> str
recalled_text(user_text, recall) -> str
```

1. **`GATE_SYSTEM`** — classify, never answer. Reply with one word. The two
   errors are not symmetric: a needless search costs latency, a skipped one
   produces a fluent confident wrong answer. So `SEARCH` covers possessive and
   relational phrasing ("our", "the team", "we decided"), status questions about
   anyone not globally famous, ambiguous named entities, private artefacts,
   anything the model could not know — **and genuine uncertainty**. Two worked
   examples, one each way.
2. **`parse_gate`** — `True` unless the reply's first word case-insensitively
   equals `KNOWN`. An unparseable verdict is not a third outcome (R6).
3. **`REWRITE_SYSTEM`** — write one search query for a store of short factual
   statements taken from past conversations. Given the user's message, the seed,
   and the queries already tried, cover a facet they miss. Phrase it in the words
   the conversation would have used, not the words the question uses. One line,
   no preamble, no quotation marks. One worked example showing the register
   shift.
4. **`JUDGE_SYSTEM`** — given the user's message and the evidence, reply
   `ENOUGH`, or `MISSING: ` and one sentence naming what is absent — wrong
   entity, wrong time frame, right topic at the wrong altitude, too vague. Never
   answer the question. Two worked examples.
5. **`parse_verdict`** — `(True, "")` when the first word is `ENOUGH`; otherwise
   `(False, everything after the first colon, or the whole reply stripped of a
   leading `MISSING`)`. Parse leniently, then decide.
6. **`RESEED_SYSTEM`** — given the message, the gap, and every query already
   tried, write at most `limit` new queries aimed **at the gap**, one per line,
   none repeating a tried query.
7. **`parse_seeds`** — one per non-empty line, bullets and numbering stripped,
   deduplicated case-insensitively, capped at `limit`. Mirror `parse_quotes`.
8. **`render_facts(hits, *, budget)`** — grouped by view, not interleaved:
   a `Facts:` block of claim-view hits and a `Said:` block of evidence-view hits
   as `author: "quote"`. Drops lowest-scored whole hits to fit `budget`, reusing
   `context.estimate_tokens` — `prompts.py` already imports it for
   `render_window`.
9. **`recall_block(recall)`** — `RECALL_HEADER`, the rendered digest, then
   `RECALL_FOOTER` restating the user's message; and
   **`recalled_text(user_text, recall)`** = `f"{user_text}\n\n{recall_block(recall)}"`.
   Split in two because U8's note renders the block through the same function
   the prompt does, which is what keeps "this is what the model saw" true rather
   than merely intended. A long digest otherwise buries the thing being asked.
   `RECALL_HEDGE` is appended to the header when
   `recall.exit == "cap"`: the evidence is what a judge has just called
   insufficient, and saying so is the difference between a hedge and a silent
   degradation (R13). The header also tells the model to fall back on its own
   knowledge where the evidence is silent, and never to mention that a retrieval
   step exists.

**Pitfalls**

- The judge and the reseed prompt are shown the **original** user message, never
  a rewritten query. Rewrites are lossy interpretations of intent; a loop that
  judges against its own last guess drifts away from what was asked, one round at
  a time. Comment this at `JUDGE_PROMPT`.
- `REWRITE_PROMPT` must show the tried queries, or the `n` calls return `n`
  paraphrases and `n·k` collapses to `k`.
- `MAX_SEEDS` is enforced in `parse_seeds`. A prompt asking for "at most two" is
  a request; the cap is the dominant term in the cost multiplication.

**Tests** (`tests/test_retrieval.py`): `parse_gate` on `KNOWN`, `known.`,
`SEARCH`, `""`, and a chatty paragraph (→ retrieve); `parse_verdict` on
`ENOUGH`, `MISSING: no dates anywhere`, a bare sentence, `""`; `parse_seeds` on
a bulleted reply, a numbered reply, duplicates differing only in case, more than
`limit`; `render_facts` groups by view and drops whole hits under a tight budget;
`recalled_text` contains the user's message twice and the hedge only on the `cap`
exit; no prompt contains the word `summarise`.

---

### U5. `core/retrieval.py` — the index

**Requirements:** R1, R2, R4, R5, R8, R9 · **Depends on:** U2, U3

```python
VIEWS = ("claim", "evidence")

@dataclass(frozen=True)
class Hit:
    fact: Fact
    view: str
    score: float

def view_text(fact: Fact, view: str) -> str:
    """`claim` → the fact text. `evidence` → `author: quote` per phrase,
    joined. `""` when the view has nothing (pre-plan-010 facts have no
    quotes) — an empty view is not embedded."""

class FactIndex:
    def __init__(self, store: ConversationStore, embedder: Embedder) -> None: ...
    async def index(self, facts: Sequence[Fact]) -> None: ...
    async def ensure_indexed(self, group_id: str) -> int: ...
    async def search(
        self, queries: Sequence[Sequence[float]], group_id: str, *,
        k: int, min_score: float, exclude: Container[str] = frozenset(),
    ) -> list[Hit]: ...
```

`search` loads the group's vectors once, stacks them per view into an
`(n, dim)` float32 matrix, and takes `queries @ matrix.T` — one product for all
queries and all views, not a loop per query. Per query and per view it keeps the
top `k` at or above `min_score`, then unions across everything by `fact.id`,
keeping the highest-scoring `Hit` for each (R8).

`assemble(hits, *, limit) -> tuple[Hit, ...]` — sort by score descending, drop a
hit whose `anchoring.significant(fact.text)` is a subset of an already-kept
hit's, cut at `limit` (R9).

**Pitfalls**

- `ensure_indexed` returns the number embedded so the caller can log it and the
  tests can assert it is `0` on the second call. A backfill that silently re-runs
  every turn is the failure mode this number exists to catch.
- The group's vectors are loaded per `search`, not cached on the instance. A fact
  written by the turn that just ended must be visible to the next one, and a
  cache keyed on nothing would not see it.
- `exclude` holds fact ids, not conversation ids — the caller decides what a
  recent window means; the index does not.
- An empty group is `[]` hits, not an exception, and must not build a `(0, dim)`
  product.

**Tests** (`tests/test_retrieval.py`, `HashingEmbedder` + sqlite store):
`index` writes two rows per fact and one for a quoteless fact; `ensure_indexed`
embeds the backlog then returns `0`; changing the embedder id re-embeds
everything and leaves the old rows in place (R4); a query matching one fact's
words ranks it first; `min_score` excludes an unrelated fact entirely; **a fact
in another group is never returned** (R1) — the single most important test in
this file; `exclude` suppresses by id; `k=1` over three matching facts returns
one; the same fact hit under both views appears once, at the better score;
`assemble` drops a subset-duplicate and respects `limit`.

---

### U6. `core/retrieval.py` — the loop

**Requirements:** R6, R7, R10, R11, R12, R13, R15 · **Depends on:** U4, U5

```python
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
    gap: str = ""

class AdaptiveRetriever:
    def __init__(
        self, registry: ModelRegistry, index: FactIndex, *,
        rounds: int = 3, rewrites: int = 3, hits: int = 10, seeds: int = 2,
        min_score: float = 0.25, digest_facts: int = 12, timeout: float = 120.0,
    ) -> None: ...

    async def recall(
        self, conversation: Conversation, user_text: str,
        on_progress: Callable[[str], None] | None = None,
    ) -> Recall | None: ...
```

`recall` is the single entry point and the single place failure is handled: the
whole loop runs inside one `asyncio.wait_for(..., self._timeout)`, and
`AgentChatError` or `TimeoutError` is logged at warning and returned as `None`
(R15) — `DelegationService.consult`'s contract exactly. `None` also means the
gate said `KNOWN`; both mean the same thing to the caller, so no branch is needed
at the call site.

Inside, the loop checks a monotonic deadline at the top of each round and exits
with `exit="deadline"` on the digest it already has, so the outer `wait_for` is
only ever the backstop for a single wedged call (R12).

`on_progress` is called with `"searching…"`, `"round 2 of 3…"`, `"judging…"` —
up to 21 calls happen before the first streamed chunk (NFR-U-07).

`exclude` is computed here, not in the index: facts of *this* conversation whose
`window_end > len(countable(conversation.messages)) - WINDOW_SIZE`. Those cover
messages the recency window is almost certainly still carrying verbatim, so
injecting them would spend budget restating what the prompt already holds. Older
facts of the same conversation are retrieved — that is context compression doing
its job (NFR-CTX-01).

`core/errors.py` gains `RetrievalError(AgentChatError)`; the embedder's and the
store's failures are wrapped into it at the boundary.

**Pitfalls**

- The digest budget is computed as in `facts.py` — `provider.info.context_window`
  minus the largest per-call `max_tokens` minus `DIGEST_PROMPT_OVERHEAD_TOKENS`,
  floored at `MIN_DIGEST_BUDGET`. A digest sized for one model and reused after a
  switch to a smaller window is how NFR-CTX-04 breaks.
- `tried` accumulates across rounds and is passed to every rewrite and reseed
  call. Resetting it per round is what makes round three re-run round one's
  searches.
- `round == cap` releases the digest; it does not raise, and it does not return
  `None`. An answer built on insufficient evidence with the hedge attached is
  strictly better than an answer built on nothing, silently.
- A round that retrieves nothing at all still goes to the judge. The judge's gap
  on an empty digest is what makes round two's queries useful.

**Tests** (`tests/test_retrieval.py`, `ScriptedProvider` + `HashingEmbedder`):
gate `KNOWN` → `None` after exactly one provider call and **no** store read;
gate `SEARCH` with a first-round `ENOUGH` → `exit="sufficient"`, `rounds == 1`,
and exactly `1 + n + 1` calls; an always-`MISSING` script → `exit="cap"`,
`rounds == 3`, and no more calls than the cost table allows; the judge's gap
reaches the reseed prompt verbatim (assert on `provider.calls[-1].messages`);
every judge call's prompt contains the original user message and none of the
rewrites (R10); the rewrite prompt of call `i` contains the query returned by
call `i-1`; a provider error mid-loop → `None`, not an exception; a `timeout` of
`0` → `None`; a deadline crossed between rounds → `exit="deadline"` with the
digest intact; `recall` on a group with no facts still runs the loop and returns
a `Recall` with no hits.

---

### U7. `ChatService` and wiring

**Requirements:** R16, R18, R19 · **Depends on:** U6

1. `ChatService.__init__` loses `extractor` and `enricher`, gains
   `retriever: AdaptiveRetriever | None = None`. `None` switches recall off, the
   way every other optional service is switched off (R16).
2. `TurnResult.enrichment` → `recall: Recall | None = None`.
3. `stream_reply`:
   ```python
   # Before the lock: the index touches the store and the CPU embedder, not
   # the provider.
   if self.retriever is not None:
       await self.retriever.index.ensure_indexed(conversation.group_id)

   async with self._provider_lock:
       ...
       consultation = ...            # unchanged
       recall = None
       if consultation is None and self.retriever is not None:
           recall = await self.retriever.recall(conversation, user_text, on_progress)
   ```
   `_prompt_messages` takes `recall` where it took `selected`, calling
   `recalled_text`. The trimming rollback is unchanged: if the injected turn does
   not survive, `recall = None` and the prompt is rebuilt.
4. Recall provenance **is** persisted, like consultation and unlike enrichment
   (KTD8), and only once the block actually reached the model:
   ```python
   reply.metadata["recall"] = {
       "rounds": recall.rounds, "exit": recall.exit, "gap": recall.gap,
       "queries": list(recall.queries),
       "facts": [{"id": h.fact.id, "conversation_id": h.fact.conversation_id,
                  "text": h.fact.text, "score": round(h.score, 4),
                  "view": h.view} for h in recall.hits],
       # The injected block verbatim — what U8's note replays after a restart,
       # when no `Recall` object exists to re-render from.
       "block": recall_block(recall),
   }
   ```
   That dict is what R14, R20 and NFR-RAG-05 are verified against after the
   fact. It is written only when the block actually reached the model, so the
   trimming rollback leaves no metadata claiming a context the reply never saw.
5. `extract_facts` indexes what it wrote, inside the same loop, after
   `save_facts` and before the watermark advances (R2):
   ```python
   await self.store.save_facts([fact])
   if self.retriever is not None:
       await self.retriever.index.index([fact])
   ```
6. `config.py`:
   ```python
   recall_facts: bool          AGENTCHAT_RECALL_FACTS          True
   embed_model: str            AGENTCHAT_EMBED_MODEL           all-MiniLM-L6-v2
   embed_model_path: Path      AGENTCHAT_EMBED_MODEL_PATH      model_root/sentence-transformers/all-MiniLM-L6-v2
   recall_rounds: int          AGENTCHAT_RECALL_ROUNDS         3
   recall_rewrites: int        AGENTCHAT_RECALL_REWRITES       3
   recall_hits: int            AGENTCHAT_RECALL_HITS           10
   recall_seeds: int           AGENTCHAT_RECALL_SEEDS          2
   recall_min_score: float     AGENTCHAT_RECALL_MIN_SCORE      0.25
   recall_digest_facts: int    AGENTCHAT_RECALL_DIGEST_FACTS   12
   recall_timeout: float       AGENTCHAT_RECALL_TIMEOUT        120.0
   ```
   ```python
   def build_embedder(settings) -> Embedder | None:
       """`None` when recall is off. `HashingEmbedder` under the mock backend —
       the same switch `build_registry` makes, for the same reason: no test and
       no laptop should need weights on disk."""

   def build_retriever(settings, registry, store, embedder) -> AdaptiveRetriever | None:
   ```
7. `pyproject.toml`: add `numpy>=1.26`. It arrives with `torch` today, but
   `retrieval.py` imports it directly and an undeclared import is one `uv sync`
   from being wrong.
8. `tests/conftest.py`: `mock_settings` gets
   `overrides.setdefault("recall_facts", False)`, documented beside the
   `subagents` and `extract_facts` switches for the same reason — otherwise every
   turn in the suite grows a gate call.

**Pitfalls**

- `ensure_indexed` before the lock, the loop inside it. Getting this backwards
  holds the provider through a cold embedder load on the first turn after start.
- `recall_seeds` is clamped to `MAX_SEEDS` at construction, not trusted from the
  environment.
- Recall is skipped when a consultation happened — check `consultation is None`
  *before* calling `recall`, so the gate call is not spent either (R19).
- Indexing inside `extract_facts`' loop, not after it: a cancelled backlog must
  leave every fact it saved indexed, the same invariant the per-window watermark
  already carries.

**Tests:** `build_embedder` / `build_retriever` on and off, and
`AGENTCHAT_RECALL_FACTS=0` (`test_core.py`). In `test_retrieval.py`: a turn with
`retriever=None` touches neither store nor embedder and makes exactly one
provider call (R16); a consulted turn makes no gate call (R19); a recalled turn's
`reply.metadata["recall"]["facts"]` names the fact that matched, with its source
conversation id (R14); a recall that does not survive context trimming leaves
**no** `recall` key in metadata; `extract_facts` on six messages writes both the
fact and its vectors in one pass.

---

### U8. UI — the recall note shows the prompt as the model received it

**Requirements:** R13, R14, R20 · **Depends on:** U7

`EnrichmentNote` is **adapted, not deleted**: the note under the user turn is
the feature, and it grows from "how many memories were appended" into a full
account of how that turn's prompt was enhanced. `CollapsibleNote`,
`MessageBubble._note`, the idempotent `show_*` mounting and the persisted-metadata
restore path all survive unchanged in shape.

`ui/widgets.py`: `EnrichmentNote` → `RecallNote(trace, block)`.

- **Header:** `recalled 4 facts · 2 rounds · sufficient`, or
  `… · round cap — evidence may be incomplete` on the `cap` and `deadline`
  exits (R13). On a `KNOWN` gate: `answered without retrieval`.
- **Detail, part one — how it was found** (NFR-CTX-05): the gate's verdict, then
  one line per round: its queries, how many facts they returned, and the judge's
  verdict with its gap sentence.
- **Detail, part two — what was actually sent** (R20): the injected block
  verbatim, under an `Appended to your message:` rule. This is the part that
  makes the model's context visible: what the note prints is the same string the
  prompt carried, down to the header wording, the fact ordering, the quotes and
  the hedge.

**Fidelity is structural, not a convention.** `prompts.py` gains

```python
def recall_block(recall: Recall) -> str:
    """The block appended to a user turn: header (plus hedge), digest, footer."""

def recalled_text(user_text: str, recall: Recall) -> str:
    return f"{user_text}\n\n{recall_block(recall)}"
```

so `_prompt_messages` and `RecallNote` render through one function and cannot
drift. The persisted block (U7.4) is `recall_block(recall)`'s output, which is
what the restore path displays.

`ui/app.py`:

- `show_enrichment(...)` → `show_recall(self.chat.last_turn.recall)` at both
  call sites, keeping the idempotence and the rollback behaviour — nothing is
  shown when the block did not survive trimming, because nothing was sent.
- `_recall_note_from(metadata)` beside `_consultation_note_from`, and a
  `show_recall_metadata` on `MessageBubble`, so reopening a conversation still
  shows what each turn was enhanced with.

**Pitfalls**

- `CollapsibleNote._detail_lines` collapses each line's whitespace and wraps it.
  Pass the block **split on `\n`**, one detail line per source line, or the
  digest's structure is flattened into a paragraph and "verbatim" becomes a
  claim the widget does not honour. Blank lines survive the base's
  `or [_DETAIL_INDENT + text]` fallback as indent-only lines; do not "fix" that
  branch away.
- The recall block is persisted on the **assistant reply** — that is the message
  whose prompt it entered — but the note is mounted under the **user turn** it
  enhanced. The restore loop at `ui/app.py:347` walks messages in order and
  mounts consultation notes on the bubble it is holding; it must keep a
  reference to the preceding user bubble to mount the recall note on. Mounting
  it on the reply instead is the easy mistake and puts the note one turn late.
- Do not truncate the block for display. A digest capped at
  `recall_digest_facts` is already bounded, and a note that shows nine of twelve
  facts is worse than no note: it is a wrong answer to "what did the model see".

**Tests** (`test_app.py`): with `recall_facts=True` and a scripted provider, a
turn against a group holding facts renders a `RecallNote` whose header names the
fact count, the rounds and the exit; its detail contains each round's queries and
the judge's gap; **the detail's block, rejoined, equals the substring the
provider actually received** — read `provider.calls[-1].messages[-1].content` and
assert the block is in it (R20, the one test that makes the feature true); a
`KNOWN` gate renders the header and no block; no note when the injection was
rolled back by trimming; reopening the conversation rebuilds an identical note
from metadata alone; a blank line inside the block survives into the rendered
detail.

---

### U9. Documentation

**Depends on:** U8

`README.md`: every variable from U7.6 in Configuration; a **Setup** line for the
embedding weights —

```
hf download sentence-transformers/all-MiniLM-L6-v2 \
  --local-dir "$AGENTCHAT_MODEL_ROOT/sentence-transformers/all-MiniLM-L6-v2"
```

— and a "How recall works" section: the gate, the rounds, the two views, that
retrieval never leaves the group, and that `AGENTCHAT_BACKEND=mock` needs no
embedding weights at all. Delete the summary/keyword paragraphs.

`AGENTS.md`: replace the `extraction.py` and `enrichment.py` lines in Layout with
`retrieval.py` and `llm/embedding.py`; note that the gate, rewrite, judge and
reseed prompt text lives in `prompts.py` like every other prompt, that
`retrieval.py` is the only module that may read `fact_embeddings`, and that
`render_transcript` survived the removal because `delegation.py` uses it.

**Verification:** every variable and path named in `README.md` exists.

---

## Test setup

The retrieval suite must be able to assert *which* fact came back, on a laptop,
in milliseconds. Three pieces make that possible.

**A deterministic embedder.** `HashingEmbedder` (U2) gives real similarity
behaviour — texts sharing significant tokens score higher — with no weights, no
GPU, and no cross-run drift. `build_embedder` returns it whenever
`backend == "mock"`, so a test opts into the whole pipeline by passing
`recall_facts=True` to `mock_settings` and never names an embedder.

**A fixed corpus.** `tests/factories.py` gains:

```python
def make_indexed_group(store, embedder, *, facts=GOLDEN_FACTS) -> Group:
    """A project group holding `facts` across three conversations, with
    phrases, quotes, and vectors already written — the state every retrieval
    test starts from."""
```

`GOLDEN_FACTS` is twelve facts over four topics (a database choice, a deployment
window, a person's dietary constraint, a travel plan), three per topic, two of
them deliberate near-duplicates from overlapping windows so read-time dedup has
something to remove, and one quoteless so the empty-evidence-view path is
covered. A table in the test file maps query → expected fact ids, and one
parametrised test walks it; a retrieval regression shows up as a named row, not
as a similarity number that moved.

A second group holds two facts using the *same vocabulary* as the first. Every
group-scoping assertion (R1) runs against it — scoping that is only tested
against unrelated text is not tested.

**A scripted loop.** The loop's call sequence is fixed: gate, `n` rewrites,
judge, reseed, `s·n` rewrites, judge, … So a test writes the script as a list and
reads the record back:

```python
provider = scripted_provider(
    "SEARCH",                                   # gate
    "postgres production database", "which database version", "db upgrade",
    "MISSING: no version number anywhere",      # judge
    "postgres 14 version\npostgres upgrade blocked",   # reseed
    ...
)
```

`ScriptedProvider` returns `""` past the end of its script, so a test that only
cares about the gate scripts one reply. Assertions about *which* prompt received
*which* input read `provider.calls[i].messages` — that record is why the suite
uses `ScriptedProvider` rather than the mock backend.

**Real weights.** `tests/test_embedding.py`'s `LocalEmbedder` cases and one
end-to-end recall are marked `skipif` on `AGENTCHAT_TEST_REAL_MODEL`, matching
`test_extraction_real_model.py`'s convention before it was deleted. They are the
only tests that need the embedding checkpoint on disk.

---

## Verification

1. `uv run pytest` passes; `uv run pytest -q` reports no collection errors from
   the three deleted suites.
2. `AGENTCHAT_BACKEND=mock uv run agentchat` starts, chats, and extracts facts
   with **no** embedding weights present.
3. GPU node, real backend. In a project group, hold three conversations with
   distinct, memorable content (a database choice with a version; a dietary
   constraint; a travel plan), long enough for each to produce facts. Confirm
   with `sqlite3 <data>/agentchat.db "SELECT COUNT(*) FROM fact_embeddings"` —
   two rows per fact that has quotes (R2).
4. New conversation, same group. Ask something answerable only from another
   conversation. The reply uses it, and the recall note names the number of
   facts, the rounds, and the exit (R13, R14). Expand it and confirm the quote
   shown is text that really appears in the source conversation (NFR-RAG-05).
5. Still on step 4's turn: compare the note's injected block against the same
   turn in the LLM transcript (`AGENTCHAT_LOG_LLM_IO=1`, the `chat`-labelled
   entry). They must match character for character — this is the check that the
   UI is showing the model's context and not a reconstruction of it (R20). Then
   switch conversations and back: the note is still there, rebuilt from
   metadata.
6. Same conversation: ask something generic ("write me a haiku"). The gate takes
   the short path — no recall note, and the transcript log shows one gate call
   and no rewrite calls (R6).
7. Ask something the group cannot answer. The loop runs to the cap, the reply
   hedges, and the note says the evidence may be incomplete (R12, R13).
8. Read the transcript log for step 4: every judge prompt contains the original
   message and none of the rewrites (R10); the reseed prompt contains the judge's
   gap sentence (R11); no round repeats an earlier round's query (R7).
9. Create a second group, ask the step-4 question there: nothing is recalled, and
   `metadata["recall"]["facts"]` is empty (R1, NFR-CTX-02).
10. `DELETE` a source conversation from the picker, then
    `SELECT COUNT(*) FROM fact_embeddings` — its vectors are gone (R17,
    NFR-S-04).
11. `AGENTCHAT_EMBED_MODEL=other-model` and restart: the next turn re-embeds the
    group once (log line names the count), the turn after embeds nothing, and the
    original vectors are still in the table (R4, R5).
12. `AGENTCHAT_RECALL_FACTS=0`: no gate call, no embedder load, no
    `fact_embeddings` read, and the app is otherwise unchanged (R16).
13. `AGENTCHAT_RECALL_ROUNDS=1` and `AGENTCHAT_RECALL_REWRITES=1`: the same
    question resolves in one round with two calls before the reply (R18).
14. Trigger `@ask_chef …` in a group holding facts: a consultation happens, no
    recall note appears, and the transcript shows no gate call (R19).
15. Time a recalled turn end to end on the GPU node. If it exceeds roughly 20
    seconds at the defaults, tune `RECALL_REWRITES` and `RECALL_ROUNDS` down and
    record the numbers in `README.md` — the cost table is the budget, and it is
    the product of the knobs.

## Definition of Done

Nine units landed, every test scenario passing, the fifteen verification steps
performed (3–11, 13–15 on a GPU node), `README.md` and `AGENTS.md` updated, and
the U1 grep clean.

## Not in scope

Ingesting user documents — text and PDF (NFR-RAG-02) — remains unimplemented and
is the next plan; this one indexes conversation facts only. Also out: an ANN
index, reciprocal-rank fusion in place of the union, cross-group recall, a facts
browser, editing or deleting a fact, re-embedding on a schedule, and reranking
hits with the chat model.
