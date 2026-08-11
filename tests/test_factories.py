"""A broken factory silently weakens every later stage's gate, so the builders
get their own tests."""

from __future__ import annotations

import pytest

from factories import GraphBuilder, make_citation, make_fragment, make_message


def test_builder_wires_the_section_111_hierarchy():
    builder = GraphBuilder()
    conversation = builder.conversation()
    m1, m2, m3 = (builder.message(conversation) for _ in range(3))
    f1, f2 = builder.extracted(m1, m2), builder.extracted(m2, m3)
    f3 = builder.consolidated(f1, f2)

    graph = builder.build()

    assert len(graph.messages) == 3
    assert len(graph.fragments) == 3
    assert len(graph.citations) == 6

    upper = [c for c in graph.citations if c.fragment_id == f3.id]
    assert {c.source_fragment_id for c in upper} == {f1.id, f2.id}
    assert all(c.source_message_id is None for c in upper)


def test_fragment_ids_are_unique_and_referenced_by_citations():
    builder = GraphBuilder()
    conversation = builder.conversation()
    m1, m2 = builder.message(conversation), builder.message(conversation)
    f1, f2 = builder.extracted(m1), builder.extracted(m2)
    builder.consolidated(f1, f2)

    graph = builder.build()
    ids = [f.id for f in graph.fragments]

    assert len(set(ids)) == len(ids)
    for citation in graph.citations:
        assert citation.fragment_id in ids
        if citation.source_fragment_id is not None:
            assert citation.source_fragment_id in ids


def test_make_citation_rejects_two_sources_and_zero_sources():
    fragment = make_fragment(id=1)
    other = make_fragment(id=2)
    message = make_message()

    with pytest.raises(ValueError):
        make_citation(fragment)

    with pytest.raises(ValueError):
        make_citation(fragment, message=message, source=other)
