"""The assistant's own system prompt, the extraction, enrichment and
consultation prompts, and the pure functions that prepare their input and
parse their output. No I/O, no provider, no store — this is the file a
non-programmer edits to tune reply, summary, keyword, routing or consultation
quality.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from agentchat.core.agents import DEFAULT_AGENT_ID, SubAgent
from agentchat.core.context import estimate_tokens
from agentchat.core.models import ConversationSummary, Message

MAX_KEYWORDS = 5
KEYWORD_SEPARATOR = "; "

#: The system prompt for the assistant the user actually talks to — the
#: `default` entry in the router's roster, which names no `SubAgent` and so
#: has no `system_prompt` field of its own. Kept short on purpose: it is
#: prepended to every turn and counts against the same context budget as the
#: conversation.
DEFAULT_SYSTEM = """\
You are a general advisor. Answer the question you were asked, then stop.

Match the answer to the question: a short question gets a short answer, a \
sentence or two. Give a long or step-by-step answer only when the user asks \
for one, or when a short answer would be wrong.

Do not restate the question, announce what you are about to do, or close with \
a summary or an offer of further help. Use headings and lists only where the \
answer really is a list. Say plainly when you don't know."""

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

#: R5 — the most quotes a single fact call can be given supporting evidence
#: from.
MAX_QUOTES = 4

FACT_SYSTEM = """\
The text below is a short extract from the middle of a conversation — it may \
open mid-topic and end mid-thought. Read it and state exactly one short \
factual statement: the single most important thing the extract *establishes*, \
not what it merely asks about.

Prefer what the text states over what it asks: when the extract is mostly \
questions, state the smallest true thing it establishes rather than \
inflating an answer that isn't there. When the text states *why* something \
holds, include the reason in the same sentence.

Reply with the fact alone: no preamble, no numbering, no quotation marks.

Example:

Text:

User: We're moving our staging environment to Kubernetes next month.

Assistant: Which distribution are you targeting — a managed offering or \
something self-hosted?

User: A managed one, we don't have anyone to run control planes.

Fact:

The team is moving its staging environment to a managed Kubernetes offering \
next month.

Example:

Text:

User: Can we drop the nightly backup job? It's been failing for a week and \
nobody's looked at why.

Assistant: I wouldn't recommend it — even a failing job is a signal \
something changed; dropping it just removes the alarm.

User: Fair, but it's paging someone every night for nothing.

Fact:

The team is considering dropping its nightly backup job because it has been \
failing for a week and paging someone every night."""

FACT_PROMPT = "Text:\n\n{text}\n\nFact:"

#: No prompt below may ask for an index, offset, position or line number —
#: the model never sees character positions, so anchoring a quote is
#: `anchoring.py`'s job by design, not something a reply can be asked to do.
QUOTES_SYSTEM = f"""\
You are given the same text and a fact drawn from it. Copy the exact \
wording from the text that supports the fact — verbatim, do not paraphrase, \
add words, or fix typos. One quote per line, at most {MAX_QUOTES} quotes, \
each long enough to identify uniquely within the text.

Reply with the quotes alone: no preamble, no numbering, no quotation marks.

Example:

Text:

User: We're moving our staging environment to Kubernetes next month.

Assistant: Which distribution are you targeting — a managed offering or \
something self-hosted?

User: A managed one, we don't have anyone to run control planes.

Fact:

The team is moving its staging environment to a managed Kubernetes offering \
next month.

Quotes:

moving our staging environment to Kubernetes next month
A managed one, we don't have anyone to run control planes"""

QUOTES_PROMPT = "Text:\n\n{text}\n\nFact: {fact}\n\nQuotes:"


def parse_quotes(text: str, *, limit: int = MAX_QUOTES) -> tuple[str, ...]:
    """Defensively parse a quotes reply: one quote per non-empty line,
    bullets/numbering stripped, surrounding quotation marks stripped, empties
    and duplicates dropped, capped at `limit`. Mirrors `parse_keywords`'s
    contract — returns `()` when nothing survives, the caller decides what
    that means. Unlike `parse_keywords`, never strips a trailing period: a
    quote's own punctuation is part of what must anchor in the source
    message."""
    text = text.strip()
    if text.lower().startswith("quotes:"):
        text = text[len("quotes:") :].strip()

    quotes: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        cleaned = _clean_quote(line)
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        quotes.append(cleaned)
        if len(quotes) >= limit:
            break
    return tuple(quotes)


def _clean_quote(line: str) -> str:
    cleaned = line.strip().strip("'\"").strip()
    while cleaned[:1] in ("-", "*"):
        cleaned = cleaned[1:].strip()
    stripped = cleaned.lstrip("0123456789")
    if stripped != cleaned and stripped[:1] in (".", ")"):
        cleaned = stripped[1:].strip()
    return cleaned


