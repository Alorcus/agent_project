"""Workarounds for rough edges in ``transformers``/``torch``.

Pure functions, no knowledge of ``LLMProvider`` — kept separate so
``local.py`` reads as "how a chat model is loaded and generated from", not
"which version of transformers are we on".
"""

from __future__ import annotations

import queue
import threading
from typing import Any

from agentchat.core.errors import ProviderError

#: Sentinel returned by ``next_chunk`` once a stream is exhausted.
DONE = object()


def import_backend(model_name: str) -> tuple[Any, Any, Any]:
    """Import lazily so this module stays importable on a machine with
    neither torch nor transformers installed."""
    try:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except ImportError as error:
        raise ProviderError(
            f"{model_name} needs torch and transformers. "
            "Run the app with `uv run agentchat`, or set AGENTCHAT_BACKEND=mock."
        ) from error
    return torch, AutoModelForCausalLM, AutoTokenizer


def quiet_backend() -> None:
    """Silence transformers' stderr output (progress bars, warnings) — the
    app owns the terminal and would otherwise get painted over."""
    from transformers.utils import logging as hf_logging

    hf_logging.disable_progress_bar()
    hf_logging.set_verbosity_error()


def dtype_kwarg(dtype: Any) -> dict[str, Any]:
    """``torch_dtype`` was renamed to ``dtype`` in transformers 4.56, and
    ``from_pretrained`` silently ignores unknown kwargs — passing the wrong
    name loads a bf16 checkpoint as fp32 instead of raising."""
    from transformers import __version__ as version

    try:
        release = tuple(int(part) for part in version.split(".")[:2])
    except ValueError:  # pragma: no cover — unparseable dev version
        release = (0, 0)
    return {"dtype": dtype} if release >= (4, 56) else {"torch_dtype": dtype}


def next_chunk(streamer: Any, model_name: str, timeout: float) -> Any:
    """Pull one chunk from a ``TextIteratorStreamer``, translating its
    exceptions into the ``DONE`` sentinel or a ``ProviderError``."""
    try:
        return next(streamer)
    except StopIteration:
        return DONE
    except queue.Empty as error:
        raise ProviderError(
            f"{model_name}: no tokens for {timeout:.0f}s — backend wedged"
        ) from error


def stop_when_set(event: threading.Event) -> Any:
    """A ``StoppingCriteria`` that ends generation once ``event`` is set."""
    from transformers import StoppingCriteria

    class _Criteria(StoppingCriteria):
        def __call__(self, input_ids: Any, scores: Any, **kwargs: Any) -> bool:
            return event.is_set()

    return _Criteria()
