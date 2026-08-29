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
| `Ctrl+F` | Open the fact evidence view |
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
| `AGENTCHAT_CORPUS_DIR` | `./corpus` | Document-ingestion source (not yet used) |
| `AGENTCHAT_SIMULATE_FAILURE` | `0` | Make the second model fail on load, to exercise error handling |
| `AGENTCHAT_EXTRACTION_TIMEOUT` | `30.0` | Seconds `Ctrl+D`/`Ctrl+Q` wait for the closing fact flush before exiting anyway |
| `AGENTCHAT_SUBAGENTS` | `1` | Route a user message to a specialist, when one clearly fits, before answering |
| `AGENTCHAT_SUBAGENT_TIMEOUT` | `60.0` | Seconds each consultation phase (routing+task, then the specialist's answer) is allowed |
| `AGENTCHAT_EXTRACT_FACTS` | `1` | Extract one grounded fact per six-message window as a conversation goes on |
| `AGENTCHAT_RECALL_FACTS` | `1` | Run the adaptive retrieval loop over the group's facts before answering |
| `AGENTCHAT_EMBED_MODEL` | `all-MiniLM-L6-v2` | Embedder id a stored vector is tagged with; a vector from another id is re-embedded |
| `AGENTCHAT_EMBED_MODEL_PATH` | `<model_root>/sentence-transformers/all-MiniLM-L6-v2` | Where the embedding checkpoint is loaded from (never downloaded) |
| `AGENTCHAT_RECALL_ROUNDS` | `3` | Hard cap on retrieval rounds before the digest is released as it stands |
| `AGENTCHAT_RECALL_REWRITES` | `3` | Query rewrites generated per seed, each shown the queries already tried |
| `AGENTCHAT_RECALL_HITS` | `10` | Top-k kept per query per view before the union by fact id |
| `AGENTCHAT_RECALL_SEEDS` | `2` | Reseed queries per round, aimed at the judge's gap (capped at 4) |
| `AGENTCHAT_RECALL_MIN_SCORE` | `0.25` | Cosine floor a hit must clear to enter the pool |
| `AGENTCHAT_RECALL_DIGEST_FACTS` | `12` | Facts the digest is capped at, deduplicated and ranked |
| `AGENTCHAT_RECALL_TIMEOUT` | `120.0` | Wall-clock deadline on the whole loop; whatever it has is released |
| `AGENTCHAT_LOG_LLM_IO` | `1` | Write a verbatim request/response transcript of every model call to `llm.jsonl` |
| `AGENTCHAT_LOG_DIR` | `<data_dir>/logs` | Where `agentchat.log` and `llm.jsonl` are written |

The cost of a recalled turn is the **product** of the knobs, not their sum:
worst case at the defaults is `1 + n + (s·n + 1)·(rounds−1)` calls before the
reply. Tune `AGENTCHAT_RECALL_REWRITES` and `AGENTCHAT_RECALL_ROUNDS` down if a
recalled turn runs long, and `AGENTCHAT_RECALL_TIMEOUT` bounds it regardless.

Variables can also go in a `.env` file at the project root (copy
`.env.example`) instead of being exported in the shell. Real environment
variables take precedence over `.env`.

**No migrations.** `agentchat.db`'s schema grows by hand-written
`CREATE TABLE IF NOT EXISTS` statements, plus one guarded `ALTER TABLE` (the
`fact_phrases.quote` column); there is no general upgrade path from an older
schema. A database written before conversation groups landed is refused,
not silently rewritten — the app raises naming the file. Delete it and restart
to get a fresh one: `rm data/agentchat.db` (or whatever `AGENTCHAT_DATA_DIR`
points at).

## What gets remembered

The conversation is read through a sliding window of six messages stepping
four, and each full window is sent through two LLM calls — one for a fact, one
for the quotes that support it — as the conversation goes on, and once more on
leaving it (the partial trailing window is flushed then). Each quote is
located in a real stored message by code, not by the model; a quote that can't
be found is not evidence, and a fact with no located quotes is discarded. A
located quote becomes a `Phrase`, carrying which message and which author (the
user, or a specific model id) it came from, plus the verbatim substring — so a
fact is always traceable back to the words that grounded it.

A specialist's advice, consulted mid-turn, is never part of this: it lives
only in `Message.metadata`, never in a message's own text, so it is counted in
no window and quoted in no fact. Each fact is embedded under two views — its
claim and its evidence — as it is written, so the common path never embeds at
read time.

`AGENTCHAT_EXTRACT_FACTS=0` switches the feature off.

`Ctrl+F` opens the evidence view: the group's facts on the left, and for the
highlighted one, the window of messages it was extracted from with each
phrase highlighted in place. The highlight is drawn from the phrase's stored
span, not by re-matching the quote at view time — it shows where anchoring
decided the phrase was. Enter opens the highlighted fact's conversation;
Escape closes and changes nothing.

## Recalling earlier conversations

In a **project** group, every message crosses a gate: a one-word classifier
call decides whether answering it well needs facts from the group's earlier
conversations. Self-contained messages ("write me a haiku") are answered as
they are. The rest enter a bounded loop over the group's facts — scoped to
that group and nothing else:

1. **rewrite** — the seed (your message, then the judge's gap in later rounds)
   is rewritten into `n` distinct search queries, generated one at a time,
   each shown the queries already tried, and phrased in the vocabulary the
   conversation would have used rather than the vocabulary of a question.
2. **retrieve** — every query is embedded by a small CPU model and scored by
   cosine against the group's fact vectors under both views; each query keeps
   its own top `k`, and the results are unioned by fact id keeping the best
   score.
3. **assemble** — the pool is deduplicated (a fact whose significant tokens
   are a subset of a higher-scored one's is dropped), ranked, and capped into
   a digest. No LLM call: a fact is already a one-sentence summary.
4. **adjudicate** — a judge call scores the digest against your **original**
   message (never a rewrite) and either accepts it or names, in one sentence,
   what is still missing. That sentence is what the next round's queries are
   written from.

The loop terminates on a sufficient verdict, a hard round cap, or a wall-clock
deadline — whichever comes first releases the digest as it stands. The digest
is appended to the copy of your message the model sees, behind a note
explaining it is background from earlier chats and telling the model to fall
back on its own knowledge where the evidence is silent. When the loop hit the
round cap, the note also says the evidence was judged incomplete.

Under your message a muted line appears — `▸ recalled 4 facts · 2 rounds ·
sufficient` — that expands to show the queries the loop ran, the judge's last
gap, and the block that was appended, character for character. Recall
provenance **is** persisted on the assistant reply (`Message.metadata["recall"]`),
so reopening the conversation rebuilds the same note. Nothing about the
injected block reaches `conversation.messages`, and if it would push the reply
over the model's context window the turn is resent without it.

A consultation and recall never happen on the same turn — a consulted turn
does not spend the gate call. Retrieval failure (provider, storage, embedder,
timeout) degrades to a normal reply, never an error or a hang.

`AGENTCHAT_RECALL_FACTS=0` switches the feature off entirely — no embedder
load, no gate call, no `fact_embeddings` reads.

### Setup: the embedding weights

The retrieval loop needs a small sentence encoder on disk (real backend only —
`AGENTCHAT_BACKEND=mock` uses a weightless hashing embedder and needs nothing):

```bash
uv run hf download sentence-transformers/all-MiniLM-L6-v2 \
  --local-dir /sc/projects/sci-lippert/intelligent-agents/model_checkpoints/sentence-transformers/all-MiniLM-L6-v2
```

Or point `AGENTCHAT_EMBED_MODEL_PATH` at wherever the checkpoint already
lives.

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

## The context meter

The right-hand end of the status bar carries a gauge of how full the model's
context window got on the last turn:

```
model: Qwen3-14B  ·  thinking: off  ·  turns: 8      ctx ██████▎░░░░░░░ 7.2k/16.4k
```

It measures what the model actually had to hold, not how long the
conversation is: the **prompt plus the tokens generated from it**, which is
what the KV cache is sized by. Both halves are the tokenizer's own counts. A
turn that drops older messages to fit is shown at its trimmed size, since that
is what was sent.

A turn is often several calls — a consulted turn is four — and the meter shows
the **largest** of them, since that is the one the window had to accommodate.
Hover it for the breakdown and which call it came from (`route`,
`subagent.task`, `subagent.<id>`, `chat`):

> Largest call of the last turn: 5 412 prompt + 806 generated = 6 218 of
> 16 384 tokens (38%), from the chat call, counted by the tokenizer.

A call's completion is only known once it ends, so during a long reply the bar
shows the prompt and catches up when the reply lands. The bar is muted until
80% of the window, amber to 95%, red beyond it. A `~` in front of the figure
means nothing counted it: backends without a tokenizer (the mock) are
estimated at four characters per token.

## Logs

Every call into a model backend writes two JSON lines to
`<data_dir>/logs/llm.jsonl`, appended across runs: a **request** record
holding exactly the string handed to the tokenizer (the mock backend has no
tokenizer, so its `prompt_text` is `null`), and a **response** record holding
exactly what the model returned — special tokens included, un-stripped. The
two share a `call_id`; a `label` (`chat`, `facts.fact`, `facts.quotes`,
`recall.gate`, `recall.rewrite`, `recall.judge`, `recall.reseed`, `route`,
`subagent.task`, `subagent.<id>`) says which call site produced them. Nothing
is truncated, redacted, or summarised, at any size — the file is an instrument,
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
  config.py        settings + the single wiring point for backends, embedder and retriever
  core/
    models.py      Message, Conversation, Author, Phrase, Fact, FactEmbedding
    chat.py        turn orchestration — the only thing that knows how a reply is made
    context.py     ContextStrategy seam (context-management elective)
    prompts.py     the assistant's system prompt, the fact/quote, gate/rewrite/judge/reseed and consultation prompt text, transcript rendering, agent-id/quote/verdict/seed parsing, the injected recall block
    agents.py      SubAgent, the shipped roster, and @mention parsing
    delegation.py  DelegationService — route → task → specialist, one Consultation or None
    anchoring.py   pure functions that locate a quote in a real message: normalise, anchor, anchor_in, significant, coverage
    facts.py       FactExtractor and the sliding-window arithmetic — one fact per full window, derived from the stored watermark
    retrieval.py   Hit, Recall, FactIndex (the only reader of fact_embeddings), and AdaptiveRetriever — the bounded RAG loop
    errors.py      every failure the UI is expected to render
  llm/
    base.py        LLMProvider protocol — the app/backend boundary
    embedding.py   Embedder protocol, LocalEmbedder (CPU sentence encoder), HashingEmbedder (weightless, for tests)
    registry.py    model catalogue, residency, runtime switching
    local.py       real backend (transformers)
    mock.py        the stub backend
    transcript.py  verbatim request/response log of every `generate()` call — see "Logs"
  storage/
    base.py        ConversationStore protocol + shared ordering/guard helpers
    schema.py      the DDL, the default-group seed, the one hand-written migration, and the old-database guard
    sqlite.py      durable implementation — one save per turn
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
- Grounded fact extraction — one fact per six-message window, each quote
  located in a real message by code. See "What gets remembered" above.
- Adaptive fact retrieval — a gated, bounded RAG loop over the group's facts
  (rewrite → retrieve → assemble → judge), scoped to the group, with the
  injected block inspectable per turn. See "Recalling earlier conversations"
  above.
- LLM I/O transcript — every model call writes a verbatim request/response
  pair to `llm.jsonl`, recorded from inside the backend so it reflects what
  the model actually saw and said. See "Logs" above.
- Sub-agent consultation — a user message is routed to a specialist roster
  entry (or none), which answers a restated task in an isolated context; its
  answer is folded into the assistant's own reply, never streamed to the user
  directly. See "Consulting a specialist" above.
- Context meter — the status bar gauges the last turn's largest call, prompt
  and completion together, against the active model's window, counted by the
  backend's own tokenizer. See "The context meter" above.

## Not yet built

Deliberately stubbed, with the seam in place:

- The two required fine-tunes.
- Ingesting user documents (text and PDF) into the retrieval corpus —
  `AGENTCHAT_CORPUS_DIR` is the seam; recall indexes conversation facts only
  for now.
- Intelligent context management (elective).
- Specialists on their own weights (LoRA adapters over the shared resident
  base) — the roster is prompt-differentiated for now; `SubAgent` has no
  `model_id`.
- History virtualisation for very long conversations.

See `requirements.md` for the full non-functional requirement set.
