"""Domain objects: plain dataclasses with no persistence or UI knowledge."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

Role = Literal["system", "user", "assistant"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid.uuid4().hex


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


@dataclass
class Conversation:
    """An ordered list of messages."""

    id: str = field(default_factory=_new_id)
    title: str = "New conversation"
    #: Project-folder handle; unused today, reserved for scoping memory recall.
    group_id: str | None = None
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
