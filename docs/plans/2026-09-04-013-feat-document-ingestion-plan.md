---
artifact_contract: ce-unified-plan/v1
artifact_readiness: proposed
execution: code
product_contract_source: ce-plan-bootstrap
origin: docs/requirements.md
title: "feat: Document ingestion — drop a file in, snippet it, and retrieve it globally"
date: 2026-09-04
depth: standard
---

# feat: Document ingestion — drop a file in, snippet it, and retrieve it globally

**Origin requirements:** `docs/requirements.md` (NFR-RAG-02, NFR-RAG-04,
NFR-RAG-05, NFR-RAG-01, NFR-RAG-03, NFR-S-03, NFR-S-04, NFR-U-07, NFR-Q-03,
VNFR-09, VNFR-10)
**Target repo:** this repo (`agentchat`), branch `feat/document-ingestion` off
`main`
**Builds on:** plan 010 (`Embedder`, `FactIndex`, `AdaptiveRetriever`, the
gate/rewrite/judge/reseed prompts) and plan 012 (the two-pane browser pattern).
This is the ingestion half plan 010 left unimplemented.

---

## What this builds

A file dropped onto the terminal is ingested: read, cut into snippets, embedded,
and stored. From then on the retrieval loop searches **two corpora with the same
queries** — the group's facts, as today, and the document snippets, which are
**global**: every chat in every group can see every ingested document.

```
drop ──▶ read (.txt/.md/.pdf) ──▶ snippet (spans over the extracted text)
                                        ▼
                                  persist + embed
                                        ▼
question ──▶ gate ──self-contained──────────────────────────────▶ reply
               │
          needs evidence
               ▼
        ┌──▶ rewrite (seed → n queries)
        │      ▼
        │    retrieve ──┬─ facts    (group-scoped, two views)
        │               └─ snippets (global, one view)
        │      ▼
        │    assemble (dedupe, cap per document, rank → digest)
        │      ▼
        └─gap─ judge ──sufficient / round cap──▶ reply with digest
```

The loop's shape does not change and it costs no extra LLM call: documents enter
at the retrieve step, on the query vectors that were embedded anyway.

---

## Requirements

| ID | Requirement |
|---|---|
| R1 | The corpus directory is authoritative: a file copied into it is ingested while the app runs, an edited file replaces its document, a deleted file's document is dropped. A file already on this machine can also be dropped on the terminal or its path submitted. |
| R2 | Plain text (`.txt`, `.md`, and the other text suffixes) and PDF are accepted; anything else is refused with a message that names the reason. |
| R3 | A document's identity is the SHA-256 of its extracted text. Re-dropping unchanged content is a no-op that reports itself as one. |
| R4 | Re-dropping a path whose content changed replaces the stored document, its snippets and its vectors — no stale text stays searchable. |
| R5 | Snippet spans index into the extracted text and are never renormalised after the fact; `text[start:end]` is the snippet, verbatim. |
| R6 | Every non-whitespace character of a document is covered by at least one snippet. |
| R7 | A snippet is small enough that the embedder's own character cap never truncates it. |
| R8 | Snippets are embedded by the same embedder instance the facts use, tagged with its id; a vector from another embedder id is replaced, not searched. |
| R9 | Snippets that exist without a current vector are embedded before the first search that would need them, incrementally and restartably. |
| R10 | Document retrieval is global — not scoped by group, conversation, or the chat the file was dropped into. |
| R11 | Every retrieval round searches both corpora with the same query vectors; documents add no LLM call. |
| R12 | The digest is bounded per corpus: a cap on snippets, a cap on snippets from any one document, and a token share that keeps documents from crowding out facts. |
| R13 | Retrieved snippets are attributable — the document title and, for a PDF, the page — in the injected block and in the UI. |
| R14 | Ingestion failure is surfaced to the user; retrieval failure over documents degrades to a normal reply exactly as fact retrieval does. |
| R15 | Ingestion is switchable off, and off means no reader import, no snippet write, no embedder load on its account. Document retrieval is switchable off separately. |
| R16 | Deleting a document removes its snippets and their vectors. |
| R17 | Ingested documents are inspectable and deletable in the UI. |
| R18 | The knobs — snippet size, overlap, size cap, hit counts, score floor, digest caps — are environment variables. |

---

## Design

### Where a drop arrives

