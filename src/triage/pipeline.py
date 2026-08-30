"""The triage workflow itself.

One pass over an incident, in a fixed order, with a defined outcome at every
step:

    validate -> redact -> prompt -> model (with retry) -> parse & validate
             -> ground -> route -> log & measure

Two properties are worth stating explicitly, because they are what make the
thing operable rather than merely functional:

* **It always returns a result.** There is no path that raises at the caller.
  A rejected input, a safety block, an exhausted retry budget - each produces a
  ``TriageResult`` carrying a status, an explanation, and a routing decision
  that sends the incident to a person. In an operations context, an incident
  that vanishes because a service raised an exception is worse than one
  triaged badly, because nobody knows it is missing.
* **The model call is one step, not the workflow.** Everything before it exists
  to avoid pointless or unsafe calls; everything after it exists because the
  model's answer is a proposal, not a verdict.
"""

from __future__ import annotations

import logging
import random
import time
import uuid
from typing import Callable

from pydantic import ValidationError

from . import gating
from .config import SETTINGS, Settings
from .grounding import check_grounding, summary_is_supported
from .observability import (
    METRICS,
    log_event,
    new_correlation_id,
    text_fingerprint,
    timed,
)
from .prompt import PROMPT_VERSION, SYSTEM_INSTRUCTION, build_prompt
from .providers import Provider, get_provider
from .providers.base import (
    ContentBlockedError,
    PermanentProviderError,
    ProviderResponse,
    TransientProviderError,
)
from .redaction import redact
from .schema import (
    SCHEMA_VERSION,
    GroundingReport,
    ModelTriage,
    Status,
    TriageMeta,
    TriageResult,
    gemini_response_schema,
)
from .validation import validate_incident


