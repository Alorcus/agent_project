"""End-to-end tests through Textual's headless pilot.

These drive the real app the way a user would, which is the only way to catch
the failure this milestone is actually about: a UI that freezes while a reply
streams.
"""

from __future__ import annotations

import asyncio

from textual.widgets import Input, ListView, Static

from agentchat.config import Settings
from agentchat.core.errors import StorageError
from agentchat.core.models import Conversation, Message
from agentchat.ui.app import ChatApp
from agentchat.ui.screens import ConfirmModal, ConversationPicker
from agentchat.ui.widgets import MessageBubble


def mock_settings(**overrides) -> Settings:
    """The stub backend, explicitly. The app defaults to the real cluster
    models; these tests are about the interface, not about inference, and must
    run without a GPU."""
    overrides.setdefault("backend", "mock")
    # Without this every app test writes a real ./data/agentchat.db as a
    # side effect.
    overrides.setdefault("store", "memory")
    return Settings(**overrides)


async def _submit(pilot, text: str) -> None:
    prompt = pilot.app.query_one("#prompt", Input)
    prompt.value = text
    await pilot.press("enter")


async def _wait_until_done(pilot, app) -> None:
    for _ in range(200):
        await pilot.pause()
        if not app._generating:
            break
        await asyncio.sleep(0.05)


async def _wait_for_screen(pilot, app, screen_type) -> None:
    # action_open_conversations is a @work worker; it needs a tick to reach
    # push_screen_wait and mount the new screen.
    for _ in range(60):
        await pilot.pause()
        if isinstance(app.screen, screen_type):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"{screen_type} never became the active screen")


async def test_prompt_produces_a_streamed_reply():
    app = ChatApp(mock_settings())
    async with app.run_test() as pilot:
        await _submit(pilot, "hello")

        for _ in range(200):
            await pilot.pause()
            bubbles = app.query(MessageBubble)
            if len(bubbles) == 2 and not app._generating:
                break
            await asyncio.sleep(0.05)

        bubbles = list(app.query(MessageBubble))
        assert len(bubbles) == 2
        assert bubbles[0].message.role == "user"
        assert bubbles[1].message.role == "assistant"
        assert "hello" in bubbles[1].message.content
        assert bubbles[1].message.model_id == app.registry.active_id


async def test_ui_stays_responsive_while_generating():
    app = ChatApp(mock_settings())
    async with app.run_test() as pilot:
        await _submit(pilot, "a long enough prompt to stream")
        await asyncio.sleep(0.6)
        assert app._generating, "should still be mid-generation"

        # Interaction that must not be blocked by the running worker.
        before = app.registry.active_id
        await pilot.press("ctrl+o")
        await pilot.pause()
        assert app.registry.active_id != before
        assert app._generating


async def test_escape_stops_generation_and_keeps_partial_text():
    app = ChatApp(mock_settings())
    async with app.run_test() as pilot:
        await _submit(pilot, "stop me")
        await asyncio.sleep(0.7)
        await pilot.press("escape")

        for _ in range(60):
            await pilot.pause()
            if not app._generating:
                break
            await asyncio.sleep(0.05)

        assert not app._generating
        reply = list(app.query(MessageBubble))[-1]
        assert reply.has_class("bubble--stopped")


async def test_backend_failure_surfaces_as_an_error_bubble_not_a_crash():
    app = ChatApp(mock_settings(simulate_failure=True))
    async with app.run_test() as pilot:
        app.registry.cycle()  # switch to the failing model
        await _submit(pilot, "hi")

        for _ in range(80):
            await pilot.pause()
            if not app._generating:
                break
            await asyncio.sleep(0.05)

        reply = list(app.query(MessageBubble))[-1]
        assert reply.has_class("bubble--error")
        assert app.is_running


async def test_new_conversation_clears_the_log():
    app = ChatApp(mock_settings())
    async with app.run_test() as pilot:
        await _submit(pilot, "first")
        await asyncio.sleep(0.4)
        await pilot.press("ctrl+n")
        await pilot.pause()
        await asyncio.sleep(0.1)
        assert not list(app.query(MessageBubble))
        assert app.conversation.messages == []


async def test_status_bar_reports_the_active_model():
    app = ChatApp(mock_settings())
    async with app.run_test() as pilot:
        assert "Mock Small" in app.status_text
        assert app.query_one("#statusbar", Static) is not None
        await pilot.press("ctrl+o")
        await pilot.pause()
        assert "Mock Large" in app.status_text


