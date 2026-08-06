"""The boundary between the app and any model backend.

Everything above this line (UI, chat service) talks only to ``LLMProvider``.
Everything below it (mock now; llama.cpp / transformers / adapters later) only
has to satisfy this protocol. Swapping the mock for a real backend must not
touch the UI.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from agentchat.core.models import Message


@dataclass(frozen=True)
class ModelInfo:
    """Static description of a selectable model or adapter.

    ``base_model_id`` being set marks this as a LoRA/QLoRA adapter that rides on
    a shared resident base model rather than its own full copy (NFR-FT-08).
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
    """Per-request knobs.

    ``thinking`` is user-controlled rather than always-on so quality can be
    traded against latency (NFR-U-08).
    """

    temperature: float = 0.7
    max_tokens: int = 1024
    thinking: bool = False
    stop: tuple[str, ...] = field(default_factory=tuple)


@runtime_checkable
class LLMProvider(Protocol):
    """A loadable, streaming text generator.

    Implementations must be cancellable: when the consumer stops iterating
    ``generate`` (task cancellation), generation aborts promptly. The UI's stop
    action depends on it (NFR-U-04).
    """

    @property
    def info(self) -> ModelInfo: ...

    @property
    def is_loaded(self) -> bool: ...

    async def load(self) -> None:
        """Acquire weights/resources. Idempotent. May be slow — the caller is
        expected to show progress (NFR-U-07)."""
        ...

    async def unload(self) -> None:
        """Release resources so another model can become resident (NFR-P-05)."""
        ...

    def generate(
        self,
        messages: Sequence[Message],
        options: GenerationOptions | None = None,
    ) -> AsyncIterator[str]:
        """Yield response chunks as they are produced."""
        ...
