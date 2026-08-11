# Stage 3 — Read path

**Status: v1 — detail plan for stage 3 of
`2026-08-10-002-memory-and-groups-skeleton.md`. Step 2 of the per-stage flow is
done: the tests are armed and `tests/` is closed. The implementer's target is
"The red baseline".**
**Design source: `memory-and-groups.md` v6, via the skeleton. This file expands
one stage; it does not restate the design.**

## Context

Stage 2 landed the write path: the six-stage extractor, the wire format and its
three pure parsers, `candidates()`'s BM25 retrieval, the chat-side adapters, and
the batching/flush wiring. Fragments accumulate. Nothing reads them back —
`select()` and `stable_core()` still raise `NotImplementedError("stage 3")` and
`rank.py` is a docstring.

Stage 3's goal is one line in the skeleton: **recall that can legitimately
return nothing.** What ships:

- an **encoder** — chosen, pinned, and behind a seam, because § 6.1's floor is a
  cosine threshold and stage 2 wrote no vectors at all;
- the **embedding lifecycle**: written at extraction time, stored as float32,
  and repaired incrementally for every fragment stage 2 left with a null
  `embedding_model`;
- `rank.py` for real — **floor → RRF fusion (four terms) → MMR**, with per-kind
  decay from `fragment_support.last_seen_at`;
- `select()` and `stable_core()` in the store, composing those pure functions
  over rows;
- `GroupMemoryStrategy`, § 2.2's two-block prompt layout, budgets as window
  fractions, and `ContextDecision.recalled`;
- the **startup guard** § 9 asks for: encoder versus `RECALL_FLOOR_MODEL`,
  encoder versus the corpus, with an affected-row count.

Still nothing the user can see: § 8.1's recall line is stage 6. This stage puts
the fragments into the prompt and records them on the decision; rendering them
is the next-but-one stage's job.

**Gate:** the 51 tests of the red baseline below green — the three activated
invariants (I-2 read side, I-11, I-13), the extended I-6 lint,
`tests/test_recall.py`, and the two tuning tests that now transcribe this
stage's constants — with the other 161 still green and no file under `tests/`
modified. Plus one run of `tests/test_recall_real_encoder.py`, which is the only
check that the pinned encoder loads, and the only check that `RECALL_FLOOR`'s
default means anything against it.

## What is already decided, and by whom

| Fixed by | Contract |
|---|---|
| skeleton, ratified | `select(scope_id, query, budget, *, tiers=Tier.EXTRACTED)`; both tiers apply the floor and the confidence-0 gate |
| skeleton, ratified | `MemoryStore` is **synchronous**; `ContextStrategy.build` is sync and recall sits on the reply path |
| skeleton, ratified | `GroupMemoryStrategy.build` takes a keyword `conversation`; § 1.3's protocol gains it at this stage's fold-back |
| skeleton, debt | the stage-0 I-11 test used `RECALL_FLOOR=1.0`; stage 3 must strengthen it to the literal case |
| `test_i2` (read) | `GroupMemoryStrategy(inner=…, store=…)`, `build(messages, context_window=…, conversation=…)`, `decision.recalled` |
| skeleton rule 2 | `rank.py` — and now `embed.py` — import no chat types; pure functions over data passed in |
| skeleton rule 5 | `apply()` is the only write path and the only write transaction; no LLM call inside one |
| skeleton rule 6 | every new number lands in `tuning.py` **and** § 9 in the same commit |
| § 6.1 | the floor is cosine on the query embedding, **never a judge** — no LLM call on the recall path |
| § 7 #15 | parked: decay runs from `last_seen_at`, so **recall must not write** |

**Nothing in this plan forces a skeleton change.** No protocol signature moves.
The additions — `EmbeddingProvider`, `RankItem`, `FragmentWrite.embedding_only`,
`ContextDecision.core`, and two read-only methods on the concrete
`SqliteMemoryStore` — are new names inside stage 3's own layer. Every place the
design doc reads differently from what ships is listed under "Documentation,
same commit" as a fold-back.

## Decisions taken before writing this plan

| Question | Answer |
|---|---|
| **Which encoder?** | `sentence-transformers/all-MiniLM-L6-v2`, pinned as the default of both `ENCODER_MODEL` and `RECALL_FLOOR_MODEL`. Section 1 argues it. |
| Where does it run? | In-process, **CPU**, through `transformers` (`AutoModel` + mean pooling + L2 normalisation) — no new dependency, and the laptop gate stays a laptop gate. Weights are read by path with `local_files_only=True`, like every other checkpoint here. |
| What if the weights are absent? | `build_encoder` returns `None` with a warning and the app runs. `select()` then returns `[]` — **fail closed**. Recall without a floor is § 6.1's failure mode (every group contributing its top-k to every prompt); returning nothing is the honest degradation, and the startup guard is what makes it visible rather than mysterious. |
| How do unit tests get vectors? | An `EmbeddingProvider` protocol with one method. `tests/factories.ScriptedEncoder` maps text → vector, so every floor and MMR assertion is a claim about the ranking code. Only `test_recall_real_encoder.py` loads real weights. |
| When are fragments embedded? | At **extraction**, in `MemoryExtractor.apply()`, for the writes that carry new text (new and revise; reinforce does not touch text, so it does not re-embed). Off the reply path by construction, and inside no transaction. |
| Stored how? | `struct.pack("<{n}f")` — float32, little-endian, 1536 bytes for this encoder's 384 dimensions. Vectors are stored unit-normalised so cosine is a dot product; `cosine()` normalises anyway, so a hand-built test vector still behaves. |
| What about stage 2's fragments? | They have neither an embedding nor an `embedding_model`, and § 9 promises the repair is incremental. `reembed(store, encoder)` takes `REEMBED_LIMIT` stale fragments per pass and writes them through `apply(..., embedding_only=True)`; `ChatMemory.extract` runs one pass after each extraction, so a group catches up in the background. Same path for a fragment embedded by a *different* model. |
| Does re-embedding count as a revision? | No. `embedding_only` writes the two columns and nothing else — no `revised_at`, no text, no confidence, and the fragment appears in neither `inserted` nor `revised`. A re-embed that stamped a revision would falsify the inspector's revision history at stage 6. |
| Token counting? | A **documented approximation** — the existing `len(text) // 4`, moved to `agentchat/core/tokens.py` so `rank.py` can import it without importing `core.context` (I-6). Section 6 says why the exact tokenizer is not on the table yet, and why the property test holds either way. |
| What does `select()` rank over? | The group's fragments in the requested tiers with `confidence > 0`, freshest first, capped at `RECALL_POOL_K`. **Not** the BM25 hits: BM25 is one of four fusion terms, and gating the pool on a lexical match would make the other three unreachable for a fragment that shares no word with the query. |
| Is the recall line the core plus the selection? | No. § 8.1 shows the query-selected fragments per turn and the stable core once, so `ContextDecision.recalled` is the **query-selected block alone** and the core rides on `ContextDecision.core`. Both together are exactly what gets spliced. |
| Who owns the final budget? | `GroupMemoryStrategy`. It is the outer strategy, so it re-checks the assembled total and trims — history first, then the selected tail, then the core tail — until it fits. Without that, `RecencyWindowStrategy`'s 256-token floor lets a small window overflow. |

