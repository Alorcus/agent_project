"""Domain objects: plain dataclasses with no persistence or UI knowledge."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

Role = Literal["system", "user", "assistant"]

#: The group the schema seeds and every conversation lands in unless another
#: is chosen at creation. It always exists, so no code path handles its absence.
DEFAULT_GROUP_ID = "default"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid.uuid4().hex


@dataclass
class Group:
    """The container a conversation belongs to, and the unit downstream
    features scope by."""

    id: str = field(default_factory=_new_id)
    name: str = "New group"
    kind: Literal["default", "project"] = "project"
    created_at: datetime = field(default_factory=_now)

    def is_project(self) -> bool:
        """The single place `kind` is branched on, so callers never spell out
        a `kind == "default"` comparison of their own."""
        return self.kind != "default"


@dataclass
class Message:
    """A single turn."""

    role: Role
    content: str = ""
    id: str = field(default_factory=_new_id)
    created_at: datetime = field(default_factory=_now)
    #: Which model/adapter produced an assistant message.
    model_id: str | None = None
    #: Free-form provenance: retrieved sources, sub-agent ids, context decisions.
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.content.strip()


@dataclass(frozen=True)
class Author:
    """Who wrote the words a phrase quotes — not who asserted the claim a
    fact built from it makes."""

    kind: Literal["user", "model"]
    label: str

    @property
    def is_user(self) -> bool:
        return self.kind == "user"

    @staticmethod
    def of(message: Message) -> "Author":
        if message.role == "user":
            return Author("user", "user")
        return Author("model", message.model_id or "unknown")


@dataclass(frozen=True)
class Phrase:
    """A span of one message, in that message's original coordinates, plus the
    verbatim substring it covers — denormalised so the evidence view can be
    built without loading the source message."""

    message_id: str
    start: int
    end: int
    author: Author
    #: `message.content[start:end]` at the time the phrase was anchored.
    #: Empty for phrases written before plan 010.
    quote: str = ""

    def text(self, message: Message) -> str:
        assert message.id == self.message_id, (
            f"Phrase for message {self.message_id!r} sliced against {message.id!r}"
        )
        return message.content[self.start : self.end]


@dataclass(frozen=True)
class FactEmbedding:
    """One view of one fact, embedded by one model. `dim` and `created_at` are
    filled in by the store at write time."""

    fact_id: str
    view: Literal["claim", "evidence"]
    model_id: str
    vector: tuple[float, ...]


@dataclass
class Fact:
    """A claim grounded in one or more `Phrase`s, produced by `FactExtractor`
    from one sliding window of a conversation."""

    id: str = field(default_factory=_new_id)
    conversation_id: str = ""
    group_id: str = ""
    text: str = ""
    phrases: tuple[Phrase, ...] = ()
    #: Half-open range of countable message indices this came from.
    window_start: int = 0
    window_end: int = 0
    model_id: str | None = None
    created_at: datetime = field(default_factory=_now)

    @property
    def message_ids(self) -> tuple[str, ...]:
        seen: list[str] = []
        for phrase in self.phrases:
            if phrase.message_id not in seen:
                seen.append(phrase.message_id)
        return tuple(seen)


@dataclass
class Document:
    """A file the user dropped in, as ingestion read it. Its identity is
    `content_hash`, not its path: the same bytes under two names are one
    document, and an edited file is a different one."""

    id: str = field(default_factory=_new_id)
    title: str = ""
    #: Absolute path at ingest time — provenance, not identity.
    path: str = ""
    media_type: Literal["text", "pdf"] = "text"
    content_hash: str = ""
    #: The extracted text a `Snippet`'s offsets index into. Empty on rows from
    #: `list_documents`; only `store.document()` fills it in.
    text: str = ""
    char_count: int = 0
    snippet_count: int = 0
    created_at: datetime = field(default_factory=_now)


@dataclass(frozen=True)
class Snippet:
    """One span of a document's text, plus the substring it covers —
    denormalised the way `Phrase` is, so retrieval never reloads the
    document to render a hit."""

    id: str = field(default_factory=_new_id)
    document_id: str = ""
    ordinal: int = 0
    text: str = ""
    start: int = 0
    end: int = 0
    #: 1-based page for a paginated document, `None` for plain text.
    page: int | None = None


@dataclass(frozen=True)
class SnippetEmbedding:
    """One snippet embedded by one model. `dim` and `created_at` are filled in
    by the store at write time."""

    snippet_id: str
    model_id: str
    vector: tuple[float, ...]


@dataclass
class Conversation:
    """An ordered list of messages."""

    id: str = field(default_factory=_new_id)
    title: str = "New conversation"
    #: NOT NULL at the schema level (I-1) and chosen once at creation — every
    #: conversation belongs to a group, even if only the seeded default one.
    group_id: str = DEFAULT_GROUP_ID
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)
    messages: list[Message] = field(default_factory=list)

    def add(self, message: Message) -> Message:
        self.messages.append(message)
        self.updated_at = _now()
        return message

    def add_user(self, content: str) -> Message:
        return self.add(Message(role="user", content=content))

    def touch(self) -> None:
        self.updated_at = _now()

    def autotitle(self) -> None:
        """Derive a title from the first user turn, once."""
        if self.title != "New conversation":
            return
        first = next((m for m in self.messages if m.role == "user"), None)
        if first is None:
            return
        text = " ".join(first.content.split())
        self.title = text[:40] + ("…" if len(text) > 40 else "")
