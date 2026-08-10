# Memory & Groups — implementation model

**Status: v2 — fragment/message corrected to an explicit many-to-many.**
**Stage: prototype, single user. Schema changes are free; see "Relationship to plan 001".**

Scope: the **chat memory mechanism** (NFR-S-02) and the **grouping** it is scoped
by (NFR-CTX-02). This is memory over the user's own conversations.

Explicitly *not* in scope, and not to be merged into these tables:

- **Persona extraction** — mining communication signals (Enron, `corpus/`) for what
  a message reveals about its sender and receiver. Different input, keyed by
  person, outlives every group. See `system_vision.md`.
- **Adaptive RAG** (NFR-RAG-\*) — retrieval over user-provided documents through a
  judge/rewriter loop. Memory shares its *primitives* (SQLite, FTS5, embeddings,
  rank fusion) but not its pipeline: an always-on, every-turn memory lookup must
  not pay for a judge loop.

## How to work with this file

Same protocol as `system_vision.md`. Report deltas, not full states — a few rough
sentences are enough and the model gets redrafted around them. ⏸ marks
"deliberately not deciding yet"; § 7 is the list. Every redraft gets a changelog
entry.

## Relationship to plan 001

`2026-08-10-001-feat-conversation-switching-plan.md` shipped `SqliteStore` with
`conversations.group_id TEXT` — nullable, no FK, no `groups` table — and deferred
memory scoping to follow-up work. This model supersedes that schema.

**No migration path is written, deliberately.** This is a prototype with a single
user, so the schema is free to change: the resolution is to drop the development
database and recreate it, not to version and migrate it. There is no
`PRAGMA user_version` and none is proposed. If that stops being true — a second
user, or data anyone minds losing — this is the first thing that has to change.

Two knock-on corrections to shipped code:

- `conversations.group_id` becomes NOT NULL with an FK to `groups`, so the
  database cannot represent a state I-1 forbids.
- `ConversationStore.list_conversations(group_id=None)` currently overloads null to
  mean "every conversation". That is the nullable ambiguity I-1 exists to remove;
  the unfiltered case wants its own method rather than a null argument.

---

# 1 — Static models

## 1.1 Data model

```mermaid
erDiagram
    GROUPS ||--o{ CONVERSATIONS : contains
    CONVERSATIONS ||--o{ MESSAGES : holds
    CONVERSATIONS ||--o{ MEMORY_FRAGMENTS : originates
    MEMORY_FRAGMENTS ||--o{ FRAGMENT_CITATIONS : cites
    MESSAGES ||--o{ FRAGMENT_CITATIONS : "is cited by"
    MEMORY_FRAGMENTS ||--o| MEMORY_FRAGMENTS : superseded_by

    GROUPS {
        text id PK
        text name
        text kind "default or project"
        text created_at
    }
    CONVERSATIONS {
        text id PK
        text title
        text group_id FK "NOT NULL, RESTRICT"
        text created_at
        text updated_at
    }
    MESSAGES {
        text id PK
        text conversation_id FK "CASCADE"
        text role
        text content
        text model_id "which model or adapter produced it"
        text created_at
    }
    MEMORY_FRAGMENTS {
        int id PK "rowid, for FTS5"
        text uuid UK
        text conversation_id FK "origin, CASCADE"
        text kind "fact decision constraint open_question artefact"
        text text "self-contained claim, pronouns resolved"
        real salience "matters to later turns in this project"
        int superseded_by FK "nullable, SET NULL"
        blob embedding "nullable"
        text created_at
    }
    FRAGMENT_CITATIONS {
        int fragment_id PK "CASCADE"
        text message_id PK "CASCADE"
        text observed_at "copy of the message timestamp"
        text quote "nullable, the supporting span"
    }
```

`FRAGMENT_CITATIONS` is an **associative entity resolving a many-to-many**: one
message can support many fragments, and one fragment can cite many messages. The
composite primary key means a given pair appears at most once.

Both directions carry meaning and they are not the same signal:

| Direction | Reading |
|---|---|
| many messages → one fragment | the claim was **corroborated** — stronger evidence |
| one message → many fragments | the message was **dense** — richer turn, not stronger claims |

Only the first feeds ranking. § 6 explains why the second must not.

Renamed from `fragment_sources` in v2. "Source" reads as an entity — the thing
being cited — which is what made the old class diagram look like fragment
ownership. "Citation" reads as the edge it is. Revert if you prefer the old name;
nothing else depends on it.

