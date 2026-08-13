# Agent notes for agentchat

## Commenting style: good code documents itself

Docstrings and comments should carry only what the code can't say for itself.

- **Module docstring:** one or two sentences — what this file is, and any
  property a reader would otherwise get wrong (e.g. "weights are read by path,
  never downloaded"). Not a design essay.
- **Class/function docstrings:** only when the name and signature don't
  already say what it does. `def touch(self) -> None:` needs nothing.
  `def cycle(self) -> ModelInfo:` benefits from "advance to the next
  registered model" because "cycle" alone is ambiguous.
- **Inline comments:** only for the *why* that isn't inferable from the code —
  a workaround for a library bug, a non-obvious ordering requirement, a magic
  number that needs justifying. Never restate what the next line already
  shows.
- **No requirement-tag citations in code.** Design rationale that ties back to
  a spec (`NFR-*` or similar) belongs in the spec doc / commit message /
  PR description, not sprinkled through docstrings. If a comment leans on a
  tag to justify itself instead of standing on its own, cut the tag and
  tighten the sentence, or cut the comment.

When in doubt: if deleting a comment loses no information a careful reader
would need, delete it.

## Layout

```
src/agentchat/
  log.py           file-handler plumbing shared by the app log and the LLM transcript
  config.py        settings + the single wiring point for backends and stores
  core/            domain model, chat orchestration, context strategy, errors
    prompts.py     the extraction and enrichment prompt text and nothing else
                    — edit this file to tune summary/keyword quality or the
                    wording of the injected memory block, not extraction.py
                    or enrichment.py
    extraction.py  ExtractionService: two LLM calls, summary then keywords
    enrichment.py  MemoryEnricher: keyword-matches a user message against the
                    group's other summaries and tracks what's been used
  llm/             LLMProvider protocol, mock + local (transformers) backends, registry
    transcript.py  verbatim request/response log, recorded *inside* local.py
                    and mock.py on purpose — it sits below the streamer's
                    cleanup and the app's message trimming, which a wrapper
                    around LLMProvider.generate() could not see. Don't "tidy"
                    the recording calls out of the backends into one; that
                    silently makes the transcript describe the wrong thing.
  storage/         ConversationStore protocol and its SQLite implementation
  ui/              Textual application, widgets, and modal screens
```

Dependency direction is one-way: `ui → core → llm/storage`. The UI never
imports a concrete backend.

## Working in this repo

- Tests: `uv run pytest` (runs against the mock backend, no GPU needed).
  `AGENTCHAT_TEST_REAL_MODEL=1 uv run pytest` also exercises real weights.
- `tests/conftest.py` scrubs every `AGENTCHAT_*` variable per test (an autouse
  fixture) and points the sqlite store at `tmp_path`, so a local `.env` never
  changes what a test sees and no test can touch `./data/agentchat.db`. Use
  `conftest.mock_settings` / `conftest.fast_registry` rather than building
  `Settings` by hand.
- Run the app: `uv run agentchat` (needs a GPU node), or
  `AGENTCHAT_BACKEND=mock uv run agentchat` on a laptop.
- See `README.md` for configuration and keybindings.