A terminal delivers a dropped file as a **paste**, not as an event of its own:
Textual has no file-drop event. `Input._on_paste` inserts the first line of the
pasted text and calls `event.stop()`, so an app-level `on_paste` never runs while
the prompt has focus. Interception therefore lives in a `PromptInput(Input)`
subclass overriding `_on_paste`: if the payload parses to at least one existing
file path, it posts a `FilesDropped` message and inserts nothing; otherwise it
defers to `super()._on_paste`, and pasting prose behaves as it does today.

Payloads vary by terminal: quoted paths, backslash-escaped spaces, `file://`
URIs, `~`, several paths on one line, a trailing newline. `dropped_paths` handles
all of them and returns only paths that exist, which is what makes "did the user
drop a file or paste a sentence?" decidable without guessing.

### Reading and snippeting

Extraction produces the document's canonical text **once**. Text files are
decoded UTF-8, falling back to Latin-1 on failure; PDFs go through `pypdf`,
page texts joined by a blank line, with each page's start offset recorded so a
snippet can name its page. Nothing is renormalised afterwards: every snippet span
indexes into that one string (R5).

Snippeting is paragraph-greedy. Blocks separated by a blank line are packed until
the next block would exceed the target size; a block larger than the target is
split on sentence ends; a sentence larger than the target is hard-cut. Each
snippet after the first extends its start backwards by the overlap, snapped
forward to the next whitespace so it never begins mid-word. A trailing snippet
below the minimum merges into its predecessor.

The default target is 1000 characters against `MAX_EMBED_CHARS = 2000` — a
snippet that reaches the embedder's cap is silently embedded as its first half,
which is the failure R7 exists to prevent.

### The two corpora

|  | facts | snippets |
|---|---|---|
| scope | one group | global |
| views | `claim` and `evidence` | one — the snippet text is its own claim and its own evidence |
| unit | one sentence, no chunking | ~1000 characters |
| written by | the extractor, per window | ingestion, per document |
| vectors | `fact_embeddings` | `snippet_embeddings` |

`SnippetIndex` mirrors `FactIndex`: `index`, `ensure_indexed`, `search`, and it
is the only module that reads `snippet_embeddings`. `search` ranks over vectors
alone and **hydrates only the winners** — the ranked snippet ids and their
documents are loaded afterwards, so a search never pulls every snippet's text
into memory.

### Assembly and the digest

Snippet hits are capped per document before the shared `assemble` runs, so one
long document cannot fill the digest with its own overlap. `render_digest` emits
the existing `Facts:` and `Said:` blocks, then a `Documents:` block of
`[title p.N] "…"` lines. Facts render into at most two thirds of the token
budget; documents take the rest plus whatever the facts left unused, so a turn
whose only evidence is a document still gets the whole budget.

The judge sees both corpora in one evidence list; its gap sentence therefore
steers the next round's queries across both. The gate is unchanged — one gate
governs retrieval as a whole.

### Cost

Per round: one extra matrix product and one extra `SELECT` over
`snippet_embeddings`. No extra generation. At 384 dimensions a thousand snippets
is ~1.5 MB of float32 read per round; past roughly ten thousand snippets the
per-round load is what to fix first, and an ANN index is the fix — not in this
plan.

---

## Output Structure

```
src/agentchat/
  core/
    snippets.py      NEW — Span, spans(), page_of(): pure, no I/O
    ingest.py        NEW — readers, dropped_paths, DocumentIngestor
tests/
  test_snippets.py   NEW — span invariants, packing, splitting, overlap
  test_ingest.py     NEW — txt/md/pdf, refusals, re-ingest, dropped_paths
```

Modified: `core/models.py`, `core/errors.py`, `core/prompts.py`,
`core/retrieval.py`, `core/chat.py`, `llm/embedding.py`, `storage/schema.py`,
`storage/base.py`, `storage/sqlite.py`, `config.py`, `ui/app.py`,
`ui/widgets.py`, `ui/screens.py`, `ui/app.tcss`, `pyproject.toml`,
`tests/factories.py`, `tests/test_storage.py`, `tests/test_retrieval.py`,
`tests/test_app.py`, `README.md`, `AGENTS.md`.

---

## Implementation Units

### U1. `pyproject.toml` — the PDF reader

**Requirements:** R2 · **Depends on:** nothing

Add `pypdf>=5.0` to `dependencies`, with a one-line comment on why this one:
pure Python, no system libraries, no network at read time.

