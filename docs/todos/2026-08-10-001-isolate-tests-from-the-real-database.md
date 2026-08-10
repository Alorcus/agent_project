---
title: "test: isolate the suite from the real database and the real .env"
date: 2026-08-10
status: done
area: tests
---

# test: isolate the suite from the real database and the real `.env`

## Problem

`tests/` runs against the developer's own configuration. `config.py` calls
`load_dotenv(find_dotenv(usecwd=True))` at import time, so the repo-root `.env`
is merged into `os.environ` before any test runs, and `Settings()` fields all
default off `AGENTCHAT_*`. A bare `Settings()` in a test therefore resolves to:

```
backend  = mock      # from .env, not from the code default
store    = sqlite
data_dir = <repo>/data
build_store(...) -> SqliteStore   # on ./data/agentchat.db, the real file
```

Two consequences, one already fixed by hand and one still live:

- **The real database is writable from tests.** `tests/test_app.py:22`
  documents the incident: *"Without this every app test writes a real
  ./data/agentchat.db as a side effect."* The fix there is one
  `overrides.setdefault("store", "memory")` inside that file's `mock_settings`
  helper. Nothing generalises it — a new test file, or one `ChatApp()` /
  `build_store(Settings())` that forgets the override, silently reopens the
  production file. `test_app.py` also exercises *deletion*
  (`test_ctrl_x_shows_inline_confirm_and_y_deletes`), so the failure mode is
  destructive, not just noisy.

- **The suite is red because of `.env`.** `tests/test_local.py:81`
  (`assert Settings().backend == "local"`) fails with `assert 'mock' == 'local'`
  purely because the local `.env` sets `AGENTCHAT_BACKEND=mock`. Verified on
  `feat/conversation-switching`: **1 failed, 78 passed, 1 skipped**. The test is
  correct; the environment leak is the bug.

Note: a full run today does *not* mutate `data/agentchat.db` (checksum stable
across a run). This todo is about removing the standing hazard and fixing the
red test, not about a reproduction happening on every run.

## Goal

Tests never read the developer's environment and never touch a path outside
`tmp_path`. Getting that wrong should fail loudly, not silently, and should not
depend on every test author remembering an override.

## Proposed work

1. **Add `tests/conftest.py` with an autouse environment fixture.**
   Strip every `AGENTCHAT_*` key from `os.environ` (`monkeypatch.delenv`) so
   `Settings()` reflects the *code* defaults, which is what `test_local.py:81`
   and the other wiring tests are asserting about. This alone turns the suite
   green.

   `load_dotenv` runs at `agentchat.config` import time, so clearing the vars
   per-test is enough — the fixture just has to run after the import, which
   autouse function-scoped fixtures do.

2. **Point storage at a tmp path by default.** In the same autouse fixture set
   `AGENTCHAT_DATA_DIR` to a per-test `tmp_path` subdirectory, so even a test
   that deliberately asks for `store="sqlite"` lands on a throwaway file. This
   is the "mock database" in the practical sense: the real `SqliteStore` code
   under a disposable path, which keeps `test_storage.py` honest about actual
   SQL behaviour instead of testing a stand-in.

3. **Promote the store fixture.** `mock_settings` in `test_app.py` and
   `fast_registry` in `test_core.py` / `test_switching.py` are three
   near-identical helpers. Move one `mock_settings(**overrides)` into
   `conftest.py`, keep `store="memory"` as its default, and delete the copies.
   `InMemoryStore` stays the mock store — no new fake type is needed; it already
   implements `ConversationStore` and is exercised side-by-side with
   `SqliteStore` in `test_switching.py:121-125`.

4. **Guard the real path.** Add a check that fails the test if a `SqliteStore`
   is constructed under the repo's `./data`. Cheapest version: an autouse
   fixture asserting `data/agentchat.db`'s mtime is unchanged at teardown, or a
   monkeypatched `SqliteStore.__init__` that raises when the path is not under
   `tmp_path`. Prefer the second — it names the offending test instead of
   failing whichever test happens to run last.

5. **Document it.** One line in `AGENTCHAT.md` under "Working in this repo":
   tests run with a scrubbed environment; `.env` does not apply to them.

## Acceptance

- `uv run pytest` is green with a populated `.env` present at the repo root.
- `uv run pytest` is green with no `.env` at all.
- `data/agentchat.db` is byte-identical before and after a full run, and is not
  created if it did not exist.
- A test written as `ChatApp()` or `build_store(Settings())`, with no
  overrides, fails with a clear message about the real data directory rather
  than quietly succeeding.

## Files

- `tests/conftest.py` (new)
- `tests/test_app.py` — drop the local `mock_settings`
- `tests/test_core.py`, `tests/test_switching.py` — drop the local `fast_registry`
- `AGENTCHAT.md` — note the test environment contract

## Resolution

Implemented as scoped above, with one refinement: `fast_registry` was also
promoted to `conftest.py` (built on top of `mock_settings`) rather than left
duplicated in `test_core.py` and `test_switching.py`, since it was the same
near-duplicate the "three near-identical helpers" note already called out.

- `tests/conftest.py` — two autouse fixtures (`_scrubbed_environment`,
  `_guard_real_database`) plus `mock_settings` / `fast_registry` helpers.
- `_scrubbed_environment` strips every `AGENTCHAT_*` var per test and pins
  `AGENTCHAT_DATA_DIR` to `tmp_path`, so a bare `Settings()` never resolves to
  the repo's `./data` even without an explicit override.
- `_guard_real_database` monkeypatches `SqliteStore.__init__` to raise
  `AssertionError` if a path under the real `data/` directory is ever opened —
  a second layer for anything that bypasses the env redirect (e.g. an
  explicit absolute path).
- `AGENTS.md` documents the contract.

Verified:
- `uv run pytest` → 79 passed, 1 skipped, 0 failed, with `.env` present.
- Same result with `.env` removed (previously: `test_local_backend_is_the_default`
  failed with `assert 'mock' == 'local'`, caused by the checked-in `.env`).
- `data/agentchat.db` byte-identical (md5 `af6ab668e99677a295d15b02d09b117b`)
  before and after both runs.
- Guard confirmed to raise for an explicit real-path `SqliteStore(...)` call.
