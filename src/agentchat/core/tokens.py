"""Token accounting shared by context assembly and memory ranking.

A documented approximation, not the provider's real tokenizer: neither
``core/context.py`` nor ``core/memory/rank.py`` has a provider to ask, and
the latter may not import the former (I-6). ``reserve_for_response`` absorbs
the resulting slack.
"""

from __future__ import annotations

CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)
