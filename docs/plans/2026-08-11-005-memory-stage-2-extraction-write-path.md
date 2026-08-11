# Stage 2 — Extraction write path

**Status: v1 — detail plan for stage 2 of
`2026-08-10-002-memory-and-groups-skeleton.md`. Step 2 of the per-stage flow is
done: the tests are armed and `tests/` is closed. The implementer's target is
"The red baseline".**
**Design source: `memory-and-groups.md` v6, via the skeleton. This file expands
one stage; it does not restate the design.**

## Context

Stage 1 landed the storage layer: the § 1.1 schema, `apply()` as the only write
path and the only write transaction, the I-8 reachability check, and the
citation-conditional confidence raise. Nothing calls it yet except the test
factories.

Stage 2's goal is one line in the skeleton: **turns become fragments, durably
and idempotently.** What ships:

- the **extraction pipeline** of § 2.1, as the six named stages of rule 3
  (`observe → audit → propose → retrieve → resolve → apply`);
- the **wire format** — what the model is asked to emit and how it is parsed —
  which stage 0 deliberately left as `Any` and stage 1 never touched;
- `candidates()`, the BM25 retrieval the resolver decides against, carrying
  support **counts** and not citation rows;
- the **chat-side adapters** (`Message → EvidenceItem`, `Conversation →
  EvidenceSource`) that rule 2 puts in `strategy.py`, plus the batching and
  flush scheduling in `chat.py`;
- **reasoning traces** gated on the existing thinking toggle, and backfill as a
  callable with no UI.

Still nothing the user can see: § 8's status line is stage 6, and this stage
deliberately leaves `ApplyResult` unrendered.

**Gate:** the 40 tests of the red baseline below green — the three activated
invariants (I-2 write side, I-5, I-12), `tests/test_extraction.py`, and the two
flush tests appended to `tests/test_app.py` — with every stage-0/1 test still
green and no file under `tests/` modified. Plus one cluster run of
`tests/test_extraction_real_model.py`, which is the only check that the format
below survives contact with the two models this project actually runs.

## What is already decided, and by whom

| Fixed by | Contract |
|---|---|
| `test_i2`, `test_i12` | `MemoryExtractor(store, provider)` lives in `agentchat.core.memory.extract`, and `await extractor.run(source)` is the whole run |
| `test_i12` | a failed run raises (`ProviderError` propagates) and leaves the watermark unmoved; a successful one advances it in `apply()`'s transaction |
| `test_i5` | `EXTRACTION_PROMPT` is a module constant in `extract.py` and says "self-contained" |
| skeleton rule 2 | `extract.py` imports neither `Message` nor `Conversation` nor anything under `agentchat.storage` — enforced by the I-6 lint, which is active and green |
| skeleton rule 5 | `store.apply(writes, watermark=…)` is the only write; no LLM call inside a transaction |
| skeleton rule 6 | every new number lands in `tuning.py` **and** § 9 in the same commit |
| stage 1 | `MemoryStore` is synchronous; `candidates()` raises `NotImplementedError("stage 2")` until this stage fills it in |

## Decisions taken before writing this plan

| Question | Answer |
|---|---|
| What does `run()` take, given `extract.py` may not know `Conversation`? | A third narrow protocol, `EvidenceSource` (`id`, `scope_id`, `items`, `read_through`), beside `EvidenceItem` in `types.py`. `ConversationEvidence` in `strategy.py` binds it to a chat; an email thread binds it later. This is the adapter stage 0 § 3 said stage 2 would need, in the place rule 2 puts it. |
| Where do the evidence rows come from? | The adapter, not the store. § 2.1's diagram draws `Ex->>Store: messages after the watermark`, but the ratified `MemoryStore` protocol has no such method and adding one is a skeleton redraft. The live `Conversation` already holds its messages and its watermark, so the adapter slices the range and hands it over. Fold-back, not a contract change. |
| One LLM call or two? | Two per batch, plus one per revise. `propose` and `resolve` are separate calls because rule 3 makes them separate stages and because the **common case gets cheaper, not dearer**: a batch with nothing worth remembering ends after `propose` returns `[]`, whereas one combined call always carries k candidates in its prompt. § 2.1's "one LLM call resolves every claim against its candidates" stays literally true of `resolve`. |
| Evidence referenced by id or by index? | 1-based index into the numbered turns in the prompt. A 32-character hex id is tokens a small model spends and then hallucinates; an out-of-range integer is trivially detectable. |
| Who owns `confidence`? | `propose`. GUM's order is trace → proposition → confidence, and one owner per number keeps the resolve prompt down to a label and an index. `revise` recomputes it (§ 2.1), `reinforce` moves it only by `REINFORCE_STEP` inside `apply()`. |
| What does an unparseable reply do? | Yields zero claims and **still advances the watermark**. It is "nothing worth remembering", not a failure: leaving the watermark unmoved would re-read the same range forever. A `ProviderError` or a cancellation is the opposite — nothing commits, per § 2.1. |
| Does `candidates()` see the consolidated tier? | No: `consolidated = 0` only. Extraction resolves against the layer it owns; letting a raw claim `revise` a consolidated insight would rewrite compressed text without re-deriving it from its evidence. It **does** see dormant fragments — I-13, no confidence predicate. |
| How do support counts reach the prompt without changing a protocol signature? | `MemoryFragment` gains `support: FragmentSupport \| None`, populated by reads that join the `fragment_support` view. § 1.3's class diagram already draws that 1–1 derived association; `candidates(…) -> list[MemoryFragment]` is unchanged, so no contract moves. |
| Where does batching live? | `ChatService`. It is the only object that sees a turn complete, and `chat.py` is a licensed adapter. `ChatMemory` (in `strategy.py`) owns the adapting and the run; `ChatService` owns the task. |
| Does extraction load its own model? | No. It takes the resident provider (`ModelRegistry.active_provider`), so a background run never evicts the model the user is talking to. |

