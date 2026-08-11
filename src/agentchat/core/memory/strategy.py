"""Chat-side adapters and the batching decision. The one file in
``core/memory/`` licensed to import chat types (I-6 exempts it): everything
else in this package must stay ignorant of ``Message``/``Conversation`` so it
can bind to a future persona pipeline instead.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime

from agentchat.core.context import MIN_CONTEXT_BUDGET, ContextDecision, ContextStrategy
from agentchat.core.memory.embed import reembed
from agentchat.core.memory.extract import MemoryExtractor
from agentchat.core.memory.models import MemoryFragment
from agentchat.core.memory.store import ApplyResult, MemoryStore
from agentchat.core.memory.tuning import Tuning
from agentchat.core.memory.types import EmbeddingProvider
from agentchat.core.models import Conversation, Message
from agentchat.core.tokens import estimate_tokens
from agentchat.llm.base import LLMProvider

ProviderSupplier = Callable[[], Awaitable[LLMProvider]]

CORE_BLOCK_TITLE = "Established context for this project:"
RECALL_BLOCK_TITLE = "Possibly relevant to the latest turn:"


class MessageEvidence:
    """``Message -> EvidenceItem``. ``EvidenceItem`` has no role field — a
    chat turn's role and an email's sender occupy the same slot — so the role
    rides into ``text`` alongside the content."""

    def __init__(self, message: Message) -> None:
        self.id = message.id
        # Blank stays blank rather than becoming "assistant: " — a cancelled
        # reply's empty turn must still read as blank to `observe`'s filter.
        self.text = f"{message.role}: {message.content}" if message.content.strip() else ""
        self.created_at = message.created_at


class ConversationEvidence:
    """``Conversation -> EvidenceSource``: the messages after the watermark,
    or every message when ``ignore_watermark`` is set (backfill — the same
    path, just a null watermark)."""

    def __init__(self, conversation: Conversation, *, ignore_watermark: bool = False) -> None:
        self.id = conversation.id
        self.scope_id = conversation.group_id

        after: tuple[datetime, str] | None = None
        if not ignore_watermark and conversation.extracted_at is not None:
            after = (conversation.extracted_at, conversation.extracted_id)

        messages = conversation.messages
        if after is not None:
            # The lexicographic range § 2.1 spends a paragraph justifying:
            # `id` breaks ties between messages created in the same instant.
            messages = [m for m in messages if (m.created_at, m.id) > after]

        self.items: tuple[MessageEvidence, ...] = tuple(MessageEvidence(m) for m in messages)
        # What was *read*, blank turns included — not what was claimed, or a
        # cancelled reply at the end of a batch is re-read forever.
        self.read_through: tuple[datetime, str] | None = (
            (messages[-1].created_at, messages[-1].id) if messages else None
        )


class ChatMemory:
    """Owns adapting a conversation into evidence and running extraction over
    it. ``ChatService`` owns the scheduling/batching around that."""

    def __init__(
        self,
        store: MemoryStore,
        provider_for: ProviderSupplier,
        tuning: Tuning | None = None,
        *,
        encoder: EmbeddingProvider | None = None,
    ) -> None:
        self._store = store
        self._provider_for = provider_for
        self._tuning = tuning or Tuning.from_env()
        self._encoder = encoder

    def pending(self, conversation: Conversation) -> int:
        return len(ConversationEvidence(conversation).items)

    def due(self, conversation: Conversation) -> bool:
        return self.pending(conversation) >= self._tuning.extract_every

    async def extract(
        self,
        conversation: Conversation,
        *,
        thinking: bool = False,
        ignore_watermark: bool = False,
    ) -> ApplyResult | None:
        source = ConversationEvidence(conversation, ignore_watermark=ignore_watermark)
        # The resident provider, not a model of extraction's own — a
        # background run must never evict the model the user is talking to.
        provider = await self._provider_for()
        extractor = MemoryExtractor(self._store, provider, self._tuning, encoder=self._encoder)
        result = await extractor.run(source, thinking=thinking)
        if result is not None:
            # The store row is authoritative; without this the live object
            # the UI holds stays stale and the next batch re-reads the range.
            conversation.extracted_at, conversation.extracted_id = source.read_through
        if self._encoder is not None:
            # A bounded catch-up pass, off the reply path: a group stage 2
            # left behind (or one whose encoder changed) repairs itself over
            # a few background extractions rather than a blocking migration.
            reembed(self._store, self._encoder, tuning=self._tuning)
        return result


def render_block(title: str, fragments: Sequence[MemoryFragment]) -> str:
    lines = [title] + [f"- ({fragment.kind}) {fragment.text}" for fragment in fragments]
    return "\n".join(lines)


class GroupMemoryStrategy:
    """Wraps another `ContextStrategy`, splicing in a group's stable core and
    the fragments the latest turn's query selects (§ 2.2)."""

    name = "group-memory"

    def __init__(self, *, inner: ContextStrategy, store: MemoryStore, tuning: Tuning | None = None) -> None:
        self._inner = inner
        self._store = store
        self._tuning = tuning or Tuning.from_env()

    def build(
        self,
        messages: Sequence[Message],
        *,
        context_window: int,
        reserve_for_response: int = 512,
        conversation: Conversation | None = None,
    ) -> ContextDecision:
        scope = self._store.memory_scope(conversation.group_id) if conversation is not None else None
        if scope is None:
            return self._inner.build(
                messages,
                context_window=context_window,
                reserve_for_response=reserve_for_response,
                conversation=conversation,
            )

        budget = max(MIN_CONTEXT_BUDGET, context_window - reserve_for_response)
        core = list(
            self._store.stable_core(scope, int(context_window * self._tuning.core_budget_fraction))
        )
        query = _latest_user_text(messages)
        selected = (
            list(
                self._store.select(
                    scope, query, int(context_window * self._tuning.sel_budget_fraction)
                )
            )
            if query
            else []
        )

        core_block, selected_block, spent = _blocks(core, selected)
        inner_decision = self._inner.build(
            messages,
            context_window=max(MIN_CONTEXT_BUDGET, context_window - spent),
            reserve_for_response=reserve_for_response,
            conversation=conversation,
        )

        system = [m for m in inner_decision.messages if m.role == "system"]
        rest = [m for m in inner_decision.messages if m.role != "system"]
        latest = rest[-1:]
        history = rest[:-1]
        dropped = list(inner_decision.dropped)

        while True:
            core_block, selected_block, _ = _blocks(core, selected)
            spliced = [*system]
            if core_block is not None:
                spliced.append(Message(role="system", content=core_block))
            spliced.extend(history)
            if selected_block is not None:
                spliced.append(Message(role="system", content=selected_block))
            spliced.extend(latest)
            total = sum(estimate_tokens(m.content) for m in spliced)

            if total <= budget or not (history or selected or core):
                break
            # § 2.2's fit order: oldest history first, then the selected
            # tail, then the core tail — the volatile parts go before the
            # cacheable prefix loses anything.
            if history:
                dropped.append(history.pop(0))
            elif selected:
                selected.pop()
            else:
                core.pop()

        return ContextDecision(
            messages=spliced,
            dropped=dropped,
            estimated_tokens=total,
            budget=budget,
            notes=[*inner_decision.notes, f"strategy={self.name}", f"recalled={len(selected)} core={len(core)}"],
            recalled=selected,
            core=core,
        )


def _blocks(
    core: Sequence[MemoryFragment], selected: Sequence[MemoryFragment]
) -> tuple[str | None, str | None, int]:
    core_block = render_block(CORE_BLOCK_TITLE, core) if core else None
    selected_block = render_block(RECALL_BLOCK_TITLE, selected) if selected else None
    spent = sum(estimate_tokens(block) for block in (core_block, selected_block) if block is not None)
    return core_block, selected_block, spent


def _latest_user_text(messages: Sequence[Message]) -> str:
    return next((m.content for m in reversed(messages) if m.role == "user"), "")
