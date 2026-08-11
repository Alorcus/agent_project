# Stage 1 — Schema and stores

**Status: v1.2 — detail plan for stage 1 of
`2026-08-10-002-memory-and-groups-skeleton.md`. Step 2 of the per-stage flow is
done: the tests are armed and `tests/` is closed. The implementer's target is
"The red baseline".**
**Design source: `memory-and-groups.md` v6, via the skeleton. This file expands
one stage; it does not restate the design.**

## Context

Stage 0 landed the drift detector: the tuning surface, the memory dataclasses,
the six-stage extraction skeleton, and the cumulative invariant suite (2 active,
15 skipped). Stage 1's goal is one line in the skeleton: **the database cannot
represent a forbidden state.**

Nothing user-visible ships here either. What ships is the storage layer the
next four stages write through:

- the **§ 1.1 schema** — `groups`, a corrected `conversations`,
  `memory_fragments`, `fragment_citations`, the `fragment_support` view and
  `memory_fts` — with I-1, I-7 and I-9 enforced by the database rather than by
  code that remembers to check;
- **`apply()`** — the single write path into memory state, the only place a
  write transaction opens, carrying the I-8 reachability check and the
  citation-conditional confidence raise that § 2.1 spends two pages on;
- the **knock-on corrections to plan 001** the design doc lists: NOT NULL
  `group_id`, the watermark pair, and the end of
  `list_conversations(group_id=None)`'s null overload;
- `factories.write(store, graph)`, which stage 0 deferred to here with a
  one-line note — the bridge from a hand-built object graph to real rows.

**Gate:** the 34 tests of the red baseline below green — the five activated
invariants (I-1, I-7, I-8 ×2, I-9), `tests/test_memory_store.py`, and the
stage-1 tests in `tests/test_storage.py` and `tests/test_core.py` — with every
stage-0 test still green and no file under `tests/` modified.

## What is already decided, and by whom

Stage 0's invariant tests were written with **real bodies against stage 1's
contracts**, which is the point of having written them early. They are the
acceptance spec and this plan is written to satisfy them as they stand:

| Fixed by | Contract |
|---|---|
| `test_i7`, `test_i8`, `test_i9` | `SqliteMemoryStore(path)` lives in `agentchat.storage.memory`, is **synchronous**, and is constructed from a `Path` |
| `test_i8_cycle_closing_edge_is_rejected` | a cycle-closing edge raises `StorageError`, not a bare `sqlite3` error |
| `test_i9` | both-sources and neither-source rows are rejected by the database itself (`sqlite3.IntegrityError` on a raw insert) |
| `test_i1` | `Conversation.group_id` is annotated `str`, and a null one fails through `SqliteStore.save` as `StorageError` |
| every stage 1–5 test | `factories.write(store, graph)` exists, is synchronous, and is idempotent |
| skeleton | `MemoryStore` is synchronous; `ConversationStore` stays async |

## Decisions taken before writing this plan

| Question | Answer |
|---|---|
| Where does the `MemoryStore` **protocol** live? | `core/memory/store.py`, not `storage/`. Stage 2's `extract.py` calls `memory_scope`, `candidates` and `apply`, and I-6's lint forbids that module importing `agentchat.storage`. Core declares the protocol; `storage/memory.py` satisfies it structurally. |
| `memory_scope(conversation)` → `memory_scope(group_id)` | § 1.3's parameter collides with I-6 head-on: § 2.1 has the *extractor* calling this method, and the extractor may not know what a `Conversation` is. Narrowing the parameter to the group id resolves it at the source rather than forcing stage 2 to invent an adapter for a one-field read. |
| `apply()` returns `ApplyResult`, not `None` | § 2.1 makes the confidence raise conditional on the citation insert actually inserting, and § 8.2 requires the `↑` count to report *confidence changes that happened*. Inside `apply()`'s transaction is the only place that fact exists. Returning `None` would mean recomputing it by diffing the table. |
| Watermark keyword now, or at stage 2? | Now. The columns are stage-1 schema, § 2.1 fully specifies the semantics ("advanced inside the same transaction as the writes"), and `apply()` is the transaction. Adding the keyword at stage 2 would be a protocol change and trigger a skeleton redraft for something already designed. |
| Who owns `groups` rows? | The conversation store. § 1.3 says `apply` is `MemoryStore`'s **only** write method, and § 1.2 draws the group tree against `cstore`. `MemoryStore` reads groups (`memory_scope`) and destroys them (`purge_group`, stage 4); it never creates one. |
| What happens to an existing `data/agentchat.db`? | It is **refused**, not rewritten. The design says "drop and recreate", and that stays true — but the `rm` is the user's, against a `StorageError` that names the file. An automatic rebuild deletes real conversations on first launch with no prompt. |
| How does `memory_fts` stay in sync? | SQL triggers on `memory_fragments`. Statements inside `apply()` would be correct today and wrong the moment stage 4's `DELETE` runs somewhere else; triggers make every write path correct by construction. |
| How much does `write()` build? | Group, conversation and message rows directly; fragments and citations through `store.apply()`. It writes memory state the way production does, so it exercises the I-7/I-8 paths the tests are about. |

