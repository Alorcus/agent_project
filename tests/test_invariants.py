"""One test per invariant of `docs/conversation-groups.md` § 1.7.

Kept apart from `test_storage.py` because these assert a property the design
names and numbers, not the behaviour of one implementation — I-1 is claimed by
the type *and* the schema, and both halves are checked here.
"""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest

from agentchat.core.errors import StorageError
from agentchat.core.models import DEFAULT_GROUP_ID, Conversation
from agentchat.storage.sqlite import SqliteStore

from factories import make_conversation


async def test_i1_conversation_requires_a_group(tmp_path: Path):
    typed = {f.name: f.type for f in fields(Conversation)}
    assert typed["group_id"] == "str"

    store = SqliteStore(tmp_path / "chat.db")
    with pytest.raises(StorageError):
        await store.save(make_conversation(group_id=None))


async def test_the_default_group_always_exists(tmp_path: Path):
    """Seeded with the schema, so no code path has to handle its absence."""
    store = SqliteStore(tmp_path / "chat.db")

    default = await store.default_group()

    assert default.id == DEFAULT_GROUP_ID
    assert default.kind == "default"