---

## State of the tree — the tests are armed

Step 2 of the skeleton's per-stage flow is **done**. Every test edit stage 3
needs has landed; `tests/` is closed for the rest of this stage.

| Path | State |
|---|---|
| `tests/test_invariants.py` | **armed** — the three `@stage(3)` markers deleted; I-11 rewritten to the literal case; I-13 and I-2 (read) given an encoder and a positive control; the I-6 lint extended to `embed.py` |
| `tests/test_recall.py` | **written** — 45 stage-3 acceptance tests |
| `tests/test_recall_real_encoder.py` | **written** — 5 encoder smoke tests, skipped without `AGENTCHAT_TEST_REAL_ENCODER=1` |
| `tests/test_memory_store.py` | **armed** — the `select`/`stable_core` clauses of `test_unimplemented_methods_name_the_stage_that_owns_them` retired; the test now carries `@expires(stage=4)` |
| `tests/test_tuning.py` | **armed** — § 9's transcribed `DEFAULTS` gains this stage's seven constants and the changed `RECALL_FLOOR_MODEL` default |
| `tests/conftest.py` | **armed** — `expires(stage=N)` and its marker registration, the skeleton's retirement mechanism made mechanical |
| `tests/factories.py` | **extended** — `ScriptedEncoder`, `embedded(vector)`, and `observed_at` on citations (the only way to build a fragment with a chosen `last_seen_at`) |
| everything under `src/agentchat/` | untouched — the whole of the work below |

**Implementation touches no file under `tests/`.** A test that looks wrong is
escalated to the planner, never adjusted.

Five notes on the test edits, since four of them go beyond deleting markers and
the flow says that is the planner's call to make and to record:

- **I-11 is the skeleton's named debt, repaid.** The stage-0 test set
  `RECALL_FLOOR=1.0` and asserted `select()` returned `[]` — which proves an
  empty result is reachable and nothing about ordering. It now builds the
  literal case: one fragment with the best BM25 match, the freshest
  `last_seen_at`, citations from three conversations and importance 10, whose
  embedding is orthogonal to the query; and one weak on every term whose
  embedding is a perfect match. With the floor at 0.0 the first is returned
  **first** — the test's own premise, asserted rather than assumed — and with
  the floor in place it is absent while the weak one is returned.
- **I-13 and I-2 (read) were vacuous as written.** Both assert that something is
  *not* recalled, and `select()` fails closed without an encoder, so both would
  have passed against a store that recalls nothing ever. Each now runs with a
  `ScriptedEncoder` and a positive control: I-13 gains a live sibling that
  *must* come back, I-2 gains the same question asked inside a project group.
  An invariant test that cannot fail is the drift this suite exists to catch.
- **The I-6 lint covers `embed.py`.** Rule 2 names three files; `embed.py` is a
  fourth of the same kind — the encoder seam and the vector format are as
  persona-facing as the ranking that reads them. Adding the name turns a green
  test red on purpose (arming job 2) and is the one line of this arming that
  the skeleton's rule 2 wording does not already spell out.