Plus a derived view and an FTS index, which carry no state of their own:

| Object | Purpose |
|---|---|
| `fragment_support` (view) | `citation_count`, `conversation_count`, `first_seen_at`, `last_seen_at` — aggregated from `fragment_citations` |
| `memory_fts` (FTS5) | external-content index over `memory_fragments.text`, BM25 ranking |

**Two modelling rules the diagram encodes.**

*Derive what is mutable, copy what is immutable.* `group_id` is **not** on the
fragment — group membership changes on every move, so a copy would drift, and
deriving it by JOIN makes moving a chat a single-row UPDATE. `observed_at` **is**
copied onto the citation row — a message timestamp never changes, so the copy is
safe, and it keeps ranking queries off the `messages` table.

*Origin versus evidence.* `conversation_id` is the conversation where the claim
first appeared. It fixes the group and owns the cascade. `fragment_citations` is
every message supporting the claim, possibly in other conversations of the same
group — that cross-conversation corroboration is the signal worth having, and
§ 2.4 / § 2.5 handle what it costs.

## 1.2 Module placement

Dependency direction stays one-way: `ui → core → llm/storage`. Nothing new
crosses it.

```mermaid
flowchart TD
    subgraph UI["ui/"]
        app["app.py<br/>Textual application"]
        tree["widgets.py<br/>group tree sidebar"]
        modal["modals.py<br/>group picker, memory inspector"]
    end

    subgraph CORE["core/"]
        chat["chat.py<br/>turn orchestration"]
        ctx["context.py<br/>ContextStrategy, ContextDecision"]
        strat["memory/strategy.py<br/>GroupMemoryStrategy"]
        extract["memory/extract.py<br/>merge / supersede / new"]
        rank["memory/rank.py<br/>rank fusion, diversity cap"]
    end

    subgraph STORAGE["storage/"]
        cstore["conversations.py<br/>ConversationStore"]
        mstore["memory.py<br/>MemoryStore"]
        db[("SQLite<br/>WAL")]
    end

    subgraph LLM["llm/"]
        prov["base.py<br/>LLMProvider"]
    end

    app --> chat
    tree --> cstore
    modal --> cstore
    modal --> mstore
    chat --> ctx
    chat --> extract
    chat --> prov
    ctx -.implements.-> strat
    strat --> rank
    strat --> mstore
    extract --> mstore
    extract --> prov
    cstore --> db
    mstore --> db
```

`memory/` is a new package under `core/`. It is the only place that knows what a
fragment is; `chat.py` sees a `ContextStrategy` and a fire-and-forget extractor.

## 1.3 Domain and protocol model

```mermaid
classDiagram
    class Group {
        +id: str
        +name: str
        +kind: str
        +created_at: datetime
        +is_memory_scope() bool
    }

    class Conversation {
        +id: str
        +title: str
        +group_id: str
        +messages: list~Message~
    }

    class Message {
        +id: str
        +role: str
        +content: str
        +model_id: str
        +created_at: datetime
    }

    class MemoryFragment {
        +id: int
        +uuid: str
        +conversation_id: str
        +kind: FragmentKind
        +text: str
        +salience: float
        +superseded_by: int
        +embedding: bytes
    }

    class FragmentCitation {
        +fragment_id: int
        +message_id: str
        +observed_at: datetime
        +quote: str
    }

    class FragmentSupport {
        +citation_count: int
        +conversation_count: int
        +first_seen_at: datetime
        +last_seen_at: datetime
    }

    class MemoryStore {
        <<Protocol>>
        +memory_scope(conversation) str
        +candidates(group_id, query, k) list~MemoryFragment~
        +stable_core(group_id, budget) list~MemoryFragment~
        +select(group_id, query, budget) list~MemoryFragment~
        +apply(decisions) None
        +purge_conversation(conversation_id) None
        +reseat_group(conversation_id, group_id) None
    }

    class ContextStrategy {
        <<Protocol>>
        +build(messages, context_window, reserve) ContextDecision
    }

    class RecencyWindowStrategy {
        +name: str
    }

    class GroupMemoryStrategy {
        +inner: ContextStrategy
        +store: MemoryStore
        +name: str
    }

    class ContextDecision {
        +messages: list~Message~
        +dropped: list~Message~
        +summarised: list~Message~
        +recalled: list~MemoryFragment~
        +notes: list~str~
    }

    Group "1" o-- "0..*" Conversation
    Conversation "1" o-- "0..*" Message
    Conversation "1" o-- "0..*" MemoryFragment : origin
    MemoryFragment "1" o-- "0..*" FragmentCitation
    Message "1" o-- "0..*" FragmentCitation
    MemoryFragment "1" -- "1" FragmentSupport : derived
    MemoryFragment ..> MemoryFragment : superseded_by
    ContextStrategy <|.. RecencyWindowStrategy
    ContextStrategy <|.. GroupMemoryStrategy
    GroupMemoryStrategy o-- RecencyWindowStrategy : wraps
    GroupMemoryStrategy --> MemoryStore
```

