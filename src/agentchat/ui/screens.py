"""Modal screens. The conversation picker takes an already-loaded list and
does no I/O itself — the app awaits store work and interprets the result."""

from __future__ import annotations

from typing import Literal

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import ListItem, ListView, Static

from agentchat.core.models import Conversation

#: ("switch"/"delete", conversation id) or ("new", None).
PickerResult = tuple[Literal["switch", "new", "delete"], str | None]

_NEW_CONVERSATION_ID = "__new__"


class ConversationPicker(ModalScreen[PickerResult | None]):
    """Overview of saved conversations; Enter switches, Escape cancels."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, conversations: list[Conversation], current_id: str | None) -> None:
        super().__init__()
        self._conversations = conversations
        self._current_id = current_id

    def compose(self) -> ComposeResult:
        with Vertical(id="picker"):
            yield Static("Conversations", classes="picker__title")
            if not self._conversations:
                yield Static("No saved conversations yet.", classes="picker__empty")
            else:
                items = [self._item_for(c) for c in self._conversations]
                items.append(
                    ListItem(
                        Static("+ New conversation", classes="picker__item"),
                        id=_NEW_CONVERSATION_ID,
                    )
                )
                yield ListView(*items)
            yield Static(
                "enter switch · ctrl+x delete · esc cancel", classes="picker__hint"
            )

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
