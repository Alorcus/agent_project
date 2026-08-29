"""The assistant's own system prompt, the fact-extraction, retrieval and
consultation prompts, and the pure functions that prepare their input and
parse their output. No I/O, no provider, no store — this is the file a
non-programmer edits to tune reply, fact/quote, retrieval, routing or
consultation quality.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import TYPE_CHECKING

from agentchat.core.agents import DEFAULT_AGENT_ID, SubAgent
from agentchat.core.context import estimate_tokens
from agentchat.core.models import Message

if TYPE_CHECKING:
    from agentchat.core.retrieval import Hit, Recall

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

#: R5 — the most quotes a single fact call can be given supporting evidence
#: from.
MAX_QUOTES = 4

FACT_SYSTEM = """\
The text below is a short extract from the middle of a conversation — it may \
open mid-topic and end mid-thought. Read it and state exactly one short \
factual statement about the user: the single most important thing the extract \
*establishes* about their world, not what it merely asks about.

You are building a record of that world — the user's systems, tools, data, \
code, colleagues, deadlines, constraints, habits, preferences and decisions — \
so that a later conversation can be answered with it. Write the fact so it \
still stands on its own weeks from now, naming what it is about rather than \
saying "it" or "the file".

Skip general knowledge. Anything that would hold for anyone — how a \
technology works, what a term means, what is usually good practice — is \
already known and useless as a fact, however much of the extract the \
assistant spent explaining it. From the assistant's turns take only what the \
user confirmed about themselves.

Prefer what the text states over what it asks: when the extract is mostly \
questions, state the smallest true thing it establishes about the user rather \
than inflating an answer that isn't there — what someone asks about is itself \
evidence of what they are working on. When the text states *why* something \
holds for them, include the reason in the same sentence.

Reply with the fact alone: no preamble, no numbering, no quotation marks.

Example:

Text:

User: We're moving our staging environment to Kubernetes next month.

Assistant: Which distribution are you targeting — a managed offering or \
something self-hosted?

User: A managed one, we don't have anyone to run control planes.

Fact:

The user's team is moving its staging environment to a managed Kubernetes \
offering next month.

Example:

Text:

User: Can we drop the nightly backup job? It's been failing for a week and \
nobody's looked at why.

Assistant: I wouldn't recommend it — even a failing job is a signal \
something changed; dropping it just removes the alarm.

User: Fair, but it's paging someone every night for nothing.

Fact:

The user's team is considering dropping its nightly backup job because it has \
been failing for a week and paging someone every night.

Example:

Text:

User: Will a group-by on 50 million distinct keys blow up in Polars?

Assistant: Polars hashes the grouping keys and streams to disk once the \
table no longer fits in memory, so the ceiling is usually disk rather than \
RAM. The engine chooses …

User: Good, because it's a 40GB parquet export of our clickstream and I only \
have a 32GB laptop.

Fact:

The user groups a 40GB parquet export of their clickstream by a column with \
50 million distinct keys, on a laptop with 32GB of memory."""

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
    and duplicates dropped, capped at `limit`. Returns `()` when nothing
    survives, the caller decides what that means. Never strips a trailing
    period: a quote's own punctuation is part of what must anchor in the
    source message."""
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


# -- adaptive fact retrieval -------------------------------------------------

GATE_KNOWN = "KNOWN"
GATE_SEARCH = "SEARCH"
VERDICT_ENOUGH = "ENOUGH"
VERDICT_MISSING = "MISSING"
#: Hard ceiling on seeds per round; `s` is configured below it and enforced
#: in `parse_seeds`, not merely requested in the prompt — the cap is the
#: dominant term in the loop's cost multiplication.
MAX_SEEDS = 4

