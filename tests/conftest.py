"""Shared fixtures.

Every test runs against the mock provider. That is a deliberate boundary: the
things worth asserting on - validation, redaction, grounding, routing, retry,
error handling - are all deterministic, and pinning them to a paid,
non-deterministic API would make the suite slow, flaky and expensive without
testing anything extra. Whether Gemini classifies well is a question for the
evaluation harness, not for unit tests.
"""

from __future__ import annotations

import pytest

from triage.config import Settings
from triage.pipeline import TriagePipeline
from triage.providers.mock import MockProvider


@pytest.fixture
def settings() -> Settings:
    return Settings(provider="mock", model="mock-rules-v1", max_retries=3)


@pytest.fixture
def pipeline(settings: Settings) -> TriagePipeline:
    # sleeper is a no-op so backoff logic is exercised without the wait.
    return TriagePipeline(
        provider=MockProvider(settings), settings=settings, sleeper=lambda _: None
    )


@pytest.fixture
def outage_text() -> str:
    return (
        "The core policy database prod-db-01 is down. All 300 call centre agents "
        "cannot process claims. Started at 06:14 this morning, no workaround."
    )
