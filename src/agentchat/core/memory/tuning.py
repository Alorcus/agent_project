"""§ 9's tuning surface: every guessed constant from
``docs/plans/memory-and-groups.md`` lives here once, with the status that
says how much it can be trusted.

Two objects, deliberately separate. ``CATALOGUE`` carries what stage 6's
read-only inspector table needs (status badge, origin, design section);
``Tuning`` carries the effective values ``rank.py`` and friends read.

Env parsing is local rather than imported from ``agentchat.config``:
``config`` imports ``storage`` and ``llm``, and ``core`` importing it back
would invert the one-way dependency direction (``ui -> core -> llm/storage``).
Duplicating two small coercion helpers is the cheap side of that trade.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from logging import Logger
from typing import Literal

from agentchat.core.errors import TuningError

Status = Literal["borrowed", "scaled", "guess", "tuned"]

ENV_PREFIX = "AGENTCHAT_MEMORY_"

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class Constant:
    name: str  # RECALL_FLOOR
    field: str  # recall_floor — the Tuning attribute
    default: int | float | str
    status: Status
    origin: str
    section: str  # "§ 6.1"

    @property
    def env_var(self) -> str:
        return f"{ENV_PREFIX}{self.name}"


CATALOGUE: tuple[Constant, ...] = (
    Constant("EXTRACT_EVERY", "extract_every", 6, "guess",
             "balance of LLM cost against recall latency", "§ 2.1"),
    Constant("REINFORCE_STEP", "reinforce_step", 0.1, "guess",
             "confidence raise per genuinely new citation", "§ 2.1"),
    Constant("CANDIDATE_POOL_K", "candidate_pool_k", 10, "guess",
             "how many fragments the extractor resolves against", "§ 2.1"),
    Constant("RECALL_FLOOR", "recall_floor", 0.35, "guess",
             "cosine admission gate; embedding-model specific", "§ 6.1"),
    Constant("RECALL_FLOOR_MODEL", "recall_floor_model", "", "guess",
             "pinned encoder id the floor above is calibrated against", "§ 6.1"),
    Constant("RRF_K", "rrf_k", 60, "borrowed",
             "standard reciprocal-rank-fusion constant", "§ 6"),
    Constant("DECAY_K", "decay_k", 2, "borrowed",
             "exponent multiplier, GUM", "§ 6.2"),
    Constant("ALPHA_DECISION", "alpha_decision", 0.01, "guess",
             "~35-day half-life", "§ 6.2"),
    Constant("ALPHA_CONSTRAINT", "alpha_constraint", 0.01, "guess",
             "~35-day half-life", "§ 6.2"),
    Constant("ALPHA_FACT", "alpha_fact", 0.03, "guess",
             "~12-day half-life", "§ 6.2"),
    Constant("ALPHA_ARTEFACT", "alpha_artefact", 0.03, "guess",
             "~12-day half-life", "§ 6.2"),
    Constant("ALPHA_OPEN_QUESTION", "alpha_open_question", 0.15, "guess",
             "~2-day half-life", "§ 6.2"),
    Constant("MMR_LAMBDA", "mmr_lambda", 0.5, "borrowed", "GUM", "§ 6.3"),
    Constant("CORE_BUDGET_FRACTION", "core_budget_fraction", 0.10, "guess",
             "share of context window for the stable core", "§ 2.2"),
    Constant("SEL_BUDGET_FRACTION", "sel_budget_fraction", 0.10, "guess",
             "ceiling, not a target (§ 6.1)", "§ 2.2"),
    Constant("CONSOLIDATE_THRESHOLD", "consolidate_threshold", 40, "scaled",
             "Generative Agents use 150 for 25 agents in a town", "§ 2.6"),
    Constant("CONSOLIDATE_QUESTIONS", "consolidate_questions", 2, "scaled",
             "theirs: 3", "§ 2.6"),
    Constant("CONSOLIDATE_INSIGHTS", "consolidate_insights", 3, "scaled",
             "theirs: 5", "§ 2.6"),
    Constant("CONSOLIDATE_RECENT_N", "consolidate_recent_n", 30, "scaled",
             "theirs: 100", "§ 2.6"),
)

BY_NAME: Mapping[str, Constant] = {c.field: c for c in CATALOGUE}


def _env_int(var: str, default: int) -> int:
    raw = os.environ.get(var)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        raise TuningError(f"{var} must be an integer, got {raw!r}") from None


def _env_float(var: str, default: float) -> float:
    raw = os.environ.get(var)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        raise TuningError(f"{var} must be a float, got {raw!r}") from None


def _resolve(constant: Constant) -> int | float | str:
    if isinstance(constant.default, int):
        return _env_int(constant.env_var, constant.default)
    if isinstance(constant.default, float):
        return _env_float(constant.env_var, constant.default)
    raw = os.environ.get(constant.env_var)
    return constant.default if raw is None else raw


@dataclass(frozen=True)
class Tuning:
    extract_every: int = field(default_factory=lambda: _resolve(BY_NAME["extract_every"]))
    reinforce_step: float = field(default_factory=lambda: _resolve(BY_NAME["reinforce_step"]))
    candidate_pool_k: int = field(default_factory=lambda: _resolve(BY_NAME["candidate_pool_k"]))
    recall_floor: float = field(default_factory=lambda: _resolve(BY_NAME["recall_floor"]))
    recall_floor_model: str = field(default_factory=lambda: _resolve(BY_NAME["recall_floor_model"]))
    rrf_k: int = field(default_factory=lambda: _resolve(BY_NAME["rrf_k"]))
    decay_k: int = field(default_factory=lambda: _resolve(BY_NAME["decay_k"]))
    alpha_decision: float = field(default_factory=lambda: _resolve(BY_NAME["alpha_decision"]))
    alpha_constraint: float = field(default_factory=lambda: _resolve(BY_NAME["alpha_constraint"]))
    alpha_fact: float = field(default_factory=lambda: _resolve(BY_NAME["alpha_fact"]))
    alpha_artefact: float = field(default_factory=lambda: _resolve(BY_NAME["alpha_artefact"]))
    alpha_open_question: float = field(default_factory=lambda: _resolve(BY_NAME["alpha_open_question"]))
    mmr_lambda: float = field(default_factory=lambda: _resolve(BY_NAME["mmr_lambda"]))
    core_budget_fraction: float = field(default_factory=lambda: _resolve(BY_NAME["core_budget_fraction"]))
    sel_budget_fraction: float = field(default_factory=lambda: _resolve(BY_NAME["sel_budget_fraction"]))
    consolidate_threshold: float = field(default_factory=lambda: _resolve(BY_NAME["consolidate_threshold"]))
    consolidate_questions: int = field(default_factory=lambda: _resolve(BY_NAME["consolidate_questions"]))
    consolidate_insights: int = field(default_factory=lambda: _resolve(BY_NAME["consolidate_insights"]))
    consolidate_recent_n: int = field(default_factory=lambda: _resolve(BY_NAME["consolidate_recent_n"]))

    @classmethod
    def from_env(cls) -> "Tuning":
        return cls()

    def alpha_for(self, kind: str) -> float:
        """Decay rate for a fragment kind; unknown kinds fall back to
        ``alpha_fact``."""
        by_kind = {
            "decision": self.alpha_decision,
            "constraint": self.alpha_constraint,
            "fact": self.alpha_fact,
            "artefact": self.alpha_artefact,
            "open_question": self.alpha_open_question,
        }
        return by_kind.get(kind, self.alpha_fact)

    def as_rows(self) -> list[tuple[Constant, int | float | str]]:
        """Catalogue paired with effective values — what stage 6's inspector
        table renders."""
        return [(constant, getattr(self, constant.field)) for constant in CATALOGUE]


def log_effective_tuning(tuning: Tuning, log: Logger | None = None) -> None:
    """One INFO record per constant: name, effective value, status."""
    logger = log if log is not None else _LOG
    for constant, value in tuning.as_rows():
        logger.info("%s = %s (%s, %s)", constant.name, value, constant.status, constant.section)
