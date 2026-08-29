"""Chat orchestration: the only thing that knows how a turn is produced.

The UI calls ``stream_reply`` and renders chunks — it knows nothing about
providers, context strategies, or the store.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field, replace

from agentchat.core import usage
from agentchat.core.context import ContextDecision, ContextStrategy, RecencyWindowStrategy
from agentchat.core.delegation import Consultation, DelegationService
from agentchat.core.errors import StorageError
from agentchat.core.facts import FactExtractor, countable, windows
from agentchat.core.models import (
    DEFAULT_GROUP_ID,
    Conversation,
    Fact,
    Group,
    Message,
)
from agentchat.core.prompts import DEFAULT_SYSTEM, consulted_text, recall_block, recalled_text
from agentchat.core.retrieval import AdaptiveRetriever, Recall
from agentchat.core.usage import TurnUsage
from agentchat.llm import transcript
from agentchat.llm.base import GenerationOptions
from agentchat.llm.registry import ModelRegistry
from agentchat.storage.base import ConversationStore


@dataclass
class TurnResult:
    """Handles to what a turn produced, for the UI to inspect after streaming."""

    message: Message
    context: ContextDecision
    #: The sub-agent consulted for this turn, if any — `None` on a plain turn
    #: or when the consultation didn't survive trimming.
    consultation: Consultation | None = None
    #: What the adaptive retrieval loop produced for this turn, if anything —
    #: `None` on a self-contained turn, when recall is off, when a
    #: consultation suppressed it, or when it didn't survive trimming.
    recall: Recall | None = None
    #: What each of the turn's calls left resident, as the backend counted it.
    #: Still filling in while the reply streams: this object exists before the
    #: reply's prompt has been sent, let alone its completion counted, so
    #: `usage.peak` is only final once the turn is.
    usage: TurnUsage = field(default_factory=TurnUsage)


class ChatService:
    def __init__(
        self,
        registry: ModelRegistry,
        store: ConversationStore,
        context_strategy: ContextStrategy | None = None,
        delegator: DelegationService | None = None,
        fact_extractor: FactExtractor | None = None,
        retriever: AdaptiveRetriever | None = None,
    ) -> None:
        self.registry = registry
        self.store = store
        self.context_strategy = context_strategy or RecencyWindowStrategy()
        self.last_turn: TurnResult | None = None
        #: `None` switches sub-agent consultation off, the same way.
        self.delegator = delegator
        #: `None` switches fact extraction off, the same way.
        self.fact_extractor = fact_extractor
        #: `None` switches adaptive recall off — no embedder load, no LLM
        #: calls, no store reads (R16).
        self.retriever = retriever
        # Held by both `stream_reply` and `extract_facts`: `TransformersProvider`
        # kills one `generate()` call when a second starts on it (KTD7), so
        # only one of the two may run at a time.
        self._provider_lock = asyncio.Lock()

    async def new_conversation(self, group_id: str = DEFAULT_GROUP_ID) -> Conversation:
        """Membership is chosen here and bound for life — there is no move."""
        return Conversation(group_id=group_id)

    async def list_conversations(self, group_id: str) -> list[Conversation]:
        return await self.store.list_conversations(group_id)

    async def list_all_conversations(self) -> list[Conversation]:
        return await self.store.list_all_conversations()

    async def list_groups(self) -> list[Group]:
        return await self.store.list_groups()

    async def create_group(self, name: str) -> Group:
        """The only way a group comes into existence in the application."""
        group = Group(name=name)
        await self.store.save_group(group)
        return group

    async def delete_group(self, group_id: str) -> None:
        """Takes the group's conversations with it — there is no re-homing,
        because there is no move."""
        await self.store.delete_group(group_id)

    async def switch_conversation(self, conversation_id: str) -> Conversation:
        """Resolve an id to the authoritative `Conversation` from the store.

        `store.load` returns a fresh instance, not the one a caller already
        holds — callers must not compare what they hold against what they
        load by identity.
        """
        conversation = await self.store.load(conversation_id)
        if conversation is None:
            raise StorageError(f"No conversation {conversation_id!r}")
        # last_turn describes the outgoing conversation's context-trimming
        # decision; carrying it over would report one conversation's
        # trimming against another's.
        self.last_turn = None
        return conversation

    async def delete_conversation(self, conversation_id: str) -> None:
        await self.store.delete(conversation_id)

    async def persist(self, conversation: Conversation) -> None:
        """Save `conversation`, skipping ones with no messages so an empty
        "New conversation" is never written to the store."""
        if not conversation.messages:
            return
        await self.store.save(conversation)

    async def stream_reply(
        self,
        conversation: Conversation,
        user_text: str,
        options: GenerationOptions | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> AsyncIterator[str]:
        """Append the user turn, then stream the assistant turn, yielding each
        chunk. Cancelling the consumer stops generation but keeps the partial
        reply in history.

        `on_progress`, when given, is called with a short phase label
        whenever the turn enters a phase that produces no output yet —
        routing, consulting a sub-agent — since three of a consulted turn's
        four generations happen before the first streamed chunk.
        """
        conversation.add_user(user_text)
        conversation.autotitle()

        # Before the lock: the index touches the store and the CPU embedder,
        # not the provider, so it must not hold the provider through a cold
        # embedder load on the first turn after start (R5).
        if self.retriever is not None:
            await self.retriever.index.ensure_indexed(conversation.group_id)

        # Wraps the whole body, not just the `async for`: a consumer cancelled
        # mid-stream still has to reach the `finally` below and release the
        # lock, or every later provider call deadlocks.
        async with self._provider_lock:
            # Opened around every generation this turn makes, the sub-agent
            # pipeline's included, so the peak it collects is the turn's and
            # not one call's.
            with usage.collecting() as turn_usage:
                provider = await self.registry.active_provider()

                consultation: Consultation | None = None
                if self.delegator is not None:
                    if on_progress is not None:
                        on_progress("routing…")
                    # `conversation.messages` already ends with this turn's user
                    # message, so the task is written from the exchange it belongs
                    # to rather than from one message with its referents missing.
                    consultation = await self.delegator.consult(
                        user_text, on_progress, history=conversation.messages
                    )

                # No recall alongside a consultation (R19) — and the gate call
                # is not spent either, because `recall` is not entered.
                recall: Recall | None = None
                if consultation is None and self.retriever is not None:
                    recall = await self.retriever.recall(
                        conversation, user_text, on_progress
                    )

                prompt_messages = self._prompt_messages(
                    conversation, consultation, recall
                )
                decision = self.context_strategy.build(
                    prompt_messages, context_window=provider.info.context_window
                )
                if (consultation is not None or recall is not None) and any(
                    m is prompt_messages[-1] for m in decision.dropped
                ):
                    # The injected turn didn't survive trimming whole — rebuild
                    # without it rather than cost the user their own question.
                    # Clearing the injected state here also keeps it from being
                    # recorded below: nothing was actually sent.
                    consultation = None
                    recall = None
                    decision = self.context_strategy.build(
                        self._prompt_messages(conversation, None, None),
                        context_window=provider.info.context_window,
                    )

                reply = conversation.add(
                    Message(role="assistant", content="", model_id=provider.info.id)
                )
                reply.metadata["context"] = {
                    "estimated_tokens": decision.estimated_tokens,
                    "budget": decision.budget,
                    "dropped": len(decision.dropped),
                    "notes": decision.notes,
                }
                # Consultation provenance *is* persisted (KTD8) — written only
                # when the block actually reached the model, so the trimming
                # rollback above leaves no metadata claiming a source the reply
                # never saw.
                if consultation is not None:
                    reply.metadata["subagent"] = {
                        "id": consultation.agent.id,
                        "name": consultation.agent.name,
                        "task": consultation.task,
                        "answer": consultation.answer,
                        "trigger": consultation.trigger,
                    }
                # Recall provenance *is* persisted, like consultation and
                # unlike the old enrichment — and only once the block actually
                # reached the model, so the trimming rollback leaves no
                # metadata claiming a context the reply never saw. `block` is
                # the injected text verbatim, what the UI note replays after a
                # restart when no `Recall` object exists to re-render from.
                if recall is not None:
                    reply.metadata["recall"] = {
                        "rounds": recall.rounds,
                        "exit": recall.exit,
                        "gap": recall.gap,
                        "queries": list(recall.queries),
                        "facts": [
                            {
                                "id": h.fact.id,
                                "conversation_id": h.fact.conversation_id,
                                "text": h.fact.text,
                                "score": round(h.score, 4),
                                "view": h.view,
                            }
                            for h in recall.hits
                        ],
                        "block": recall_block(recall),
                    }
                self.last_turn = TurnResult(
                    message=reply,
                    context=decision,
                    consultation=consultation,
                    recall=recall,
                    usage=turn_usage,
                )

                if on_progress is not None:
                    on_progress("writing the reply…")
                parts: list[str] = []
                try:
                    # Scoped to the generation only, not the persist below — a
                    # cancelled consumer must not carry "chat" across the store
                    # I/O's own await boundaries.
                    with transcript.label("chat"):
                        async for chunk in provider.generate(decision.messages, options):
                            parts.append(chunk)
                            reply.content = "".join(parts)
                            yield chunk
                finally:
                    reply.content = "".join(parts)
                    conversation.touch()
                    await self.persist(conversation)

    def _prompt_messages(
        self,
        conversation: Conversation,
        consultation: Consultation | None = None,
        recall: Recall | None = None,
    ) -> list[Message]:
        """The assistant's system prompt followed by `conversation.messages`,
        the last of which — the user turn just added — carries the
        consultation's or the recall's material appended to its content when
        there is anything to inject. The two are mutually exclusive (R19).
        Nothing in `conversation.messages` is touched, which is what keeps both
        the system prompt and the injection out of the database without needing
        a rule anyone has to remember."""
        system = Message(role="system", content=DEFAULT_SYSTEM)
        last = conversation.messages[-1]
        if consultation is not None:
            content = consulted_text(
                last.content, consultation.agent, consultation.task, consultation.answer
            )
        elif recall is not None:
            content = recalled_text(last.content, recall)
        else:
            return [system, *conversation.messages]
        copy = replace(last, content=content)
        return [system, *conversation.messages[:-1], copy]

    async def pending_fact_windows(
        self, conversation: Conversation, *, flush: bool = False
    ) -> tuple[tuple[int, int], ...]:
        """Which windows `extract_facts` would run. The UI's cheap probe: one
        indexed row read, no provider."""
        if self.fact_extractor is None:
            return ()
        covered = await self.store.fact_watermark(conversation.id)
        return windows(covered, len(countable(conversation.messages)), flush=flush)

    async def extract_facts(self, conversation: Conversation, *, flush: bool = False) -> tuple[Fact, ...]:
        """Extract and persist one fact per due window. `()` when there is no
        extractor and when nothing is due."""
        ranges = await self.pending_fact_windows(conversation, flush=flush)
        if not ranges:
            return ()

        items = countable(conversation.messages)
        results: list[Fact] = []
        for start, end in ranges:
            async with self._provider_lock:
                fact = await self.fact_extractor.extract(items[start:end])
            if fact is not None:
                fact.conversation_id = conversation.id
                fact.group_id = conversation.group_id
                fact.window_start = start
                fact.window_end = end
                await self.store.save_facts([fact])
                if self.retriever is not None:
                    # Inside the loop, not after it: a cancelled backlog must
                    # leave every fact it saved indexed, the same invariant
                    # the per-window watermark already carries (R2).
                    await self.retriever.index.index([fact])
                results.append(fact)
            # The watermark advances even on a barren window — a window that
            # produced nothing cannot change, so re-running it buys the same
            # nothing at the same price. Saved one window at a time, so a
            # cancelled backlog keeps every window it finished.
            await self.store.set_fact_watermark(conversation.id, end)
        return tuple(results)