**Pitfalls**

- Import it **inside** the PDF reader function, not at module scope, so
  `AGENTCHAT_INGEST_DOCUMENTS=0` and a text-only ingest never pay for it and a
  broken install surfaces as an `IngestError` on a PDF rather than an import
  error at startup (R15).

---

### U2. `core/snippets.py` — cutting text into spans

**Requirements:** R5, R6, R7 · **Depends on:** nothing

Pure, like `anchoring.py`: no store, no embedder, no I/O.

```python
SNIPPET_CHARS = 1000
SNIPPET_OVERLAP = 150
MIN_SNIPPET_CHARS = 200

@dataclass(frozen=True)
class Span:
    start: int
    end: int

def spans(text: str, *, size: int = SNIPPET_CHARS,
          overlap: int = SNIPPET_OVERLAP,
          minimum: int = MIN_SNIPPET_CHARS) -> tuple[Span, ...]: ...

def page_of(start: int, page_starts: Sequence[int]) -> int | None:
    """1-based page holding `start`. `None` when the document has no pages."""
```

Order of operations inside `spans`:

1. Whitespace-only text → `()`.
2. Split into paragraph blocks on a blank line, keeping offsets.
3. Pack blocks greedily up to `size`.
4. A block longer than `size` splits on sentence ends (`(?<=[.!?])\s+`); a
   sentence longer than `size` is hard-cut at `size`.
5. Extend each snippet's start back by `overlap`, snapped forward to the next
   whitespace; never before the previous snippet's start.
6. Merge a trailing span shorter than `minimum` into its predecessor.

**Pitfalls**

- Clamp `overlap` to `size // 2`. An overlap at or above `size` makes step 5
  produce a span that starts at or before its predecessor's start, and the
  packing loop stops advancing.
- Do not strip, dedent, collapse whitespace, or NFKC the text here or anywhere
  after extraction — every one of those shifts the offsets the spans are
  expressed in (R5). Trim by moving the span's boundaries, never by rewriting
  the string.
- Snippet spans overlap by construction, so adjacent snippets are near
  duplicates. That is intended, and it is why retrieval caps hits per document
  (U6) rather than trusting the subset dedup to remove them.

**Tests** (`tests/test_snippets.py`): `text[s.start:s.end]` round-trips for
every span; spans are ordered and cover every non-whitespace character (R6);
a paragraph document packs into whole paragraphs; a 5000-character paragraph
splits on sentences; a 5000-character single sentence hard-cuts into spans no
longer than `size`; a 40-character tail merges into its predecessor; every span
starts at a non-whitespace character; `spans("")` and `spans("   \n\n ")` are
`()`; `overlap >= size` is clamped rather than looping.

---

### U3. `core/models.py` — Document, Snippet, SnippetEmbedding

**Requirements:** R3, R13 · **Depends on:** nothing

```python
@dataclass
class Document:
    id: str = field(default_factory=_new_id)
    title: str = ""            # basename at ingest time
    path: str = ""             # absolute path at ingest time, for provenance
    media_type: Literal["text", "pdf"] = "text"
    content_hash: str = ""     # sha256 of `text` — the identity (R3)
    #: The extracted text the snippets' spans index into. Empty on rows from
    #: `list_documents`; only `store.document()` fills it.
    text: str = ""
    char_count: int = 0
    snippet_count: int = 0
    created_at: datetime = field(default_factory=_now)

@dataclass(frozen=True)
class Snippet:
    id: str
    document_id: str
    ordinal: int
    text: str
    start: int
    end: int
    page: int | None = None

@dataclass(frozen=True)
class SnippetEmbedding:
    snippet_id: str
    model_id: str
    vector: tuple[float, ...]
```

**Pitfalls**

- `Document.text` being empty on a listed row is the one asymmetry here. Every
  reader that needs the text must go through `store.document(id)`; a `Document`
  from `list_documents` is a header, not a body.

---

### U4. Storage — three tables and the hydrate-the-winners reads

**Requirements:** R3, R4, R8, R16, NFR-S-03, NFR-S-04 · **Depends on:** U3

`storage/schema.py`, appended to `SCHEMA`:

```sql
CREATE TABLE IF NOT EXISTS documents (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, path TEXT NOT NULL,
  media_type TEXT NOT NULL, content_hash TEXT NOT NULL,
  text TEXT NOT NULL, char_count INTEGER NOT NULL,
  snippet_count INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_hash ON documents(content_hash);
CREATE INDEX IF NOT EXISTS idx_documents_path ON documents(path);

CREATE TABLE IF NOT EXISTS document_snippets (
  id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL, text TEXT NOT NULL,
  start INTEGER NOT NULL, "end" INTEGER NOT NULL, page INTEGER,
  UNIQUE (document_id, ordinal));
CREATE INDEX IF NOT EXISTS idx_snippets_document ON document_snippets(document_id);

-- One row per (snippet, embedder), keyed like `fact_embeddings` and for the
-- same reason: switching embedders and back must not have discarded the
-- first embedder's vectors.
CREATE TABLE IF NOT EXISTS snippet_embeddings (
  snippet_id TEXT NOT NULL REFERENCES document_snippets(id) ON DELETE CASCADE,
  model_id TEXT NOT NULL, dim INTEGER NOT NULL, vector BLOB NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (snippet_id, model_id));
```

`storage/base.py` — protocol additions, under a `-- documents` banner:

```python
async def save_document(self, document: Document,
                        snippets: Sequence[Snippet]) -> None:
    """Write the document and its snippets in one transaction."""
async def document(self, document_id: str) -> Document | None:
    """With `text` filled in."""
async def document_by_hash(self, content_hash: str) -> Document | None: ...
async def document_by_path(self, path: str) -> Document | None: ...
async def list_documents(self) -> list[Document]:
    """Newest first, `text` left empty."""
async def list_snippets(self, document_id: str) -> list[Snippet]: ...
async def snippets_by_ids(self, ids: Sequence[str]) -> list[Snippet]: ...
async def delete_document(self, document_id: str) -> None: ...

async def save_snippet_embeddings(self, rows: Sequence[SnippetEmbedding]) -> None: ...
async def snippet_vectors(self, *, model_id: str) -> list[SnippetEmbedding]:
    """Every snippet vector for `model_id` — ids and vectors, no text."""
async def snippets_without_embeddings(self, *, model_id: str) -> list[Snippet]:
    """One LEFT JOIN, not a Python set difference."""
```

`storage/sqlite.py` implements each as the existing methods do: an `async`
wrapper over a `_name` body run through `asyncio.to_thread`, `sqlite3.Error`
wrapped in `StorageError`, vectors packed with the existing `_pack_vector` /
`_unpack_vector`.

**Pitfalls**

- **`end` and `start` are SQLite keywords in this schema's company — quote
  `"end"`**, exactly as `fact_phrases` does. An unquoted `end` is a syntax
  error at `CREATE TABLE`, which means at first launch.
- `PRAGMA foreign_keys = ON` is per connection and `schema.connect` already
  sets it; without it the two cascades above are decorative (R16).
- `documents.content_hash` is `UNIQUE`, so `save_document` on an existing hash
  raises. The ingestor checks first (U5) — do not paper over it with
  `INSERT OR IGNORE`, which would silently write a document with no snippets.
- No new migration helper. Fresh databases get these tables from `SCHEMA`;
  a database from before this plan gets them the same way, because
  `executescript` runs `CREATE TABLE IF NOT EXISTS` on every open.

**Tests** (`tests/test_storage.py`): round-trip a document with snippets;
`list_documents` returns `text == ""` and `document()` fills it; duplicate hash
raises `StorageError`; `delete_document` removes snippets and vectors;
`snippets_without_embeddings` returns exactly the unembedded ones and nothing
after they are written; `snippet_vectors` is scoped by `model_id`.

---

### U5. `core/ingest.py` — readers, path parsing, the ingestor

**Requirements:** R1, R2, R3, R4, R14 · **Depends on:** U1–U4

```python
MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".json"}

@dataclass(frozen=True)
class Extracted:
    text: str
    media_type: Literal["text", "pdf"]
    page_starts: tuple[int, ...]      # () for text

def read_document(path: Path) -> Extracted: ...
def dropped_paths(text: str) -> tuple[Path, ...]:
    """The existing file paths in a paste payload, in order. `()` when it is
    prose — which is how the prompt tells a drop from a paste."""

@dataclass(frozen=True)
class Ingested:
    document: Document
    snippets: tuple[Snippet, ...]
    #: True when the content was already stored — nothing was written.
    reused: bool = False

class DocumentIngestor:
    def __init__(self, store: ConversationStore, index: "SnippetIndex | None",
                 *, size: int = SNIPPET_CHARS, overlap: int = SNIPPET_OVERLAP,
                 max_bytes: int = MAX_DOCUMENT_BYTES) -> None: ...
    async def ingest(self, path: Path) -> Ingested: ...
    async def ingest_dir(self, directory: Path) -> tuple[Ingested, ...]: ...
```

