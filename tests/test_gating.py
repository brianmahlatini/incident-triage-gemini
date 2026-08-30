"""Routing rules - the layer that decides when a human is needed."""

from __future__ import annotations

from triage.config import Settings
from triage.gating import decide, failure_routing
from triage.schema import GroundingReport, ModelTriage, ReviewReason

SETTINGS = Settings(confidence_threshold=0.70)
CLEAN = GroundingReport(checked=1, grounded=1)
SOURCE = "The reporting service is slow for the finance team since 14:00 today."


def _triage(**overrides) -> ModelTriage:
    base = {
        "category": "PERFORMANCE_DEGRADATION",
        "priority": "P3_MEDIUM",
        "summary": "The reporting service is responding slowly for the finance team.",
        "next_action": "Check resource utilisation on the reporting service.",
        "evidence": ["reporting service is slow"],
        "missing_information": [],
        "category_confidence": 0.85,
        "priority_confidence": 0.80,
        "reasoning": "Slowness reported without unavailability.",
    }
    base.update(overrides)
    return ModelTriage.model_validate(base)


def test_confident_routine_incident_is_auto_triaged():
    decision = decide(_triage(), SOURCE, CLEAN, settings=SETTINGS)
    assert not decision.requires_human_review
    assert decision.reasons == []


def test_low_confidence_triggers_review():
    decision = decide(_triage(priority_confidence=0.4), SOURCE, CLEAN, settings=SETTINGS)
    assert decision.requires_human_review
    assert ReviewReason.LOW_CONFIDENCE in decision.reasons


def test_abstention_triggers_review():
    decision = decide(_triage(category="UNKNOWN"), SOURCE, CLEAN, settings=SETTINGS)
    assert ReviewReason.MODEL_ABSTAINED in decision.reasons


def test_p1_always_triggers_review_even_when_highly_confident():
    """The policy backstop: no confidence score buys an unreviewed P1."""
    decision = decide(
        _triage(
            priority="P1_CRITICAL",
            category_confidence=0.99,
            priority_confidence=0.99,
            evidence=["reporting service is slow"],
        ),
        SOURCE,
        CLEAN,
        settings=SETTINGS,
    )
    assert decision.requires_human_review
    assert ReviewReason.HIGH_SEVERITY in decision.reasons


def test_security_category_always_triggers_review():
    decision = decide(
        _triage(category="SECURITY_INCIDENT", category_confidence=0.99),
        SOURCE,
        CLEAN,
        settings=SETTINGS,
    )
    assert ReviewReason.SAFETY_OR_SECURITY_KEYWORD in decision.reasons


def test_keyword_backstop_fires_on_the_raw_report():
    """Works even when the model classified the incident as something mundane.

    This is the rule that survives a confidently wrong model, which is the only
    case where a backstop is worth having.
    """
    source = "Finance laptop is slow. A ransomware note appeared on the desktop."
    decision = decide(_triage(), source, CLEAN, settings=SETTINGS)
    assert decision.requires_human_review
    assert ReviewReason.SAFETY_OR_SECURITY_KEYWORD in decision.reasons


def test_ungrounded_evidence_triggers_review():
    poor = GroundingReport(checked=2, grounded=0, ungrounded_spans=["invented", "also invented"])
    decision = decide(_triage(), SOURCE, poor, settings=SETTINGS)
    assert ReviewReason.UNGROUNDED_EVIDENCE in decision.reasons


def test_drifting_summary_is_reported_as_a_grounding_problem():
    decision = decide(_triage(), SOURCE, CLEAN, summary_drifted=True, settings=SETTINGS)
    assert ReviewReason.UNGROUNDED_EVIDENCE in decision.reasons


def test_several_missing_facts_trigger_review():
    decision = decide(
        _triage(missing_information=["Which system", "How many users"]),
        SOURCE,
        CLEAN,
        settings=SETTINGS,
    )
    assert ReviewReason.INSUFFICIENT_INFORMATION in decision.reasons


def test_input_warnings_trigger_review():
    decision = decide(
        _triage(), SOURCE, CLEAN, input_warnings=["Very short report"], settings=SETTINGS
    )
    assert ReviewReason.INPUT_QUALITY in decision.reasons


def test_p1_without_supporting_evidence_is_contradictory():
    decision = decide(
        _triage(priority="P1_CRITICAL", evidence=[]), SOURCE, GroundingReport(), settings=SETTINGS
    )
    assert ReviewReason.CONFLICTING_SIGNALS in decision.reasons


def test_critical_access_request_is_contradictory():
    decision = decide(
        _triage(category="ACCESS_REQUEST", priority="P1_CRITICAL"),
        SOURCE,
        CLEAN,
        settings=SETTINGS,
    )
    assert ReviewReason.CONFLICTING_SIGNALS in decision.reasons


def test_reasons_are_deduplicated():
    source = "Ransomware detected on the finance share."
    decision = decide(
        _triage(category="SECURITY_INCIDENT"), source, CLEAN, settings=SETTINGS
    )
    assert len(decision.reasons) == len(set(decision.reasons))


def test_failure_always_routes_to_a_human():
    """An incident that could not be triaged is still an incident."""
    decision = failure_routing("the model call failed")
    assert decision.requires_human_review
    assert ReviewReason.MODEL_FAILURE in decision.reasons