---

## State of the tree — the tests are armed

Step 2 of the skeleton's per-stage flow is **done**. Every test edit stage 1
needs has landed; `tests/` is closed for the rest of this stage.

| Path | State |
|---|---|
| `tests/test_invariants.py` | **armed** — the five `@stage(1)` markers deleted |
| `tests/test_memory_store.py` | **written** — 21 stage-1 acceptance tests |
| `tests/test_storage.py` | **written** — 6 new tests; 2 existing ones moved to the new API |
| `tests/test_core.py` | `test_store_scopes_listing_by_group` moved to the new API |
| `tests/factories.py` | **written** — `write(store, graph)`; `make_conversation` now defaults to the seeded group |
| everything under `src/agentchat/` | untouched — the whole of the work below |

**Implementation touches no file under `tests/`.** A test that looks wrong is
escalated to the planner, never adjusted — the flow puts the last legal moment
for a test edit behind us, and that is what makes the list below a fixed target
rather than a negotiable one.

---

## The red baseline

`uv run pytest -q` on the armed tree: **34 failed, 90 passed, 11 skipped**.
Every failure below is substantive — a missing module, attribute, name or
behaviour. None is a typo or a stale API guess; the arming run was checked
test by test for exactly that. This list is the target: stage 1 is done when
all 34 are green, the 90 are still green, and the 11 skips all read `stage 2`
or later.

**`tests/test_invariants.py` — 5, the newly activated invariants**

| Test | Fails with |
|---|---|
| `test_i1_conversation_requires_a_group` | `AssertionError: assert 'str \| None' == 'str'` — the annotation, before the `StorageError` half is even reached |
| `test_i7_repeat_citation_is_a_noop` | `ModuleNotFoundError: agentchat.storage.memory` |
| `test_i8_cycle_closing_edge_is_rejected` | `ModuleNotFoundError: agentchat.storage.memory` |
| `test_i8_forward_edge_in_id_order_is_allowed` | `ModuleNotFoundError: agentchat.storage.memory` |
| `test_i9_citation_cites_exactly_one_source` | `ModuleNotFoundError: agentchat.storage.memory` |

**`tests/test_memory_store.py` — 21, all on a missing module**

Seven die in the `fresh()` helper on `ModuleNotFoundError:
agentchat.storage.memory`: `test_apply_honours_an_explicit_fragment_id`,
`test_memory_scope_is_none_for_the_default_group`,
`test_memory_scope_returns_the_group_id_for_a_project_group`,
`test_fragment_support_counts_citations_and_conversations`,
`test_unimplemented_methods_name_the_stage_that_owns_them`,
`test_write_round_trips_the_section_111_hierarchy`,
`test_opening_a_pre_memory_database_raises_naming_the_file`.

The other fourteen die one line earlier, importing the `apply()` payloads:
`ModuleNotFoundError: agentchat.core.memory.store`.

Both modules are stage 1's to write, so the whole file turns over together —
`SqliteMemoryStore` and `core/memory/store.py` are the first two things to
build, and nothing in this file reports anything more specific until they exist.

**`tests/test_storage.py` — 7**

| Test | Fails with |
|---|---|
| `test_default_group_is_seeded_and_is_not_a_memory_scope` | `AttributeError: 'SqliteStore' object has no attribute 'list_groups'` |
| `test_groups_round_trip_with_the_default_group_first` | `AttributeError: … no attribute 'save_group'` |
| `test_list_conversations_filters_by_group_id` | `AttributeError: … no attribute 'save_group'` |
| `test_saving_a_conversation_into_an_unknown_group_raises` | `Failed: DID NOT RAISE StorageError` — there is no foreign key yet |
| `test_list_all_conversations_spans_groups_and_scoped_listing_does_not` | `ImportError: cannot import name 'DEFAULT_GROUP_ID' from 'agentchat.core.models'` |
| `test_watermark_round_trips_on_the_conversation` | `ImportError: … 'DEFAULT_GROUP_ID'` |
| `test_in_memory_store_holds_i1_too` | `ImportError: … 'DEFAULT_GROUP_ID'` |

**`tests/test_core.py` — 1**

`test_store_scopes_listing_by_group` — `AttributeError: 'InMemoryStore' object
has no attribute 'save_group'`. It is the only baseline entry that names
`InMemoryStore`, and the reminder that the stub carries the same group surface
as the real store.

