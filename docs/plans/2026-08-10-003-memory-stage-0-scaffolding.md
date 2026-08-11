# Stage 0 — Invariant suite and scaffolding

**Status: v1 — detail plan for stage 0 of
`2026-08-10-002-memory-and-groups-skeleton.md`.**
**Design source: `memory-and-groups.md` v6, via the skeleton. This file expands
one stage; it does not restate the design.**

## Context

`docs/plans/memory-and-groups.md` (v6) designs a group-scoped memory system;
`docs/plans/2026-08-10-002-memory-and-groups-skeleton.md` (v1) slices it into
seven stages, each ending at a gate. Stage 0's goal is stated there in one line:
**the drift detector exists before anything it detects.**

Nothing user-visible ships. What ships is the machinery that keeps stages 1–6
honest:

- a **tuning surface** where every guessed number from § 9 lives once, carrying
  its status, so nobody six weeks from now reads `0.35` in `rank.py` as a fact;
- the **persona-forward protocols** (`EvidenceItem`, opaque `scope_id`, `Tier`)
  that keep `extract/rank/consolidate` from growing chat-shaped dependencies
  they would later have to be surgically freed of (I-6);
- the **cumulative invariant suite** — one test per I-1..I-13, written now,
  almost all skipped. A test written before the code it guards cannot be quietly
  shaped to fit whatever the code turned out to do;
- **test factories** so stages 1–5 build citation hierarchies without an LLM.

**Gate:** `uv run pytest` green (active tests only); tuning surface importable
and logged at startup.

## Decisions taken before writing this plan

| Question | Answer |
|---|---|
| Where does the startup log go? | A file under `data_dir`. Stdout/stderr would scribble over the Textual TUI, and § 9's point ("three weeks from now, which of these was in force?") needs it to outlive the session. |
| Where do the memory domain dataclasses live? | `core/memory/models.py`, in stage 0. Pure data, fully specified by §§ 1.1/1.3; both the factories and the stage-1 `MemoryStore` protocol need them. Keeping them out of `core/models.py` makes the I-6 lint a clean blanket ban on that module. |
| How much do the factories build? | Object graphs only. Stage 1 adds `write(store, graph)` once `apply()` exists — building the store adapter now would be planning past the gate. |

---

## File manifest

| File | Change |
|---|---|
| `src/agentchat/log.py` | **new** — file logging setup |
| `src/agentchat/core/memory/__init__.py` | **new** — re-exports |
| `src/agentchat/core/memory/tuning.py` | **new** — § 9 catalogue + effective values |
| `src/agentchat/core/memory/types.py` | **new** — `EvidenceItem`, `Tier`, kinds |
| `src/agentchat/core/memory/models.py` | **new** — memory domain dataclasses |
| `src/agentchat/core/memory/extract.py` | **new** — six named pipeline stages, no-ops |
| `src/agentchat/core/memory/rank.py` | **new** — docstring only (exists for the I-6 lint) |
| `src/agentchat/core/memory/consolidate.py` | **new** — docstring only (ditto) |
| `src/agentchat/core/errors.py` | +`TuningError` |
| `src/agentchat/__main__.py` | +3 lines: logging setup, tuning log |
| `tests/factories.py` | **new** — object-graph builders |
| `tests/test_invariants.py` | **new** — the I-1..I-13 suite |
| `tests/test_tuning.py` | **new** |
| `tests/test_factories.py` | **new** |
| `tests/test_storage.py` | `make_conversation` moved to `factories.py`, imported |
| `.env.example`, `README.md` | document the `AGENTCHAT_MEMORY_*` overrides |
| `docs/plans/memory-and-groups.md` | § 9 `RECALL_FLOOR_MODEL` row; `select(tiers=)` fold-back; changelog |
| `docs/plans/…-skeleton.md` | changelog entry |

Nothing under `src/agentchat/ui/` or `src/agentchat/storage/` is touched.

---

## 1. `src/agentchat/log.py`

The app has no logging today. One module, called only from `__main__.py`.

```python
LOG_FILENAME = "agentchat.log"

def setup_logging(data_dir: Path, level: int = logging.INFO) -> Path:
    """Attach a file handler at ``data_dir/agentchat.log``; return its path.
    Idempotent — a second call does not double the handler."""
```

