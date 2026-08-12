"""End-to-end tests through Textual's headless pilot.

These drive the real app the way a user would, which is the only way to catch
the failure this milestone is actually about: a UI that freezes while a reply
streams.
"""

from __future__ import annotations

import asyncio

from textual.widgets import Input, ListView, Static

from agentchat.core.errors import StorageError
from agentchat.core.models import DEFAULT_GROUP_ID, Conversation, Group, Message
from agentchat.ui.app import ChatApp
from agentchat.ui.screens import ConversationPicker, GroupChooser
from agentchat.ui.widgets import ConversationHeader, MessageBubble
from conftest import mock_settings
from factories import make_conversation

# mock_settings() defaults to near-instant timing; tests that assert on
# mid-generation state need a real gap to observe, so they opt back into
# mock.default_models()'s realistic load/chunk delays.
_REALISTIC_TIMING = {"mock_chunk_delay": None, "mock_load_delay": None}


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


async def _wait_for_rows(pilot, app, count: int) -> None:
    """The picker is refreshed by the app after it has awaited the store, so
    the new row set lands a tick or two after the confirming keypress."""
    for _ in range(60):
        await pilot.pause()
        rows = list(app.screen.query(ListView).first().children)
        if len(rows) == count:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"picker never settled on {count} rows")


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
    app = ChatApp(mock_settings(**_REALISTIC_TIMING))
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
    app = ChatApp(mock_settings(**_REALISTIC_TIMING))
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
    app = ChatApp(mock_settings(**_REALISTIC_TIMING))
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
        await _wait_until_done(pilot, app)

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
    app = ChatApp(mock_settings(**_REALISTIC_TIMING))
    async with app.run_test() as pilot:
        await _submit(pilot, "a long enough prompt to stream")
        await asyncio.sleep(0.6)
        assert app._generating, "should still be mid-generation"

        old_conversation = app.conversation
        target = Conversation()
        await app.chat.store.save(target)

        await app._switch_to(target.id)
        await _wait_until_done(pilot, app)

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
    app = ChatApp(mock_settings(**_REALISTIC_TIMING))
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


async def test_ctrl_x_shows_inline_confirm_and_y_deletes():
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
        picker = app.screen

        # Index 0 is the current ("second") conversation; move to "first".
        await pilot.press("down")
        await pilot.press("ctrl+x")
        await pilot.pause()

        hint = app.screen.query_one("#picker-hint")
        assert "y confirms" in hint.content
        assert hint.has_class("-confirming")

        await pilot.press("y")
        # Only "second" (current) remains, plus "+ New conversation".
        await _wait_for_rows(pilot, app, 2)

        remaining = await app.chat.list_all_conversations()
        assert first_id not in {c.id for c in remaining}
        # The same screen the whole time: a delete must not close and reopen
        # the picker.
        assert app.screen is picker

        await pilot.press("escape")
        await pilot.pause()


async def test_ctrl_x_any_other_key_cancels_and_leaves_conversation_in_store():
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
        await pilot.pause()

        # A stray Enter (or anything but "y") must be the safe choice.
        await pilot.press("enter")
        await pilot.pause()

        hint = app.screen.query_one("#picker-hint")
        assert "y confirms" not in hint.content
        assert not hint.has_class("-confirming")
        assert isinstance(app.screen, ConversationPicker)

        remaining = await app.chat.list_all_conversations()
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
        hint = app.screen.query_one("#picker-hint")
        assert "y confirms" not in hint.content

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
        picker = app.screen

        # Index 0 is the current ("second") conversation — delete it.
        await pilot.press("ctrl+x")
        await pilot.pause()
        await pilot.press("y")

        for _ in range(60):
            await pilot.pause()
            if app.conversation.id != second_id:
                break
            await asyncio.sleep(0.05)

        assert app.conversation.id == first_id
        remaining = await app.chat.list_all_conversations()
        assert second_id not in {c.id for c in remaining}

        # Deleting the conversation being viewed swaps another one in behind
        # the picker; the picker itself stays open, now marking that one.
        assert app.screen is picker
        await _wait_for_rows(pilot, app, 2)  # "first", plus "+ New conversation"
        assert len(list(picker.query(".picker__item.-current"))) == 1

        await pilot.press("escape")
        await pilot.pause()
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
        picker = app.screen

        await pilot.press("ctrl+x")
        await pilot.pause()
        await pilot.press("y")

        for _ in range(60):
            await pilot.pause()
            if app.conversation.id != only_id:
                break
            await asyncio.sleep(0.05)

        assert app.conversation.id != only_id
        assert app.conversation.messages == []

        # Nothing left to list, so the still-open picker falls back to its
        # empty state rather than closing.
        assert app.screen is picker
        for _ in range(60):
            await pilot.pause()
            if list(picker.query(".picker__empty")):
                break
            await asyncio.sleep(0.05)
        assert list(picker.query(".picker__empty"))
        assert not list(picker.query(ListView))

        # The deleted conversation must not have been resurrected by a
        # persist call on the way out.
        remaining = await app.chat.list_all_conversations()
        assert remaining == []
        assert await app.chat.store.load(only_id) is None

        await pilot.press("escape")
        await pilot.pause()
        assert not list(app.query(MessageBubble))


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
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()

        assert isinstance(app.screen, ConversationPicker)
        assert app.is_running

        await pilot.press("escape")
        await pilot.pause()


