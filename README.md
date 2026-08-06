# agentchat

Terminal chat client for the Intelligent Agents course project.

**Status: foundation milestone.** The interface is real; the model backend is a
mock. Replies are fabricated text streamed on a realistic cadence — nothing here
reflects an actual language model yet.

## Run it

```bash
uv run agentchat
```

`uv` resolves the interpreter and dependencies from `pyproject.toml`; there is
no separate install step (NFR-D-01, NFR-D-02).

Tests:

```bash
uv run pytest
```

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

Environment variables, all prefixed `AGENTCHAT_` (NFR-Q-03):

| Variable | Default | Meaning |
|---|---|---|
| `AGENTCHAT_DATA_DIR` | `./data` | Durable state (not yet written) |
| `AGENTCHAT_CORPUS_DIR` | `./corpus` | RAG ingestion source (not yet used) |
| `AGENTCHAT_MODEL` | first registered | Model selected at startup |
| `AGENTCHAT_SIMULATE_FAILURE` | `0` | Make the second model fail on load, to exercise error handling |

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

## What this milestone establishes

The point of building against a mock is to lock in the properties that are
painful to retrofit:

- **Generation runs on a cancellable worker.** Scrolling, model switching and
  stopping all stay live while tokens arrive (NFR-U-04). The mock streams with
  jittered delays specifically so this is exercised.
- **The backend is behind a protocol.** `LLMProvider` is the whole surface a
  real model has to implement; swapping the mock touches `config.py` only.
- **Model switching is a user action** (`Ctrl+O`), not a config edit
  (NFR-U-05), and the registry keeps one model resident at a time (NFR-P-05).
- **Every prompt is assembled by a `ContextStrategy`,** so the context-management
  elective is a swap of one object rather than a rewrite (NFR-CTX-*).
- **Backend failure is a UI error, not a crash** (NFR-Q-02) — set
  `AGENTCHAT_SIMULATE_FAILURE=1` and switch to the second model to see it.
- **Assistant messages record which model produced them** (NFR-FT-10).

## Not yet built

Deliberately stubbed, with the seam in place:

- Durable storage — `InMemoryStore` satisfies the protocol; SQLite replaces it
  (NFR-S-01, NFR-S-03).
- Conversation list, switching and deletion (NFR-U-03, NFR-U-06).
- Real model backends and the two required fine-tunes (NFR-FT-01..10).
- Adaptive RAG, sub-agent deployment, intelligent context management (electives).
- History virtualisation for very long conversations (NFR-U-02).

See `requirements.md` for the full non-functional requirement set.
