"""The CPU embedders: the deterministic hashing one the retrieval suite runs
against, and the real local encoder behind the env flag."""

from __future__ import annotations

import math
import os

import pytest

from agentchat.llm.embedding import (
    EMBED_BATCH,
    EmbedderInfo,
    HashingEmbedder,
    LocalEmbedder,
)
from agentchat.llm.local import default_models  # noqa: F401 — keep import graph stable

REAL_MODEL_TESTS = os.environ.get("AGENTCHAT_TEST_REAL_MODEL", "").strip().lower() in {
    "1",
    "true",
    "yes",
}


def _cos(a, b) -> float:
    return sum(x * y for x, y in zip(a, b))


def _norm(v) -> float:
    return math.sqrt(sum(x * x for x in v))


# -- HashingEmbedder ------------------------------------------------------


async def test_hashing_is_deterministic_across_instances_and_processes():
    a = await HashingEmbedder().embed(["we run Postgres 14 in production"])
    b = await HashingEmbedder().embed(["we run Postgres 14 in production"])
    assert a == b
    # Hard-coded: `hash()` is salted per process, so a written vector must
    # still match a query written by the next run.
    assert a[0][:3] == [0.0, 0.0, pytest.approx(0.5773502691896258)]


async def test_every_hashing_vector_has_unit_norm():
    vectors = await HashingEmbedder().embed(
        ["one two three four", "Postgres 14", "a single longword"]
    )
    for v in vectors:
        assert _norm(v) == pytest.approx(1.0)


async def test_embed_empty_list_is_empty():
    assert await HashingEmbedder().embed([]) == []


async def test_shared_tokens_score_above_unshared():
    (q, near, far) = await HashingEmbedder().embed(
        [
            "we run Postgres in production",
            "the database is Postgres",
            "the cat sat on the mat",
        ]
    )
    assert _cos(q, near) > _cos(q, far)


async def test_batch_boundary_returns_every_vector_in_order():
    texts = [f"distinct token{i} here" for i in range(EMBED_BATCH + 1)]
    vectors = await HashingEmbedder().embed(texts)
    assert len(vectors) == EMBED_BATCH + 1
    # Re-embedding one at a time must line up with the batched result.
    for text, vector in zip(texts, vectors):
        assert (await HashingEmbedder().embed([text]))[0] == vector


# -- LocalEmbedder (real weights) ---------------------------------------


@pytest.mark.skipif(
    not REAL_MODEL_TESTS,
    reason="set AGENTCHAT_TEST_REAL_MODEL=1 to load the embedding checkpoint",
)
async def test_local_embedder_dimensions_norm_and_similarity():
    from agentchat.config import Settings

    settings = Settings()
    path = settings.embed_model_path
    embedder = LocalEmbedder(EmbedderInfo(id=settings.embed_model, name="local"), path=path)

    vectors = await embedder.embed(
        [
            "we run Postgres in production",
            "the database is Postgres",
            "the cat sat on the mat",
        ]
    )
    assert len(vectors[0]) == 384
    for v in vectors:
        assert _norm(v) == pytest.approx(1.0, abs=1e-3)
    assert _cos(vectors[0], vectors[1]) > _cos(vectors[0], vectors[2])
