"""Modal screens. Both screens take already-loaded lists and do no I/O
themselves — the app awaits store work and interprets the result."""

from __future__ import annotations

from collections import defaultdict
from itertools import chain
from typing import ClassVar, Literal

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Input, ListItem, ListView, Static

from agentchat.core.models import Conversation, Group

#: ("switch", conversation id), ("new", None) or ("choose", None).
PickerResult = tuple[Literal["switch", "new", "choose"], str | None]

#: ("existing", group id) or ("create", group name).
GroupChoice = tuple[Literal["existing", "create"], str]

_NEW_CONVERSATION_ID = "__new__"
_NEW_GROUP_ID = "__new_group__"
_NEW_GROUP_LABEL = "+ New group…"
_HINT = "enter switch · ctrl+g group · ctrl+x delete · esc cancel"
_CHOOSER_HINT = "enter start here · ctrl+x delete group · esc cancel"
_EDITING_HINT = "enter creates the group and starts here · esc backs out"


def groups_for_display(groups: list[Group]) -> list[Group]:
    """Project groups by creation order, the **default group last** — the
    reverse of the store's `by_default_first`, which anchors the group that is
    always there at the top of a tree. Which way round to read them is a
    presentation choice, so it lives here rather than in the store.
    """
    return sorted(groups, key=lambda g: (not g.is_project(), g.created_at))


def _first_selectable(list_view: ListView, index: int) -> int:
    """`ListView.validate_index` clamps to range but does not skip disabled
    rows the way the cursor keys do, so an assigned index can land on a group
    header. Step forward off it, then back."""
    rows = list_view.children
    for candidate in chain(range(index, len(rows)), reversed(range(index))):
        if not rows[candidate].disabled:
            return candidate
    return index


class _HintLine:
    """The row at the bottom of both modals, and the delete confirmation that
    borrows it.

    Delete asks inline rather than opening a second modal: Ctrl+X replaces the
    hint with a "delete this?" prompt that only "y" confirms — any other key
    backs out, so a stray keypress can't delete. Mixed into a screen that
    composes a `Static` with id `HINT_ID` showing `IDLE_HINT`.
    """

    HINT_ID: ClassVar[str]
    IDLE_HINT: ClassVar[str]

    _pending_delete_id: str | None = None

    def _ask_delete(self, target_id: str, question: str) -> None:
        self._pending_delete_id = target_id
        self._set_hint(f"{question} Y confirms — any other key cancels", alert=True)

    def _answer_pending_delete(self, event: events.Key) -> str | None:
        """Read `event` as the answer to a pending confirmation and return what
        to delete, or `None` if it was declined — or if nothing was pending, in
        which case `event` is left for the screen's own handling."""
        if self._pending_delete_id is None:
            return None
        # While a delete is pending the screen's own bindings (Escape, Ctrl+X)
        # must not fire — stopping the event here keeps it from reaching
        # binding resolution, so only "y" can confirm.
        event.stop()
        event.prevent_default()
        pending, self._pending_delete_id = self._pending_delete_id, None
        self._set_hint(self.IDLE_HINT)
        return pending if event.key == "y" else None

    def _set_hint(self, text: str, *, alert: bool = False) -> None:
        hint = self.query_one(f"#{self.HINT_ID}", Static)
        hint.set_class(alert, "-confirming")
        hint.update(text)