# -- conversation groups --------------------------------------------------


async def _project_group(app, name: str) -> Group:
    """A project group, with the app's name cache refreshed the way
    action_choose_group refreshes it after creating one."""
    group = await app.chat.create_group(name)
    await app._refresh_groups()
    return group


def _header_text(app) -> str:
    header = app._chat_screen.query_one("#conversation-header", ConversationHeader)
    return "".join(str(part.content) for part in header.query(Static) if part.display)


def _row_texts(picker) -> list[str]:
    return [
        str(row.query_one(Static).content)
        for row in picker.query_one(ListView).children
    ]


def _chooser_names(chooser) -> list[str]:
    """Group names in display order, with the "+ New group…" row last."""
    return [
        str(row.query(Static).first().content)
        for row in chooser.query_one(ListView).children
    ]


async def _wait_for_group(pilot, app, group_id: str) -> None:
    for _ in range(60):
        await pilot.pause()
        if app.conversation.group_id == group_id:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"conversation never landed in group {group_id!r}")


async def _open_chooser(pilot, app) -> GroupChooser:
    await pilot.press("ctrl+g")
    await _wait_for_screen(pilot, app, GroupChooser)
    return app.screen


async def _begin_new_group_edit(pilot, chooser: GroupChooser) -> None:
    """Move to "+ New group…" — always the last row — and open it."""
    chooser.query_one(ListView).index = len(chooser.query_one(ListView).children) - 1
    await pilot.press("enter")
    await pilot.pause()


async def test_header_states_the_group_for_a_project_chat_only():
    app = ChatApp(mock_settings())
    async with app.run_test() as pilot:
        await _submit(pilot, "why is the sky blue")
        await _wait_until_done(pilot, app)

        # The default group is the absence of a group, as far as a user is
        # concerned, so its name never appears.
        assert _header_text(app) == "why is the sky blue"

        group = await _project_group(app, "Thesis")
        app.conversation = await app.chat.new_conversation(group.id)
        app._refresh_status()
        await pilot.pause()

        assert _header_text(app) == "Thesis  ›  New conversation"


async def test_a_title_that_looks_like_markup_renders_literally_in_the_header():
    app = ChatApp(mock_settings())
    async with app.run_test() as pilot:
        await _submit(pilot, "[bold]red")
        await _wait_until_done(pilot, app)

        assert _header_text(app) == "[bold]red"


async def test_ctrl_n_inherits_the_group_of_the_chat_it_was_pressed_in():
    app = ChatApp(mock_settings())
    async with app.run_test() as pilot:
        group = await _project_group(app, "Thesis")
        app.conversation = await app.chat.new_conversation(group.id)

        await pilot.press("ctrl+n")
        await pilot.pause()

        assert app.conversation.group_id == group.id
        # Inheritance is instant, or it is not worth having.
        assert len(app.screen_stack) == 1


async def test_ctrl_n_in_an_unfiled_chat_stays_in_the_default_group():
    app = ChatApp(mock_settings())
    async with app.run_test() as pilot:
        await _project_group(app, "Thesis")

        await pilot.press("ctrl+n")
        await pilot.pause()

        assert app.conversation.group_id == DEFAULT_GROUP_ID
        assert len(app.screen_stack) == 1


async def test_ctrl_g_starts_the_conversation_in_the_chosen_group():
    app = ChatApp(mock_settings())
    async with app.run_test() as pilot:
        thesis = await _project_group(app, "Thesis")

        chooser = await _open_chooser(pilot, app)
        # Project groups first, the default group last — and the current
        # group is the one the list opens on.
        assert _chooser_names(chooser) == ["Thesis", "Chats", "+ New group…"]
        assert chooser.query_one(ListView).index == 1

        await pilot.press("up")
        await pilot.press("enter")
        await _wait_for_group(pilot, app, thesis.id)

        assert app.conversation.group_id == thesis.id


