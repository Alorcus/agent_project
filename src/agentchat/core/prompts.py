"""The extraction prompts, and the pure functions that prepare their input and
parse their output. No I/O, no provider, no store — this is the file a
non-programmer edits to tune summary or keyword quality.
"""

from __future__ import annotations

from collections.abc import Sequence

from agentchat.core.context import estimate_tokens
from agentchat.core.models import ConversationSummary, Message

MAX_KEYWORDS = 5
KEYWORD_SEPARATOR = "; "

#: A long assistant reply contributes its shape to the transcript, not its
#: bulk — the summary is about what was asked, not how much was answered.
ASSISTANT_CHAR_CAP = 800

#: Marks the point where older turns were dropped to fit the budget, so the
#: model is not told the transcript is complete when it is not.
ELISION = "[… earlier turns omitted …]"

SUMMARY_SYSTEM = """\
You summarise chat transcripts for later recall. Write a dense, factual \
summary of what the conversation is about. Weight the user's turns above the \
assistant's — the user's turns are what the conversation is *for*, the \
assistant's are only evidence of what was asked. Target \
3-5 sentences. Write in the third person about the topics covered.

Example:

Transcript:

User: We store about 40M sensor readings a month in one Postgres table and \
queries over the last week have gotten slow. Is partitioning worth it?

Assistant: Range partitioning on the reading timestamp is the usual fit for \
this shape of table. Declarative partitioning lets the planner prune …

User: Monthly partitions, and we only keep 18 months. Does that make dropping \
old data cheaper?

Assistant: Considerably. DROP TABLE on an expired partition is close to \
instant, where a bulk DELETE has to rewrite …

User: What breaks if I partition a table that already holds 700M rows?

Summary:

Partitioning a 700M-row Postgres table of sensor readings, growing by roughly \
40M rows a month, after queries over recent data slowed down. Monthly range \
partitions on the reading timestamp are the shape under consideration, with \
an 18-month retention window enforced by dropping expired partitions instead \
of issuing bulk DELETEs. The open question is how to migrate the existing \
unpartitioned table in place, and what it costs while the migration runs.\n """

SUMMARY_PROMPT = """\
Transcript:

{transcript}

Summarise the conversation above."""

KEYWORDS_SYSTEM = """\
You extract keywords from a conversation summary. Emit at most 5 keywords, \
separated by "; ", on a single line, and nothing else — no numbering, no \
label, no trailing period. Prefer nouns and named entities.

Example:

Summary:

The conversation covers debugging a Flask application that returns 500 \
errors on POST requests to the /upload endpoint. The failure is traced to \
a missing Content-Type check and a SQLAlchemy session that isn't rolled \
back after a failed commit. The discussion then turns to replacing local \
disk storage with S3 uploads via boto3.

Keywords:

Flask; SQLAlchemy; /upload endpoint; boto3; S3"""

KEYWORDS_PROMPT = """\
Summary:

{summary}

Keywords:"""

ENRICHMENT_HEADER = """\
---
The following notes were extracted from the user's earlier conversations in \
this project. They are background that may or may not be relevant — the \
user did not write them and cannot see them. Use them only where they help \
answer the message above; do not mention them otherwise."""

ENRICHMENT_BULLET = "- "


def enriched_text(user_text: str, summaries: Sequence[ConversationSummary]) -> str:
    """`user_text` with `summaries` appended behind `ENRICHMENT_HEADER`, one
    bulleted line per summary. Returns `user_text` unchanged when `summaries`
    is empty."""
    if not summaries:
        return user_text
    bullets = "\n".join(
        f"{ENRICHMENT_BULLET}{' '.join(summary.summary.split())}" for summary in summaries
    )
    return f"{user_text}\n\n{ENRICHMENT_HEADER}\n\n{bullets}"


def render_transcript(messages: Sequence[Message], *, budget: int) -> str:
    """Render `messages` as a `User:`/`Assistant:` transcript that fits
    `budget` tokens, dropping the oldest assistant turns first and the most
    recent user turn last."""
    turns = [
        (message.role, _capped(message))
        for message in messages
        if message.role != "system" and not message.is_empty
    ]

    elided = False
    while turns and _rendered_tokens(turns) > budget:
        drop_index = _oldest_droppable(turns)
        if drop_index is None:
            break
        del turns[drop_index]
        elided = True

    rendered = _render(turns)
    if elided:
        rendered = f"{ELISION}\n\n{rendered}" if rendered else ELISION

    if turns and estimate_tokens(rendered) > budget and len(turns) == 1:
        # A single user turn larger than the whole budget: truncate it rather
        # than drop it, so the last question always survives in some form.
        role, content = turns[0]
        chars = max(0, budget * 4)
        content = content[:chars] + ("…" if len(content) > chars else "")
        rendered_prefix = f"{ELISION}\n\n" if elided else ""
        rendered = rendered_prefix + f"{_label(role)}: {content}"

    return rendered


def _capped(message: Message) -> str:
    content = message.content
    if message.role == "assistant" and len(content) > ASSISTANT_CHAR_CAP:
        return content[:ASSISTANT_CHAR_CAP] + "…"
    return content


def _label(role: str) -> str:
    return "User" if role == "user" else "Assistant"


def _render(turns: list[tuple[str, str]]) -> str:
    return "\n\n".join(f"{_label(role)}: {content}" for role, content in turns)


def _rendered_tokens(turns: list[tuple[str, str]]) -> int:
    return estimate_tokens(_render(turns))


def _oldest_droppable(turns: list[tuple[str, str]]) -> int | None:
    """The oldest assistant turn, or — if none are left — the oldest user
    turn, but never the single most recent turn."""
    for index, (role, _content) in enumerate(turns):
        if role == "assistant":
            return index
    if len(turns) > 1:
        return 0
    return None


def parse_keywords(text: str, *, limit: int = MAX_KEYWORDS) -> tuple[str, ...]:
    """Defensively parse a keyword reply. Small models decorate their output —
    labels, bullets, prose — so this tolerates all of that and returns `()`
    when nothing survives; the caller decides what an empty result means."""
    text = text.strip().strip("`").strip()
    text = text.strip("'\"").strip()

    lower = text.lower()
    if lower.startswith("keywords:"):
        text = text[len("keywords:") :].strip()

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    line = next((candidate for candidate in lines if ";" in candidate), None)
    if line is None:
        line = lines[0] if lines else ""

    pieces = line.split(";")
    keywords: list[str] = []
    seen: set[str] = set()
    for piece in pieces:
        cleaned = _clean_keyword(piece)
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        keywords.append(cleaned)
        if len(keywords) >= limit:
            break
    return tuple(keywords)


def _clean_keyword(piece: str) -> str:
    cleaned = piece.strip().strip("'\"").strip()
    # Drop a leading bullet: "-", "*", or "1." / "2)" style numbering.
    while cleaned[:1] in ("-", "*"):
        cleaned = cleaned[1:].strip()
    stripped = cleaned.lstrip("0123456789")
    if stripped != cleaned and stripped[:1] in (".", ")"):
        cleaned = stripped[1:].strip()
    return cleaned.rstrip(".").strip()
