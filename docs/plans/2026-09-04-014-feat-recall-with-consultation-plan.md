---
artifact_contract: ce-unified-plan/v1
artifact_readiness: proposed
execution: code
product_contract_source: ce-plan-bootstrap
origin: docs/requirements.md
title: "feat: Recall and consultation in the same turn — the specialist gets the evidence too"
date: 2026-09-04
depth: standard
---

# feat: Recall and consultation in the same turn — the specialist gets the evidence too

**Target repo:** this repo (`agentchat`), branch `feat/recall-with-consultation`
off `main`
**Builds on:** plan 010 (`AdaptiveRetriever`, `Recall`, the injected recall
block) and plan 011 (`DelegationService`, the roster, the consultation block).

---

## What this builds

Recall and consultation stop being mutually exclusive. Recall runs first and
unconditionally; the council then runs on top of it, and the retrieved digest is
handed to the specialist alongside its task, so a specialist answers with the
project's own facts and documents in front of it instead of the question alone.

```
user turn
   │
   ▼
recall ──gate says self-contained──▶ (no digest)
   │
   ▼ digest
route ──default──▶ (no specialist)
   │
   ▼
task (T) ──────────────▶ specialist (S)   ◀── digest, as its own block
   │                          │
   └──────────┬───────────────┘
              ▼
   reply: question + consultation block + recall block
```

The authored task does not carry the evidence. It stays what it is — one or two
sentences restating the question — and the digest travels beside it in the same
user message.

---

## Requirements

| ID | Requirement |
|---|---|
| R1 | A turn may recall and consult; neither suppresses the other, and either may happen alone. |
| R2 | Recall runs before delegation. Its digest reaches the specialist as a second block in the specialist's user message; the specialist's prompt is still exactly two messages. |
| R3 | The authored task never carries recalled evidence. `TASK_MAX_TOKENS` and `TASK_CHAR_CAP` are unchanged, and `Consultation.task` stays the restated question. |
| R4 | On a turn that had both, the reply's prompt carries the question, then the consultation block, then the recall block — the recall footer's restated question closes the turn. |
| R5 | Trimming drops the recall block first, the consultation block second, and the user's own question never. |
| R6 | `metadata["subagent"]` and `metadata["recall"]` are written independently, each only when its block actually reached the model. |
| R7 | Each pipeline keeps its own failure funnel: a failed recall still leaves a consultation, a failed consultation still leaves a recall, and both stay `None`-on-failure. |
| R8 | Both switches keep working independently and mean what they say: `AGENTCHAT_SUBAGENTS=0`, `AGENTCHAT_RECALL_FACTS=0`. |
| R9 | A turn that had both shows both notes — the recall note on the user bubble, the consultation note on the assistant bubble — live and after a restart. |

---

## Design

### Order

In `stream_reply`, the recall block moves above the delegation block and the
`consultation is None` guard goes. `DelegationService.consult` keeps its single
entry point and gains a `recall` keyword; it does not need splitting, because
the task is authored without the digest.

Routing is unchanged, mention path included: a mentioned turn still spends no
routing call. Every turn now spends the gate call, which is what "recall runs
unconditionally" costs.

### What the specialist sees

`_agent_messages(agent, task, recall)` still builds two messages — the
specialist's system prompt, and one user message that is the task, or the task
followed by `SPECIALIST_EVIDENCE_HEADER` and the digest. Depth stays structural:
the digest is text, and nothing reachable from it is a `DelegationService`.

The specialist's prompt is not run through `ContextStrategy` and does not need
to be. `AdaptiveRetriever._run` already budgets the digest against the active
provider's context window, and the specialist's prompt is a fraction of the
reply's — system prompt plus ≤600 characters of task plus the same digest.

### What the reply sees

`_prompt_messages` composes rather than chooses. `consulted_text` and
`recalled_text` are replaced by one `injected_text(user_text, consultation,
recall)` that appends whichever blocks exist, consultation first. `recall_block`
is unchanged and is still what both the prompt and `RecallNote` render through.

### Trimming

Three attempts, in order: both blocks, consultation only, neither. Each attempt
checks whether the rebuilt last message survived `decision.dropped`, and the
variables for the blocks that were given up are cleared before the metadata is
written, so the rollback cannot leave metadata claiming a block the reply never
saw.

### Cost

