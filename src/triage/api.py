"""HTTP surface for the React frontend.

Thin by design. Every rule that matters lives in the pipeline, so the API
translates HTTP to a pipeline call and back, and nothing else. Business logic
that leaks into a route handler is logic the batch path and the tests do not
share.

The human-review endpoints are not decoration. They close the loop: a reviewer
either confirms or corrects a triage, and that decision is exactly the labelled
example an evaluation set is starved of. Capturing it at the moment of review
is far cheaper than commissioning annotation later, and it is the only source
of labels that reflects live traffic rather than a curated sample.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import SETTINGS
from .observability import METRICS, log_event
from .pipeline import TriagePipeline
from .samples import SAMPLE_INCIDENTS
from .schema import Category, Priority, TriageResult

app = FastAPI(
    title="Incident Triage API",
    version="0.1.0",
    description="Gemini-backed first-stage triage for operational incidents.",
)

# The Vite dev server runs on a different origin during development. In
# production the built frontend is served from this same app, so no cross-origin
# request happens at all and this list can be empty.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# Built once at startup, not per request: constructing the provider means
# building an SDK client and its connection pool.
PIPELINE = TriagePipeline()


# --------------------------------------------------------------------------- #
# Request / response models
# --------------------------------------------------------------------------- #


class TriageRequest(BaseModel):
    """Incoming incident.

    Bounds are declared here as well as in the pipeline. This layer rejects
    obvious abuse before any work is done; the pipeline repeats the check
    because it is also reachable from the batch runner and the tests, and a
    validation rule that only exists in the web layer is not a rule.
    """

    text: str = Field(min_length=1, max_length=50_000)
    incident_id: str | None = Field(default=None, max_length=64)


class BatchRequest(BaseModel):
    incidents: list[str] = Field(min_length=1, max_length=25)


class ReviewDecision(BaseModel):
    """A human reviewer's verdict on a triaged incident."""

    incident_id: str
    accepted: bool
    corrected_category: Category | None = None
    corrected_priority: Priority | None = None
    reviewer_note: str = Field(default="", max_length=1000)


# --------------------------------------------------------------------------- #
# In-memory stores
# --------------------------------------------------------------------------- #

# Process-local and lost on restart. Correct for a proof of concept and wrong
# for production, where these become Firestore (queue state) and BigQuery
# (results and reviewer decisions) - see docs/ARCHITECTURE.md. The shape of
# what is stored is the same in both cases, so the swap is a repository
# implementation rather than a redesign.
_RECENT: list[TriageResult] = []
_REVIEW_QUEUE: dict[str, dict[str, Any]] = {}
_REVIEWED: list[dict[str, Any]] = []

_MAX_RECENT = 100


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _remember(result: TriageResult) -> None:
    _RECENT.insert(0, result)
    del _RECENT[_MAX_RECENT:]
    if result.routing.requires_human_review:
        _REVIEW_QUEUE[result.incident_id] = {
            "queued_at": _now(),
            "result": result.model_dump(mode="json"),
        }


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@app.get("/api/health")
def health() -> dict[str, Any]:
    """Liveness plus the configuration a responder needs during an incident."""
    return {**PIPELINE.health(), "time": _now()}


@app.get("/api/config")
def config() -> dict[str, Any]:
    """Vocabularies and thresholds, so the UI never hardcodes a copy of them."""
    return {
        "categories": [c.value for c in Category],
        "priorities": [p.value for p in Priority],
        "confidence_threshold": SETTINGS.confidence_threshold,
        "provider": SETTINGS.provider,
        "model": SETTINGS.model,
    }


@app.get("/api/samples")
def samples() -> list[dict[str, str]]:
    return SAMPLE_INCIDENTS


@app.post("/api/triage")
def triage(request: TriageRequest) -> dict[str, Any]:
    result = PIPELINE.run(request.text, request.incident_id)
    _remember(result)
    # A rejected input is a client error, but the body is still the full
    # result: the UI shows the same panel either way, and the caller gets the
    # correlation id needed to look the request up in the logs.
    return result.model_dump(mode="json")


@app.post("/api/triage/batch")
def triage_batch(request: BatchRequest) -> dict[str, Any]:
    results = PIPELINE.run_batch(request.incidents)
    for result in results:
        _remember(result)
    return {
        "count": len(results),
        "review_required": sum(r.routing.requires_human_review for r in results),
        "results": [r.model_dump(mode="json") for r in results],
    }


@app.get("/api/metrics")
def metrics() -> dict[str, Any]:
    """Snapshot of the counters the dashboard renders."""
    snapshot = METRICS.snapshot()
    snapshot["queue_depth"] = len(_REVIEW_QUEUE)
    snapshot["reviewed"] = len(_REVIEWED)
    if _REVIEWED:
        agreed = sum(entry["accepted"] for entry in _REVIEWED)
        # The single most useful live quality signal: of the triages a human
        # actually checked, how many did they accept unchanged? It needs no
        # labelled dataset and it tracks the real deployment.
        snapshot["reviewer_agreement_rate"] = round(agreed / len(_REVIEWED), 3)
    return snapshot


@app.get("/api/recent")
def recent(limit: int = 20) -> list[dict[str, Any]]:
    return [result.model_dump(mode="json") for result in _RECENT[: max(1, min(limit, 100))]]


@app.get("/api/review-queue")
def review_queue() -> list[dict[str, Any]]:
    """Incidents awaiting a human, newest first."""
    return sorted(_REVIEW_QUEUE.values(), key=lambda entry: entry["queued_at"], reverse=True)


@app.post("/api/review-queue/decision")
def submit_review(decision: ReviewDecision = Body(...)) -> dict[str, Any]:
    """Record a reviewer's verdict and clear the incident from the queue."""
    entry = _REVIEW_QUEUE.pop(decision.incident_id, None)
    if entry is None:
        raise HTTPException(status_code=404, detail="Incident is not in the review queue.")

    original = entry["result"]
    record = {
        "incident_id": decision.incident_id,
        "reviewed_at": _now(),
        "accepted": decision.accepted,
        "model_category": original.get("category"),
        "model_priority": original.get("priority"),
        "corrected_category": decision.corrected_category.value
        if decision.corrected_category
        else None,
        "corrected_priority": decision.corrected_priority.value
        if decision.corrected_priority
        else None,
        "reviewer_note": decision.reviewer_note,
        "correlation_id": original.get("meta", {}).get("correlation_id"),
        "prompt_version": original.get("meta", {}).get("prompt_version"),
    }
    _REVIEWED.append(record)

    # Logged as its own event type so these lines can be routed to a BigQuery
    # sink and become the seed of an evaluation set drawn from live traffic.
    log_event(
        "triage.reviewed",
        level=logging.INFO,
        **record,
    )
    return {"status": "recorded", "queue_depth": len(_REVIEW_QUEUE), "record": record}


@app.get("/api/reviews")
def reviews() -> list[dict[str, Any]]:
    return list(reversed(_REVIEWED))


# --------------------------------------------------------------------------- #
# Static frontend
# --------------------------------------------------------------------------- #

_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"

if _DIST.is_dir():  # pragma: no cover - only present after a frontend build
    app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(_DIST / "index.html")