`FragmentCitation` hangs off **both** `MemoryFragment` and `Message` — that pair of
edges is the many-to-many. Neither parent owns it.

`superseded_by`, `embedding`, `quote` and `model_id` are nullable; Mermaid has no
notation for it. `ContextDecision.recalled` is the one addition to the existing
dataclass — without it the UI cannot show what memory contributed, which
NFR-CTX-05 asks for.

---

# 2 — Dynamic models

## 2.1 Write path — extraction

Runs **after** the assistant turn completes and **off** the reply path. Memory
extraction must never sit between the user pressing Enter and the first token.

```mermaid
sequenceDiagram
    autonumber
    participant Chat as ChatService
    participant Ex as MemoryExtractor
    participant Store as MemoryStore
    participant LLM as LLMProvider

    Chat->>Chat: assistant turn completes
    Chat-)Ex: schedule(conversation, new_messages)
    Note over Chat,Ex: fire-and-forget, cancellable worker

    Ex->>Store: memory_scope(conversation)
    alt default group
        Store-->>Ex: none
        Note over Ex: no extraction — ungrouped chats<br/>do not accumulate fragments
    else project group
        Store-->>Ex: group_id
        Ex->>Store: candidates(group_id, new_text, k)
        Store->>Store: BM25 over memory_fts, scoped to group
        Store-->>Ex: k candidate fragments

        Ex->>LLM: decide(new turns, candidates)
        LLM-->>Ex: zero or more resolved claims
        Note over Ex: one turn may yield several claims,<br/>and one message may be cited by several

        loop per claim
            alt new
                Ex->>Store: insert(fragment) plus citation rows
            else merge
                Ex->>Store: cite(fragment_id, message_ids, quote)
            else supersede
                Ex->>Store: insert(new) then set superseded_by(old)
            else ignore
                Note over Ex: nothing worth remembering.<br/>The common case — no row is written.
            end
        end

        Ex->>Store: sync memory_fts
    end
```

One LLM call resolves every claim. New, merge and supersede are the same judgement
over the same candidates, so splitting them into separate passes would multiply
the cost for nothing.

### What the four outcomes are decided on

Extraction is two steps. First, pull candidate **claims** out of the new turn.
Then resolve each claim against the fragments BM25 retrieved from the same group.
The branch is per claim — a turn can produce several, or none at all.

```mermaid
flowchart TD
    turn["new turn"] --> claims["extract candidate claims"]
    claims --> q0{"worth remembering<br/>beyond this turn?"}
    q0 -->|no| ignore["IGNORE<br/>no row written"]
    q0 -->|yes| match{"does a retrieved candidate<br/>share this claim's subject?"}
    match -->|no| new["NEW<br/>insert fragment, cite messages"]
    match -->|yes| compat{"does it assert<br/>something compatible?"}
    compat -->|compatible| merge["MERGE<br/>add citation to the existing fragment"]
    compat -->|conflicts| sup["SUPERSEDE<br/>insert new, mark old superseded_by"]
    compat -->|unclear| new
```

Merge and supersede both require a **subject match** and differ only on whether
the predicates agree. "Database choice → SQLite" said again in another chat is a
merge; "database choice → Postgres" is a supersede.

**The three errors do not cost the same**, which is why the unclear edge routes to
`new`:

| Error | Consequence | Recoverable |
|---|---|---|
| false `new` | a duplicate fragment | yes — a later merge can absorb it |
| false `merge` | two distinct claims collapsed under one text | partly — the citation is wrong but visible |
| false `supersede` | a true claim silently leaves the digest | only by inspecting superseded rows |

Two rules that fall out of this:

- **At most one resolution per claim.** If several candidates match, take the
  highest-ranked; do not emit a merge against each.
- **Merge never rewrites fragment text.** Only a citation is added. Otherwise text
  churns on every corroboration and `memory_fts` must re-index each time.

⏸ **"Same subject" is doing all the work here**, and it is currently a free-form
judgement by a 4B–14B model — the likeliest failure point in the whole design. See
§ 7 #8.

