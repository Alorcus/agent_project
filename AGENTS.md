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

## Plan documents: what to build, not how we got there

`docs/plans/*.md` describe the implementation someone is about to write. They
are not a record of the discussion that produced them.

- **Keep:** what gets built — requirements, data shapes, signatures, file
  lists, schema, the order of operations, test scenarios, verification steps.
- **Keep: pitfalls.** A trap a competent implementer would otherwise fall into,
  stated as a rule with one sentence of why: "`end` is a SQLite keyword — quote
  it", "don't NFKC the whole string first, it shifts every offset in the index
  map". The *why* is there so the rule survives someone's tidy-up, not to
  justify the design.
- **Cut:** rejected alternatives, "an earlier iteration did X", the reasoning
  that led to a decision, risk tables that restate pitfalls in prose, open
  questions that are really musings, and research trails. State the decision;
  delete the argument for it.

**A revision should not make the plan longer.** Removing a feature means
deleting its text, not adding a passage explaining that it was removed —
readers of the plan need the design, and the history is in git. If an edit that
deletes something grows the file, that is the smell: find the paragraph
apologising for the change and cut it too.

Applies equally to a plan being revised mid-discussion: answer the question in
chat, and write to the plan only what the implementation needs.

## Layout

```
src/agentchat/
  log.py           file-handler plumbing shared by the app log and the LLM transcript
  config.py        settings + the single wiring point for backends and stores
  core/            domain model, chat orchestration, context strategy, errors
    prompts.py     the assistant's own system prompt (DEFAULT_SYSTEM) plus the
                    extraction, enrichment and consultation prompt text, and
                    nothing else — edit this file to tune how the assistant
                    answers, summary/keyword quality, routing quality, or the
                    wording of the injected memory/consultation block, not
                    chat.py, extraction.py, enrichment.py, or delegation.py
    extraction.py  ExtractionService: two LLM calls, summary then keywords
    enrichment.py  MemoryEnricher: keyword-matches a user message against the
                    group's other summaries and tracks what's been used
    agents.py      SubAgent, the shipped roster, and @mention parsing
    delegation.py  DelegationService: route → task → specialist, one
                    Consultation or None, one entry point for every failure
    usage.py       CallUsage/TurnUsage, the context meter's figures: a call's
                    prompt plus what it generated, recorded in two steps
                    because the halves are known at different times. Recorded
                    from *inside* the backends, like the transcript and for
                    the same reason — only they hold the tokenizer — but it
                    lives here because core and ui are what read it. Inert
                    unless a collector is installed, which only
                    ChatService.stream_reply does
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
