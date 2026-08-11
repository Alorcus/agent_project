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


class TuningError(AgentChatError):
    """A memory tuning environment variable could not be parsed."""
