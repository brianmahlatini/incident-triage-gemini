"""The data contract for the triage workflow.

Everything the model is allowed to say is described here, once. The same
definitions drive three things that would otherwise drift apart:

  1. the JSON schema handed to Gemini for constrained decoding,
  2. the validation applied to whatever comes back,
  3. the response body served to the React frontend.

Keeping one source of truth is the main defence against the classic LLM
integration failure: the prompt says one thing, the parser expects another,
and the mismatch only shows up in production.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

# --------------------------------------------------------------------------- #
# Controlled vocabularies
# --------------------------------------------------------------------------- #


class Category(str, Enum):
    """Closed set of incident categories.

    UNKNOWN is deliberately part of the taxonomy. A model with no way to say
    "I cannot tell" will always pick the nearest-looking label, and a confident
    wrong category is far more expensive to unwind than an explicit abstention.
    """

    INFRASTRUCTURE_OUTAGE = "INFRASTRUCTURE_OUTAGE"
    APPLICATION_ERROR = "APPLICATION_ERROR"
    NETWORK_CONNECTIVITY = "NETWORK_CONNECTIVITY"
    SECURITY_INCIDENT = "SECURITY_INCIDENT"
    DATA_INTEGRITY = "DATA_INTEGRITY"
    PERFORMANCE_DEGRADATION = "PERFORMANCE_DEGRADATION"
    HARDWARE_FAILURE = "HARDWARE_FAILURE"
    THIRD_PARTY_SERVICE = "THIRD_PARTY_SERVICE"
    ACCESS_REQUEST = "ACCESS_REQUEST"
    USER_SUPPORT = "USER_SUPPORT"
    UNKNOWN = "UNKNOWN"


class Priority(str, Enum):
    """Suggested priority, mapped to the operational definitions in the prompt."""

    P1_CRITICAL = "P1_CRITICAL"
    P2_HIGH = "P2_HIGH"
    P3_MEDIUM = "P3_MEDIUM"
    P4_LOW = "P4_LOW"
    UNKNOWN = "UNKNOWN"


# Ordinal view of priority, so evaluation can measure *how far* off a
# misprediction was. Treating P1-called-P4 the same as P2-called-P3 would hide
# the only errors that actually matter.
PRIORITY_RANK: dict[Priority, int] = {
    Priority.P1_CRITICAL: 1,
    Priority.P2_HIGH: 2,
    Priority.P3_MEDIUM: 3,
    Priority.P4_LOW: 4,
}


class ReviewReason(str, Enum):
    """Why an incident was routed to a human.

    Enumerated rather than free text so the deferral rate can be sliced by
    cause in monitoring: "review rate is up" is not actionable, "review rate is
    up because LOW_CONFIDENCE tripled after the prompt change" is.
    """

    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    MODEL_ABSTAINED = "MODEL_ABSTAINED"
    HIGH_SEVERITY = "HIGH_SEVERITY"
    SAFETY_OR_SECURITY_KEYWORD = "SAFETY_OR_SECURITY_KEYWORD"
    INSUFFICIENT_INFORMATION = "INSUFFICIENT_INFORMATION"
    UNGROUNDED_EVIDENCE = "UNGROUNDED_EVIDENCE"
    CONFLICTING_SIGNALS = "CONFLICTING_SIGNALS"
    MODEL_FAILURE = "MODEL_FAILURE"
    INPUT_QUALITY = "INPUT_QUALITY"


class Status(str, Enum):
    """Outcome of the workflow as a whole, not of the model call."""

    OK = "OK"  # model answered, output validated
    REJECTED = "REJECTED"  # input failed validation, no model call made
    FAILED = "FAILED"  # model or transport error after retries


# --------------------------------------------------------------------------- #
# What the model is asked to produce
# --------------------------------------------------------------------------- #


class ModelTriage(BaseModel):
    """The model's own assessment.

    This is *only* the model's opinion. It is never served directly: the
    pipeline layers deterministic checks and routing rules on top, because a
    model's self-reported confidence is an input to the decision, not the
    decision itself.
    """

    model_config = {"extra": "forbid"}

    category: Category
    priority: Priority

    summary: str = Field(
        min_length=10,
        max_length=400,
        description="One or two sentences describing the incident, in plain language.",
    )
    next_action: str = Field(
        min_length=10,
        max_length=400,
        description="The single next step the operations team should take.",
    )

    evidence: list[str] = Field(
        default_factory=list,
        max_length=5,
        description=(
            "Short quotes copied verbatim from the incident text that justify "
            "the category and priority. Checked against the source."
        ),
    )
    missing_information: list[str] = Field(
        default_factory=list,
        max_length=5,
        description="Facts a human would need that the report does not contain.",
    )

    category_confidence: float = Field(ge=0.0, le=1.0)
    priority_confidence: float = Field(ge=0.0, le=1.0)

    reasoning: str = Field(
        default="",
        max_length=600,
        description="Brief justification. Shown to reviewers, not to end users.",
    )

    @field_validator("summary", "next_action", "reasoning")
    @classmethod
    def _collapse_whitespace(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator("evidence", "missing_information")
    @classmethod
    def _clean_list(cls, values: list[str]) -> list[str]:
        return [" ".join(v.split()) for v in values if v and v.strip()]

    @model_validator(mode="after")
    def _abstention_is_consistent(self) -> ModelTriage:
        """An abstention must not arrive wearing a confident face.

        If the model says UNKNOWN it cannot also claim high confidence; that
        combination is incoherent and, left unchecked, would let an abstention
        sail through the confidence gate.
        """
        if self.category is Category.UNKNOWN:
            self.category_confidence = min(self.category_confidence, 0.5)
        if self.priority is Priority.UNKNOWN:
            self.priority_confidence = min(self.priority_confidence, 0.5)
        return self

    @property
    def is_abstention(self) -> bool:
        """Whether the model declined to classify either dimension."""
        return self.category is Category.UNKNOWN or self.priority is Priority.UNKNOWN

    @property
    def overall_confidence(self) -> float:
        """Deliberately the *minimum*, not the mean.

        A right category with a wildly wrong priority is still a bad triage, so
        the weakest component governs. Averaging would let one confident field
        mask an uncertain one.
        """
        return round(min(self.category_confidence, self.priority_confidence), 3)


# --------------------------------------------------------------------------- #
# What the workflow returns
# --------------------------------------------------------------------------- #


class GroundingReport(BaseModel):
    """Result of checking the model's quoted evidence against the source text."""

    checked: int = 0
    grounded: int = 0
    ungrounded_spans: list[str] = Field(default_factory=list)

    @property
    def ratio(self) -> float:
        return 1.0 if self.checked == 0 else round(self.grounded / self.checked, 3)


