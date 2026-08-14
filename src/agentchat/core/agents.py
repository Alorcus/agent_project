"""The sub-agent roster: what a specialist *is*, which ones ship, and how a
`@mention` resolves to one of them."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

#: The roster entry that means "no specialist". Rendered into the router
#: prompt like any other, but resolves to no `SubAgent`.
DEFAULT_AGENT_ID = "default"

_MENTION = re.compile(r"^@([a-z0-9_]+)\b", re.IGNORECASE)


@dataclass(frozen=True)
class SubAgent:
    id: str
    name: str
    #: The trigger phrase shown to the router — what this agent is *for*, in
    #: the words a user would use, not a personality blurb.
    purpose: str
    system_prompt: str


def default_agents() -> tuple[SubAgent, ...]:
    """The shipped roster: three entries, enough to prove an N-way choice
    rather than a binary one."""
    return (
        SubAgent(
            id="ask_chef",
            name="Chef",
            purpose="cooking, recipes, ingredients, kitchen technique, meal planning",
            system_prompt=(
                "You are a professional chef advising another assistant. You "
                "cannot see the conversation the task came from — answer only "
                "what the task below states, restating nothing you were not "
                "told. You are writing for that assistant, not for the end "
                "user, so be dense and skip pleasantries: no greeting, no "
                "sign-off, just the culinary substance."
            ),
        ),
        SubAgent(
            id="ask_bank",
            name="Banking",
            purpose="personal banking, interest rates, loans, mortgages, budgeting",
            system_prompt=(
                "You are a personal banking specialist advising another "
                "assistant. You cannot see the conversation the task came "
                "from — answer only what the task below states, restating "
                "nothing you were not told. You are writing for that "
                "assistant, not for the end user, so be dense and skip "
                "pleasantries: no greeting, no sign-off, just the financial "
                "substance."
            ),
        ),
        SubAgent(
            id="ask_tutor",
            name="Tutor",
            purpose="explaining a concept step by step, teaching, worked examples",
            system_prompt=(
                "You are a patient tutor advising another assistant. You "
                "cannot see the conversation the task came from — answer only "
                "what the task below states, restating nothing you were not "
                "told. You are writing for that assistant, not for the end "
                "user, so be dense and skip pleasantries: no greeting, no "
                "sign-off, just the explanation, broken into clear steps."
            ),
        ),
    )


def find_mention(text: str, agents: Sequence[SubAgent]) -> str | None:
    """The `@id` a message opens with, if it names a roster entry or
    `default`. Returns the id — including `DEFAULT_AGENT_ID`, which the
    caller reads as "consult nobody". `None` for anything else, including a
    typo, so it falls through to normal routing rather than failing the turn.
    Does not modify `text`."""
    match = _MENTION.match(text.strip())
    if match is None:
        return None
    candidate = match.group(1).lower()
    ids = {DEFAULT_AGENT_ID, *(agent.id for agent in agents)}
    return candidate if candidate in ids else None
