"""From a user message to a `Consultation`, with every failure already
handled.

Depth is enforced structurally, not by a counter: `_agent_messages` builds
exactly two messages — the specialist's own system prompt and its task — and
nothing reachable from them is another `DelegationService`, so a sub-agent has
nothing to delegate *with*.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from agentchat.core.agents import DEFAULT_AGENT_ID, SubAgent, default_agents, find_mention
from agentchat.core.errors import AgentChatError, DelegationError
from agentchat.core.models import Message
from agentchat.core.prompts import (
    ROUTER_PROMPT,
    ROUTER_SYSTEM,
    TASK_PROMPT,
    TASK_SYSTEM,
    parse_agent_id,
    parse_task,
    render_transcript,
    roster_text,
)
from agentchat.llm import transcript
from agentchat.llm.base import GenerationOptions, complete
from agentchat.llm.registry import ModelRegistry

_log = logging.getLogger(__name__)

ROUTE_MAX_TOKENS = 8  # one name and nothing else
TASK_MAX_TOKENS = 96
#: Head-room for the instruction text around the transcript in call T.
#: `TASK_SYSTEM` alone is ~300 tokens once its worked example is counted.
TASK_PROMPT_OVERHEAD_TOKENS = 512
#: A floor under the task transcript budget, so a tiny context window still
#: leaves the last turn something to be written from.
MIN_TASK_TRANSCRIPT_BUDGET = 256
#: The specialist's answer becomes prompt material for the reply, so its
#: length is a context-budget decision, not a quality one.
ANSWER_MAX_TOKENS = 512
#: Temperature 0, thinking off, for all three calls. Thinking on would put a
#: reasoning preamble where the name is supposed to be — and would inject one
#: verbatim into the reply's prompt.
_CONSULTATION_OPTIONS_BASE = dict(temperature=0.0, thinking=False)

Trigger = Literal["router", "mention"]


@dataclass(frozen=True)
class Consultation:
    agent: SubAgent
    task: str
    answer: str
    trigger: Trigger


@dataclass(frozen=True)
class Pending:
    """A routed agent and its authored task, before the specialist has run —
    calls R and T done, call S not yet."""

    agent: SubAgent
    task: str
    trigger: Trigger


class DelegationService:
    def __init__(
        self,
        registry: ModelRegistry,
        agents: Sequence[SubAgent] | None = None,
        *,
        timeout: float = 60.0,
    ) -> None:
        self._registry = registry
        self.agents = tuple(agents) if agents is not None else default_agents()
        self._timeout = timeout

    async def consult(
        self,
        user_text: str,
        on_progress: Callable[[str], None] | None = None,
        *,
        history: Sequence[Message] = (),
    ) -> Consultation | None:
        """Route `user_text`, write the task, and run the specialist. `None`
        means answer normally — including whenever anything went wrong.

        `history` is the conversation `user_text` was typed into, its own turn
        included and last; call T reads it so a follow-up question can be
        restated whole for a specialist who will never see what it refers
        back to. Empty means `user_text` stands alone.

        The two phases get `_timeout` each, not one budget between them:
        routing and task-authoring are short calls, the specialist's answer
        is not."""
        try:
            pending = await asyncio.wait_for(
                self._decide(user_text, on_progress, history), self._timeout
            )
        except (AgentChatError, TimeoutError) as error:
            _log.warning("sub-agent routing failed: %s", error)
            return None
        if pending is None:
            return None

        try:
            return await asyncio.wait_for(self._run(pending, on_progress), self._timeout)
        except (AgentChatError, TimeoutError) as error:
            _log.warning("sub-agent %s failed: %s", pending.agent.id, error)
            return None

    async def _decide(
        self,
        user_text: str,
        on_progress: Callable[[str], None] | None,
        history: Sequence[Message] = (),
    ) -> Pending | None:
        mentioned = find_mention(user_text, self.agents)
        if mentioned is not None:
            if mentioned == DEFAULT_AGENT_ID:
                return None
            agent = self._agent(mentioned)
            trigger: Trigger = "mention"
        else:
            agent_id = await self._route(user_text)
            if agent_id is None:
                return None
            agent = self._agent(agent_id)
            trigger = "router"

        task = await self._write_task(agent, user_text, history)
        return Pending(agent=agent, task=task, trigger=trigger)

    async def _run(
        self, pending: Pending, on_progress: Callable[[str], None] | None
    ) -> Consultation:
        if on_progress is not None:
            on_progress(f"consulting {pending.agent.name}…")
        answer = await self._run_agent(pending.agent, pending.task)
        return Consultation(
            agent=pending.agent, task=pending.task, answer=answer, trigger=pending.trigger
        )

    def _agent(self, agent_id: str) -> SubAgent:
        return next(a for a in self.agents if a.id == agent_id)

    async def _route(self, user_text: str) -> str | None:
        provider = await self._registry.active_provider()
        with transcript.label("route"):
            reply = await complete(
                provider,
                [
                    Message(role="system", content=ROUTER_SYSTEM),
                    Message(
                        role="user",
                        content=ROUTER_PROMPT.format(
                            roster=roster_text(self.agents), message=user_text
                        ),
                    ),
                ],
                GenerationOptions(
                    max_tokens=ROUTE_MAX_TOKENS, stop=("\n",), **_CONSULTATION_OPTIONS_BASE
                ),
            )
        return parse_agent_id(reply, agent_ids=[a.id for a in self.agents])

    async def _write_task(
        self, agent: SubAgent, user_text: str, history: Sequence[Message] = ()
    ) -> str:
        provider = await self._registry.active_provider()
        budget = max(
            MIN_TASK_TRANSCRIPT_BUDGET,
            provider.info.context_window - TASK_MAX_TOKENS - TASK_PROMPT_OVERHEAD_TOKENS,
        )
        messages = list(history) or [Message(role="user", content=user_text)]
        rendered = render_transcript(messages, budget=budget)
        with transcript.label("subagent.task"):
            reply = await complete(
                provider,
                [
                    Message(role="system", content=TASK_SYSTEM),
                    Message(
                        role="user",
                        content=TASK_PROMPT.format(
                            name=agent.id, purpose=agent.purpose, transcript=rendered
                        ),
                    ),
                ],
                GenerationOptions(max_tokens=TASK_MAX_TOKENS, **_CONSULTATION_OPTIONS_BASE),
            )
        return parse_task(reply, fallback=user_text)

    async def _run_agent(self, agent: SubAgent, task: str) -> str:
        provider = await self._registry.active_provider()
        with transcript.label(f"subagent.{agent.id}"):
            reply = await complete(
                provider,
                self._agent_messages(agent, task),
                GenerationOptions(max_tokens=ANSWER_MAX_TOKENS, **_CONSULTATION_OPTIONS_BASE),
            )
        answer = reply.strip()
        if not answer:
            raise DelegationError(f"{agent.id} returned an empty answer")
        return answer

    def _agent_messages(self, agent: SubAgent, task: str) -> list[Message]:
        """Two messages, no roster, no history — this method *is* R4 and R7.
        Private: nothing outside this class has a reason to build a
        specialist's context."""
        return [
            Message(role="system", content=agent.system_prompt),
            Message(role="user", content=task),
        ]