⏸ The default-group branch shown here does **no** extraction. The alternative —
extract always, so moving a chat into a project carries its history instantly —
costs GPU on throwaway chats. See § 7.

## 2.2 Read path — context assembly

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant App as ui/app.py
    participant Chat as ChatService
    participant GMS as GroupMemoryStrategy
    participant Store as MemoryStore
    participant Inner as RecencyWindowStrategy
    participant LLM as LLMProvider

    User->>App: send message
    App->>Chat: turn(conversation, text)
    Chat->>GMS: build(messages, context_window)

    GMS->>Store: memory_scope(conversation)
    alt default group
        Store-->>GMS: none
        GMS->>Inner: build(messages, full budget)
        Inner-->>GMS: ContextDecision
        Note over GMS: chat-local only.<br/>No cross-chat recall exists to leak.
    else project group
        Store-->>GMS: group_id
        GMS->>Store: stable_core(group_id, core_budget)
        Store-->>GMS: top-N by salience and recency, no query
        GMS->>Store: select(group_id, query=latest turn, sel_budget)
        Store->>Store: fuse bm25, recency, support ranks
        Store->>Store: apply diversity cap on shared citations
        Store-->>GMS: selected fragments
        GMS->>Inner: build(messages, remaining budget)
        Inner-->>GMS: ContextDecision
        GMS->>GMS: splice memory blocks, record recalled
    end

    GMS-->>Chat: ContextDecision
    Chat->>LLM: generate(prompt)
    LLM--)App: stream tokens
    App->>App: render reply plus memory indicator
```

**Prompt layout matters here.** The stable core changes only when fragments
change; the query-selected block changes every turn. Putting them in one block
would invalidate the KV cache on every message.

```mermaid
flowchart LR
    A["system prompt"] --> B["stable group core<br/>salience + recency"]
    B --> C["conversation history<br/>from RecencyWindowStrategy"]
    C --> D["query-selected fragments"]
    D --> E["latest user turn"]

    subgraph CACHE["cacheable prefix — stable across turns"]
        A
        B
    end

    subgraph VOL["recomputed per turn"]
        D
        E
    end
```

Two blocks, two jobs: the stable core is context **compression** (NFR-CTX-01), the
query-selected block is context **selection by relevance** (NFR-CTX-03). Both are
MUSTs, so both blocks are needed regardless of the cache argument.

## 2.3 Fragment lifecycle

```mermaid
stateDiagram-v2
    [*] --> Active : extraction emits new

    Active --> Active : extraction emits merge<br/>(one more citation)
    Active --> Active : origin deleted but other citations survive<br/>(re-homed to a surviving conversation)
    Active --> Superseded : a later claim contradicts it

    Superseded --> Superseded : retained for history

    Active --> [*] : last citation removed
    Active --> [*] : origin deleted, no other citations
    Active --> [*] : group deleted
    Superseded --> [*] : last citation removed

    note right of Active
        enters the digest and
        is reachable by recall
    end note

    note right of Superseded
        excluded from the digest,
        still reachable by explicit recall
    end note
```

Deleting a single message can weaken many fragments at once and kill any that were
resting on it alone — the many-to-many makes that a fan-out, which is why the
orphan sweep in § 2.4 is a set operation rather than a per-fragment check.

## 2.4 Deleting a conversation

The cascade alone no longer suffices, because a fragment may be corroborated by
conversations that survive. Order matters and the whole thing is one transaction.

```mermaid
flowchart TD
    start["delete conversation C"] --> tx["BEGIN"]
    tx --> q1{"fragments originating in C<br/>with citations in other conversations?"}
    q1 -->|yes| rehome["re-home: repoint conversation_id<br/>to a surviving cited conversation"]
    q1 -->|no| del
    rehome --> del["DELETE conversation C"]
    del --> casc["cascade removes C's messages,<br/>its remaining fragments,<br/>and citations of C's messages"]
    casc --> sweep["sweep fragments left with zero citations"]
    sweep --> fts["rebuild affected memory_fts rows"]
    fts --> commit["COMMIT"]
