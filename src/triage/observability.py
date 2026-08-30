"""Structured logging and in-process metrics.

Logs are emitted as one JSON object per line on stdout. That is not a stylistic
preference: it is the format Cloud Logging ingests natively from Cloud Run and
GKE, and `severity` and `trace` are the exact field names it promotes into
first-class log properties. The same lines stay readable with `jq` locally, so
there is no separate "local logging" mode to drift out of step.

The rule that shapes everything here: **the incident text is never logged.**
Not the original, and not the redacted version. Logs are the least controlled
copy of any data - they fan out to sinks, get exported to BigQuery, and are
readable by people who would never be granted access to the source system. What
is logged instead is a SHA-256 prefix of the text, which supports the questions
that actually get asked in an incident review - is this the same report we saw
earlier? did this input change between retries? - without holding the content.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

from .config import SETTINGS

_LOGGER_NAME = "triage"

# Python level names differ from the strings Cloud Logging understands.
_SEVERITY = {
    "DEBUG": "DEBUG",
    "INFO": "INFO",
    "WARNING": "WARNING",
    "ERROR": "ERROR",
    "CRITICAL": "CRITICAL",
}


class CloudLoggingFormatter(logging.Formatter):
    """Render records as Cloud Logging structured JSON."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "severity": _SEVERITY.get(record.levelname, "DEFAULT"),
            "message": record.getMessage(),
            "logger": record.name,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
        }
        # Anything passed via extra={"context": {...}} is merged in flat, which
        # is what makes fields queryable in Cloud Logging rather than buried in
        # a formatted message string.
        context = getattr(record, "context", None)
        if isinstance(context, dict):
            payload.update(context)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def get_logger() -> logging.Logger:
    """Return the configured triage logger (idempotent)."""
    logger = logging.getLogger(_LOGGER_NAME)
    if not logger.handlers:
        # stderr, not stdout: it keeps machine-readable program output (the CLI
        # report, the evaluation JSON) separable from the log stream by simple
        # redirection. Cloud Logging captures both streams from Cloud Run, so
        # nothing is lost in production by choosing the more useful one locally.
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(CloudLoggingFormatter())
        logger.addHandler(handler)
        logger.setLevel(getattr(logging, SETTINGS.log_level, logging.INFO))
        logger.propagate = False
    return logger


LOGGER = get_logger()


def new_correlation_id() -> str:
    """One id per incident, threaded through logs, metrics and the response.

    Returned to the caller as well as logged, so a user reporting "this triage
    looks wrong" hands over an id that goes straight to the exact request.
    """
    return uuid.uuid4().hex[:16]


def text_fingerprint(text: str) -> str:
    """Stable, non-reversible identifier for an input.

    Truncated to 16 hex characters - enough to distinguish inputs in practice,
    short enough to read in a log line, and not a rainbow-table target for
    short texts the way a full digest of a known-format string would be.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def log_event(event: str, level: int = logging.INFO, **fields: Any) -> None:
    """Emit one structured event."""
    LOGGER.log(level, event, extra={"context": {"event": event, **fields}})


@contextmanager
def timed() -> Iterator[dict[str, int]]:
    """Measure wall-clock duration of a block in milliseconds."""
    holder: dict[str, int] = {"elapsed_ms": 0}
    started = time.perf_counter()
    try:
        yield holder
    finally:
        holder["elapsed_ms"] = int((time.perf_counter() - started) * 1000)


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #


@dataclass
class Metrics:
    """Counters for the demo's live dashboard.

    In-process and therefore per-instance: correct for a proof of concept,
    wrong for production, where these same measurements would be written as
    Cloud Monitoring custom metrics (or derived from the BigQuery results
    table) so they survive restarts and aggregate across instances. Keeping
    the names identical to the intended production metric names means the
    dashboards do not have to be rebuilt at that point.
    """

    total: int = 0
    ok: int = 0
    rejected: int = 0
    failed: int = 0
    review_required: int = 0
    auto_triaged: int = 0
    retries: int = 0
    latencies_ms: list[int] = field(default_factory=list)
    by_category: dict[str, int] = field(default_factory=dict)
    by_priority: dict[str, int] = field(default_factory=dict)
    by_review_reason: dict[str, int] = field(default_factory=dict)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    estimated_cost_usd: float = 0.0

    def record(self, result: Any) -> None:
        """Fold one :class:`~triage.schema.TriageResult` into the counters."""
        self.total += 1
        status = result.status.value
        if status == "OK":
            self.ok += 1
        elif status == "REJECTED":
            self.rejected += 1
        else:
            self.failed += 1

        if result.routing.requires_human_review:
            self.review_required += 1
        else:
            self.auto_triaged += 1

        for reason in result.routing.reasons:
            key = reason.value
            self.by_review_reason[key] = self.by_review_reason.get(key, 0) + 1

        if status == "OK":
            self.by_category[result.category.value] = (
                self.by_category.get(result.category.value, 0) + 1
            )
            self.by_priority[result.priority.value] = (
                self.by_priority.get(result.priority.value, 0) + 1
            )

        meta = result.meta
        self.retries += max(0, meta.attempts - 1)
        if meta.latency_ms:
            # Bounded so a long-running instance cannot grow this list without
            # limit; percentiles over the recent window are what gets watched.
            self.latencies_ms.append(meta.latency_ms)
            del self.latencies_ms[:-1000]
        self.total_input_tokens += meta.input_tokens or 0
        self.total_output_tokens += meta.output_tokens or 0
        self.estimated_cost_usd = round(
            self.estimated_cost_usd + (meta.estimated_cost_usd or 0.0), 6
        )

    def _percentile(self, pct: float) -> int:
        if not self.latencies_ms:
            return 0
        ordered = sorted(self.latencies_ms)
        index = min(len(ordered) - 1, int(round(pct / 100 * (len(ordered) - 1))))
        return ordered[index]

    def snapshot(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "ok": self.ok,
            "rejected": self.rejected,
            "failed": self.failed,
            "review_required": self.review_required,
            "auto_triaged": self.auto_triaged,
            "review_rate": round(self.review_required / self.total, 3) if self.total else 0.0,
            "failure_rate": round(self.failed / self.total, 3) if self.total else 0.0,
            "retries": self.retries,
            "latency_p50_ms": self._percentile(50),
            "latency_p95_ms": self._percentile(95),
            "by_category": self.by_category,
            "by_priority": self.by_priority,
            "by_review_reason": self.by_review_reason,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
        }

    def reset(self) -> None:
        self.__init__()  # type: ignore[misc]


METRICS = Metrics()
