"""Entry point: ``uv run agentchat`` (or ``python -m agentchat``)."""

from __future__ import annotations

from agentchat.config import Settings
from agentchat.core.memory.tuning import Tuning, log_effective_tuning
from agentchat.log import setup_logging
from agentchat.ui.app import ChatApp


def main() -> None:
    settings = Settings.from_env()
    setup_logging(settings.data_dir)
    log_effective_tuning(Tuning.from_env())
    ChatApp(settings).run()


if __name__ == "__main__":
    main()