A turn that does both is the sum of both, serialised: gate, then per round
`rewrites` × rewrite plus one judge plus one reseed, then route, task,
specialist, reply. Nothing overlaps — they share `_provider_lock`, and
`TransformersProvider` kills one `generate()` when a second starts. The gate and
the router are what keep the combination rare: most messages are self-contained
and most routes are `default`.

---

## Output Structure

```
tests/
  test_combined_turn.py   NEW — order, both blocks, staged rollback, metadata
```

Modified: `core/chat.py`, `core/delegation.py`, `core/prompts.py`,
`tests/test_retrieval.py`, `tests/test_delegation.py`, `README.md`, `AGENTS.md`.

No UI change. `MessageBubble.show_recall` and `show_consultation` already mount
on different bubbles and are already both called on every turn, live
(`ui/app.py:627`, `:646`) and on restore (`:484`, `:491`).

---

## Implementation Units

### U1. `core/prompts.py` — the specialist's evidence block and the composed injection

**Requirements:** R2, R4 · **Depends on:** nothing

- `SPECIALIST_EVIDENCE_HEADER`: tells the specialist the notes below were
  retrieved for it, that the person it is answering did not write them and
  cannot see them, to use them where they help and fall back on its own
  knowledge where they are silent, and never to mention that a retrieval step
  happened.
- `specialist_text(task: str, recall: "Recall | None") -> str` — the task, or
  the task plus the header plus `recall.digest`. Returns the task unchanged when
  `recall` is `None` or its digest is empty.
- `consultation_block(agent, task, answer) -> str` — the body `consulted_text`
  built, without the user text.
- `injected_text(user_text, consultation, recall) -> str` — `user_text`, then
  `consultation_block(...)` if there is one, then `recall_block(...)` if there is
  one, joined by blank lines. Replaces `consulted_text` and `recalled_text`.

**Pitfalls**

- Render the digest, not `recall_block`, into the specialist's message.
  `RECALL_FOOTER` restates *the user's message*, which the specialist was
  deliberately not given — it would contradict the task it was handed.
- Keep `recall_block` as the one renderer for the injected recall text and the
  `RecallNote`. The note replays `metadata["recall"]["block"]` character for
  character, and a second renderer is how that drifts.
- Order in `injected_text` is consultation then recall, so the recall footer is
  the last thing before the model answers; reversing it buries the question
  behind the digest.

**Tests**: `specialist_text` returns the task untouched with no recall and with
an empty digest, and carries the digest but not the footer otherwise;
`injected_text` emits question → consultation → recall in that order and each
subset correctly.

---

### U2. `core/delegation.py` — carry the recall to the specialist

**Requirements:** R2, R3, R7 · **Depends on:** U1

- `consult(user_text, on_progress=None, *, history=(), recall: Recall | None = None)`.
- `_run(pending, on_progress, recall)` → `_run_agent(agent, task, recall)` →
  `_agent_messages(agent, task, recall)`, whose user message is
  `specialist_text(task, recall)`.
- `Consultation` is unchanged. `_decide` and `_write_task` are unchanged.
- Update the module docstring's second sentence: `_agent_messages` builds the
  specialist's system prompt and one user message carrying its task and, when
  the turn recalled anything, that evidence.

**Pitfalls**

- Import `Recall` under `TYPE_CHECKING` only, the way `prompts.py` does. A
  runtime import pulls `numpy` and the whole retrieval module into a delegation
  path that may be running with recall switched off.
- The digest goes in the specialist's message, never through `parse_task`.
  `TASK_CHAR_CAP` would cut it, and `Consultation.task` is persisted and shown
  in the consultation note as the task the specialist was given.
- `_timeout` still covers each phase separately. A longer specialist prompt does
  not change which phase the deadline belongs to.

**Tests**: a specialist's prompt contains the digest and the task, in that order
of task-then-evidence, and is still two messages; with `recall=None` the prompt
is byte-identical to today's; the persisted `Consultation.task` never contains
digest text.

---

### U3. `core/chat.py` — order, staged rollback, metadata

**Requirements:** R1, R4, R5, R6, R7 · **Depends on:** U1, U2

- Recall moves above delegation and loses the `consultation is None` guard; the
  comment about not spending the gate call goes with it.
- `consult(...)` gains `recall=recall`.
- `_prompt_messages(conversation, consultation, recall)` calls `injected_text`
  and returns the plain messages only when both are `None`.
- The single rollback becomes a staged one: build with both; if the last message
  was dropped, clear `recall` and rebuild; if it was dropped again, clear
  `consultation` and rebuild.
