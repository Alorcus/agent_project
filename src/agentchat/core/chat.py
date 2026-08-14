"""Chat orchestration: the only thing that knows how a turn is produced.

The UI calls ``stream_reply`` and renders chunks — it knows nothing about
providers, context strategies, or the store.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field, replace

from agentchat.core import usage
from agentchat.core.context import ContextDecision, ContextStrategy, RecencyWindowStrategy
from agentchat.core.delegation import Consultation, DelegationService
from agentchat.core.enrichment import MemoryEnricher
from agentchat.core.errors import StorageError
from agentchat.core.extraction import ExtractionService
from agentchat.core.models import DEFAULT_GROUP_ID, Conversation, ConversationSummary, Group, Message
from agentchat.core.prompts import DEFAULT_SYSTEM, consulted_text, enriched_text
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
    #: The summaries actually sent with this turn — `()` if none matched, or
    #: the enrichment was rebuilt away because it didn't survive trimming.
    enrichment: tuple[ConversationSummary, ...] = ()
    #: The sub-agent consulted for this turn, if any — `None` on a plain turn
    #: or when the consultation didn't survive trimming.
    consultation: Consultation | None = None
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
        extractor: ExtractionService | None = None,
        enricher: MemoryEnricher | None = None,
        delegator: DelegationService | None = None,
    ) -> None:
        self.registry = registry
        self.store = store
        self.context_strategy = context_strategy or RecencyWindowStrategy()
        self.last_turn: TurnResult | None = None
        #: `None` switches extraction off — the only place `summarise` checks
        #: for that, so no flag needs threading through every call site.
        self.extractor = extractor
        #: `None` switches enrichment off, the same way.
        self.enricher = enricher
        #: `None` switches sub-agent consultation off, the same way.
        self.delegator = delegator
        # Held by both `stream_reply` and `summarise`: `TransformersProvider`
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
        # The outgoing conversation's used-memories ledger must not describe
        # the incoming one — this is the one swap MemoryEnricher's own id
        # check cannot see, since it covers re-selecting the *same*
        # conversation from the picker, which is a new visit and so a new
        # session.
        if self.enricher is not None:
            self.enricher.reset()
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

        # Selected before the lock so an idle store lookup never holds up a
        # provider another turn or a summarisation is waiting on.
        selected: tuple[ConversationSummary, ...] = ()
        if self.enricher is not None:
            selected = await self.enricher.select(conversation, user_text)

        # Wraps the whole body, not just the `async for`: a consumer cancelled
        # mid-stream still has to reach the `finally` below and release the
        # lock, or every later `summarise()` on this provider deadlocks.
        async with self._provider_lock:
            # Opened around every generation this turn makes, the sub-agent
            # pipeline's included, so the peak it collects is the turn's and
            # not one call's — and so a background summarisation between
            # turns, which records nothing while no collector is installed,
            # cannot land in a turn's figure.
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
                if consultation is not None:
                    # No enrichment alongside an injected consultation (KTD12):
                    # generations were already spent reaching this point, and
                    # appending both blocks to one user turn would double the
                    # overflow risk the trimming fallback below exists to absorb.
                    selected = ()

                prompt_messages = self._prompt_messages(
                    conversation, selected, consultation
                )
                decision = self.context_strategy.build(
                    prompt_messages, context_window=provider.info.context_window
                )
                if (selected or consultation) and any(
                    m is prompt_messages[-1] for m in decision.dropped
                ):
                    # The injected turn didn't survive trimming whole — rebuild
                    # without it rather than cost the user their own question.
                    # Clearing the injected state here also keeps it from being
                    # marked used / recorded below: nothing was actually sent.
                    selected = ()
                    consultation = None
                    decision = self.context_strategy.build(
                        self._prompt_messages(conversation, ()),
                        context_window=provider.info.context_window,
                    )
                if selected and self.enricher is not None:
                    self.enricher.mark_used(selected)

                reply = conversation.add(
                    Message(role="assistant", content="", model_id=provider.info.id)
                )
                reply.metadata["context"] = {
                    "estimated_tokens": decision.estimated_tokens,
                    "budget": decision.budget,
                    "dropped": len(decision.dropped),
                    "notes": decision.notes,
                }
                # The enrichment is deliberately absent from `metadata`, which is
                # persisted verbatim on every turn (`_save` rewrites every row
                # from `conversation.messages`); `TurnResult.enrichment` is the
                # only place it is recorded. Consultation provenance, unlike
                # enrichment, *is* persisted (KTD8) — written only when the block
                # actually reached the model, so the trimming rollback above
                # leaves no metadata claiming a source the reply never saw.
                if consultation is not None:
                    reply.metadata["subagent"] = {
                        "id": consultation.agent.id,
                        "name": consultation.agent.name,
                        "task": consultation.task,
                        "answer": consultation.answer,
                        "trigger": consultation.trigger,
                    }
                self.last_turn = TurnResult(
                    message=reply,
                    context=decision,
                    enrichment=selected,
                    consultation=consultation,
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
        selected: Sequence[ConversationSummary],
        consultation: Consultation | None = None,
    ) -> list[Message]:
        """The assistant's system prompt followed by `conversation.messages`,
        the last of which — the user turn just added — carries the
        consultation's or `selected`'s material appended to its content when
        there is anything to inject. The two are mutually exclusive (KTD12).
        Nothing in `conversation.messages` is touched, which is what keeps
        both the system prompt and the injection out of the database without
        needing a rule anyone has to remember."""
        system = Message(role="system", content=DEFAULT_SYSTEM)
        last = conversation.messages[-1]
        if consultation is not None:
            content = consulted_text(
                last.content, consultation.agent, consultation.task, consultation.answer
            )
        elif selected:
            content = enriched_text(last.content, selected)
        else:
            return [system, *conversation.messages]
        copy = replace(last, content=content)
        return [system, *conversation.messages[:-1], copy]

    async def summarise(self, conversation: Conversation) -> ConversationSummary | None:
        """Extract and persist a summary for `conversation`, unless there is
        nothing to do: no extractor configured, no messages yet, or no
        messages since the last summary (R7's watermark)."""
        if self.extractor is None or not conversation.messages:
            return None

        existing = await self.store.summary(conversation.id)
        if existing is not None and existing.covered_messages == len(conversation.messages):
            return None

        async with self._provider_lock:
            summary = await self.extractor.run(conversation)
        if existing is not None:
            summary.created_at = existing.created_at

        await self.store.save_summary(summary)
        return summary
