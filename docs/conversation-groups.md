# Conversation groups — design extract and implementation status

**Sources:** `docs/plans/memory-and-groups.md` (v6), the skeleton
`docs/plans/2026-08-10-002-memory-and-groups-skeleton.md`, and the landed stage
plans (`…-003-memory-stage-0-scaffolding.md`,
`…-004-memory-stage-1-schema-and-stores.md`,
`…-005-memory-stage-2-extraction-write-path.md`,
`…-006-memory-stage-3-read-path.md`). **Those files live on
`feat/memory-pipeline`, not on this branch** — every bare `§` below cites
`memory-and-groups.md`, and this document is the standalone statement of the
part of it that is about groups.

**Scope of this file:** groups as a feature of their own — what a group is, how
a conversation acquires one, how they are listed, and what deleting one means.
Features that *scope data by group* specify their own behaviour in their own
documents; nothing about them is restated here, including the two columns the
schema carries on their behalf (§ 1.1). Part 2 records what exists in the tree
today, checked against `src/` rather than taken from the plans.

---

# Part 1 — What the design plans for groups

## 1.1 What a group is

A group is the container a conversation belongs to, and the unit downstream
features scope by.

| Field | Meaning |
|---|---|
| `id` | PK, text |
| `name` | display name |
| `kind` | `default` or `project` — the only field that carries behaviour |
| `created_at` | ordering key for listing |

Relation: `GROUPS ||--o{ CONVERSATIONS`. The FK is NOT NULL and CASCADE, so the
database cannot represent a conversation without a group (I-1) nor a
conversation whose group is gone.

Two further columns exist on these tables and are **not part of the group
model**: `groups.last_consolidated_at` and the pair
`conversations.extracted_at` / `conversations.extracted_id`. They are trigger
and watermark state owned by the feature that scopes by group; the group model
neither reads nor maintains them.

## 1.2 Two kinds, and the group that is always there

`kind` is `default` or `project`.

- **The default group is seeded with the schema** and is the group a
  conversation lands in when no other is chosen. It always exists, so no code
  path has to handle its absence.
