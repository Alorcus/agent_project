"""Tests for the § 9 tuning surface.

Imports of ``agentchat`` live inside the test bodies to match the rest of the
stage-0 suite, which has to stay collectable before ``core/memory/`` exists.
"""

from __future__ import annotations

import logging
import os

import pytest

#: § 9's table, transcribed. The point of the test is the second copy.
DEFAULTS = {
    "extract_every": 6,
    "reinforce_step": 0.1,
    "candidate_pool_k": 10,
    "extract_max_tokens": 2048,
    "extract_temperature": 0.0,
    "quote_max_chars": 240,
    "extract_close_timeout": 30.0,
    "claim_importance_default": 5.0,
    "claim_confidence_default": 0.5,
    "recall_floor": 0.35,
    "recall_floor_model": "sentence-transformers/all-MiniLM-L6-v2",
    "encoder_model": "sentence-transformers/all-MiniLM-L6-v2",
    "embed_batch": 16,
    "reembed_limit": 64,
    "recall_pool_k": 200,
    "rrf_k": 60,
    "decay_k": 2,
    "alpha_decision": 0.01,
    "alpha_constraint": 0.01,
    "alpha_fact": 0.03,
    "alpha_artefact": 0.03,
    "alpha_open_question": 0.15,
    "mmr_lambda": 0.5,
    "core_budget_fraction": 0.10,
    "sel_budget_fraction": 0.10,
    "consolidate_threshold": 40,
    "consolidate_questions": 2,
    "consolidate_insights": 3,
    "consolidate_recent_n": 30,
}


def clean_env(monkeypatch) -> None:
    for name in list(os.environ):
        if name.startswith("AGENTCHAT_MEMORY_"):
            monkeypatch.delenv(name)


def test_defaults_match_the_section_9_table(monkeypatch):
    from agentchat.core.memory.tuning import Tuning

    clean_env(monkeypatch)
    tuning = Tuning.from_env()

    assert {name: getattr(tuning, name) for name in DEFAULTS} == DEFAULTS


def test_env_override_applies_to_one_constant_only(monkeypatch):
    from agentchat.core.memory.tuning import Tuning

    clean_env(monkeypatch)
    monkeypatch.setenv("AGENTCHAT_MEMORY_RECALL_FLOOR", "0.5")

    tuning = Tuning.from_env()

    assert tuning.recall_floor == 0.5
    others = {name: value for name, value in DEFAULTS.items() if name != "recall_floor"}
    assert {name: getattr(tuning, name) for name in others} == others


def test_bad_override_raises_naming_the_variable(monkeypatch):
    from agentchat.core.errors import TuningError
    from agentchat.core.memory.tuning import Tuning

    clean_env(monkeypatch)
    monkeypatch.setenv("AGENTCHAT_MEMORY_EXTRACT_EVERY", "abc")

    with pytest.raises(TuningError) as excinfo:
        Tuning.from_env()

    message = str(excinfo.value)
    assert "AGENTCHAT_MEMORY_EXTRACT_EVERY" in message
    assert "abc" in message


def test_alpha_resolves_per_kind(monkeypatch):
    from agentchat.core.memory.tuning import Tuning

    clean_env(monkeypatch)
    tuning = Tuning.from_env()

    assert tuning.alpha_for("decision") == 0.01
    assert tuning.alpha_for("fact") == 0.03
    assert tuning.alpha_for("open_question") == 0.15


def test_unknown_kind_falls_back_to_alpha_fact(monkeypatch):
    from agentchat.core.memory.tuning import Tuning

    clean_env(monkeypatch)
    tuning = Tuning.from_env()

    assert tuning.alpha_for("persona_trait") == tuning.alpha_fact


def test_catalogue_and_dataclass_agree(monkeypatch):
    from dataclasses import fields

    from agentchat.core.memory.tuning import CATALOGUE, Tuning

    clean_env(monkeypatch)

    assert {c.field for c in CATALOGUE} == {f.name for f in fields(Tuning)}


def test_no_constant_is_marked_tuned():
    from agentchat.core.memory.tuning import CATALOGUE

    assert [c.name for c in CATALOGUE if c.status == "tuned"] == []


def test_log_effective_tuning_emits_every_constant_with_status(monkeypatch, caplog):
    from agentchat.core.memory.tuning import CATALOGUE, Tuning, log_effective_tuning

    clean_env(monkeypatch)
    logger = logging.getLogger("test.memory.tuning")

    with caplog.at_level(logging.INFO, logger=logger.name):
        log_effective_tuning(Tuning.from_env(), logger)

    records = [r.getMessage() for r in caplog.records if r.name == logger.name]

    assert len(records) == len(CATALOGUE)
    for constant, value in Tuning.from_env().as_rows():
        assert any(
            constant.name in line and str(value) in line and constant.status in line
            for line in records
        ), f"no record for {constant.name}"
