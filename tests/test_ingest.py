"""Reading a dropped file, cutting it up, and storing it once."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentchat.core.errors import IngestError
from agentchat.core.ingest import (
    CorpusSync,
    DocumentIngestor,
    dropped_paths,
    read_document,
)
from agentchat.core.retrieval import SnippetIndex
from agentchat.llm.embedding import HashingEmbedder

from factories import make_pdf_bytes

PROSE = "\n\n".join(
    f"Paragraph {n}. The deploy window is Tuesday and the release train "
    f"leaves at noon, every week without exception." * 3
    for n in range(6)
)


def _ingestor(store, *, index: SnippetIndex | None = None, **kwargs) -> DocumentIngestor:
    return DocumentIngestor(store, index, **kwargs)


def _written(tmp_path: Path, name: str, text: str = PROSE) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# -- reading ---------------------------------------------------------------


async def test_a_text_file_is_stored_with_its_snippets_and_vectors(tmp_path, store):
    index = SnippetIndex(store, HashingEmbedder())
    result = await _ingestor(store, index=index).ingest(_written(tmp_path, "notes.txt"))

    assert not result.reused
    assert result.document.title == "notes.txt"
    assert result.document.media_type == "text"
    assert result.document.snippet_count == len(result.snippets) > 1
    assert await store.list_snippets(result.document.id) == list(result.snippets)
    assert len(await store.snippet_vectors(model_id="hashing-64")) == len(result.snippets)


async def test_a_snippet_slices_back_out_of_the_stored_document_text(tmp_path, store):
    result = await _ingestor(store).ingest(_written(tmp_path, "notes.md"))

    document = await store.document(result.document.id)
    for snippet in result.snippets:
        assert document.text[snippet.start : snippet.end] == snippet.text


async def test_a_pdf_ingests_with_its_snippets_attributed_to_pages(tmp_path, store):
    path = tmp_path / "handbook.pdf"
    path.write_bytes(
        make_pdf_bytes(
            [
                "Deploy window is Tuesday.\nThe release train leaves at noon.",
                "Priya is vegetarian and allergic to peanuts.",
            ]
        )
    )

    result = await _ingestor(store, size=60, overlap=0, minimum=0).ingest(path)

    assert result.document.media_type == "pdf"
    assert "Priya" in result.document.text
    pages = {snippet.page for snippet in result.snippets}
    assert pages == {1, 2}


async def test_a_pdf_with_no_extractable_text_is_refused(tmp_path, store):
    path = tmp_path / "scan.pdf"
    path.write_bytes(make_pdf_bytes([" "]))

    with pytest.raises(IngestError, match="OCR"):
        await _ingestor(store).ingest(path)


async def test_a_file_that_is_not_utf8_still_reads(tmp_path, store):
    path = tmp_path / "legacy.txt"
    path.write_bytes("Café closes at noon. ".encode("latin-1") * 40)

    result = await _ingestor(store).ingest(path)

    assert "Caf" in result.document.text


# -- refusals --------------------------------------------------------------


@pytest.mark.parametrize(
    "name, match",
    [("report.docx", "not a document"), ("Makefile", "not a document")],
)
async def test_an_unsupported_file_is_refused_by_name(tmp_path, store, name, match):
    path = tmp_path / name
    path.write_text("whatever")
    with pytest.raises(IngestError, match=match):
        await _ingestor(store).ingest(path)


async def test_a_directory_a_missing_file_and_an_oversize_file_are_refused(
    tmp_path, store
):
    with pytest.raises(IngestError, match="directory"):
        await _ingestor(store).ingest(tmp_path)
    with pytest.raises(IngestError, match="does not exist"):
        await _ingestor(store).ingest(tmp_path / "nope.txt")
    with pytest.raises(IngestError, match="over the"):
        await _ingestor(store, max_bytes=10).ingest(_written(tmp_path, "big.txt"))


async def test_read_document_refuses_an_unknown_suffix_on_its_own(tmp_path):
    path = tmp_path / "archive.zip"
    path.write_bytes(b"PK\x03\x04")
    with pytest.raises(IngestError):
        read_document(path)


# -- identity --------------------------------------------------------------


async def test_the_same_content_ingests_once_and_reports_itself_as_reused(
    tmp_path, store
):
    path = _written(tmp_path, "notes.txt")
    ingestor = _ingestor(store, index=SnippetIndex(store, HashingEmbedder()))
    first = await ingestor.ingest(path)

    again = await ingestor.ingest(path)

    assert again.reused
    assert again.document.id == first.document.id
    assert again.snippets == ()
    assert len(await store.list_documents()) == 1


async def test_the_same_content_under_another_name_is_the_same_document(
    tmp_path, store
):
    ingestor = _ingestor(store)
    await ingestor.ingest(_written(tmp_path, "notes.txt"))

    copy = await ingestor.ingest(_written(tmp_path, "copy.txt"))

    assert copy.reused
    assert len(await store.list_documents()) == 1


async def test_a_changed_file_replaces_the_document_it_supersedes(tmp_path, store):
    index = SnippetIndex(store, HashingEmbedder())
    ingestor = _ingestor(store, index=index)
    path = _written(tmp_path, "notes.txt")
    first = await ingestor.ingest(path)

    path.write_text(PROSE.replace("Tuesday", "Thursday"), encoding="utf-8")
    second = await ingestor.ingest(path)

    documents = await store.list_documents()
    assert len(documents) == 1
    assert documents[0].id == second.document.id != first.document.id
    # The superseded document's vectors went with it: nothing stale is left
    # to retrieve.
    assert len(await store.snippet_vectors(model_id="hashing-64")) == len(
        second.snippets
    )


async def test_without_an_index_the_document_is_still_stored_for_the_catch_up(
    tmp_path, store
):
    result = await _ingestor(store).ingest(_written(tmp_path, "notes.txt"))

    assert await store.snippet_vectors(model_id="hashing-64") == []
    embedded = await SnippetIndex(store, HashingEmbedder()).ensure_indexed()
    assert embedded == len(result.snippets)


# -- the corpus directory --------------------------------------------------


async def test_sync_takes_what_it_can_and_collects_what_it_cannot(tmp_path, store):
    corpus = tmp_path / "corpus"
    (corpus / "nested").mkdir(parents=True)
    _written(corpus, "one.txt")
    _written(corpus / "nested", "two.md", PROSE.replace("Tuesday", "Wednesday"))
    (corpus / "ignored.docx").write_text("not a document")
    (corpus / "broken.pdf").write_bytes(b"not really a pdf")

    sync = await _ingestor(store, settle=0.0).sync_dir(corpus)

    assert {r.document.title for r in sync.ingested} == {"one.txt", "two.md"}
    # An unreadable file is reported, not raised, and the rest still lands.
    assert [path.name for path, _ in sync.failed] == ["broken.pdf"]
    assert sync.changed


async def test_sync_of_an_absent_directory_changes_nothing(tmp_path, store):
    sync = await _ingestor(store).sync_dir(tmp_path / "absent")

    assert sync == CorpusSync()
    assert not sync.changed


async def test_a_second_sync_with_nothing_new_does_nothing(tmp_path, store):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _written(corpus, "one.txt")
    ingestor = _ingestor(store, settle=0.0)
    await ingestor.sync_dir(corpus)

    again = await ingestor.sync_dir(corpus)

    assert not again.changed
    assert len(await store.list_documents()) == 1


async def test_a_file_added_to_the_corpus_is_picked_up_by_the_next_sync(
    tmp_path, store
):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _written(corpus, "one.txt")
    ingestor = _ingestor(store, settle=0.0)
    await ingestor.sync_dir(corpus)

    _written(corpus, "two.md", PROSE.replace("Tuesday", "Wednesday"))
    sync = await ingestor.sync_dir(corpus)

    assert [r.document.title for r in sync.ingested] == ["two.md"]
    assert len(await store.list_documents()) == 2


async def test_editing_a_corpus_file_replaces_its_document(tmp_path, store):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    path = _written(corpus, "one.txt")
    ingestor = _ingestor(store, settle=0.0)
    first = await ingestor.sync_dir(corpus)

    path.write_text(PROSE.replace("Tuesday", "Thursday"), encoding="utf-8")
    second = await ingestor.sync_dir(corpus)

    documents = await store.list_documents()
    assert len(documents) == 1
    assert documents[0].id != first.ingested[0].document.id
    assert documents[0].id == second.ingested[0].document.id


async def test_removing_a_file_from_the_corpus_drops_its_document(tmp_path, store):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    path = _written(corpus, "one.txt")
    _written(corpus, "two.md", PROSE.replace("Tuesday", "Wednesday"))
    index = SnippetIndex(store, HashingEmbedder())
    ingestor = _ingestor(store, index=index, settle=0.0)
    await ingestor.sync_dir(corpus)

    path.unlink()
    sync = await ingestor.sync_dir(corpus)

    assert [document.title for document in sync.removed] == ["one.txt"]
    assert [d.title for d in await store.list_documents()] == ["two.md"]
    # The dropped document took its vectors with it.
    remaining = await store.list_snippets((await store.list_documents())[0].id)
    assert len(await store.snippet_vectors(model_id="hashing-64")) == len(remaining)


async def test_a_document_from_outside_the_corpus_is_never_dropped(tmp_path, store):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    ingestor = _ingestor(store, settle=0.0)
    dropped = await ingestor.ingest(_written(tmp_path, "handbook.txt"))

    sync = await ingestor.sync_dir(corpus)

    assert sync.removed == ()
    assert [d.id for d in await store.list_documents()] == [dropped.document.id]


async def test_a_file_still_being_written_waits_for_the_next_sync(tmp_path, store):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    path = _written(corpus, "one.txt")
    # A copy in flight: the timestamp is now, so the file has not stood still
    # for `settle` seconds yet.
    ingestor = _ingestor(store, settle=60.0)

    first = await ingestor.sync_dir(corpus)

    assert first.ingested == ()
    assert [p.name for p in first.pending] == ["one.txt"]
    assert await store.list_documents() == []

    # Unchanged since the previous pass — finished, whatever its timestamp.
    second = await ingestor.sync_dir(corpus)

    assert [r.document.title for r in second.ingested] == ["one.txt"]


# -- reading a drop off the terminal ---------------------------------------


def test_dropped_paths_reads_the_shapes_a_terminal_sends(tmp_path):
    plain = _written(tmp_path, "notes.txt", "hello")
    spaced = tmp_path / "my notes.txt"
    spaced.write_text("hello")

    assert dropped_paths(str(plain)) == (plain,)
    assert dropped_paths(f"{plain}\n") == (plain,)
    assert dropped_paths(f"'{plain}'") == (plain,)
    assert dropped_paths(f'"{plain}"') == (plain,)
    assert dropped_paths(f"file://{plain}") == (plain,)
    assert dropped_paths(str(spaced)) == (spaced,)
    assert dropped_paths(str(spaced).replace(" ", "\\ ")) == (spaced,)
    assert dropped_paths(f"{plain}\n{spaced}\n") == (plain, spaced)
    # Several paths on one line: a terminal that sends more than one quotes or
    # escapes the spaces, because otherwise the payload is ambiguous.
    other = _written(tmp_path, "other.txt", "hello there")
    assert dropped_paths(f"{plain} {other}") == (plain, other)
    assert dropped_paths(f"{plain} '{spaced}'") == (plain, spaced)


def test_dropped_paths_ignores_a_repeat_of_the_same_file(tmp_path):
    plain = _written(tmp_path, "notes.txt", "hello")
    assert dropped_paths(f"{plain} {plain}") == (plain,)


def test_prose_and_absent_paths_are_not_drops(tmp_path):
    assert dropped_paths("Let's use SQLite for storage, it's simple.") == ()
    assert dropped_paths(str(tmp_path / "absent.txt")) == ()
    assert dropped_paths("") == ()
    # An unbalanced quote is prose, not a payload to be split.
    assert dropped_paths("it's a plan, isn't it?") == ()
