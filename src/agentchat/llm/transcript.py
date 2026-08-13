"""Verbatim LLM I/O transcript.

Recorded *inside* each backend, at the exact point tokenization happens and
the exact point generation returns — not wrapped around ``LLMProvider``. A
wrapper would only ever see the ``Message`` list going in and the
UI-cleaned-up text coming out, which is precisely what this file exists to
see past. Do not "tidy" the recording calls out of ``llm/local.py`` and
``llm/mock.py`` into a decorator — that would silently make the transcript
describe the wrong thing.

Inert until ``setup_llm_log`` is called: the logger carries a
``NullHandler`` and does not propagate, so an unconfigured process (tests, a
library import, an ad-hoc script) writes nothing and never reaches
``logging.lastResort``, which prints to stderr.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentchat.log import attach_jsonl_handler

LOGGER_NAME = "agentchat.llm.transcript"
FILENAME = "llm.jsonl"

_log = logging.getLogger(LOGGER_NAME)
_log.addHandler(logging.NullHandler())
_log.propagate = False

_RUN_ID = uuid.uuid4().hex
_call_label: ContextVar[str] = ContextVar("llm_call_label", default="unknown")
_enabled = False


def setup_llm_log(settings: Any) -> Path | None:
    """Open the transcript at ``settings.resolved_log_dir/llm.jsonl``.
    Returns ``None`` — and leaves the logger inert — when
    ``settings.log_llm_io`` is off."""
    global _enabled
    if not settings.log_llm_io:
        _enabled = False
        return None
    path = attach_jsonl_handler(_log, settings.resolved_log_dir / FILENAME)
    _log.setLevel(logging.INFO)
    _enabled = True
    return path


def is_enabled() -> bool:
    return _enabled


@contextmanager
def label(name: str) -> Iterator[None]:
    """Set the caller label recorded by every request/response written
    inside this block."""
    token = _call_label.set(name)
    try:
        yield
    finally:
        _call_label.reset(token)


def current_label() -> str:
    return _call_label.get()


def new_call_id() -> str:
    return uuid.uuid4().hex


def record_request(
    *,
    call_id: str,
    label: str,
    model_id: str,
    backend: str,
    messages: Sequence[Any],
    prompt_text: str | None,
    prompt_tokens: int | None,
    options: Any,
) -> None:
    if not _enabled:
        return
    try:
        _emit(
            _base("request", call_id, label, model_id, backend),
            {
                "messages": [
                    {"role": m.role, "content": m.content} for m in messages
                ],
                "prompt_text": prompt_text,
                "prompt_tokens": prompt_tokens,
                "options": {
                    "temperature": options.temperature,
                    "max_tokens": options.max_tokens,
                    "thinking": options.thinking,
                    "stop": list(options.stop),
                },
            },
        )
    except Exception:
        pass


def record_response(
    *,
    call_id: str,
    label: str,
    model_id: str,
    backend: str,
    output: str,
    completion_tokens: int | None,
    outcome: str,
    error: str | None = None,
) -> None:
    if not _enabled:
        return
    try:
        fields: dict[str, Any] = {
            "output": output,
            "completion_tokens": completion_tokens,
            "outcome": outcome,
        }
        if error is not None:
            fields["error"] = error
        _emit(_base("response", call_id, label, model_id, backend), fields)
    except Exception:
        pass


def _base(record_type: str, call_id: str, label: str, model_id: str, backend: str) -> dict[str, Any]:
    return {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "type": record_type,
        "call_id": call_id,
        "run_id": _RUN_ID,
        "label": label,
        "model_id": model_id,
        "backend": backend,
    }


def _emit(base: dict[str, Any], fields: dict[str, Any]) -> None:
    record = {**base, **fields}
    _log.info(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
