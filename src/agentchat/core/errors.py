"""Error taxonomy: every failure that can reach the user is one of these, so
the UI can render an error instead of crashing."""

from __future__ import annotations


class AgentChatError(Exception):
    """Base class for all errors the UI is expected to handle."""


class ProviderError(AgentChatError):
    """A model backend failed, was unavailable, or returned nothing usable."""


class ModelNotFoundError(ProviderError):
    """A model id was requested that the registry does not know about."""


class ContextOverflowError(AgentChatError):
    """The context manager could not fit the prompt into the active window."""


class StorageError(AgentChatError):
    """Persisting or loading conversation state failed."""


class ExtractionError(AgentChatError):
    """Summarising a conversation produced nothing worth storing."""


class FactExtractionError(AgentChatError):
    """Extracting a fact from one window failed — a wrapped provider error,
    not a window that simply held no fact (that is `None`, not this)."""


class DelegationError(AgentChatError):
    """Consulting a sub-agent failed — routing, task authoring, or the
    specialist's own answer produced nothing worth injecting."""