- Root logger gets exactly one `FileHandler`; the idempotence guard compares
  `baseFilename` against existing handlers.
- No `StreamHandler`: the terminal belongs to Textual.
- Named `log.py`, not `logging.py` — a sibling shadowing the stdlib name is the
  kind of import confusion that costs an afternoon.

`__main__.main()` becomes:

```python
settings = Settings.from_env()
setup_logging(settings.data_dir)
log_effective_tuning(Tuning.from_env())
ChatApp(settings).run()
```

## 2. `src/agentchat/core/memory/tuning.py`

**Two objects, deliberately separate.** The catalogue carries what stage 6's
read-only inspector table needs (status badge, origin, design section); `Tuning`
carries the effective values `rank.py` and friends read.

```python
Status = Literal["borrowed", "scaled", "guess", "tuned"]

ENV_PREFIX = "AGENTCHAT_MEMORY_"

@dataclass(frozen=True)
class Constant:
    name: str                       # RECALL_FLOOR
    field: str                      # recall_floor — the Tuning attribute
    default: int | float | str
    status: Status
    origin: str
    section: str                    # "§ 6.1"

    @property
    def env_var(self) -> str: ...   # AGENTCHAT_MEMORY_RECALL_FLOOR

CATALOGUE: tuple[Constant, ...]     # § 9 order
BY_NAME: Mapping[str, Constant]

@dataclass(frozen=True)
class Tuning:
    extract_every: int = ...
    reinforce_step: float = ...
    candidate_pool_k: int = ...
    recall_floor: float = ...
    recall_floor_model: str = ...
    rrf_k: int = ...
    decay_k: int = ...
    alpha_decision: float = ...
    alpha_constraint: float = ...
    alpha_fact: float = ...
    alpha_artefact: float = ...
    alpha_open_question: float = ...
    mmr_lambda: float = ...
    core_budget_fraction: float = ...
    sel_budget_fraction: float = ...
    consolidate_threshold: float = ...
    consolidate_questions: int = ...
    consolidate_insights: int = ...
    consolidate_recent_n: int = ...

    @classmethod
    def from_env(cls) -> "Tuning": ...

    def alpha_for(self, kind: str) -> float:
        """Decay rate for a fragment kind; unknown kinds fall back to
        ``alpha_fact``."""

    def as_rows(self) -> list[tuple[Constant, int | float | str]]:
        """Catalogue paired with effective values — what stage 6's inspector
        table renders."""

def log_effective_tuning(tuning: Tuning, log: Logger | None = None) -> None:
    """One INFO record per constant: name, effective value, status."""
```

Each field's default is `field(default_factory=lambda: _resolve(BY_NAME["…"]))`,
mirroring `config.Settings`' shape so the file reads like the config module
already in the repo.

**Values** — every § 9 row, with the paired α rows split so each is
independently overridable, and **nothing `tuned`**:

| field | default | status |
|---|---|---|
| `extract_every` | 6 | `guess` |
| `reinforce_step` | 0.1 | `guess` |
| `candidate_pool_k` | 10 | `guess` |
| `recall_floor` | 0.35 | `guess` |
| `recall_floor_model` | *pinned encoder id* | `guess` |
| `rrf_k` | 60 | `borrowed` |
| `decay_k` | 2 | `borrowed` |
| `alpha_decision`, `alpha_constraint` | 0.01 | `guess` |
| `alpha_fact`, `alpha_artefact` | 0.03 | `guess` |
| `alpha_open_question` | 0.15 | `guess` |
| `mmr_lambda` | 0.5 | `borrowed` |
| `core_budget_fraction` | 0.10 | `guess` |
| `sel_budget_fraction` | 0.10 | `guess` |
| `consolidate_threshold` | 40 | `scaled` |
| `consolidate_questions` | 2 | `scaled` |
| `consolidate_insights` | 3 | `scaled` |
| `consolidate_recent_n` | 30 | `scaled` |

`recall_floor_model` is **not in § 9's table but required by its prose** — the
floor is encoder-specific and stage 3's startup guard compares against it. Per
skeleton rule 6, its row is added to § 9 in the same commit. No encoder is
chosen yet, so the default is the empty string, meaning "unpinned"; stage 3's
guard treats empty as "warn that the pin is unset".