- **`test_memory_store.py`'s stub pins are retired**, exactly as stage 2 retired
  the `candidates()` clause: the moment `select()` returns rows it stops
  raising, and a test asserting it raises would go red through no fault of the
  implementation. The remaining `purge_*` clauses now carry `@expires(stage=4)`,
  which is the skeleton's retirement marker retrofitted as it asked. `pytest -m
  expires` lists everything the next arming has to look at.
- **`test_tuning.py`'s `DEFAULTS` is § 9 transcribed**, and its whole value is
  being a second copy. Seven constants are added and one default changes
  (`RECALL_FLOOR_MODEL`, from empty to the pinned encoder), which pins section 9
  as the implementer's target.

---

## The red baseline

`uv run pytest -q` on the armed tree: **51 failed, 161 passed, 13 skipped**
(from 164 / 11 / 0 at the stage-2 gate). Every failure is substantive — a
missing module, a missing name, a missing constructor argument, or a missing
constant. This list is the target: stage 3 is done when all 51 are green, the
161 are still green, and the 13 skips read `stage 4`, `stage 5`, or a real-weights
flag.

**`tests/test_invariants.py` — 4**

| Test | Fails with |
|---|---|
| `test_i2_default_group_recalls_nothing` | `ImportError: cannot import name 'GroupMemoryStrategy'` |
| `test_i11_floor_is_applied_before_fusion` | `ModuleNotFoundError: agentchat.core.memory.embed` (via `factories.embedded`) |
| `test_i13_dormant_is_reachable_by_candidates_not_select` | `TypeError: SqliteMemoryStore.__init__() got an unexpected keyword argument 'encoder'` |
| `test_i6_memory_modules_never_import_chat_types` | `AssertionError: …/core/memory/embed.py is missing` |

**`tests/test_recall.py` — 45, by cause**

| Fails with | Count | Which |
|---|---|---|
| `ImportError: … 'RankItem' / 'admit' / 'diversify' / 'core_order' from …memory.rank` | 12 | the pure ranking tests |
| `TypeError: SqliteMemoryStore.__init__() … 'encoder'` | 11 | `select()`, `stable_core()`, the audit |
| `ModuleNotFoundError: agentchat.core.memory.embed` | 11 | the byte format, the re-embed path, the guard |
| `ImportError: cannot import name 'GroupMemoryStrategy'` | 6 | the two blocks, `recalled`, the budgets, the property |
| `ModuleNotFoundError: agentchat.core.tokens` | 4 | everything that costs a fragment in tokens |
| `TypeError: RecencyWindowStrategy.build() … 'conversation'` | 1 | the § 1.3 fold-back |

**`tests/test_tuning.py` — 2**

`test_defaults_match_the_section_9_table` and
`test_env_override_applies_to_one_constant_only`, both `AttributeError: 'Tuning'
object has no attribute 'claim_importance_default'`. They go green when
section 9's constants land.

**The baseline was checked against a throwaway reference implementation.** Every
assertion in `test_recall.py` and the three rewritten invariants was run against
a scratch build of `tokens.py`, `embed.py`, `rank.py`, the store's two reads and
`GroupMemoryStrategy` — the whole suite went to **212 passed, 13 skipped** — and
the scratch build was then deleted. This is not the implementation and the
implementer owes nothing to its shape; it exists because two tests were wrong
and arming is the last moment a test edit is legal. Both were fixed here: a
vector round-trip asserted `(0.6, 0.8)` where float32 returns
`(0.6000000238…, 0.8000000119…)` (the tests now use exactly-representable
values), and one re-embed test read its batch limit off the store instead of
passing it. Neither would have been visible behind an `ImportError`.

---

## 1. The encoder

**`sentence-transformers/all-MiniLM-L6-v2`.** Pinned as the default of
`RECALL_FLOOR_MODEL` — the value § 6.1 says the floor is meaningless without —
and of `ENCODER_MODEL`, which is what actually gets loaded. Four reasons, in the
order they mattered:

- **It runs on a CPU in milliseconds.** 22M parameters, 384 dimensions, 6
  layers. Recall is synchronous and sits between Enter and the first token
  (NFR-U-04), and the laptop gate has no GPU. A larger encoder buys accuracy
  this design cannot spend.
- **It needs no new dependency.** `transformers` is already here; the
  `sentence-transformers` package is a convenience wrapper over
  `AutoModel` + mean pooling + L2 normalisation, which is nine lines.
- **It is symmetric.** It was trained on paraphrase and STS pairs, so a question
  and a statement compare directly. `bge-small` and `e5-small` score better on
  retrieval benchmarks and require `"query: "` / `"passage: "` prefixes to do
  it; a caller that forgets one degrades silently, which is the same failure
  shape as an unpinned encoder.
- **Its similarity range is usable.** Related short sentences land around
  0.5–0.7 and unrelated ones near 0.0–0.15, which leaves `RECALL_FLOOR`'s 0.35
  somewhere sensible. That the number is still a `guess` is the point of § 9 —
  and `test_the_section_9_floor_sits_between_the_two` is the one test that
  checks it against the real encoder.

Rejected: reusing the chat model's hidden states (a 14B model on the reply path,
and no pooling head), and any encoder that must be downloaded at runtime.

### Where the weights come from

Read by path, `local_files_only=True`, exactly like `llm/local.py`'s
checkpoints. `Settings` gains `encoder_path`, defaulting to
`model_root / "sentence-transformers" / "all-MiniLM-L6-v2"`, overridable with
`AGENTCHAT_ENCODER_PATH`. One-time fetch, documented in the README beside the
model root:

```bash
hf download sentence-transformers/all-MiniLM-L6-v2 \
  --local-dir "$AGENTCHAT_MODEL_ROOT/sentence-transformers/all-MiniLM-L6-v2" \
  --include "*.json" "*.txt" "model.safetensors"
```

The `--include` matters: the repo also ships ONNX, OpenVINO and TensorFlow
copies of the same weights, and none of them is loaded here.

### The seam

```python
# core/memory/types.py — beside EvidenceItem, and as narrow
@runtime_checkable
class EmbeddingProvider(Protocol):
    model_id: str
    def encode(self, texts: Sequence[str]) -> list[tuple[float, ...]]: ...
```

```python
# llm/embed.py — the backend, where backends live
DEFAULT_ENCODER_ID = "sentence-transformers/all-MiniLM-L6-v2"

class LocalEncoder:
    def __init__(self, model_id: str, *, path: Path) -> None: ...
    @property
    def model_id(self) -> str: ...
    def load(self) -> None: ...          # idempotent; called on first encode
    def encode(self, texts) -> list[tuple[float, ...]]: ...
```

`encode` tokenises with `padding=True, truncation=True, max_length=256` (this
encoder's trained sequence length — a model property, not a § 9 constant, so it
stays a documented literal in this file), runs under `torch.inference_mode()` on
the CPU, mean-pools the last hidden state over the attention mask, L2-normalises
and returns plain float tuples. No torch object crosses the seam: `rank.py` and
the store see sequences of floats.

`config.build_encoder(settings) -> EmbeddingProvider | None` is the single
wiring point, next to `build_registry` and `build_memory_store`. It returns
`None` — with a `WARNING` naming the path it looked in — when the directory is
absent or the load fails, because a missing encoder must degrade recall, not
stop the app.

## 2. The embedding lifecycle

**`core/memory/embed.py`** (new, and in the I-6 lint from this stage on):

```python
def pack(vector: Sequence[float]) -> bytes          # struct "<{n}f"
def unpack(blob: bytes | None) -> tuple[float, ...] | None   # None if empty or misaligned
def cosine(a, b) -> float                            # 0.0 on length mismatch or zero norm

@dataclass(frozen=True)
class EmbeddingAudit:
    total: int; usable: int; missing: int; stale: int