def render_window(messages: Sequence[Message], *, budget: int) -> str:
    """Render `messages` as a `User:`/`Assistant:` transcript that fits
    `budget` tokens, dropping whole assistant turns oldest-first when it
    doesn't fit. Unlike `render_transcript`, no per-turn character cap — a
    quote the model is asked to copy verbatim has to exist somewhere in a
    real message, and a cap would put text in the prompt that exists in no
    message and so could never anchor."""
    turns = [
        (message.role, message.content)
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
    return rendered


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


#: What the router is told `default` covers — `default` names no `SubAgent`
#: (agents.DEFAULT_AGENT_ID), so its description lives here rather than in a
#: roster entry.
DEFAULT_PURPOSE = "general conversation, coding, writing, and anything not clearly covered below"

ROUTER_SYSTEM = """\
You route a user's message to the assistant best suited to answer it. Reply \
with exactly one name from the list below and nothing else: no explanation, \
no punctuation, no quotation marks. Choose `default` when no specialist \
clearly fits — most messages do.

Example:

Assistants:

default — general conversation, coding, writing, and anything not clearly \
covered below
ask_vet — animal health, symptoms, diet, and behaviour for pets and livestock

Message:

My dog has been limping since this morning, is that something to worry about?

Name:

ask_vet"""

ROUTER_PROMPT = "Assistants:\n\n{roster}\n\nMessage:\n\n{message}\n\nName:"

TASK_SYSTEM = """\
You write the task for a specialist assistant who will answer without seeing \
this conversation. You are shown the conversation so far: the last user turn \
is the one that needs answering, and the earlier turns are there so you can \
resolve what it refers to. In one or two sentences, address the specialist \
directly and restate every detail they need — they cannot see the \
conversation, so anything you don't restate is lost to them. Do not answer \
the question yourself.

Example:

Specialist: ask_vet — animal health, symptoms, diet, and behaviour for pets \
and livestock

Conversation:

User: My dog has been limping since this morning, is that something to worry \
about?

Assistant: Sudden limping in an otherwise healthy dog is usually a \
soft-tissue strain or something lodged in a paw pad. Check between the toes \
and along the pads first …

User: Nothing in the pads, and he hasn't been anywhere unusual. He's nine \
though — does his age change the answer?

Task:

A nine-year-old dog has been limping since this morning with no known injury; \
the owner has already checked the paw pads for anything lodged and found \
nothing, and the dog has not been anywhere unusual. Explain what else causes \
sudden limping in a dog that age, whether being nine changes how urgent it \
is, and when it warrants an urgent vet visit."""

TASK_PROMPT = "Specialist: {name} — {purpose}\n\nConversation:\n\n{transcript}\n\nTask:"

#: The point past which `parse_task` cuts an over-long authored task, on a
#: word boundary — `TASK_MAX_TOKENS` already bounds the model's own reply,
#: this is a second floor under whatever gets this far.
TASK_CHAR_CAP = 600

CONSULTATION_HEADER = """\
---
A specialist was consulted for this reply. It was given only the task shown \
below and could not see this conversation. Use its answer where it helps, \
correct it where it does not fit what the user actually asked, and answer in \
your own voice."""


def roster_text(agents: Sequence[SubAgent]) -> str:
    """One line per assistant the router can choose, `default` first."""
    lines = [f"{DEFAULT_AGENT_ID} — {DEFAULT_PURPOSE}"]
    lines += [f"{agent.id} — {agent.purpose}" for agent in agents]
    return "\n".join(lines)


def consulted_text(user_text: str, agent: SubAgent, task: str, answer: str) -> str:
    """`user_text` with the specialist's task and answer appended behind
    `CONSULTATION_HEADER`."""
    return (
        f"{user_text}\n\n{CONSULTATION_HEADER}\n\n"
        f"Task given to {agent.name}:\n{task}\n\n"
        f"{agent.name}'s answer:\n{answer}"
    )


def parse_agent_id(text: str, *, agent_ids: Sequence[str]) -> str | None:
    """The agent id in a router reply, or `None` for `default` and for
    anything that cannot be read as a listed id. Never raises."""
    raw = text.strip().strip("`").strip()
    raw = raw.strip("'\"").strip()
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if not lines:
        return None

    line = lines[0]
    if line.lower().startswith("name:"):
        line = line[len("name:") :].strip()
    line = _clean_keyword(line)
    candidate = line.lower()

    lower_ids = {agent_id.lower(): agent_id for agent_id in agent_ids}
    if candidate in lower_ids:
        return lower_ids[candidate]
    if not candidate or candidate == DEFAULT_AGENT_ID:
        return None

    for agent_id in agent_ids:
        prefix_stripped = agent_id.split("_", 1)[-1] if "_" in agent_id else agent_id
        if candidate == prefix_stripped.lower():
            return agent_id

    # Last resort: the first listed id occurring as a whole word anywhere in
    # the line — a rambling reply routes reproducibly rather than randomly.
    best: tuple[int, str] | None = None
    for agent_id in agent_ids:
        match = re.search(rf"(?<!\w){re.escape(agent_id)}(?!\w)", line, re.IGNORECASE)
        if match and (best is None or match.start() < best[0]):
            best = (match.start(), agent_id)
    return best[1] if best else None


def parse_task(text: str, *, fallback: str) -> str:
    """A defensively-cleaned task: a leading `Task:` label dropped, whitespace
    collapsed, capped at `TASK_CHAR_CAP` on a word boundary. `fallback` when
    nothing survives — losing the authored task costs phrasing, not the
    turn."""
    text = text.strip()
    if text.lower().startswith("task:"):
        text = text[len("task:") :].strip()
    text = " ".join(text.split())
    if not text:
        return fallback
    if len(text) > TASK_CHAR_CAP:
        cut = text.rfind(" ", 0, TASK_CHAR_CAP)
        text = (text[:cut] if cut > 0 else text[:TASK_CHAR_CAP]).rstrip() + "…"
    return text


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