class TriagePipeline:
    """Runs incidents through the workflow."""

    def __init__(
        self,
        provider: Provider | None = None,
        settings: Settings = SETTINGS,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        # Constructed once and reused: the SDK client holds connection state,
        # and rebuilding it per request adds latency to every single call.
        self.provider = provider or get_provider(settings)
        # Injected so tests exercise the backoff logic without waiting for it.
        self._sleep = sleeper

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def run(self, raw_text: object, incident_id: str | None = None) -> TriageResult:
        """Triage one incident. Never raises."""
        incident_id = incident_id or f"INC-{uuid.uuid4().hex[:8].upper()}"
        correlation_id = new_correlation_id()

        base_meta = TriageMeta(
            correlation_id=correlation_id,
            provider=self.provider.name,
            model=self.settings.model,
            prompt_version=PROMPT_VERSION,
            schema_version=SCHEMA_VERSION,
        )

        # --- 1. validate --------------------------------------------------
        validation = validate_incident(raw_text, self.settings)
        if not validation.ok:
            log_event(
                "triage.rejected",
                level=logging.WARNING,
                correlation_id=correlation_id,
                incident_id=incident_id,
                errors=validation.errors,
            )
            result = TriageResult(
                status=Status.REJECTED,
                incident_id=incident_id,
                routing=gating.rejection_routing(validation.errors),
                meta=base_meta,
                error="; ".join(validation.errors),
            )
            METRICS.record(result)
            return result

        # --- 2. redact before the text leaves the process -----------------
        redaction = redact(validation.text)
        safe_text = redaction.text
        base_meta.redactions = redaction.counts
        base_meta.warnings = list(validation.warnings)

        log_event(
            "triage.started",
            correlation_id=correlation_id,
            incident_id=incident_id,
            input_chars=len(safe_text),
            input_fingerprint=text_fingerprint(safe_text),
            redactions=redaction.counts,
            input_warnings=validation.warnings,
            suspected_injection=validation.suspected_injection,
            prompt_version=PROMPT_VERSION,
        )

        # --- 3-4. prompt and call ----------------------------------------
        prompt = build_prompt(safe_text, incident_id)
        with timed() as clock:
            outcome = self._call_with_retry(prompt, correlation_id, incident_id)
        base_meta.latency_ms = clock["elapsed_ms"]

        if isinstance(outcome, _CallFailure):
            base_meta.attempts = outcome.attempts
            log_event(
                "triage.failed",
                level=logging.ERROR,
                correlation_id=correlation_id,
                incident_id=incident_id,
                error_type=outcome.error_type,
                error=outcome.message,
                attempts=outcome.attempts,
                latency_ms=base_meta.latency_ms,
            )
            result = TriageResult(
                status=Status.FAILED,
                incident_id=incident_id,
                routing=gating.failure_routing(outcome.human_reason),
                meta=base_meta,
                error=outcome.message,
            )
            METRICS.record(result)
            return result

        triage, response, attempts = outcome.triage, outcome.response, outcome.attempts
        base_meta.attempts = attempts
        base_meta.model = response.model
        base_meta.input_tokens = response.input_tokens
        base_meta.output_tokens = response.output_tokens
        base_meta.estimated_cost_usd = self.settings.estimate_cost(
            response.input_tokens, response.output_tokens
        )

        # --- 5. grounding -------------------------------------------------
        grounding = check_grounding(triage.evidence, safe_text)

        # The drift check is skipped on an abstention. When the model declines
        # to classify, its summary is necessarily about the *absence* of
        # information and shares little vocabulary with the report by design;
        # scoring that as drift would penalise the behaviour the prompt asks
        # for and flood the signal with false positives.
        summary_drifted = not triage.is_abstention and not summary_is_supported(
            triage.summary, safe_text
        )
        if summary_drifted:
            base_meta.warnings.append(
                "Summary shares little vocabulary with the report; check for drift."
            )

        # --- 6. routing ---------------------------------------------------
        # Input warnings and model-output warnings are passed separately: they
        # are different failures with different owners. Conflating them made
        # every drifting summary look like a malformed submission.
        routing = gating.decide(
            triage=triage,
            source_text=safe_text,
            grounding=grounding,
            input_warnings=validation.warnings,
            summary_drifted=summary_drifted,
            settings=self.settings,
        )

        result = TriageResult(
            status=Status.OK,
            incident_id=incident_id,
            category=triage.category,
            priority=triage.priority,
            summary=triage.summary,
            next_action=triage.next_action,
            evidence=triage.evidence,
            missing_information=triage.missing_information,
            reasoning=triage.reasoning,
            category_confidence=triage.category_confidence,
            priority_confidence=triage.priority_confidence,
            overall_confidence=triage.overall_confidence,
            grounding=grounding,
            routing=routing,
            meta=base_meta,
        )

        log_event(
            "triage.completed",
            correlation_id=correlation_id,
            incident_id=incident_id,
            category=result.category.value,
            priority=result.priority.value,
            confidence=result.overall_confidence,
            grounding_ratio=grounding.ratio,
            ungrounded_spans=len(grounding.ungrounded_spans),
            requires_human_review=routing.requires_human_review,
            review_reasons=[r.value for r in routing.reasons],
            attempts=attempts,
            latency_ms=base_meta.latency_ms,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            estimated_cost_usd=base_meta.estimated_cost_usd,
        )
        METRICS.record(result)
        return result

    def run_batch(self, incidents: list[str]) -> list[TriageResult]:
        """Triage several incidents in sequence.

        Sequential on purpose. Concurrency belongs to the platform - in
        production this same call sits behind Pub/Sub and Cloud Run, which
        parallelise across instances and give back-pressure and retries for
        free. A thread pool here would only add a second, weaker copy of that.
        """
        return [self.run(text) for text in incidents]

    def health(self) -> dict[str, object]:
        return {
            "status": "ok",
            "provider": self.provider.health(),
            "prompt_version": PROMPT_VERSION,
            "schema_version": SCHEMA_VERSION,
            "confidence_threshold": self.settings.confidence_threshold,
        }

    # ------------------------------------------------------------------ #
    # Retry
    # ------------------------------------------------------------------ #

    def _call_with_retry(
        self, prompt: str, correlation_id: str, incident_id: str
    ) -> "_CallSuccess | _CallFailure":
        """Call the model, retrying only what is worth retrying.

        Two distinct failures share this loop because both are recoverable by
        trying again: a transient transport error, and a response that parses
        as JSON but violates the contract. The second is rare with constrained
        decoding but not impossible - a confidence of 1.5, a summary of three
        characters - and one more attempt is cheaper than sending an invalid
        record downstream.

        Permanent errors and safety blocks break out immediately. Retrying a
        bad API key just multiplies the latency before the same failure.
        """
        schema = gemini_response_schema()
        last_error: Exception | None = None
        attempts = 0

        for attempt in range(1, self.settings.max_retries + 1):
            attempts = attempt
            try:
                augmented = prompt if attempt == 1 else prompt + _REPAIR_HINT
                response = self.provider.generate(SYSTEM_INSTRUCTION, augmented, schema)
                triage = ModelTriage.model_validate(response.payload)
                return _CallSuccess(triage=triage, response=response, attempts=attempt)

            except (PermanentProviderError, ContentBlockedError) as exc:
                log_event(
                    "triage.provider_error",
                    level=logging.ERROR,
                    correlation_id=correlation_id,
                    incident_id=incident_id,
                    attempt=attempt,
                    retryable=False,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                return _CallFailure(
                    message=str(exc),
                    error_type=type(exc).__name__,
                    attempts=attempt,
                    human_reason=(
                        "the model declined to process this report"
                        if isinstance(exc, ContentBlockedError)
                        else "the model call failed and cannot be retried"
                    ),
                )

            except (TransientProviderError, ValidationError) as exc:
                last_error = exc
                is_last = attempt >= self.settings.max_retries
                log_event(
                    "triage.provider_error",
                    level=logging.WARNING,
                    correlation_id=correlation_id,
                    incident_id=incident_id,
                    attempt=attempt,
                    retryable=not is_last,
                    error_type=type(exc).__name__,
                    error=str(exc)[:500],
                )
                if is_last:
                    break
                self._sleep(self._backoff(attempt, getattr(exc, "retry_after", None)))

            except Exception as exc:  # noqa: BLE001 - last line of defence
                # An unexpected exception must still produce a routed incident
                # rather than a 500 with the report lost in a stack trace.
                log_event(
                    "triage.unexpected_error",
                    level=logging.ERROR,
                    correlation_id=correlation_id,
                    incident_id=incident_id,
                    attempt=attempt,
                    error_type=type(exc).__name__,
                    error=str(exc)[:500],
                )
                return _CallFailure(
                    message=f"{type(exc).__name__}: {exc}",
                    error_type=type(exc).__name__,
                    attempts=attempt,
                    human_reason="an unexpected error occurred during triage",
                )

        return _CallFailure(
            message=f"{type(last_error).__name__}: {last_error}",
            error_type=type(last_error).__name__ if last_error else "Unknown",
            attempts=attempts,
            human_reason="the model did not return a valid triage after several attempts",
        )

    def _backoff(self, attempt: int, retry_after: float | None = None) -> float:
        """How long to wait before the next attempt.

        The server's own hint wins when there is one. Against a live 429 that
        said "retry in 32s", the computed curve below gave up after three
        attempts inside two seconds - three guaranteed failures and a lost
        incident. A rate limiter that tells you when to come back is the best
        available information; anything we compute is a guess about it.

        A small jitter is still added on top, because many instances hitting
        the same limit receive the same hint and would otherwise retry in
        unison and rebuild the spike. The cap stops a very long hint (a daily
        quota can report minutes) from pinning a worker; past that the incident
        is better off failing to a human than blocking.
        """
        if retry_after is not None and retry_after > 0:
            return min(retry_after, _MAX_BACKOFF_SECONDS) + random.uniform(0, 1.0)
        ceiling = min(8.0, 0.5 * (2 ** (attempt - 1)))
        return random.uniform(0, ceiling)


# Upper bound on a server-supplied retry hint. Long enough to absorb a
# per-minute rate limit, short enough that a daily-quota hint measured in
# minutes fails fast to a human instead of holding the worker.
_MAX_BACKOFF_SECONDS = 45.0

_REPAIR_HINT = (
    "\n\nThe previous attempt did not return a valid record. Return only a JSON "
    "object matching the required schema exactly: every field present, "
    "confidences as numbers between 0 and 1, category and priority from the "
    "permitted lists, and evidence quoted verbatim from the report."
)


class _CallSuccess:
    __slots__ = ("triage", "response", "attempts")

    def __init__(self, triage: ModelTriage, response: ProviderResponse, attempts: int) -> None:
        self.triage = triage
        self.response = response
        self.attempts = attempts


class _CallFailure:
    __slots__ = ("message", "error_type", "attempts", "human_reason")

    def __init__(
        self, message: str, error_type: str, attempts: int, human_reason: str
    ) -> None:
        self.message = message
        self.error_type = error_type
        self.attempts = attempts
        self.human_reason = human_reason


__all__ = ["TriagePipeline", "GroundingReport", "TriageResult"]