async def test_thinking_toggle_is_user_controlled():
    app = ChatApp(mock_settings())
    async with app.run_test() as pilot:
        assert app.options.thinking is False
        await pilot.press("ctrl+t")
        await pilot.pause()
        assert app.options.thinking is True


async def test_mount_builds_a_fresh_unsaved_conversation():
    app = ChatApp(mock_settings())
    pre_mount_id = app.conversation.id
    async with app.run_test():
        assert app.conversation.id != pre_mount_id
        assert await app.chat.store.load(app.conversation.id) is None


async def test_switch_to_restores_conversation_history():
    app = ChatApp(mock_settings())
    async with app.run_test() as pilot:
        await _submit(pilot, "hello")

        for _ in range(200):
            await pilot.pause()
            if not app._generating:
                break
            await asyncio.sleep(0.05)

        first_id = app.conversation.id

        await pilot.press("ctrl+n")
        await pilot.pause()
        assert not list(app.query(MessageBubble))

        await app._switch_to(first_id)
        await pilot.pause()

        bubbles = list(app.query(MessageBubble))
        assert len(bubbles) == 2
        assert bubbles[0].message.role == "user"
        assert bubbles[0].message.content == "hello"
        assert app.conversation.id == first_id


async def test_show_conversation_falls_back_to_raw_model_id():
    app = ChatApp(mock_settings())
    async with app.run_test():
        conversation = Conversation(
            messages=[
                Message(role="assistant", content="x", model_id="not-a-real-model")
            ]
        )
        await app._show_conversation(conversation)

        bubble = list(app.query(MessageBubble))[0]
        assert "not-a-real-model" in bubble._header.content


async def test_show_conversation_empty_shows_placeholder_and_no_bubbles():
    app = ChatApp(mock_settings())
    async with app.run_test() as pilot:
        await app._show_conversation(Conversation())
        await pilot.pause()

        assert not list(app.query(MessageBubble))
        assert list(app.query(".placeholder"))


async def test_switching_cancels_running_generation_and_keeps_partial_reply():
    app = ChatApp(mock_settings())
    async with app.run_test() as pilot:
        await _submit(pilot, "a long enough prompt to stream")
        await asyncio.sleep(0.6)
        assert app._generating, "should still be mid-generation"

        old_conversation = app.conversation
        target = Conversation()
        await app.chat.store.save(target)

        await app._switch_to(target.id)

        for _ in range(60):
            await pilot.pause()
            if not app._generating:
                break
            await asyncio.sleep(0.05)

        assert not app._generating
        assert app.conversation.id == target.id
        assert old_conversation.messages[-1].content.strip() != ""


async def test_switch_to_unknown_id_notifies_and_leaves_conversation_unchanged():
    app = ChatApp(mock_settings())
    async with app.run_test():
        current = app.conversation
        await app._switch_to("does-not-exist")
        assert app.conversation is current


async def test_ctrl_l_opens_picker_listing_conversations_and_new_row():
    app = ChatApp(mock_settings())
    async with app.run_test() as pilot:
        await _submit(pilot, "hello")
        await _wait_until_done(pilot, app)

        await pilot.press("ctrl+l")
        await _wait_for_screen(pilot, app, ConversationPicker)

        picker = app.screen
        rows = list(picker.query_one(ListView).children)
        assert len(rows) == 2  # the saved conversation + "+ New conversation"


async def test_escape_dismisses_picker_without_affecting_generation_state():
    app = ChatApp(mock_settings())
    async with app.run_test() as pilot:
        main_screen = app.screen

        await pilot.press("ctrl+l")
        await _wait_for_screen(pilot, app, ConversationPicker)
        generating_before = app._generating

        await pilot.press("escape")
        await pilot.pause()

        assert app.screen is main_screen
        assert app._generating == generating_before


async def test_picker_switches_to_selected_conversation():
    app = ChatApp(mock_settings())
    async with app.run_test() as pilot:
        await _submit(pilot, "first")
        await _wait_until_done(pilot, app)
        first_id = app.conversation.id

        await pilot.press("ctrl+n")
        await pilot.pause()

        await _submit(pilot, "second")
        await _wait_until_done(pilot, app)

        await pilot.press("ctrl+l")
        await _wait_for_screen(pilot, app, ConversationPicker)

        # Most-recently-updated first: index 0 is the current (second)
        # conversation, index 1 is the first one.
        await pilot.press("down")
        await pilot.press("enter")

        for _ in range(60):
            await pilot.pause()
            if app.conversation.id == first_id:
                break
            await asyncio.sleep(0.05)

        assert app.conversation.id == first_id
        bubbles = list(app.query(MessageBubble))
        assert any(
            b.message.role == "user" and b.message.content == "first" for b in bubbles
        )