- **It is not deletable** (§ 2.4.1's first guard).
- The kind is the branch point downstream features read. The group model's
  contribution is only that the distinction exists, is durable, and has exactly
  **one predicate** expressing it (`Group.is_project()` in the current code)
  rather than a `kind == "default"` comparison scattered across callers.

## 1.3 Membership: exactly one group, chosen once (§ 2.5)

A conversation is assigned its group **at creation** and **bound to it for
life**.

What immutability buys is that anything derived per group can copy the group id
instead of deriving it by JOIN, with no risk of drift (§ 1.1's "derive what is
mutable, copy what is immutable").

What it costs, stated plainly in the design: **you cannot retroactively file a
chat into a project.** Starting a conversation, realising three turns in that it
belongs to an existing project, and moving it there is a thing both ChatGPT and
Claude support and this design does not. The mitigation is entirely at creation
time — **the new-conversation flow has to make the group choice obvious and
cheap, because it is the only chance to get it right.**

## 1.4 Deleting a group (§ 2.4.1)

**Deleting a group deletes its conversations.** They are *not* re-homed to the
default group: re-homing is a move, and § 2.5 forbids moves. Permitting it would
make the immutability claim false in exactly one path, which is the path
everything derived per group relies on.

```
delete group G
  ├─ is G the default group? → refuse
  ├─ confirm, naming the blast radius
  └─ one transaction:
       DELETE group G
         → cascade: conversations in G → their messages
```

- **The confirmation has to state the blast radius**, not merely ask. Under the
  old move-to-default behaviour a group delete lost the grouping and kept the
  content, which is recoverable-ish; now it destroys conversations and messages
  outright, making it the most destructive action in the application (NFR-U-06).
  The count of what is about to go is what makes the confirmation mean anything.
  Features that scope data by group contribute their own counts and their own
  cascade.
- "For life" is meant literally, including here. The obvious escape hatch —
  re-home a group's conversations instead of deleting them — is the one thing
  § 2.5 cannot permit. If that ever feels too harsh, the section to reopen is
  § 2.5, not this one.

## 1.5 Listing

| Method | Contract |
|---|---|
| `list_conversations(group_id)` | one group's conversations, most-recently-updated first. `group_id` is **required** |
| `list_all_conversations()` | every conversation regardless of group |
| `list_groups()` | **default group first**, then by `created_at` — the tree wants a stable order and the default group is the one that is always there |
| `save_group(group)` | create or update |
| `default_group()` | the seeded group |

`list_conversations(group_id=None)` meaning "every conversation" is the nullable
ambiguity I-1 exists to remove; the unfiltered case gets its own method instead
(§ 1.8).

## 1.6 Ownership

**The conversation store owns `groups` rows** — it is what the UI's group tree
reads through, and the only thing that creates a group. Stores that merely scope
by group read the table and never write it.

## 1.7 Invariants

| ID | Invariant | Enforced by |
|---|---|---|
| I-1 | Every conversation belongs to exactly one group; `group_id` is NOT NULL | schema (FK) and the type (`str`, not `str \| None`) |
| — | The default group always exists and is not deletable | schema seed; the delete guard |

## 1.8 Knock-on corrections to plan 001

Plan 001 shipped `conversations.group_id TEXT` — nullable, no FK, no `groups`
table. This design supersedes that schema, with **no migration path, written
deliberately**: single-user prototype, so the resolution is to drop the
development database and recreate it. There is no `PRAGMA user_version` and none
is proposed. If that stops being true — a second user, or data anyone minds
losing — this is the first thing that has to change.

- `conversations.group_id` becomes NOT NULL with an FK to `groups`, so the
  database cannot represent a state I-1 forbids.
- `list_conversations(group_id=None)`'s null overload is removed.

## 1.9 UI (all of it stage 6 in the skeleton)

- **Group picker modal at creation** — the one chance § 2.5 allows. NFR-P-03: a
  modal, no drag-and-drop.
- **Group tree sidebar** (`ui/widgets.py`), reading through the conversation
  store, default group first.
- **Delete-with-blast-radius confirmation** (§ 1.4).
- The conversation header states which group a chat belongs to, in a place that
  does not repeat per turn.

---

# Part 2 — What is implemented, and where

This branch carries the group model and nothing that scopes by it. The two
columns § 1.1 lists as belonging to a downstream feature —
`groups.last_consolidated_at` and `conversations.extracted_at` /
`extracted_id` — are **not** in this schema; the feature that owns them brings
them when it lands. `Group.kind` is here in full, because the distinction is
the group model's own.

## 2.1 The group model

| Piece | Location |
|---|---|
| `Group` dataclass — `id`, `name`, `kind`, `created_at` | `src/agentchat/core/models.py:25-38` |
| The single kind predicate, `Group.is_project()` | `src/agentchat/core/models.py:35-38` |
| `DEFAULT_GROUP_ID = "default"` | `src/agentchat/core/models.py:12-14` |
| `Conversation.group_id: str = DEFAULT_GROUP_ID` — I-1 in the type | `src/agentchat/core/models.py:65-67` |

`Group` sits in `core/models.py` beside `Conversation`, the table it is the
parent of. § 1.2's "exactly one predicate" is `is_project()`; a feature that
scopes by group reads it rather than comparing `kind` itself.

## 2.2 Schema

| Piece | Location |
|---|---|
| `groups` table | `src/agentchat/storage/schema.py:19-21` |
| `conversations.group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE` | `src/agentchat/storage/schema.py:23-26` |
| Default group seeded on creation (`id="default"`, `name="Chats"`, `kind="default"`) | `src/agentchat/storage/schema.py:16`, `:75-80` |
| Old-database guard — a pre-`groups` database is **refused** with a `StorageError` naming the file, never rewritten, so first launch cannot destroy real conversations | `src/agentchat/storage/schema.py:60-72` |
| `connect()` sets `PRAGMA foreign_keys = ON` — without it the FK above is decorative | `src/agentchat/storage/schema.py:38-44` |

The guard matters more than it looks: plan 001's `conversations` table already
exists in real databases with a nullable, FK-less `group_id`, and
`CREATE TABLE IF NOT EXISTS` would leave it exactly as it is and report
success. § 1.8 chose "drop the database" over a migration — refusing is how
that choice gets made deliberately instead of silently.

## 2.3 Stores

| Piece | Location |
|---|---|
| `ConversationStore` group surface: `list_conversations(group_id)`, `list_all_conversations()`, `list_groups()`, `save_group()`, `default_group()` — null overload gone | `src/agentchat/storage/base.py:25-39` |
| `by_default_first()` — the ordering every implementation's `list_groups` shares | `src/agentchat/storage/base.py:18-21` |
| `InMemoryStore`: same surface, seeds the default group, and **rejects a conversation whose group does not exist** — it has no FK to do it for it, and a stub that permits what the real store forbids lets I-1 break in every test using it | `src/agentchat/storage/base.py:52-88` |
| `SqliteStore` group methods and their sync bodies | `src/agentchat/storage/sqlite.py:33-47`, `:76-101` |

## 2.4 Service and UI

| Piece | Location |
|---|---|
| `new_conversation(group_id=DEFAULT_GROUP_ID)` | `src/agentchat/core/chat.py` |
| `list_all_conversations()`, `list_groups()`, `create_group(name)` | `src/agentchat/core/chat.py` |
| The `group › title` header, and the group-name cache it renders from | `src/agentchat/ui/widgets.py`, `src/agentchat/ui/app.py` |
| `Ctrl+G` group chooser; `Ctrl+N` inherits the current conversation's group | `src/agentchat/ui/screens.py`, `src/agentchat/ui/app.py` |
| Grouped conversation overview, default group's block last | `src/agentchat/ui/screens.py` |

The UI these describe is designed in `docs/conversation-groups-ui.md`, which
supersedes § 1.9's first two bullets: there is no group tree sidebar.

## 2.5 Tests

| Test | File |
|---|---|
| `test_i1_conversation_requires_a_group` — the annotation is `str`, and a null group fails through the store as `StorageError` | `tests/test_invariants.py` |
| `test_the_default_group_always_exists` | `tests/test_invariants.py` |
| `test_default_group_is_seeded_and_is_not_a_project` — a fresh database has exactly one group, `kind="default"` | `tests/test_storage.py` |
| `test_groups_round_trip_with_the_default_group_first` | `tests/test_storage.py` |
| `test_saving_a_conversation_into_an_unknown_group_raises` — the FK from the conversation side | `tests/test_storage.py` |
| `test_list_all_conversations_spans_groups_and_scoped_listing_does_not` — both readings have a name | `tests/test_storage.py` |
| `test_list_conversations_filters_by_group_id` | `tests/test_storage.py` |
| `test_deleting_a_group_cascades_to_its_conversations` — the cascade § 1.4 relies on, exercised by hand because nothing calls for it yet | `tests/test_storage.py` |
| `test_a_pre_groups_database_is_refused_not_rewritten` — the guard leaves the old rows intact | `tests/test_storage.py` |
| `test_in_memory_store_holds_i1_too` | `tests/test_storage.py` |
| `test_store_scopes_listing_by_group`, `test_new_conversation_lands_in_the_group_it_was_given` | `tests/test_core.py` |
| `make_group()` and `make_conversation()` factories | `tests/factories.py` |

---

# Part 3 — Group work not yet implemented

| Item | State |
|---|---|
| **Group deletion** (§ 1.4) — the refuse-default guard, the cascade transaction, and the blast-radius counts the confirmation needs | Not implemented. The conversation store has no `delete_group`; the FK cascade fires if a row is deleted by hand, which is all `test_deleting_a_group_cascades_to_its_conversations` can pin |
| **Delete-with-blast-radius confirmation** | Not started. The overview's group header rows are its natural home, which is why they are real `ListItem`s rather than decorations |
| **Group tree sidebar** | Dropped, not deferred — the UI design replaces it with a header breadcrumb and group blocks in the overview |

## Deviations worth knowing

- **`Group.is_project()` is spelled for the group model alone.** On the branch
  this was extracted from it reads `is_memory_scope()`, naming the feature that
  consumes it. Same predicate, same branch point (§ 1.2); a consumer that wants
  the other name should alias it rather than add a second `kind` comparison.