Two notes on reading the list. `test_i1` is the one test whose message will
change *during* implementation rather than at the end: fixing the annotation
moves it on to the `StorageError` assertion, which is a second, real failure,
not a regression. And the 21 identical `ModuleNotFoundError`s are shallow by
construction — they say nothing about whether `apply()` is right, which is what
the 21 assertions behind them are for.

---

## File manifest

| File | Change |
|---|---|
| `src/agentchat/storage/schema.py` | **new** — the DDL, `connect()`, default-group seed, old-database guard |
| `src/agentchat/storage/memory.py` | **new** — `SqliteMemoryStore` |
| `src/agentchat/core/memory/store.py` | **new** — `MemoryStore` protocol and the `apply()` payload types |
| `src/agentchat/core/memory/__init__.py` | re-export the new names |
| `src/agentchat/core/models.py` | `DEFAULT_GROUP_ID`; `Conversation.group_id: str`; the watermark pair |
| `src/agentchat/core/memory/models.py` | `FragmentCitation.fragment_id: int \| None` — annotation only |
| `src/agentchat/storage/base.py` | protocol + `InMemoryStore`: group methods, `list_all_conversations` |
| `src/agentchat/storage/sqlite.py` | schema moves out; groups, watermark columns, unfiltered listing |
| `src/agentchat/core/chat.py` | `new_conversation(group_id=DEFAULT_GROUP_ID)`, `list_all_conversations` |
| `src/agentchat/ui/app.py` | three listing call sites |
| `README.md` | the no-migrations note and how to reset the database |

Everything under `tests/` is already done — see "State of the tree". The
implementer's manifest is the `src/` rows and `README.md`, nothing else.

Nothing under `src/agentchat/llm/` is touched, and `core/memory/extract.py`,
`rank.py` and `consolidate.py` stay exactly as stage 0 left them.

---

## 1. `src/agentchat/storage/schema.py`

One DDL script, applied by **both** stores, because either may open the file
first — the invariant tests construct `SqliteMemoryStore` and then write
conversation rows through it.

```python
DEFAULT_GROUP_NAME = "Chats"

SCHEMA = "…"                                   # the statements below

def connect(path: Path) -> sqlite3.Connection:
    """Open `path` with foreign keys on and WAL set. Does not create tables."""

def ensure_schema(path: Path) -> None:
    """Create the schema if absent and seed the default group. Raises
    `StorageError` if `path` holds a pre-memory database."""
```

