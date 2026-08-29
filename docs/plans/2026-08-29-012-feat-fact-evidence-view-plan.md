---
artifact_contract: ce-unified-plan/v1
artifact_readiness: proposed
execution: code
product_contract_source: ce-plan-bootstrap
origin: docs/requirements.md
title: "feat: Fact evidence view — facts on the left, the phrases they are grounded in on the right"
date: 2026-08-29
depth: standard
---

# feat: Fact evidence view — facts on the left, the phrases they are grounded in on the right

**Target repo:** this repo (`agentchat`), branch `feat/fact-evidence-view` off
`feat/RAG-pipeline-fact-extraction`
**Builds on:** plan 009 (`Fact`, `Phrase`, `fact_phrases`) and plan 010
(`fact_phrases.quote`). Read-only over both: no new table, no column, no LLM
call, no change to extraction or retrieval.

---

## What this builds

`Ctrl+F` opens a modal over the chat. The group's facts are listed on the
left; highlighting one shows on the right the window of messages it was
extracted from, with each anchored phrase highlighted in place.

```
┌ Facts · Thesis ────────────────────────────────────────────────────────┐
│ The deadline moved to     │ The deadline moved to 14 March because the  │
│ 14 March because the …  ◀ │ reviewer is away.                          │
│ Anna owns the ingest      │ Ingest planning · messages 4–10            │
│ pipeline                  │                                            │
│ The pilot runs on two     │ You                                        │
│ nodes                     │ can we still make the 7th? [the reviewer   │
│                           │ is away] until then                        │
│                           │                                            │
│                           │ Assistant · qwen3-8b                       │
│                           │ No — [the deadline moved to 14 March].     │
└────────────────────────────────────────────────────────────────────────┘
  ↑↓ fact · enter open its chat · tab evidence · esc close
```

The highlight is drawn from the stored span (`Phrase.start`/`end`), never by
re-matching the quote text at view time — `core/anchoring.py` already decided
where the phrase is, and the view must show that decision, not a second one.

## Requirements

| ID | Requirement |
|---|---|
| R1 | One screen lists every fact of the current conversation's group. |
| R2 | Highlighting a fact shows the messages of the window it was extracted from, in order. |
| R3 | Each phrase of the selected fact is highlighted in place, at its stored span. |
| R4 | The evidence names its source: conversation title, window range, and each message's author. |
| R5 | A phrase whose message is not in the window is still shown, as its stored quote text. |
| R6 | The screen does no I/O: it is handed the facts and the conversations they came from. |
| R7 | Enter switches to the selected fact's conversation; Escape closes and changes nothing. |
| R8 | A group with no facts says so rather than opening an empty list. |
| R9 | Opening the view never cancels a running generation or extraction. |
| R10 | Facts of the conversation currently open are shown against its live messages, not the stored copy. |

---

## Design

### The projection

A fact points at spans; a view needs messages. `core/evidence.py` is the pure
function between them — no store, no widget, testable without a pilot, the
same contract `core/anchoring.py` holds.

```python
@dataclass(frozen=True)
class Excerpt:
    message: Message
    #: Sorted, non-overlapping, in `message.content` coordinates.
    spans: tuple[tuple[int, int], ...]

@dataclass(frozen=True)
class Evidence:
    excerpts: tuple[Excerpt, ...]
    #: Quotes whose message is not in the window, in phrase order.
    orphans: tuple[str, ...]

def evidence_for(fact: Fact, conversation: Conversation) -> Evidence
def merge_spans(spans: Iterable[tuple[int, int]]) -> tuple[tuple[int, int], ...]
```

`fact.window_start`/`window_end` index `countable(conversation.messages)`, not
`conversation.messages` — the window slice must go through `core.facts.countable`
or it lands on the wrong turns as soon as a conversation holds a system or an
empty message.

### Where it runs

```
Ctrl+F ─▶ ChatApp.action_open_facts  (bare @work)
            └─ ChatService.facts_with_sources(group_id)
                 └─ store.list_facts + store.load per distinct conversation_id
            └─ push_screen_wait(FactBrowser(facts, conversations, group_name))
                 └─ ListView.Highlighted ─▶ evidence_for ─▶ EvidenceExcerpt widgets
            └─ result: conversation id ─▶ ChatApp._switch_to
```

Bare `@work`, like `action_open_conversations`: not the generation group and
not exclusive, so opening the view cannot cancel a turn (R9).

---

## Output Structure

