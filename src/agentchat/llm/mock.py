"""Mock backend.

Not meant to be convincing — meant to have the same timing shape as a real
local model: a slow load, then chunks arriving one at a time with gaps. That
keeps the non-blocking/streaming/stop machinery exercised without a GPU.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Sequence

from agentchat.core import usage
from agentchat.core.errors import ProviderError
from agentchat.core.models import Message
from agentchat.llm import transcript
from agentchat.llm.base import GenerationOptions, ModelInfo

_LOREM = (
    "This is a mocked reply. The backend is a stub, so nothing here reflects an "
    "actual model — but it arrives the way a local model's output would: chunk by "
    "chunk, over a real interval, on a worker that the interface can cancel."
)

#: Tagged the way a reasoning model tags it, so the interface's handling of a
#: thinking block is exercised without a GPU.
_THINKING_BLOCK = (
    "<think>\nConsidering the request, the available context, and what a useful "
    "answer would contain.\n</think>\n\n"
)


class MockProvider:
    """An ``LLMProvider`` that fabricates streamed text.

    ``load_delay`` and ``fail`` mirror a real backend's failure modes so error
    handling can be tested without one.
    """

    def __init__(
        self,
        info: ModelInfo,
        *,
        chunk_delay: float = 0.045,
        load_delay: float = 0.4,
        fail: bool = False,
        seed: int | None = None,
    ) -> None:
        self._info = info
        self._chunk_delay = chunk_delay
        self._load_delay = load_delay
        self._fail = fail
        self._loaded = False
        self._rng = random.Random(seed)

    @property
    def info(self) -> ModelInfo:
        return self._info

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    async def load(self) -> None:
        if self._loaded:
            return
        if self._fail:
            raise ProviderError(f"{self._info.name} is unavailable (simulated failure)")
        await asyncio.sleep(self._load_delay)
        self._loaded = True

    async def unload(self) -> None:
        self._loaded = False

    async def generate(
        self,
        messages: Sequence[Message],
        options: GenerationOptions | None = None,
    ) -> AsyncIterator[str]:
        if not self._loaded:
            raise ProviderError(f"{self._info.name} is not loaded")
        options = options or GenerationOptions()

        call_id = transcript.new_call_id()
        label = transcript.current_label()
        transcript.record_request(
            call_id=call_id,
            label=label,
            model_id=self._info.id,
            backend="mock",
            messages=messages,
            prompt_text=None,  # no template on this backend (KTD5)
            prompt_tokens=None,  # no tokenizer on this backend
            options=options,
        )
        metered = usage.record(
            label=label,
            messages=messages,
            prompt_tokens=None,  # estimated from the messages instead
            context_window=self._info.context_window,
        )
        text = self._compose(messages, options)
        emitted: list[str] = []
        try:
            for token in _tokenize(text):
                # Jitter keeps the UI honest: no fixed cadence to accidentally rely on.
                await asyncio.sleep(self._chunk_delay * self._rng.uniform(0.5, 1.5))
                emitted.append(token)
                yield token
        finally:
            output = "".join(emitted)
            transcript.record_response(
                call_id=call_id,
                label=label,
                model_id=self._info.id,
                backend="mock",
                output=output,
                completion_tokens=None,
                outcome="complete" if output == text else "stopped",
            )
            if metered is not None:
                metered.complete(completion_tokens=None, output=output)

    def _compose(self, messages: Sequence[Message], options: GenerationOptions) -> str:
        last_user = next(
            (m.content for m in reversed(messages) if m.role == "user"), ""
        )
        turns = sum(1 for m in messages if m.role in ("user", "assistant"))
        parts: list[str] = []
        if options.thinking:
            parts.append(_THINKING_BLOCK)
        parts.append(f'You said: "{last_user.strip()}". ')
        parts.append(_LOREM)
        parts.append(
            f" [{self._info.name} · turn {turns} · {len(messages)} messages in context]"
        )
        return "".join(parts)


def _tokenize(text: str) -> list[str]:
    """Split into whitespace-preserving pieces so streamed output reassembles
    exactly into ``text``."""
    out: list[str] = []
    for word in text.split(" "):
        out.append(word + " ")
    if out:
        out[-1] = out[-1][:-1]
    return out


def default_models(
    *, chunk_delay: float | None = None, load_delay: float | None = None
) -> list[tuple[ModelInfo, dict]]:
    """The mock model catalogue: two entries with different context windows,
    so model switching and window-aware trimming are both testable.

    ``chunk_delay``/``load_delay``, when given, replace both entries' timing —
    tests that don't care about real streaming/loading gaps can run near
    instantly instead of paying the realistic delays below.
    """
    models = [
        (
            ModelInfo(
                id="mock-small",
                name="Mock Small",
                context_window=2048,
                description="Fast stub. Narrow window — trips context trimming sooner.",
            ),
            {"chunk_delay": 0.03, "load_delay": 0.3, "seed": 1},
        ),
        (
            ModelInfo(
                id="mock-large",
                name="Mock Large",
                context_window=8192,
                description="Slower stub with a wider window.",
            ),
            {"chunk_delay": 0.07, "load_delay": 0.9, "seed": 2},
        ),
    ]
    for _, kwargs in models:
        if chunk_delay is not None:
            kwargs["chunk_delay"] = chunk_delay
        if load_delay is not None:
            kwargs["load_delay"] = load_delay
    return models
