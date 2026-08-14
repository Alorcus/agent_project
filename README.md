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
| `AGENTCHAT_SUBAGENTS` | `1` | Route a user message to a specialist, when one clearly fits, before answering |
| `AGENTCHAT_SUBAGENT_TIMEOUT` | `60.0` | Seconds each consultation phase (routing+task, then the specialist's answer) is allowed |
| `AGENTCHAT_EXTRACT_FACTS` | `1` | Extract one grounded fact per six-message window as a conversation goes on |
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

## What gets remembered

Alongside the conversation summary, the conversation is read through a
sliding window of six messages stepping four, and each full window is sent
through two LLM calls — one for a fact, one for the quotes that support it —
as the conversation goes on, not only when it's left. Each quote is located
in a real stored message by code, not by the model; a quote that can't be
found is not evidence, and a fact with no located quotes is discarded. A
located quote becomes a `Phrase`, carrying which message and which author (the
user, or a specific model id) it came from — so a fact is always traceable
back to the words that grounded it.

A specialist's advice, consulted mid-turn, is never part of this: it lives
only in `Message.metadata`, never in a message's own text, so it is counted
in no window and quoted in no fact. Facts have no consumer yet — nothing
reads them back into a conversation — they are stored and ready for the recall
mechanism that will read from them later. Summary extraction runs beside this
unchanged; the two features don't interact.

`AGENTCHAT_EXTRACT_FACTS=0` switches the feature off.

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

## Consulting a specialist

Before answering, the active model is asked which of a small roster of
specialists — if any — best fits the message:

| `@id` | Handles |
|---|---|
| `ask_chef` | cooking, recipes, ingredients, kitchen technique, meal planning |
| `ask_bank` | personal banking, interest rates, loans, mortgages, budgeting |
| `ask_tutor` | explaining a concept step by step, teaching, worked examples |

A turn that routes to a specialist is four LLM calls, in sequence, on the one
resident model: **route** (which specialist, or none), **task** (what exactly
to ask it — the specialist never sees the conversation, only this restated
task), **specialist** (its answer), and **reply** (the assistant folds that
answer into the response it actually writes). The specialist advises the
assistant and never speaks to the user directly — its answer is appended to a
throwaway copy of your message, and the reply you see is written by the model
you're talking to, in its own voice, free to correct what the specialist got
wrong.

Under a reply written with a specialist's help, a muted line appears —
`▸ answered with help from Banking` — that expands on click to show the task
it was given and what it answered. Unlike message enrichment, this **is**
persisted: `Message.metadata["subagent"]` survives a restart, since the
specialist's own words appear nowhere else.

Starting a message with `@<id>` (e.g. `@ask_bank what's a good rate right
now?`) forces that specialist and skips the routing call; `@default` forces a
plain turn. An unrecognised `@mention` is not an error — it just falls through
to normal routing.

Grep `llm.jsonl` (see "Logs" below) for `"label":"route"`,
`"label":"subagent.task"`, `"label":"subagent.<id>"`, and `"label":"chat"` to
see each of the four calls in a consulted turn.

`AGENTCHAT_SUBAGENTS=0` switches the feature off.

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
    models.py      Message, Conversation, ConversationSummary, Author, Phrase, Fact
    chat.py        turn orchestration — the only thing that knows how a reply is made
    context.py     ContextStrategy seam (context-management elective)
    prompts.py     the assistant's system prompt, extraction, enrichment, consultation and fact/quote prompt text, transcript rendering, keyword/agent-id/quote parsing
    extraction.py  ExtractionService — two LLM calls, summary then keywords
    enrichment.py  MemoryEnricher — keyword matching and the once-per-visit session ledger
    agents.py      SubAgent, the shipped roster, and @mention parsing
    delegation.py  DelegationService — route → task → specialist, one Consultation or None
    anchoring.py   pure functions that locate a quote in a real message: normalise, anchor, anchor_in, significant, coverage
    facts.py       FactExtractor and the sliding-window arithmetic — one fact per full window, derived from the stored watermark
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
- The assistant answers under a system prompt (`prompts.DEFAULT_SYSTEM`) that
  keeps a reply as short as the question asked for. It is prepended per turn
  and never stored, so editing it changes the next reply in an old
  conversation too.
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
- Sub-agent consultation — a user message is routed to a specialist roster
  entry (or none), which answers a restated task in an isolated context; its
  answer is folded into the assistant's own reply, never streamed to the user
  directly. See "Consulting a specialist" above.

## Not yet built

Deliberately stubbed, with the seam in place:

- The two required fine-tunes.
- Adaptive RAG, intelligent context management (electives).
- Specialists on their own weights (LoRA adapters over the shared resident
  base) — the roster is prompt-differentiated for now; `SubAgent` has no
  `model_id`.
- History virtualisation for very long conversations.

See `requirements.md` for the full non-functional requirement set.
