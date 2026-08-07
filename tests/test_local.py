"""Tests for the real-weights backend.

Everything here runs without a GPU and without touching the checkpoints, except
the opt-in integration test at the bottom. The point is that the parts which can
be wrong *silently* — where weights are read from, whether the thinking toggle
reaches the model, what the context window ends up being — are checked cheaply.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentchat.config import ConfigurationError, Settings, build_registry
from agentchat.core.errors import ProviderError
from agentchat.core.models import Message
from agentchat.llm.base import GenerationOptions, ModelInfo
from agentchat.llm.local import (
    DEFAULT_MODEL_ROOT,
    TransformersProvider,
    default_models,
)

REAL_MODEL_TESTS = os.environ.get("AGENTCHAT_TEST_REAL_MODEL", "").strip().lower() in {
    "1",
    "true",
    "yes",
}


class FakeTokenizer:
    """Records what ``_render_prompt`` asks of a tokenizer."""

    chat_template = ""
    pad_token_id = 0
    eos_token_id = 1

    def __init__(self) -> None:
        self.calls: list[tuple[list[dict[str, str]], dict]] = []

    def apply_chat_template(self, payload, **kwargs):
        self.calls.append((payload, kwargs))
        return "".join(f"<{m['role']}>{m['content']}" for m in payload) + "<assistant>"


def provider(**overrides) -> TransformersProvider:
    info = overrides.pop("info", ModelInfo(id="t", name="T", context_window=2048))
    path = overrides.pop("path", Path("/nonexistent/checkpoint"))
    return TransformersProvider(info, path=path, **overrides)


# -- catalogue ------------------------------------------------------------


def test_catalogue_offers_the_two_cluster_models():
    catalogue = default_models()
    assert [info.id for info, _ in catalogue] == ["phi-4-mini", "qwen3-14b"]


def test_checkpoints_are_addressed_by_path_not_by_repo_id():
    """A repo id would send transformers to the hub and download the weights
    into the user's home cache. Every entry must be an absolute local path."""
    for _, kwargs in default_models():
        path = kwargs["path"]
        assert isinstance(path, Path)
        assert path.is_absolute()
        assert path.is_relative_to(DEFAULT_MODEL_ROOT)


def test_model_root_is_relocatable(tmp_path: Path):
    for _, kwargs in default_models(tmp_path):
        assert kwargs["path"].is_relative_to(tmp_path)


# -- wiring ---------------------------------------------------------------


def test_local_backend_is_the_default():
    assert Settings().backend == "local"


def test_registry_wires_the_local_backend(tmp_path: Path):
    registry = build_registry(Settings(backend="local", model_root=tmp_path))
    assert [info.id for info in registry.available()] == ["phi-4-mini", "qwen3-14b"]


def test_unknown_backend_is_a_configuration_error():
    with pytest.raises(ConfigurationError):
        build_registry(Settings(backend="vllm"))


def test_max_context_caps_both_the_registry_and_the_provider(tmp_path: Path):
    """The chat service sizes context off the *provider's* info, so a cap that
    only reached the registry's copy would silently do nothing (NFR-CTX-04)."""
    registry = build_registry(
        Settings(backend="local", model_root=tmp_path, max_context=4096)
    )
    assert all(info.context_window == 4096 for info in registry.available())

    instance = registry._factories["qwen3-14b"]()
    assert instance.info.context_window == 4096


def test_max_context_does_not_widen_a_narrow_window(tmp_path: Path):
    registry = build_registry(
        Settings(backend="local", model_root=tmp_path, max_context=999_999)
    )
    assert registry.info("qwen3-14b").context_window == 16384


# -- failure modes --------------------------------------------------------


async def test_simulated_failure_is_a_provider_error(tmp_path: Path):
    registry = build_registry(
        Settings(backend="local", model_root=tmp_path, simulate_failure=True)
    )
    registry.cycle()  # the second model is the one made to fail
    with pytest.raises(ProviderError):
        await registry.active_provider()


async def test_missing_checkpoint_is_a_provider_error_not_a_crash():
    with pytest.raises(ProviderError, match="no checkpoint"):
        await provider().load()


async def test_generation_before_load_is_refused():
    with pytest.raises(ProviderError):
        [chunk async for chunk in provider().generate([])]


# -- prompt construction --------------------------------------------------


def test_thinking_reaches_a_template_that_supports_it():
    instance = provider()
    instance._tokenizer = FakeTokenizer()
    instance._supports_thinking = True

    instance._render_prompt(
        [Message(role="user", content="hi")], GenerationOptions(thinking=True)
    )
    payload, kwargs = instance._tokenizer.calls[-1]
    assert kwargs["enable_thinking"] is True
    assert kwargs["add_generation_prompt"] is True
    assert [m["role"] for m in payload] == ["user"]


def test_thinking_off_is_passed_explicitly():
    """Qwen3 reasons by default; leaving the flag out would make the toggle
    one-way."""
    instance = provider()
    instance._tokenizer = FakeTokenizer()
    instance._supports_thinking = True

    instance._render_prompt([Message(role="user", content="hi")], GenerationOptions())
    _, kwargs = instance._tokenizer.calls[-1]
    assert kwargs["enable_thinking"] is False


def test_thinking_degrades_to_an_instruction_without_template_support():
    instance = provider()
    instance._tokenizer = FakeTokenizer()
    instance._supports_thinking = False

    instance._render_prompt(
        [Message(role="user", content="hi")], GenerationOptions(thinking=True)
    )
    payload, kwargs = instance._tokenizer.calls[-1]
    assert "enable_thinking" not in kwargs
    assert payload[0]["role"] == "system"
    assert "step by step" in payload[0]["content"]


def test_thinking_instruction_folds_into_an_existing_system_turn():
    instance = provider()
    instance._tokenizer = FakeTokenizer()
    instance._supports_thinking = False

    instance._render_prompt(
        [
            Message(role="system", content="You are terse."),
            Message(role="user", content="hi"),
        ],
        GenerationOptions(thinking=True),
    )
    payload, _ = instance._tokenizer.calls[-1]
    assert [m["role"] for m in payload] == ["system", "user"]
    assert payload[0]["content"].startswith("You are terse.")


def test_empty_turns_are_not_sent_to_the_model():
    instance = provider()
    instance._tokenizer = FakeTokenizer()

    instance._render_prompt(
        [
            Message(role="user", content="hi"),
            Message(role="assistant", content="   "),
        ],
        GenerationOptions(),
    )
    payload, _ = instance._tokenizer.calls[-1]
    assert [m["role"] for m in payload] == ["user"]


# -- opt-in: the real thing ----------------------------------------------


@pytest.mark.skipif(
    not REAL_MODEL_TESTS,
    reason="set AGENTCHAT_TEST_REAL_MODEL=1 to load real weights on a GPU node",
)
async def test_real_model_loads_and_streams():
    info, kwargs = default_models()[0]  # Phi-4-mini: the cheap one to load
    instance = TransformersProvider(info, **kwargs)
    await instance.load()
    try:
        chunks = [
            chunk
            async for chunk in instance.generate(
                [
                    Message(
                        role="user",
                        content="Count from one to five in words, comma separated.",
                    )
                ],
                # Long enough to need several chunks — a one-word answer would
                # arrive as a single chunk and prove nothing about streaming.
                GenerationOptions(max_tokens=64, temperature=0.0),
            )
        ]
        assert len(chunks) > 3, "must stream, not return one blob"
        assert "five" in "".join(chunks).lower()
    finally:
        await instance.unload()
    assert not instance.is_loaded