def check_encoder(audit, *, encoder_id: str | None, tuning: Tuning, log=None) -> list[str]
def reembed(store, encoder, *, tuning: Tuning | None = None) -> int
```

**Write time.** `MemoryExtractor` takes `encoder=None` and, in `apply()` before
the writes reach the store, encodes the text of every non-`reinforce` write in
batches of `EMBED_BATCH`, setting `fragment.embedding` and
`fragment.embedding_model`. Reinforcement leaves text alone (§ 2.1), so it
leaves the vector alone. This is the one place extraction touches embeddings,
and it is inside no transaction.

**Repair.** `reembed` asks the store for up to `REEMBED_LIMIT` fragments whose
`embedding_model` is null or not the encoder's, encodes them in `EMBED_BATCH`
batches, and writes them back through `apply()` with `embedding_only=True`.
`ChatMemory.extract` calls it once per run when it has an encoder, so a group
left behind by stage 2 catches up over a few background extractions without a
blocking migration. It is also a plain callable — stage 6 gives it a button
beside backfill's.

```python
@dataclass(frozen=True)
class FragmentWrite:
    fragment: MemoryFragment
    citations: Sequence[FragmentCitation] = ()
    reinforce: bool = False
    #: Refresh `embedding`/`embedding_model` on an existing row and nothing
    #: else — not a revision, so no `revised_at` and no text. Mutually
    #: exclusive with `reinforce`.
    embedding_only: bool = False
```

`ApplyResult` gains `reembedded: list[int]`; an `embedding_only` write appears
there and in neither `inserted` nor `revised`.

## 3. `rank.py` — floor, fusion, diversity

Pure functions over data passed in. No store, no `Message`, no `Conversation`,
no SQL — the I-6 lint stays green and the persona pipeline can call every one of
these with its own rows.

```python
@dataclass(frozen=True)
class RankItem:
    id: int
    text: str
    kind: str
    importance: float
    last_seen_at: datetime
    conversation_count: int
    embedding: tuple[float, ...] | None = None
    bm25_rank: int | None = None      # None: the query's words never matched it

@dataclass(frozen=True)
class Ranked:
    item: RankItem
    similarity: float
    score: float

def decay_factor(kind, last_seen_at, *, now, tuning) -> float
def admit(items, query_vector, *, floor) -> list[tuple[RankItem, float]]
def fuse(admitted, *, now, tuning) -> list[Ranked]
def diversify(ranked, *, budget, cost, tuning) -> list[RankItem]
def core_order(items, *, now, tuning) -> list[RankItem]
def fill(items, *, budget, cost) -> list[RankItem]
def rank_and_select(items, query_vector, *, budget, cost, now, tuning) -> list[RankItem]
```

**`admit` is the floor, and it runs first** (I-11). An item with no embedding, or
one whose vector came from another encoder, never reaches it — the store leaves
`embedding=None` on those, which is the same fail-closed rule as a missing
encoder. Admission is `cosine(query, item) >= floor`.

**`fuse` is RRF over four rank orderings**, never over scores:

```
score(f) = Σ 1 / (RRF_K + rank_term(f))   for term in (bm25, decayed recency,
                                                       log support, importance)
```

- ranks are **competition ranks**: equal values share the best position, so a
  tie contributes equally to all tied items. This is what makes
  `test_terms_that_tie_contribute_equally` and the "reads ranks, not scores"
  test true, and it is the whole reason § 6 fuses ranks — a weight tuned on one
  group's BM25 magnitudes is wrong for the next group's.
- `bm25_rank is None` sorts to one shared worst position (`len(items) + 1`), so
  an unmatched fragment is ranked, not excluded. It can still be recalled on
  recency, support and importance — which is the point of having four terms.
- recency is `decay_factor(kind, last_seen_at)` = `exp(−α(kind)·DECAY_K·age_days)`,
  descending, with `alpha_for` already falling back to `ALPHA_FACT` for an
  unknown kind (rule 4).
- support is `log(1 + conversation_count)`, per § 6.3's refusal to count
  citations.
- the final sort is `(-score, id)`: deterministic to the last tie.

**`diversify` is MMR** with `MMR_LAMBDA`, and one implementation detail worth
stating because it is not in § 6.3: **the relevance term is the fused score
normalised by the top score**, so it lands in `(0, 1]` like the cosine
similarity it is traded against. Un-normalised, RRF scores cluster around
`4/(k+1) ≈ 0.066` and the diversity term would dominate every comparison. The
loop picks the highest MMR value, appends it when it fits the budget, and
continues over the rest — so the budget is a **ceiling**: a fragment too large
for what is left is skipped rather than ending the selection, and selecting
nothing at all is a legitimate outcome of an empty admitted set.

**`core_order`** is § 2.2's query-free ordering, `importance · decay_factor`,
with `fill` applying the budget. Same decay, same constants, no floor — there is
no query for the core to be relevant to.

## 4. `select()` and `stable_core()`

Both are compositions in `storage/memory.py`; neither holds a ranking rule of
its own.

```python
def __init__(self, path: Path, tuning: Tuning | None = None,
             encoder: EmbeddingProvider | None = None) -> None
