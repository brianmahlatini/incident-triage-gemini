"""Model providers behind a single interface.

The pipeline depends on :class:`~triage.providers.base.Provider`, never on a
vendor SDK. That is what lets the same validated workflow run against live
Gemini, against a deterministic offline stub for tests and demos, and - with
one more class - against a different backend, without the surrounding code
changing.
"""

from __future__ import annotations

from ..config import SETTINGS, Settings
from .base import (
    ContentBlockedError,
    PermanentProviderError,
    Provider,
    ProviderResponse,
    TransientProviderError,
)
from .mock import MockProvider


def get_provider(settings: Settings = SETTINGS) -> Provider:
    """Build the configured provider.

    The Gemini import is deliberately lazy: the project must remain installable
    and fully testable without the vendor SDK or any credentials present.
    """
    name = settings.provider.lower()
    if name in {"mock", "offline", "stub"}:
        return MockProvider(settings)
    if name in {"gemini", "vertex", "vertexai", "google"}:
        from .gemini import GeminiProvider

        return GeminiProvider(settings)
    raise ValueError(
        f"Unknown TRIAGE_PROVIDER {settings.provider!r}. Expected 'mock' or 'gemini'."
    )


__all__ = [
    "ContentBlockedError",
    "MockProvider",
    "PermanentProviderError",
    "Provider",
    "ProviderResponse",
    "TransientProviderError",
    "get_provider",
]