GATE_SYSTEM = f"""\
Classify the message below. Do not answer it. Reply with exactly one word: \
`{GATE_SEARCH}` if answering it well needs facts from the user's earlier \
conversations, or `{GATE_KNOWN}` if a general assistant can answer it from \
common knowledge alone.

The two errors are not equal. A needless search costs a few seconds. A \
skipped one produces a fluent, confident, wrong answer. So choose \
`{GATE_SEARCH}` for possessive or relational phrasing ("our", "the team", \
"we decided", "my"), status questions about anyone not globally famous, \
ambiguous named entities, private projects or artefacts, anything that \
depends on this user's own situation — and whenever you are unsure.

Example:

Message:

what did we land on for the staging database?

Verdict:

{GATE_SEARCH}

Example:

Message:

write me a haiku about autumn

Verdict:

{GATE_KNOWN}"""

GATE_PROMPT = "Message:\n\n{message}\n\nVerdict:"


def parse_gate(text: str) -> bool:
    """`True` (retrieve) unless the reply's first word is, case-insensitively,
    `KNOWN`. An unparseable verdict is not a third outcome — it means
    retrieve (R6)."""
    first = _undecorate(text.strip().splitlines()[0]) if text.strip() else ""
    first = first.split()[0] if first.split() else ""
    return first.lower() != GATE_KNOWN.lower()


REWRITE_SYSTEM = """\
Write one search query for a store of short factual statements taken from the \
user's past conversations. You are given the user's message, one seed phrase \
to work from, and the queries already tried this search. Write a query that \
covers a facet those miss.

Phrase it in the words the conversation would have used, not the words the \
question uses: the user asks "what did we pick for the DB", the stored fact \
reads "the team chose Postgres 14 for the billing service". One line, no \
preamble, no quotation marks, no leading label.

Example:

Message: what version did we settle on?
Seed: what version did we settle on?
Already tried: (none)

Query:

database version chosen for the billing service"""

REWRITE_PROMPT = (
    "Message: {message}\nSeed: {seed}\nAlready tried:\n{tried}\n\nQuery:"
)


def parse_query(text: str) -> str:
    """One cleaned line from a rewrite reply — label dropped, quotes and
    bullets stripped. `""` when nothing survives."""
    for line in text.strip().splitlines():
        cleaned = _undecorate(line)
        if cleaned.lower().startswith("query:"):
            cleaned = _undecorate(cleaned[len("query:") :])
        if cleaned:
            return cleaned
    return ""


JUDGE_SYSTEM = f"""\
You are given the user's original message and a list of retrieved facts. \
Decide whether the facts are enough to answer the message well. Never answer \
the question yourself.

Reply `{VERDICT_ENOUGH}` if they are. Otherwise reply `{VERDICT_MISSING}: ` \
followed by one sentence naming precisely what is absent — the wrong entity, \
the wrong time frame, the right topic at the wrong level of detail, or too \
vague to use.

Example:

Message: what Postgres version are we on in production?
Facts:
- The team runs Postgres for the billing service.

Verdict:

{VERDICT_MISSING}: no version number anywhere, only that Postgres is used.

Example:

Message: what Postgres version are we on in production?
Facts:
- The team upgraded production to Postgres 15 in March.

Verdict:

{VERDICT_ENOUGH}"""

JUDGE_PROMPT = "Message: {message}\nFacts:\n{facts}\n\nVerdict:"
#: The judge and the reseed prompt are shown the ORIGINAL user message, never
#: a rewritten query: rewrites are lossy interpretations of intent, and a
#: loop that judges against its own last guess drifts away from what was
#: asked, one round at a time.


def parse_verdict(text: str) -> tuple[bool, str]:
    """`(True, "")` when the reply's first word is `ENOUGH`; otherwise
    `(False, the gap sentence)` — everything after the first colon, or the
    whole reply with a leading `MISSING` stripped. Parses leniently, then
    decides."""
    stripped = text.strip()
    first = stripped.split()[0].strip(":").lower() if stripped.split() else ""
    if first == VERDICT_ENOUGH.lower():
        return True, ""
    if ":" in stripped:
        return False, stripped.split(":", 1)[1].strip()
    body = stripped
    if body.lower().startswith(VERDICT_MISSING.lower()):
        body = body[len(VERDICT_MISSING) :].strip()
    return False, body.strip()