```

`select(group_id, query, budget, *, tiers=Tier.EXTRACTED)`:

1. no encoder → `[]`, immediately. Fail closed.
2. `query_vector, = encoder.encode([query])`.
3. **the pool**: one statement, `group_id` scoped, `confidence > 0` (I-13's
   gate), `consolidated IN (…)` from `tiers`, `LEFT JOIN fragment_support`,
   `ORDER BY COALESCE(s.last_seen_at, f.created_at) DESC, f.id`, `LIMIT
   RECALL_POOL_K`.
4. **the BM25 ranks**: a second statement over `memory_fts` with the same
   `_fts_match` sanitising `candidates()` already uses, giving `{id: position}`;
   fragments absent from it keep `bm25_rank=None`.
5. rows → `RankItem`, with `embedding=unpack(f.embedding)` **only when
   `f.embedding_model == encoder.model_id`**, `last_seen_at` from the support
   view (falling back to `created_at` for a fragment the view has no row for),
   `conversation_count` likewise.
6. `rank.rank_and_select(...)` with `cost=estimate_tokens` and `now=_now()`.
7. map the chosen ids back to the `MemoryFragment` rows, in the returned order.

`stable_core(group_id, budget)`: the same pool with `Tier.CONSOLIDATED`, through
`rank.fill(rank.core_order(...))`. No query, no encoder, no floor — so it works
on a machine with no weights at all, and it returns `[]` before the first
consolidation, which § 2.2 says is correct rather than an error.

Two read-only helpers on the concrete store (not on the protocol — the protocol
is ratified and this is not the kind of thing that belongs behind it):

```python
def embedding_audit(self, encoder_id: str | None) -> EmbeddingAudit
def fragments_needing_embedding(self, encoder_id: str, limit: int) -> list[MemoryFragment]
```

`fragments_needing_embedding` orders by `id` so a bounded pass is resumable and
two passes never re-encode the same row.

**Neither read writes.** § 7 #15's decay-on-retrieval stays parked, and
`test_select_writes_nothing` dumps both tables around a `select()` to keep it
that way.

## 5. The startup guard

`check_encoder` takes an audit and returns the warnings it logged, so a test can
assert on them and the app can log them. Four cases, in this order:

| Condition | Warning |
|---|---|
| no encoder at all | recall is disabled until one loads, with the fragment count it makes unreachable |
| `RECALL_FLOOR_MODEL` empty | the floor was never calibrated against the encoder in force |
| `RECALL_FLOOR_MODEL` ≠ encoder id | both ids, and that the floor means something else against this one |
| any `stale` or `missing` fragments | **both counts**, and that they are invisible to recall until re-embedded |

The first three are mutually exclusive — one message about the pin, not three.
The corpus warning is independent and can accompany any of them. `ChatApp`
calls it once at construction, after `build_memory_store` and `build_encoder`,
so it lands in the same startup log as `log_effective_tuning`'s § 9 dump.

## 6. Token accounting — `core/tokens.py`

`CHARS_PER_TOKEN = 4` and `estimate_tokens` move out of `core/context.py` into
`agentchat/core/tokens.py`; `context.py` re-exports both so nothing importing
them today breaks. The move exists because `rank.py` needs the same unit and may
not import `core.context` (I-6).

**It stays an approximation, deliberately.** The exact tokenizer belongs to the
provider, and the strategy has no provider — reaching one would mean changing
`ContextStrategy.build`, which is a skeleton contract. So one estimator counts
the memory blocks, the history and the assertion, and the guarantee the property
test checks is exact under that unit: *the assembled context never exceeds the
budget the decision reports*. The slack against a real tokenizer is absorbed by
`reserve_for_response`, which is 512 tokens against a worst case of a few
percent. If that ever bites, the fix is a smaller `CHARS_PER_TOKEN`, not a
different design.

## 7. `GroupMemoryStrategy` and § 2.2's layout

In `core/memory/strategy.py`, the file rule 2 licenses to know both worlds.

```python
CORE_BLOCK_TITLE  = "Established context for this project:"
RECALL_BLOCK_TITLE = "Possibly relevant to the latest turn:"

def render_block(title: str, fragments: Sequence[MemoryFragment]) -> str
    # title, then one "- (kind) text" line per fragment

class GroupMemoryStrategy:
    name = "group-memory"
    def __init__(self, *, inner: ContextStrategy, store: MemoryStore,
                 tuning: Tuning | None = None) -> None
    def build(self, messages, *, context_window: int, reserve_for_response: int = 512,
              conversation: Conversation | None = None) -> ContextDecision