```
src/agentchat/
  core/
    evidence.py      NEW  Excerpt, Evidence, evidence_for, merge_spans
    chat.py          +    facts_with_sources
  ui/
    screens.py       +    FactBrowser, facts_for_display
    widgets.py       +    EvidenceExcerpt
    app.py           +    Ctrl+F binding, action_open_facts
    app.tcss         +    #facts, .facts__* rules
tests/
  test_evidence.py   NEW  the projection, spans, orphans, clamping
  test_app.py        +    open, arrow, enter, escape, empty group
README.md            +    Ctrl+F row, a section on the view
AGENTS.md            +    core/evidence.py in the layout map
```

---

## Implementation Units

### U1. `core/evidence.py` — the projection

`merge_spans`: sort by start, fold overlapping and touching pairs into one.
Merging is what makes the output canonical enough to assert on, and it keeps
two quotes that anchored to overlapping regions from being drawn as two
highlights with a seam between them.

`evidence_for`:

1. `items = countable(conversation.messages)`.
2. `start, end = fact.window_start, min(fact.window_end, len(items))`;
   if `start >= end` the window is empty and every phrase is an orphan.
3. Group `fact.phrases` by `message_id`.
4. For each message in `items[start:end]`, clamp each of its spans to
   `len(message.content)` and drop any that clamps empty, then `merge_spans`.
5. Orphans: `phrase.quote` for every phrase whose `message_id` is not in the
   window, skipping empty quotes.

Pitfalls:

- **Clamp, don't trust.** A span is stored against the message content as it
  was when anchored. It is out of range if that content ever changes, and
  `Text.stylize` would silently draw nothing while the list still claimed a
  highlight — clamping and dropping makes the miss visible as an orphan-free
  excerpt rather than a lie.
- **Phrases written before plan 010 have `quote == ""`.** They highlight fine
  (the span is all the view needs) but contribute no orphan text; skipping
  empties is what keeps a blank line out of the pane.
- No `Message` lookup by id across conversations: a phrase belongs to the
  window or it is an orphan. The store has no message-by-id read and this
  view is not the reason to add one.

### U2. `ChatService.facts_with_sources`

```python
async def facts_with_sources(
    self, group_id: str, *, live: Conversation | None = None
) -> tuple[list[Fact], dict[str, Conversation]]:
```

`store.list_facts(group_id)`, then one `store.load` per distinct
`fact.conversation_id`. A `None` from `load` is skipped — the conversation was
deleted under us and its facts went with it (a race; the cascade means the
next call sees neither).

`live` overrides the loaded copy for its own id (R10): the stored copy lags the
in-memory one for the duration of a turn, and a fact extracted mid-turn would
otherwise render against messages the store has not seen yet.

### U3. `ui/screens.py` — `FactBrowser`

```python
class FactBrowser(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(
        self,
        facts: list[Fact],
        conversations: dict[str, Conversation],
        group_name: str,
    ) -> None
```

Dismisses with a conversation id (Enter) or `None` (Escape). Takes loaded
data and does no I/O, exactly as `ConversationPicker` and `GroupChooser` do
(R6).

`facts_for_display(facts, conversations)` — module-level, beside
`groups_for_display`: conversations most-recently-updated first, facts within
one by `window_start` ascending, which is the order they were said in.
`store.list_facts` promises no order, so this is the only thing that gives the
list one.

Composition: `Horizontal(ListView#facts-list, VerticalScroll#facts-evidence)`
inside `Vertical#facts`, with the title row above and the hint row below.

- A row is `Static(_one_line(fact.text))` — whitespace collapsed, truncated to
  `_LABEL_CELLS` with an ellipsis. The list pane is a fixed width, so the
  truncation is deterministic and a test can assert on it; the untruncated
  text is the first thing the evidence pane shows.
- `item.fact_id = fact.id`, the same handle-on-the-ListItem pattern the picker
  uses for `conversation_id`.
- `on_list_view_highlighted` rebuilds the evidence pane: `remove_children`,
  then mount the fact text, a muted source line
  (`{title} · messages {window_start}–{window_end}`), then one
  `EvidenceExcerpt` per excerpt, then the orphans as muted quoted lines.
  Textual delivers a widget's messages one at a time, so held arrow keys
  queue rather than interleave — the handler may be `async` and await its
  mounts.
- Empty group (R8): no `ListView` at all, a single
  `Static("No facts extracted yet.", classes="picker__empty")`, and `on_mount`
  skips the focus call. The picker's empty branch is the precedent.

Pitfall: `ListView.Highlighted` fires once on mount, which is what renders the
first fact — do not also render it from `on_mount`, or the pane is built twice
and the second build races the first one's mounts. `event.item` is `None` for
an empty list; return early.

