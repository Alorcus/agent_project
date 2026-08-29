"""Chat widgets.

``MessageBubble`` owns its own text buffer and appends in place — appending to
one widget rather than re-rendering the log keeps cost flat as a conversation
grows.
"""

from __future__ import annotations

import textwrap
from collections.abc import Sequence
from typing import Any

from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Static

from agentchat.core.delegation import Consultation
from agentchat.core.models import ConversationSummary, Message
from agentchat.core.usage import CallUsage

_ROLE_LABEL = {"user": "You", "assistant": "Assistant", "system": "System"}

_SEPARATOR = "  ›  "

_DETAIL_INDENT = "    "


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


#: Bar width in cells. Fixed rather than proportional: the status line's left
#: half is variable-length text, and a bar that resized with it would make the
#: same figure look different from one turn to the next.
_METER_CELLS = 14
#: Room for the widest figure the meter prints — `~999.9k/999.9k`.
_FIGURE_CELLS = 14
_METER_FULL = "█"
_METER_EMPTY = "░"
#: Left-aligned partial blocks, so the bar's leading edge moves in eighths of
#: a cell instead of jumping a whole one.
_METER_PARTIALS = "▏▎▍▌▋▊▉"

#: Fractions at which the meter stops being muted. Set above where a trimmed
#: prompt alone lands — the context strategy holds room back for the response,
#: so filling its budget exactly is normal and not worth a colour. Amber is
#: "this turn nearly filled the window", red is "it reached it", which a reply
#: generated on top of an already-trimmed prompt can now do: the strategy's
#: reserve is not the same number as the generation's own token limit.
_METER_TIGHT = 0.8
_METER_FULL_AT = 0.95


