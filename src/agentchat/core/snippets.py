"""Cutting a document's text into overlapping snippets.

Pure functions over one string and its offsets: a span indexes into the text
extraction produced, and nothing here rewrites that text — `text[start:end]` is
the snippet, verbatim. No I/O, no store, no embedder, the same contract
`anchoring.py` holds.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

#: Target snippet length in characters. Well under `embedding.MAX_EMBED_CHARS`,
#: which truncates rather than splits — a snippet that reaches it is embedded
#: as its own first half.
SNIPPET_CHARS = 1000
SNIPPET_OVERLAP = 150
#: A tail shorter than this is merged into its predecessor instead of standing
#: as a snippet of its own.
MIN_SNIPPET_CHARS = 200

_BLANK_LINE = re.compile(r"\n[ \t]*\n")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class Span:
    start: int
    end: int

    def __len__(self) -> int:
        return self.end - self.start


def spans(
    text: str,
    *,
    size: int = SNIPPET_CHARS,
    overlap: int = SNIPPET_OVERLAP,
    minimum: int = MIN_SNIPPET_CHARS,
) -> tuple[Span, ...]:
    """Paragraph-greedy spans over `text`, in order, together covering every
    non-whitespace character. Each span after the first reaches `overlap`
    characters back into its predecessor, snapped forward so it never starts
    mid-word."""
    if not text.strip():
        return ()
    size = max(1, size)
    # An overlap at or above the target makes a span start at or before its
    # predecessor's start, and the packing below stops advancing.
    overlap = max(0, min(overlap, size // 2))
    minimum = max(0, min(minimum, size))

    packed = _merge_tail(_pack(list(_pieces(text, size)), size), minimum)
    return tuple(_overlapped(text, packed, overlap))


def page_of(start: int, page_starts: Sequence[int]) -> int | None:
    """The 1-based page holding `start`. `None` when the document has no
    pages — a plain text file is one unpaginated string."""
    if not page_starts:
        return None
    page = 1
    for number, page_start in enumerate(page_starts, start=1):
        if start >= page_start:
            page = number
        else:
            break
    return page


def _pieces(text: str, size: int) -> Iterator[Span]:
    """The atomic units packing may not split: paragraphs, or — when a
    paragraph is longer than `size` — its sentences, hard-cut if a single
    sentence is still longer."""
    for block in _blocks(text):
        if len(block) <= size:
            yield block
            continue
        for sentence in _sentences(text, block):
            if len(sentence) <= size:
                yield sentence
                continue
            for start in range(sentence.start, sentence.end, size):
                yield Span(start, min(start + size, sentence.end))


def _blocks(text: str) -> Iterator[Span]:
    cursor = 0
    for match in _BLANK_LINE.finditer(text):
        block = _trimmed(text, Span(cursor, match.start()))
        if block is not None:
            yield block
        cursor = match.end()
    block = _trimmed(text, Span(cursor, len(text)))
    if block is not None:
        yield block


def _sentences(text: str, block: Span) -> Iterator[Span]:
    cursor = block.start
    for match in _SENTENCE_END.finditer(text, block.start, block.end):
        sentence = _trimmed(text, Span(cursor, match.start()))
        if sentence is not None:
            yield sentence
        cursor = match.end()
    sentence = _trimmed(text, Span(cursor, block.end))
    if sentence is not None:
        yield sentence


def _trimmed(text: str, span: Span) -> Span | None:
    """`span` without its surrounding whitespace, or `None` when it holds
    none of its own."""
    start, end = span.start, span.end
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return Span(start, end) if end > start else None


def _pack(pieces: list[Span], size: int) -> list[Span]:
    packed: list[Span] = []
    current: Span | None = None
    for piece in pieces:
        if current is None:
            current = piece
        elif piece.end - current.start <= size:
            current = Span(current.start, piece.end)
        else:
            packed.append(current)
            current = piece
    if current is not None:
        packed.append(current)
    return packed


def _merge_tail(packed: list[Span], minimum: int) -> list[Span]:
    if len(packed) > 1 and len(packed[-1]) < minimum:
        tail = packed.pop()
        packed[-1] = Span(packed[-1].start, tail.end)
    return packed


def _overlapped(text: str, packed: list[Span], overlap: int) -> Iterator[Span]:
    previous: Span | None = None
    for span in packed:
        if previous is not None and overlap:
            start = _word_start(text, max(previous.start + 1, span.start - overlap))
            if start < span.end:
                span = Span(start, span.end)
        yield span
        previous = span


def _word_start(text: str, index: int) -> int:
    """The first character of the word at or after `index` — landing inside a
    word walks forward to the next one rather than slicing it in half."""
    if index <= 0:
        return 0
    while (
        index < len(text)
        and not text[index].isspace()
        and not text[index - 1].isspace()
    ):
        index += 1
    while index < len(text) and text[index].isspace():
        index += 1
    return index
