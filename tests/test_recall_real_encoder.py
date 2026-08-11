"""The encoder check for stage 3: real weights, on the CPU, one load.

Every other recall test scripts its vectors (skeleton rule 7's spirit: the
ranking code is what is under test, not an encoder's opinion), which proves
nothing about whether the pinned encoder loads from disk, produces vectors of
the shape `pack` expects, or separates a related pair from an unrelated one at
all. That question needs the weights, so it lives here, behind
`AGENTCHAT_TEST_REAL_ENCODER=1`:

    AGENTCHAT_TEST_REAL_ENCODER=1 uv run pytest -k real_encoder

Its own flag rather than `AGENTCHAT_TEST_REAL_MODEL`, because these need no GPU
and no cluster node — a laptop with the encoder on disk can run them, and that
is the machine the mock gate is for.

These assert **mechanics**, never quality: that the related pair scores above
the unrelated one, not by how much, and not that the encoder is good. The one
number they do check is `RECALL_FLOOR`, which § 6.1 says is meaningless without
naming the encoder it was set against — a failure there means the default in
§ 9 is wrong for the pinned model, which is exactly what this file is for.

The flag is read at import, like `test_extraction_real_model.py`'s: `conftest`
scrubs every `AGENTCHAT_*` variable per test, so reading it in a body would
always see it unset.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REAL_ENCODER_TESTS = os.environ.get("AGENTCHAT_TEST_REAL_ENCODER", "").strip().lower() in {
    "1",
    "true",
    "yes",
}

pytestmark = pytest.mark.skipif(
    not REAL_ENCODER_TESTS,
    reason="set AGENTCHAT_TEST_REAL_ENCODER=1 to load the pinned encoder from disk",
)

#: Fixed, so a failure is about the encoder and not about what was said this
#: time. The related pair shares a subject with `CLAIM`; the unrelated one
#: shares nothing but the language.
CLAIM = "This project stores every conversation in SQLite, in WAL mode."
RELATED = "Which database did we choose for storage?"
UNRELATED = "Kyoto is mild in October and the maples turn late."


def loaded_encoder():
    from agentchat.config import Settings, build_encoder

    encoder = build_encoder(Settings.from_env())
    if encoder is None:
        pytest.fail(
            "no encoder could be built — set AGENTCHAT_ENCODER_PATH (or "
            "AGENTCHAT_MODEL_ROOT) to where the pinned weights live"
        )
    return encoder


def test_the_encoder_reports_the_id_the_floor_is_pinned_to():
    from agentchat.core.memory.tuning import Tuning

    assert loaded_encoder().model_id == Tuning.from_env().recall_floor_model


def test_encoding_returns_one_unit_vector_per_text():
    vectors = loaded_encoder().encode([CLAIM, RELATED])

    assert len(vectors) == 2
    assert len({len(v) for v in vectors}) == 1, "one dimension for the whole corpus"
    for vector in vectors:
        norm = sum(x * x for x in vector) ** 0.5
        assert abs(norm - 1.0) < 1e-3, "cosine is a dot product only if these are unit vectors"


def test_a_related_pair_scores_above_an_unrelated_one():
    from agentchat.core.memory.embed import cosine

    claim, related, unrelated = loaded_encoder().encode([CLAIM, RELATED, UNRELATED])

    assert cosine(claim, related) > cosine(claim, unrelated)


def test_the_section_9_floor_sits_between_the_two():
    """The one calibration check in the suite. `RECALL_FLOOR` is
    embedding-model specific (§ 6.1), so its default is only meaningful against
    the pinned encoder — a failure here says § 9's number needs re-tuning, not
    that the code is broken."""
    from agentchat.core.memory.embed import cosine
    from agentchat.core.memory.tuning import Tuning

    floor = Tuning.from_env().recall_floor
    claim, related, unrelated = loaded_encoder().encode([CLAIM, RELATED, UNRELATED])

    assert cosine(claim, related) >= floor, f"the floor {floor} rejects a plain topical match"
    assert cosine(claim, unrelated) < floor, f"the floor {floor} admits an unrelated sentence"


def test_the_round_trip_through_the_store_admits_only_the_related_query(tmp_path: Path):
    """End to end on the real encoder: embed at write time, recall at read
    time, with the floor deciding membership."""
    from agentchat.core.memory.embed import pack
    from agentchat.storage.memory import SqliteMemoryStore

    from factories import GraphBuilder, make_group, write

    encoder = loaded_encoder()
    store = SqliteMemoryStore(tmp_path / "chat.db", encoder=encoder)
    builder = GraphBuilder(make_group(kind="project"))
    chat = builder.conversation()
    (vector,) = encoder.encode([CLAIM])
    builder.extracted(
        builder.message(chat, content=CLAIM),
        text=CLAIM,
        embedding=pack(vector),
        embedding_model=encoder.model_id,
    )
    write(store, builder.build())

    assert store.select(builder.group.id, RELATED, 512) != []
    assert store.select(builder.group.id, UNRELATED, 512) == []