`connect()` carries the two pragmas the design depends on: `foreign_keys = ON`
(without it every `CASCADE` and every FK in this plan is decorative) and
`journal_mode = WAL` (§ 2.1's single-writer argument is about WAL).

### The statements

```sql
CREATE TABLE IF NOT EXISTS groups (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
  last_consolidated_at TEXT, created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY, title TEXT NOT NULL,
  group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  extracted_at TEXT, extracted_id TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS messages (…unchanged from today…);

CREATE TABLE IF NOT EXISTS memory_fragments (
  id INTEGER PRIMARY KEY, uuid TEXT NOT NULL UNIQUE,
  group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  origin_conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
  consolidated INTEGER NOT NULL DEFAULT 0,
  kind TEXT NOT NULL, text TEXT NOT NULL,
  confidence REAL NOT NULL, importance REAL NOT NULL, decay REAL NOT NULL,
  embedding BLOB, embedding_model TEXT, reasoning TEXT,
  created_at TEXT NOT NULL, revised_at TEXT);

CREATE TABLE IF NOT EXISTS fragment_citations (
  id INTEGER PRIMARY KEY,
  fragment_id INTEGER NOT NULL REFERENCES memory_fragments(id) ON DELETE CASCADE,
  source_message_id TEXT REFERENCES messages(id) ON DELETE CASCADE,
  source_fragment_id INTEGER REFERENCES memory_fragments(id) ON DELETE CASCADE,
  observed_at TEXT NOT NULL, quote TEXT,
  CHECK ((source_message_id IS NULL) <> (source_fragment_id IS NULL)));

CREATE UNIQUE INDEX IF NOT EXISTS idx_citation_message
  ON fragment_citations(fragment_id, source_message_id)
  WHERE source_message_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_citation_fragment
  ON fragment_citations(fragment_id, source_fragment_id)
  WHERE source_fragment_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_citation_source_fragment
  ON fragment_citations(source_fragment_id);

CREATE VIEW IF NOT EXISTS fragment_support AS
SELECT fc.fragment_id AS fragment_id,
       COUNT(*) AS citation_count,
       COUNT(DISTINCT m.conversation_id) AS conversation_count,
       MIN(fc.observed_at) AS first_seen_at,
       MAX(fc.observed_at) AS last_seen_at
  FROM fragment_citations fc
  LEFT JOIN messages m ON m.id = fc.source_message_id
 GROUP BY fc.fragment_id;

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts
  USING fts5(text, content='memory_fragments', content_rowid='id');
-- plus the three external-content sync triggers (ai / ad / au).
```

Points worth stating, because each is a decision rather than boilerplate:

- **No `CHECK` on `kind`** — rule 4. A persona kind must plug in without a
  schema change, and `Tuning.alpha_for` already falls back for unknown kinds.
- **The two partial unique indexes are I-7.** A composite primary key would not
  dedupe: SQLite treats NULLs as distinct in unique indexes, and both source
  columns are nullable (§ 1.1.1).
- **`CHECK ((a IS NULL) <> (b IS NULL))` is I-9**, and it is the same rule
  `FragmentCitation.__post_init__` already mirrors in object-land.
- **`fragment_citations.source_fragment_id` cascades.** Deleting a fragment
  removes the edges that cite it, which is exactly what makes stage 4's sweep
  find the next layer of orphans.
- **`conversation_count` joins `messages` with a LEFT JOIN**, so a
  fragment→fragment edge contributes to `citation_count` and not to
  `conversation_count`. § 6's `rank_support` orders on the latter, and a
  consolidated fragment's evidence is fragments, not conversations.
- **The FTS index is external-content.** The fragment text lives in
  `memory_fragments`; `memory_fts` holds only the index. The three triggers keep
  them in step on insert, update and delete.

### The old-database guard

```python
row = conn.execute("PRAGMA table_info(conversations)").fetchall()
if row and "extracted_at" not in {column[1] for column in row}:
    raise StorageError(
        f"{path} predates the memory schema and this prototype has no "
        f"migrations — delete the file and restart to recreate it"
    )
```

Plan 001's note says the resolution is to drop and recreate the development
database. This keeps that answer and moves the `rm` to the user: the file holds
real conversations, and an automatic rebuild would destroy them on first launch
with nothing asked and nothing said.

## 2. `src/agentchat/core/memory/store.py`

The protocol and the payload `apply()` consumes. In `core/`, not `storage/`,
so stage 2's `extract.py` can import it without tripping the I-6 lint.

```python
@dataclass(frozen=True)
class FragmentWrite:
    """One fragment's worth of write. ``citations`` are attached to
    ``fragment`` — their ``fragment_id`` is filled in from the row id, so a
    caller can build them before the fragment has one."""
    fragment: MemoryFragment
    citations: Sequence[FragmentCitation] = ()
    #: Reinforcement (§ 2.1): the row's text and confidence are left alone, and
    #: confidence rises by REINFORCE_STEP per citation that was genuinely new.
    #: False means the fragment's own values are authoritative — new and revise.
    reinforce: bool = False

@dataclass(frozen=True)
class Watermark:
    conversation_id: str
    extracted_at: datetime
    extracted_id: str

@dataclass(frozen=True)
class ConfidenceChange:
    fragment_id: int
    before: float
    after: float

@dataclass(frozen=True)
class ApplyResult:
    inserted: list[int]                     # § 8.2's "+ new"
    revised: list[int]                      # § 8.2's "~ revised"
    citations_added: int
    confidence_changes: list[ConfidenceChange]   # § 8.2's "↑ reinforced"

class MemoryStore(Protocol):
    def memory_scope(self, group_id: str) -> str | None: ...
    def candidates(self, group_id: str, query: str, k: int) -> list[MemoryFragment]: ...
    def stable_core(self, group_id: str, budget: int) -> list[MemoryFragment]: ...
    def select(self, group_id: str, query: str, budget: int, *,
               tiers: Tier = Tier.EXTRACTED) -> list[MemoryFragment]: ...
    def apply(self, writes: Sequence[FragmentWrite], *,
              watermark: Watermark | None = None) -> ApplyResult: ...
    def purge_conversation(self, conversation_id: str) -> None: ...
    def purge_group(self, group_id: str) -> None: ...
```

Stage 1 implements `memory_scope` and `apply`. The rest raise
`NotImplementedError` naming the stage that owns them, so a skipped invariant
test activated early fails loudly instead of passing against a stub that
returns `[]`. `test_unimplemented_methods_name_the_stage_that_owns_them` matches
on the message, so the mapping is fixed: `candidates` → `"stage 2"`, `select`
and `stable_core` → `"stage 3"`, `purge_conversation` and `purge_group` →
`"stage 4"`.

`FragmentCitation.fragment_id` widens to `int | None` in
`core/memory/models.py` — annotation only, no default and no `__post_init__`
change. A citation built against a fragment that has not been inserted yet
carries `None` there until `apply()` fills it in, which is the case extraction
is always in and the case `test_apply_mints_ids_and_writes_citations_against_them`
asserts on directly.

`ApplyResult`'s four fields are § 8.2's three counters plus the one number the
transaction knows and nobody else can recover: `confidence_changes` is what
actually moved, which § 2.1 is explicit is *not* the number of `reinforce`
decisions the LLM returned.

## 3. `src/agentchat/storage/memory.py`

```python
class SqliteMemoryStore:
    """Synchronous `MemoryStore` over the same file as `SqliteStore`."""
    def __init__(self, path: Path) -> None:
        self.path = path        # public: tests and factories write through it
```

Synchronous by the skeleton's ratified contract: recall sits on the reply path,
`ContextStrategy.build` is sync, and SQLite reads are fast enough not to earn an
async surface.

### `memory_scope(group_id)`

Loads the group and returns `group_id` if `Group.is_memory_scope()`, else
`None`; an unknown group is `None` too. The `kind` comparison stays in
`Group.is_memory_scope()` — I-2's single decision point is a claim about there
being exactly one, so the store must not grow a second one.

### `apply(writes, *, watermark=None)`

One transaction, in four **phases over the whole batch** — not four steps per
`FragmentWrite`. Every fragment row in the batch exists before the first
citation is inserted, because a citation may name a fragment that appears later
in the same batch: `test_i8_forward_edge_in_id_order_is_allowed` writes exactly
that edge, and a per-write loop would fail its foreign key. The same ordering is
what lets phase 2's reachability check see the batch's own earlier edges.

1. **Fragment upsert.** `INSERT … ON CONFLICT(id) DO UPDATE`. An explicit
   `fragment.id` is honoured (factories mint provisional ids and citation rows
   already point at them); a `None` id is minted by SQLite and **written back
   onto the dataclass**, which is what lets a caller build citations before the
   row exists. `reinforce=True` writes no text or confidence and requires an
   existing id — a reinforcement with nothing to reinforce is a `StorageError`.
   An update through the non-reinforce path is a revise: `revised_at` is set by
   the store, so no caller can forget it.
2. **Citations, one at a time.** For a fragment→fragment edge, the I-8
   reachability check runs first:

   ```sql
   WITH RECURSIVE reach(id) AS (
     SELECT :source
     UNION
     SELECT fc.source_fragment_id FROM fragment_citations fc
       JOIN reach r ON fc.fragment_id = r.id
      WHERE fc.source_fragment_id IS NOT NULL)
   SELECT 1 FROM reach WHERE id = :fragment
   ```

   A hit means the edge would close a cycle: `StorageError`, and the whole batch
   rolls back. `fragment == source` is the degenerate case and is rejected the
   same way. Because the check runs *inside* the transaction, after earlier
   inserts of the same batch, a cycle closed by two edges in one batch is caught
   too. Message→fragment edges are exempt: messages cite nothing.

   Per § 1.1.1, **id comparison is not a fast path** and must not be added — the
   forward edges § 2.6 needs make it unsound.
3. **The insert itself** is `ON CONFLICT DO NOTHING RETURNING id`. A returned
   row means the citation was genuinely new; only then, and only for
   `reinforce=True` writes, does confidence rise by `Tuning.reinforce_step`
   (clamped at 1.0), which is § 2.1's answer to the backfill-overlap case no
   transaction boundary can fix.
4. **The watermark**, if given: `UPDATE conversations SET extracted_at = ?,
   extracted_id = ? WHERE id = ?`. In this transaction or not at all — I-12.

No LLM call, no network, nothing unbounded runs between BEGIN and COMMIT (WAL
has a single writer). Stage 5 adds `last_consolidated_at` by the same pattern:
one more optional keyword, advanced in the same transaction as its writes.

### Four smaller contracts the acceptance tests pin

None of these is a new decision; each is somewhere the tests are more specific
than the prose above, and each is a place an otherwise reasonable
implementation goes wrong.

- **`writes` may be empty.** `apply([], watermark=…)` is the common outcome, not
  an edge case: § 2.1's `ignore` decision writes no fragment, and a run that
  read turns and believed none of them still has to move the watermark or those
  turns are re-read forever. An empty batch opens the transaction and advances
  the watermark like any other.
- **A `reinforce=True` write appears in neither `inserted` nor `revised`.**
  Those two lists are the § 8.2 counters for *content* that changed; a
  reinforcement changes evidence and possibly confidence, and it reports through
  `citations_added` and `confidence_changes`. A reinforce write with no new
  citation reports in nothing at all.
- **Confidence moves only on the reinforce path.** A non-reinforce write that
  adds a genuinely new citation sets confidence from the dataclass and
  contributes no `ConfidenceChange` — the revise path is authoritative, so there
  is no "change" to report beyond the value the caller gave.
- **Integrity failures inside `apply()` surface as `StorageError`.** The FK on
  `memory_fragments.group_id`, the `CHECK` of I-9, anything else the schema
  rejects: caught and re-raised, the way `SqliteStore` already treats
  `sqlite3.Error`. This is not in tension with `test_i9`, which asserts a bare
  `sqlite3.IntegrityError` — that test inserts over its own raw connection
  precisely to prove the *database* is the thing enforcing the rule, with no
  Python in the way.

## 4. Knock-on corrections to plan 001

Listed in the design doc's "Relationship to plan 001"; this is where they land.

**`core/models.py`.** `DEFAULT_GROUP_ID = "default"`, and
`Conversation.group_id: str = DEFAULT_GROUP_ID` — no longer `str | None`, which
is I-1 in the type as well as in the schema. Plus `extracted_at: datetime |
None` and `extracted_id: str | None`, which `SqliteStore` round-trips.

**`storage/base.py`.** `ConversationStore` gains:

```python
async def list_conversations(self, group_id: str) -> list[Conversation]: ...
async def list_all_conversations(self) -> list[Conversation]: ...
async def list_groups(self) -> list[Group]: ...
async def save_group(self, group: Group) -> None: ...
async def default_group(self) -> Group: ...
```

The null overload is gone: `group_id` is required, and "every conversation"
gets its own method. That is the nullable ambiguity I-1 exists to remove, and
it is the reason the design calls it out as a correction rather than a
preference. `list_groups` returns the default group first, then by
`created_at` — the tree in stage 6 wants a stable order and the default group
is the one that is always there.

`InMemoryStore` gets the same surface, seeds the default group, and **rejects a
conversation whose group does not exist**. It has no foreign keys to do it for
it, and a stub that quietly permits what the real store forbids is a stub that
lets I-1 break in every test that uses it.

**`core/chat.py`.** `new_conversation(group_id: str = DEFAULT_GROUP_ID)` and a
`list_all_conversations()` passthrough. **`ui/app.py`.** Three listing call
sites move to `list_all_conversations()`; the picker is group-blind until
stage 6.

`ConversationStore` now hands back `Group`, which lives in
`core/memory/models.py` — so `storage/` imports from `core/memory/`. That
direction is fine and the dataclass does not move: I-6's lint is about
`extract.py`, `rank.py` and `consolidate.py` not importing `agentchat.storage`,
and `storage → core` is the dependency direction `AGENTS.md` already states.

**`tests/factories.py`** (done at arming). `make_conversation`'s default
`group_id` moved from `"g1"` to `"default"`. Today the string is arbitrary
because nothing checks it; under the new FK `"g1"` is a group that does not
exist, and every test that saves a factory conversation without meaning
anything by its group would start failing on I-1. The tests that *do* mean
something by it name their group explicitly, which is what made the two listing
tests below a rename of intent rather than a repair.

It is the literal, not `DEFAULT_GROUP_ID` imported. `factories.py` is imported
at module scope by tests that must stay collectable before the constant exists,
and a lazy import inside `make_conversation` would turn every existing caller
red today for a reason that has nothing to do with stage 1 — polluting the
baseline below. `DEFAULT_GROUP_ID = "default"` is the definition the literal
tracks; the factory carries a comment saying so.

## 5. `tests/factories.py` — `write(store, graph)` *(written)*

```python
def write(store, graph: MemoryGraph) -> None:
    """Persist a built graph: group, conversation and message rows directly,
    fragments and citations through `store.apply` — memory state's only write
    path. Idempotent, so a test can write the same graph twice."""
```

Group, conversation and message rows go in over `schema.connect()`, with
`ON CONFLICT DO NOTHING` on the ids. Fragments and citations go through
`apply()`: the citations are grouped by `fragment_id` into one `FragmentWrite`
per fragment, `reinforce=False`, so a repeat write restates the same rows and
moves no confidence.

Two consequences the tests rely on:

- appending a citation to `graph.citations` and re-writing routes that edge
  through `apply()`'s reachability check — which is how
  `test_i8_cycle_closing_edge_is_rejected` gets its `StorageError`;
- writing the same graph twice exercises I-7's indexes rather than a
  test-local shortcut, which is what makes `test_i7` a test of the schema.

"Idempotent" here means the row-set is unchanged and no confidence moves — a
second `write` does restamp `revised_at`, because it goes down the revise path
like any other non-reinforce write. Nothing reads that column before stage 4,
and making the store special-case an unchanged text would be a second definition
of "revised" for a test factory's benefit.

The conversation and message inserts duplicate column knowledge that
`SqliteStore._save` already has. The alternative — reaching into that private
sync body from a test factory — couples the factories to the store's internals
instead, and the suite already reads these tables directly in a dozen places.

---

## 6. `tests/test_memory_store.py` — stage-1 acceptance *(written)*

In the tree and red until the store exists. Reads go through raw SQL on
purpose — asserting through the store that did the writing would let a bug in
the store hide itself. Gate items first.

| Test | Asserts |
|---|---|
| `test_apply_mints_ids_and_writes_citations_against_them` | a `None` id comes back set on the dataclass; the citation row points at the minted id even though it was built before the insert |
| `test_apply_honours_an_explicit_fragment_id` | provisional ids from `GraphBuilder` survive, which is what keeps hand-built citation rows valid |
| `test_double_apply_of_one_batch_moves_confidence_once` | **the gate's idempotence case** — second `apply` of an identical reinforce batch adds no citation and leaves confidence where the first put it |
| `test_reinforce_step_is_paid_per_new_citation` | two new citations, two steps, read from `Tuning` — never an inlined `0.1` |
| `test_reinforce_without_a_new_citation_leaves_confidence_alone` | the backfill-overlap case: the citation already exists, so nothing moves and `confidence_changes` is empty |
| `test_reinforce_never_rewrites_the_text` | § 2.1's "identical does not rewrite" |
| `test_revise_rewrites_text_and_sets_confidence_outright` | the non-reinforce path is authoritative, `revised_at` is stamped by the store, and `memory_fts` follows the new text |
| `test_apply_result_reports_new_revised_and_actual_confidence_changes` | § 8.2's three counters, with `↑` following citations rather than decisions |
| `test_cycle_closing_edge_rolls_the_whole_batch_back` | I-8's rejection is atomic — the unrelated fragment in the same batch is not written either |
| `test_self_citation_is_rejected` | the degenerate cycle |
| `test_watermark_advances_even_when_every_claim_was_ignored` | the pair lands from `apply([], watermark=…)` — the empty-batch case § 3 calls out |
| `test_a_rejected_batch_leaves_the_watermark_unmoved` | I-12's mechanism at store level, one stage before the invariant test activates |
| `test_memory_scope_is_none_for_the_default_group` | I-2's single decision point, and `None` for an unknown group |
| `test_memory_scope_returns_the_group_id_for_a_project_group` | the other half |
| `test_fragment_support_counts_citations_and_conversations` | two messages in two conversations → `citation_count` 2, `conversation_count` 2; a consolidated fragment's fragment-edges count as citations and not as conversations |
| `test_memory_fts_follows_inserts_revisions_and_deletes` | the triggers, including the delete path stage 4 will lean on |
| `test_an_unknown_kind_is_accepted` | rule 4 — no `CHECK` on `kind`, so a persona kind writes without a schema change |
| `test_a_fragment_in_an_unknown_group_is_rejected` | scope is NOT NULL at every tier (§ 1.1) |
| `test_opening_a_pre_memory_database_raises_naming_the_file` | the refuse-don't-destroy decision, from both stores |
| `test_unimplemented_methods_name_the_stage_that_owns_them` | `candidates`/`select`/`stable_core`/`purge_*` raise `NotImplementedError("stage N")` rather than returning an empty stub |
| `test_write_round_trips_the_section_111_hierarchy` | `factories.write` puts the § 1.1.1 figure in the database: 3 messages, 3 fragments, 6 citations, and the consolidated fragment's rows carry `source_fragment_id` with `source_message_id` null |

### `tests/test_storage.py` *(written)*

The conversation-store half, added alongside the existing suite:

| Test | Asserts |
|---|---|
| `test_default_group_is_seeded_and_is_not_a_memory_scope` | a fresh database has exactly one group and it is `kind="default"` |
| `test_groups_round_trip_with_the_default_group_first` | `save_group` / `list_groups`, default group first |
| `test_saving_a_conversation_into_an_unknown_group_raises` | the FK, from the conversation side (I-1's other half) |
| `test_list_all_conversations_spans_groups_and_scoped_listing_does_not` | the null overload is gone and both readings have a name |
| `test_watermark_round_trips_on_the_conversation` | `extracted_at` / `extracted_id` survive save and load |
| `test_in_memory_store_holds_i1_too` | the stub rejects an unknown group and accepts the default one |

Three existing tests moved to the new API at arming time, and are **done**:

| Test | Moved how |
|---|---|
| `test_storage.py::test_list_conversations_filters_by_group_id` | `"g1"`/`"g2"` became saved `Group`s; red on `save_group` |
| `test_storage.py::test_round_trip_title_group_messages_and_model_id` | asserts `group_id == "default"`; green already |
| `test_core.py::test_store_scopes_listing_by_group` | saves both groups on the `InMemoryStore` first, and the bare `list_conversations()` that meant "everything" became `list_all_conversations()`; red on `save_group` |

`test_list_conversations_orders_most_recently_updated_first` needed nothing: it
never names a group, so `make_conversation`'s new default carries it.

## 7. Documentation, same commit

- **`memory-and-groups.md` § 1.3 fold-back:** `memory_scope(group_id)` and
  `apply(writes, watermark) -> ApplyResult`, each with the one-sentence reason
  (I-6 collision; § 8.2's honest count). Changelog entry.
- **Skeleton changelog:** stage 1 landed, plus those two contract points
  ratified — the same treatment stage 0's two got.
- **README:** the database has no migrations; a schema change means
  `rm data/agentchat.db`, and the app says so on startup rather than crashing.

---

## Out of scope

No LLM call, no ranking, no UI, no `strategy.py`. `candidates()` is stage 2 even
though the FTS index it needs ships here — BM25 is ranking, and the skeleton
puts ranking past this gate. No `build_memory_store()` in `config.py`: nothing
constructs a `MemoryStore` outside tests until stage 2 wires the extractor in.
No in-memory `MemoryStore` implementation — nothing needs one, and writing a
second implementation of `apply()` doubles the surface where I-7 and I-8 can
diverge.

## Verification

```bash
uv run pytest -q                                    # 124 passed, 11 skipped, 0 failed
                                                    # (from 90 / 11 / 34 at arming)

uv run pytest tests/test_invariants.py -v
#   -> I-1, I-7, I-8 (both), I-9 and I-6 active; 10 skipped, reasons "stage 2".."stage 5"
uv run pytest tests/test_memory_store.py tests/test_storage.py -v

# the schema, by eye — the fastest way to catch a missing index or CHECK
uv run python - <<'PY'
from pathlib import Path
from agentchat.storage.memory import SqliteMemoryStore
import sqlite3
SqliteMemoryStore(Path("/tmp/stage1.db"))
with sqlite3.connect("/tmp/stage1.db") as conn:
    for (sql,) in conn.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"):
        print(sql, "\n")
PY

AGENTCHAT_BACKEND=mock uv run agentchat             # starts against a fresh data/, quit
```

Two negative checks worth running by hand once, because a guard nobody has seen
fire is not known to work:

- point the app at a database written before this stage and confirm the
  `StorageError` names the file instead of raising `sqlite3.OperationalError`
  somewhere deep;
- delete the `PRAGMA foreign_keys = ON` line in `connect()` and confirm
  `test_a_fragment_in_an_unknown_group_is_rejected` and
  `test_saving_a_conversation_into_an_unknown_group_raises` both fail. Every FK
  in this plan rests on that one line.

## Risks

- **`apply()` is doing a lot for one method** — upsert, reachability, dedupe,
  confidence, watermark. That is the design's shape rather than this plan's:
  § 1.3 makes it the only write method and § 2.1 makes it the only transaction,
  and splitting it would either open a second transaction or leak a half-written
  batch. Mitigation is that each responsibility is a named private helper and
  each has its own test above.
- **The old-database guard is a one-way door for anyone with real data.** It is
  deliberate, and the prototype-scope note in the design doc is what licenses
  it. If a second user ever appears, this is the first thing that has to change
  — the same sentence the design doc already carries about migrations.
- **`write()` duplicates the conversation and message inserts.** It will drift
  if those tables change without the factories being updated. The tests that
  read those tables directly would catch it, and the alternative couplings are
  worse; recorded here so the drift is expected rather than surprising.
- **`ApplyResult` and the `watermark` keyword are stage-2 shapes decided at
  stage 1.** Both are fully specified by § 2.1 and § 8.2, which is the test for
  whether a thing may be built ahead of its stage — unlike `Claim`/`Candidate`,
  which stage 0 correctly refused to invent because nothing specifies them yet.

## Changelog

- **2026-08-11** — v1.2. Step 2 of the skeleton's per-stage flow executed: the
  five `@stage(1)` markers deleted, the remaining test edits landed
  (`make_conversation`'s default group and the three existing tests § 6 names),
  and the suite run. All 34 failures verified substantive — one contract check
  worth recording is that `Tuning.from_env().reinforce_step`, which no failure
  reaches, does exist, so the reinforce tests are not built on a stale guess.
  New "The red baseline" section carries the list; the manifest and § 6 now
  read as done rather than pending, and § 4 records why the factory carries the
  literal `"default"` instead of importing `DEFAULT_GROUP_ID`.
- **2026-08-11** — v1.1. The acceptance tests were written against v1 and are
  now in the tree; this revision folds back what writing them settled. New
  "State of the tree" section and a baseline test count. `@stage(1)` markers
  corrected from four to five (I-8 is two tests), and the skip counts with
  them. § 3 gains the batch-phase ordering — `test_i8_forward_edge_in_id_order`
  writes a citation naming a fragment later in the same batch — and four
  smaller contracts the tests pin: the empty batch, what `inserted`/`revised`
  exclude, where confidence may move, and `StorageError` vs the raw
  `sqlite3.IntegrityError` `test_i9` wants. § 2 fixes the
  `NotImplementedError` stage per method. § 4 adds `make_conversation`'s
  default group and the `storage → core/memory` import direction. § 6's tables
  follow the names as written, and name the three existing tests that move.
- **2026-08-10** — v1. Detail plan for skeleton stage 1.
