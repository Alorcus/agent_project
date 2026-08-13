# agentchat

Terminal chat client for the Intelligent Agents course project.

**Status: real models.** Replies come from actual weights — Phi-4-mini-instruct
and Qwen3-14B, loaded in-process from the cluster's shared checkpoint directory
and streamed token by token. The mock backend is still there, behind a switch,
for working on the interface without a GPU.

## Run it

```bash
uv run agentchat
```

`uv` resolves the interpreter and dependencies from `pyproject.toml`; there is
no separate install step.

Needs a GPU node — the first message loads the weights, which takes a moment.
Without one, or on a laptop:

```bash
AGENTCHAT_BACKEND=mock uv run agentchat
```

Tests:

```bash
uv run pytest
```

The suite runs against the mock and needs no GPU. To exercise the real weights
as well:

```bash
AGENTCHAT_TEST_REAL_MODEL=1 uv run pytest
```

## Models

| Model | Size | Window | Thinking mode |
|---|---|---|---|
| `phi-4-mini` | 3.8B | 32768 | prompted (no native mode) |
| `qwen3-14b` | 14B | 16384 | native, via the chat template |

`Ctrl+O` switches between them at runtime; only one is resident at a time, so
switching evicts the other from the GPU.

The context windows are set below what the model configs advertise (40960 for
Qwen3, 131072 for Phi). The limit is KV-cache memory, not the architecture: on
a 40 GB A100, Qwen3-14B's weights take ~29 GB and its cache costs ~0.33 MB per
token. `AGENTCHAT_MAX_CONTEXT` lowers them further for a smaller GPU.

**Weights are never downloaded.** Checkpoints are read from disk by path with
`local_files_only=True`. Passing a Hugging Face repo id instead would pull tens
of gigabytes into `~/.cache/huggingface` per model, per user; a missing
checkpoint fails loudly instead.

## Keys

| Key | Action |
|---|---|
| `Enter` | Send |
| `Escape` | Stop the running generation |
| `Ctrl+N` | New conversation, in the group the current one belongs to |
| `Ctrl+G` | New conversation, choosing the group — or making one |
| `Ctrl+L` | Open the conversation overview |
| `Ctrl+X` | Delete the highlighted conversation (overview) or group (chooser) |
| `Ctrl+O` | Cycle model |
| `Ctrl+T` | Toggle thinking mode |
| `Ctrl+D` | Exit |
| `Delete` | Delete forward in the prompt |

`Ctrl+D` is a priority binding, so it exits even while the prompt has focus —
which is where it usually is. The cost is `Input`'s own `Ctrl+D` binding
(delete-forward); use `Delete` for that instead.

`Ctrl+N` inherits rather than asking: working inside a project means starting
several chats inside it, so the group you are in is a better guess than "no
group" is. The line at the top of the screen states which group that is, and
so answers "where will the next chat land?" before you press it. A
conversation's group is fixed at creation and cannot be changed afterwards,
which is why `Ctrl+G` exists and why the header is worth a permanent row.

## Configuration

Environment variables, all prefixed `AGENTCHAT_`:

| Variable | Default | Meaning |
|---|---|---|
| `AGENTCHAT_BACKEND` | `local` | `local` for real weights, `mock` for the stub |
| `AGENTCHAT_MODEL_ROOT` | `/sc/projects/sci-lippert/intelligent-agents/model_checkpoints` | Where the checkpoints live |
| `AGENTCHAT_MODEL` | first registered | Model selected at startup (`phi-4-mini`, `qwen3-14b`) |
| `AGENTCHAT_MAX_CONTEXT` | unset | Cap every model's context window, for a smaller GPU |
| `AGENTCHAT_STORE` | `sqlite` | `sqlite` is the only store |
| `AGENTCHAT_DATA_DIR` | `./data` | Where `agentchat.db` lives (the `sqlite` store) |
| `AGENTCHAT_CORPUS_DIR` | `./corpus` | RAG ingestion source (not yet used) |
| `AGENTCHAT_SIMULATE_FAILURE` | `0` | Make the second model fail on load, to exercise error handling |
| `AGENTCHAT_EXTRACT_SUMMARIES` | `1` | Derive a summary and keywords for a conversation when it is left |
| `AGENTCHAT_EXTRACTION_TIMEOUT` | `30.0` | Seconds `Ctrl+D`/`Ctrl+Q` wait for extraction before exiting anyway |
| `AGENTCHAT_ENRICH_MESSAGES` | `1` | Append matching summaries from the group to a user message before it reaches the model |
| `AGENTCHAT_LOG_LLM_IO` | `1` | Write a verbatim request/response transcript of every model call to `llm.jsonl` |
| `AGENTCHAT_LOG_DIR` | `<data_dir>/logs` | Where `agentchat.log` and `llm.jsonl` are written |