```

The sequence:

1. `scope = store.memory_scope(conversation.group_id)` — `None` without a
   conversation, and `None` for the default group (I-2). Either way the answer
   is `inner.build(...)` unchanged, with `recalled` and `core` left empty.
2. `budget = max(MIN_CONTEXT_BUDGET, context_window - reserve_for_response)`.
3. `core = store.stable_core(scope, int(context_window * CORE_BUDGET_FRACTION))`.
4. `query` = the content of the latest `user` message, raw — not
   `MessageEvidence`'s role-prefixed text, which exists for the extractor's
   prompt. Blank query → no `select()` call.
5. `selected = store.select(scope, query, int(context_window * SEL_BUDGET_FRACTION))`.
6. `inner.build(messages, context_window=max(MIN_CONTEXT_BUDGET, context_window - spent))`,
   where `spent` is what the two rendered blocks cost.
7. splice, in § 2.2's order: **system messages, core block, history, selected
   block, latest turn**. The cacheable prefix is everything up to the history;
   the volatile tail is the selected block and the turn being answered.
8. fit: while the total exceeds `budget`, drop the oldest history message; when
   only the latest turn is left, drop selected fragments from the tail, then
   core fragments from the tail. Dropped history joins `decision.dropped`, and
   `recalled`/`core` report what actually survived — which is what makes
   "`recalled` carries exactly what was spliced" true even at 256 tokens.

An empty `core` or `selected` splices **no block at all**: an empty header is a
sentence the model has to interpret, and § 8.1's `▸ 0` is rendered from
`recalled`, not from the prompt.

`ContextDecision` gains `recalled: list[MemoryFragment]` and
`core: list[MemoryFragment]`; `ContextStrategy.build` and
`RecencyWindowStrategy.build` gain the keyword `conversation` argument, ignored
by the latter.

## 8. Wiring — `chat.py`, `config.py`, `ui/app.py`

- `ChatService.stream_reply` passes `conversation=conversation` to
  `context_strategy.build`. That is the whole change to `chat.py`.
- `config.build_encoder(settings)` as in section 1; `Settings.encoder_path`.
- `ChatApp.__init__` builds the encoder once, hands it to `ChatMemory` and to
  `SqliteMemoryStore`, wraps its `RecencyWindowStrategy` in
  `GroupMemoryStrategy` when there is a memory store, and calls `check_encoder`.
  With `store="memory"` there is no memory store, so every `mock_settings()`
  test stays on exactly the path it has today.

`SqliteMemoryStore` is constructed in `build_memory_store`, which does not have
the encoder unless `build_encoder` runs first — so `build_memory_store` grows an
`encoder` parameter rather than reaching for one.

## 9. Constants — `tuning.py` and § 9, same commit

| Constant | Default | Status | Origin |
|---|---|---|---|
| `ENCODER_MODEL` | `sentence-transformers/all-MiniLM-L6-v2` | `guess` | the encoder recall embeds and queries with |
| `RECALL_FLOOR_MODEL` | *changed:* `sentence-transformers/all-MiniLM-L6-v2` | `guess` | was empty; § 6.1 requires the floor to name its encoder |
| `EMBED_BATCH` | 16 | `guess` | texts per encoder call, extraction and re-embed alike |
| `REEMBED_LIMIT` | 64 | `guess` | stale fragments one background pass repairs |
| `RECALL_POOL_K` | 200 | `guess` | fragments `select()` considers before the floor |
| `CLAIM_IMPORTANCE_DEFAULT` | 5.0 | `guess` | importance for a claim the model did not score |
| `CLAIM_CONFIDENCE_DEFAULT` | 0.5 | `guess` | confidence for a claim the model did not score |

The last two are stage 2's inlined fallbacks, promoted as the skeleton's rule 6
requires: `parse_claims` reads them off `Tuning` (it gains an optional `tuning`
argument, defaulting to `Tuning.from_env()`), and `Claim`'s dataclass defaults
read the same numbers from `CATALOGUE` rather than repeating the literals.
`MemoryFragment`'s own `importance`/`confidence` defaults stay as they are —
they mirror the schema's column defaults, not the extractor's fallback.

Everything else this stage needs is already in the table and is **read, never
inlined**: `RECALL_FLOOR`, `RRF_K`, `DECAY_K`, the five `ALPHA_*`,
`MMR_LAMBDA`, `CORE_BUDGET_FRACTION`, `SEL_BUDGET_FRACTION`.

## 10. Documentation, same commit

Fold-backs into `memory-and-groups.md`, each one or two sentences with its
reason, plus a changelog entry:

- **§ 1.3's `ContextStrategy` gains `conversation`**, as the skeleton scheduled:
  `build(messages, context_window, reserve, conversation=None)`. `memory_scope`
  is drawn taking a conversation and takes a `group_id` as landed in stage 2 —
  the strategy is what holds the conversation, and it passes the id.
- **`ContextDecision.core`** beside `recalled`: § 8.1 shows the query-selected
  fragments per turn and the stable core once, so the two blocks are reported
  separately rather than concatenated.
- **§ 6.1's "the embeddings are already computed for § 2.1's match step" is now
  false** and should say what is true: stage 2's `candidates()` matches on BM25,
  and embeddings are written at extraction time for the floor's benefit. The
  gate is not free; it costs one encoder pass per written fragment and one per
  turn.
- **§ 6.3 gains MMR's normalisation**: the relevance term is the fused score
  over the top score, because an RRF score and a cosine are not on the same
  scale.
- **§ 9 gains the seven rows of section 9** and the changed
  `RECALL_FLOOR_MODEL` default, plus a line on where the encoder's weights come
  from.

`README.md` gains the one-time `hf download` and `AGENTCHAT_ENCODER_PATH`. The
skeleton's changelog gets a stage-3 entry recording: the encoder choice, the
fail-closed rule, `ContextDecision.core`, `FragmentWrite.embedding_only`, and
that `embed.py` joined rule 2's list of persona-forward modules.

---

## 11. `tests/test_recall.py` — stage-3 acceptance *(written)*

Grouped as the file is. Gate items are marked.

| Test | Asserts |
|---|---|
| `test_the_floor_admits_only_what_clears_it` | admission is cosine ≥ floor |
| `test_a_fragment_without_an_embedding_is_never_admitted` | fail closed, per fragment |
| `test_a_floor_nothing_clears_admits_nothing` | the empty admitted set exists |
| `test_fusion_is_deterministic_and_independent_of_input_order` | **gate** — same inputs, same order, same scores |
| `test_fusion_reads_ranks_not_scores` | **gate** — rescaling a signal without reordering it changes nothing |
| `test_terms_that_tie_contribute_equally` | competition ranks |
| `test_an_unmatched_fragment_ranks_behind_every_bm25_hit` | `bm25_rank=None` ranks, not excludes |
| `test_an_untouched_open_question_ranks_below_an_equally_old_decision` | **gate** — § 6.2's per-kind α |
| `test_a_kind_nobody_registered_decays_like_a_fact` | rule 4 |
| `test_mmr_suppresses_a_near_duplicate_of_what_is_already_selected` | **gate** — with λ=1 as the control |
| `test_selection_stops_at_the_budget_ceiling` | the ceiling, in the pure layer |
| `test_the_core_ordering_folds_importance_into_the_same_decay` | § 2.2's `core_rank` |
| `test_select_returns_nothing_when_nothing_clears_the_floor` | **gate** — an empty selection is an outcome |
| `test_select_is_a_ceiling_not_a_target` | **gate** — both directions: budget and floor |
| `test_select_skips_the_consolidated_tier_unless_asked` | the ratified `tiers` contract |
| `test_select_is_scoped_to_its_group` | scope |
| `test_select_without_an_encoder_recalls_nothing` | the fail-closed rule, stated |
| `test_select_ignores_a_fragment_embedded_by_another_model` | per-fragment `embedding_model` gating |
| `test_select_tolerates_fts_syntax_in_the_query` | a user's words are not a query language |
| `test_select_writes_nothing` | **gate** — § 7 #15 stays parked |
| `test_stable_core_is_empty_before_the_first_consolidation` | **gate** — the cold start |
| `test_stable_core_needs_no_encoder` | no query, no floor |
| `test_stable_core_truncates_by_core_rank_when_it_overflows` | § 2.2's overflow ordering |
| `test_stable_core_excludes_dormant_fragments` | the confidence gate, both reads |
| `test_the_byte_format_round_trips` | float32, and what a misaligned blob does |
| `test_extraction_embeds_the_fragment_it_writes` | **gate** — where vectors come from |
| `test_reembedding_fills_in_what_stage_2_left_behind` | **gate** — § 9's incremental repair, ending in a successful recall |
| `test_reembedding_replaces_a_vector_from_another_encoder` | the swap case |
| `test_reembedding_is_bounded_and_resumable` | `REEMBED_LIMIT`, four passes, no double work |
| `test_reembedding_leaves_text_confidence_and_citations_alone` | `embedding_only` is not a revision |
| `test_the_guard_warns_when_the_encoder_is_unpinned` | **gate** |
| `test_the_guard_warns_when_the_encoder_is_not_the_one_the_floor_was_set_for` | **gate** |
| `test_the_guard_counts_the_fragments_a_swap_invalidated` | **gate** — the affected-row count, and that it is logged |
| `test_the_guard_is_silent_when_the_encoder_and_the_corpus_agree` | no warning theatre |
| `test_the_guard_says_so_when_there_is_no_encoder_at_all` | the fail-closed case is announced |
| `test_the_audit_counts_what_the_store_actually_holds` | the counts come from SQL, not from hope |
| `test_the_two_blocks_are_spliced_in_the_section_22_order` | **gate** — cacheable prefix, volatile tail |
| `test_recalled_carries_exactly_the_query_selected_block` | **gate** — and the core reported separately, per § 8.1's split |
| `test_an_empty_recall_splices_no_block_at_all` | no empty headers in the prompt |
| `test_the_query_is_the_latest_turn` | what `select()` is asked |
| `test_the_budgets_are_fractions_of_the_context_window` | § 9's fractions, not token counts |
| `test_a_conversation_the_strategy_is_not_given_is_chat_local` | the protocol carries only messages |
| `test_the_recency_window_strategy_ignores_the_conversation_keyword` | the § 1.3 fold-back |
| `test_the_assembled_context_never_exceeds_the_window` | **gate** — the property, over seven window sizes |
| `test_switching_to_a_smaller_model_shrinks_the_assembly_to_fit` | **gate** — NFR-CTX-04 |

The property tests are deterministic rather than `hypothesis`-driven: a seeded
`random.Random` builds a 61-turn conversation, and the assertion runs it against
every window from 32768 down to 256. No new dependency, and a failure is
reproducible by name.

### `tests/test_recall_real_encoder.py` — the encoder check *(written)*

| Test | Asserts |
|---|---|
| `test_the_encoder_reports_the_id_the_floor_is_pinned_to` | `ENCODER_MODEL` and `RECALL_FLOOR_MODEL` describe the same weights |
| `test_encoding_returns_one_unit_vector_per_text` | shape and normalisation — cosine is a dot product only if this holds |
| `test_a_related_pair_scores_above_an_unrelated_one` | mechanics: the ordering, never the margin |
| `test_the_section_9_floor_sits_between_the_two` | `RECALL_FLOOR`'s default against the pinned encoder — the one calibration check |
| `test_the_round_trip_through_the_store_admits_only_the_related_query` | embed at write, recall at read, floor deciding |

Its own flag, `AGENTCHAT_TEST_REAL_ENCODER=1`, rather than
`AGENTCHAT_TEST_REAL_MODEL`: these need no GPU and no cluster node, so a laptop
with the weights on disk can run them, and that is the machine the mock gate
exists for. Read at import, because `conftest` scrubs every `AGENTCHAT_*`
variable per test.

---

## Out of scope

No UI: § 8.1's recall line, the memory inspector's § 9 table and the re-embed
button are stage 6, and `ContextDecision.recalled` goes deliberately unrendered
here. No consolidation, so `stable_core` returns `[]` in practice until stage 5
writes the tier it reads — the cold-start path is the one this stage exercises.
No deletion (`purge_*` still raise `stage 4`). `candidates()` is left on BM25:
§ 2.1's diagram calls its match step an embedding comparison, and now that
vectors exist it could become retrieve-then-rerank, but that is a change to a
green stage-2 path with its own tests and no gate asking for it. Recorded as a
possibility, not done. § 6.1's relative drop-off (§ 7 #17) stays parked, as does
decay-on-retrieval (§ 7 #15).

## Risks

- **The floor's default is unvalidated until someone runs the encoder.** 0.35 is
  a `guess` attached, from this stage on, to a named model —
  `test_the_section_9_floor_sits_between_the_two` is the falsification step, and
  it needs the weights. If it fails, § 9's number moves; nothing else does.
- **Fail-closed recall is silent from inside a chat.** A missing encoder, a
  swapped one, or a corpus of stage-2 fragments all produce "0 memories
  recalled" and look identical to a group with nothing to say. The startup
  warning and the re-embed pass are the mitigations; § 8.1's line is what makes
  it legible, and that is stage 6.
- **`RECALL_POOL_K` is a truncation nobody sees.** A group with more than 200
  live fragments ranks only its 200 freshest, and the fusion never learns what
  it missed. That is a defensible cap for a chat client and a wrong one for a
  large corpus; it belongs in § 9 precisely so it is not mistaken for a law.
- **The token estimate is a proxy.** Every budget in this stage is denominated
  in `len(text) // 4`, so a model whose tokenizer is meaningfully denser gets a
  slightly larger prompt than the arithmetic claims. `reserve_for_response`
  covers the gap; section 6 says why closing it properly is a contract change.
