"""Builders for memory test data — object graphs only, no store involved.

Stage 1 adds ``write(store, graph)`` here once ``apply()`` exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agentchat.core.models import Conversation, Message

if TYPE_CHECKING:
    from agentchat.core.memory.models import FragmentCitation, Group, MemoryFragment


def make_conversation(**overrides) -> Conversation:
    defaults = dict(
        title="Trip planning",
        group_id="g1",
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