### U4. `ui/widgets.py` — `EvidenceExcerpt`

```python
class EvidenceExcerpt(Vertical):
    """One message of a fact's window: an author header and the message body
    with the fact's phrases highlighted."""

    def __init__(self, excerpt: Excerpt, model_name: str | None = None) -> None
```

Header reuses `_ROLE_LABEL` and the `" · "` join already in this module. Body
is a `Static` holding a `rich.text.Text`:

```python
text = Text(excerpt.message.content)
for start, end in excerpt.spans:
    text.stylize("reverse", start, end)
```

Pitfalls:

- **Pass the `Text`, never `str(text)`.** A `Text` object carries its spans
  and bypasses markup parsing outright — which is also what keeps a message
  containing `[bold]` from being read as a tag, the trap `markup=False` covers
  everywhere else in this file.
- **`reverse`, not a colour.** Rich style strings in a `Text` are resolved by
  Rich, not the CSS engine, so `$accent` would not resolve and a literal
  colour would fail against one of the two themes.

### U5. `ui/app.py` + `app.tcss` + docs

`Binding("ctrl+f", "open_facts", "Facts")` — free at both levels; `Input`
binds no `ctrl+f`, so no `priority=True` is needed.

```python
@work
async def action_open_facts(self) -> None:
    facts, conversations = await self.chat.facts_with_sources(
        self.conversation.group_id, live=self.conversation
    )
    group = self._groups.get(self.conversation.group_id)
    chosen = await self.push_screen_wait(
        FactBrowser(facts, conversations, group.name if group else "")
    )
    if chosen is not None and chosen != self.conversation.id:
        await self._switch_to(chosen)
```

CSS: `#facts { width: 90%; height: 80%; }` with the picker's border, panel
background and padding; `#facts-list { width: 44; }`; `#facts-evidence
{ width: 1fr; padding-left: 2; }`. Reuse `.picker__title`, `.picker__hint`,
`.picker__empty`. New: `.facts__item` (height 1), `.facts__source` and
`.facts__orphan` (`color: $text-muted`), `.facts__claim` (`text-style: bold`,
`padding-bottom: 1`).

Docs: a `Ctrl+F` row in README's key table and a short section under
"What gets remembered" saying the view exists and that highlights are the
stored anchors; one line for `core/evidence.py` in the AGENTS.md layout map.

---

## Verification

`tests/test_evidence.py` (no pilot, no provider):

| Scenario | Expectation |
|---|---|
| Fact over a full window | one excerpt per countable message of `[window_start, window_end)`, in order |
| Conversation holding a system and an empty message | the window skips them — the slice goes through `countable` |
| Two phrases in one message | one excerpt, two spans, sorted |
| Overlapping and touching spans | merged into one |
| Span past the end of the content | clamped, or dropped when it clamps empty |
| Phrase pointing at a message outside the window | no excerpt for it; its quote appears in `orphans` |
| Phrase with `quote == ""` outside the window | no orphan line |
| `window_end` past the end of the conversation | clamped, no `IndexError` |

`tests/test_app.py` (headless pilot, mock backend, facts written straight to
the store via `factories.make_fact`/`make_phrase`):

| Scenario | Expectation |
|---|---|
| `ctrl+f` with facts in the group | `FactBrowser` on the stack, one row per fact, evidence pane rendered for the first |
| `down` | the evidence pane shows the second fact's window |
| `enter` on a fact of another conversation | screen dismissed, `app.conversation.id` is that fact's conversation |
| `escape` | screen dismissed, conversation unchanged |
| `ctrl+f` in a group with no facts | the empty line, no `ListView` |
| `ctrl+f` mid-generation | the generation worker is still running afterwards |

Manual: `AGENTCHAT_BACKEND=mock uv run agentchat`, hold a conversation long
enough for a window to extract, `Ctrl+F`, arrow through the facts and confirm
each highlight sits on the words the fact is made of.

## Definition of Done

- `uv run pytest` green, `core/evidence.py` covered by unit tests alone.
- No new store method beyond `facts_with_sources`' use of existing ones, and
  no schema change.
- `ui → core` only: `screens.py` and `widgets.py` import `core.evidence`, and
  `core/evidence.py` imports nothing from `ui`.

## Not in scope

- Editing, deleting or re-extracting a fact from the view.
- Searching or filtering the list; showing facts across groups.
- Jumping from a highlight into the chat log at that message.
- Showing a fact's embeddings, retrieval scores or coverage figure.