class RoutingDecision(BaseModel):
    """The workflow's decision about human involvement."""

    requires_human_review: bool
    reasons: list[ReviewReason] = Field(default_factory=list)
    explanation: str = ""


class TriageMeta(BaseModel):
    """Observability payload attached to every result."""

    correlation_id: str
    provider: str
    model: str
    latency_ms: int = 0
    attempts: int = 1
    prompt_version: str = ""
    schema_version: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    estimated_cost_usd: float | None = None
    redactions: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class TriageResult(BaseModel):
    """The complete, validated response served to callers."""

    status: Status
    incident_id: str

    category: Category = Category.UNKNOWN
    priority: Priority = Priority.UNKNOWN
    summary: str = ""
    next_action: str = ""

    evidence: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    reasoning: str = ""

    category_confidence: float = 0.0
    priority_confidence: float = 0.0
    overall_confidence: float = 0.0

    grounding: GroundingReport = Field(default_factory=GroundingReport)
    routing: RoutingDecision
    meta: TriageMeta

    error: str | None = None


# --------------------------------------------------------------------------- #
# Schema handed to Gemini
# --------------------------------------------------------------------------- #

SCHEMA_VERSION = "triage-v1"


def gemini_response_schema() -> dict[str, Any]:
    """JSON schema for Gemini's constrained decoding.

    Hand-written rather than derived from ``ModelTriage.model_json_schema()``
    on purpose: Pydantic emits ``$defs``/``$ref`` for the enums plus extra
    keywords the Gemini schema dialect does not accept. Generating and then
    stripping that output is more fragile than stating these few lines
    directly, and the drift risk is covered by a test asserting that this
    schema and the Pydantic model agree on field names.
    """
    return {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "enum": [c.value for c in Category],
                "description": (
                    "Best-fitting category, or UNKNOWN if the report does not "
                    "support a choice."
                ),
            },
            "priority": {
                "type": "string",
                "enum": [p.value for p in Priority],
                "description": "Suggested priority using the definitions in the instructions.",
            },
            "summary": {
                "type": "string",
                "description": "One or two plain-language sentences. Facts from the report only.",
            },
            "next_action": {
                "type": "string",
                "description": "The single most useful next step for the operations team.",
            },
            "evidence": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Up to 3 short quotes copied verbatim from the incident text.",
            },
            "missing_information": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Facts needed for confident triage that the report does not contain.",
            },
            "category_confidence": {
                "type": "number",
                "description": "0.0-1.0 confidence in the category.",
            },
            "priority_confidence": {
                "type": "number",
                "description": "0.0-1.0 confidence in the priority.",
            },
            "reasoning": {
                "type": "string",
                "description": "Two sentences at most, justifying the category and priority.",
            },
        },
        "required": [
            "category",
            "priority",
            "summary",
            "next_action",
            "evidence",
            "missing_information",
            "category_confidence",
            "priority_confidence",
            "reasoning",
        ],
    }