**Nothing here forces a skeleton change.** No protocol signature moves, no stage
boundary moves, no invariant changes. The three additions — `EvidenceSource`,
`MemoryFragment.support`, and the `PromptTurn` of section 3 — are new names
inside stage 2's own layer, which is what a detail plan is for. Every place the
design doc reads differently from what ships is listed under "Documentation,
same commit" as a fold-back.

---

## State of the tree — the tests are armed

Step 2 of the skeleton's per-stage flow is **done**. Every test edit stage 2
needs has landed; `tests/` is closed for the rest of this stage.

| Path | State |
|---|---|
| `tests/test_invariants.py` | **armed** — the three `@stage(2)` markers deleted; `ONE_CLAIM` rewritten to the section-2 format and `ONE_DECISION` added; the two extraction call sites wrapped in `ConversationEvidence`; `test_i2`'s `apply` stub given stage 1's real signature |
| `tests/test_extraction.py` | **written** — 35 stage-2 acceptance tests |
| `tests/test_extraction_real_model.py` | **written** — 3 cluster smoke tests, skipped without `AGENTCHAT_TEST_REAL_MODEL=1` |
| `tests/test_app.py` | **written** — 2 flush-wiring tests appended |
| everything under `src/agentchat/` | untouched — the whole of the work below |

**Implementation touches no file under `tests/`.** A test that looks wrong is
escalated to the planner, never adjusted.

Four notes on the test edits, since three of them go beyond deleting markers and
the flow says that is the planner's call to make and to record:

- **`ONE_CLAIM` is stage 2's to define.** Stage 0 wrote it as a placeholder and
  said so in a comment. It is now the section-2 propose reply, with `ONE_DECISION`
  beside it for the resolve reply.
- **Its `evidence` list covers all three turns of `test_i12`,** because that
  test reads I-12's "extracted" as "cited" — a message nothing was claimed about
  would fail an assertion the invariant does not actually make. The fixture is
  what makes the proxy honest; the alternative was rewriting an invariant test's
  assertion, which is a bigger edit for a smaller reason.
- **`ConversationEvidence` at the two call sites** is forced by the adapter this
  stage was told to build: `Message.content` is not `EvidenceItem.text`, and the
  translation may not happen inside `extract.py`.
- **`test_i2`'s `apply` stub** was `lambda decisions: …`, written blind against a
  signature stage 1 later settled as `apply(writes, *, watermark=None)`. It is
  now `lambda writes, **kwargs: …`. The test never reaches it (that is the
  point of the test), but a stub that would `TypeError` if it did is a trap
  laid for the next reader.

---

## The red baseline

`uv run pytest -q` on the armed tree: **40 failed, 124 passed, 11 skipped**
(from 124 / 11 / 0 at the stage-1 gate). Every failure is substantive — a
missing module, name, or an unimplemented store method. This list is the
target: stage 2 is done when all 40 are green, the 124 are still green, and the
11 skips still read `stage 3`, `stage 4`, `stage 5` or the real-model flag.

**`tests/test_invariants.py` — 3, the newly activated invariants**

| Test | Fails with |
|---|---|
| `test_i2_default_group_extracts_nothing` | `ImportError: cannot import name 'MemoryExtractor'` |
| `test_i5_extraction_prompt_demands_self_contained_text` | `ImportError: cannot import name 'EXTRACTION_PROMPT'` |
| `test_i12_watermark_advances_only_with_its_writes` | `ImportError: cannot import name 'MemoryExtractor'` |

**`tests/test_extraction.py` — 35, in four groups**