async def test_picker_new_conversation_row_starts_a_fresh_conversation():
    app = ChatApp(mock_settings())
    async with app.run_test() as pilot:
        await _submit(pilot, "hello")
        await _wait_until_done(pilot, app)
        old_id = app.conversation.id

        await pilot.press("ctrl+l")
        await _wait_for_screen(pilot, app, ConversationPicker)

        # Only conversation is at index 0; "+ New conversation" is next.
        await pilot.press("down")
        await pilot.press("enter")

        for _ in range(60):
            await pilot.pause()
            if app.conversation.id != old_id:
                break
            await asyncio.sleep(0.05)

        assert app.conversation.id != old_id
        assert not list(app.query(MessageBubble))


async def test_ctrl_l_with_empty_store_shows_empty_state():
    app = ChatApp(mock_settings())
    async with app.run_test() as pilot:
        await pilot.press("ctrl+l")
        await _wait_for_screen(pilot, app, ConversationPicker)

        picker = app.screen
        assert list(picker.query(".picker__empty"))
        assert not list(picker.query(ListView))


async def test_ctrl_l_during_generation_does_not_cancel_it():
    app = ChatApp(mock_settings())
    async with app.run_test() as pilot:
        await _submit(pilot, "a long enough prompt to stream")
        await asyncio.sleep(0.3)
        assert app._generating, "should still be mid-generation"

        await pilot.press("ctrl+l")
        await _wait_for_screen(pilot, app, ConversationPicker)

        assert app._generating

        await pilot.press("escape")
        await pilot.pause()
        await _wait_until_done(pilot, app)


async def test_picker_marks_the_active_conversation_row():
    app = ChatApp(mock_settings())
    async with app.run_test() as pilot:
        await _submit(pilot, "hello")
        await _wait_until_done(pilot, app)

        await pilot.press("ctrl+l")
        await _wait_for_screen(pilot, app, ConversationPicker)

        picker = app.screen
        current_rows = list(picker.query(".picker__item.-current"))
        assert len(current_rows) == 1


async def test_ctrl_x_on_highlighted_conversation_deletes_after_confirmation():
    app = ChatApp(mock_settings())
    async with app.run_test() as pilot:
        await _submit(pilot, "first")
        await _wait_until_done(pilot, app)
        first_id = app.conversation.id

        await pilot.press("ctrl+n")
        await pilot.pause()

        await _submit(pilot, "second")
        await _wait_until_done(pilot, app)

        await pilot.press("ctrl+l")
        await _wait_for_screen(pilot, app, ConversationPicker)

        # Index 0 is the current ("second") conversation; move to "first".
        await pilot.press("down")
        await pilot.press("ctrl+x")
        await _wait_for_screen(pilot, app, ConfirmModal)

        # Cancel is focused by default; Tab reaches Delete.
        await pilot.press("tab")
        await pilot.press("enter")
        await _wait_for_screen(pilot, app, ConversationPicker)

        remaining = await app.chat.list_conversations()
        assert first_id not in {c.id for c in remaining}
        rows = list(app.screen.query_one(ListView).children)
        # Only "second" (current) remains, plus "+ New conversation".
        assert len(rows) == 2

        await pilot.press("escape")
        await pilot.pause()


async def test_ctrl_x_cancel_leaves_conversation_in_store_and_reopens_picker():
    app = ChatApp(mock_settings())
    async with app.run_test() as pilot:
        await _submit(pilot, "first")
        await _wait_until_done(pilot, app)
        first_id = app.conversation.id

        await pilot.press("ctrl+n")
        await pilot.pause()
        await _submit(pilot, "second")
        await _wait_until_done(pilot, app)

        await pilot.press("ctrl+l")
        await _wait_for_screen(pilot, app, ConversationPicker)

        await pilot.press("down")
        await pilot.press("ctrl+x")
        await _wait_for_screen(pilot, app, ConfirmModal)

        # Cancel is already focused: a bare Enter must be the safe choice.
        await pilot.press("enter")
        await _wait_for_screen(pilot, app, ConversationPicker)

        remaining = await app.chat.list_conversations()
        assert first_id in {c.id for c in remaining}
        rows = list(app.screen.query_one(ListView).children)
        assert len(rows) == 3  # "first", "second" (current), "+ New conversation"

        await pilot.press("escape")
        await pilot.pause()


