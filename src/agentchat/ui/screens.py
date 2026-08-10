"""Modal screens. The conversation picker takes an already-loaded list and
does no I/O itself — the app awaits store work and interprets the result."""

from __future__ import annotations

from typing import Literal

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import ListItem, ListView, Static

from agentchat.core.models import Conversation

#: ("switch", conversation id) or ("new", None).
PickerResult = tuple[Literal["switch", "new"], str | None]

_NEW_CONVERSATION_ID = "__new__"
_HINT = "enter switch · ctrl+x delete · esc cancel"


class ConversationPicker(ModalScreen[PickerResult | None]):
    """Overview of saved conversations; Enter switches, Escape cancels.

    Delete asks inline, in the hint line, rather than opening a second modal:
    Ctrl+X on a row replaces the hint with a "delete this?" prompt that only
    "y" confirms — any other key backs out, so a stray keypress can't delete.

    Deleting does not dismiss the screen: the picker posts `DeleteRequested`,
    the app does the store work and hands back the new list via
    `refresh_conversations`, so the user stays in the list they were browsing.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+x", "delete_highlighted", "Delete"),
    ]

    class DeleteRequested(Message):
        def __init__(self, conversation_id: str) -> None:
            super().__init__()
            self.conversation_id = conversation_id

    def __init__(self, conversations: list[Conversation], current_id: str | None) -> None:
        super().__init__()
        self._conversations = conversations
        self._current_id = current_id
        self._confirm_delete_id: str | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="picker"):
            yield Static("Conversations", classes="picker__title")
            yield self._body()
            yield Static(_HINT, classes="picker__hint", id="picker-hint")

    def _body(self) -> Widget:
        if not self._conversations:
            return Static(
                "No saved conversations yet.", classes="picker__empty", id="picker-body"
            )
        items = [self._item_for(c) for c in self._conversations]
        items.append(
            ListItem(
                Static("+ New conversation", classes="picker__item"),
                id=_NEW_CONVERSATION_ID,
            )
        )
        return ListView(*items, id="picker-body")

    def _item_for(self, conversation: Conversation) -> ListItem:
        classes = "picker__item"
        if conversation.id == self._current_id:
            classes += " -current"
        item = ListItem(
            Static(conversation.title, classes=classes),
            Static(
                f"{len(conversation.messages)} turns · "
                f"{conversation.updated_at:%b %d %H:%M}",
                classes="picker__meta",
            ),
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
        self.dismiss(("switch", event.item.conversation_id))

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_delete_highlighted(self) -> None:
        list_views = self.query(ListView)
        if not list_views:
            return
        item = list_views.first().highlighted_child
        if item is None or item.id == _NEW_CONVERSATION_ID:
            return
        conversation = next(
            (c for c in self._conversations if c.id == item.conversation_id), None
        )
        title = conversation.title if conversation is not None else "this conversation"
        self._confirm_delete_id = item.conversation_id
        self._set_hint(
            f'Delete "{title}"? y confirms — any other key cancels', confirming=True
        )

    def on_key(self, event: events.Key) -> None:
        """While a delete is pending, this screen's own bindings (Escape,
        Ctrl+X) must not fire — stopping the event here keeps it from
        reaching the app's binding resolution, so only "y" can confirm."""
        if self._confirm_delete_id is None:
            return
        event.stop()
        event.prevent_default()
        confirm_id = self._confirm_delete_id
        self._confirm_delete_id = None
        self._set_hint(_HINT)
        if event.key == "y":
            self.post_message(self.DeleteRequested(confirm_id))

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
        list_view.index = min(highlighted, len(list_view.children) - 1)
        list_view.focus()

    def _highlighted_index(self) -> int:
        list_views = self.query(ListView)
        if not list_views:
            return 0
        return list_views.first().index or 0

    def _set_hint(self, text: str, *, confirming: bool = False) -> None:
        hint = self.query_one("#picker-hint", Static)
        hint.set_class(confirming, "-confirming")
        hint.update(text)
