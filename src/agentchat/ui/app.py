"""The Textual application.

Generation always runs on a worker, never on the event loop's critical path —
scrolling, switching models and stopping all stay live while tokens arrive.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Header, Input, Static

from agentchat.config import Settings, build_registry, build_store
from agentchat.core.chat import ChatService
from agentchat.core.errors import AgentChatError, ModelNotFoundError
from agentchat.core.models import Conversation, Message
from agentchat.llm.base import GenerationOptions
from agentchat.ui.screens import ConversationPicker
from agentchat.ui.widgets import MessageBubble

_GENERATION_GROUP = "generation"


class ChatApp(App[None]):
    CSS_PATH = Path(__file__).with_name("app.tcss")
    TITLE = "agentchat"

    BINDINGS = [
        Binding("ctrl+d", "quit", "Exit"),
        Binding("escape", "stop", "Stop"),
        Binding("ctrl+n", "new_conversation", "New chat"),
        Binding("ctrl+l", "open_conversations", "Chats"),
        Binding("ctrl+o", "cycle_model", "Model"),
        Binding("ctrl+t", "toggle_thinking", "Thinking"),
    ]

    def __init__(self, settings: Settings | None = None) -> None:
        super().__init__()
        self.settings = settings or Settings.from_env()
        self.registry = build_registry(self.settings)
        self.chat = ChatService(self.registry, store=build_store(self.settings))
        self.conversation: Conversation = Conversation()
        self.options = GenerationOptions()
        self.status_text = ""
        self._generating = False

    # -- composition ------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield VerticalScroll(
            Static(self._placeholder_text(), classes="placeholder"),
            id="chat-log",
        )
        yield Static("", id="statusbar")
        yield Container(
            Input(placeholder="Message…", id="prompt"),
            id="composer",
        )
        yield Footer()

    async def on_mount(self) -> None:
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

    async def action_new_conversation(self) -> None:
        self.action_stop()
        await self.chat.persist(self.conversation)
        self.conversation = await self.chat.new_conversation()
        await self._show_conversation(self.conversation)
        self._refresh_status()
        self._chat_screen.query_one("#prompt", Input).focus()

    @work
    async def action_open_conversations(self) -> None:
        # Bare @work (not the generation group, not exclusive): opening the
        # picker must never cancel a running generation.
        conversations = await self.chat.list_conversations()
        result = await self.push_screen_wait(
            ConversationPicker(conversations, self.conversation.id)
        )
        if result is None:
            return
        action, conversation_id = result
        if action == "switch":
            await self._switch_to(conversation_id)
        elif action == "new":
            await self.action_new_conversation()

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
            remaining = await self.chat.list_conversations()
            if remaining:
                await self._switch_to(remaining[0].id)
            else:
                await self.action_new_conversation()
        # Escape during the store work leaves nothing to refresh.
        if self.screen is picker:
            await picker.refresh_conversations(
                await self.chat.list_conversations(), self.conversation.id
            )

    async def _switch_to(self, conversation_id: str) -> None:
        # A live generation holds bubbles that _show_conversation is about to
        # remove, and its finally block writes to them after cancellation —
        # so generation must be stopped before the log is torn down.
        self.action_stop()
        try:
            await self.chat.persist(self.conversation)
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
            await log.mount(
                MessageBubble(message, model_name=self._model_name_for(message))
            )
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

    # -- generation -------------------------------------------------------

    @work(exclusive=True, group=_GENERATION_GROUP)
    async def _turn(self, text: str) -> None:
        log = self._chat_screen.query_one("#chat-log", VerticalScroll)
        await log.query(".placeholder").remove()

        await log.mount(MessageBubble(Message(role="user", content=text)))
        log.scroll_end(animate=False)

        model_name = info.name if (info := self.registry.active_info) else None
        bubble = MessageBubble(Message(role="assistant"), model_name=model_name)
        await log.mount(bubble)
        log.scroll_end(animate=False)

        self._generating = True
        self._refresh_status(busy="loading model…")

        stream = self.chat.stream_reply(self.conversation, text, self.options)
        first = True
        try:
            async for chunk in stream:
                if first:
                    first = False
                    if self.chat.last_turn is not None:
                        bubble.bind_message(self.chat.last_turn.message)
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

        self.status_text = ("  ·  ".join(bits)) + (f"  ·  {busy}" if busy else "")
        bar = self._chat_screen.query_one("#statusbar", Static)
        bar.set_class(busy is not None, "-busy")
        bar.update(self.status_text)