`alpha_for` is skeleton rule 4 made real: `kind` is open, so persona kinds plug
in with no schema change and no `KeyError`.

**Env parsing is local, not imported from `config.py`.** `config` imports
`storage` and `llm`; `core` importing it would invert the dependency direction.
Two ~8-line coercion helpers (`_env_int`, `_env_float`) are the cheap side of
that trade, with one inline comment saying why — "just import config's" is the
obvious wrong instinct. A bad value raises `TuningError(AgentChatError)` (new,
in `core/errors.py`) naming the variable and the offending value, matching
`config._env_int`'s message shape.

## 3. `src/agentchat/core/memory/types.py`

```python
@runtime_checkable
class EvidenceItem(Protocol):
    """What extraction consumes. The chat pipeline binds it to a message, the
    future persona pipeline to an email."""
    id: str
    text: str
    created_at: datetime

class Tier(Flag):
    EXTRACTED = auto()          # memory_fragments.consolidated = 0
    CONSOLIDATED = auto()       # = 1
    ALL = EXTRACTED | CONSOLIDATED

FragmentKind = str              # open by rule 4 — deliberately not an Enum
KNOWN_KINDS: tuple[str, ...] = (
    "fact", "decision", "constraint", "open_question", "artefact",
)
```

**Note for stage 2, not a contract change:** `Message` carries `content`, not
`text`, so it does **not** structurally satisfy `EvidenceItem`. A thin adapter is
required, and it belongs in `strategy.py`/`chat.py` — exactly where skeleton
rule 2 already puts the chat-specific adapters. Flagged here so stage 2 does not
meet it as a surprise and reach for the wrong fix (renaming `Message.content`).

## 4. `src/agentchat/core/memory/models.py`

Faithful to §§ 1.1/1.3, including the nullable fields Mermaid cannot express.

```python
@dataclass
class Group:
    id: str = field(default_factory=_new_id)
    name: str = "New group"
    kind: Literal["default", "project"] = "project"
    last_consolidated_at: datetime | None = None
    created_at: datetime = field(default_factory=_now)

    def is_memory_scope(self) -> bool:
        """False for the default group — I-2's single decision point."""
        return self.kind != "default"

@dataclass
class MemoryFragment:
    group_id: str
    text: str
    id: int | None = None                        # SQLite rowid, unset until written
    uuid: str = field(default_factory=_new_id)
    origin_conversation_id: str | None = None
    consolidated: bool = False
    kind: FragmentKind = "fact"
    confidence: float = 0.5
    importance: float = 5.0
    decay: float = 0.03
    embedding: bytes | None = None
    embedding_model: str | None = None
    reasoning: str | None = None
    created_at: datetime = field(default_factory=_now)
    revised_at: datetime | None = None

    @property
    def tier(self) -> Tier: ...
    @property
    def is_dormant(self) -> bool:                # confidence == 0 (I-10, I-13)
        ...

@dataclass
class FragmentCitation:
    fragment_id: int
    id: int | None = None
    source_message_id: str | None = None
    source_fragment_id: int | None = None
    observed_at: datetime = field(default_factory=_now)
    quote: str | None = None

    def __post_init__(self) -> None:
        """Mirrors the schema CHECK (I-9) so factories cannot build a row
        SQLite would reject at stage 1."""

@dataclass(frozen=True)
class FragmentSupport:
    """The derived view of § 1.1.1 — never written, only read."""
    citation_count: int
    conversation_count: int
    first_seen_at: datetime
    last_seen_at: datetime
```

`_now` / `_new_id` are the same two helpers as `core/models.py:13-18`; import
them rather than re-defining.

## 5. `src/agentchat/core/memory/extract.py`

Rule 3's six stages, in order, typed against the narrow protocols only:

