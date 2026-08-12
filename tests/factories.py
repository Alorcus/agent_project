"""Builders for test data, shared by the tests that need a group to file a
conversation under."""

from __future__ import annotations

from agentchat.core.models import Conversation, Group, Message


def make_conversation(**overrides) -> Conversation:
    # `default` is the one group the schema seeds, so a conversation that means
    # nothing in particular by its group still satisfies the FK.
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


def make_group(**overrides) -> Group:
    defaults = dict(name="Picker rewrite", kind="project")
    defaults.update(overrides)
    return Group(**defaults)