`ingest`, in order:

1. Resolve the path; refuse a directory, a missing file, an oversize file or an
   unknown suffix with `IngestError` naming which (R2).
2. `await asyncio.to_thread(read_document, path)` — a PDF parse is blocking.
3. `content_hash = sha256(extracted.text.encode("utf-8")).hexdigest()`.
4. `document_by_hash` hits → return `Ingested(existing, (), reused=True)` (R3).
5. `document_by_path` hits with a different hash → `delete_document` it (R4).
6. Build `Snippet`s from `spans(extracted.text, ...)`, `page` from `page_of`.
7. `save_document(document, snippets)`.
8. `if self._index is not None: await self._index.index(snippets)` — after the
   write, so a cancelled ingest leaves a document that `ensure_indexed` will
   finish rather than snippets with no document.

`sync_dir` is the corpus pass: `corpus_dir` is authoritative, so a file added
there is ingested, a file edited there replaces its document, and a file
deleted there takes its document with it. Only documents whose stored path is
under `corpus_dir` are ever pruned. Per-file failures are collected, not
raised.

`core/errors.py` gains:

```python
class IngestError(AgentChatError):
    """Reading, snippeting or storing a dropped document failed. Unlike
    `RetrievalError` this is shown to the user: they asked for it explicitly."""
```

**Pitfalls**

- `dropped_paths` must handle `file://` URIs (percent-decoded), single and
  double quotes, `\ `-escaped spaces, `~`, and several paths on one line.
  Existence is the filter — never treat an arbitrary token as a path.
- A PDF that yields no text is a scanned image, not a bug. Refuse it with an
  `IngestError` that says so; ingesting a document of zero snippets would
  otherwise report success and retrieve nothing forever.
- `pypdf`'s `extract_text()` output — spacing, hyphenation, line breaks —
  varies by version. Tests assert on substrings, never on equality.
- Read the file's bytes once for the size check via `stat()`, before decoding:
  a 2 GB `.log` must be refused, not decoded and then refused.
- **Poll the corpus, do not watch it.** It sits on a network filesystem, where
  a file written by an `scp` from another host raises no inotify event on this
  node — the write never passes through its kernel.
- A file whose `(mtime, size)` moved since the last pass, and whose timestamp
  is younger than `SETTLE_SECONDS`, is a copy still in flight. Ingesting it
  stores half a document under a hash that will never be revisited.

**Tests** (`tests/test_ingest.py`): a `.txt` and a `.md` ingest to the expected
snippet count with vectors written; a PDF built by `make_pdf_bytes()` ingests
and its snippets carry pages; `.docx`, a directory, a missing file and an
oversize file each raise `IngestError`; a text-free PDF raises; re-ingesting
identical content returns `reused=True` and writes nothing; re-ingesting a
changed path leaves exactly one document and no orphan vectors (R4);
`dropped_paths` over a table of payloads — bare path, quoted, escaped spaces,
`file://`, two paths, trailing newline, a prose sentence, a nonexistent path.

---

### U6. `core/retrieval.py` — the snippet index and its assembly

**Requirements:** R8, R9, R10, R12 · **Depends on:** U4

```python
@dataclass(frozen=True)
class SnippetHit:
    snippet: Snippet
    document: Document          # header only — `text` is empty
    score: float

class SnippetIndex:
    """The global corpus. The ONLY module that reads `snippet_embeddings`."""
    async def index(self, snippets: Sequence[Snippet]) -> None: ...
    async def ensure_indexed(self) -> int: ...
    async def search(self, queries: Sequence[Sequence[float]], *,
                     k: int, min_score: float) -> list[SnippetHit]: ...
```

`search` loads `snippet_vectors(model_id=...)`, one matrix product for all
queries, takes each query's top `k` above `min_score`, unions by snippet id
keeping the best score, then hydrates the survivors with `snippets_by_ids` and
their documents.

`assemble` is generalised over the hit type rather than duplicated:

