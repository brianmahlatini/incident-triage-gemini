"""Deterministic offline provider.

This exists for three practical reasons, and it is not a throwaway:

* **The workflow must be demonstrable without credentials.** A reviewer can
  clone the repo and see the whole pipeline run end to end in seconds.
* **Tests must not call a paid, non-deterministic API.** Everything around the
  model - validation, redaction, grounding, routing, error handling - is
  deterministic and deserves deterministic tests.
* **It is a floor for evaluation.** Keyword rules are the honest baseline any
  LLM has to beat. If Gemini cannot outperform this on the eval set, the
  interesting finding is that the problem did not need an LLM.

It mimics the *behaviour* that matters, not the intelligence: it abstains on
vague input, quotes real spans from the source, and reports lower confidence
when the evidence is thin.
"""

from __future__ import annotations

import re
from typing import Any

from ..config import SETTINGS, Settings
from ..schema import Category, Priority
from .base import Provider, ProviderResponse, TransientProviderError

# Keyword weights per category. Ordering within a list is irrelevant; weights
# encode how diagnostic a term is - "ransomware" is decisive, "error" is not.
_CATEGORY_SIGNALS: dict[Category, list[tuple[str, float]]] = {
    Category.SECURITY_INCIDENT: [
        ("ransomware", 5.0), ("phishing", 4.0), ("malware", 4.0), ("breach", 4.0),
        ("compromise", 3.5), ("unauthorised", 3.0), ("unauthorized", 3.0),
        ("exfiltrat", 4.0), ("brute force", 3.5), ("suspicious login", 3.0),
        ("credential", 2.0), ("virus", 3.0), ("attack", 2.0),
    ],
    Category.INFRASTRUCTURE_OUTAGE: [
        ("outage", 3.5), ("is down", 3.5), ("went down", 3.5), ("server down", 4.0),
        ("cluster", 2.0), ("failover", 2.5), ("unavailable", 2.5), ("offline", 2.5),
        ("database", 2.0), ("kubernetes", 2.0), ("vm ", 1.5), ("restart", 1.0),
    ],
    Category.APPLICATION_ERROR: [
        ("exception", 3.0), ("stack trace", 3.5), ("error 500", 3.0), ("nullpointer", 3.5),
        ("crash", 2.5), ("failed job", 2.5), ("batch failed", 3.0), ("bug", 2.0),
        ("not calculating", 2.5), ("incorrect result", 2.5), ("500 error", 3.0),
    ],
    Category.NETWORK_CONNECTIVITY: [
        ("vpn", 3.5), ("dns", 3.5), ("firewall", 3.0), ("packet loss", 3.5),
        ("cannot connect", 2.5), ("no connectivity", 3.5), ("link down", 3.5),
        ("routing", 2.5), ("mpls", 3.0), ("network", 1.5),
    ],
    Category.DATA_INTEGRITY: [
        ("duplicate record", 3.5), ("missing records", 3.5), ("data mismatch", 3.5),
        ("corrupt", 3.0), ("out of sync", 3.0), ("reconcil", 2.5), ("stale data", 3.0),
        ("wrong balance", 3.0), ("another client", 3.5), ("wrong details", 3.0),
        ("incorrect details", 3.0), ("mixed up", 2.5), ("wrong customer", 3.5),
    ],
    Category.PERFORMANCE_DEGRADATION: [
        ("slow", 2.5), ("latency", 3.0), ("timing out", 2.5), ("timeout", 2.0),
        ("degraded", 2.5), ("performance", 2.0), ("taking minutes", 3.0), ("sluggish", 2.5),
    ],
    Category.HARDWARE_FAILURE: [
        ("disk fail", 4.0), ("hard drive", 3.0), ("power supply", 3.5), ("ups ", 2.5),
        ("overheat", 3.0), ("fan ", 2.0), ("laptop won't", 3.0), ("printer", 2.5),
        ("hardware", 2.0), ("raid", 3.0),
    ],
    Category.THIRD_PARTY_SERVICE: [
        ("vendor", 3.0), ("third party", 3.5), ("third-party", 3.5), ("supplier", 2.5),
        ("api provider", 3.0), ("upstream", 2.5), ("their side", 2.5), ("sla", 1.5),
    ],
    Category.ACCESS_REQUEST: [
        ("password reset", 4.0), ("access request", 4.0), ("new user", 3.0),
        ("permission", 2.5), ("locked out", 3.0), ("licence", 2.5), ("license", 2.5),
        ("onboard", 2.5), ("cannot log in", 2.0), ("account", 1.5),
    ],
    Category.USER_SUPPORT: [
        ("how do i", 4.0), ("how to", 3.0), ("training", 3.0), ("please assist", 1.0),
        ("signature", 2.0), ("question", 2.0), ("guide me", 3.0), ("outlook", 1.5),
    ],
}

