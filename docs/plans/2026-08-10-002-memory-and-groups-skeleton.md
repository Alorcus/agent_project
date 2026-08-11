# Memory & Groups — implementation skeleton

**Status: v1 — the stage structure detailed plans are written against.**
**Design source: `memory-and-groups.md` v6. This file does not restate the design;
it slices it.**

## What this file is

The layer structure for implementing `memory-and-groups.md`, plus the rules that
keep per-stage planning from drifting. Three roles:

- **This skeleton (Fable):** stage boundaries, contracts, invariant-to-test
  mapping, acceptance gates. Changes here are expensive and deliberate.
- **Detail planner (Opus):** expands *one stage at a time* into an implementation
  plan — file-level changes, function signatures, and the stage's tests. A stage
  is planned only after the previous stage's gate is green. Never plan ahead of
  the gate: a detailed plan written against unimplemented assumptions is the
  drift this structure exists to prevent.
- **Implementer (Sonnet):** implements against the detail plan and its tests.
  The implementer does not write or modify acceptance tests. If a test looks
  wrong, that goes back to the planner, not into the test file.

**Re-planning trigger:** if implementing a stage forces a change to any contract
in this file (a protocol signature, a stage boundary, an invariant), stop; the
skeleton is redrafted first, with a changelog entry, and the affected detail
plan is regenerated. Contract changes never happen silently inside a stage.

## The per-stage flow

Stage N runs in five steps, and every test edit in the cycle happens in steps
1–2, nowhere else. `tests/` is planner-owned at every moment; implementer
commits never touch it. Deleting a `@stage(N)` skip marker **is** a test edit
and therefore planner work — it happens when the tests are armed, not inside
implementation.

1. **Plan** (Opus). The detail plan for stage N, including its test manifest:
   which skipped invariant tests activate (markers to delete) and which new
   acceptance tests are added.
2. **Arm the tests** (Opus, same working session). Delete the stage-N markers,
   write the new tests, run the suite. Every activated or new test must fail
   **for a substantive reason** — a missing module, table, or behaviour — not a
   typo or a stale API guess. Skipped tests were written blind against
   contracts, so this first execution is precisely where a mismatch surfaces;
   a test found broken here is fixed here, because this is the last moment a
   test edit is legal.
3. **Record the red baseline** in the plan: the list of tests now failing and
   why. That list is the implementer's target, verbatim.
4. **Implement** (Sonnet). Make the baseline green. No edits under `tests/`;
   a test that looks wrong is escalated to the planner, never adjusted.
