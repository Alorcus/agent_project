"""Opt-in check that the `;`-separated keyword contract survives contact with
the two models this project actually runs — the mock backend cannot tell us
whether a real model answers the keyword prompt in a parsable line rather
than a polite paragraph.
"""

from __future__ import annotations

import os

import pytest

from agentchat.core.extraction import ExtractionService
from agentchat.core.models import Conversation, Message
from agentchat.llm.local import TransformersProvider, default_models
from agentchat.llm.registry import ModelRegistry

REAL_MODEL_TESTS = os.environ.get("AGENTCHAT_TEST_REAL_MODEL", "").strip().lower() in {
    "1",
    "true",
    "yes",
}

pytestmark = pytest.mark.skipif(
    not REAL_MODEL_TESTS,
    reason="set AGENTCHAT_TEST_REAL_MODEL=1 to load real weights on a GPU node",
)


def _conversation() -> Conversation:
    return Conversation(
        messages=[
            Message(role="user", content="I want to plan a week in Japan, mostly Kyoto."),
            Message(
                role="assistant",
                content="Kyoto is a great base — temples, gardens, and easy day "
                "trips to Nara and Osaka. Best in spring for cherry blossoms or "
                "autumn for the leaves.",
            ),
            Message(role="user", content="What should I budget for food per day?"),
        ]
    )


async def _run_against(model_id: str) -> None:
    info, kwargs = next(
        (info, kwargs) for info, kwargs in default_models() if info.id == model_id
    )
    registry = ModelRegistry()
    registry.register(info, lambda info=info, kwargs=kwargs: TransformersProvider(info, **kwargs))
    service = ExtractionService(registry)

    summary = await service.run(_conversation())
    print(f"\n[{model_id}] summary: {summary.summary!r}")
    print(f"[{model_id}] keywords: {summary.keywords!r}")

    assert summary.summary
    assert len(summary.summary) < len("".join(m.content for m in _conversation().messages))
    assert 1 <= len(summary.keywords) <= 5

    await registry.shutdown()


async def test_extraction_format_survives_phi_4_mini():
    await _run_against("phi-4-mini")


async def test_extraction_format_survives_qwen3_14b():
    await _run_against("qwen3-14b")
