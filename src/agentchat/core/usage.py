"""How much of the context window a turn's generations actually occupied.

The figure is what the model still holds when a call ends — its prompt *plus*
everything it generated, which is what the KV cache is sized by — not the
prompt alone. A call is therefore recorded twice: once when its prompt is
known, once when its completion is. The collector keeps the largest total, so
the second record supersedes the first without anything having to retract it.

Recorded *inside* each backend for the same reason the transcript is: only the
backend holds the tokenizer that can count either half. A backend without one
says so by passing ``None`` and gets a character-count guess, flagged as one.

Recording is a no-op unless a collector is installed. ``ChatService`` installs
one per turn, which is what makes the peak below mean "this turn" and not
"since the process started". ``record`` hands back the handle that closes the
call, so a completion that arrives late — the local backend reports it from
its own generation thread, after the consumer has moved on — still lands in
the turn it belongs to rather than whichever turn is running by then.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace

from agentchat.core.context import estimate_tokens
from agentchat.core.models import Message


@dataclass(frozen=True)
class CallUsage:
    """What one generation left resident: the prompt it was given, plus what
    it wrote in reply."""

    prompt_tokens: int
    completion_tokens: int
    context_window: int
    #: The transcript label of the call this came from — `chat`, `route`,
    #: `subagent.<id>` — so the UI can say which call the peak was.
    label: str = "unknown"
    #: True when no tokenizer counted this call and its figures are guesses.
    estimated: bool = False

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def fraction(self) -> float:
        """Clamped: a call that outgrew the window is a full bar, not an
        overflowing one."""
        if self.context_window <= 0:
            return 0.0
        return min(1.0, self.tokens / self.context_window)


class TurnUsage:
    """The largest call made while this collector was installed.

    The max, not the last: a consulted turn spends three generations before
    the reply's own, and it is the biggest of the four that says how much the
    model had to hold at once.
    """

    def __init__(self) -> None:
        self.peak: CallUsage | None = None

    def record(self, usage: CallUsage) -> None:
        if self.peak is None or usage.tokens > self.peak.tokens:
            self.peak = usage


class Call:
    """A generation recorded with its prompt, waiting to be told what it
    generated."""

    def __init__(self, collector: TurnUsage, usage: CallUsage) -> None:
        self._collector = collector
        self._usage = usage

    def complete(self, *, completion_tokens: int | None, output: str = "") -> None:
        """Re-record the call with its completion counted. Holds its own
        collector rather than looking one up, so it is safe to call from
        another thread and after the turn has finished; calling it twice, or
        not at all, only ever leaves the smaller total standing."""
        estimated = completion_tokens is None
        tokens = _estimate_text(output) if estimated else completion_tokens
        self._collector.record(
            replace(
                self._usage,
                completion_tokens=tokens,
                estimated=self._usage.estimated or estimated,
            )
        )


_collector: TurnUsage | None = None


@contextmanager
def collecting() -> Iterator[TurnUsage]:
    """Install a fresh collector for the duration of the block. Saves and
    restores rather than clearing, so a nested block cannot strand an outer
    one."""
    global _collector
    previous = _collector
    collector = TurnUsage()
    _collector = collector
    try:
        yield collector
    finally:
        _collector = previous


def record(
    *,
    label: str,
    messages: Sequence[Message],
    prompt_tokens: int | None,
    context_window: int,
) -> Call | None:
    """Report a call's prompt and return the handle that closes it once the
    completion is known. ``prompt_tokens=None`` means the backend has no
    tokenizer, and the size is estimated from ``messages`` instead. ``None``
    back means nothing is collecting and the completion needs no reporting.
    """
    collector = _collector
    if collector is None:
        return None
    estimated = prompt_tokens is None
    usage = CallUsage(
        prompt_tokens=(
            sum(estimate_tokens(m.content) for m in messages)
            if prompt_tokens is None
            else prompt_tokens
        ),
        completion_tokens=0,
        context_window=context_window,
        label=label,
        estimated=estimated,
    )
    collector.record(usage)
    return Call(collector, usage)


def _estimate_text(text: str) -> int:
    # `estimate_tokens` floors at one token, which is right for a message and
    # wrong for a reply that produced nothing.
    return estimate_tokens(text) if text else 0
