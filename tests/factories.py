"""Builders for memory test data: object graphs, plus `write` to persist one.

`write` is the only part that touches a store, and it goes through
`MemoryStore.apply` rather than around it — so a test that builds a hierarchy
by hand still exercises the I-7 and I-8 paths a real extraction run would.
"""

from __future__ import annotations

import json
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from agentchat.core.models import Conversation, Message

if TYPE_CHECKING:
    from agentchat.core.memory.models import FragmentCitation, Group, MemoryFragment


def make_conversation(**overrides) -> Conversation:
    # Spelled out rather than imported from `core.models`: this module is
    # imported for its object builders by tests that must stay collectable
    # before `DEFAULT_GROUP_ID` exists. It is the one group the schema seeds,
    # so a conversation that means nothing by its group satisfies the FK.
    defaults = dict(
        title="Trip planning",
        group_id="default",
        messages=[
            Message(role="user", content="Where should I go?"),
            Message(role="assistant", content="Try Kyoto.", model_id="qwen"),
        ],
    )
    defaults.update(overrides)
    return Conversation(**defaults)


def make_message(**overrides) -> Message:
    defaults = dict(role="user", content="Let's use SQLite for storage.")
    defaults.update(overrides)
    return Message(**defaults)


# The memory dataclasses are imported per call rather than at module level:
# test_storage.py takes make_conversation from here and has to stay collectable
# whether or not core/memory/ exists yet.


def make_group(**overrides) -> Group:
    from agentchat.core.memory.models import Group

    defaults = dict(name="Picker rewrite", kind="project")
    defaults.update(overrides)
    return Group(**defaults)


def make_fragment(**overrides) -> MemoryFragment:
    from agentchat.core.memory.models import MemoryFragment

    defaults = dict(
        group_id="g1",
        text="We chose SQLite for storage.",
        kind="fact",
    )
    defaults.update(overrides)
    return MemoryFragment(**defaults)


def make_citation(
    fragment: MemoryFragment,
    *,
    message: Message | None = None,
    source: MemoryFragment | None = None,
    quote: str | None = None,
) -> FragmentCitation:
    from agentchat.core.memory.models import FragmentCitation

    return FragmentCitation(
        fragment_id=fragment.id,
        source_message_id=message.id if message is not None else None,
        source_fragment_id=source.id if source is not None else None,
        quote=quote,
    )


@dataclass
class MemoryGraph:
    """The five row-sets stage 1's writer will insert, flat."""

    group: Group
    conversations: list[Conversation] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)
    fragments: list[MemoryFragment] = field(default_factory=list)
    citations: list[FragmentCitation] = field(default_factory=list)


class GraphBuilder:
    """Assembles a group → conversations → messages → fragments → citations
    graph. Fragment ids are provisional sequential ints; stage 1's writer
    inserts them explicitly so citation rows stay valid."""

    def __init__(self, group: Group | None = None) -> None:
        self.group = group if group is not None else make_group()
        self.conversations: list[Conversation] = []
        self.messages: list[Message] = []
        self.fragments: list[MemoryFragment] = []
        self.citations: list[FragmentCitation] = []
        self._next_fragment_id = 1

    def conversation(self, **overrides) -> Conversation:
        overrides.setdefault("group_id", self.group.id)
        overrides.setdefault("messages", [])
        conversation = make_conversation(**overrides)
        self.conversations.append(conversation)
        return conversation

    def message(self, conversation: Conversation, **overrides) -> Message:
        message = conversation.add(make_message(**overrides))
        self.messages.append(message)
        return message

    def extracted(self, *sources: Message, **overrides) -> MemoryFragment:
        fragment = self._fragment(consolidated=False, **overrides)
        for message in sources:
            self.citations.append(make_citation(fragment, message=message))
        return fragment

    def consolidated(self, *sources: MemoryFragment, **overrides) -> MemoryFragment:
        fragment = self._fragment(consolidated=True, **overrides)
        for source in sources:
            self.citations.append(make_citation(fragment, source=source))
        return fragment

    def build(self) -> MemoryGraph:
        return MemoryGraph(
            group=self.group,
            conversations=list(self.conversations),
            messages=list(self.messages),
            fragments=list(self.fragments),
            citations=list(self.citations),
        )

    def _fragment(self, *, consolidated: bool, **overrides) -> MemoryFragment:
        overrides.setdefault("group_id", self.group.id)
        overrides.setdefault("consolidated", consolidated)
        overrides.setdefault("id", self._next_fragment_id)
        self._next_fragment_id = max(self._next_fragment_id, overrides["id"]) + 1
        fragment = make_fragment(**overrides)
        self.fragments.append(fragment)
        return fragment


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def write(store, graph: MemoryGraph) -> None:
    """Persist `graph`: group, conversation and message rows directly,
    fragments and citations through `store.apply` — the only write path memory
    state has. Idempotent, so the same graph can be written twice."""
    from agentchat.core.memory.store import FragmentWrite
    from agentchat.storage.schema import connect

    with closing(connect(store.path)) as conn, conn:
        conn.execute(
            "INSERT INTO groups (id, name, kind, last_consolidated_at, created_at)"
            " VALUES (?, ?, ?, ?, ?) ON CONFLICT(id) DO NOTHING",
            (
                graph.group.id,
                graph.group.name,
                graph.group.kind,
                _iso(graph.group.last_consolidated_at),
                graph.group.created_at.isoformat(),
            ),
        )
        for conversation in graph.conversations:
            conn.execute(
                "INSERT INTO conversations (id, title, group_id, extracted_at,"
                " extracted_id, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET"
                " title = excluded.title, updated_at = excluded.updated_at",
                (
                    conversation.id,
                    conversation.title,
                    conversation.group_id,
                    _iso(conversation.extracted_at),
                    conversation.extracted_id,
                    conversation.created_at.isoformat(),
                    conversation.updated_at.isoformat(),
                ),
            )
            for ordinal, message in enumerate(conversation.messages):
                conn.execute(
                    "INSERT INTO messages (id, conversation_id, ordinal, role,"
                    " content, created_at, model_id, metadata)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO NOTHING",
                    (
                        message.id,
                        conversation.id,
                        ordinal,
                        message.role,
                        message.content,
                        message.created_at.isoformat(),
                        message.model_id,
                        json.dumps(message.metadata),
                    ),
                )

    by_fragment: dict[int | None, list[FragmentCitation]] = {}
    for citation in graph.citations:
        by_fragment.setdefault(citation.fragment_id, []).append(citation)
    store.apply(
        [FragmentWrite(fragment, by_fragment.get(fragment.id, [])) for fragment in graph.fragments]
    )
