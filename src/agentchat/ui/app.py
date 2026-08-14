"""The Textual application.

Generation always runs on a worker, never on the event loop's critical path —
scrolling, switching models and stopping all stay live while tokens arrive.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Input, Static

from agentchat.config import (
    Settings,
    build_delegator,
    build_enricher,
    build_extractor,
    build_registry,
    build_store,
)
from agentchat.core.chat import ChatService
from agentchat.core.errors import AgentChatError, ModelNotFoundError
from agentchat.core.models import Conversation, Group, Message
from agentchat.llm.base import GenerationOptions
from agentchat.ui.screens import ConversationPicker, GroupChooser
from agentchat.ui.widgets import ContextMeter, ConversationHeader, MessageBubble

_GENERATION_GROUP = "generation"
_EXTRACTION_GROUP = "extraction"
_SUMMARISING_STATUS = "summarising…"


class ChatApp(App[None]):
    CSS_PATH = Path(__file__).with_name("app.tcss")
    TITLE = "agentchat"

    BINDINGS = [
        # priority=True: `Input.BINDINGS` binds `ctrl+d` to `delete_right`,
        # which would otherwise shadow this while the prompt has focus (the
        # common case). The cost is `delete_right` itself — `Delete` still
        # does it.
        Binding("ctrl+d", "quit", "Exit", priority=True),
        Binding("escape", "stop", "Stop"),
        Binding("ctrl+n", "new_conversation", "New chat"),
        Binding("ctrl+g", "choose_group", "New chat in…"),
        Binding("ctrl+l", "open_conversations", "Chats"),
        Binding("ctrl+o", "cycle_model", "Model"),
        Binding("ctrl+t", "toggle_thinking", "Thinking"),
    ]

    def __init__(self, settings: Settings | None = None) -> None:
        super().__init__()
        self.settings = settings or Settings.from_env()
        self.registry = build_registry(self.settings)
        store = build_store(self.settings)
        self.chat = ChatService(
            self.registry,
            store=store,
            extractor=build_extractor(self.settings, self.registry),
            enricher=build_enricher(self.settings, store),
            delegator=build_delegator(self.settings, self.registry),
        )
        self.conversation: Conversation = Conversation()
        self.options = GenerationOptions()
        self.status_text = ""
        self._generating = False
        # A count, not a bool: two fast switches can leave one background
        # extraction winding down (cancelled) while its replacement is still
        # running, and a bool would let the first one's `finally` clear the
        # indicator out from under the second.
        self._summarising_count = 0
        # The header renders synchronously and holds only a group id, so it
        # cannot await the store per repaint.
        self._groups: dict[str, Group] = {}

    # -- composition ------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield ConversationHeader(id="conversation-header")
        yield VerticalScroll(
            Static(self._placeholder_text(), classes="placeholder"),
            id="chat-log",
        )
        # Two widgets on one row rather than one line of text: the meter is
        # coloured by how full it is, which it can only be as a widget of its
        # own, and right-aligning it keeps the bar still while the status text
        # on the left changes length.
        yield Horizontal(
            Static("", id="statusbar"),
            ContextMeter(id="context-meter"),
            id="statusrow",
        )
        yield Container(
            Input(placeholder="Message…", id="prompt"),
            id="composer",
        )
        yield Footer()

    async def on_mount(self) -> None:
        await self._refresh_groups()
        # The first conversation of a session has no current group to inherit
        # and nothing has been typed yet, so it lands in the default group
        # rather than opening the chooser. Ctrl+G is right there.
        self.conversation = await self.chat.new_conversation()
        self._chat_screen.query_one("#prompt", Input).focus()
        self._refresh_status()

    @property
    def _chat_screen(self) -> Screen:
        """The chat itself, which is the bottom of the screen stack. Queries
        have to start here rather than at the app: with the picker open,
        `App.query` only sees the modal on top."""
        return self.screen_stack[0]

    def _placeholder_text(self) -> str:
        info = self.registry.active_info
        model = info.name if info else "no model"
        if self.settings.backend == "mock":
            return f"Mocked backend ({model}) — replies are stubs. Type below and press Enter."
        return (
            f"{model} — the first message loads the weights, which takes a moment. "
            "Type below and press Enter."
        )

    # -- events -----------------------------------------------------------

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        if self._generating:
            self.notify("Still generating — press Escape to stop.", severity="warning")
            return
        event.input.value = ""
        self._turn(text)

    # -- actions ----------------------------------------------------------

    def action_stop(self) -> None:
        if not self._generating:
            return
        self.workers.cancel_group(self, _GENERATION_GROUP)

    async def action_quit(self) -> None:
        """The only awaitable pre-exit hook (KTD11): `App.exit()` is
        synchronous, so overriding this coroutine is the one place that can
        await the closing summary — and it covers every route out of the
        app, the app's own Ctrl+D binding and textual's default Ctrl+Q."""
        self.workers.cancel_group(self, _GENERATION_GROUP)
        await self._summarise_before_exit()
        await super().action_quit()

    async def _summarise_before_exit(self) -> None:
        if self.chat.extractor is None or not self.conversation.messages:
            return
        # A background run from an earlier switch may still hold the
        # provider lock this call needs.
        self.workers.cancel_group(self, _EXTRACTION_GROUP)
        self._refresh_status(busy=_SUMMARISING_STATUS)
        # Actions run as tasks off the message pump; without yielding here,
        # the status line's repaint can lose the race against `wait_for`
        # below and never actually be seen.
        await asyncio.sleep(0)
        try:
            await asyncio.wait_for(
                self.chat.summarise(self.conversation),
                timeout=self.settings.extraction_timeout,
            )
        except TimeoutError:
            # The app is a line of code from gone; a toast nobody can read is
            # worse than silence. The watermark makes the next exit from this
            # conversation (if there is one) retry it.
            pass
        except AgentChatError:
            pass

    async def action_new_conversation(self) -> None:
        """Instant, and inherits the current conversation's group — the group
        you are in is the one you were last thinking about. The header states
        which it is, which is what keeps inheritance from surprising anyone."""
        await self._start_conversation(self.conversation.group_id)

    @work
    async def action_choose_group(self) -> None:
        # Bare @work, like the picker: choosing a group must never cancel a
        # running generation.
        groups = await self._refresh_groups()
        choice = await self.push_screen_wait(
            GroupChooser(groups, await self._group_counts(), self.conversation.group_id)
        )
        if choice is None:
            return
        kind, chosen = choice
        if kind == "existing":
            await self._start_conversation(chosen)
            return
        # The chooser does no I/O, so creating the group happens here and a
        # StorageError surfaces through notify rather than inside a modal with
        # no way to report it.
        try:
            group = await self.chat.create_group(chosen)
        except AgentChatError as error:
            self.notify(str(error), severity="error")
            return
        await self._refresh_groups()
        await self._start_conversation(group.id)

    async def _start_conversation(self, group_id: str) -> None:
        self.action_stop()
        await self.chat.persist(self.conversation)
        self._maybe_summarise(self.conversation)
        self.conversation = await self.chat.new_conversation(group_id)
        await self._show_conversation(self.conversation)
        self._refresh_status()
        self._chat_screen.query_one("#prompt", Input).focus()

    async def _refresh_groups(self) -> list[Group]:
        groups = await self.chat.list_groups()
        self._groups = {group.id: group for group in groups}
        return groups

    async def _group_counts(self) -> Counter[str]:
        """Conversations per group — the chooser's blast-radius figure, and
        the only count it shows."""
        return Counter(c.group_id for c in await self.chat.list_all_conversations())

    async def on_group_chooser_delete_requested(
        self, event: GroupChooser.DeleteRequested
    ) -> None:
        """The chooser already confirmed inline, naming what goes with the
        group; it stays open and gets the remaining groups back."""
        chooser = self.screen
        if not isinstance(chooser, GroupChooser):
            return
        try:
            await self.chat.delete_group(event.group_id)
        except AgentChatError as error:
            self.notify(str(error), severity="error")
            return
        groups = await self._refresh_groups()
        if self.conversation.group_id == event.group_id:
            # The conversation we were in went with its group, and there is
            # nowhere to re-home it to — membership is for life. A fresh,
            # unsaved one in the default group is what is left; persist skips
            # it while it has no messages, so nothing is resurrected.
            self.action_stop()
            self.conversation = Conversation()
            await self._show_conversation(self.conversation)
            self._refresh_status()
        # Escape during the store work leaves nothing to refresh.
        if self.screen is chooser:
            await chooser.refresh_groups(
                groups, await self._group_counts(), self.conversation.group_id
            )

    @work
    async def action_open_conversations(self) -> None:
        # Bare @work (not the generation group, not exclusive): opening the
        # picker must never cancel a running generation.
        conversations = await self.chat.list_all_conversations()
        groups = await self._refresh_groups()
        result = await self.push_screen_wait(
            ConversationPicker(conversations, self.conversation.id, groups)
        )
        if result is None:
            return
        action, conversation_id = result
        if action == "switch":
            await self._switch_to(conversation_id)
        elif action == "new":
            await self.action_new_conversation()
        elif action == "choose":
            # The picker is already gone, so the chooser is pushed after it
            # rather than on top of it.
            self.action_choose_group()

    async def on_conversation_picker_delete_requested(
        self, event: ConversationPicker.DeleteRequested
    ) -> None:
        """The picker already confirmed inline, so no second confirmation
        here; it stays open and gets the remaining conversations back."""
        picker = self.screen
        if not isinstance(picker, ConversationPicker):
            return
        try:
            await self.chat.delete_conversation(event.conversation_id)
        except AgentChatError as error:
            self.notify(str(error), severity="error")
            return
        if event.conversation_id == self.conversation.id:
            # The conversation we were looking at is gone; persisting it
            # (as _switch_to/action_new_conversation do) would resurrect it,
            # so swap in a fresh, unsaved one first — persist already skips
            # conversations with no messages.
            self.conversation = Conversation()
            remaining = await self.chat.list_all_conversations()
            if remaining:
                await self._switch_to(remaining[0].id)
            else:
                await self.action_new_conversation()
        # Escape during the store work leaves nothing to refresh.
        if self.screen is picker:
            await picker.refresh_conversations(
                await self.chat.list_all_conversations(), self.conversation.id
            )

    async def _switch_to(self, conversation_id: str) -> None:
        # A live generation holds bubbles that _show_conversation is about to
        # remove, and its finally block writes to them after cancellation —
        # so generation must be stopped before the log is torn down.
        self.action_stop()
        try:
            await self.chat.persist(self.conversation)
            self._maybe_summarise(self.conversation)
            self.conversation = await self.chat.switch_conversation(conversation_id)
        except AgentChatError as error:
            self.notify(str(error), severity="error")
            return
        await self._show_conversation(self.conversation)
        self._refresh_status()
        self._chat_screen.query_one("#prompt", Input).focus()

    async def _show_conversation(self, conversation: Conversation) -> None:
        log = self._chat_screen.query_one("#chat-log", VerticalScroll)
        await log.remove_children()
        if not conversation.messages:
            await log.mount(Static("New conversation.", classes="placeholder"))
            return
        for message in conversation.messages:
            bubble = MessageBubble(message, model_name=self._model_name_for(message))
            await log.mount(bubble)
            # The only way a consultation note survives a restart (R12,
            # KTD8): there is no live `Consultation` here, only what was
            # persisted.
            bubble.show_consultation_metadata(message.metadata)
        log.scroll_end(animate=False)

    def _model_name_for(self, message: Message) -> str | None:
        if message.role != "assistant":
            return None
        try:
            return self.registry.info(message.model_id).name
        except ModelNotFoundError:
            return message.model_id

    def action_cycle_model(self) -> None:
        """Takes effect on the next turn; a running generation keeps the
        model it started with."""
        info = self.registry.cycle()
        self.notify(f"Model → {info.name} ({info.context_window} tokens)")
        self._refresh_status()

    def action_toggle_thinking(self) -> None:
        self.options = GenerationOptions(
            temperature=self.options.temperature,
            max_tokens=self.options.max_tokens,
            thinking=not self.options.thinking,
            stop=self.options.stop,
        )
        self._refresh_status()

    # -- extraction ---------------------------------------------------------

    def _maybe_summarise(self, conversation: Conversation) -> None:
        """Start the background extraction worker, unless extraction is off
        — checked here, not inside the worker, so a disabled feature starts
        no worker at all rather than one that immediately no-ops.

        Incrementing the counter here, before the worker has even started,
        means the caller's own trailing `_refresh_status()` (after the switch
        completes) already shows the indicator — no race with the worker's
        first tick.
        """
        if self.chat.extractor is None:
            return
        self._summarising_count += 1
        self._summarise(conversation)

    @work(group=_EXTRACTION_GROUP, exclusive=True)
    async def _summarise(self, conversation: Conversation) -> None:
        """Summarise `conversation` in the background, once it is left. Not
        in `_GENERATION_GROUP`: sharing it would make switching cancel a
        running generation, the exact regression plan 001's U5 guarded
        against (R9). `exclusive=True` within its own group so two fast
        switches leave one extraction in flight, not two."""
        try:
            await self.chat.summarise(conversation)
        except asyncio.CancelledError:
            # Cancellation is the designed path (a new turn pre-empts a
            # stale extraction, or the app is shutting down, KTD7) — not a
            # failure, and not reported. The counter still needs correcting,
            # but nothing here may touch the screen: cancellation reaches
            # here during app shutdown too, once the screen stack is already
            # torn down.
            self._summarising_count -= 1
            raise
        except AgentChatError as error:
            # Warning, not error: a missing summary costs the user nothing
            # they asked for, and a red toast every time a model misbehaves
            # would be worse than the missing row (R11).
            self._summarising_count -= 1
            self._refresh_status()
            self.notify(str(error), severity="warning")
        else:
            self._summarising_count -= 1
            self._refresh_status()

    # -- generation -------------------------------------------------------

    @work(exclusive=True, group=_GENERATION_GROUP)
    async def _turn(self, text: str) -> None:
        # A user who types immediately after switching never waits on a
        # summary; the abandoned conversation is picked up next time it is
        # left, since the watermark makes that free (KTD7).
        self.workers.cancel_group(self, _EXTRACTION_GROUP)

        log = self._chat_screen.query_one("#chat-log", VerticalScroll)
        await log.query(".placeholder").remove()

        user_bubble = MessageBubble(Message(role="user", content=text))
        await log.mount(user_bubble)
        log.scroll_end(animate=False)

        model_name = info.name if (info := self.registry.active_info) else None
        bubble = MessageBubble(Message(role="assistant"), model_name=model_name)
        await log.mount(bubble)
        log.scroll_end(animate=False)

        self._generating = True
        self._refresh_status(busy="loading model…")

        stream = self.chat.stream_reply(
            self.conversation,
            text,
            self.options,
            on_progress=lambda phase: self._refresh_status(busy=phase),
        )
        first = True
        try:
            async for chunk in stream:
                if first:
                    first = False
                    if self.chat.last_turn is not None:
                        bubble.bind_message(self.chat.last_turn.message)
                        user_bubble.show_enrichment(self.chat.last_turn.enrichment)
                        bubble.show_consultation(self.chat.last_turn.consultation)
                    self._refresh_status(busy="generating…")
                bubble.append(chunk)
                log.scroll_end(animate=False)
            bubble.mark_done()
        except asyncio.CancelledError:
            bubble.mark_stopped()
            raise
        except AgentChatError as error:
            bubble.mark_error(str(error))
        finally:
            # A reply that errors or is stopped before its first chunk still
            # spent the memories (`stream_reply` marks them used before it
            # starts streaming) and, on a consulted turn, the specialist
            # still ran — show_enrichment/show_consultation are idempotent,
            # so repeating the calls here is what makes the notes appear
            # regardless.
            if self.chat.last_turn is not None:
                user_bubble.show_enrichment(self.chat.last_turn.enrichment)
                bubble.show_consultation(self.chat.last_turn.consultation)
            self._generating = False
            self.call_later(self._refresh_status)
            log.scroll_end(animate=False)

    # -- status -----------------------------------------------------------

    def _refresh_status(self, busy: str | None = None) -> None:
        info = self.registry.active_info
        bits = [f"model: {info.name if info else 'none'}"]
        bits.append(f"thinking: {'on' if self.options.thinking else 'off'}")
        bits.append(f"turns: {len(self.conversation.messages)}")

        turn = self.chat.last_turn
        if turn is not None and turn.context.was_trimmed:
            bits.append(f"context: dropped {len(turn.context.dropped)}")

        if busy is None and self._summarising_count > 0:
            # Worth a status line on its own, but a caller's explicit `busy`
            # (loading, generating, the quit-time wait) is foreground work
            # and takes priority when both are happening at once.
            busy = _SUMMARISING_STATUS

        self.status_text = ("  ·  ".join(bits)) + (f"  ·  {busy}" if busy else "")
        bar = self._chat_screen.query_one("#statusbar", Static)
        bar.set_class(busy is not None, "-busy")
        bar.update(self.status_text)
        self._refresh_meter()
        self._refresh_header()

    def _refresh_meter(self) -> None:
        """The meter tracks `last_turn.usage`, which is the live collector for
        a turn still streaming — so refreshing the status bar at each phase is
        all it takes for the bar to grow as the turn spends its context."""
        turn = self.chat.last_turn
        info = self.registry.active_info
        meter = self._chat_screen.query_one("#context-meter", ContextMeter)
        meter.show(
            turn.usage.peak if turn is not None else None,
            # Only reached before the first turn (or after a switch clears
            # `last_turn`): with a peak in hand the window comes from it, so
            # the figure and the scale always describe the same call even
            # after Ctrl+O.
            context_window=info.context_window if info else 0,
        )

    def _refresh_header(self) -> None:
        """Folded into `_refresh_status` so the two cannot drift: every point
        where the conversation changes already refreshes the status bar, and
        the header picks up the auto-derived title at the first streamed chunk
        for free."""
        group = self._groups.get(self.conversation.group_id)
        header = self._chat_screen.query_one("#conversation-header", ConversationHeader)
        header.show(
            self.conversation.title,
            group_name=group.name if group is not None and group.is_project() else None,
        )
