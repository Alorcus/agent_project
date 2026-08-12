"""Chat widgets.

``MessageBubble`` owns its own text buffer and appends in place — appending to
one widget rather than re-rendering the log keeps cost flat as a conversation
grows.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Static

from agentchat.core.models import Message

_ROLE_LABEL = {"user": "You", "assistant": "Assistant", "system": "System"}

_SEPARATOR = "  ›  "


class ConversationHeader(Horizontal):
    """The line above the chat: `group › title`, or the title alone.

    The default group prints as no group at all — it is the absence of one as
    far as a user is concerned, and prefixing every unfiled chat with it would
    make the common case look like the special one.

    Two children rather than one styled line: `$text-muted` is `auto 60%`,
    which only the CSS engine can resolve against a background, so the two
    halves have to be separate widgets to be coloured differently. It buys the
    widths as well — the group sizes to its content and the title takes the
    rest, so the title is what an overflow ellipsises.
    """

    def compose(self) -> ComposeResult:
        # markup=False on both: group names and titles alike are user text,
        # so a chat opening with "[bold]" is text and not a tag — the same
        # trap MessageBubble avoids.
        yield Static("", id="header-group", markup=False)
        yield Static("", id="header-title", markup=False)

    def show(self, title: str, group_name: str | None = None) -> None:
        group = self.query_one("#header-group", Static)
        group.display = group_name is not None
        if group_name is not None:
            group.update(f"{group_name}{_SEPARATOR}")
        self.query_one("#header-title", Static).update(title)


class MessageBubble(Vertical):
    """One turn: a role/model header plus a growing body."""

    def __init__(self, message: Message, model_name: str | None = None) -> None:
        super().__init__(classes=f"bubble bubble--{message.role}")
        self.message = message
        self._model_name = model_name
        self._buffer = message.content
        self._status: str | None = None
        # Built eagerly and held by reference: streaming updates can arrive
        # before compose() finishes. markup=False so model output containing
        # brackets is never parsed as Textual markup.
        self._header = Static(self._header_text(), classes="bubble__header")
        self._body = Static(self._buffer, classes="bubble__body", markup=False)

    def compose(self) -> ComposeResult:
        yield self._header
        yield self._body

    # -- streaming --------------------------------------------------------

    @property
    def text(self) -> str:
        return self._buffer

    def bind_message(self, message: Message) -> None:
        """Attach the real conversation message once the service has created
        it, so the widget and the stored history are the same object."""
        message.content = self._buffer
        self.message = message
        self._refresh_header()

    def append(self, chunk: str) -> None:
        self._buffer += chunk
        self.message.content = self._buffer
        self._body.update(self._buffer)

    def set_text(self, text: str) -> None:
        self._buffer = text
        self._body.update(text)

    def mark_stopped(self) -> None:
        self._status = "stopped"
        self.add_class("bubble--stopped")
        if not self._buffer.strip():
            self.set_text("[stopped before any output]")
        self._refresh_header()

    def mark_error(self, detail: str) -> None:
        self._status = "error"
        self.add_class("bubble--error")
        self.set_text(detail)
        self._refresh_header()

    def mark_done(self) -> None:
        self._status = None
        self._refresh_header()

    # -- internals --------------------------------------------------------

    def _refresh_header(self) -> None:
        self._header.update(self._header_text())

    def _header_text(self) -> str:
        label = _ROLE_LABEL.get(self.message.role, self.message.role)
        parts = [label]
        if self.message.role == "assistant" and self._model_name:
            parts.append(self._model_name)
        if self._status:
            parts.append(self._status)
        return " · ".join(parts)
