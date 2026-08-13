"""Entry point: ``uv run agentchat`` (or ``python -m agentchat``)."""

from __future__ import annotations

from agentchat.config import Settings
from agentchat.llm.transcript import setup_llm_log
from agentchat.log import setup_logging
from agentchat.ui.app import ChatApp


def main() -> None:
    settings = Settings.from_env()
    setup_logging(settings.resolved_log_dir)
    setup_llm_log(settings)
    ChatApp(settings).run()


if __name__ == "__main__":
    main()