Variables can also go in a `.env` file at the project root (copy
`.env.example`) instead of being exported in the shell. Real environment
variables take precedence over `.env`.

**No migrations.** `agentchat.db`'s schema only ever grows by hand-written
`CREATE TABLE IF NOT EXISTS` statements; there is no upgrade path from an
older schema. A database written before conversation groups landed is refused,
not silently rewritten — the app raises naming the file. Delete it and restart
to get a fresh one: `rm data/agentchat.db` (or whatever `AGENTCHAT_DATA_DIR`
points at).

## Conversation summaries

Every conversation gets a dense summary and up to five keywords, produced by
two LLM calls on the active model (summary first, keywords from the summary)
and stored in `conversation_summaries`, keyed by conversation and carrying the
group id. Extraction fires when a conversation is left — switching away
(`Ctrl+N`/`Ctrl+G`/`Ctrl+L`) runs it in the background and the status bar
shows `summarising…` for as long as it's in flight, without blocking the next
turn; quitting (`Ctrl+D`/`Ctrl+Q`) shows the same status line but waits for
it, bounded by `AGENTCHAT_EXTRACTION_TIMEOUT`. A conversation with no new
messages since its last summary is not re-summarised.
`AGENTCHAT_EXTRACT_SUMMARIES=0` switches the feature off.

## Recalling earlier conversations

In a **project** group, sending a message checks the group's other
conversations' summaries for a keyword hit against what you typed — a
whole-phrase, case-insensitive match, not a substring. Up to three matching
summaries are appended to the copy of your message the model sees, behind a
short note explaining they're background from earlier chats. Under your
message a muted line appears — `▸ enriched by 2 memories` — that expands on
click to show what was sent.

Each summary is used at most once per visit to a conversation; switching away
and back makes it available again. Nothing about the enrichment reaches the
database: `conversation.messages` keeps exactly what you typed, and reopening
the conversation later shows no trace of it. It never runs in the default
group, and never at the cost of your own message — if the appended summaries
would push a reply over the model's context window, the turn is resent
without them rather than dropped.

**Known limitation:** an enriched reply is itself summarised when that
conversation is later left, so injected material can be folded into *that*
conversation's own summary and recalled a second time from a third
conversation. Accepted as a limitation of a keyword-only recall mechanism
rather than engineered around.

`AGENTCHAT_ENRICH_MESSAGES=0` switches the feature off.

## Logs

Every call into a model backend writes two JSON lines to
`<data_dir>/logs/llm.jsonl`, appended across runs: a **request** record
holding exactly the string handed to the tokenizer (the mock backend has no
tokenizer, so its `prompt_text` is `null`), and a **response** record holding
exactly what the model returned — special tokens included, un-stripped. The
two share a `call_id`; a `label` (`chat`, `extraction.summary`,
`extraction.keywords`) says which call site produced them. Nothing is
truncated, redacted, or summarised, at any size — the file is an instrument,
not a second opinion, and it **contains full conversation text in
cleartext**, the same exposure `agentchat.db` already has.

Reading it:

