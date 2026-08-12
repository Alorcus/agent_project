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


#: The `;`-joined encoding `ConversationSummary` stores keywords in. Kept
#: local rather than imported from `core.prompts` — that module cleans up
#: after a model's reply, this one reads back what the store itself wrote,
#: and models.py must not depend on prompts.py (prompts.py already depends on
#: models.py for `Message`).
_KEYWORD_SEPARATOR = "; "


@dataclass
class ConversationSummary:
    """Derived state for one conversation: a dense summary and up to five
    keywords, produced by `ExtractionService` and scoped to `group_id` so a
    group's summaries are readable without reading any conversation's
    messages."""

    conversation_id: str
    group_id: str
    summary: str
    keywords: tuple[str, ...] = ()
    #: How many of the conversation's messages this summary was built from —
    #: the watermark that makes re-extraction a no-op when nothing changed.
    covered_messages: int = 0
    #: Which model produced this summary; `None` only if provenance was lost.
    model_id: str | None = None
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)

    @property
    def keywords_text(self) -> str:
        return _KEYWORD_SEPARATOR.join(self.keywords)

    @staticmethod
    def split_keywords(text: str) -> tuple[str, ...]:
        return tuple(piece.strip() for piece in text.split(";") if piece.strip())


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
