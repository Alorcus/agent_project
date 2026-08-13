"""Two-call summarisation: a transcript in, a `ConversationSummary` out.

Knows nothing about storage — `run` returns an object and the caller persists
it, which keeps the two LLM calls outside any transaction and keeps this
class testable against a scripted provider alone.
"""

from __future__ import annotations

from agentchat.core.errors import ExtractionError
from agentchat.core.models import Conversation, ConversationSummary, Message
from agentchat.core.prompts import (
    KEYWORDS_PROMPT,
    KEYWORDS_SYSTEM,
    SUMMARY_PROMPT,
    SUMMARY_SYSTEM,
    parse_keywords,
    render_transcript,
)
from agentchat.llm import transcript as llm_transcript
from agentchat.llm.base import GenerationOptions, complete
from agentchat.llm.registry import ModelRegistry

SUMMARY_MAX_TOKENS = 256
KEYWORDS_MAX_TOKENS = 64
#: Head-room for the instruction text around the transcript. `SUMMARY_SYSTEM`
#: alone is ~350 tokens once its worked example is counted.
PROMPT_OVERHEAD_TOKENS = 512
#: A floor under the transcript budget so a tiny context window still leaves
#: room for something to summarise.
MIN_TRANSCRIPT_BUDGET = 256

#: Every call in this module runs at temperature 0 with thinking off: the
#: same conversation must summarise the same way twice (KTD6), and with
#: thinking on, a reasoning preamble would be stored as the summary itself.
_EXTRACTION_OPTIONS_BASE = dict(temperature=0.0, thinking=False)


class ExtractionService:
    def __init__(self, registry: ModelRegistry) -> None:
        self._registry = registry

    async def run(self, conversation: Conversation) -> ConversationSummary:
        """Summarise `conversation` in two calls: summary, then keywords
        derived from the summary — never from the transcript directly."""
        provider = await self._registry.active_provider()
        budget = max(
            MIN_TRANSCRIPT_BUDGET,
            provider.info.context_window - SUMMARY_MAX_TOKENS - PROMPT_OVERHEAD_TOKENS,
        )
        transcript = render_transcript(conversation.messages, budget=budget)

        with llm_transcript.label("extraction.summary"):
            summary_text = await complete(
                provider,
                [
                    Message(role="system", content=SUMMARY_SYSTEM),
                    Message(role="user", content=SUMMARY_PROMPT.format(transcript=transcript)),
                ],
                GenerationOptions(max_tokens=SUMMARY_MAX_TOKENS, **_EXTRACTION_OPTIONS_BASE),
            )
        summary_text = summary_text.strip()
        if not summary_text:
            raise ExtractionError("summarisation produced an empty reply")

        with llm_transcript.label("extraction.keywords"):
            keywords_reply = await complete(
                provider,
                [
                    Message(role="system", content=KEYWORDS_SYSTEM),
                    Message(role="user", content=KEYWORDS_PROMPT.format(summary=summary_text)),
                ],
                GenerationOptions(max_tokens=KEYWORDS_MAX_TOKENS, **_EXTRACTION_OPTIONS_BASE),
            )
        # An unparseable reply loses the keywords, not the summary (KTD9):
        # the summary is the expensive artefact and stands alone.
        keywords = parse_keywords(keywords_reply)

        return ConversationSummary(
            conversation_id=conversation.id,
            group_id=conversation.group_id,
            summary=summary_text,
            keywords=keywords,
            covered_messages=len(conversation.messages),
            model_id=provider.info.id,
        )
