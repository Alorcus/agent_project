"""The fact→messages projection: windows, spans, orphans, clamping. No pilot,
no provider — `core/evidence.py` is a pure function."""

from __future__ import annotations

from agentchat.core.evidence import evidence_for, merge_spans
from agentchat.core.models import Author, Conversation, Fact, Message, Phrase


def _msg(content: str, id: str, role: str = "user") -> Message:
    return Message(role=role, content=content, id=id)


def _phrase(message_id: str, start: int, end: int, quote: str = "") -> Phrase:
    return Phrase(
        message_id=message_id,
        start=start,
        end=end,
        author=Author(kind="user", label="user"),
        quote=quote,
    )


def _fact(**overrides) -> Fact:
    defaults = dict(window_start=0, window_end=6, phrases=())
    defaults.update(overrides)
    return Fact(**defaults)


def test_full_window_gives_one_excerpt_per_countable_message_in_order():
    conv = Conversation(messages=[_msg(f"line {i}", f"m{i}") for i in range(6)])
    fact = _fact(phrases=(_phrase("m0", 0, 4),))

    ev = evidence_for(fact, conv)

    assert [e.message.id for e in ev.excerpts] == [f"m{i}" for i in range(6)]
    assert ev.excerpts[0].spans == ((0, 4),)
    assert all(e.spans == () for e in ev.excerpts[1:])


def test_window_slice_goes_through_countable_skipping_system_and_empty():
    conv = Conversation(
        messages=[
            _msg("sys", "s0", role="system"),
            _msg("hello there", "m0"),
            _msg("", "m1", role="assistant"),
            _msg("second turn", "m2"),
            _msg("third answer", "m3", role="assistant"),
        ]
    )
    # countable == [m0, m2, m3]
    fact = _fact(window_start=0, window_end=3, phrases=(_phrase("m0", 0, 5),))

    ev = evidence_for(fact, conv)

    assert [e.message.id for e in ev.excerpts] == ["m0", "m2", "m3"]


def test_two_phrases_in_one_message_are_one_excerpt_with_sorted_spans():
    conv = Conversation(messages=[_msg("abc def ghi", "m0")])
    fact = _fact(
        window_start=0,
        window_end=1,
        phrases=(_phrase("m0", 8, 11), _phrase("m0", 0, 3)),
    )

    ev = evidence_for(fact, conv)

    assert len(ev.excerpts) == 1
    assert ev.excerpts[0].spans == ((0, 3), (8, 11))


def test_overlapping_and_touching_spans_merge():
    assert merge_spans([(0, 3), (3, 5), (4, 8)]) == ((0, 8),)
    assert merge_spans([(10, 12), (0, 2)]) == ((0, 2), (10, 12))

    conv = Conversation(messages=[_msg("0123456789", "m0")])
    fact = _fact(
        window_start=0,
        window_end=1,
        phrases=(_phrase("m0", 0, 4), _phrase("m0", 4, 6), _phrase("m0", 5, 9)),
    )

    ev = evidence_for(fact, conv)

    assert ev.excerpts[0].spans == ((0, 9),)


def test_span_past_the_content_is_clamped_and_an_empty_clamp_is_dropped():
    conv = Conversation(messages=[_msg("abcd", "m0")])
    fact = _fact(
        window_start=0,
        window_end=1,
        phrases=(_phrase("m0", 2, 10), _phrase("m0", 5, 9)),
    )

    ev = evidence_for(fact, conv)

    assert ev.excerpts[0].spans == ((2, 4),)


def test_phrase_outside_the_window_becomes_an_orphan_quote_not_an_excerpt():
    conv = Conversation(messages=[_msg("inside text", "m0")])
    fact = _fact(
        window_start=0,
        window_end=1,
        phrases=(
            _phrase("m0", 0, 6),
            _phrase("mX", 0, 4, quote="outside quote"),
        ),
    )

    ev = evidence_for(fact, conv)

    assert [e.message.id for e in ev.excerpts] == ["m0"]
    assert ev.orphans == ("outside quote",)


def test_orphan_with_empty_quote_contributes_no_line():
    conv = Conversation(messages=[_msg("inside text", "m0")])
    fact = _fact(
        window_start=0,
        window_end=1,
        phrases=(_phrase("m0", 0, 6), _phrase("mX", 0, 4, quote="")),
    )

    ev = evidence_for(fact, conv)

    assert ev.orphans == ()


def test_window_end_past_the_conversation_is_clamped_without_indexerror():
    conv = Conversation(messages=[_msg(f"line {i}", f"m{i}") for i in range(3)])
    fact = _fact(window_start=0, window_end=99, phrases=(_phrase("m0", 0, 2),))

    ev = evidence_for(fact, conv)

    assert len(ev.excerpts) == 3


def test_empty_window_makes_every_phrase_an_orphan():
    conv = Conversation(messages=[_msg(f"line {i}", f"m{i}") for i in range(3)])
    fact = _fact(
        window_start=5,
        window_end=5,
        phrases=(_phrase("m0", 0, 3, quote="a quote"),),
    )

    ev = evidence_for(fact, conv)

    assert ev.excerpts == ()
    assert ev.orphans == ("a quote",)