- **Two encoder calls per turn in the worst case.** Recall encodes the query
  synchronously; a laptop CPU does that in single-digit milliseconds for
  MiniLM, but the first call also loads the model. `LocalEncoder.load` on app
  start, not on first recall, is the fix if the first turn feels slow — left
  undone because it is a startup cost either way.
- **The stage-2 `GraphBuilder` id collision is still there.** Two builders in
  one database both mint fragment ids from 1. Stage 3's tests pass explicit ids
  where they use two builders, as stage 2's did; stage 4's `test_i3` is the one
  that finally needs the shared counter.

## Changelog

- **2026-08-11** — v1. Detail plan for skeleton stage 3, with steps 1–3 of the
  per-stage flow done in one session: planned, armed, baseline recorded. Five
  test edits beyond deleting the three `@stage(3)` markers, each listed under
  "State of the tree" with its reason — the I-11 strengthening the skeleton
  named as a debt, falsifiability fixes to I-13 and I-2 (read), the I-6 lint
  extended to `embed.py`, the `select`/`stable_core` stub pins retired with
  `@expires(stage=4)` retrofitted onto what remains, and § 9's second copy
  extended. All 51 failures checked one by one for substance, and the whole
  baseline re-run against a throwaway reference implementation that took the
  suite to 212 passed — which caught two wrong tests (a float32 round-trip
  compared against decimal literals, and a re-embed limit read off the store
  instead of passed in) at the last moment a test edit was legal.

---

## Handover

### File manifest

