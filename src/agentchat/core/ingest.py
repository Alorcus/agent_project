"""Turning a dropped file into stored, embedded snippets.

One entry point — `DocumentIngestor.ingest` — and one boundary: everything
below it that can fail becomes an `IngestError` naming the file and the reason,
because the user asked for this ingest and has to be told what happened to it.

A document's identity is the SHA-256 of its extracted text, not its path: the
same bytes under two names are one document, and an edited file is a new one
that replaces its predecessor at that path.

`sync_dir` treats the corpus directory as authoritative: what is in it is what
is retrievable. It is polled rather than watched — the corpus lives on a
network filesystem, where a file written by an `scp` from another host raises
no local inotify event, because the write never passes through this node's
kernel.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import shlex
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from urllib.parse import unquote, urlparse

from agentchat.core.errors import AgentChatError, IngestError
from agentchat.core.models import Document, Snippet
from agentchat.core.snippets import (
    MIN_SNIPPET_CHARS,
    SNIPPET_CHARS,
    SNIPPET_OVERLAP,
    page_of,
    spans,
)
from agentchat.storage.base import ConversationStore

if TYPE_CHECKING:
    from agentchat.core.retrieval import SnippetIndex

_log = logging.getLogger(__name__)

MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
#: How long a file's size and timestamp must have stood still before it is
#: read. A copy in flight is a file that grows: ingesting one mid-`scp`
#: would store its first half and call it the document.
SETTLE_SECONDS = 2.0
TEXT_SUFFIXES = frozenset(
    {".txt", ".md", ".markdown", ".rst", ".text", ".log", ".csv", ".json"}
)
PDF_SUFFIXES = frozenset({".pdf"})
SUPPORTED_SUFFIXES = TEXT_SUFFIXES | PDF_SUFFIXES

#: Pages are joined by a blank line, so a page boundary is also a snippet
#: boundary candidate.
PAGE_SEPARATOR = "\n\n"


@dataclass(frozen=True)
class Extracted:
    text: str
    media_type: Literal["text", "pdf"]
    #: Offset of each page's first character in `text`. `()` for plain text,
    #: which has no pages to attribute a snippet to.
    page_starts: tuple[int, ...] = ()


@dataclass(frozen=True)
class Ingested:
    document: Document
    snippets: tuple[Snippet, ...]
    #: True when this content was already stored — nothing was written.
    reused: bool = False


def read_document(path: Path) -> Extracted:
    """Extract `path`'s text once. Every snippet span indexes into the string
    returned here, so nothing downstream may normalise it."""
    suffix = path.suffix.lower()
    if suffix in PDF_SUFFIXES:
        return _read_pdf(path)
    if suffix in TEXT_SUFFIXES:
        return _read_text(path)
    raise IngestError(
        f"{path.name}: {suffix or 'no extension'} is not a document — "
        f"accepted: {', '.join(sorted(SUPPORTED_SUFFIXES))}"
    )


def _read_text(path: Path) -> Extracted:
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        # Latin-1 decodes any byte string, so this is the end of the line
        # rather than one more guess in a chain.
        text = raw.decode("latin-1")
    return Extracted(text=text, media_type="text")


def _read_pdf(path: Path) -> Extracted:
    # Imported here, not at module scope: a text-only ingest and an app with
    # ingestion switched off never pay for it, and a broken install surfaces
    # as an IngestError on a PDF rather than at startup.
    try:
        from pypdf import PdfReader
    except ImportError as error:  # pragma: no cover - dependency is declared
        raise IngestError("reading PDFs needs pypdf; run `uv sync`") from error

    reader = PdfReader(str(path))
    parts: list[str] = []
    starts: list[int] = []
    cursor = 0
    for page in reader.pages:
        text = page.extract_text() or ""
        starts.append(cursor)
        parts.append(text)
        cursor += len(text) + len(PAGE_SEPARATOR)
    text = PAGE_SEPARATOR.join(parts)
    if not text.strip():
        raise IngestError(
            f"{path.name}: no extractable text — a scanned PDF needs OCR first"
        )
    return Extracted(text=text, media_type="pdf", page_starts=tuple(starts))


def dropped_paths(text: str) -> tuple[Path, ...]:
    """The existing files named by a paste payload, in order.

    `()` when the payload is prose — which is how the prompt tells a dropped
    file from an ordinary paste. Terminals differ on how they encode a drop:
    bare, quoted, `\\ `-escaped, or as a `file://` URI, one path or several.
    """
    found: list[Path] = []
    seen: set[Path] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        for candidate in _candidates(line):
            resolved = _as_file(candidate)
            if resolved is not None and resolved not in seen:
                seen.add(resolved)
                found.append(resolved)
    return tuple(found)


def _candidates(line: str) -> Iterator[str]:
    # Whole line first: an unquoted, unescaped path with spaces in it only
    # resolves this way.
    yield line
    try:
        tokens = shlex.split(line)
    except ValueError:
        # An unbalanced quote means this was prose, not a shell-ish payload.
        tokens = line.split()
    yield from tokens


def _as_file(candidate: str) -> Path | None:
    candidate = candidate.strip()
    if not candidate:
        return None
    if candidate.startswith("file://"):
        candidate = unquote(urlparse(candidate).path)
    elif len(candidate) > 1 and candidate[0] == candidate[-1] and candidate[0] in "\"'":
        candidate = candidate[1:-1]
    else:
        candidate = re.sub(r"\\(.)", r"\1", candidate)
    try:
        path = Path(candidate).expanduser()
        if not path.is_file():
            return None
        return path.resolve()
    except OSError:
        # A candidate longer than the filesystem's limit, or with a NUL in it.
        return None


@dataclass(frozen=True)
class CorpusSync:
    """What one pass over the corpus directory changed."""

    ingested: tuple[Ingested, ...] = ()
    removed: tuple[Document, ...] = ()
    #: `(path, reason)` per file that could not be read — collected, not
    #: raised: one bad PDF must not stop the corpus from syncing.
    failed: tuple[tuple[Path, str], ...] = ()
    #: Files still being written; the next pass picks them up.
    pending: tuple[Path, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.ingested or self.removed)


class DocumentIngestor:
    """Reads a file, cuts it into snippets, stores both, and hands the
    snippets to the index. `index` is `None` when retrieval is off — the
    document is stored either way and `SnippetIndex.ensure_indexed` catches it
    up later."""

    def __init__(
        self,
        store: ConversationStore,
        index: "SnippetIndex | None" = None,
        *,
        size: int = SNIPPET_CHARS,
        overlap: int = SNIPPET_OVERLAP,
        minimum: int = MIN_SNIPPET_CHARS,
        max_bytes: int = MAX_DOCUMENT_BYTES,
        settle: float = SETTLE_SECONDS,
    ) -> None:
        self._store = store
        self._index = index
        self._size = size
        self._overlap = overlap
        self._minimum = minimum
        self._max_bytes = max_bytes
        self._settle = settle
        #: `path -> (mtime, size)` as of the previous `sync_dir` pass. A file
        #: whose fingerprint has not moved since then is finished being
        #: written, however old or new its timestamp.
        self._seen: dict[Path, tuple[float, int]] = {}

    async def ingest(self, path: Path) -> Ingested:
        path = self._accepted(path)
        extracted = await self._extract(path)
        content_hash = hashlib.sha256(extracted.text.encode("utf-8")).hexdigest()

        existing = await self._store.document_by_hash(content_hash)
        if existing is not None:
            return Ingested(existing, (), reused=True)

        # A path whose content changed is a different document; the one it
        # replaces must not stay searchable.
        stale = await self._store.document_by_path(str(path))
        if stale is not None:
            await self._store.delete_document(stale.id)

        document = Document(
            title=path.name,
            path=str(path),
            media_type=extracted.media_type,
            content_hash=content_hash,
            text=extracted.text,
            char_count=len(extracted.text),
        )
        snippets = self._snippets(document.id, extracted)
        if not snippets:
            raise IngestError(f"{path.name} holds no text to index")
        document.snippet_count = len(snippets)

        await self._store.save_document(document, snippets)
        # After the write, so a cancelled ingest leaves a stored document for
        # `ensure_indexed` to finish rather than vectors with no document.
        if self._index is not None:
            await self._index.index(snippets)
        return Ingested(document, snippets)

    async def sync_dir(self, directory: Path) -> CorpusSync:
        """Bring the store in line with `directory`: ingest what is new or
        changed there, and drop the documents whose file has been deleted.

        The directory is the corpus — what is in it is what is retrievable.
        Only documents ingested *from under it* are ever dropped; a file
        dropped onto the terminal from elsewhere is not the corpus's to
        remove.
        """
        if not directory.is_dir():
            return CorpusSync()
        directory = directory.resolve()

        ingested: list[Ingested] = []
        failed: list[tuple[Path, str]] = []
        pending: list[Path] = []
        on_disk: set[Path] = set()
        now = time.time()

        for path in sorted(directory.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
                continue
            path = path.resolve()
            on_disk.add(path)
            try:
                stat = path.stat()
            except OSError:
                # Deleted between the walk and the stat.
                continue
            fingerprint = (stat.st_mtime, stat.st_size)
            settled = (
                self._seen.get(path) == fingerprint
                or now - stat.st_mtime > self._settle
            )
            self._seen[path] = fingerprint
            if not settled:
                pending.append(path)
                continue
            try:
                result = await self.ingest(path)
            except AgentChatError as error:
                _log.warning("skipping %s: %s", path, error)
                failed.append((path, str(error)))
                continue
            if not result.reused:
                ingested.append(result)

        removed = await self._prune(directory, on_disk)
        for path in set(self._seen) - on_disk:
            del self._seen[path]
        return CorpusSync(
            ingested=tuple(ingested),
            removed=tuple(removed),
            failed=tuple(failed),
            pending=tuple(pending),
        )

    async def _prune(self, directory: Path, on_disk: set[Path]) -> list[Document]:
        removed: list[Document] = []
        for document in await self._store.list_documents():
            if not document.path:
                continue
            path = Path(document.path)
            if not path.is_relative_to(directory) or path in on_disk:
                continue
            await self._store.delete_document(document.id)
            removed.append(document)
        return removed

    def _accepted(self, path: Path) -> Path:
        path = Path(path).expanduser()
        if path.is_dir():
            raise IngestError(f"{path.name} is a directory, not a document")
        if not path.is_file():
            raise IngestError(f"{path} does not exist")
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            raise IngestError(
                f"{path.name}: {path.suffix or 'no extension'} is not a document "
                f"— accepted: {', '.join(sorted(SUPPORTED_SUFFIXES))}"
            )
        # Checked from the directory entry, before anything is read: a
        # gigabyte-long log must be refused, not decoded and then refused.
        size = path.stat().st_size
        if size > self._max_bytes:
            raise IngestError(
                f"{path.name} is {size // (1024 * 1024)} MB, over the "
                f"{self._max_bytes // (1024 * 1024)} MB limit"
            )
        return path.resolve()

    async def _extract(self, path: Path) -> Extracted:
        try:
            # Off the event loop: a PDF parse is CPU-bound and blocking.
            return await asyncio.to_thread(read_document, path)
        except AgentChatError:
            raise
        except Exception as error:  # noqa: BLE001 — one boundary
            raise IngestError(f"could not read {path.name}: {error}") from error

    def _snippets(self, document_id: str, extracted: Extracted) -> tuple[Snippet, ...]:
        return tuple(
            Snippet(
                document_id=document_id,
                ordinal=ordinal,
                text=extracted.text[span.start : span.end],
                start=span.start,
                end=span.end,
                page=page_of(span.start, extracted.page_starts),
            )
            for ordinal, span in enumerate(
                spans(
                    extracted.text,
                    size=self._size,
                    overlap=self._overlap,
                    minimum=self._minimum,
                )
            )
        )
