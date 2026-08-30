"""Contract tests for the structured output."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from triage.schema import (
    Category,
    ModelTriage,
    Priority,
    gemini_response_schema,
)


def _payload(**overrides) -> dict:
    base = {
        "category": "APPLICATION_ERROR",
        "priority": "P3_MEDIUM",
        "summary": "The billing job failed overnight with an exception.",
        "next_action": "Assign to the billing application team to review the job log.",
        "evidence": ["billing job failed"],
        "missing_information": [],
        "category_confidence": 0.8,
        "priority_confidence": 0.7,
        "reasoning": "Explicit application exception.",
    }
    base.update(overrides)
    return base


def test_valid_payload_parses():
    triage = ModelTriage.model_validate(_payload())
    assert triage.category is Category.APPLICATION_ERROR
    assert triage.priority is Priority.P3_MEDIUM


def test_unknown_category_value_is_rejected():
    """A category outside the taxonomy must fail rather than pass through."""
    with pytest.raises(ValidationError):
        ModelTriage.model_validate(_payload(category="SOMETHING_ELSE"))


def test_confidence_outside_range_is_rejected():
    with pytest.raises(ValidationError):
        ModelTriage.model_validate(_payload(category_confidence=1.4))
    with pytest.raises(ValidationError):
        ModelTriage.model_validate(_payload(priority_confidence=-0.1))


def test_extra_fields_are_rejected():
    """Silent acceptance of unexpected keys hides prompt/schema drift."""
    with pytest.raises(ValidationError):
        ModelTriage.model_validate(_payload(severity="high"))


def test_short_summary_is_rejected():
    with pytest.raises(ValidationError):
        ModelTriage.model_validate(_payload(summary="down"))


def test_overall_confidence_is_the_minimum_not_the_mean():
    """A confident category must not mask an uncertain priority."""
    triage = ModelTriage.model_validate(
        _payload(category_confidence=0.95, priority_confidence=0.35)
    )
    assert triage.overall_confidence == 0.35


def test_unknown_category_cannot_claim_high_confidence():
    """An abstention arriving with 0.99 confidence would defeat the gate."""
    triage = ModelTriage.model_validate(
        _payload(category="UNKNOWN", category_confidence=0.99)
    )
    assert triage.category_confidence <= 0.5
    assert triage.is_abstention


def test_whitespace_is_collapsed():
    triage = ModelTriage.model_validate(
        _payload(summary="The   billing\n\njob   failed overnight badly.")
    )
    assert "  " not in triage.summary


def test_gemini_schema_matches_the_pydantic_model():
    """Guards the one duplication in the codebase.

    The Gemini schema is hand-written for dialect reasons, so this asserts the
    two definitions cannot drift apart unnoticed.
    """
    schema_fields = set(gemini_response_schema()["properties"])
    model_fields = set(ModelTriage.model_fields)
    assert schema_fields == model_fields


def test_gemini_schema_enums_cover_every_taxonomy_value():
    properties = gemini_response_schema()["properties"]
    assert set(properties["category"]["enum"]) == {c.value for c in Category}
    assert set(properties["priority"]["enum"]) == {p.value for p in Priority}
