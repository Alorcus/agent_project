"""The LLM I/O transcript: request/response pairing, verbatim content, the
switch that makes it inert, and that a broken handler cannot break a turn."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest

from agentchat.config import Settings, build_registry
from agentchat.core.chat import ChatService
from agentchat.core.extraction import ExtractionService
from agentchat.core.models import Message
from agentchat.llm import transcript
from agentchat.llm.base import ModelInfo
from agentchat.llm.local import TransformersProvider
from conftest import mock_settings
from factories import make_conversation


def _settings(tmp_path: Path, **overrides) -> Settings:
    overrides.setdefault("log_llm_io", True)
    overrides.setdefault("log_dir", tmp_path / "logs")
    return mock_settings(**overrides)


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


# -- pairing / verbatim content -------------------------------------------


async def test_one_turn_writes_exactly_one_paired_request_and_response(tmp_path: Path):
    settings = _settings(tmp_path)
    path = transcript.setup_llm_log(settings)
    registry = build_registry(settings)

    provider = await registry.active_provider()
    [c async for c in provider.generate([Message(role="user", content="hi")])]

    records = _records(path)
    assert len(records) == 2
    request, response = records
    assert request["type"] == "request"
    assert response["type"] == "response"
    assert request["call_id"] == response["call_id"]


async def test_request_messages_match_exactly_what_generate_received(tmp_path: Path):
    settings = _settings(tmp_path)
    path = transcript.setup_llm_log(settings)
    registry = build_registry(settings)
    provider = await registry.active_provider()

    messages = [
        Message(role="system", content="be terse"),
        Message(role="user", content="why is the sky blue?"),
    ]
    [c async for c in provider.generate(messages)]

    request = _records(path)[0]
    assert request["messages"] == [{"role": m.role, "content": m.content} for m in messages]


async def test_response_output_matches_the_concatenated_stream(tmp_path: Path):
    settings = _settings(tmp_path)
    path = transcript.setup_llm_log(settings)
    registry = build_registry(settings)
    provider = await registry.active_provider()

    chunks = [c async for c in provider.generate([Message(role="user", content="hi")])]

    response = _records(path)[1]
    assert response["output"] == "".join(chunks)
    assert response["outcome"] == "complete"


async def test_large_multiline_message_round_trips_on_one_physical_line(tmp_path: Path):
    settings = _settings(tmp_path)
    path = transcript.setup_llm_log(settings)
    registry = build_registry(settings)
    provider = await registry.active_provider()

    # Newlines inside the content, at a size no accidental truncation would
    # survive — both R4 (no truncation) and KTD8 (one line per record) at once.
    content = "first line\nsecond line\n" * 5_000
    [c async for c in provider.generate([Message(role="user", content=content)])]

    lines = path.read_text().splitlines()
    assert len(lines) == 2
    request = json.loads(lines[0])
    assert request["messages"][0]["content"] == content


# -- cancellation / failure -------------------------------------------------


async def test_cancel_mid_stream_writes_a_stopped_response(tmp_path: Path, store):
    # Needs a real mid-generation gap to cancel into.
    settings = _settings(tmp_path, mock_chunk_delay=None, mock_load_delay=None)
    path = transcript.setup_llm_log(settings)
    registry = build_registry(settings)
    chat = ChatService(registry, store=store)
    conversation = await chat.new_conversation()

    async def consume():
        async for _ in chat.stream_reply(conversation, "hello"):
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    response = next(r for r in _records(path) if r["type"] == "response")
    assert response["outcome"] == "stopped"
    assert response["output"]
    assert response["output"] == conversation.messages[-1].content


def test_local_backend_records_error_outcome_without_a_gpu(tmp_path: Path):
    """`_record_completion` is exercised directly — no checkpoint or model
    load needed, since a caught generation error never produces `sequences`."""
    settings = _settings(tmp_path)
    path = transcript.setup_llm_log(settings)

    info = ModelInfo(id="t", name="T", context_window=2048)
    instance = TransformersProvider(info, path=tmp_path / "nonexistent")
    error = RuntimeError("boom")
    instance._record_completion(
        sequences=None, state={"error": error}, prompt_len=0,
        call_id="abc123", label="chat", metered=None,
    )

    records = _records(path)
    assert len(records) == 1
    record = records[0]
    assert record["type"] == "response"
    assert record["outcome"] == "error"
    assert record["output"] == ""
    assert "boom" in record["error"]


# -- labels -------------------------------------------------------------


async def test_chat_turn_is_labeled_chat(tmp_path: Path, store):
    settings = _settings(tmp_path)
    path = transcript.setup_llm_log(settings)
    registry = build_registry(settings)
    chat = ChatService(registry, store=store)
    conversation = await chat.new_conversation()

    [c async for c in chat.stream_reply(conversation, "hi")]

    records = _records(path)
    assert records
    assert all(r["label"] == "chat" for r in records)


async def test_extraction_calls_are_labeled_summary_then_keywords(tmp_path: Path):
    settings = _settings(tmp_path, extract_summaries=True)
    path = transcript.setup_llm_log(settings)
    registry = build_registry(settings)
    extractor = ExtractionService(registry)

    await extractor.run(make_conversation())

    requests = [r for r in _records(path) if r["type"] == "request"]
    assert [r["label"] for r in requests] == ["extraction.summary", "extraction.keywords"]


async def test_a_call_outside_any_label_is_recorded_as_unknown(tmp_path: Path):
    settings = _settings(tmp_path)
    path = transcript.setup_llm_log(settings)
    registry = build_registry(settings)
    provider = await registry.active_provider()

    [c async for c in provider.generate([Message(role="user", content="hi")])]

    assert all(r["label"] == "unknown" for r in _records(path))


# -- the switch -----------------------------------------------------------


async def test_disabled_creates_no_file_and_builds_no_record(tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path, log_llm_io=False)
    result = transcript.setup_llm_log(settings)
    assert result is None
    assert not transcript.is_enabled()

    monkeypatch.setattr(
        transcript, "_emit", lambda *a, **k: pytest.fail("a disabled logger built a record")
    )
    registry = build_registry(settings)
    provider = await registry.active_provider()
    [c async for c in provider.generate([Message(role="user", content="hi")])]

    assert not (tmp_path / "logs" / "llm.jsonl").exists()


async def test_unconfigured_process_writes_nothing_and_is_silent(capsys):
    assert not transcript.is_enabled()

    from conftest import fast_registry

    registry = fast_registry()
    provider = await registry.active_provider()
    chunks = [c async for c in provider.generate([Message(role="user", content="hi")])]

    assert chunks
    assert capsys.readouterr().err == ""


def test_setup_llm_log_is_idempotent(tmp_path: Path):
    settings = _settings(tmp_path)
    path1 = transcript.setup_llm_log(settings)
    path2 = transcript.setup_llm_log(settings)

    assert path1 == path2
    file_handlers = [h for h in transcript._log.handlers if isinstance(h, logging.FileHandler)]
    assert len(file_handlers) == 1


async def test_transcript_write_failure_does_not_break_a_turn(
    tmp_path: Path, store, monkeypatch
):
    settings = _settings(tmp_path)
    transcript.setup_llm_log(settings)

    def _boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(transcript._log, "info", _boom)

    registry = build_registry(settings)
    chat = ChatService(registry, store=store)
    conversation = await chat.new_conversation()

    chunks = [c async for c in chat.stream_reply(conversation, "hi")]

    assert chunks
    assert conversation.messages[-1].content == "".join(chunks)