5. **Gate** (coordinator, independently — not from the implementer's report).
   Zero failed; remaining skips all read `stage M` with M > N; every
   previously active test still green; the plan's manual verification steps
   run.

Confusion about this flow is itself an entrance for drift: an agent unsure who
owns an edit will guess, and the guess becomes precedent. When a step here is
ambiguous for a concrete case, the answer is decided in this file first, not
improvised in a working session.

## Cross-cutting rules (bind every stage)

1. **Dependency direction** stays `ui → core → llm/storage` (AGENTS.md). No
   stage introduces a crossing edge.
2. **Persona-forward code shape.** `extract.py`, `rank.py`, `consolidate.py`
   are written against two narrow protocols, not chat types:
   - `EvidenceItem` — `id: str`, `text: str`, `created_at: datetime`. The chat
     pipeline binds it to `Message`; the future persona pipeline binds it to an
     email. These modules never import `Message` or `Conversation`.
   - an opaque `scope_id: str` — bound to `group_id` here, to a person later.
   `strategy.py` and `chat.py` are the chat-specific adapters and may know both
   worlds. This is I-6's boundary made concrete in the signatures.
3. **Audit stage.** The extractor is structured as explicit stages
   (`observe → audit → propose → retrieve → resolve → apply`), with `audit` an
   identity function carrying a comment pointing at GUM's audit step. It exists
   so the persona pipeline can insert a contextual-integrity gate without
   restructuring.
4. **`kind` is open.** No `CHECK` constraint on `memory_fragments.kind`; the
   α lookup falls back to `ALPHA_FACT` for unknown kinds. Persona kinds plug in
   without a schema change.
5. **Transactions.** `apply(decisions)` is the only write path into memory
   state and the only place a write transaction opens. No LLM call ever runs
   inside a transaction.
6. **Constants.** Every number from § 9 lives in `core/memory/tuning.py` with
   its status; nothing is inlined. New constants discovered during
   implementation are added to § 9 of the design doc in the same commit.
7. **Tests run against the mock backend** (`uv run pytest`, no GPU). LLM
   behaviour in tests is scripted through the mock provider; extraction and
   consolidation tests assert on what reaches `apply()`, not on model output
   quality.

## Contract resolution carried into the design doc

`select()` in the design doc has two contradictory readings (§ 2.2 draws
extracted-only for the query-selected block; § 2.6 retrieves both tiers). The
skeleton fixes the contract as:

```
select(scope_id, query, budget, *, tiers=Tier.EXTRACTED) -> list[MemoryFragment]
```

Recall (§ 2.2) uses the default; consolidation (§ 2.6) passes
`Tier.EXTRACTED | Tier.CONSOLIDATED`. Both apply the floor and the
confidence-0 gate (I-11, I-13 asymmetry lives in `candidates()` vs `select()`,
not in the tier flag). Fold this back into `memory-and-groups.md`.

Two further contract points, surfaced by the stage-0 test author and ratified
here rather than left implicit in test files:

- **`MemoryStore` is synchronous.** `ContextStrategy.build` is sync and recall
  sits on the reply path by design; SQLite reads are fast enough not to earn an
  async surface. `ConversationStore` stays async. Stage 3 may renegotiate only
  via a skeleton redraft.
- **`GroupMemoryStrategy.build` takes the conversation.** `memory_scope()`
  needs it and the `ContextStrategy` protocol carries only messages; the
  strategy accepts a keyword `conversation` argument. The design doc's § 1.3
  protocol does not show this and should gain it at the stage-3 fold-back.
- **Known weakening to repay:** the stage-0 I-11 test uses an unclearable
  floor (`RECALL_FLOOR=1.0`) because no encoder exists yet. Stage 3's detail
  plan must strengthen it to the literal case — a fragment fusion would rank
  first sitting below the floor — once embeddings are real.

---

## Stages

Each stage ends at a **gate**: its acceptance tests pass, and every invariant
test from prior stages still passes. The invariant suite is cumulative and
never shrinks.

### Stage 0 — Invariant suite and scaffolding

**Goal:** the drift detector exists before anything it detects.

- `core/memory/tuning.py` with the full § 9 table, env overrides, statuses,
  and the startup log of effective values.
- The `EvidenceItem` protocol, `Tier` flag, and empty `core/memory/` package
  skeleton with the stage boundaries of rule 3 as named no-op functions.
- **The invariant test suite, written now, mostly xfail/skip:** one test per
  I-1..I-13, keyed to the table below. Tests activate as stages land — by the
  planner, at arming time, per the per-stage flow above; a test that cannot
  yet run is `skip("stage N")`, never deleted.
- Test factories for building groups/conversations/messages/fragments/citations
  directly at the store level, so later stages can construct hierarchies
  without the LLM.

**Gate:** suite runs green (active tests only); tuning surface importable and
logged.

### Stage 1 — Schema and stores

**Goal:** the database cannot represent a forbidden state.

- New schema per § 1.1: `groups`, corrected `conversations` (NOT NULL FK
  CASCADE, watermark pair), `memory_fragments` (incl. `embedding_model`),
  `fragment_citations` (surrogate key, two partial unique indexes, CHECK
  exactly-one-source), `fragment_support` view, `memory_fts`.
- `MemoryStore` protocol as in § 1.3 (`apply` sole write; `purge_group`, no
  `reseat_group`). `apply()` implements the reachability check (I-8) and
  rejects cycle-closing edges.
- `ConversationStore`: unfiltered listing gets its own method; null `group_id`
  overload removed. Development DB is dropped and recreated, per plan-001 note.
- Confidence raise inside `apply()` only on `ON CONFLICT DO NOTHING RETURNING`
  hit (`REINFORCE_STEP`).

**Explicitly out:** any LLM call, any ranking, any UI.

**Gate:** I-1, I-7, I-8, I-9 tests active and green, exercised through the
store API against hand-built graphs — including the forward-edge-then-cycle
case that killed id ordering, and the double-apply of an identical decision
batch (confidence moves once).

### Stage 2 — Extraction write path

**Goal:** turns become fragments, durably and idempotently.

- Extractor pipeline per § 2.1 with the rule-3 stage structure; batching on
  `EXTRACT_EVERY`, flush on switch/close, fire-and-forget cancellable worker.
- Watermark `(extracted_at, extracted_id)` advanced only inside `apply()`.
- Four-outcome resolution (new/reinforce/revise/ignore), one resolution per
  claim, unclear → new. Candidates carry support counts, not citation rows.
- Backfill = same path, null watermark; the explicit trigger UI is stage 6 —
  here it is only a callable.
- Reasoning traces gated on the thinking toggle.

**Gate:** I-2 (write side: default group extracts nothing), I-5 (prompt-level:
extraction prompt demands self-contained text; test is a golden-prompt check,
acknowledged unenforceable), I-12 active. Idempotence tests: kill the run
before/after `apply()`, re-run, assert no drift in fragments, citations,
confidence, or watermark. Overlapping backfill double-counts nothing.

### Stage 3 — Read path

**Goal:** recall that can legitimately return nothing.

- `GroupMemoryStrategy` wrapping `RecencyWindowStrategy`; budgets as window
  fractions; `ContextDecision.recalled`.
- `select()` per the contract above: floor → RRF fusion (four terms) →
  MMR. Per-kind decay from `last_seen_at`. `stable_core()` with `core_rank`
  overflow ordering; empty core before first consolidation is correct.
- Prompt layout per § 2.2 (cacheable prefix / volatile tail).
- Startup guard: encoder vs `RECALL_FLOOR_MODEL` and per-fragment
  `embedding_model`, warning with affected-row count.

**Gate:** I-2 (read side), I-11 (floor before fusion — test with fragments that
rank first yet sit below the floor), I-13 (dormant reachable by `candidates()`,
invisible to `select()`) active. Property test: assembled context never exceeds
the window after switching to a smaller model.

### Stage 4 — Deletion

**Goal:** derived state never outlives its evidence or its scope.

- `purge_conversation`: § 2.4 transaction, transitive sweep to fixpoint
  (recursive CTE), FTS rebuild.
- `purge_group`: § 2.4.1 — refuse default group, destructive cascade, no sweep
  loop, blast-radius counts returned for the confirmation UI.

**Gate:** I-3, I-4, I-10 active. Sweep tests on hand-built multi-level
hierarchies (stage 0 factories): message delete orphans h=1 which orphans h=2;
dormant fragments survive sweeps while cited; group delete leaves zero rows
scoped to the group and touches nothing outside it.

### Stage 5 — Consolidation

**Goal:** compression that cites its evidence.

- § 2.6 loop: durable trigger (`last_consolidated_at`, derived SUM with
  `consolidated = 0`), questions → per-question `select(tiers=both)` →
  insights with parsed evidence indices → new/reinforce/revise → `apply()`.
- Reinforce-with-newer-evidence exercises the I-8 reachability path in anger.

**Gate:** trigger fires on importance not count; self-excitation test (a run's
own output does not re-trigger); insights with unparseable citation indices are
dropped, not written citation-less (I-4); crash before `apply()` leaves
`last_consolidated_at` unmoved and the rerun is clean.

### Stage 6 — Visibility and group UI

**Goal:** § 8, plus the group management the rest assumed.

- Group picker modal at creation (the one chance § 2.5 allows), group tree,
  delete-with-blast-radius confirmation.
- Recall line per § 8.1 (incl. `▸ 0`, suppressed only in default group);
  extraction status line per § 8.2 (`↑` counts actual confidence changes,
  `· no traces` as literal text); memory inspector: fragments, citations with
  quotes, revision diffs, dormant marked, backfill trigger with progress,
  read-only § 9 table with status badges.

**Gate:** snapshot tests for the three § 8 surfaces across colour capabilities
(NFR-P-04); recall line count equals `len(ContextDecision.recalled)`; inspector
reaches every fragment including dormant.

---

## Invariant → stage map

| Invariant | Tested from | Mechanism under test |
|---|---|---|
| I-1 group NOT NULL | 1 | schema |
| I-2 default group not a scope | 2 (write), 3 (read) | `memory_scope()` |
| I-3 citations stay in-group | 4 | construction + purge tests |
| I-4 no citation-less fragments | 4, re-checked in 5 | sweep; consolidation parser |
| I-5 self-contained text | 2 | golden prompt (best effort) |
| I-6 memory/persona boundary | 0 onward | import-linting `core/memory/` (rule 2) |
| I-7 citation dedupe | 1 | partial unique indexes |
| I-8 acyclic | 1, exercised in 5 | reachability check in `apply()` |
| I-9 exactly one source | 1 | CHECK |
| I-10 dormant retained | 4 | sweep ignores confidence |
| I-11 floor before fusion | 3 | `select()` internals |
| I-12 watermark | 2 | transaction boundary |
| I-13 dormant reachable by extraction only | 3 | `candidates()` vs `select()` |

## Changelog

- **2026-08-11** — v1.1. Added "The per-stage flow": five explicit steps with
  test ownership pinned. Resolves an ambiguity the stage-1 planning session
  surfaced — who deletes a `@stage(N)` marker, and when. Answer: the planner,
  at arming time (step 2), because marker deletion is a test edit and the
  implementer never makes those. This also runs blind-written tests at the
  earliest possible moment, where a contract mismatch is cheapest to fix.
- **2026-08-10** — Stage 0 landed. Tuning surface, `EvidenceItem`/`Tier`
  protocols, memory domain dataclasses, the six-stage extraction skeleton, and
  the cumulative invariant suite (2 active, 15 skipped) are in. The
  `select(tiers=Tier.EXTRACTED)` fold-back promised below is done, in
  `memory-and-groups.md`.
- **2026-08-10** — v1. Six stages plus stage 0; cumulative invariant suite;
  persona-forward rules (EvidenceItem/scope protocols, no-op audit stage, open
  `kind`) bound as cross-cutting constraints; `select()` tier ambiguity between
  design §§ 2.2/2.6 resolved via a `tiers` parameter, to be folded back into
  the design doc.