```python
def observe(items: Sequence[EvidenceItem]) -> list[EvidenceItem]: ...
def audit(items: Sequence[EvidenceItem]) -> list[EvidenceItem]:
    # Identity by design. GUM gates observations on contextual integrity here;
    # chat memory has no such gate, and the persona pipeline will need one
    # without restructuring the pipeline around it.
    return list(items)

def propose(items: Sequence[EvidenceItem], *, scope_id: str) -> list[Any]: ...
def retrieve(claims: Sequence[Any], *, scope_id: str) -> list[Any]: ...
def resolve(claims: Sequence[Any], candidates: Sequence[Any]) -> list[Any]: ...
def apply(decisions: Sequence[Any]) -> None: ...
```

`audit` is a real (trivial) implementation and stays one. The other five raise
`NotImplementedError("stage 2")`.

The `Any` payloads are honest: `Claim`, `Candidate` and `Decision` are stage 2's
contract, and inventing them now is exactly the drift "never plan ahead of the
gate" forbids. A module-level comment says so, so a reader does not read `Any`
as laziness.

No import of `Message`, `Conversation`, or anything under `agentchat.storage` —
enforced from this stage on by the I-6 test.

`rank.py` and `consolidate.py` are module-docstring-only files. Their sole
stage-0 job is to exist so the I-6 lint has something to check.

## 6. `tests/factories.py`

```python
def make_group(**overrides) -> Group
def make_conversation(**overrides) -> Conversation
def make_message(**overrides) -> Message
def make_fragment(**overrides) -> MemoryFragment
def make_citation(fragment: MemoryFragment, *,
                  message: Message | None = None,
                  source: MemoryFragment | None = None,
                  quote: str | None = None) -> FragmentCitation

@dataclass
class MemoryGraph:
    """The five row-sets stage 1's writer will insert, flat."""
    group: Group
    conversations: list[Conversation]
    messages: list[Message]
    fragments: list[MemoryFragment]
    citations: list[FragmentCitation]

class GraphBuilder:
    """Assembles a group → conversations → messages → fragments → citations
    graph. Fragment ids are provisional sequential ints; stage 1's writer
    inserts them explicitly so citation rows stay valid."""

    def __init__(self, group: Group | None = None) -> None: ...
    def conversation(self, **overrides) -> Conversation: ...
    def message(self, conversation: Conversation, **overrides) -> Message: ...
    def extracted(self, *sources: Message, **overrides) -> MemoryFragment: ...
    def consolidated(self, *sources: MemoryFragment, **overrides) -> MemoryFragment: ...
    def build(self) -> MemoryGraph: ...
```

`extracted`/`consolidated` mint the fragment *and* its citation rows in one
call, which is what makes the § 1.1.1 example three lines:

```python
b = GraphBuilder()
c = b.conversation(); m1, m2, m3 = (b.message(c) for _ in range(3))
f1, f2 = b.extracted(m1, m2), b.extracted(m2, m3)
f3 = b.consolidated(f1, f2)
```

That is also stage 4's two-level sweep case, for free.

**Reuse:** `tests/test_storage.py:14` already defines a local
`make_conversation` doing this job. Move it here and import it there — one
definition, not two drifting ones. `tests/` has no `__init__.py`, so pytest puts
it on `sys.path` and `from factories import …` works.

### `tests/test_factories.py`

Three tests, because a broken factory silently weakens every later stage's gate:

| Test | Asserts |
|---|---|
| `test_builder_wires_the_section_111_hierarchy` | the graph above yields 3 messages, 3 fragments, 6 citations; `f3`'s citations carry `source_fragment_id` and no `source_message_id` |
| `test_fragment_ids_are_unique_and_referenced_by_citations` | every `citation.fragment_id` and `source_fragment_id` resolves to a fragment in the graph |
| `test_make_citation_rejects_two_sources_and_zero_sources` | `__post_init__` raises — I-9 caught in object-land, before stage 1's CHECK exists |

## 7. `tests/test_invariants.py` — the drift detector

Module docstring points at § 3 and the skeleton's invariant→stage map. Skipping
is one decorator, so activating a test is deleting one line:

```python
def stage(n: int):
    """Not yet implementable. Activating this test is deleting one line."""
    return pytest.mark.skip(reason=f"stage {n}")
```

Every test gets a **real body written against the contracts as the skeleton
states them** — a skipped `pass` stub guards nothing, and rewriting it at
activation time is precisely the drift this stage exists to prevent.