- `TurnResult.recall`'s comment loses "when a consultation suppressed it".

**Pitfalls**

- Clear the variable for each block as it is given up, before the metadata
  writes. Those writes are guarded on `consultation is not None` /
  `recall is not None` and nothing else.
- `ensure_indexed` stays where it is, above `_provider_lock` — it touches the
  store and the CPU embedder, and holding the provider through a cold embedder
  load is what that placement exists to prevent.
- The staged rollback compares against `prompt_messages[-1]` by identity, and
  each rebuild makes a new last message. Re-read `prompt_messages` from the
  rebuild rather than comparing a stale reference.

**Tests**: a turn where the router picks a specialist and the gate says "needs
evidence" produces both a `Consultation` and a `Recall`, and the reply's prompt
carries both blocks; a recall failure leaves the consultation intact and vice
versa; a context window that fits the consultation but not the digest keeps the
consultation, records `subagent` and records no `recall`; one that fits neither
records neither and still sends the user's question.

---

### U4. Tests — the inverted exclusivity assertions

**Requirements:** R1, R8, R9 · **Depends on:** U3

- `tests/test_retrieval.py:480` `test_a_consulted_turn_makes_no_gate_call`
  inverts to `test_a_consulted_turn_also_recalls`: the gate call is spent, the
  call sequence is gate → (rewrite/judge …) → route → task → specialist → chat,
  and `last_turn.recall` and `last_turn.consultation` are both set.
- `tests/test_delegation.py:122` follows `consulted_text` into `injected_text`.
- `tests/test_combined_turn.py` is new and holds U3's scenarios plus the switch
  matrix: subagents off and recall on, recall off and subagents on, both off.
- The app test asserts both notes mount on a turn that had both.

**Pitfalls**

- `scripted_provider` replies are consumed in call order, so every combined-turn
  script has to spell out the recall calls before the delegation ones. Getting
  the order wrong shows up as a routing failure, not as a script error.

---

### U5. Documentation

**Requirements:** all · **Depends on:** U1–U4

- `README.md:206` — the paragraph saying a consultation and recall never happen
  on the same turn is replaced by what now happens: both can, recall runs first,
  and the specialist is given the digest.
- `README.md:260`, `:279` — "a consulted turn is four calls" becomes four plus
  the recall loop's when the gate opens.
- `AGENTS.md` — the `delegation.py` line gains that the specialist is given the
  turn's recalled evidence alongside its task.

---

## Test setup

No new factories. The combined-turn tests use `conftest.mock_settings` with
`recall_facts=True` and `subagents=True`, `HashingEmbedder`,
`make_indexed_group` from the retrieval suite, and a `scripted_provider` whose
replies cover both pipelines in call order.

---

## Verification

1. `uv run pytest` passes.
2. `AGENTCHAT_BACKEND=mock uv run agentchat` — ask, in a group with facts, a
   question that both routes to a specialist and needs earlier context. Both
   notes appear: recall under the question, consultation above the reply (R9).
3. Restart and reopen that conversation: both notes are still there, from
   metadata alone (R6, R9).
4. `llm.jsonl` for that turn: the `recall.gate` entry precedes `route`, and the
   `subagent.<id>` entry's prompt contains the digest while `subagent.task`'s
   reply does not (R2, R3).
5. The `chat` entry's prompt has the question, then the consultation block, then
   the recall block ending in the restated question (R4).
6. Shrink the context window until only the consultation fits: the reply is sent
   with the consultation, the consultation note appears, the recall note does
   not, and `metadata` has `subagent` and no `recall` (R5, R6).
7. `AGENTCHAT_SUBAGENTS=0`: recall alone, exactly as today.
   `AGENTCHAT_RECALL_FACTS=0`: consultation alone, and no gate call in the
   transcript (R8).
8. GPU node, real backend. Ask a specialist-routed question the facts answer and
   check the specialist's answer uses them; time it against a consulted turn
   with `AGENTCHAT_RECALL_FACTS=0` to see what the combination costs.

## Definition of Done

Five units landed, every test scenario passing, the eight verification steps
performed (8 on a GPU node), `README.md` and `AGENTS.md` updated.

## Not in scope

Seeding retrieval from the authored task rather than the user's message; a
second recall aimed at the specialist; letting a specialist trigger retrieval of
its own; a knob restoring the old exclusivity; routing more than one specialist
per turn; running recall and routing concurrently.
