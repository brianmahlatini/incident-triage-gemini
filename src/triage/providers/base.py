"""Provider interface and the error taxonomy the retry logic depends on.

Errors are split by *what the caller should do about them* rather than by
where they came from. A 429 and a 503 are the same event to this system - wait
and try again - while a bad API key and a safety block are both permanent, and
retrying either just burns quota and latency on a call that cannot succeed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class ProviderError(RuntimeError):
    """Base class for every model-call failure."""


class TransientProviderError(ProviderError):
    """Worth retrying: rate limits, timeouts, 5xx, transport blips.

    ``retry_after`` carries the server's own hint about how long to wait, when
    it supplies one. Honouring it beats any backoff curve we invent: a 429 that
    says "retry in 30s" will fail every time against a schedule that gives up
    after 8, which is precisely what happened the first time this ran against a
    live rate limit.
    """

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class PermanentProviderError(ProviderError):
    """Not worth retrying: auth failure, malformed request, missing model."""


class ContentBlockedError(ProviderError):
    """The request or response was stopped by a safety filter.

    Permanent by nature, and never silently swallowed - a blocked incident is
    routed to a human rather than dropped, because safety blocks correlate with
    exactly the sensitive reports that most need attention.
    """


@dataclass
class ProviderResponse:
    """Everything the pipeline needs from one model call."""

    payload: dict[str, Any]
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    finish_reason: str | None = None
    raw_text: str = ""
    warnings: list[str] = field(default_factory=list)


class Provider(ABC):
    """A source of structured triage records."""

    name: str = "provider"

    @abstractmethod
    def generate(
        self,
        system_instruction: str,
        prompt: str,
        response_schema: dict[str, Any],
    ) -> ProviderResponse:
        """Return a parsed JSON object conforming to ``response_schema``.

        Implementations raise the error classes above; they never return a
        partial or unparsed result, so the pipeline has exactly two cases to
        handle rather than a spectrum of half-successes.
        """

    def health(self) -> dict[str, Any]:
        """Cheap description of provider state, surfaced on the health endpoint."""
        return {"provider": self.name, "ready": True}