| Test | Marker | Asserts |
|---|---|---|
| `test_i1_conversation_requires_a_group` | `stage(1)` | inserting a conversation with `group_id=None` raises through the store; `Conversation.group_id` is typed `str` |
| `test_i2_default_group_extracts_nothing` | `stage(2)` | extractor run over a default-group conversation reaches `apply()` zero times; no fragments exist afterwards |
| `test_i2_default_group_recalls_nothing` | `stage(3)` | `GroupMemoryStrategy.build` on a default-group conversation returns `recalled == []` and splices no memory block, even with fragments present in other groups |
| `test_i3_citations_stay_within_their_group` | `stage(4)` | over a two-group graph, no citation joins a fragment in A to a message in B; `purge_group(A)` leaves every B row untouched |
| `test_i4_sweep_removes_citationless_fragments` | `stage(4)` | delete the only message citing `f1` → `f1` gone; `f3` (which cited only `f1`) gone too — transitive, not one pass |
| `test_i4_uncited_insights_are_dropped_not_written` | `stage(5)` | a consolidation insight whose citation indices do not parse is discarded, never written citation-less |
| `test_i5_extraction_prompt_demands_self_contained_text` | `stage(2)` | golden-prompt check: the rendered extraction prompt contains the self-containment instruction. Docstring states this is best-effort — § 3 concedes I-5 is unenforceable |
| **`test_i6_memory_modules_never_import_chat_types`** | **active** | see below |
| `test_i7_repeat_citation_is_a_noop` | `stage(1)` | applying the same `(fragment, message)` citation twice leaves one row; likewise `(fragment, fragment)` — one assertion per partial unique index |
| `test_i8_cycle_closing_edge_is_rejected` | `stage(1)` | `f1→f2` accepted, then `f2→f1` rejected by the reachability check |
| `test_i8_forward_edge_in_id_order_is_allowed` | `stage(1)` | an older fragment citing a *newer* one is accepted — the § 2.6 reinforce-with-newer-evidence case that killed v5's monotonic id rule |
| `test_i9_citation_cites_exactly_one_source` | `stage(1)` | both source columns null → rejected; both set → rejected |
| `test_i10_dormant_fragments_survive_the_sweep` | `stage(4)` | a `confidence == 0` fragment with a live citation survives a purge that removes unrelated evidence — the sweep keys on citations, never on confidence |
| `test_i11_floor_is_applied_before_fusion` | `stage(3)` | a fragment that would rank first under fusion but sits below `recall_floor` does not appear in `select()` output |
| `test_i12_watermark_advances_only_with_its_writes` | `stage(2)` | a run failing before `apply()` leaves `(extracted_at, extracted_id)` unmoved; after a successful run every message is either cited or past the watermark |
| `test_i13_dormant_is_reachable_by_candidates_not_select` | `stage(3)` | a `confidence == 0` fragment is returned by `candidates()` and absent from `select()` — without the asymmetry dormancy is irreversible |
| `test_every_invariant_has_a_test` | active | meta-test: I-1..I-13 each map to ≥1 test function in this module, by name prefix |

**`test_i6_memory_modules_never_import_chat_types` — the one active invariant.**
AST-parse `src/agentchat/core/memory/{extract,rank,consolidate}.py` and assert
none of them import:

- anything under `agentchat.core.models`, `agentchat.core.chat`,
  `agentchat.core.context`, `agentchat.storage`, or `agentchat.ui`;
- the names `Message` or `Conversation` from any module.

It also asserts the three files exist, so a rename cannot silently empty the
lint. `strategy.py` is explicitly **not** checked — rule 2 licenses it to know
both worlds. The "persona code never reads fragments" half of I-6 is untestable
until a persona package exists; the test says so in a comment rather than
pretending to cover it.

`test_every_invariant_has_a_test` is the drift detector for the drift detector:
the suite is cumulative and never shrinks, and this makes that mechanical rather
than a matter of discipline.

## 8. `tests/test_tuning.py`

