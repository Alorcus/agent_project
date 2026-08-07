"""Model registry — the single place that decides which model is resident.

Only one base model is kept loaded at a time, so the app fits both a laptop
and the cluster.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator

from agentchat.core.errors import ModelNotFoundError
from agentchat.llm.base import LLMProvider, ModelInfo

ProviderFactory = Callable[[], LLMProvider]


class ModelRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, ProviderFactory] = {}
        self._infos: dict[str, ModelInfo] = {}
        self._instances: dict[str, LLMProvider] = {}
        self._active_id: str | None = None
        self._lock = asyncio.Lock()

    # -- registration -----------------------------------------------------

    def register(self, info: ModelInfo, factory: ProviderFactory) -> None:
        self._infos[info.id] = info
        self._factories[info.id] = factory
        if self._active_id is None:
            self._active_id = info.id

    def available(self) -> list[ModelInfo]:
        return list(self._infos.values())

    def info(self, model_id: str) -> ModelInfo:
        try:
            return self._infos[model_id]
        except KeyError:
            raise ModelNotFoundError(f"Unknown model: {model_id!r}") from None

    # -- active model -----------------------------------------------------

    @property
    def active_id(self) -> str | None:
        return self._active_id

    @property
    def active_info(self) -> ModelInfo | None:
        return self._infos.get(self._active_id) if self._active_id else None

    def cycle(self) -> ModelInfo:
        """Advance to the next registered model. Backs the quick-switch key."""
        ids = list(self._infos)
        if not ids:
            raise ModelNotFoundError("No models registered")
        idx = ids.index(self._active_id) if self._active_id in ids else -1
        self._active_id = ids[(idx + 1) % len(ids)]
        return self._infos[self._active_id]

    def set_active(self, model_id: str) -> ModelInfo:
        """Select a model. Cheap and synchronous — loading is deferred to
        ``active_provider`` so the UI can show progress around it."""
        info = self.info(model_id)
        self._active_id = model_id
        return info

    async def active_provider(self) -> LLMProvider:
        """Return the loaded provider for the active model, loading it and
        evicting the previous one if needed."""
        if self._active_id is None:
            raise ModelNotFoundError("No models registered")
        async with self._lock:
            model_id = self._active_id
            provider = self._instances.get(model_id)
            if provider is None:
                provider = self._factories[model_id]()
                self._instances[model_id] = provider
            if not provider.is_loaded:
                await self._evict_others(model_id)
                await provider.load()
            return provider

    async def _evict_others(self, keep_id: str) -> None:
        for other_id, other in self._instances.items():
            if other_id != keep_id and other.is_loaded:
                await other.unload()

    async def shutdown(self) -> None:
        for provider in self._instances.values():
            if provider.is_loaded:
                await provider.unload()

    def __iter__(self) -> Iterator[ModelInfo]:
        return iter(self._infos.values())