```bash
jq -r 'select(.label=="chat" and .type=="request") | .prompt_text' data/logs/llm.jsonl
jq -c 'select(.call_id=="<id>")' data/logs/llm.jsonl        # one exchange
jq -r 'select(.outcome=="error") | .error' data/logs/llm.jsonl
```

`AGENTCHAT_LOG_LLM_IO=0` switches it off — no file is opened and no record is
built. `AGENTCHAT_LOG_DIR` moves both this and the application log
(`agentchat.log`) out from under `data_dir`.

## Layout

```
src/agentchat/
  log.py           file-handler plumbing shared by the app log and the LLM transcript
  config.py        settings + the single wiring point for backends
  core/
    models.py      Message, Conversation, ConversationSummary
    chat.py        turn orchestration — the only thing that knows how a reply is made
    context.py     ContextStrategy seam (context-management elective)
    prompts.py     extraction and enrichment prompt text, transcript rendering, keyword parsing
    extraction.py  ExtractionService — two LLM calls, summary then keywords
    enrichment.py  MemoryEnricher — keyword matching and the once-per-visit session ledger
    errors.py      every failure the UI is expected to render
  llm/
    base.py        LLMProvider protocol — the app/backend boundary
    registry.py    model catalogue, residency, runtime switching
    local.py       real backend (transformers)
    mock.py        the stub backend
    transcript.py  verbatim request/response log of every `generate()` call — see "Logs"
  storage/
    base.py        ConversationStore protocol + shared ordering/guard helpers
    schema.py      the DDL, the default-group seed, and the old-database guard
    sqlite.py      durable implementation — three tables, one save per turn
  ui/
    app.py         Textual application
    widgets.py     message bubbles and the group/title header
    screens.py     conversation overview and group chooser
    app.tcss       styling
```

The dependency direction is one-way: `ui → core → llm/storage`. The UI never
imports a concrete backend.

## What's in place

- Generation runs on a cancellable worker — scrolling, model switching and
  stopping stay live while tokens arrive.
- The backend is behind a protocol (`LLMProvider`); swapping backends touches
  `config.py` only.
- Model switching is a runtime action (`Ctrl+O`), and the registry keeps only
  one model resident at a time.
- Every prompt is assembled by a `ContextStrategy`, so smarter context
  management is a swap of one object.
- Backend failure surfaces as a UI error, not a crash — set
  `AGENTCHAT_SIMULATE_FAILURE=1` and switch to the second model to see it.
- Assistant messages record which model produced them.
- Durable storage — conversations survive a restart via `SqliteStore`. For a
  throwaway run, point `AGENTCHAT_DATA_DIR` at a scratch directory, e.g.
  `AGENTCHAT_DATA_DIR=$(mktemp -d) uv run agentchat`.
- Conversation list, switching and deletion — `Ctrl+L` opens an overview of
  saved conversations and switches to one; `Ctrl+X` deletes the highlighted
  one behind a confirmation, and the overview stays open so several can be
  cleared in a row.
- Conversation groups — chats can be filed into projects, chosen at creation
  with `Ctrl+G` and inherited by `Ctrl+N`. The overview shows one block per
  group. `Ctrl+X` in the group chooser deletes a group **and every
  conversation in it** — they are not re-homed, because a conversation's group
  cannot change — so the confirmation names how many chats are about to go.
  The default group is not deletable.
- Conversation summaries — a dense summary and up to five keywords, derived
  on leaving a conversation and stored per group. See "Conversation
  summaries" above.
- Message enrichment — a user message in a project group is checked against
  the group's other conversations' summaries, and up to three keyword matches
  are appended before the model sees it. See "Recalling earlier
  conversations" above.
- LLM I/O transcript — every model call writes a verbatim request/response
  pair to `llm.jsonl`, recorded from inside the backend so it reflects what
  the model actually saw and said. See "Logs" above.

## Not yet built

Deliberately stubbed, with the seam in place:

- The two required fine-tunes.
- Adaptive RAG, sub-agent deployment, intelligent context management (electives).
- History virtualisation for very long conversations.

See `requirements.md` for the full non-functional requirement set.