# Scope and impact signals, weighted towards a priority band.
_P1_SIGNALS = [
    "all users", "everyone", "countrywide", "entire", "company-wide", "companywide",
    "production down", "cannot process", "no workaround", "data loss", "breach",
    "ransomware", "complete outage", "all branches", "revenue",
]
_P2_SIGNALS = [
    "whole team", "department", "branch", "multiple users", "several users",
    "severely", "major", "site is", "unable to work", "backlog",
]
_P3_SIGNALS = ["a few users", "workaround", "intermittent", "one system", "non-critical"]
_P4_SIGNALS = [
    "single user", "cosmetic", "how do i", "when convenient", "no rush",
    "request", "question", "training", "signature",
]

_NEXT_ACTIONS: dict[Category, str] = {
    Category.SECURITY_INCIDENT: (
        "Escalate to the security team immediately, isolate the affected accounts or "
        "hosts, and preserve logs for investigation."
    ),
    Category.INFRASTRUCTURE_OUTAGE: (
        "Page the platform on-call engineer to confirm component health and begin "
        "restoration of the affected service."
    ),
    Category.APPLICATION_ERROR: (
        "Assign to the owning application team with the full error output and the "
        "steps needed to reproduce the failure."
    ),
    Category.NETWORK_CONNECTIVITY: (
        "Route to the network team to verify link, DNS and firewall status for the "
        "affected path."
    ),
    Category.DATA_INTEGRITY: (
        "Halt downstream processing on the affected dataset and assign to the data "
        "team to establish the scope of the discrepancy."
    ),
    Category.PERFORMANCE_DEGRADATION: (
        "Check current resource utilisation and recent deployments for the affected "
        "service, then assign to the owning team."
    ),
    Category.HARDWARE_FAILURE: (
        "Log a hardware support call with the vendor and arrange a replacement or "
        "swap-out for the affected equipment."
    ),
    Category.THIRD_PARTY_SERVICE: (
        "Open a ticket with the third-party provider, quoting the SLA, and confirm "
        "whether the fault is acknowledged on their side."
    ),
    Category.ACCESS_REQUEST: (
        "Route to the service desk access queue for approval and provisioning under "
        "the standard access request process."
    ),
    Category.USER_SUPPORT: (
        "Assign to the service desk for a direct response to the user during normal "
        "business hours."
    ),
    Category.UNKNOWN: (
        "Contact the reporter to obtain the affected system, the specific symptom "
        "and the number of users affected before assigning a queue."
    ),
}

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?\n])\s+")

# Categories that describe a request rather than a fault.
_REQUEST_CATEGORIES = {Category.ACCESS_REQUEST, Category.USER_SUPPORT}


def _phrase_matcher(phrases: list[str]) -> re.Pattern[str]:
    """Compile phrases into a word-boundary matcher.

    Plain substring matching caused a real misclassification: "request" in the
    low-priority list matched "a third of requests are failing", downgrading a
    P2 outage to P4 and auto-triaging it. Word boundaries cost nothing and
    remove the entire class of accidental-substring bug.
    """
    alternatives = "|".join(re.escape(phrase.strip()) for phrase in phrases)
    return re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)")