async def test_ctrl_g_can_take_a_project_chat_back_out_to_the_default_group():
    app = ChatApp(mock_settings())
    async with app.run_test() as pilot:
        thesis = await _project_group(app, "Thesis")
        app.conversation = await app.chat.new_conversation(thesis.id)

        chooser = await _open_chooser(pilot, app)
        assert chooser.query_one(ListView).index == 0

        await pilot.press("down")
        await pilot.press("enter")
        await _wait_for_group(pilot, app, DEFAULT_GROUP_ID)

        assert app.conversation.group_id == DEFAULT_GROUP_ID


async def test_new_group_row_creates_the_group_and_starts_a_chat_in_it():
    app = ChatApp(mock_settings())
    async with app.run_test() as pilot:
        chooser = await _open_chooser(pilot, app)
        await _begin_new_group_edit(pilot, chooser)

        assert chooser.query_one("#group-name", Input).has_focus
        await pilot.press("t", "h", "e", "s", "i", "s")
        await pilot.press("enter")

        for _ in range(60):
            await pilot.pause()
            created = [g for g in await app.chat.list_groups() if g.name == "thesis"]
            if created:
                break
            await asyncio.sleep(0.05)

        assert created, "the group was never written to the store"
        await _wait_for_group(pilot, app, created[0].id)


async def test_new_group_row_left_empty_creates_nothing_and_restores_the_label():
    app = ChatApp(mock_settings())
    async with app.run_test() as pilot:
        chooser = await _open_chooser(pilot, app)
        await _begin_new_group_edit(pilot, chooser)

        await pilot.press("enter")
        await pilot.pause()

        assert not list(chooser.query(Input))
        assert _chooser_names(chooser) == ["Chats", "+ New group…"]
        assert app.screen is chooser
        assert [g.name for g in await app.chat.list_groups()] == ["Chats"]


async def test_a_duplicate_group_name_is_rejected_without_leaving_the_edit():
    app = ChatApp(mock_settings())
    async with app.run_test() as pilot:
        await _project_group(app, "Thesis")

        chooser = await _open_chooser(pilot, app)
        await _begin_new_group_edit(pilot, chooser)

        await pilot.press("t", "h", "e", "s", "i", "s")
        await pilot.press("enter")
        await pilot.pause()

        hint = chooser.query_one("#chooser-hint", Static)
        assert "already exists" in str(hint.content)
        # The text stays put, so fixing the name is not retyping it.
        field = chooser.query_one("#group-name", Input)
        assert field.value == "thesis"
        assert field.has_focus
        assert len(await app.chat.list_groups()) == 2


async def test_escape_while_naming_a_group_cancels_the_edit_not_the_chooser():
    app = ChatApp(mock_settings())
    async with app.run_test() as pilot:
        chooser = await _open_chooser(pilot, app)
        await _begin_new_group_edit(pilot, chooser)

        await pilot.press("escape")
        await pilot.pause()

        assert app.screen is chooser
        assert not list(chooser.query(Input))


async def test_arrow_keys_still_move_the_list_after_an_edit_ends():
    app = ChatApp(mock_settings())
    async with app.run_test() as pilot:
        await _project_group(app, "Thesis")

        chooser = await _open_chooser(pilot, app)
        await _begin_new_group_edit(pilot, chooser)
        await pilot.press("escape")
        await pilot.pause()

        list_view = chooser.query_one(ListView)
        assert list_view.has_focus
        before = list_view.index
        await pilot.press("up")
        await pilot.pause()
        assert list_view.index != before


async def test_arrow_keys_do_not_move_the_list_while_naming_a_group():
    app = ChatApp(mock_settings())
    async with app.run_test() as pilot:
        await _project_group(app, "Thesis")

        chooser = await _open_chooser(pilot, app)
        await _begin_new_group_edit(pilot, chooser)

        list_view = chooser.query_one(ListView)
        before = list_view.index
        await pilot.press("up")
        await pilot.pause()

        assert list_view.index == before
        assert chooser.query_one("#group-name", Input).has_focus