async def test_ctrl_x_on_new_conversation_row_does_nothing():
    app = ChatApp(mock_settings())
    async with app.run_test() as pilot:
        await _submit(pilot, "hello")
        await _wait_until_done(pilot, app)

        await pilot.press("ctrl+l")
        await _wait_for_screen(pilot, app, ConversationPicker)

        # Index 0 is the current conversation; index 1 is "+ New conversation".
        await pilot.press("down")
        await pilot.press("ctrl+x")
        await pilot.pause()

        assert isinstance(app.screen, ConversationPicker)

        await pilot.press("escape")
        await pilot.pause()


async def test_deleting_active_conversation_switches_to_most_recent_remaining():
    app = ChatApp(mock_settings())
    async with app.run_test() as pilot:
        await _submit(pilot, "first")
        await _wait_until_done(pilot, app)
        first_id = app.conversation.id

        await pilot.press("ctrl+n")
        await pilot.pause()
        await _submit(pilot, "second")
        await _wait_until_done(pilot, app)
        second_id = app.conversation.id

        await pilot.press("ctrl+l")
        await _wait_for_screen(pilot, app, ConversationPicker)

        # Index 0 is the current ("second") conversation — delete it.
        await pilot.press("ctrl+x")
        await _wait_for_screen(pilot, app, ConfirmModal)
        await pilot.press("tab")
        await pilot.press("enter")

        for _ in range(60):
            await pilot.pause()
            if app.conversation.id != second_id:
                break
            await asyncio.sleep(0.05)

        assert app.conversation.id == first_id
        assert not isinstance(app.screen, ConversationPicker)
        remaining = await app.chat.list_conversations()
        assert second_id not in {c.id for c in remaining}
        bubbles = list(app.query(MessageBubble))
        assert any(
            b.message.role == "user" and b.message.content == "first" for b in bubbles
        )


async def test_deleting_only_conversation_leaves_fresh_empty_state_without_resurrection():
    app = ChatApp(mock_settings())
    async with app.run_test() as pilot:
        await _submit(pilot, "only one")
        await _wait_until_done(pilot, app)
        only_id = app.conversation.id

        await pilot.press("ctrl+l")
        await _wait_for_screen(pilot, app, ConversationPicker)

        await pilot.press("ctrl+x")
        await _wait_for_screen(pilot, app, ConfirmModal)
        await pilot.press("tab")
        await pilot.press("enter")

        for _ in range(60):
            await pilot.pause()
            if app.conversation.id != only_id:
                break
            await asyncio.sleep(0.05)

        assert app.conversation.id != only_id
        assert app.conversation.messages == []
        assert not list(app.query(MessageBubble))
        assert not isinstance(app.screen, ConversationPicker)

        # The deleted conversation must not have been resurrected by a
        # persist call on the way out.
        remaining = await app.chat.list_conversations()
        assert remaining == []
        assert await app.chat.store.load(only_id) is None


async def test_confirm_modal_opens_with_cancel_focused():
    app = ChatApp(mock_settings())
    async with app.run_test() as pilot:
        await _submit(pilot, "hello")
        await _wait_until_done(pilot, app)

        await pilot.press("ctrl+l")
        await _wait_for_screen(pilot, app, ConversationPicker)

        await pilot.press("ctrl+x")
        await _wait_for_screen(pilot, app, ConfirmModal)

        modal = app.screen
        assert modal.query_one("#confirm-cancel").has_focus

        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()


async def test_delete_conversation_storage_error_notifies_and_keeps_running():
    app = ChatApp(mock_settings())
    async with app.run_test() as pilot:
        await _submit(pilot, "first")
        await _wait_until_done(pilot, app)

        await pilot.press("ctrl+n")
        await pilot.pause()
        await _submit(pilot, "second")
        await _wait_until_done(pilot, app)

        async def boom(conversation_id: str) -> None:
            raise StorageError("boom")

        app.chat.delete_conversation = boom

        await pilot.press("ctrl+l")
        await _wait_for_screen(pilot, app, ConversationPicker)

        await pilot.press("down")
        await pilot.press("ctrl+x")
        await _wait_for_screen(pilot, app, ConfirmModal)
        await pilot.press("tab")
        await pilot.press("enter")

        await _wait_for_screen(pilot, app, ConversationPicker)
        assert app.is_running

        await pilot.press("escape")
        await pilot.pause()