class ContextMeter(Static):
    """A one-line gauge of how full the model's context window got on the last
    turn — the tokens it had to hold at once, not how long the conversation is.

    Measures the turn's largest call, prompt and completion together, since
    that is what the cache had to fit: on a consulted turn the specialist's
    call can be the larger one, and a reply's own generation goes on top of
    the prompt that produced it.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("", markup=False, **kwargs)

    def show(self, usage: CallUsage | None, context_window: int) -> None:
        """Render `usage`, or an empty bar against `context_window` when no
        turn has been sent yet. A window of zero (no model registered) hides
        the meter — an empty gauge over an empty scale says nothing."""
        window = usage.context_window if usage is not None else context_window
        self.display = window > 0
        if not self.display:
            return

        tokens = usage.tokens if usage is not None else 0
        fraction = usage.fraction if usage is not None else 0.0
        prefix = "~" if usage is not None and usage.estimated else ""
        # Padded, not just written: the widget is `width: auto`, so a figure
        # that grows a digit would otherwise slide the bar a column left and
        # make it look like it moved when only the number did.
        figure = f"{prefix}{_short(tokens)}/{_short(window)}"
        self.update(f"ctx {_bar(fraction, _METER_CELLS)} {figure:<{_FIGURE_CELLS}}")
        self.set_class(_METER_TIGHT <= fraction < _METER_FULL_AT, "-tight")
        self.set_class(fraction >= _METER_FULL_AT, "-full")
        self.tooltip = _meter_tooltip(usage, window)


def _meter_tooltip(usage: CallUsage | None, window: int) -> str:
    if usage is None:
        return f"Nothing sent yet. Context window: {window} tokens."
    counted = "estimated" if usage.estimated else "counted by the tokenizer"
    return (
        f"Largest call of the last turn: {usage.prompt_tokens} prompt + "
        f"{usage.completion_tokens} generated = {usage.tokens} of {window} "
        f"tokens ({usage.fraction:.0%}), from the {usage.label} call, {counted}."
    )


def _bar(fraction: float, width: int) -> str:
    filled = max(0.0, min(1.0, fraction)) * width
    cells = [_METER_FULL] * int(filled)
    remainder = filled - int(filled)
    if len(cells) < width and remainder > 0:
        cells.append(_METER_PARTIALS[int(remainder * len(_METER_PARTIALS))])
    return "".join(cells) + _METER_EMPTY * (width - len(cells))


def _short(tokens: int) -> str:
    """Token counts read as magnitudes here, not exact figures — the exact one
    is in the tooltip."""
    if tokens < 1000:
        return str(tokens)
    return f"{tokens / 1000:.1f}k"


class CollapsibleNote(Static):
    """A muted line under a bubble that expands on click. Subclasses supply
    the collapsed header text and the indented lines shown under it once
    expanded — this base owns only the toggle and the marker."""

    def __init__(self) -> None:
        self._collapsed = True
        self._wrapped_width = 0
        # markup=False: the detail lines are model output and may contain
        # brackets — the same trap MessageBubble's own body avoids.
        super().__init__(self._text(), markup=False, classes="bubble__memo")

    def on_click(self, event: events.Click) -> None:
        # Its own widget, not a handler on the bubble, so a click anywhere
        # else in the bubble does nothing.
        self._collapsed = not self._collapsed
        self.update(self._text())
        event.stop()

    def on_resize(self, event: events.Resize) -> None:
        # The detail lines are wrapped against a measured width, so a width
        # change has to re-wrap them. Comparing widths keeps the update from
        # bouncing off its own relayout.
        if not self._collapsed and self.content_size.width != self._wrapped_width:
            self.update(self._text())

    def _text(self) -> str:
        marker = "▸" if self._collapsed else "▾"
        header = f"{marker} {self._header_text()}"
        if self._collapsed:
            return header
        return "\n".join([header, *self._wrapped_detail_lines()])

    def _wrapped_detail_lines(self) -> Sequence[str]:
        """The detail lines indented, and wrapped here rather than by the
        widget: a wrap `Static` performs restarts the continuation at column
        zero, which reads as a line that lost its indent."""
        width = self.content_size.width
        self._wrapped_width = width
        lines: list[str] = []
        for detail in self._detail_lines():
            text = " ".join(detail.split())
            if width <= len(_DETAIL_INDENT) + 1:
                # Unmeasured (built before the first layout) or too narrow to
                # wrap into; let Static do what it can with the whole line.
                lines.append(_DETAIL_INDENT + text)
                continue
            lines.extend(
                textwrap.wrap(
                    text,
                    width=width,
                    initial_indent=_DETAIL_INDENT,
                    subsequent_indent=_DETAIL_INDENT,
                )
                or [_DETAIL_INDENT + text]
            )
        return lines

    def _header_text(self) -> str:
        raise NotImplementedError

    def _detail_lines(self) -> Sequence[str]:
        """One unindented, unwrapped line per detail; the base indents and
        wraps them."""
        raise NotImplementedError


class EnrichmentNote(CollapsibleNote):
    """One line under a user turn: how many memories were appended, and —
    on click — which."""

    def __init__(self, summaries: Sequence[ConversationSummary]) -> None:
        self._summaries = tuple(summaries)
        super().__init__()

    def _header_text(self) -> str:
        count = len(self._summaries)
        noun = "memory" if count == 1 else "memories"
        return f"enriched by {count} {noun}"

    def _detail_lines(self) -> Sequence[str]:
        return [summary.summary for summary in self._summaries]


class ConsultationNote(CollapsibleNote):
    """One line under a reply written with a specialist's help: which one,
    and — on click — the task it was given and what it answered."""

    def __init__(self, agent_name: str, task: str, answer: str) -> None:
        self._agent_name = agent_name
        # Not `self._task`: `MessagePump.__init__` (a Textual base class)
        # owns that name for its own running task and overwrites it the
        # moment this widget is mounted.
        self._agent_task = task
        self._agent_answer = answer
        super().__init__()

    def _header_text(self) -> str:
        return f"answered with help from {self._agent_name}"

    def _detail_lines(self) -> Sequence[str]:
        return [f"Task: {self._agent_task}", f"Answer: {self._agent_answer}"]


def _consultation_note_from(metadata: dict[str, Any]) -> ConsultationNote | None:
    """Rebuild the note from a persisted `Message.metadata["subagent"]` block
    — used to restore a conversation, where no `Consultation` object exists.
    `None` when the message carries no such block."""
    block = metadata.get("subagent")
    if not block:
        return None
    return ConsultationNote(block["name"], block["task"], block["answer"])


class MessageBubble(Vertical):
    """One turn: a role/model header plus a growing body."""

    def __init__(self, message: Message, model_name: str | None = None) -> None:
        super().__init__(classes=f"bubble bubble--{message.role}")
        self.message = message
        self._model_name = model_name
        self._buffer = message.content
        self._status: str | None = None
        self._note: EnrichmentNote | None = None
        self._consultation_note: ConsultationNote | None = None
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

    def show_enrichment(self, summaries: Sequence[ConversationSummary]) -> None:
        """Mount the note under the body, once. Empty input and a second call
        are both no-ops."""
        if self._note is not None or not summaries:
            return
        self._note = EnrichmentNote(summaries)
        self.mount(self._note)

    def show_consultation(self, consultation: Consultation | None) -> None:
        """Mount the note between the header and the body, once. `None` and a
        second call are both no-ops. No header change: the turn *was* written
        by the assistant on the active model, so the note carries the
        attribution rather than the header."""
        if self._consultation_note is not None or consultation is None:
            return
        self._consultation_note = ConsultationNote(
            consultation.agent.name, consultation.task, consultation.answer
        )
        self._mount_consultation_note(self._consultation_note)

    def show_consultation_metadata(self, metadata: dict[str, Any]) -> None:
        """The restart path for `show_consultation`: rebuilds the note from
        persisted `Message.metadata` rather than a live `Consultation`, since
        no `SubAgent` is reconstructed from storage."""
        if self._consultation_note is not None:
            return
        note = _consultation_note_from(metadata)
        if note is None:
            return
        self._consultation_note = note
        self._mount_consultation_note(note)

    # -- internals --------------------------------------------------------

    def _mount_consultation_note(self, note: ConsultationNote) -> None:
        # Above the body: the attribution belongs with the header that names
        # who is speaking, not trailing the answer it qualifies.
        self.mount(note, after=self._header)

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
