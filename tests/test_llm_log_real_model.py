"""Opt-in exactness checks that only real weights can prove: that
`prompt_text` tokenizes to exactly the ids the model was given, and that the
logged output carries special tokens the streamed UI text does not — the
concrete evidence that the transcript is taken below the streamer's cleanup
(KTD1/KTD2), not a description of it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agentchat.config import Settings
from agentchat.core.models import Message
from agentchat.llm import transcript
from agentchat.llm.base import GenerationOptions
from agentchat.llm.local import TransformersProvider, default_models

REAL_MODEL_TESTS = os.environ.get("AGENTCHAT_TEST_REAL_MODEL", "").strip().lower() in {
    "1",
    "true",
    "yes",
}

pytestmark = pytest.mark.skipif(
    not REAL_MODEL_TESTS,
    reason="set AGENTCHAT_TEST_REAL_MODEL=1 to load real weights on a GPU node",
)


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


async def test_prompt_text_tokenizes_to_the_exact_ids_the_model_saw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    settings = Settings(log_llm_io=True, log_dir=tmp_path / "logs")
    path = transcript.setup_llm_log(settings)

    info, kwargs = default_models()[0]  # Phi-4-mini: the cheap one to load
    instance = TransformersProvider(info, **kwargs)
    await instance.load()

    tokenizer_cls = type(instance._tokenizer)
    original_call = tokenizer_cls.__call__
    captured_ids: list[int] = []

    def spy(self, text, *args, **kwargs):
        result = original_call(self, text, *args, **kwargs)
        if isinstance(text, str):
            captured_ids.extend(result["input_ids"][0].tolist())
        return result

    monkeypatch.setattr(tokenizer_cls, "__call__", spy)
    try:
        [
            c
            async for c in instance.generate(
                [Message(role="user", content="Say hello in one word.")],
                GenerationOptions(max_tokens=16, temperature=0.0),
            )
        ]

        request = next(r for r in _records(path) if r["type"] == "request")
        reencoded = original_call(
            instance._tokenizer, request["prompt_text"], add_special_tokens=False
        )["input_ids"][0].tolist()
        assert reencoded == captured_ids
    finally:
        await instance.unload()


async def test_logged_output_contains_special_tokens_the_stream_does_not(tmp_path: Path):
    settings = Settings(log_llm_io=True, log_dir=tmp_path / "logs")
    path = transcript.setup_llm_log(settings)

    info, kwargs = default_models()[0]
    instance = TransformersProvider(info, **kwargs)
    await instance.load()
    special_tokens = list(instance._tokenizer.all_special_tokens)
    try:
        chunks = [
            c
            async for c in instance.generate(
                [Message(role="user", content="Say hello in one word.")],
                GenerationOptions(max_tokens=16, temperature=0.0),
            )
        ]
    finally:
        await instance.unload()

    streamed = "".join(chunks)
    response = next(r for r in _records(path) if r["type"] == "response")

    assert not any(token in streamed for token in special_tokens)
    assert any(token in response["output"] for token in special_tokens)
