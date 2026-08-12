"""The boundary between the app and any model backend.

Everything above this line (UI, chat service) talks only to ``LLMProvider``.
Swapping the mock for a real backend must not touch the UI.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from agentchat.core.models import Message


@dataclass(frozen=True)
class ModelInfo:
    """Static description of a selectable model or adapter.

    ``base_model_id`` set marks this as a LoRA/QLoRA adapter riding on a shared
    resident base model rather than its own full copy.
    """

    id: str
    name: str
    context_window: int
    description: str = ""
    base_model_id: str | None = None

    @property
    def is_adapter(self) -> bool:
        return self.base_model_id is not None


@dataclass(frozen=True)
class GenerationOptions:
    """Per-request knobs."""

    temperature: float = 0.7
    max_tokens: int = 1024
    thinking: bool = False
    stop: tuple[str, ...] = field(default_factory=tuple)


@runtime_checkable
class LLMProvider(Protocol):
    """A loadable, streaming text generator.

    Implementations must be cancellable: when the consumer stops iterating
    ``generate`` (task cancellation), generation aborts promptly.
    """

    @property
    def info(self) -> ModelInfo: ...

    @property
    def is_loaded(self) -> bool: ...

    async def load(self) -> None:
        """Acquire weights/resources. Idempotent; may be slow."""
        ...

    async def unload(self) -> None:
        """Release resources so another model can become resident."""
        ...

    def generate(
        self,
        messages: Sequence[Message],
        options: GenerationOptions | None = None,
    ) -> AsyncIterator[str]:
        """Yield response chunks as they are produced."""
        ...


async def complete(
    provider: LLMProvider,
    messages: Sequence[Message],
    options: GenerationOptions | None = None,
) -> str:
    """Drain ``generate`` into one string, for callers that want a whole
    answer rather than a stream."""
    parts = [chunk async for chunk in provider.generate(messages, options)]
    return "".join(parts)
