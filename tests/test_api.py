"""HTTP surface, including the human-review loop."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from triage.api import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_health_reports_provider_and_versions(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["provider"]["ready"] is True
    assert body["prompt_version"]


def test_config_exposes_the_vocabularies(client):
    """The UI must not keep its own copy of the taxonomy."""
    body = client.get("/api/config").json()
    assert "UNKNOWN" in body["categories"]
    assert "P1_CRITICAL" in body["priorities"]


def test_triage_returns_a_structured_result(client):
    response = client.post(
        "/api/triage",
        json={"text": "The claims portal is down for all 200 branch users since 09:00."},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "OK"
    assert body["category"]
    assert body["routing"]["requires_human_review"] in {True, False}
    assert body["meta"]["correlation_id"]


def test_short_input_is_rejected_with_a_full_result_body(client):
    """The caller still gets a correlation id and a routing decision."""
    body = client.post("/api/triage", json={"text": "help"}).json()
    assert body["status"] == "REJECTED"
    assert body["routing"]["requires_human_review"] is True
    assert body["error"]


def test_empty_text_fails_request_validation(client):
    assert client.post("/api/triage", json={"text": ""}).status_code == 422


def test_oversized_request_is_refused_at_the_edge(client):
    assert client.post("/api/triage", json={"text": "x" * 60_000}).status_code == 422


def test_batch_endpoint(client):
    response = client.post(
        "/api/triage/batch",
        json={
            "incidents": [
                "The VPN is down for the Cape Town office, nobody can connect.",
                "How do I change my email signature in Outlook?",
            ]
        },
    )
    body = response.json()
    assert body["count"] == 2
    assert len(body["results"]) == 2


def test_review_queue_receives_flagged_incidents(client):
    client.post(
        "/api/triage",
        json={
            "incident_id": "INC-QUEUE-TEST",
            "text": "Something is broken somewhere, please look into it when you can.",
        },
    )
    queued = [entry["result"]["incident_id"] for entry in client.get("/api/review-queue").json()]
    assert "INC-QUEUE-TEST" in queued


def test_reviewer_decision_clears_the_queue_and_is_recorded(client):
    client.post(
        "/api/triage",
        json={
            "incident_id": "INC-REVIEW-TEST",
            "text": "Ransomware note found on the finance file share this morning.",
        },
    )
    response = client.post(
        "/api/review-queue/decision",
        json={
            "incident_id": "INC-REVIEW-TEST",
            "accepted": False,
            "corrected_priority": "P1_CRITICAL",
            "reviewer_note": "Confirmed active encryption, escalating.",
        },
    )
    assert response.status_code == 200
    record = response.json()["record"]
    assert record["corrected_priority"] == "P1_CRITICAL"

    remaining = [e["result"]["incident_id"] for e in client.get("/api/review-queue").json()]
    assert "INC-REVIEW-TEST" not in remaining
    assert any(r["incident_id"] == "INC-REVIEW-TEST" for r in client.get("/api/reviews").json())


def test_decision_on_an_unknown_incident_is_a_404(client):
    response = client.post(
        "/api/review-queue/decision", json={"incident_id": "INC-NOPE", "accepted": True}
    )
    assert response.status_code == 404


def test_correction_with_an_invalid_category_is_rejected(client):
    """Reviewer corrections are held to the same taxonomy as the model."""
    response = client.post(
        "/api/review-queue/decision",
        json={"incident_id": "INC-X", "accepted": False, "corrected_category": "MADE_UP"},
    )
    assert response.status_code == 422


def test_metrics_track_throughput_and_review_rate(client):
    client.post("/api/triage", json={"text": "The payroll batch failed with an exception."})
    body = client.get("/api/metrics").json()
    assert body["total"] >= 1
    assert 0.0 <= body["review_rate"] <= 1.0
    assert "by_category" in body