class ConversationPicker(ModalScreen[PickerResult | None], _HintLine):
    """Overview of saved conversations; Enter switches, Escape cancels.

    Conversations are shown in blocks, one per project group, with the
    default group's chats flush at the left margin and unheaded. A row is the
    title and nothing else: the question this screen answers is *which*
    conversation, and turn counts and timestamps are not what answers it.

    Deleting does not dismiss the screen: the picker posts `DeleteRequested`,
    the app does the store work and hands back the new list via
    `refresh_conversations`, so the user stays in the list they were browsing.
    """

    HINT_ID = "picker-hint"
    IDLE_HINT = _HINT

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+g", "choose_group", "Group"),
        Binding("ctrl+x", "delete_highlighted", "Delete"),
    ]

    class DeleteRequested(Message):
        def __init__(self, conversation_id: str) -> None:
            super().__init__()
            self.conversation_id = conversation_id

    def __init__(
        self,
        conversations: list[Conversation],
        current_id: str | None,
        groups: list[Group],
    ) -> None:
        super().__init__()
        self._conversations = conversations
        self._current_id = current_id
        self._groups = groups

    def compose(self) -> ComposeResult:
        with Vertical(id="picker"):
            yield Static("Conversations", classes="picker__title")
            yield self._body()
            yield Static(self.IDLE_HINT, classes="picker__hint", id=self.HINT_ID)

    def _body(self) -> Widget:
        if not self._conversations:
            return Static(
                "No saved conversations yet.", classes="picker__empty", id="picker-body"
            )

        by_group: dict[str, list[Conversation]] = defaultdict(list)
        for conversation in self._conversations:
            by_group[conversation.group_id].append(conversation)

        items: list[ListItem] = []

        def add(item: ListItem, *, starts_block: bool = False) -> None:
            # A blank line between blocks, but not above the first one.
            if starts_block and items:
                item.add_class("-spaced")
            items.append(item)

        for group in groups_for_display(self._groups):
            rows = by_group.get(group.id, [])
            if not group.is_project():
                for position, conversation in enumerate(rows):
                    add(self._item_for(conversation), starts_block=position == 0)
                continue
            add(self._group_header(group), starts_block=True)
            if rows:
                for conversation in rows:
                    add(self._item_for(conversation, nested=True))
            else:
                add(self._disabled_row("(no conversations)", "picker__group-empty"))

        add(
            ListItem(
                Static("+ New conversation", classes="picker__item"),
                id=_NEW_CONVERSATION_ID,
            ),
            starts_block=True,
        )
        return ListView(*items, id="picker-body")

    def _group_header(self, group: Group) -> ListItem:
        return self._disabled_row(group.name, "picker__group")

    @staticmethod
    def _disabled_row(text: str, css_class: str) -> ListItem:
        return ListItem(Static(text, classes=css_class, markup=False), disabled=True)

    def _item_for(self, conversation: Conversation, *, nested: bool = False) -> ListItem:
        classes = "picker__item"
        if nested:
            classes += " -nested"
        if conversation.id == self._current_id:
            classes += " -current"
        item = ListItem(
            Static(conversation.title, classes=classes, markup=False),
            id=f"conversation-{conversation.id}",
        )
        item.conversation_id = conversation.id
        return item

    def on_mount(self) -> None:
        if self._conversations:
            self.query_one(ListView).focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.item.id == _NEW_CONVERSATION_ID:
            self.dismiss(("new", None))
            return
        conversation_id = getattr(event.item, "conversation_id", None)
        if conversation_id is not None:
            self.dismiss(("switch", conversation_id))

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_choose_group(self) -> None:
        """Hand the group chooser back to the app rather than pushing it here:
        one modal on screen at a time keeps `_chat_screen` the bottom of the
        stack."""
        self.dismiss(("choose", None))

    def action_delete_highlighted(self) -> None:
        list_views = self.query(ListView)
        if not list_views:
            return
        item = list_views.first().highlighted_child
        # Group headers and "+ New conversation" have no conversation behind
        # them, so neither is deletable.
        conversation_id = None if item is None else getattr(item, "conversation_id", None)
        if conversation_id is None:
            return
        conversation = next(
            (c for c in self._conversations if c.id == conversation_id), None
        )
        title = conversation.title if conversation is not None else "this conversation"
        self._ask_delete(conversation_id, f'Delete "{title}"?')

    def on_key(self, event: events.Key) -> None:
        confirmed = self._answer_pending_delete(event)
        if confirmed is not None:
            self.post_message(self.DeleteRequested(confirmed))

    async def refresh_conversations(
        self, conversations: list[Conversation], current_id: str | None
    ) -> None:
        """Swap in a new list without closing the screen, keeping the
        highlight where it was so the row after a deleted one is selected."""
        highlighted = self._highlighted_index()
        self._conversations = conversations
        self._current_id = current_id

        await self.query_one("#picker-body").remove()
        await self.query_one("#picker", Vertical).mount(
            self._body(), before="#picker-hint"
        )

        list_views = self.query(ListView)
        if not list_views:
            return
        list_view = list_views.first()
        list_view.index = _first_selectable(
            list_view, min(highlighted, len(list_view.children) - 1)
        )
        list_view.focus()

    def _highlighted_index(self) -> int:
        list_views = self.query(ListView)
        if not list_views:
            return 0
        return list_views.first().index or 0


