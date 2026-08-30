"""Decide whether a human must look at this incident.

The central design claim of this workflow: **the model proposes, the gate
disposes.** A model's self-reported confidence is one input among several, and
on its own it is not trustworthy enough to hold a routing decision. LLMs are
systematically overconfident on inputs that look familiar but are not, so a
threshold on `confidence` alone would let exactly the wrong incidents through.

So review is triggered by four independent kinds of evidence:

1. **Self-reported confidence** below the configured threshold.
2. **Explicit abstention** - the model chose UNKNOWN.
3. **Deterministic policy** - severity and sensitive-topic rules that fire
   regardless of what the model concluded, and that no prompt change can
   silently weaken.
4. **Automated quality checks** - ungrounded quotes, degraded input, internal
   contradictions.

Rules 3 and 4 are the ones that matter most. They keep working when the model
is confidently wrong, which is the only situation where a safety net earns its
cost.
"""

from __future__ import annotations

from .config import SETTINGS, Settings
from .schema import (
    Category,
    GroundingReport,
    ModelTriage,
    Priority,
    ReviewReason,
    RoutingDecision,
)

# Bands that always get human eyes before action, whatever the model says.
# Deliberately conservative: the cost of a reviewed P1 is a few minutes of an
# operator's time, and the cost of an unreviewed wrong P1 is an outage nobody
# was paged for.
_ALWAYS_REVIEW_PRIORITIES = {Priority.P1_CRITICAL}
_ALWAYS_REVIEW_CATEGORIES = {Category.SECURITY_INCIDENT}

_REASON_TEXT = {
    ReviewReason.LOW_CONFIDENCE: "model confidence is below the automation threshold",
    ReviewReason.MODEL_ABSTAINED: "the model could not determine a category or priority",
    ReviewReason.HIGH_SEVERITY: "high-severity incidents are always confirmed by a person",
    ReviewReason.SAFETY_OR_SECURITY_KEYWORD: "the report touches a security, safety or regulatory topic",
    ReviewReason.INSUFFICIENT_INFORMATION: "key facts needed for triage are missing from the report",
    ReviewReason.UNGROUNDED_EVIDENCE: "the model quoted text that does not appear in the report",
    ReviewReason.CONFLICTING_SIGNALS: "the category and priority are inconsistent with each other",
    ReviewReason.MODEL_FAILURE: "the model call did not complete successfully",
    ReviewReason.INPUT_QUALITY: "the submitted report is incomplete or malformed",
}


def _contains_escalation_keyword(text: str, settings: Settings) -> bool:
    lowered = text.lower()
    return any(keyword in lowered for keyword in settings.escalation_keywords)


def decide(
    triage: ModelTriage,
    source_text: str,
    grounding: GroundingReport,
    input_warnings: list[str] | None = None,
    summary_drifted: bool = False,
    settings: Settings = SETTINGS,
) -> RoutingDecision:
    """Apply every routing rule and return the combined decision."""
    reasons: list[ReviewReason] = []

    # --- 1. self-reported confidence -------------------------------------
    if triage.overall_confidence < settings.confidence_threshold:
        reasons.append(ReviewReason.LOW_CONFIDENCE)

    # --- 2. explicit abstention ------------------------------------------
    if triage.category is Category.UNKNOWN or triage.priority is Priority.UNKNOWN:
        reasons.append(ReviewReason.MODEL_ABSTAINED)

    # --- 3. deterministic policy -----------------------------------------
    if triage.priority in _ALWAYS_REVIEW_PRIORITIES:
        reasons.append(ReviewReason.HIGH_SEVERITY)
    if triage.category in _ALWAYS_REVIEW_CATEGORIES:
        reasons.append(ReviewReason.SAFETY_OR_SECURITY_KEYWORD)
    # Checked against the raw report, not the model's reading of it, so a
    # missed classification cannot also disable the keyword backstop.
    if _contains_escalation_keyword(source_text, settings):
        if ReviewReason.SAFETY_OR_SECURITY_KEYWORD not in reasons:
            reasons.append(ReviewReason.SAFETY_OR_SECURITY_KEYWORD)

    # --- 4. automated quality checks -------------------------------------
    if grounding.checked and grounding.ratio < settings.grounding_threshold:
        reasons.append(ReviewReason.UNGROUNDED_EVIDENCE)
    elif summary_drifted:
        # A summary drawn from vocabulary the report never used is the same
        # class of problem as a fabricated quote, so it carries the same reason
        # code rather than being filed as a bad submission.
        reasons.append(ReviewReason.UNGROUNDED_EVIDENCE)

    if len(triage.missing_information) >= 2:
        reasons.append(ReviewReason.INSUFFICIENT_INFORMATION)

    if _is_contradictory(triage):
        reasons.append(ReviewReason.CONFLICTING_SIGNALS)

    if input_warnings:
        reasons.append(ReviewReason.INPUT_QUALITY)

    if not reasons:
        return RoutingDecision(
            requires_human_review=False,
            reasons=[],
            explanation=(
                "Auto-triaged: confidence is above threshold, all quoted evidence "
                "was found in the report, and no policy rule was triggered."
            ),
        )

    ordered = list(dict.fromkeys(reasons))  # de-duplicate, keep rule order
    explanation = "Routed for human review because " + "; ".join(
        _REASON_TEXT[reason] for reason in ordered
    ) + "."
    return RoutingDecision(requires_human_review=True, reasons=ordered, explanation=explanation)


def _is_contradictory(triage: ModelTriage) -> bool:
    """Catch category/priority pairs that cannot both be right.

    Cheap consistency checks like these are worth more than they look: they
    catch a model that has half-followed the rubric, and unlike the confidence
    score they cannot be talked up by the model itself.
    """
    # A confident P1 with nothing quoted from the report to support it.
    if triage.priority is Priority.P1_CRITICAL and not triage.evidence:
        return True
    # Routine request categories are almost never genuinely critical; when the
    # model says both, one of the two is wrong.
    if triage.category in {Category.ACCESS_REQUEST, Category.USER_SUPPORT}:
        if triage.priority is Priority.P1_CRITICAL:
            return True
    # A named category paired with an unknown priority, asserted confidently,
    # means the model has not actually assessed impact.
    if triage.priority is Priority.UNKNOWN and triage.category_confidence >= 0.9:
        return True
    return False


def failure_routing(reason: str) -> RoutingDecision:
    """Routing for an incident whose model call failed.

    Failures route to a person rather than being dropped or retried forever.
    An incident that cannot be triaged automatically is still an incident.
    """
    return RoutingDecision(
        requires_human_review=True,
        reasons=[ReviewReason.MODEL_FAILURE],
        explanation=f"Routed for human review because {reason}",
    )


def rejection_routing(errors: list[str]) -> RoutingDecision:
    """Routing for input that never reached the model."""
    return RoutingDecision(
        requires_human_review=True,
        reasons=[ReviewReason.INPUT_QUALITY],
        explanation="Rejected before triage: " + " ".join(errors),
    )
