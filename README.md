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
| `Ctrl+N` | New conversation |
| `Ctrl+O` | Cycle model |
| `Ctrl+T` | Toggle thinking mode |
| `Ctrl+D` | Exit |

## Configuration

Environment variables, all prefixed `AGENTCHAT_`:

| Variable | Default | Meaning |
|---|---|---|
| `AGENTCHAT_BACKEND` | `local` | `local` for real weights, `mock` for the stub |
| `AGENTCHAT_MODEL_ROOT` | `/sc/projects/sci-lippert/intelligent-agents/model_checkpoints` | Where the checkpoints live |
| `AGENTCHAT_MODEL` | first registered | Model selected at startup (`phi-4-mini`, `qwen3-14b`) |
| `AGENTCHAT_MAX_CONTEXT` | unset | Cap every model's context window, for a smaller GPU |
| `AGENTCHAT_DATA_DIR` | `./data` | Durable state (not yet written) |
| `AGENTCHAT_CORPUS_DIR` | `./corpus` | RAG ingestion source (not yet used) |
| `AGENTCHAT_SIMULATE_FAILURE` | `0` | Make the second model fail on load, to exercise error handling |

Variables can also go in a `.env` file at the project root (copy
`.env.example`) instead of being exported in the shell. Real environment
variables take precedence over `.env`.

## Layout

```
src/agentchat/
  config.py        settings + the single wiring point for backends
  core/
    models.py      Message, Conversation
    chat.py        turn orchestration — the only thing that knows how a reply is made
    context.py     ContextStrategy seam (context-management elective)
    errors.py      every failure the UI is expected to render
  llm/
    base.py        LLMProvider protocol — the app/backend boundary
    registry.py    model catalogue, residency, runtime switching
    local.py       real backend (transformers)
    mock.py        the stub backend
  storage/
    base.py        ConversationStore protocol + in-memory implementation
  ui/
    app.py         Textual application
    widgets.py     message bubbles
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

## Not yet built

Deliberately stubbed, with the seam in place:

- Durable storage — `InMemoryStore` satisfies the protocol; SQLite replaces it.
- Conversation list, switching and deletion.
- The two required fine-tunes.
- Adaptive RAG, sub-agent deployment, intelligent context management (electives).
- History virtualisation for very long conversations.

See `requirements.md` for the full non-functional requirement set.
