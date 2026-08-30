"""Verify that the model's quoted evidence actually appears in the report.

This is the part of the design that turns "reduce hallucination" from an
aspiration into a measurement. Asking a model to be faithful is unfalsifiable;
requiring it to quote the source and then *checking those quotes* produces a
number per incident that can be thresholded, logged, alerted on and tracked
across prompt versions.

Matching is deliberately forgiving about form and strict about substance.
Whitespace, casing and smart quotes are normalised away, because a model that
re-wraps a line has not invented anything. A quote whose words do not appear
in the source is a fabrication regardless of how it is punctuated.
"""

from __future__ import annotations

import re
import unicodedata

from .schema import GroundingReport

_PUNCT = re.compile(r"[^\w\s]")
_WS = re.compile(r"\s+")

# Quotes shorter than this carry no information - "the" appears in everything -
# so they are not counted either way rather than inflating the grounded ratio.
_MIN_SPAN_CHARS = 12


def _normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    # Curly quotes and dashes are routinely re-rendered by models; treating
    # them as differences would produce false fabrication alerts.
    text = text.replace("‘", "'").replace("’", "'")
    text = text.replace("“", '"').replace("”", '"')
    text = text.replace("–", "-").replace("—", "-")
    text = _PUNCT.sub(" ", text)
    return _WS.sub(" ", text).strip()


def _token_overlap(span: str, source: str) -> float:
    """Fraction of the span's words that appear in the source."""
    span_tokens = span.split()
    if not span_tokens:
        return 0.0
    source_tokens = set(source.split())
    return sum(token in source_tokens for token in span_tokens) / len(span_tokens)


def check_grounding(evidence: list[str], source_text: str) -> GroundingReport:
    """Score each evidence span against the incident text.

    A span counts as grounded if it is a substring of the normalised source, or
    if at least 85% of its words appear there. The second test exists because
    an otherwise-faithful quote that drops a stray word is not the failure mode
    worth alerting on; a span assembled from words the report never used is.
    """
    report = GroundingReport()
    if not evidence:
        return report

    normalised_source = _normalise(source_text)

    for span in evidence:
        normalised_span = _normalise(span)
        if len(normalised_span) < _MIN_SPAN_CHARS:
            continue

        report.checked += 1
        if normalised_span in normalised_source:
            report.grounded += 1
        elif _token_overlap(normalised_span, normalised_source) >= 0.85:
            report.grounded += 1
        else:
            report.ungrounded_spans.append(span)

    return report


# Vocabulary a summary may legitimately use without it appearing in the report:
# the taxonomy's own words, plus the framing verbs a summary naturally opens
# with. These are drawn from a closed enum or from the act of summarising, so
# their presence is never evidence of fabrication - and counting them was
# producing false positives on faithful summaries. A summary reading "Reported
# infrastructure outage: <verbatim first sentence>" scored 0.5 and was flagged,
# despite every factual word coming straight from the source.
_TAXONOMY_WORDS = frozenset(
    """
    infrastructure outage application error network connectivity security
    incident data integrity performance degradation hardware failure third
    party service access request user support unknown critical high medium low
    priority reported reports affected affecting issue system systems
    """.split()
)


def summary_is_supported(summary: str, source_text: str, threshold: float = 0.55) -> bool:
    """Weak check that the summary is drawn from the report.

    Summaries are legitimately abstractive, so this cannot be strict without
    firing constantly. It catches the blatant case - a summary describing an
    incident that shares almost no vocabulary with the source - and nothing
    subtler. Deliberately weak: its job is to catch a summary about a different
    incident, not to police paraphrasing.

    Only content words are scored, and taxonomy vocabulary is excluded, so the
    ratio reflects whether the *facts* came from the report rather than whether
    the model happened to name its own category.
    """
    normalised_summary = _normalise(summary)
    normalised_source = _normalise(source_text)
    content_words = [
        word
        for word in normalised_summary.split()
        if len(word) > 4 and word not in _TAXONOMY_WORDS
    ]
    # Too few content words to judge. Scoring two or three words produces a
    # ratio that swings on a single match, which is noise, not signal.
    if len(content_words) < 3:
        return True
    source_tokens = set(normalised_source.split())
    overlap = sum(word in source_tokens for word in content_words) / len(content_words)
    return overlap >= threshold
