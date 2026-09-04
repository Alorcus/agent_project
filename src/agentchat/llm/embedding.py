"""Text embedders for fact retrieval.

Two implementations behind one `Embedder` protocol: `LocalEmbedder` loads a
small sentence encoder from a path (never downloaded), mirroring
`TransformersProvider`'s lazy load; `HashingEmbedder` is weightless and
deterministic across processes — what the retrieval suite searches against.

An encoder is not a generator: it takes no `GenerationOptions`, records no
transcript, and reports no usage. It is deliberately not wired into
`core.usage` — the context meter measures the reply's window, and a forward
pass over a fact has nothing to do with it.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from agentchat.core.anchoring import significant
from agentchat.core.errors import ProviderError

DEFAULT_EMBED_MODEL_ID = "all-MiniLM-L6-v2"
EMBED_BATCH = 32
#: Past this a fact is not a fact; the cap is a guard against a pathological
#: row, not a truncation policy.
MAX_EMBED_CHARS = 2000


@dataclass(frozen=True)
class EmbedderInfo:
    id: str
    name: str


@runtime_checkable
class Embedder(Protocol):
    @property
    def info(self) -> EmbedderInfo: ...

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """One L2-normalised vector per input, in order. `[]` for `[]`."""
        ...


def _l2_normalise(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    if norm == 0.0:
        return vector
    return [x / norm for x in vector]


class HashingEmbedder:
    """Deterministic, weightless, and similar for texts that share words —
    what the retrieval suite searches against.

    Each significant token (see `anchoring.significant`) is folded into a
    bucket by a process-independent hash and the bucket vector is
    L2-normalised. `hashlib`, not `hash()` — `hash()` is salted per process,
    so a vector written by one run would not match a query in the next.
    """

    def __init__(self, info: EmbedderInfo | None = None, *, dim: int = 64) -> None:
        self._info = info or EmbedderInfo(id="hashing-64", name="Hashing embedder")
        self._dim = dim

    @property
    def info(self) -> EmbedderInfo:
        return self._info

    @property
    def dim(self) -> int:
        return self._dim

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self._dim
        for token in significant(text[:MAX_EMBED_CHARS]):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            vector[int.from_bytes(digest, "big") % self._dim] += 1.0
        return _l2_normalise(vector)


class LocalEmbedder:
    """A CPU sentence encoder loaded from `path` with `local_files_only=True`,
    mean-pooled over the attention mask and L2-normalised."""

    def __init__(self, info: EmbedderInfo, *, path: Path) -> None:
        self._info = info
        self._path = Path(path)
        self._tokenizer: Any = None
        self._model: Any = None
        self._loaded = False
        # A background ingest and a turn's `ensure_indexed` both call `embed`;
        # without this both see `_loaded == False` and load the encoder twice.
        self._load_lock = asyncio.Lock()

    @property
    def info(self) -> EmbedderInfo:
        return self._info

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        if not self._loaded:
            async with self._load_lock:
                if not self._loaded:
                    await asyncio.to_thread(self._load_blocking)
        out: list[list[float]] = []
        batch: list[str] = []
        for text in texts:
            batch.append(text[:MAX_EMBED_CHARS])
            if len(batch) == EMBED_BATCH:
                out.extend(await asyncio.to_thread(self._embed_batch, batch))
                batch = []
        if batch:
            out.extend(await asyncio.to_thread(self._embed_batch, batch))
        return out

    def _load_blocking(self) -> None:
        if not self._path.is_dir():
            raise ProviderError(
                f"{self._info.name}: no checkpoint at {self._path}. "
                "Set AGENTCHAT_EMBED_MODEL_PATH to where the weights live."
            )
        try:
            import torch  # noqa: F401
            from transformers import AutoModel, AutoTokenizer
        except ImportError as error:
            raise ProviderError(
                f"{self._info.name} needs torch and transformers. "
                "Run with AGENTCHAT_BACKEND=mock to use the hashing embedder."
            ) from error
        try:
            self._tokenizer = AutoTokenizer.from_pretrained(
                str(self._path), local_files_only=True, trust_remote_code=False
            )
            self._model = AutoModel.from_pretrained(
                str(self._path), local_files_only=True, trust_remote_code=False
            )
        except Exception as error:  # noqa: BLE001 — every load failure is one error
            raise ProviderError(
                f"{self._info.name} failed to load from {self._path}: {error}"
            ) from error
        self._model.eval()
        self._loaded = True

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        import torch

        encoded = self._tokenizer(
            texts, padding=True, truncation=True, return_tensors="pt"
        )
        with torch.inference_mode():
            output = self._model(**encoded)
        hidden = output.last_hidden_state  # (batch, seq, dim)
        mask = encoded["attention_mask"].unsqueeze(-1).to(hidden.dtype)
        # Mean-pool over the attention mask, not the [CLS] token and not an
        # unmasked mean — padding would drag every short fact toward the same
        # vector.
        summed = (hidden * mask).sum(dim=1)
        counts = mask.sum(dim=1).clamp(min=1e-9)
        pooled = summed / counts
        pooled = torch.nn.functional.normalize(pooled, p=2, dim=1)
        return pooled.to(torch.float32).tolist()
