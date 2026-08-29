"""Locating a quoted phrase inside a real stored message, by code — no model
is ever asked where text is. Pure functions only: no I/O, no provider, no
store, and it must stay that way (`facts.py` and `retrieval.py` are the
callers that know what a match is *for*)."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from difflib import SequenceMatcher

from agentchat.core.models import Message

FUZZY_FLOOR = 0.85          # similarity a fuzzy window must reach
MIN_COVERAGE = 0.2          # share of a fact's tokens that must be quoted
MIN_TOKEN_LENGTH = 4        # cheap stand-in for a stopword list
PUNCTUATION_FOLD = {        # what a tokenizer trades when it re-generates text
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-",
    "—": "-", "―": "-", "−": "-",
    "…": "...",
    "​": "", "‌": "", "‍": "", "­": "",
}

_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


def normalise(text: str) -> tuple[str, list[int]]:
    """Fold `text` for matching and return `(folded, index_map)`, where
    `index_map[i]` is the index in `text` of the character that produced
    `folded[i]` — one entry per *emitted* character, so a fold that changes
    length still maps back exactly."""
    chars: list[str] = []
    index_map: list[int] = []
    started = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            start = i
            while i < n and text[i].isspace():
                i += 1
            if started:
                chars.append(" ")
                index_map.append(start)
            continue
        started = True
        if ch in PUNCTUATION_FOLD:
            for c in PUNCTUATION_FOLD[ch]:
                chars.append(c)
                index_map.append(i)
        elif unicodedata.combining(ch):
            pass
        else:
            # NFD-decompose this one character (not the whole string, which
            # would shift every later offset) so a precomposed accented
            # letter sheds its combining mark the same way an
            # already-decomposed one does, two lines up.
            for sub in unicodedata.normalize("NFD", ch):
                if unicodedata.combining(sub):
                    continue
                for c in sub.casefold():
                    chars.append(c)
                    index_map.append(i)
        i += 1
    return "".join(chars), index_map


def _mapped_span(index_map: list[int], start_idx: int, end_idx: int) -> tuple[int, int]:
    return index_map[start_idx], index_map[end_idx] + 1


def _anchor_with_score(quote: str, message: Message) -> tuple[tuple[int, int], float] | None:
    norm_quote, _ = normalise(quote)
    if not norm_quote:
        return None
    norm_message, index_map = normalise(message.content)
    if not norm_message:
        return None

    exact = norm_message.find(norm_quote)
    if exact != -1:
        span = _mapped_span(index_map, exact, exact + len(norm_quote) - 1)
        return span, 1.0

    # One seeded window, not a slide over every offset: find the longest
    # common substring and align the quote's start against it.
    matcher = SequenceMatcher(None, norm_message, norm_quote, autojunk=False)
    match = matcher.find_longest_match(0, len(norm_message), 0, len(norm_quote))
    if match.size == 0:
        return None
    max_start = max(0, len(norm_message) - len(norm_quote))
    candidate_start = min(max(0, match.a - match.b), max_start)
    candidate_end = min(len(norm_message), candidate_start + len(norm_quote))
    window = norm_message[candidate_start:candidate_end]
    if not window:
        return None
    ratio = SequenceMatcher(None, norm_quote, window, autojunk=False).ratio()
    span = _mapped_span(index_map, candidate_start, candidate_end - 1)
    return span, ratio


def anchor(quote: str, message: Message) -> tuple[int, int] | None:
    """Locate `quote` in `message.content`, exact match first, a single
    bounded fuzzy match second. `None` below `FUZZY_FLOOR`, empty quote, or
    empty message."""
    result = _anchor_with_score(quote, message)
    if result is None:
        return None
    span, score = result
    return span if score >= FUZZY_FLOOR else None


def anchor_in(
    quote: str, messages: Sequence[Message]
) -> tuple[Message, tuple[int, int]] | None:
    """The best match for `quote` across `messages`, ties broken toward the
    most recent; an exact hit short-circuits."""
    best: tuple[float, Message, tuple[int, int]] | None = None
    for message in messages:
        result = _anchor_with_score(quote, message)
        if result is None:
            continue
        span, score = result
        if score >= 1.0:
            return message, span
        if best is None or score >= best[0]:
            best = (score, message, span)
    if best is None or best[0] < FUZZY_FLOOR:
        return None
    _, message, span = best
    return message, span


def significant(text: str) -> set[str]:
    """Tokens of `text` worth requiring in a quote: at least `MIN_TOKEN_LENGTH`
    characters, or containing a digit — versions, dates and quantities are
    where a drifted digit matters most."""
    normalised, _ = normalise(text)
    tokens = _TOKEN_RE.findall(normalised)
    return {t for t in tokens if len(t) >= MIN_TOKEN_LENGTH or any(c.isdigit() for c in t)}


def coverage(fact_text: str, quotes: Sequence[str]) -> float:
    """Share of `fact_text`'s significant tokens that appear in `quotes`.
    `1.0` when the fact has no significant tokens. The denominator is the
    fact's tokens only — a quote saying more than the fact costs nothing; a
    fact saying more than its quotes is what this catches."""
    fact_tokens = significant(fact_text)
    if not fact_tokens:
        return 1.0
    quote_tokens: set[str] = set()
    for quote in quotes:
        quote_tokens |= significant(quote)
    return len(fact_tokens & quote_tokens) / len(fact_tokens)
