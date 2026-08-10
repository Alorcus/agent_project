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
  config.py        settings + the single wiring point for backends and stores
  core/            domain model, chat orchestration, context strategy, errors
  llm/             LLMProvider protocol, mock + local (transformers) backends, registry
  storage/         ConversationStore protocol, in-memory and SQLite implementations
  ui/              Textual application, widgets, and modal screens
```

Dependency direction is one-way: `ui → core → llm/storage`. The UI never
imports a concrete backend.

## Working in this repo

- Tests: `uv run pytest` (runs against the mock backend, no GPU needed).
  `AGENTCHAT_TEST_REAL_MODEL=1 uv run pytest` also exercises real weights.
- Run the app: `uv run agentchat` (needs a GPU node), or
  `AGENTCHAT_BACKEND=mock uv run agentchat` on a laptop.
- See `README.md` for configuration and keybindings.