```

Re-homing must happen **before** the delete, or the cascade removes the fragment
first.

⚠ The re-homing step is a judgement call against NFR-S-04. A fragment corroborated
in two other chats survives deletion of the chat that first said it: the reference
to the deleted conversation is gone, but the claim persists. Reading it strictly,
that surviving fragment is residue. Decide deliberately and state which reading
was taken — this is a likely demo question. See § 7.

## 2.5 Moving a conversation between groups

```mermaid
flowchart TD
    start["move conversation C into group G"] --> tx["BEGIN"]
    tx --> upd["UPDATE conversations SET group_id = G"]
    upd --> find["find citations on C's fragments<br/>pointing at messages now outside G"]
    find --> q1{"any cross-group citations?"}
    q1 -->|yes| drop["DELETE those citation rows"]
    q1 -->|no| commit
    drop --> sweep["sweep fragments left with zero citations"]
    sweep --> commit["COMMIT"]
```

Because `group_id` is derived rather than stored on the fragment, the fragments
follow the conversation with no write of their own. The second step exists only to
enforce invariant I-3.

---

# 3 — Invariants

| ID | Invariant | Enforced by |
|---|---|---|
| I-1 | Every conversation belongs to exactly one group. `group_id` is NOT NULL. | Schema |
| I-2 | The default group is not a memory scope. Fragments never cross it. | `memory_scope()`, single decision point |
| I-3 | A fragment's citations all lie within the fragment's own group. | § 2.5 pruning step |
| I-4 | A fragment with zero citations does not exist. | Sweep in § 2.4 and § 2.5 |
| I-5 | Fragment text is self-contained — no unresolved pronouns or references. | Extraction prompt; unenforceable in schema |
| I-6 | Memory code never reads persona data, and persona code never reads fragments. | Package boundary; if they ever meet it is at prompt assembly, explicitly |
| I-7 | A (fragment, message) pair is cited at most once. | Composite PK |

I-2 is the one that earns its keep. Making the default group a real memory scope
would pool every unrelated one-off chat into one invisible digest — a leak that is
worse for being invisible, and precisely what NFR-CTX-02 prohibits.

I-5 is the most common quiet failure. A fragment reading "she prefers it" is
worthless once detached from its turn, and nothing in the schema can catch it.

---

# 4 — Requirement traceability

| Decision | Requirement | Priority |
|---|---|---|
| Fragments scoped by group, resolved by JOIN | NFR-S-02 | MUST |
| Default group is not a memory scope (I-2) | NFR-CTX-02 | MUST |
| Ordered delete transaction, orphan sweep | NFR-S-04 | SHOULD |
| SQLite, single file, inspectable | NFR-S-01, NFR-S-03 | MUST / SHOULD |
| Stable group core | NFR-CTX-01 | MUST |
| Query-selected block, ranked not recency-only | NFR-CTX-03 | MUST |
| Budgeted assembly, survives model switch to a smaller window | NFR-CTX-04 | MUST |
| `ContextDecision.recalled` plus memory indicator in the UI | NFR-CTX-05, NFR-Q-01 | SHOULD / MUST |
| Citation rows with quotes — evidence is attributable to its message | NFR-CTX-05 | SHOULD |
| Extraction off the reply path, cancellable worker | NFR-U-04 | MUST |
| Group picker as a modal, no drag-and-drop | NFR-P-03 | MUST |
| Move-to-default on group delete, with confirmation | NFR-U-06 | SHOULD |

---

# 5 — Beyond requirements

Nothing in this section is demanded by any NFR. It is real engineering, and it is
what to cut first if the schedule tightens. Listed most-cuttable first.

| Feature | What is lost by cutting it |
|---|---|
| `quote` on the citation row | The memory inspector can name the message but not highlight the span. Cheap to keep; nullable, so it can be added later without migration pain. |
| Diversity cap on shared citations | One dense message can dominate the digest. Only bites once messages are commonly multi-cited. |
| Support-count rank fusion | Corroboration stops influencing ranking. BM25 + recency alone still satisfies NFR-CTX-03. |
| `superseded_by` chains | Contradictory claims both surface. Mitigated by recency weighting, badly. |
| Cross-conversation citations | Fragments become conversation-local; § 2.4 re-homing and § 2.5 pruning both disappear, and the model gets materially simpler. |
| Re-homing on delete | Corroborated fragments die with their origin. Strictly *more* compliant with NFR-S-04. |
| KV-cache-aware prompt split | Slower per turn. Both blocks are still required; only their placement is an optimisation. |

Cutting the cross-conversation row collapses roughly half of § 2 — worth knowing
which half is load-bearing. Note that it does **not** collapse the many-to-many:
one message can support several fragments within a single conversation too.

---

# 6 — Ranking

Fusion over ranks, not scores. BM25 values are unbounded and corpus-dependent, so
weights tuned on one group are wrong for the next.

```
score(f) = 1/(k + rank_bm25(f)) + 1/(k + rank_recency(f)) + 1/(k + rank_support(f))
k ≈ 60
```

- `rank_recency` orders by `fragment_support.last_seen_at`, not `created_at`. A
  backfilled three-month-old chat is extracted today; ranking on extraction time
  would make its stale claims look freshest.
- `rank_support` orders by `conversation_count`, not `citation_count`, and
  saturates: `log(1 + conversation_count)`. Two messages five turns apart in one
  session is usually the user rephrasing, not independent confirmation.
- The fusion is deterministic and each term is inspectable, which is what makes
  the memory indicator in § 2.2 able to explain itself (NFR-CTX-05).

**Message density is deliberately not a ranking signal.** The many-to-many makes
"how many fragments does this message support" cheap to compute, and it is
tempting to read a dense message as an important one. It is not the same quantity
as corroboration: density measures how much was packed into one turn, not how well
a claim is supported. Feeding it into `rank_support` would let a single verbose
message outrank a claim independently confirmed across three conversations.

**Diversity cap.** Because one message can support several fragments, an
unconstrained selection can fill the digest with claims all resting on the same
turn. The model then sees one piece of evidence three times and weights it as
three. Cap the number of selected fragments sharing any single citation — 2 is a
reasonable start — applied after fusion, before the budget cut.

---

# 7 — Open decisions

| # | Question | Blocks |
|---|---|---|
| ⏸ 1 | Do ungrouped chats accumulate fragments? § 2.1 says no. "Yes" makes moving a chat into a project carry its history instantly, at the cost of extraction on every throwaway chat. | § 2.1, § 2.5 |
| ⚠ 2 | Strict or lenient reading of NFR-S-04 on re-homing (§ 2.4). | § 2.4, § 5 |
| ⏸ 3 | Salience: model-assigned at extraction, or derived from support and recency alone? A derived value is one less thing for a small model to get wrong. | § 6 |
| ⏸ 4 | Does `stable_core` get regenerated by an LLM summarisation pass, or is it just top-N fragment text? The former is truer compression; the latter has no failure mode. | § 2.2, NFR-CTX-01 |
| ⏸ 5 | Backfill trigger. Chats predating the feature and failed extractions both need one; is it manual, on-move, or on-open? | § 2.1 |
| ⏸ 7 | Does the extractor get told which messages it already cited for a candidate fragment? Without it, merge decisions may re-cite the same message — harmless (I-7 dedupes) but wasteful of prompt budget. | § 2.1 |
| ⏸ 8 | Store an explicit `subject` next to `text`? It turns the merge/supersede subject match from a free-form model judgement into a comparison, and lets candidates be retrieved by subject rather than full-text BM25. Costs a column and one more thing for the extractor to get right. | § 2.1, § 1.1 |
| ✔ 6 | *Resolved.* Open decision #4 in `requirements.md` — memory and the persona/profile store are **separate mechanisms** sharing storage primitives only. Fold this back into `requirements.md`. | — |

---

## Changelog

- **2026-08-10** — v2.2. Specified what new/merge/supersede are actually decided
  on — § 2.1 had the writes but never the judgement. Added the fourth outcome
  (`ignore`, the common case), a decision flowchart, the error-cost asymmetry that
  biases unclear cases to `new`, and the one-resolution-per-claim and
  merge-never-rewrites rules. New parked item § 7 #8 on an explicit `subject`.
- **2026-08-10** — v2.1. Recorded the relationship to plan 001: this model
  supersedes its `conversations` schema, and no migration path is written because
  the project is a single-user prototype. Noted the
  `list_conversations(group_id=None)` overload as a knock-on correction.
- **2026-08-10** — v2. Fragment↔message corrected to an explicit many-to-many: the
  v1 class diagram showed citations as fragment-owned, which was wrong. Renamed
  `fragment_sources` → `fragment_citations` so the associative entity reads as an
  edge. Added the nullable `quote` span, a diversity cap in § 6, invariant I-7, and
  an explicit warning against using message density as a ranking signal. New
  parked item § 7 #7.
- **2026-08-10** — v1. First full static and dynamic model. Settled: default group
  as a non-scope, origin-versus-support fragment ownership, derive-mutable /
  copy-immutable, ordered delete transaction, rank fusion over BM25 + recency +
  support. Resolved `requirements.md` open decision #4 (memory separate from the
  persona pipeline). Five items parked in § 7.
