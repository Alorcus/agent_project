"""End-to-end tests through Textual's headless pilot.

These drive the real app the way a user would, which is the only way to catch
the failure this milestone is actually about: a UI that freezes while a reply
streams.
"""

from __future__ import annotations

import asyncio

from textual.widgets import Input, Static

from agentchat.config import Settings
from agentchat.ui.app import ChatApp
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