| Fails with | Count | Which |
|---|---|---|
| `ImportError: cannot import name 'parse_claims' from 'agentchat.core.memory.extract'` | 6 | the claim-parsing tests |
| `ImportError: … 'Outcome'` | 6 | the decision-mapping tests |
| `ImportError: … 'parse_revision'` / `… 'Candidate'` | 2 | the rewrite fallback; the candidate block |
| `ModuleNotFoundError: No module named 'agentchat.core.memory.strategy'` | 17 | everything that runs a pipeline, adapts a conversation, or schedules a batch |
| `NotImplementedError: stage 2` (`storage/memory.py:57`) | 4 | the four `candidates()` tests |

The four `NotImplementedError`s are the only ones that reach code stage 1 wrote,
and they are the marker stage 1 deliberately left: `candidates()` names the
stage that owns it rather than returning an empty stub, so an early-activated
test fails loudly instead of passing against nothing.

**`tests/test_app.py` — 2**

Both `AttributeError: 'ChatService' object has no attribute 'flush_extraction'`,
raised in the spy helper before the pilot does anything — the wiring does not
exist yet, which is the point.

Two notes on reading the list. The 17 `ModuleNotFoundError`s are shallow by
construction: `strategy.py` is the first file to write and nothing behind it
reports anything more specific until it exists. And one test was **fixed at
arming rather than left as written**: `test_a_run_cancelled_before_apply…`
waited on an `asyncio.Event` the missing module never set, so it hung the suite
instead of failing it. It now waits on the event *or* the run's own failure,
whichever lands first, and reports the real error. A test that can hang is a
test that stops the gate from being readable.

---

## 1. The pipeline

`MemoryExtractor` is the six stages of rule 3, in order, with `run()` composing
them:

```python
class MemoryExtractor:
    def __init__(self, store: MemoryStore, provider, tuning: Tuning | None = None) -> None: ...

    async def run(self, source: EvidenceSource, *, thinking: bool = False) -> ApplyResult | None:
        scope_id = self._store.memory_scope(source.scope_id)
        if scope_id is None or source.read_through is None:
            return None                                    # I-2, or nothing new
        items = self.audit(self.observe(source.items))
        claims = await self.propose(items, thinking=thinking) if items else []
        candidates = self.retrieve(claims, scope_id=scope_id)
        decisions = await self.resolve(claims, candidates, thinking=thinking)
        return self.apply(decisions, scope_id=scope_id, source=source)

    @staticmethod
    def observe(items: Sequence[EvidenceItem]) -> list[EvidenceItem]: ...
    @staticmethod
    def audit(items: Sequence[EvidenceItem]) -> list[EvidenceItem]: ...
    async def propose(self, items, *, thinking: bool = False) -> list[Claim]: ...
    def retrieve(self, claims, *, scope_id: str) -> list[Candidate]: ...
    async def resolve(self, claims, candidates, *, thinking: bool = False) -> list[Decision]: ...
    def apply(self, decisions, *, scope_id: str, source: EvidenceSource) -> ApplyResult: ...
```

Stage 0 wrote these as module-level functions with `Any` payloads. They become
methods because every one but `observe`/`audit` needs the store, the provider or
the tuning, and threading three keyword dependencies through six free functions
is the same object written out longhand. `audit` stays the identity function
with its comment pointing at GUM's contextual-integrity step — the whole reason
rule 3 exists.

- **`observe`** materialises the batch: drops items whose text is blank and
  sorts by `(created_at, id)`. The blank ones matter — a generation stopped
  before its first token leaves an empty assistant turn, which is not evidence
  but must still fall behind the watermark.
- **`retrieve`** calls `store.candidates(scope_id, query, tuning.candidate_pool_k)`
  **once**, with the claims' texts joined as the query, and returns `Candidate`s.
  Returns `[]` — and `resolve` then skips its LLM call — when there are no
  claims or the group holds no fragments yet, which is every group's first run.
- **`apply`** stages `FragmentWrite`s and makes the single `store.apply(...)`
  call. It shares a name with the store method deliberately: rule 5 says there
  is one write path, and this is the last place before it.

`run()` returns `None` when there was nothing to do and an `ApplyResult`
otherwise — § 8.2's three counters, ready for stage 6.

## 2. The wire format

The whole stage rests on this, so it is written down here in full rather than
left to be read out of the prompts.

### What the model is asked for

**Propose** (`EXTRACTION_PROMPT`) — a JSON array, one object per claim:

```json
[{"text": "The project stores conversations in SQLite.",
  "kind": "decision", "importance": 7, "confidence": 0.8, "evidence": [1, 2]}]
```