class GroupChooser(ModalScreen[GroupChoice | None], _HintLine):
    """Where the next conversation should live — the one chance to say so,
    since membership is bound for life.

    Escape cancels the conversation rather than falling back to a group:
    having pressed Ctrl+G you are mid-choice, and declining to choose is
    declining to create.

    `+ New group…` is an ordinary row that Enter edits in place, so Enter
    keeps one meaning throughout — "start a conversation here" — with that row
    differing only in that it has to be told where "here" is first.

    Ctrl+X deletes a group, which takes its conversations with it — the most
    destructive action in the application, so the confirmation names the count
    that is about to go. Like the picker's, the delete does not dismiss the
    screen: the chooser posts `DeleteRequested` and the app hands back the
    remaining groups via `refresh_groups`.
    """

    HINT_ID = "chooser-hint"
    IDLE_HINT = _CHOOSER_HINT

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+x", "delete_highlighted", "Delete group"),
    ]

    class DeleteRequested(Message):
        def __init__(self, group_id: str) -> None:
            super().__init__()
            self.group_id = group_id

    def __init__(
        self,
        groups: list[Group],
        counts: dict[str, int],
        current_group_id: str,
    ) -> None:
        super().__init__()
        self._groups = groups_for_display(groups)
        self._counts = counts
        self._current_group_id = current_group_id
        self._editing = False

    def compose(self) -> ComposeResult:
        with Vertical(id="chooser"):
            yield Static("New conversation in…", classes="picker__title")
            yield self._body()
            yield Static(self.IDLE_HINT, classes="picker__hint", id=self.HINT_ID)

    def _body(self) -> ListView:
        # Rebuilt rather than reused after a delete, so the "+ New group…" row
        # is a fresh widget each time and never a removed one re-mounted.
        self._new_group_row = ListItem(
            Static(_NEW_GROUP_LABEL, classes="picker__item"),
            id=_NEW_GROUP_ID,
            classes="-spaced",
        )
        return ListView(
            *(self._row_for(group) for group in self._groups),
            self._new_group_row,
            id="chooser-body",
        )

    def _row_for(self, group: Group) -> ListItem:
        item = ListItem(
            Horizontal(
                Static(group.name, classes="chooser__name", markup=False),
                Static(
                    "current" if group.id == self._current_group_id else "",
                    classes="chooser__current",
                ),
                Static(self._chats(group), classes="chooser__count"),
                classes="chooser__row",
            ),
            id=f"group-{group.id}",
        )
        item.group_id = group.id
        return item

    def _chats(self, group: Group) -> str:
        count = self._counts.get(group.id, 0)
        return "1 chat" if count == 1 else f"{count} chats"

    def on_mount(self) -> None:
        # Inheritance is the baseline everywhere, so the list opens on the
        # group you are already in and you arrow away from it.
        list_view = self.query_one(ListView)
        current = next(
            (i for i, g in enumerate(self._groups) if g.id == self._current_group_id),
            0,
        )
        list_view.index = current
        list_view.focus()

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.item.id == _NEW_GROUP_ID:
            await self._begin_edit()
            return
        self.dismiss(("existing", event.item.group_id))

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        name = event.value.strip()
        if not name:
            # Two Enters in a row is the natural "I changed my mind", and it
            # must not create a group called "".
            await self._end_edit()
            return
        if any(g.name.casefold() == name.casefold() for g in self._groups):
            # Rejected names stay in edit mode with the text intact, so fixing
            # one does not mean retyping it.
            self._set_hint(f'A group called "{name}" already exists', alert=True)
            return
        self.dismiss(("create", name))

    async def on_key(self, event: events.Key) -> None:
        """The `Input` mounted into a `ListItem` binds Enter but not the arrow
        keys or Escape, which would otherwise move the list cursor off the row
        being edited, or close the whole chooser."""
        if self._pending_delete_id is not None:
            confirmed = self._answer_pending_delete(event)
            if confirmed is not None:
                self.post_message(self.DeleteRequested(confirmed))
            return
        if not self._editing or event.key not in ("up", "down", "escape"):
            return
        event.stop()
        event.prevent_default()
        if event.key == "escape":
            await self._end_edit()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_delete_highlighted(self) -> None:
        if self._editing:
            return
        item = self.query_one(ListView).highlighted_child
        # "+ New group…" has no group behind it.
        group_id = None if item is None else getattr(item, "group_id", None)
        group = next((g for g in self._groups if g.id == group_id), None)
        if group is None:
            return
        if not group.is_project():
            # The group every unfiled chat lands in always exists.
            self._set_hint(f'"{group.name}" cannot be deleted', alert=True)
            return
        self._ask_delete(
            group.id, f'Delete "{group.name}" and its {self._chats(group)}?'
        )

    async def refresh_groups(
        self, groups: list[Group], counts: dict[str, int], current_group_id: str
    ) -> None:
        """Swap in a new list without closing the screen, keeping the highlight
        where it was so the row after a deleted one is selected."""
        index = self.query_one(ListView).index or 0
        self._groups = groups_for_display(groups)
        self._counts = counts
        self._current_group_id = current_group_id

        await self.query_one("#chooser-body").remove()
        await self.query_one("#chooser", Vertical).mount(
            self._body(), before="#chooser-hint"
        )

        list_view = self.query_one(ListView)
        list_view.index = min(index, len(list_view.children) - 1)
        list_view.focus()

    async def _begin_edit(self) -> None:
        await self._new_group_row.query(Static).remove()
        await self._new_group_row.mount(Input(placeholder="Group name…", id="group-name"))
        self._editing = True
        self._set_hint(_EDITING_HINT)
        self.query_one("#group-name", Input).focus()

    async def _end_edit(self) -> None:
        await self._new_group_row.query(Input).remove()
        await self._new_group_row.mount(
            Static(_NEW_GROUP_LABEL, classes="picker__item")
        )
        self._editing = False
        self._set_hint(self.IDLE_HINT)
        # ListItem is can_focus=False, so focus has to be handed back to the
        # list explicitly or the chooser goes keyboard-dead with no visible
        # cause.
        self.query_one(ListView).focus()