RESEED_SYSTEM = """\
You are given the user's original message, a one-sentence description of what \
the retrieved evidence is still missing, and every query already tried. Write \
new search queries aimed at closing that gap — one per line, at most as many \
as asked for, none repeating a tried query, no numbering or bullets."""

RESEED_PROMPT = (
    "Message: {message}\nGap: {gap}\nAlready tried:\n{tried}\n\n"
    "Write at most {limit} queries:"
)


def parse_seeds(text: str, *, limit: int = MAX_SEEDS) -> tuple[str, ...]:
    """One seed per non-empty line, bullets and numbering stripped,
    deduplicated case-insensitively, capped at `min(limit, MAX_SEEDS)`.
    Mirrors `parse_quotes`."""
    cap = max(0, min(limit, MAX_SEEDS))
    seeds: list[str] = []
    seen: set[str] = set()
    for line in text.strip().splitlines():
        cleaned = _undecorate(line)
        if not cleaned or cleaned.lower() in seen:
            continue
        seen.add(cleaned.lower())
        seeds.append(cleaned)
        if len(seeds) >= cap:
            break
    return tuple(seeds)


RECALL_HEADER = """\
---
The notes below were retrieved from the user's earlier conversations in this \
project. The user did not write them and cannot see them. Use them where they \
help answer the message above; where they are silent, fall back on your own \
knowledge. Never mention that a retrieval step happened."""

RECALL_HEDGE = (
    " A check judged this evidence incomplete for the question — treat it as "
    "partial and say plainly where you are unsure."
)

RECALL_FOOTER = "The user's message, again: {user_text}"


def render_facts(hits: Sequence["Hit"], *, budget: int) -> str:
    """The retrieved facts, grouped by view: a `Facts:` block of claim-view
    hits and a `Said:` block of evidence-view hits as `author: "quote"`.
    Drops whole hits, lowest-scored first, to fit `budget` tokens."""
    ordered = sorted(hits, key=lambda h: h.score, reverse=True)

    def render(kept: Sequence["Hit"]) -> str:
        claims = [h for h in kept if h.view == "claim"]
        said = [h for h in kept if h.view == "evidence"]
        blocks: list[str] = []
        if claims:
            blocks.append(
                "Facts:\n" + "\n".join(f"- {h.fact.text}" for h in claims)
            )
        if said:
            lines: list[str] = []
            for hit in said:
                for phrase in hit.fact.phrases:
                    if phrase.quote:
                        lines.append(f'- {phrase.author.label}: "{phrase.quote}"')
            if lines:
                blocks.append("Said:\n" + "\n".join(lines))
        return "\n\n".join(blocks)

    kept = list(ordered)
    while kept and estimate_tokens(render(kept)) > budget:
        kept.pop()
    return render(kept)


def recall_block(recall: "Recall") -> str:
    """The block appended to a user turn: header (plus the hedge on a
    `cap`/`deadline` exit), the rendered digest, then the footer restating the
    user's message. `_prompt_messages` and `RecallNote` both render through
    this so what the model saw and what the note shows cannot drift."""
    header = RECALL_HEADER
    if recall.exit == "cap":
        header += RECALL_HEDGE
    footer = RECALL_FOOTER.format(user_text=recall.user_text)
    body = recall.digest or "(no matching facts)"
    return f"{header}\n\n{body}\n\n{footer}"


def recalled_text(user_text: str, recall: "Recall") -> str:
    return f"{user_text}\n\n{recall_block(recall)}"


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
    line = _undecorate(line)
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


def _undecorate(piece: str) -> str:
    """Strip a model's list decoration from one line: surrounding quotes, a
    leading `-`/`*` bullet, `1.`/`2)` numbering, and a trailing period."""
    cleaned = piece.strip().strip("'\"").strip()
    while cleaned[:1] in ("-", "*"):
        cleaned = cleaned[1:].strip()
    stripped = cleaned.lstrip("0123456789")
    if stripped != cleaned and stripped[:1] in (".", ")"):
        cleaned = stripped[1:].strip()
    return cleaned.rstrip(".").strip()
