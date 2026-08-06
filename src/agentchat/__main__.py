"""Entry point: ``uv run agentchat`` (or ``python -m agentchat``)."""

from __future__ import annotations

from agentchat.config import Settings
from agentchat.ui.app import ChatApp


def main() -> None:
    ChatApp(Settings.from_env()).run()


if __name__ == "__main__":
    main()
