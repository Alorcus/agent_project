"""`core.anchoring`'s pure functions: no model, no store."""

from __future__ import annotations

import pytest

from agentchat.core.anchoring import (
    FUZZY_FLOOR,
    MIN_COVERAGE,
    PUNCTUATION_FOLD,
    anchor,
    anchor_in,
    coverage,
    normalise,
    significant,
)
from agentchat.core.models import Message

# -- anchor: exact path -----------------------------------------------------


def _msg(content: str, **overrides) -> Message:
    return Message(role="user", content=content, **overrides)


def test_exact_quote_span_slices_back_to_the_quote():
    message = _msg("We store readings in Postgres and query them nightly.")
    span = anchor("readings in Postgres", message)
    assert span is not None
    start, end = span
    assert message.content[start:end] == "readings in Postgres"


def test_whitespace_only_difference_span_covers_the_original_run():
    message = _msg("We store readings\nin Postgres nightly.")
    span = anchor("readings in Postgres", message)
    assert span is not None
    start, end = span
    assert message.content[start:end] == "readings\nin Postgres"


def test_case_only_difference_still_anchors_exactly():
    message = _msg("We rely on POSTGRES for storage.")
    span = anchor("postgres", message)
    assert span is not None
    start, end = span
    assert message.content[start:end] == "POSTGRES"


@pytest.mark.parametrize("original, folded", sorted(PUNCTUATION_FOLD.items()))
def test_each_punctuation_fold_substitution_anchors_on_the_exact_path(original, folded):
    message = _msg(f"before{original}after")
    quote = f"before{folded}after"
    result = normalise(quote)[0]
    span = anchor(quote, message)
    assert span is not None, f"{original!r} -> {folded!r} did not anchor"
    start, end = span
    assert normalise(message.content[start:end])[0] == result


def test_precomposed_and_decomposed_e_acute_fold_alike():
    precomposed = _msg("Le café est ouvert.")  # é as one codepoint
    decomposed_quote = "café"  # e + combining acute accent

    span = anchor(decomposed_quote, precomposed)

    assert span is not None
    start, end = span
    assert precomposed.content[start:end] == "café"


def test_one_substituted_character_anchors_via_the_fuzzy_path():
    message = _msg("The client's ops team forbids upgrading Postgres in production.")
    # "forbids" swapped for "forbid s" is nonsense; use a single substituted
    # character instead so the exact path can never match.
    span = anchor("the client's ops team forxids upgrading Postgres", message)
    assert span is not None
    start, end = span
    assert "client" in message.content[start:end]


def test_an_added_clause_does_not_anchor():
    message = _msg("We run Postgres 14 in production.")
    assert anchor("We run Postgres 14 in production and also MySQL", message) is None


def test_an_absent_quote_does_not_anchor():
    message = _msg("We run Postgres 14 in production.")
    assert anchor("We run MongoDB in staging", message) is None


def test_quote_at_index_zero():
    message = _msg("Kyoto is lovely in spring.")
    span = anchor("Kyoto", message)
    assert span == (0, 5)


def test_quote_at_the_last_character():
    message = _msg("The trip is set for spring")
    span = anchor("spring", message)
    assert span is not None
    assert span[1] == len(message.content)


def test_empty_quote_returns_none():
    assert anchor("", _msg("anything")) is None
    assert anchor("   ", _msg("anything")) is None


def test_empty_message_returns_none():
    assert anchor("hi", _msg("")) is None


# -- anchor_in ----------------------------------------------------------


def test_anchor_in_picks_the_containing_message():
    messages = [
        _msg("Let's plan the Kyoto trip.", id="m1"),
        _msg("Try the ramen place near the station.", id="m2"),
    ]
    located = anchor_in("ramen place near the station", messages)
    assert located is not None
    message, span = located
    assert message.id == "m2"
    assert message.content[span[0] : span[1]] == "ramen place near the station"


def test_anchor_in_breaks_a_fuzzy_tie_toward_the_most_recent_message():
    # Identical (typo'd) content in both messages gives identical fuzzy
    # scores, neither an exact match — a genuine tie, not the exact-hit
    # short-circuit.
    messages = [
        _msg("Postgres 15 is stable enough for us.", id="older"),
        _msg("Postgres 15 is stable enough for us.", id="newer"),
    ]
    located = anchor_in("Postgres 14 is stable enough for us", messages)
    assert located is not None
    assert located[0].id == "newer"


def test_anchor_in_short_circuits_on_the_first_exact_hit():
    messages = [
        _msg("Postgres 14 is stable enough for us.", id="first"),
        _msg("Postgres 14 is stable enough for us.", id="second"),
    ]
    located = anchor_in("Postgres 14 is stable enough for us", messages)
    assert located is not None
    assert located[0].id == "first"


def test_anchor_in_returns_none_when_nothing_reaches_the_floor():
    messages = [_msg("Completely unrelated content here.", id="m1")]
    assert anchor_in("Something about Postgres upgrades entirely absent", messages) is None


# -- significant / coverage --------------------------------------------------

_FACT = (
    "runs Postgres 14 in production and won't upgrade, because the "
    "client's ops team forbids it"
)


def test_significant_keeps_long_tokens_and_short_ones_with_digits():
    tokens = significant("a 14 runs ab team")
    assert "14" in tokens  # short but has a digit
    assert "runs" in tokens  # >= MIN_TOKEN_LENGTH
    assert "team" in tokens
    assert "a" not in tokens
    assert "ab" not in tokens  # short, no digit


def test_coverage_low_when_quotes_barely_touch_the_fact():
    assert coverage(_FACT, ["Postgres 14"]) == pytest.approx(2 / 9)


def test_coverage_high_when_quotes_carry_the_fact():
    value = coverage(
        _FACT,
        [
            "we run Postgres 14 in production",
            "the client's ops team won't let us upgrade",
        ],
    )
    assert value == pytest.approx(6 / 9)


def test_coverage_is_directional_extra_quote_material_costs_nothing():
    low = coverage("Postgres 14 in production", ["Postgres 14"])
    high = coverage(
        "Postgres 14 in production",
        ["Postgres 14 in production plus a great deal of unrelated extra material"],
    )
    assert high > low


def test_coverage_below_floor_for_a_near_miss():
    value = coverage("runs Postgres 15", ["we run Postgres 14"])
    assert value < MIN_COVERAGE


def test_coverage_is_1_when_the_fact_has_no_significant_tokens():
    assert coverage("it is", ["anything at all"]) == 1.0


def test_fuzzy_floor_rejects_a_genuine_paraphrase():
    message = _msg("We store about 40 million sensor readings a month.")
    paraphrase = "roughly forty million readings land in the database monthly"
    result = anchor(paraphrase, message)
    assert result is None
