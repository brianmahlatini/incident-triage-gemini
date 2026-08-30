"""Grounding: the hallucination check that produces a number."""

from __future__ import annotations

from triage.grounding import check_grounding, summary_is_supported

SOURCE = (
    "The core policy database prod-db-01 went offline at 06:14 this morning. "
    "All 300 call centre agents cannot process claims and there is no workaround."
)


def test_verbatim_quote_is_grounded():
    report = check_grounding(["prod-db-01 went offline at 06:14"], SOURCE)
    assert report.checked == 1
    assert report.grounded == 1
    assert report.ratio == 1.0


def test_case_and_whitespace_differences_still_ground():
    """Reformatting is not fabrication."""
    report = check_grounding(["PROD-DB-01   went\noffline  at 06:14"], SOURCE)
    assert report.grounded == 1


def test_punctuation_and_smart_quotes_are_normalised():
    report = check_grounding(["All 300 call centre agents cannot process claims!"], SOURCE)
    assert report.grounded == 1


def test_fabricated_quote_is_caught():
    """The failure this whole module exists for."""
    report = check_grounding(
        ["the backup datacentre in Durban also failed over at 07:00"], SOURCE
    )
    assert report.checked == 1
    assert report.grounded == 0
    assert report.ungrounded_spans


def test_mixed_evidence_produces_a_partial_ratio():
    report = check_grounding(
        ["All 300 call centre agents cannot process claims", "root cause was a firmware bug"],
        SOURCE,
    )
    assert report.checked == 2
    assert report.grounded == 1
    assert report.ratio == 0.5


def test_trivially_short_spans_are_not_counted():
    """A three-word quote proves nothing either way, so it is not scored."""
    report = check_grounding(["the", "at 06:14"], SOURCE)
    assert report.checked == 0
    assert report.ratio == 1.0


def test_no_evidence_is_not_treated_as_ungrounded():
    report = check_grounding([], SOURCE)
    assert report.checked == 0
    assert not report.ungrounded_spans


def test_summary_drawn_from_the_report_is_supported():
    assert summary_is_supported(
        "The core policy database went offline and agents cannot process claims.", SOURCE
    )


def test_summary_about_a_different_incident_is_unsupported():
    assert not summary_is_supported(
        "A ransomware infection encrypted several finance workstations overnight.", SOURCE
    )