# Priority signals are matched on word boundaries. Category signals stay as
# substrings on purpose - entries like "exfiltrat" and "reconcil" are stems
# meant to catch every inflection.
_P1_MATCHER = _phrase_matcher(_P1_SIGNALS)
_P2_MATCHER = _phrase_matcher(_P2_SIGNALS)
_P3_MATCHER = _phrase_matcher(_P3_SIGNALS)
_P4_MATCHER = _phrase_matcher(_P4_SIGNALS)
_ANY_SCOPE_MATCHER = _phrase_matcher(_P1_SIGNALS + _P2_SIGNALS + _P3_SIGNALS + _P4_SIGNALS)


class MockProvider(Provider):
    """Rule-based stand-in for Gemini."""

    name = "mock"

    def __init__(self, settings: Settings = SETTINGS, failure: Exception | None = None) -> None:
        self.settings = settings
        # Lets tests drive the retry and error paths without patching the SDK.
        self._failure = failure

    def generate(
        self,
        system_instruction: str,
        prompt: str,
        response_schema: dict[str, Any],
    ) -> ProviderResponse:
        if self._failure is not None:
            raise self._failure

        incident = self._extract_incident(prompt)
        if not incident:
            raise TransientProviderError("Mock provider could not locate the incident block.")

        payload = self._classify(incident)
        return ProviderResponse(
            payload=payload,
            model="mock-rules-v1",
            # Rough parity with real token counts, so cost and latency panels
            # have plausible numbers to render in the offline demo.
            input_tokens=len(prompt) // 4,
            output_tokens=120,
            finish_reason="STOP",
            raw_text="",
        )

    # ------------------------------------------------------------------ #

    @staticmethod
    def _extract_incident(prompt: str) -> str:
        match = re.search(r"<incident_report>\n(.*?)\n</incident_report>", prompt, re.DOTALL)
        return match.group(1).strip() if match else ""

    def _classify(self, text: str) -> dict[str, Any]:
        lowered = text.lower()

        scores: dict[Category, float] = {}
        hits: dict[Category, list[str]] = {}
        for category, signals in _CATEGORY_SIGNALS.items():
            score = 0.0
            matched: list[str] = []
            for term, weight in signals:
                if term in lowered:
                    score += weight
                    matched.append(term)
            if score:
                scores[category] = score
                hits[category] = matched

        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)

        if not ranked or ranked[0][1] < 2.0:
            return self._abstain(text)

        category, top_score = ranked[0]
        runner_up = ranked[1][1] if len(ranked) > 1 else 0.0

        # Confidence rises with the strength of the winning signal and with how
        # far clear of the runner-up it is. Two categories scoring alike is
        # genuine ambiguity and should read as such.
        margin = (top_score - runner_up) / top_score
        confidence = min(0.95, 0.45 + 0.09 * top_score * 0.5 + 0.25 * margin)

        priority, priority_confidence = self._priority(lowered, category)
        evidence = self._evidence(text, hits.get(category, []))
        missing = self._missing(lowered, text, category)

        if missing:
            # Unknowns should visibly cost confidence rather than being noted
            # in a field nobody reads.
            confidence -= 0.05 * len(missing)
            priority_confidence -= 0.06 * len(missing)

        return {
            "category": category.value,
            "priority": priority.value,
            "summary": self._summary(text, category),
            "next_action": _NEXT_ACTIONS[category],
            "evidence": evidence,
            "missing_information": missing,
            "category_confidence": round(max(0.05, min(0.95, confidence)), 2),
            "priority_confidence": round(max(0.05, min(0.95, priority_confidence)), 2),
            "reasoning": (
                f"Matched {category.value} on terms: {', '.join(hits.get(category, [])[:4])}. "
                f"Priority derived from stated scope and impact."
            ),
        }

    def _abstain(self, text: str) -> dict[str, Any]:
        return {
            "category": Category.UNKNOWN.value,
            "priority": Priority.UNKNOWN.value,
            "summary": (
                "The report does not identify a system, a symptom or a scope of impact, "
                "so it cannot be categorised as submitted."
            ),
            "next_action": _NEXT_ACTIONS[Category.UNKNOWN],
            "evidence": self._first_sentence(text),
            "missing_information": [
                "Which system or service is affected",
                "The specific symptom or error observed",
                "How many users or sites are affected",
            ],
            "category_confidence": 0.15,
            "priority_confidence": 0.15,
            "reasoning": "No category signal exceeded the confidence floor for this report.",
        }

    @staticmethod
    def _priority(lowered: str, category: Category) -> tuple[Priority, float]:
        if _P1_MATCHER.search(lowered):
            return Priority.P1_CRITICAL, 0.85
        if category is Category.SECURITY_INCIDENT:
            # A security report with unstated scope is escalated on purpose:
            # under-calling a compromise costs far more than over-calling it.
            return Priority.P2_HIGH, 0.60
        if _P2_MATCHER.search(lowered):
            return Priority.P2_HIGH, 0.75
        if category in _REQUEST_CATEGORIES:
            # Requests and how-do-I questions are low priority by definition.
            # Their absence of scope wording is normal, not a sign of a thin
            # report, so it should not drag confidence down.
            return Priority.P4_LOW, 0.78
        if _P4_MATCHER.search(lowered):
            return Priority.P4_LOW, 0.75
        if _P3_MATCHER.search(lowered):
            return Priority.P3_MEDIUM, 0.70
        return Priority.P3_MEDIUM, 0.50

    @staticmethod
    def _sentences(text: str) -> list[str]:
        return [s.strip() for s in _SENTENCE_SPLIT.split(text) if s.strip()]

    def _evidence(self, text: str, terms: list[str]) -> list[str]:
        """Return verbatim sentences containing the strongest matched terms.

        Verbatim matters: these spans are checked against the source by the
        grounding step, so the mock has to play by the same rule the real model
        is held to.
        """
        found: list[str] = []
        for sentence in self._sentences(text):
            lowered = sentence.lower()
            if any(term in lowered for term in terms):
                found.append(sentence[:200])
            if len(found) == 2:
                break
        return found or self._first_sentence(text)

    def _first_sentence(self, text: str) -> list[str]:
        sentences = self._sentences(text)
        return [sentences[0][:200]] if sentences else []

    def _summary(self, text: str, category: Category) -> str:
        """Lead with the first sentence that actually carries content.

        Skipping short fragments matters: reports frequently open with
        "URGENT!!!" or "Hi team", and leading the summary with that produces a
        summary sharing no vocabulary with the report, which then trips the
        drift check downstream for no real reason.
        """
        sentences = self._sentences(text)
        lead = next((s for s in sentences if len(s) >= 30), sentences[0] if sentences else text[:150])
        label = category.value.replace("_", " ").lower()
        return f"Reported {label}: {lead}"[:390]

    @staticmethod
    def _missing(lowered: str, text: str, category: Category) -> list[str]:
        missing: list[str] = []
        # Scope and start time are triage-critical for a fault and meaningless
        # for a request. Asking for them anyway produced a confidence penalty
        # on precisely the routine tickets that should flow through untouched.
        if category in _REQUEST_CATEGORIES:
            return missing
        if not _ANY_SCOPE_MATCHER.search(lowered) and not re.search(
            r"\d+\s*(users|staff|people)", lowered
        ):
            missing.append("Number of users or sites affected")
        if not re.search(r"\b(?:\d{1,2}[:h]\d{2}|yesterday|today|this morning|since|ago)\b", lowered):
            missing.append("When the issue started")
        # A named system usually shows up as a hostname, an acronym or a
        # CamelCase product name; none of those present means we cannot route.
        if not re.search(r"\b(?:[a-z0-9\-]+\d[a-z0-9\-]*|[A-Z]{2,}|[A-Z][a-z]+[A-Z])\b", text):
            missing.append("The specific system or application affected")
        return missing[:3]

    def health(self) -> dict[str, Any]:
        return {"provider": self.name, "ready": True, "model": "mock-rules-v1"}
