"""File logging setup. Named ``log.py``, not ``logging.py`` — a sibling
shadowing the stdlib name would make every ``import logging`` in the package
ambiguous."""

from __future__ import annotations

import logging
from pathlib import Path

LOG_FILENAME = "agentchat.log"


def setup_logging(data_dir: Path, level: int = logging.INFO) -> Path:
    """Attach a file handler at ``data_dir/agentchat.log``; return its path.
    Idempotent — a second call does not double the handler."""
    data_dir.mkdir(parents=True, exist_ok=True)
    log_path = data_dir / LOG_FILENAME

    root = logging.getLogger()
    root.setLevel(level)

    for handler in root.handlers:
        if isinstance(handler, logging.FileHandler) and Path(handler.baseFilename) == log_path:
            return log_path

    handler = logging.FileHandler(log_path)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    return log_path