```python
def assemble(hits: Sequence[H], *, limit: int,
             text: Callable[[H], str]) -> tuple[H, ...]: ...
def cap_per_document(hits: Sequence[SnippetHit], *,
                     per_document: int) -> tuple[SnippetHit, ...]:
    """Keep each document's best `per_document` hits — adjacent snippets
    overlap, so one long document would otherwise fill the digest."""
```

Existing fact calls pass `text=lambda h: h.fact.text`; snippets pass
`lambda h: h.snippet.text`.

**Pitfalls**

- `ensure_indexed` takes no group: documents are global (R10). Resist adding a
  `group_id` parameter "for symmetry" with `FactIndex` — it would be a lie the
  next reader has to disprove.
- Guard the dimension mismatch the way `FactIndex.search` already does: a
  vector whose width differs from the query's is skipped, not broadcast into a
  crash.
- One embedder, two indexes. `FactIndex` and `SnippetIndex` must be handed the
  *same* `Embedder` instance (U8) — two `LocalEmbedder`s means loading the
  encoder twice.

**Tests** (`tests/test_retrieval.py`): index writes one row per snippet;
`ensure_indexed` embeds the backlog then returns 0; changing the embedder id
re-embeds and keeps the old rows; a document ingested from one group is found
from another and from a group with no facts at all (R10); `min_score` excludes
unrelated snippets; `cap_per_document` keeps the best two of five hits from one
document; `assemble` still behaves as before for facts.

---

### U7. `core/prompts.py` and the loop — one digest, two corpora

**Requirements:** R11, R12, R13 · **Depends on:** U6

`prompts.py` — every wording change in this plan lands here and nowhere else:

- `render_facts` → `render_digest(fact_hits, snippet_hits, *, budget)`, adding
  a `Documents:` block of `- [title p.N] "…"` lines. Facts get at most
  `DIGEST_FACT_SHARE = 2/3` of the budget; documents get the remainder plus
  whatever the facts left unused. Both drop whole hits, lowest score first.
- `RECALL_HEADER` extends to say the notes come from earlier conversations
  **and from documents the user provided**.
- `JUDGE_PROMPT`'s `{facts}` becomes `{evidence}` and carries both corpora's
  lines; `JUDGE_SYSTEM` says so.

`retrieval.py` — inside `_run`, per round:

- search `self._snippets` (when present) with the same query vectors, pooled by
  snippet id like the fact pool;
- `snippet_hits = assemble(cap_per_document(pool, per_document=...),
  limit=self._digest_snippets, text=...)`;
- `digest = render_digest(fact_hits, snippet_hits, budget=budget)`;
- the judge sees both.

`Recall` gains `snippets: tuple[SnippetHit, ...] = ()`.

`core/chat.py` — `metadata["recall"]` gains a `documents` list of
`{snippet_id, document_id, title, page, score, text}`, written under the same
rule as `facts`: only when the block actually reached the model.

**Pitfalls**

- Wrap the snippet search in `RetrievalError` at the same boundary the fact
  search uses. A document corpus failure must degrade to a normal reply, never
  to an error (R14).
- The gate stays as it is. Do not add a second gate for documents: one gate
  governs retrieval, and a message that is self-contained is self-contained
  whatever the corpus.
- `recall_block` is unchanged — it renders `recall.digest`, which now already
  contains the documents. Rendering documents a second time there would double
  them in the injected prompt.

**Tests**: `render_digest` emits all three blocks with attribution; a tight
budget drops documents before facts, and with no facts documents get the whole
budget; the judge prompt contains both a fact line and a document line; a
recalled turn's metadata carries the snippet, its document title and page; a
snippet-search failure returns a `Recall` built from facts alone rather than
`None`.

---

### U8. `config.py` and `llm/embedding.py` — wiring and one lock

**Requirements:** R15, R18 · **Depends on:** U5, U6

New `Settings` fields, all `AGENTCHAT_`-prefixed:

| Field | Var | Default |
|---|---|---|
| `ingest_documents` | `INGEST_DOCUMENTS` | `1` |
| `ingest_corpus_on_start` | `INGEST_CORPUS_ON_START` | `1` |
| `snippet_chars` | `SNIPPET_CHARS` | `1000` |
| `snippet_overlap` | `SNIPPET_OVERLAP` | `150` |
| `max_document_mb` | `MAX_DOCUMENT_MB` | `10` |
| `recall_documents` | `RECALL_DOCUMENTS` | `1` |
| `recall_snippet_hits` | `RECALL_SNIPPET_HITS` | `8` |
| `recall_snippet_min_score` | `RECALL_SNIPPET_MIN_SCORE` | `0.25` |
| `recall_digest_snippets` | `RECALL_DIGEST_SNIPPETS` | `5` |
| `recall_snippets_per_document` | `RECALL_SNIPPETS_PER_DOCUMENT` | `2` |

