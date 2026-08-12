# Conversation groups — UI design

**Companion to** `docs/conversation-groups.md`, which states the group *model*
(bare `§` references below are into that file). This document covers only what
a user sees and presses. It is a proposal: Part 5 lists the decisions that are
still open, each with a recommendation.

**It supersedes § 1.9's first two bullets.** § 1.9 sketched a *group tree
sidebar* plus a group picker modal. There is no sidebar here. Grouping shows up
in three places instead — a breadcrumb in the header, a chooser at creation, and
group blocks inside the existing conversation overview. The reason is that a
sidebar costs permanent horizontal space for information that is relevant at
exactly two moments (when you create a chat, and when you go looking for one),
and the overview modal already owns the second moment.

Group *deletion* (§ 1.4) was deliberately left out of the first draft; it is
designed in Part 5.7, and lives in the chooser.

---

# Part 1 — The three surfaces

## 1.1 The breadcrumb header

The conversation header states the group, left-aligned, at the top of the
screen, and does not repeat per turn (§ 1.9's last bullet).

```
 Thesis  ›  Chapter 3 — related work          ← a project group
 Why is the sky blue                          ← the default group: title only
```

The default group's name (`"Chats"`) is never shown. It is the absence of a
group, as far as the user is concerned; printing `Chats › …` above every
unfiled chat would make the common case look like the special one.

**The header is load-bearing, not decoration.** Because `Ctrl+N` inherits the
current group (§ 1.2), this line is also the answer to "where will the next
chat land?" — it is what makes inheritance predictable instead of surprising,
and the reason the group half is worth a permanent row.

**This replaces the built-in `Header`.** Textual's `Header` centres the app
title, and a TUI cannot afford two header rows. `agentchat` as a standing label
carries no information after the first second of use; which chat you are in
does. A `ConversationHeader` widget in `ui/widgets.py`, docked top, height 1,
padded to line up with `#statusbar`, takes its place.

| Concern | Decision |
|---|---|
| Styling | group name in `$text-muted`, `›` in `$text-muted`, title in the default colour — the title is what you scan for |
| Long titles | the title ellipsises, the group name never does. The group is the shorter, higher-signal half, and it is fixed-width per group so the header does not jitter as you switch chats |
| Untitled chats | a fresh conversation is `"New conversation"` and reads as such until `autotitle()` fires |
| Update timing | fold `_refresh_header()` into `_refresh_status()`. Every point where the conversation changes already calls it, so the two cannot drift — and it means the header picks up the auto-derived title mid-turn, at the first streamed chunk, for free |

**The header must not build its text with Rich markup.** Titles are derived
from user input (`Conversation.autotitle()`), so a chat that opens with
`[bold]` would be parsed as markup — the same trap `MessageBubble` avoids with
`markup=False` (`ui/widgets.py:32`). Because the header *does* want two styles
in one line, the fix is a `rich.text.Text` assembled from styled spans rather
than an f-string with markup tags; user text goes in as a plain span and is
never parsed.

## 1.2 Choosing a group at creation — `Ctrl+N`, and `Ctrl+G`

§ 1.3 is the constraint everything here answers to: membership is chosen at
creation and **bound for life**, so creation is the only chance to get it
right, and it has to be "obvious and cheap".

**`Ctrl+N` stays instant and inherits the current conversation's group.** In a
`Thesis` chat it starts another `Thesis` chat; in an unfiled chat it starts
another unfiled one. No modal, no extra keystroke over today.

The reasoning is that group membership is *sticky in practice*: working inside
a project means starting several chats inside it, and a chooser on every
`Ctrl+N` would tax the common case to guard against the rare one. Inheritance
guards it instead — the group you are already in is, by construction, the group
you were last thinking about, which makes it a far better default than "Chats"
would be.

**`Ctrl+G` is the deliberate act: choose a different group, or make one.**

```
┌─ New conversation in… ───────────────────────┐
│  Thesis                    current  3 chats  │  ← pre-highlighted
│  Cluster ops                        0 chats  │
│  Chats                              7 chats  │
│                                              │
│  + New group…                                │
│                                              │
│ enter start here · ctrl+x delete group · esc…│
└──────────────────────────────────────────────┘
```

- **The current conversation's group is pre-highlighted and marked `current`.**
  Inheritance is the baseline everywhere, so the chooser opens on it and you
  arrow *away* from it — which makes the choice you are about to make legible
  as a change rather than a fresh decision.
- **The default group is an ordinary row.** It is how you get *out* of a
  project: `Ctrl+G`, select `Chats`, Enter.
- **Escape cancels the conversation**, it does not fall back to any group.
  Having pressed `Ctrl+G` you are mid-choice; declining to choose is declining
  to create. (`Ctrl+N` is still right there if what you wanted was the fast
  path after all.)
- **`+ New group…` is a row, and Enter edits it in place.** The label is
  replaced by an input; you type a name; a second Enter creates the group and
  starts a conversation in it.

```
│  Thesis                    current  3 chats  │
│  Cluster ops                        0 chats  │
│  Chats                              7 chats  │
│                                              │
│  Group name…▌                                │  ← was "+ New group…"
│                                              │
│  enter creates the group and starts here     │
```

  **There is no second chord.** An earlier draft made this `Ctrl+G` again from
  inside the chooser; the row is better, because it is the only affordance in
  the list that is *visible*. Enter also keeps one meaning throughout the
  chooser — "start a conversation here" — with this row differing only in that
  it has to be told where "here" is first.

  Creating a group and starting a chat in it stays one gesture: there is no way
  to make a group without also making a conversation, which is the same
  "created for a purpose" shape § 1.3 gives conversations.

- **Enter on an empty field cancels**, restoring the `+ New group…` label. Two
  Enters in a row is the natural "I changed my mind", and it should not create
  a group called `""`.

```
│  New group: Thesis▌                          │
│  enter creates and starts here · esc backs out│
```

  Enter creates the group *and* dismisses with it selected. Typing a name is
  already an unambiguous statement of where you want to be; making you then
  find the new row in the list and press Enter again would be ceremony.

**The modal does no I/O.** `ui/screens.py`'s opening docstring makes this the
house rule, and the chooser keeps it: it is handed a `list[Group]` and dismisses
with `("existing", group_id)` or `("create", name)`. The app is what calls
`ChatService.create_group()`, so a `StorageError` surfaces through the existing
`notify(..., severity="error")` path instead of inside a modal that has no way
to report it.

**Startup does not ask.** `on_mount` keeps landing in the default group — the
first conversation of a session has no current group to inherit, and being
interrogated before you have typed anything is hostile. `Ctrl+G` is right
there.

## 1.3 The grouped overview — `Ctrl+L`

The existing picker keeps its shape (one `ListView`, inline delete), loses its
per-row metadata, and grows group blocks.

```
┌─ Conversations ──────────────────────────────┐
│  Thesis                                      │
│      Chapter 3 — related work                │
│      Reviewer 2 rebuttal                     │
│      Do I need ethics approval for this      │
│                                              │
│  Cluster ops                                 │
│      (no conversations)                      │
│                                              │
│  Why is the sky blue                         │
│  Groceries for the week                      │
│                                              │
│  + New conversation                          │
│                                              │
│ enter switch · ctrl+g group · ctrl+x delete… │
└──────────────────────────────────────────────┘
```

The hint line grows one entry. At `#picker`'s 60 cells (56 usable, `app.tcss`)
`enter switch · ctrl+g group · ctrl+x delete · esc cancel` is 56 exactly — it
fits, with nothing to spare, so a fifth binding here would need a second line.

| Rule | Detail |
|---|---|
| Order | project groups first by `created_at`, **default group last** |
| Headers | one non-selectable title row per project group; the default block has none |
| Row | **the title, and nothing else** — one line per conversation |
| Indent | project conversations indent (`padding-left`, not a literal tab); default-group conversations sit flush at the left margin |
| Within a block | most-recently-updated first — exactly what `list_all_conversations()` already returns, grouped client-side |
| `+ New conversation` | stays last, outside every block. Enter on it is `Ctrl+N` — a chat in the current conversation's group; `Ctrl+G` from the overview opens § 1.2's chooser instead |
| Empty state | unchanged: "No saved conversations yet." when there is not a single conversation in any group |

**One line per conversation.** The `N turns · Aug 12 14:02` line goes
(`ui/screens.py:80-84`, and `.picker__meta` with it in `app.tcss`). It was
metadata about a conversation, offered where the question being asked is
*which* conversation — the title answers that and the rest is noise. Halving
the row height also roughly doubles how many chats fit before the list scrolls,
which matters more once rows are split across group blocks.

Two consequences to accept deliberately:

- **The ordering is now unexplained.** The timestamp was the only visible
  reason a row sat where it did; within a block the order is still
  most-recently-updated first, but nothing on screen says so. Most-recent-first
  is the convention a list of chats is read with, so this is fine — but it is a
  choice, not an oversight.
- **`-current` is the only per-row state left.** The marker on the conversation
  you are in (`.picker__item.-current`) now carries the whole burden of "you
  are here", so it should stay a visible accent rather than a subtle one.

**Default-last is a presentation choice and lives in the UI.** The store
contract is the opposite — `list_groups()` promises default *first*
(§ 1.5, `by_default_first()` in `storage/base.py:18`), because the tree wants
the group that is always there at a stable anchor. Reversing it for display is
a `groups_for_display()` helper in `ui/screens.py`; `by_default_first()` is not
touched. The chooser in § 1.2 uses the same display order and does not special
case the default group's *position* at all — what it pre-highlights is the
current conversation's group, wherever that row happens to sit.

**Group headers are `ListItem(disabled=True)`.** Textual 8.2.8's `ListView`
skips disabled children in `action_cursor_up`/`action_cursor_down` and moves
the initial index off a disabled row on mount, so arrowing through the list
walks conversations only and the headers are unreachable — see Part 3.3 for the
one hole in that.

## 1.4 The bindings, in one place

Groups add exactly one global binding. `Ctrl+N` and `Ctrl+L` keep their
meanings; what changes is that `Ctrl+N` now inherits rather than always landing
in the default group.

| Key | Where | Action |
|---|---|---|
| `Ctrl+N` | chat screen, overview | new conversation in the **current** group — no modal |
| `Ctrl+G` | chat screen, overview | new conversation, choosing the group |
| `Enter` | chooser | start a conversation in the highlighted group — or, on `+ New group…`, name one first |
| `Ctrl+L` | chat screen | the overview, now grouped |
| `Ctrl+X` | overview | delete the highlighted conversation (unchanged; never a group) |
| `Ctrl+X` | chooser | delete the highlighted group **and its conversations** — see Part 5.7 |

`README.md`'s key table gains the `Ctrl+G` row and a note on `Ctrl+N`.

---

# Part 2 — What changes below the UI

Nothing in `storage/` changes; the group model landed whole (§ 2.1–2.3). The
UI needs three things it cannot reach today, because `ui → core → storage` is
one-way and the UI never touches a store.

| Addition | Where | Why |
|---|---|---|
| `ChatService.list_groups()` | `core/chat.py` | the chooser and the overview both need names and order |
| `ChatService.create_group(name) -> Group` | `core/chat.py` | § 2's "no group can be created through the application" is the gap this closes; it wraps `store.save_group(Group(name=name))` |
| A group-name lookup on the app | `ui/app.py` | the header renders synchronously and only holds `conversation.group_id`; it cannot await a store call per repaint. A `dict[str, Group]` refreshed on mount, after `create_group`, and whenever the overview loads groups |

**Collapse the null overload while here.** `ChatService.list_conversations`
still takes `group_id: str | None = None` and delegates to
`list_all_conversations()` on `None` (`core/chat.py:44-49`) — the ambiguity
§ 1.8 removed from the store and the deviation list flags as "worth collapsing
when the UI becomes group-aware". This is that moment: the UI calls
`list_all_conversations()` for the overview and never needs the union method.

---

# Part 3 — Mechanics that will bite

## 3.1 Modal over modal

`Ctrl+G` inside the overview has to end up in the chooser, which is a second
modal. The existing flow already does the right thing by accident: the overview
dismisses with `("new", None)` and the *app* then runs
`action_new_conversation` (`ui/app.py:130-131`), so a follow-on screen is
pushed after the overview is gone rather than on top of it. `PickerResult`
gains a third case — `("choose", None)` — routed the same way. Keep that shape,
one modal on screen at a time, and `_chat_screen = screen_stack[0]`
(`ui/app.py:73-78`) stays valid.

## 3.2 An `Input` inside a `ListView` row

Editing `+ New group…` in place puts a focused `Input` inside a `ListItem`,
which is not a combination Textual arranges for. Checked against 8.2.8:

| Key | What happens unguarded | Needed |
|---|---|---|
| `enter` | fine — `Input` binds it (`_input.py:121`) and consumes it, so `ListView.action_select_cursor` never fires | nothing |
| `up` / `down` | `Input` does **not** bind them; they bubble to `ListView`, which moves the cursor off the row being edited while its input keeps focus | swallow while editing |
| `escape` | `Input` does **not** bind it; it bubbles to the screen and closes the whole chooser | intercept: cancel the edit, restore the label |

All three fit the guard the overview already uses for delete confirmation
(`ConversationPicker.on_key`, `ui/screens.py:119-131`): while a mode is active,
stop the event here so it never reaches binding resolution. The chooser gets
the same shape with `_editing` in place of `_confirm_delete_id`.

**Focus has to be handed back.** `ListItem` is `can_focus=False`, so the
`ListView` normally holds focus for the whole list; mounting an `Input` and
focusing it moves focus out of the list. Whichever way the edit ends — created,
cancelled, or rejected — focus must return to the `ListView` explicitly, or the
chooser goes keyboard-dead with no visible cause.

**A rejected name stays in edit mode.** § 5.3's duplicate check reports in the
hint line and leaves the input focused with the text intact, so fixing a name
does not mean retyping it.

## 3.3 The programmatic-index hole

`ListView.validate_index` clamps to range but does **not** skip disabled rows.
Cursor keys are safe; assignment is not. `refresh_conversations` assigns
directly (`ui/screens.py:151`), so after deleting the last conversation of a
project group the restored index can land on a group header, and
`highlighted_child` will hand back that header. Two consequences:

- the index restore has to step off a disabled row (forward, then back);
- `action_delete_highlighted` and `on_list_view_selected` need a guard for a
  header row regardless — cheap, and it means neither depends on Textual's
  skip behaviour staying as it is.

## 3.4 Deleting the last conversation in a group

Deleting a conversation can empty a project group. The group survives — a
conversation delete is not a group delete — so the block stays, with its
"(no conversations)" row. Nothing cascades upward, which is worth stating
because § 1.4's cascade runs the other way and is easy to misremember.

---

# Part 4 — What the tests should pin

Through the headless pilot, in the style of `tests/test_app.py`:

| Test | Pins |
|---|---|
| header shows `group › title` for a project group, bare title for the default | § 1.9's last bullet, and the default group's invisibility |
| a conversation titled `[bold]red` renders literally in the header | the markup trap in § 1.1 |
| `Ctrl+N` inside a project chat → the new conversation carries that group | inheritance, the whole of § 1.2's fast path |
| `Ctrl+N` inside a default-group chat → the new one is in the default group | the same rule, in the case that looks like the old behaviour and must not be confused with it |
| `Ctrl+N` opens no modal | inheritance is instant, or it is not worth having |
| `Ctrl+G`, arrow to another group, `Enter` → `conversation.group_id` is that group | § 1.3's only chance, exercised |
| `Ctrl+G`, select the default group from inside a project chat → the new conversation is unfiled | the way out of a project |
| `Ctrl+G`, `Enter` on `+ New group…`, type a name, `Enter` → group exists in the store and the conversation is in it | closes the § 2 deviation |
| `Enter` on `+ New group…` with the field left empty restores the label and creates nothing | Part 3.2's cancel path |
| `Escape` while editing the name cancels the edit only — the chooser stays open | the escape guard, the one that fails silently if forgotten |
| after an edit ends, any way it ends, arrow keys still move the list | focus handed back to the `ListView` |
| Escape in the chooser creates nothing and leaves the current conversation intact | declining to choose is declining to create |
| overview row order: project blocks by `created_at`, default block last, headers only on project blocks | § 1.3's ordering |
| an overview row renders the title and nothing else | the meta line is gone and does not creep back |
| arrowing from the last row of one block to the next skips the header | the disabled-row contract |
| deleting the last conversation of a project group leaves the header, and the restored index is not on it | Part 3.3 and 3.4 |
| `Ctrl+X` in the chooser, then `y` → the group and its chats are gone, other groups' chats are not, and the chooser stays open | Part 5.7's delete |
| the confirmation names the group and how many chats go with it | the blast radius, which is the whole point of asking |
| `Ctrl+X` on the default group refuses without a prompt, and `y` afterwards deletes nothing | § 1.4's first guard, at the surface a user meets it |
| `Ctrl+X` on the group you are currently in → a fresh unfiled conversation, and the deleted one is not written back | Part 5.7's last bullet |

---

# Part 5 — Open questions, with recommendations

**5.1 (settled) `Ctrl+N` inherits; `Ctrl+G` chooses.** The alternative
considered was a chooser on every `Ctrl+N` with the default pre-highlighted.
Rejected: it taxes every new chat to guard a case inheritance already guards
better, since the current group is a sharper guess than "Chats" ever is. The
residual risk is different from the one that alternative addressed — not "chats
pile up in the default group" but **"a chat lands in `Thesis` because the last
one did, and you did not look at the header."** § 1.1's breadcrumb is the whole
mitigation, which is why it is described there as load-bearing. Worth watching
in use: if that misfiling happens in practice, the fix is a brief confirmation
in the status bar after an inherited `Ctrl+N` (`new chat in Thesis`), not a
modal.

**5.2 (settled) The header replaces Textual's `Header`.** One row, left
aligned, breadcrumb only.

**5.3 Are duplicate group names rejected?** The schema permits them — ids are
distinct — but two `Thesis` blocks in the overview are indistinguishable.
*Recommendation: reject case-insensitive duplicates in the chooser*, reported
in the hint line. It is a UI-level rule, not an invariant; the store stays as
it is.

**5.4 Do empty project groups show?** *Recommendation: yes*, with a muted
"(no conversations)" row. A group whose last chat you deleted should not
silently disappear — and when group deletion lands (§ 1.4), an empty group is
exactly the one you want to be able to find and remove.

**5.5 Indent width: 2 cells or 4?** Reopened by the single-line row. The
argument for 2 was that rows carry a two-line body, and they no longer do —
with the meta line gone there is no vertical rhythm separating rows, so
indentation carries more of the "these belong to that header" signal on its
own. *Recommendation: 4*, with 56 usable cells still leaving 52 for a title
that is capped at 41 by `autotitle()`. One value in `app.tcss`; worth looking
at both before settling.

**5.6 Should the chooser show per-group conversation counts?** They are free
when the overview already loaded the conversations, and one extra store round
trip when `Ctrl+G` is pressed cold. *Recommendation: show them* — a count is
what tells you whether the group you are about to pick is the one you were
thinking of. Drop it if the round trip is visible in practice.

**5.7 (settled) Group deletion lives in the chooser, not the overview.**
§ 1.4 is the most destructive action in the application. This section
previously proposed `Ctrl+X` on an overview group header; what shipped is
`Ctrl+X` in the **chooser** instead, because the chooser is the one surface
that lists every group — including the empty ones, which are exactly the ones
worth removing — and a row there is a group rather than a header above
conversations. The overview's headers stay non-selectable, so `Ctrl+X` there
still means "the highlighted conversation" and never silently escalates to
its group.

```
┌─ New conversation in… ───────────────────────┐
│  Thesis                    current  3 chats  │
│  Cluster ops                        0 chats  │
│  Chats                              7 chats  │
│                                              │
│  + New group…                                │
│                                              │
│ Delete "Thesis" and its 3 chats? y confirms  │
│ — any other key cancels                      │
└──────────────────────────────────────────────┘
```

- **The confirmation names the count**, which is what makes it mean anything
  (§ 1.4): the chooser already holds per-group counts for its rows, so the
  blast radius costs no extra store call.
- **It reuses the overview's inline confirmation**, hint line and all — one
  `_HintLine` mixin, so "only *y* confirms, any other key backs out" is
  written once and cannot drift between the two modals.
- **The default group refuses without asking.** `Ctrl+X` on it replaces the
  hint with `"Chats" cannot be deleted` rather than a prompt, so there is no
  keypress that could delete it by reflex. The store refuses it too
  (`refuse_default_group`); the UI-side message is there to explain, not to
  enforce.
- **The delete does not dismiss the chooser**, exactly as in the overview: it
  posts `DeleteRequested`, the app does the store work and hands the remaining
  groups back through `refresh_groups`.
- **If the group held the conversation you were in**, that conversation is
  gone with it — § 2.5 forbids re-homing — and the chat screen behind the
  modal falls back to a fresh, unsaved conversation in the default group. The
  fresh one is never persisted while it is empty, so nothing is resurrected.

**5.8 Should the group be visible in the status bar too?** *Recommendation:
no.* The header states it, and the status bar is per-turn state (model,
thinking, turns, trimming). Two places to read the same fact is two places to
keep in sync.
