"""The pure snippeting layer: spans over a document's text, nothing else."""

from __future__ import annotations

from agentchat.core.snippets import (
    MIN_SNIPPET_CHARS,
    SNIPPET_CHARS,
    Span,
    page_of,
    spans,
)

PARAGRAPH = "Alpha beta gamma delta epsilon. " * 8  # ~250 chars
DOCUMENT = "\n\n".join(f"Paragraph {n}. {PARAGRAPH}" for n in range(10))


def _covered(text: str, result: tuple[Span, ...]) -> set[int]:
    return {i for span in result for i in range(span.start, span.end)}


def test_every_span_slices_back_to_its_own_text():
    for span in spans(DOCUMENT):
        assert DOCUMENT[span.start : span.end].strip()


def test_spans_are_ordered_and_cover_every_non_whitespace_character():
    result = spans(DOCUMENT)
    assert [s.start for s in result] == sorted(s.start for s in result)
    covered = _covered(DOCUMENT, result)
    missed = [
        i for i, char in enumerate(DOCUMENT) if not char.isspace() and i not in covered
    ]
    assert missed == []


def test_a_paragraph_document_packs_whole_paragraphs_up_to_the_size():
    result = spans(DOCUMENT, overlap=0)
    assert len(result) > 1
    assert all(len(span) <= SNIPPET_CHARS for span in result)
    # Packing is greedy, so a snippet holds more than one 250-character
    # paragraph.
    assert max(len(span) for span in result) > 500


def test_a_paragraph_longer_than_the_size_splits_on_sentence_ends():
    text = "One sentence here. " * 300  # a single 5700-character paragraph
    result = spans(text, overlap=0)
    assert all(len(span) <= SNIPPET_CHARS for span in result)
    # Every span ends on a sentence end, never mid-sentence.
    assert all(text[span.end - 1] == "." for span in result)


def test_a_single_sentence_longer_than_the_size_is_hard_cut():
    text = "word " * 500  # 2500 characters, no sentence end anywhere
    result = spans(text, overlap=0)
    assert len(result) > 1
    assert all(len(span) <= SNIPPET_CHARS for span in result)


def test_a_short_tail_merges_into_its_predecessor():
    text = ("Alpha beta gamma. " * 60).strip() + "\n\nTiny tail."
    result = spans(text, overlap=0)
    assert all(len(span) >= MIN_SNIPPET_CHARS for span in result)
    assert text[result[-1].start : result[-1].end].endswith("Tiny tail.")


def test_overlap_reaches_back_but_never_starts_mid_word():
    result = spans(DOCUMENT, overlap=150)
    assert len(result) > 1
    for previous, span in zip(result, result[1:]):
        assert previous.start < span.start < previous.end
        assert not DOCUMENT[span.start].isspace()
        assert DOCUMENT[span.start - 1].isspace()


def test_an_overlap_at_or_above_the_size_is_clamped_rather_than_looping():
    result = spans(DOCUMENT, size=400, overlap=900)
    assert [s.start for s in result] == sorted({s.start for s in result})
    assert len(result) > 1


def test_text_with_nothing_in_it_produces_no_spans():
    assert spans("") == ()
    assert spans("   \n\n \t ") == ()


def test_page_of_reads_a_start_offset_back_to_a_page():
    assert page_of(0, ()) is None
    starts = (0, 100, 250)
    assert page_of(0, starts) == 1
    assert page_of(99, starts) == 1
    assert page_of(100, starts) == 2
    assert page_of(400, starts) == 3