Builders: `build_snippet_index(settings, store, embedder)` and
`build_ingestor(settings, store, index)`, both `None` when switched off;
`build_retriever` takes the snippet index. `build_embedder` now returns an
embedder when **either** recall or ingestion is on — today it keys off
`recall_facts` alone, which would leave ingestion with nothing to embed with.
`corpus_dir` finally has a consumer: `ingest_corpus_on_start` ingests it in the
background when the directory exists, which the content hash makes a no-op after
the first run (NFR-RAG-04).

`llm/embedding.py`: give `LocalEmbedder` an `asyncio.Lock` around the lazy load.
A background ingest and a turn's `ensure_indexed` can now call `embed` at the
same time; both would see `_loaded == False` and load the encoder twice.

**Pitfalls**

- Build the embedder **once** and hand the same instance to `FactIndex`,
  `SnippetIndex` and, through the index, the ingestor.
- `AGENTCHAT_RECALL_FACTS=0` with ingestion on must still ingest and still
  retrieve documents; `AGENTCHAT_INGEST_DOCUMENTS=0` must leave the fact path
  byte-identical to today (R15).

---

### U9. UI — drop, ingest, and say what happened

**Requirements:** R1, R13, R14, R17 · **Depends on:** U5, U7

`ui/widgets.py`:

```python
class PromptInput(Input):
    class FilesDropped(Message):
        def __init__(self, paths: tuple[Path, ...]) -> None: ...

    def _on_paste(self, event: events.Paste) -> None:
        paths = dropped_paths(event.text)
        if not paths:
            super()._on_paste(event)
            return
        event.stop()
        self.post_message(self.FilesDropped(paths))
```

`RecallNote` gains the document count in its header (`recalled 4 facts ·
3 snippets from 2 documents · 2 rounds · sufficient`) and one detail line per
snippet naming its document and page.

`ui/app.py`: `compose` yields `PromptInput`; `on_prompt_input_files_dropped`
starts an `@work(group=_INGEST_GROUP)` ingest, shows `ingesting <name>…` in the
status bar, and notifies per file — the snippet count on success, "already
ingested" on `reused`, the `IngestError` message on failure. A new `Ctrl+U`
binding opens `DocumentBrowser`. The empty-chat placeholder mentions dropping a
file.

`ui/screens.py`: `DocumentBrowser(ModalScreen)`, built on plan 012's two-pane
pattern — documents on the left, the highlighted document's snippets on the
right — with `Ctrl+X` delete behind the picker's inline confirm.

`ChatService` gains `ingest(path)`, `documents()` and `delete_document(id)`
pass-throughs so the UI still does no store I/O of its own, and `stream_reply`
calls `snippet_index.ensure_indexed()` next to the fact one, before the provider
lock, for the same reason it is there.

**Pitfalls**

- Override `_on_paste`, not `on_paste`: Textual's private handler is what runs,
  and the public one would fire after the text was already inserted and the
  event stopped.
- Ingest runs in its own worker group. It must not be cancelled by
  `action_stop` or by a conversation switch, and it holds no provider lock —
  it only touches the store and the CPU embedder, so a drop mid-generation is
  fine.
- Some terminals type a dropped path as ordinary keystrokes instead of a
  bracketed paste. The typed path still works: on submit, a prompt whose whole
  text is an existing path ingests instead of asking the model. Say so in the
  README rather than treating the terminal as broken.
- The toast says the document is available in **every** chat. A user who
  dropped a file into one conversation will otherwise assume it is scoped to it.

**Tests** (`tests/test_app.py`): pasting an existing path posts `FilesDropped`
and leaves the prompt empty; pasting prose inserts text as before; a drop
ingests and notifies with the snippet count; a second drop of the same file
says "already ingested"; a drop of an unsupported file notifies the error and
the app keeps running; `Ctrl+U` lists documents and `Ctrl+X` deletes one.

---

### U10. Documentation

**Requirements:** all · **Depends on:** U1–U9