| Test | Asserts |
|---|---|
| `test_defaults_match_the_section_9_table` | `Tuning.from_env()` on a clean environment equals every documented default |
| `test_env_override_applies_to_one_constant_only` | `AGENTCHAT_MEMORY_RECALL_FLOOR=0.5` moves `recall_floor`, nothing else (via `monkeypatch`) |
| `test_bad_override_raises_naming_the_variable` | `AGENTCHAT_MEMORY_EXTRACT_EVERY=abc` → `TuningError` whose message contains the env var name and `abc` |
| `test_alpha_resolves_per_kind` | `decision` → 0.01, `fact` → 0.03, `open_question` → 0.15 |
| `test_unknown_kind_falls_back_to_alpha_fact` | `alpha_for("persona_trait")` → `alpha_fact` — rule 4's open-`kind` guarantee |
| `test_catalogue_and_dataclass_agree` | every `Constant.field` is a `Tuning` field and vice versa; the one duplication in the module gets its own detector |
| `test_no_constant_is_marked_tuned` | § 9: "Nothing is `tuned` yet. That is the honest current state." Fails the day someone validates one without recording what against |
| `test_log_effective_tuning_emits_every_constant_with_status` | via `caplog`: one record per catalogue entry, each carrying name, value and status |

## 9. Documentation, same commit

- **`RECALL_FLOOR_MODEL`** added to § 9's table (`guess`, § 6.1) — rule 6.
- **`select()`'s `tiers` parameter** folded into `memory-and-groups.md`. The
  skeleton resolved §§ 2.2/2.6's contradiction as
  `select(scope_id, query, budget, *, tiers=Tier.EXTRACTED)` and says to fold it
  back; doing it now means stage 3 never reads the contradiction. Touches § 1.3's
  `MemoryStore` signature, § 2.2 (uses the default), § 2.6 (passes both tiers).
- Changelog entries in both docs.
- `.env.example` and README's Configuration table gain the `AGENTCHAT_MEMORY_*`
  block — the overrides are useless if undiscoverable.

---

## Out of scope

No schema, no `MemoryStore`, no store writes, no LLM call, no ranking, no UI, no
`strategy.py`. `rank.py` and `consolidate.py` stay docstring-only.

## Verification

```bash
uv run pytest -q                          # green

uv run pytest tests/test_invariants.py -v
#   -> test_i6_… and test_every_invariant_has_a_test pass;
#      15 skipped, each reason reading "stage N"
uv run pytest tests/test_tuning.py tests/test_factories.py -v

uv run python -c "from agentchat.core.memory.tuning import Tuning; print(Tuning.from_env())"
AGENTCHAT_MEMORY_RECALL_FLOOR=0.5 uv run python -c \
  "from agentchat.core.memory.tuning import Tuning; print(Tuning.from_env().recall_floor)"
AGENTCHAT_MEMORY_EXTRACT_EVERY=abc uv run python -c \
  "from agentchat.core.memory.tuning import Tuning; Tuning.from_env()"   # clear TuningError

# the gate's "and logged" half, end to end
AGENTCHAT_BACKEND=mock uv run agentchat   # start, quit
grep memory.tuning data/agentchat.log     # one line per constant, with status
```

Negative check worth running once by hand: add `from agentchat.core.models import
Message` to `rank.py` and confirm `test_i6_…` fails. A lint that has never been
seen to fail is not known to work.

Existing suites (`test_core.py`, `test_storage.py`, `test_switching.py`,
`test_app.py`) stay green — `test_storage.py` is touched only by the
`make_conversation` move.

## Risks

- **The `Any` payloads in `extract.py`** read as unfinished, because they are.
  The alternative is inventing stage 2's `Claim`/`Candidate`/`Decision` now and
  having stage 2 discover they were wrong — the drift the staged structure
  exists to prevent. The module comment carries this justification.
- **Skipped tests rot.** `test_every_invariant_has_a_test` covers *existence*,
  not *correctness*, of a test that has never run. Mitigation: each activation
  is a deliberate gate step in the stage that owns it, and the skeleton forbids
  the implementer from editing acceptance tests.
- **`recall_floor_model` has no encoder to pin.** No embedding model is chosen
  or depended on anywhere yet. Stage 0 ships the constant and the empty-means-
  unpinned convention; stage 3 owns choosing the encoder and the guard.

## Changelog

- **2026-08-10** — v1. Detail plan for skeleton stage 0.