async def test_escape_in_the_chooser_creates_nothing():
    app = ChatApp(mock_settings())
    async with app.run_test() as pilot:
        await _submit(pilot, "hello")
        await _wait_until_done(pilot, app)
        current = app.conversation

        await _open_chooser(pilot, app)
        await pilot.press("escape")
        await pilot.pause()

        assert app.conversation is current
        assert len(app.screen_stack) == 1
        assert len(await app.chat.list_groups()) == 1


async def test_ctrl_g_in_the_overview_opens_the_chooser_after_it_closes():
    app = ChatApp(mock_settings())
    async with app.run_test() as pilot:
        await _submit(pilot, "hello")
        await _wait_until_done(pilot, app)

        await pilot.press("ctrl+l")
        await _wait_for_screen(pilot, app, ConversationPicker)
        await pilot.press("ctrl+g")
        await _wait_for_screen(pilot, app, GroupChooser)

        # One modal on screen at a time, so the chat screen stays the bottom
        # of the stack.
        assert len(app.screen_stack) == 2

        await pilot.press("escape")
        await pilot.pause()


async def test_overview_orders_project_blocks_first_and_the_default_block_last():
    app = ChatApp(mock_settings())
    async with app.run_test() as pilot:
        thesis = await _project_group(app, "Thesis")
        await _project_group(app, "Cluster ops")
        await app.chat.store.save(
            make_conversation(title="Chapter 3", group_id=thesis.id)
        )
        await app.chat.store.save(
            make_conversation(title="Why is the sky blue", group_id=DEFAULT_GROUP_ID)
        )

        await pilot.press("ctrl+l")
        await _wait_for_screen(pilot, app, ConversationPicker)

        assert _row_texts(app.screen) == [
            "Thesis",
            "Chapter 3",
            "Cluster ops",
            "(no conversations)",
            "Why is the sky blue",
            "+ New conversation",
        ]

        await pilot.press("escape")
        await pilot.pause()


async def test_an_overview_row_is_the_title_and_nothing_else():
    app = ChatApp(mock_settings())
    async with app.run_test() as pilot:
        await _submit(pilot, "hello")
        await _wait_until_done(pilot, app)

        await pilot.press("ctrl+l")
        await _wait_for_screen(pilot, app, ConversationPicker)

        rows = list(app.screen.query_one(ListView).children)
        assert all(len(row.query(Static)) == 1 for row in rows)
        assert not list(app.screen.query(".picker__meta"))

        await pilot.press("escape")
        await pilot.pause()


async def test_arrowing_through_the_overview_skips_group_headers():
    app = ChatApp(mock_settings())
    async with app.run_test() as pilot:
        thesis = await _project_group(app, "Thesis")
        await _project_group(app, "Cluster ops")
        await app.chat.store.save(
            make_conversation(title="Chapter 3", group_id=thesis.id)
        )
        await app.chat.store.save(
            make_conversation(title="Why is the sky blue", group_id=DEFAULT_GROUP_ID)
        )

        await pilot.press("ctrl+l")
        await _wait_for_screen(pilot, app, ConversationPicker)

        list_view = app.screen.query_one(ListView)
        # Mounting steps off the "Thesis" header onto the row below it.
        assert list_view.index == 1
        await pilot.press("down")
        await pilot.pause()
        # Straight past "Cluster ops" and its "(no conversations)" row.
        assert list_view.index == 4

        await pilot.press("escape")
        await pilot.pause()


async def test_emptying_a_project_group_keeps_its_header_and_moves_the_cursor_off_it():
    app = ChatApp(mock_settings())
    async with app.run_test() as pilot:
        thesis = await _project_group(app, "Thesis")
        await app.chat.store.save(
            make_conversation(title="Chapter 3", group_id=thesis.id)
        )
        await app.chat.store.save(
            make_conversation(title="Why is the sky blue", group_id=DEFAULT_GROUP_ID)
        )

        await pilot.press("ctrl+l")
        await _wait_for_screen(pilot, app, ConversationPicker)
        picker = app.screen

        await pilot.press("ctrl+x")
        await pilot.pause()
        await pilot.press("y")

        for _ in range(60):
            await pilot.pause()
            if "(no conversations)" in _row_texts(picker):
                break
            await asyncio.sleep(0.05)

        # A conversation delete is not a group delete: the group survives it.
        assert _row_texts(picker) == [
            "Thesis",
            "(no conversations)",
            "Why is the sky blue",
            "+ New conversation",
        ]
        list_view = picker.query_one(ListView)
        assert not list_view.highlighted_child.disabled
        assert list_view.index == 2

        await pilot.press("escape")
        await pilot.pause()