| File | Change |
|---|---|
| `src/agentchat/core/tokens.py` | **new** — `CHARS_PER_TOKEN`, `estimate_tokens`, moved out of `context.py` |
| `src/agentchat/core/context.py` | re-export the two; `MIN_CONTEXT_BUDGET`; `ContextDecision.recalled`/`.core`; `conversation` keyword on the protocol and on `RecencyWindowStrategy.build` |
| `src/agentchat/core/memory/types.py` | `EmbeddingProvider` protocol |
| `src/agentchat/core/memory/embed.py` | **new** — `pack`/`unpack`/`cosine`, `EmbeddingAudit`, `check_encoder`, `reembed` |
| `src/agentchat/core/memory/rank.py` | **the stage's core** — `RankItem`, `Ranked`, `decay_factor`, `admit`, `fuse`, `diversify`, `core_order`, `fill`, `rank_and_select` |
| `src/agentchat/core/memory/store.py` | `FragmentWrite.embedding_only`; `ApplyResult.reembedded` |
| `src/agentchat/core/memory/extract.py` | `encoder=` on `MemoryExtractor`, embedding in `apply()`; `Claim`'s fallbacks read from `tuning` |
| `src/agentchat/core/memory/strategy.py` | `GroupMemoryStrategy`, `render_block`, the two block titles; `encoder=` on `ChatMemory` and the re-embed pass |
| `src/agentchat/core/memory/tuning.py` | the seven constants of section 9 |
| `src/agentchat/core/memory/__init__.py` | re-export the new names |
| `src/agentchat/storage/memory.py` | `encoder=`; `select()`, `stable_core()`, `_pool`, `_bm25_ranks`, `embedding_audit`, `fragments_needing_embedding`; `embedding_only` in `_upsert_fragment` |
| `src/agentchat/llm/embed.py` | **new** — `LocalEncoder`, `DEFAULT_ENCODER_ID`, `default_encoder_path` |
| `src/agentchat/config.py` | `Settings.encoder_path`; `build_encoder`; `encoder=` on `build_memory_store` |
| `src/agentchat/core/chat.py` | pass `conversation=` to `context_strategy.build` |
| `src/agentchat/ui/app.py` | build the encoder, wrap the strategy, call the guard |
| `docs/plans/memory-and-groups.md` | §§ 1.3, 6.1, 6.3, 9 fold-backs; changelog |
| `docs/plans/2026-08-10-002-…-skeleton.md` | stage-3 changelog entry |
| `README.md` | the one-time encoder fetch and `AGENTCHAT_ENCODER_PATH` |

Nothing under `tests/`. `consolidate.py` stays exactly as stage 0 left it.

### Expected tally after the test edits

| | Before (stage-2 gate) | Armed (now) | After stage 3 |
|---|---|---|---|
| failed | 0 | **51** | 0 |
| passed | 164 | 161 | 212 |
| skipped | 11 | 13 | 13 |

The 51: 4 in `test_invariants.py` (I-2 read, I-11, I-13, and the extended I-6
lint), 45 in `test_recall.py`, 2 in `test_tuning.py`. The three that moved out
of the passing column are the tuning pair and the I-6 lint — the same tests,
now transcribing constants and checking a file that do not exist yet. The 13
skips are **4 stage markers** (3 × `stage 4`, 1 × `stage 5`; down from 7, which
is this stage's own condition), `test_local`'s weights check, the 3 real-model
smoke tests, and the 5 new real-encoder ones.

### Verification

```bash
uv run pytest -q                    # the mock gate: 212 passed, 13 skipped, 0 failed
                                    # (from 161 / 13 / 51 at arming)
uv run pytest tests/test_recall.py -v
uv run pytest tests/test_invariants.py -v   # I-2 (read), I-11, I-13 now active;
                                            # 4 skips, all "stage 4"/"stage 5"
uv run pytest -m expires                    # what stage 4's arming has to retire

# the encoder check — the only run that touches real weights. No GPU needed.
hf download sentence-transformers/all-MiniLM-L6-v2 \
  --local-dir "$AGENTCHAT_MODEL_ROOT/sentence-transformers/all-MiniLM-L6-v2" \
  --include "*.json" "*.txt" "model.safetensors"
AGENTCHAT_TEST_REAL_ENCODER=1 uv run pytest -k real_encoder
```

One end-to-end check by hand, because the path nobody has watched is the one
that matters — **a project-group chat visibly using a memory a default-group
chat cannot see**:

1. Create a project group and put one conversation in it:
   ```bash
   sqlite3 data/agentchat.db \
     "INSERT INTO groups (id,name,kind,created_at) VALUES ('proj','Picker rewrite','project',datetime('now'));"
   ```
2. Run the app (`uv run agentchat` on a GPU node), start a chat, send one turn
   so the row exists, and quit. Move it into the group — extraction must run
   *inside* the group or I-2 throws the claims away:
   ```bash
   sqlite3 data/agentchat.db "UPDATE conversations SET group_id='proj' WHERE id='<id>';"
   ```
3. Reopen that conversation and plant something durable and specific — *"For
   this project we decided the conversation picker is a modal, never a
   sidebar."* — then keep talking until extraction fires (`EXTRACT_EVERY` = 6
   turns, or quit to force the flush). `SELECT text, embedding_model FROM
   memory_fragments` must show the claim with the pinned encoder id beside it:
   the write side and the embedding lifecycle in one query.
4. Start a **new conversation**, move it into the same group the same way,
   reopen it and ask *"remind me how the picker works?"*. The reply must use the
   planted fact, and `GroupMemoryStrategy`'s log line names what it spliced.
5. Ask the identical question in a **default-group chat**. The reply must not
   know — that is I-2's read side with a user watching it.
6. Finally, `AGENTCHAT_MEMORY_ENCODER_MODEL=some-other-encoder uv run agentchat`
   and confirm the startup log carries both warnings — the floor/encoder
   mismatch and the affected-row count — and that recall goes quiet rather than
   noisy.

The move dance in steps 2 and 4 is only necessary until stage 6's
group picker exists; § 2.5 forbids moving conversations between groups in the
product, and this is a developer working around the missing creation UI, not a
supported operation.