| Field | Rule on the way in |
|---|---|
| `text` | required, non-blank. The prompt demands it be **self-contained** — I-5, and the one instruction `test_i5` pins |
| `kind` | free text, defaulted to `"fact"`. Not validated against `KNOWN_KINDS`: rule 4 keeps `kind` open and `Tuning.alpha_for` falls back |
| `importance` | 1–10 (Generative Agents' poignancy scale), defaulted to the dataclass's 5.0. § 2.6's trigger fires on importance, so a constant here would make that trigger a disguised count |
| `confidence` | 0.0–1.0, clamped, defaulted to the dataclass's 0.5 |
| `evidence` | 1-based indices into the numbered turns. Unknown indices are dropped; **a claim left with none is dropped entirely** — I-4 at the write side |
| `why` | asked for only when thinking is on; becomes `memory_fragments.reasoning` |

**Resolve** (`RESOLUTION_PROMPT`) — a JSON array, one object per claim:

```json
[{"claim": 1, "relation": "identical", "candidate": 2}]
```

`relation` is GUM's three reranker labels plus `ignore`, and the mapping to the
four outcomes is done **in code**, not by the model:

| `relation` | Outcome | Notes |
|---|---|---|
| `unrelated` | `NEW` | insert, cite the evidence turns |
| `identical` | `REINFORCE` | `candidate` required; no rewrite (§ 2.1) |
| `similar` | `REVISE` | `candidate` required; pays the rewrite call |
| `ignore` | `IGNORE` | no row written — the common case |
| anything else, missing, or a `candidate` that does not resolve | `NEW` | "the unclear edge routes to `new`, since a duplicate is the cheapest mistake" |

A claim the reply never mentions is unclear too, and lands on `NEW`. A claim
mentioned twice takes its first resolution — § 2.1's one surviving rule.

**Rewrite** (`REWRITE_PROMPT`), only on the `similar` branch — a JSON object:

```json
{"text": "…", "confidence": 0.6}
```

Unparseable rewrite → the claim falls back to `NEW`. Blending two claims into
one text is the one revise error that loses information; a duplicate is not.

### How it is parsed

Three pure functions, so the format can be falsified without a store or a model:

```python
def parse_claims(reply: str, items: Sequence[EvidenceItem], *, thinking: bool = False) -> list[Claim]
def parse_decisions(reply, claims: Sequence[Claim], candidates: Sequence[Candidate]) -> list[Decision]
def parse_revision(reply: str) -> tuple[str, float] | None
```

`parse_claims` and `parse_decisions` never raise on model output. The reply is
cleaned (`<think>…</think>` removed and kept as the fallback trace, code fences
stripped), then the first balanced `[…]` / `{…}` is `json.loads`ed; a malformed
element is dropped, a malformed reply yields `[]`. Everything the model can get
wrong has a defined, testable landing place, because the alternative is an
exception on a background worker over text nobody controls.

`reasoning` is `why` if the claim carried one, else the shared `<think>` block,
else `None` — and **`None` unconditionally when thinking is off**, so the column
answers "was this claim reasoned about", not "did the model volunteer prose".

### Why this format, and how it fails cheaply

Every choice here is aimed at being falsified in one cluster run:

- **one array, one object per claim** — a model that emits prose instead
  produces zero claims and an empty run, which
  `test_real_model_extraction_reaches_apply` reports as a failure naming the
  raw reply (logged at DEBUG by `propose`/`resolve`);
- **small integers for evidence and candidates** — an index is either in range
  or it is not, so a hallucinated reference degrades to `NEW` rather than to a
  wrong citation;
- **labels, not free text, for the relation** — anything unrecognised routes to
  `NEW`, the recoverable error;
- **`<think>` tolerated everywhere** — `qwen3-14b` emits it natively and
  `phi-4-mini` answers the prompted instruction in prose, and the parser must
  not care which.

## 3. Prompts, and the `Message` problem

`LLMProvider.generate` takes a `Sequence[Message]`, and `extract.py` may not
import `Message`. The turns it sends are therefore `PromptTurn(role, content)`,
a two-field frozen dataclass in `types.py`: the providers read `.role` and
`.content` and nothing else, and `consolidate.py` needs the same thing at stage
5. This is the second tax the persona boundary charges, after `EvidenceItem`,
and it is the cheaper of the two ways to pay it — the other being a `Message`
import that the I-6 lint fails on.

`GenerationOptions` **is** imported from `agentchat.llm.base`: it is the LLM
seam, not a chat type, and the lint's forbidden list does not include it.
Extraction generates at `EXTRACT_TEMPERATURE` (0.0 — greedy, so the same turns
extract the same claims twice) and `EXTRACT_MAX_TOKENS`, with `thinking` passed
straight through so § 2.1's "reuse the generation path's own mechanism" is
literal: native via the chat template for `qwen3-14b`, prompted for
`phi-4-mini`, and neither known to `extract.py`.

Three renderers keep the prompt assembly testable and the golden-prompt check
honest: `render_evidence(items)`, `render_claims(claims)`,
`render_candidates(candidates)`. The last one is where § 2.1's "counts, not
rows" lives — a `Candidate` cannot carry citation rows, because the type has no
field for them.

## 4. `candidates()` — the retrieval the resolver decides against

```python
def candidates(self, group_id: str, query: str, k: int) -> list[MemoryFragment]
```

BM25 over `memory_fts`, joined to `memory_fragments` and the `fragment_support`
view, `WHERE f.group_id = ? AND f.consolidated = 0`, `ORDER BY bm25(memory_fts)
LIMIT k`. **No confidence predicate** — that omission is I-13, and it is the
only reason a dormant fragment can ever come back.

Two things that are easy to get wrong and are separately tested:

- **The query is a user's own words, and FTS5 MATCH is a query language.** A
  stray quote, a `-`, or the bare word `NEAR` is a syntax error raised from a
  background worker. The query is tokenised to word characters, deduped, quoted
  and joined with `OR`; a query with no usable token returns `[]` without
  touching the database.
- **`support` is attached to each returned fragment**, from the view, in the
  same statement. A second round-trip per candidate would be k+1 queries on a
  path that runs every batch.

The row → `MemoryFragment` mapper is a module-level helper, because stage 3's
`select()` and `stable_core()` need the identical one.

## 5. The chat adapters — `strategy.py`

New file, and the one stage 3 will add `GroupMemoryStrategy` to.

```python
@dataclass(frozen=True)
class MessageEvidence:            # Message -> EvidenceItem
    id: str
    text: str
    created_at: datetime
    def __init__(self, message: Message) -> None: ...   # text = f"{role}: {content}"

class ConversationEvidence:       # Conversation -> EvidenceSource
    def __init__(self, conversation: Conversation, *, ignore_watermark: bool = False) -> None: ...
    id: str; scope_id: str
    items: tuple[MessageEvidence, ...]
    read_through: tuple[datetime, str] | None

class ChatMemory:
    def __init__(self, store: MemoryStore, provider_for, tuning: Tuning | None = None) -> None: ...
    def pending(self, conversation) -> int
    def due(self, conversation) -> bool
    async def extract(self, conversation, *, thinking=False, ignore_watermark=False) -> ApplyResult | None
```

- **The role goes into `text`.** `EvidenceItem` has no role field and should not
  grow one: a chat turn's role and an email's sender occupy the same slot, and
  rendering evidence as text is exactly what an adapter is for.
- **`read_through` is the last message of the range, blank ones included.** The
  watermark records what was *read*, not what was claimed, or an empty turn at
  the end of a batch is re-read on every run for ever.
- **The range is `(created_at, id) > (extracted_at, extracted_id)`,** the
  lexicographic comparison § 2.1 spends a paragraph justifying. `ignore_watermark`
  is backfill: same path, null watermark, § 2.1's "not a separate mechanism —
  only a separate trigger".
- **`ChatMemory.extract` writes the watermark back onto the live `Conversation`**
  after `apply()` returns. The store row is authoritative, but the object the UI
  holds would otherwise stay stale and the next batch would re-read the range.

## 6. Batching and flushing — `chat.py`

```python
class ChatService:
    def __init__(self, registry, store=None, context_strategy=None, memory: ChatMemory | None = None)

    def schedule_extraction(self, conversation, *, thinking: bool = False) -> asyncio.Task | None
    async def wait_for_extraction(self) -> None
    async def flush_extraction(self, conversation, *, thinking: bool = False) -> ApplyResult | None
    def cancel_extraction(self) -> None
    async def backfill(self, conversation, *, thinking: bool = False) -> ApplyResult | None
```

`stream_reply`'s `finally` calls `schedule_extraction`, which starts a task only
when `memory.due(conversation)` — `EXTRACT_EVERY` messages past the watermark —
and only when no run is already in flight. A batch that is skipped because one
is in flight is not lost: `due` is derived from the watermark, so the next turn
asks again.

The task is fire-and-forget and **swallows its own failures** (logged, not
raised): a background run that dies must not surface on the reply path, and by
§ 2.1 it needs no retry mechanism — the watermark did not move, so the next run
picks the range up again. `MemoryExtractor.run` still raises, which is what
`test_i12` asserts on; the swallowing is the scheduler's, one layer out.

`flush_extraction` awaits any in-flight run, then extracts whatever is still
pending regardless of `EXTRACT_EVERY`. Three call sites in `ui/app.py` —
`action_new_conversation`, `_switch_to`, and `on_unmount` — which are the two
moments § 2.1 names ("switch and close") plus the new-chat path, which is a
switch by another name.

`on_unmount` awaits the flush behind `asyncio.wait_for(…,
EXTRACT_CLOSE_TIMEOUT)`. Unbounded, a 14B model's last batch hangs the quit;
skipped, "flush on close" is a promise the code does not keep and the last turns
wait until that conversation happens to be reopened.

## 7. `config.py` and the app wiring

```python
def build_memory_store(settings: Settings) -> MemoryStore | None:
    """`None` when the conversation store is non-durable — memory lives in the
    same file, and there is no in-memory implementation of `apply()`."""
```

`ChatApp` builds `ChatMemory(store, self.registry.active_provider)` when it gets
one, and passes `memory=None` otherwise, which is what keeps every existing
`mock_settings()` test (store `"memory"`) on exactly the path it has today.

## 8. Constants — `tuning.py` and § 9, same commit

| Constant | Default | Status | Origin |
|---|---|---|---|
| `EXTRACT_MAX_TOKENS` | 2048 | `guess` | must fit a reasoning trace **and** the JSON array; too small truncates the array into an unparseable reply |
| `EXTRACT_TEMPERATURE` | 0.0 | `guess` | greedy: the same turns should extract the same claims twice |
| `QUOTE_MAX_CHARS` | 240 | `guess` | how much of a turn a citation quotes for the inspector |
| `EXTRACT_CLOSE_TIMEOUT` | 30.0 | `guess` | seconds the flush-on-close waits before the app stops caring |

`EXTRACT_EVERY`, `REINFORCE_STEP` and `CANDIDATE_POOL_K` are already there from
stage 0 and are read, never inlined — `reinforce_step` by the store, the other
two by `ChatMemory` and `retrieve`.

## 9. Documentation, same commit

Three fold-backs into `memory-and-groups.md` § 2.1, each one sentence with its
reason, plus a changelog entry:

- **Extraction is two calls, not one.** The diagram's `decide(new turns,
  candidates)` is the `resolve` step; claim extraction is a call of its own, as
  the flowchart under "What the four outcomes are decided on" already draws it.
  The batch that finds nothing pays one call, not one call carrying k
  candidates.
- **The extractor is handed its evidence.** The diagram's `Ex->>Store: messages
  after the watermark` is not a `MemoryStore` method and must not become one —
  it would put chat rows behind a protocol `extract.py` shares with the persona
  pipeline. The adapter slices the range from the live conversation.
- **`candidates()` returns the extracted tier only,** and the claim payload
  carries `importance` and `confidence` — the first so § 2.6's trigger fires on
  something other than a constant, the second because GUM's order puts
  confidence with the proposition.

§ 1.3 gains one line: `MemoryFragment.support` is populated by reads that join
`fragment_support` and ignored by writes, which is the 1–1 derived association
the class diagram already draws. The design doc's § 9 table gains the four rows
of section 8 above. The skeleton's changelog gets a stage-2 entry, including
`EvidenceSource` as the third narrow protocol rule 2 implies but does not name.

---

## 10. `tests/test_extraction.py` — stage-2 acceptance *(written)*

Grouped as the file is. Gate items are marked.

| Test | Asserts |
|---|---|
| `test_claims_survive_a_thinking_block_a_code_fence_and_prose` | the parser finds the array inside whatever a local model wraps it in |
| `test_an_unparseable_reply_yields_no_claims` | garbage is zero claims, not an exception |
| `test_a_claim_with_no_usable_evidence_index_is_dropped` | I-4 at the write side |
| `test_an_unknown_kind_survives_parsing` | rule 4 |
| `test_a_reasoning_trace_is_kept_only_when_thinking_is_on` | **gate** — the toggle decides, not the model |
| `test_the_shared_thinking_block_is_the_fallback_trace` | `<think>` for qwen3, prose for phi-4 |
| `test_the_three_reranker_labels_map_to_the_three_write_outcomes` | unrelated/identical/similar → NEW/REINFORCE/REVISE |
| `test_ignore_resolves_to_the_ignore_outcome` | the fourth outcome exists and writes nothing |
| `test_an_unclear_relation_routes_to_new` | **gate** |
| `test_a_claim_the_resolver_never_mentions_routes_to_new` | silence is unclear too |
| `test_a_candidate_index_that_does_not_resolve_routes_to_new` | `identical` with nothing to match |
| `test_only_the_first_resolution_of_a_claim_counts` | § 2.1's one surviving rule |
| `test_an_unparseable_revision_falls_back_to_a_new_fragment` | the one revise error that loses information |
| `test_the_candidate_block_carries_support_counts_and_no_quotes` | § 2.1's "counts, not rows", through `candidates()` |
| `test_a_new_claim_lands_with_one_citation_per_evidence_turn` | the NEW path end to end, watermark included |
| `test_an_identical_claim_reinforces_without_rewriting_the_text` | text untouched, confidence +`REINFORCE_STEP`, read from `Tuning` |
| `test_a_similar_claim_rewrites_the_text_and_its_confidence` | the rewrite call happens **only** here (3 calls) |
| `test_a_batch_with_nothing_worth_remembering_still_advances_the_watermark` | the common case |
| `test_a_run_cancelled_before_apply_commits_nothing_and_the_retry_is_clean` | **gate** — kill before `apply()` |
| `test_re_running_an_extracted_conversation_changes_nothing` | **gate** — kill after `apply()`: the range is empty and the model is not called |
| `test_an_overlapping_backfill_double_counts_no_confidence` | **gate** — the case no transaction boundary can fix |
| `test_reasoning_is_stored_when_thinking_is_on_and_null_when_off` | **gate** |
| `test_extraction_asks_the_provider_for_the_thinking_mode_it_was_given` | the toggle reaches `GenerationOptions`, and I-5's instruction reaches the prompt |
| `test_candidates_are_scoped_to_their_group_and_ranked_by_bm25` | scope and order |
| `test_candidates_carry_support_counts` | the view, joined in one statement |
| `test_candidates_include_dormant_fragments_and_skip_the_consolidated_tier` | I-13's write half, and the tier decision |
| `test_candidates_tolerate_fts_syntax_in_the_query` | a user's words are not a query language |
| `test_message_evidence_renders_the_role_with_the_content` | the adapter, and where role went |
| `test_conversation_evidence_starts_after_the_watermark` | the lexicographic range, and `ignore_watermark` |
| `test_observe_drops_empty_turns_but_the_watermark_still_covers_them` | the cancelled-reply case |
| `test_extraction_waits_until_extract_every_messages_are_pending` | **gate** — batching |
| `test_a_flush_extracts_a_partial_batch` | **gate** — flush |
| `test_a_flush_with_nothing_pending_does_not_reach_the_model` | flush is not "extract again" |
| `test_extraction_is_cancellable` | NFR-U-04 |
| `test_backfill_re_reads_a_conversation_that_is_already_watermarked` | the callable stage 6 gives a button |

`tests/test_app.py` gains two, which are about **wiring** rather than about
extraction: `test_starting_a_new_conversation_flushes_the_pending_range` and
`test_quitting_flushes_the_pending_range`, both spying on
`ChatService.flush_extraction` through the pilot. They run against the mock
backend, whose lorem parses to zero claims — which is the correct behaviour of
an unparseable reply and exactly why they can assert on the call rather than on
the fragments.

### `tests/test_extraction_real_model.py` — the cluster check *(written)*

| Test | Asserts |
|---|---|
| `test_real_model_extraction_reaches_apply[phi-4-mini]` | a real 3.8B model's reply parses; every fragment written has text, a plausible confidence and ≥1 citation; the watermark moved |
| `test_real_model_extraction_reaches_apply[qwen3-14b]` | the same for the 14B |
| `test_real_model_extraction_stores_a_reasoning_trace` | qwen3 with thinking on stores a trace |

Mechanics only — never which claims the model found. Each failure message
carries the raw reply from the DEBUG log, so a format that does not survive
contact says so in one run instead of one bisect. The flag is read at **import**
time, because `conftest`'s autouse fixture scrubs every `AGENTCHAT_*` variable
per test — read in a body it would always look unset.

`-k real_model` also selects `test_local.py::test_real_model_loads_and_streams`,
which is the existing weights check and wanted: four tests in the one cluster
command, and the older one fails first if the checkpoints are simply not there.

---

## Out of scope

No UI beyond the three flush call sites: the § 8.2 status line, the memory
inspector and the backfill button are stage 6, and `ApplyResult` goes
deliberately unrendered here. No ranking — `select()` and `stable_core()` still
raise `NotImplementedError("stage 3")`, and `candidates()` is BM25 only, with
no embeddings, no floor, no fusion and no decay. No consolidation. No group UI,
so a project group is still something only a test or a `sqlite3` prompt can
create — which is why `build_memory_store` is wired but the app's default group
means the app itself extracts nothing until stage 6.

## Risks

- **The wire format is unvalidated against real weights until the cluster run.**
  That is the whole reason the smoke tests exist and the reason they assert
  mechanics: they are the falsification step, not a quality bar. If `phi-4-mini`
  cannot hold the format, the fallback is a per-model prompt variant, not a
  looser parser.
- **Two LLM calls per batch, on a local model.** Batching is what pays for it,
  and the empty-claim case still costs one call. If extraction turns out to lag
  the conversation badly, `EXTRACT_EVERY` is the knob, and § 9 already says the
  6 is a guess.
- **`parse_*` never raising is a deliberate blind spot.** A model that silently
  drifts to a near-miss format degrades to "nothing worth remembering" with the
  watermark advancing over it, and the only signal is an empty inspector.
  Stage 6's status line is where that becomes visible; the DEBUG log is the
  interim answer.
- **`MemoryFragment.support` is a read-only field on a dataclass that is
  otherwise written back.** Nothing writes it to the table and nothing should;
  if a later stage passes a `candidates()` result into `apply()`, the field is
  simply ignored.
- **Two `GraphBuilder`s in one database collide** — each mints fragment ids from
  1, and identical ids are one row, so the second builder silently revises the
  first's fragments. Found while writing
  `test_candidates_are_scoped_to_their_group_and_ranked_by_bm25`, which passes
  explicit ids to avoid it. Not fixed here: `test_i3` (stage 4) builds two
  groups the same way and is the test that needs the factory to grow a shared
  counter. Recorded so stage 4's planner meets it in a plan rather than in a
  debugger.

## Changelog

- **2026-08-11** — v1. Detail plan for skeleton stage 2, with steps 1–3 of the
  per-stage flow done in one session: planned, armed, baseline recorded. Four
  test edits beyond deleting the three `@stage(2)` markers, each listed under
  "State of the tree" with its reason. All 40 failures checked one by one for
  substance; one test was repaired at arming, where a test edit is still legal
  — `test_a_run_cancelled_before_apply…` blocked on an `asyncio.Event` that the
  missing module never set and hung the suite for fifteen minutes instead of
  failing it in a second. Two contract checks worth recording because no
  failure reaches them: `Tuning.from_env().candidate_pool_k` exists, and
  `GenerationOptions` carries `thinking`, so the resolve and reasoning tests
  are not built on stale guesses.

---

## Handover

### File manifest

| File | Change |
|---|---|
| `src/agentchat/core/memory/types.py` | `EvidenceSource` protocol; `PromptTurn` |
| `src/agentchat/core/memory/extract.py` | `Claim`/`Candidate`/`Decision`/`Outcome`, the three prompts, the three parsers, the three renderers, `MemoryExtractor` |
| `src/agentchat/core/memory/strategy.py` | **new** — `MessageEvidence`, `ConversationEvidence`, `ChatMemory` |
| `src/agentchat/core/memory/models.py` | `MemoryFragment.support` |
| `src/agentchat/core/memory/tuning.py` | `EXTRACT_MAX_TOKENS`, `EXTRACT_TEMPERATURE`, `QUOTE_MAX_CHARS`, `EXTRACT_CLOSE_TIMEOUT` |
| `src/agentchat/core/memory/__init__.py` | re-export the new names |
| `src/agentchat/storage/memory.py` | `candidates()` and the shared row → `MemoryFragment` mapper |
| `src/agentchat/core/chat.py` | `memory=`; `schedule_extraction`, `wait_for_extraction`, `flush_extraction`, `cancel_extraction`, `backfill` |
| `src/agentchat/config.py` | `build_memory_store(settings)` |
| `src/agentchat/ui/app.py` | wire `ChatMemory`; flush on new-conversation, on switch, on quit |
| `docs/plans/memory-and-groups.md` | § 2.1 and § 1.3 fold-backs; § 9's four new rows |
| `docs/plans/2026-08-10-002-…-skeleton.md` | changelog entry |

Nothing under `tests/` and nothing under `src/agentchat/llm/`. `rank.py` and
`consolidate.py` stay exactly as stage 0 left them.

### Expected tally after the test edits

| | Before (stage-1 gate) | Armed (now) | After stage 2 |
|---|---|---|---|
| failed | 0 | **40** | 0 |
| passed | 124 | 124 | 164 |
| skipped | 11 | 11 | 11 |

The 40: 3 invariants (I-2 write, I-5, I-12), 35 in `test_extraction.py`, 2 in
`test_app.py`. The 11 skips are 7 invariants reading `stage 3`/`stage 4`/`stage
5`, `test_local`'s weights check, and the 3 real-model smoke tests — none reads
`stage 2`, which is the gate's own condition.

### Verification

```bash
uv run pytest -q                    # the mock gate: 164 passed, 11 skipped, 0 failed
                                    # (from 124 / 11 / 40 at arming)
uv run pytest tests/test_extraction.py -v
uv run pytest tests/test_invariants.py -v   # I-2 (write), I-5, I-12 now active;
                                            # 7 skips, all "stage 3".."stage 5"

# the cluster check — the only run that tests the wire format itself.
# Four tests: the three stage-2 smoke tests plus test_local's weights check.
AGENTCHAT_TEST_REAL_MODEL=1 uv run pytest -k real_model
```

Two checks by hand, because a path nobody has watched is not known to work:

- run the app against a project group (`sqlite3 data/agentchat.db "INSERT INTO
  groups …"`, then move a conversation's `group_id`), send six turns, and watch
  `memory_fragments` fill; then quit mid-batch and confirm the flush lands the
  remainder;
- set `AGENTCHAT_MEMORY_EXTRACT_MAX_TOKENS=64` and confirm the truncated reply
  produces zero claims, an advanced watermark, and no traceback anywhere — the
  degradation every unparseable reply takes.
