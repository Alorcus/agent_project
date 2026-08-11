"""The embedding lifecycle (§ 9): the byte format vectors are stored in, the
incremental repair pass, and the startup guard that makes a silent recall
failure visible.

Pure over the data passed in, like `rank.py` — no store type, no chat type
(I-6). `reembed` takes a `store` duck-typed to `fragments_needing_embedding`
and `apply` rather than importing `MemoryStore`, so this module names nothing
from `agentchat.storage`.
"""

from __future__ import annotations

import struct
from collections.abc import Sequence
from dataclasses import dataclass, replace
from logging import Logger

from .models import MemoryFragment
from .store import FragmentWrite
from .tuning import Tuning
from .types import EmbeddingProvider


def pack(vector: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def unpack(blob: bytes | None) -> tuple[float, ...] | None:
    if not blob or len(blob) % 4 != 0:
        return None
    return struct.unpack(f"<{len(blob) // 4}f", blob)


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


@dataclass(frozen=True)
class EmbeddingAudit:
    total: int
    usable: int
    missing: int
    stale: int


def check_encoder(
    audit: EmbeddingAudit, *, encoder_id: str | None, tuning: Tuning, log: Logger | None = None
) -> list[str]:
    """Warnings for a startup log, in § 5's order. The first three are
    mutually exclusive — one message about the pin, not three; the corpus
    warning is independent and can accompany any of them."""
    warnings: list[str] = []

    if encoder_id is None:
        warnings.append(
            f"no encoder loaded — recall is disabled, making {audit.total} "
            "fragment(s) unreachable"
        )
    elif not tuning.recall_floor_model:
        warnings.append(
            f"RECALL_FLOOR_MODEL is unset — RECALL_FLOOR was never calibrated "
            f"against {encoder_id!r}, the encoder in force"
        )
    elif tuning.recall_floor_model != encoder_id:
        warnings.append(
            f"RECALL_FLOOR_MODEL is {tuning.recall_floor_model!r} but the encoder "
            f"in force is {encoder_id!r} — the floor means something else against it"
        )

    if audit.stale or audit.missing:
        warnings.append(
            f"{audit.stale} stale and {audit.missing} missing embedding(s) are "
            "invisible to recall until re-embedded"
        )

    if log is not None:
        for message in warnings:
            log.warning(message)
    return warnings


def reembed(store, encoder: EmbeddingProvider, *, tuning: Tuning | None = None) -> int:
    """One bounded pass: up to `REEMBED_LIMIT` fragments whose `embedding_model`
    is null or not `encoder.model_id`, encoded in `EMBED_BATCH` batches and
    written back as `embedding_only` — never a revision (§ 2's contract)."""
    tuning = tuning or Tuning.from_env()
    fragments = store.fragments_needing_embedding(encoder.model_id, tuning.reembed_limit)
    if not fragments:
        return 0

    writes: list[FragmentWrite] = []
    for start in range(0, len(fragments), tuning.embed_batch):
        batch = fragments[start : start + tuning.embed_batch]
        vectors = encoder.encode([fragment.text for fragment in batch])
        for fragment, vector in zip(batch, vectors):
            updated: MemoryFragment = replace(
                fragment, embedding=pack(vector), embedding_model=encoder.model_id
            )
            writes.append(FragmentWrite(fragment=updated, embedding_only=True))

    store.apply(writes)
    return len(fragments)