`README.md`: a "Documents" section — how to drop one, what is accepted, where
snippets live, that retrieval is global while facts are group-scoped, and the
typed-path fallback. The config table gains the ten new variables and
`AGENTCHAT_CORPUS_DIR`'s row loses "not yet used". `AGENTS.md`: `core/snippets.py`
and `core/ingest.py` in the layout block, `SnippetIndex`'s ownership of
`snippet_embeddings` stated the way `FactIndex`'s is.

---

## Test setup

`tests/factories.py` gains:

```python
def make_document(**overrides) -> Document: ...
def make_snippet(**overrides) -> Snippet: ...
def make_pdf_bytes(pages: Sequence[str]) -> bytes:
    """A minimal one-object-per-page PDF written literally, so the PDF path is
    covered without a writer dependency and without a binary fixture."""
async def make_indexed_corpus(store, embedder, *, docs=GOLDEN_DOCUMENTS) -> tuple[Document, ...]:
    """Documents, snippets and vectors already written — the state every
    document-retrieval test starts from."""
```

`GOLDEN_DOCUMENTS` is three short documents over distinct topics, one of them a
PDF with two pages, and one long enough to produce five snippets so the
per-document cap has something to cap. A query → expected snippet id table drives
one parametrised test, as plan 010's fact table does.

`HashingEmbedder` keeps the suite weightless and deterministic; a test opts into
the pipeline with `mock_settings(recall_facts=True, ingest_documents=True)`.
Ingestion defaults to **off** in `mock_settings`, like the other pipelines, so no
existing test grows a store write it never asked for.

---

## Verification

1. `uv run pytest` passes.
2. `AGENTCHAT_BACKEND=mock uv run agentchat` — drag a `.txt` from a file manager
   onto the terminal. The toast names the snippet count; the prompt is empty
   (R1). `sqlite3 <data>/agentchat.db "SELECT COUNT(*) FROM document_snippets"`
   agrees with it.
3. Drop the same file again: "already ingested", and the counts are unchanged
   (R3). Edit the file, drop it again: the counts change and
   `SELECT COUNT(*) FROM documents` is still 1 (R4).
4. Drop a PDF. Snippet pages are populated:
   `SELECT DISTINCT page FROM document_snippets WHERE document_id = …`.
5. Drop a `.docx` and a scanned PDF: each notifies its own reason, the app keeps
   running (R2, R14).
6. Paste a paragraph of prose into the prompt: it is inserted as text, nothing
   is ingested.
7. GPU node, real backend. Ask, in a **new group with no facts**, a question the
   dropped PDF answers. The reply uses it and the recall note names the document
   and page (R10, R13). Compare the note's block against the `chat` entry in
   `llm.jsonl`: character for character equal.
8. Same turn's transcript: the judge prompt carries both fact lines and document
   lines, and no extra generation appears next to plan 010's call count (R11).
9. `Ctrl+U`: the documents are listed with their snippet counts; highlight one
   and its snippets show; `Ctrl+X` deletes it, and
   `SELECT COUNT(*) FROM snippet_embeddings` drops accordingly (R16, R17).
10. `AGENTCHAT_EMBED_MODEL=other-model` and restart: the next turn re-embeds the
    snippets once (the log names the count), the turn after embeds nothing, and
    the original rows are still there (R8, R9).
11. `AGENTCHAT_RECALL_DOCUMENTS=0`: recall runs over facts only and no
    `snippet_embeddings` read happens. `AGENTCHAT_INGEST_DOCUMENTS=0`: a drop
    does nothing but say so, and no reader is imported (R15).
12. Put two files in `./corpus` and restart: they are ingested once in the
    background; restart again and nothing is re-ingested (NFR-RAG-04).
13. Time a recalled turn against plan 010's baseline on the same question. The
    documents corpus should cost a fraction of a second per round; if it does
    not, `RECALL_SNIPPET_HITS` is the first knob to turn.

## Definition of Done

Ten units landed, every test scenario passing, the thirteen verification steps
performed (7, 8, 10, 13 on a GPU node), `README.md` and `AGENTS.md` updated.

## Not in scope

OCR for scanned PDFs; `.docx`, `.html` and mail formats; per-group or
per-conversation document scoping; attaching a document to a single turn instead
of the corpus; an ANN index; reranking snippets with the chat model; re-snippeting
in place when the size knobs change (re-ingest instead); watching `corpus_dir`
for changes while the app runs; editing a snippet.
