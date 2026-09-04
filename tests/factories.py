"""Builders for test data, shared by the tests that need a group to file a
conversation under."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass

from agentchat.core.models import (
    Author,
    Conversation,
    Document,
    Fact,
    Group,
    Message,
    Phrase,
    Snippet,
)
from agentchat.core import usage
from agentchat.llm import transcript
from agentchat.llm.base import GenerationOptions, ModelInfo


def make_conversation(**overrides) -> Conversation:
    # `default` is the one group the schema seeds, so a conversation that means
    # nothing in particular by its group still satisfies the FK.
    defaults = dict(
        title="Trip planning",
        group_id="default",
        messages=[
            Message(role="user", content="Where should I go?"),
            Message(role="assistant", content="Try Kyoto.", model_id="qwen"),
        ],
    )
    defaults.update(overrides)
    return Conversation(**defaults)


def make_message(**overrides) -> Message:
    defaults = dict(role="user", content="Let's use SQLite for storage.")
    defaults.update(overrides)
    return Message(**defaults)


def make_group(**overrides) -> Group:
    defaults = dict(name="Picker rewrite", kind="project")
    defaults.update(overrides)
    return Group(**defaults)


@dataclass
class RecordedCall:
    messages: list[Message]
    options: GenerationOptions | None
    started: float
    #: `None` until the call finishes — a cancelled call may never set this.
    finished: float | None = None


class ScriptedProvider:
    """An always-loaded `LLMProvider` that yields each canned reply, whole,
    in turn, and records every call it received. Assertions about *which*
    prompt got *which* input, and about overlapping calls, need that record —
    the mock backend (which echoes the last user turn) cannot supply it.

    Replies run out silently (as `""`) once a test's script is exhausted,
    rather than raising `IndexError`, so a scenario that only cares about the
    first call or two doesn't have to script the rest.
    """

    def __init__(
        self,
        *replies: str,
        info: ModelInfo | None = None,
        chunk_delay: float = 0.0,
    ) -> None:
        self._replies = list(replies)
        self._info = info or ModelInfo(id="scripted", name="Scripted", context_window=4096)
        self._chunk_delay = chunk_delay
        self.calls: list[RecordedCall] = []

    @property
    def info(self) -> ModelInfo:
        return self._info

    @property
    def is_loaded(self) -> bool:
        return True

    async def load(self) -> None:
        return None

    async def unload(self) -> None:
        return None

    async def generate(
        self,
        messages: Sequence[Message],
        options: GenerationOptions | None = None,
    ) -> AsyncIterator[str]:
        call = RecordedCall(messages=list(messages), options=options, started=time.monotonic())
        self.calls.append(call)
        # Reported like a real backend's, so tests that assert on the context
        # meter can drive it with a scripted script rather than a GPU.
        metered = usage.record(
            label=transcript.current_label(),
            messages=messages,
            prompt_tokens=None,
            context_window=self._info.context_window,
        )
        index = len(self.calls) - 1
        reply = self._replies[index] if index < len(self._replies) else ""
        emitted: list[str] = []
        try:
            for chunk in _chunks(reply):
                if self._chunk_delay:
                    await asyncio.sleep(self._chunk_delay)
                emitted.append(chunk)
                yield chunk
        finally:
            if metered is not None:
                metered.complete(completion_tokens=None, output="".join(emitted))
        call.finished = time.monotonic()


def scripted_provider(
    *replies: str, info: ModelInfo | None = None, chunk_delay: float = 0.0
) -> ScriptedProvider:
    return ScriptedProvider(*replies, info=info, chunk_delay=chunk_delay)


def _chunks(text: str) -> list[str]:
    """Split into whitespace-preserving pieces so a joined stream reassembles
    exactly into `text`, and multi-word replies still exercise multi-chunk
    streaming."""
    words = text.split(" ")
    if not words:
        return []
    return [w + " " for w in words[:-1]] + [words[-1]]


def make_phrase(**overrides) -> Phrase:
    defaults = dict(
        message_id="m1", start=0, end=5, author=Author(kind="user", label="user")
    )
    defaults.update(overrides)
    return Phrase(**defaults)


def make_fact(**overrides) -> Fact:
    defaults = dict(
        conversation_id="c1",
        group_id="default",
        text="The team is planning a trip to Kyoto.",
        phrases=(make_phrase(),),
        window_start=0,
        window_end=6,
        model_id="mock-small",
    )
    defaults.update(overrides)
    return Fact(**defaults)


# -- retrieval fixtures --------------------------------------------------

#: Twelve facts over four topics, three each, filed across three conversations.
#: Two are deliberate near-duplicates (overlapping windows) so read-time dedup
#: has something to remove; one is quoteless so the empty-evidence-view path is
#: covered.
GOLDEN_FACTS: tuple[dict, ...] = (
    # database choice
    dict(text="The team chose Postgres 14 for the billing service.",
         quote="we're going with Postgres 14 for billing", author=("user", "user")),
    dict(text="The team chose Postgres 14 for billing.",  # near-duplicate
         quote="Postgres 14 for billing it is", author=("user", "user")),
    dict(text="The billing database runs on a single primary with one replica.",
         quote="one primary, one replica for the billing db", author=("user", "user")),
    # deployment window
    dict(text="Production deploys happen on Tuesday mornings only.",
         quote="we only deploy on Tuesday mornings", author=("user", "user")),
    dict(text="The team froze deploys for the last week of December.",
         quote="no deploys the last week of December", author=("user", "user")),
    dict(text="A deploy needs sign-off from two reviewers.",
         quote="two reviewers have to sign off on a deploy", author=("model", "qwen3-14b")),
    # a person's dietary constraint
    dict(text="Priya is vegetarian and does not eat eggs.",
         quote="Priya's vegetarian, no eggs either", author=("user", "user")),
    dict(text="Priya is allergic to peanuts.",
         quote="Priya has a peanut allergy", author=("user", "user")),
    dict(text="The team lunch is usually catered by the place on 5th.",
         quote="", author=("user", "user")),  # quoteless
    # travel plan
    dict(text="The offsite is booked for Lisbon in October.",
         quote="offsite is Lisbon, October", author=("user", "user")),
    dict(text="Flights to Lisbon are booked out of Berlin.",
         quote="flying to Lisbon from Berlin", author=("user", "user")),
    dict(text="The offsite hotel is walking distance from the venue.",
         quote="hotel's a short walk from the venue", author=("model", "qwen3-14b")),
)


async def make_indexed_group(store, embedder, *, name="Retrieval", facts=GOLDEN_FACTS) -> Group:
    """A project group holding `facts` across three conversations, with
    phrases, quotes and vectors already written — the state every retrieval
    test starts from."""
    from agentchat.core.retrieval import FactIndex

    group = make_group(name=name)
    await store.save_group(group)
    conversations = []
    for i in range(3):
        conv = make_conversation(id=f"{group.id}-c{i}", group_id=group.id, messages=[
            Message(id=f"{group.id}-c{i}-m0", role="user", content="context line"),
        ])
        await store.save(conv)
        conversations.append(conv)

    built: list[Fact] = []
    for n, spec in enumerate(facts):
        conv = conversations[n % 3]
        kind, label = spec["author"]
        phrases = ()
        if spec["quote"]:
            phrases = (Phrase(
                message_id=conv.messages[0].id, start=0, end=1,
                author=Author(kind=kind, label=label), quote=spec["quote"],
            ),)
        fact = Fact(
            conversation_id=conv.id, group_id=group.id, text=spec["text"],
            phrases=phrases, window_start=0, window_end=6, model_id="mock",
        )
        built.append(fact)
    await store.save_facts(built)
    await FactIndex(store, embedder).index(built)
    group.facts = built  # convenience handle for tests
    return group


# -- documents -------------------------------------------------------------


def make_document(**overrides) -> Document:
    defaults = dict(
        title="handbook.txt",
        path="/tmp/handbook.txt",
        media_type="text",
        content_hash="hash-handbook",
        text="The deploy window is Tuesday.",
        char_count=29,
        snippet_count=1,
    )
    defaults.update(overrides)
    return Document(**defaults)


def make_snippet(**overrides) -> Snippet:
    defaults = dict(
        document_id="doc",
        ordinal=0,
        text="The deploy window is Tuesday.",
        start=0,
        end=29,
        page=None,
    )
    defaults.update(overrides)
    return Snippet(**defaults)


def make_pdf_bytes(pages: Sequence[str]) -> bytes:
    """A minimal PDF carrying `pages` as real text objects.

    Written by hand rather than with a writer dependency: pypdf reads PDFs and
    does not author them, and a checked-in binary fixture is a file nobody can
    review. Offsets in the xref table are computed, not guessed — pypdf
    tolerates a broken one by rebuilding it, which would silently make this
    fixture stop testing what it claims to.
    """
    page_count = len(pages)
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [{}] /Count {} >>".format(
            " ".join(f"{3 + i * 2} 0 R" for i in range(page_count)), page_count
        ).encode("ascii"),
    ]
    font_number = 3 + page_count * 2
    for index, page in enumerate(pages):
        content = _content_stream(page)
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Contents {4 + index * 2} 0 R "
                f"/Resources << /Font << /F1 {font_number} 0 R >> >> >>"
            ).encode("ascii")
        )
        objects.append(
            b"<< /Length "
            + str(len(content)).encode("ascii")
            + b" >>\nstream\n"
            + content
            + b"\nendstream"
        )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode("ascii") + body + b"\nendobj\n"

    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n".encode("ascii")
    out += f"startxref\n{xref_at}\n%%EOF\n".encode("ascii")
    return bytes(out)


def _content_stream(page: str) -> bytes:
    lines = [line for line in page.splitlines() if line.strip()]
    body = ["BT", "/F1 12 Tf", "14 TL", "40 750 Td"]
    for line in lines:
        body.append(f"({_escaped(line)}) Tj")
        body.append("T*")
    body.append("ET")
    return "\n".join(body).encode("latin-1")


def _escaped(line: str) -> str:
    for char in ("\\", "(", ")"):
        line = line.replace(char, "\\" + char)
    return line


#: Three documents over distinct topics, one paginated, one long enough that
#: the per-document cap has something to cap. Vocabulary deliberately apart
#: from `GOLDEN_FACTS`: a document hit and a fact hit must be tellable apart.
GOLDEN_DOCUMENTS: tuple[dict, ...] = (
    dict(
        title="handbook.pdf",
        media_type="pdf",
        snippets=(
            ("Expenses over two hundred euro need written approval from a "
             "finance partner before the purchase.", 1),
            ("Approved expenses are reimbursed within thirty days of the "
             "claim being filed with finance.", 2),
            ("Travel booked through the agency is billed directly and needs "
             "no expense claim at all.", 2),
            ("Equipment purchases are capitalised and follow the asset "
             "register process instead of expenses.", 3),
            ("Receipts must be legible and name the vendor, the date and the "
             "amount in euro.", 3),
        ),
    ),
    dict(
        title="onboarding.md",
        media_type="text",
        snippets=(
            ("New joiners get a laptop on their first morning and a mentor "
             "for their first month.", None),
            ("Accounts are provisioned by the platform team the working day "
             "before a joiner starts.", None),
        ),
    ),
    dict(
        title="incident-review.txt",
        media_type="text",
        snippets=(
            ("Every incident gets a blameless review within five working "
             "days of resolution.", None),
        ),
    ),
)


async def make_indexed_corpus(store, embedder, *, documents=GOLDEN_DOCUMENTS):
    """The document corpus with snippets and vectors already written — the
    global counterpart to `make_indexed_group`."""
    from agentchat.core.retrieval import SnippetIndex

    index = SnippetIndex(store, embedder)
    built: list[Document] = []
    for spec in documents:
        text = "\n\n".join(snippet for snippet, _ in spec["snippets"])
        document = Document(
            title=spec["title"],
            path=f"/corpus/{spec['title']}",
            media_type=spec["media_type"],
            content_hash=f"hash-{spec['title']}",
            text=text,
            char_count=len(text),
            snippet_count=len(spec["snippets"]),
        )
        cursor = 0
        snippets = []
        for ordinal, (body, page) in enumerate(spec["snippets"]):
            snippets.append(
                Snippet(
                    document_id=document.id,
                    ordinal=ordinal,
                    text=body,
                    start=cursor,
                    end=cursor + len(body),
                    page=page,
                )
            )
            cursor += len(body) + 2
        await store.save_document(document, snippets)
        await index.index(snippets)
        built.append(document)
    return tuple(built)
