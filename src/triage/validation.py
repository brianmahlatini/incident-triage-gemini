"""Input validation and sanitisation - the gate in front of the model call.

Two separable jobs live here, and the distinction drives the whole design:

* **Rejections** are inputs that cannot produce a meaningful triage - empty,
  too short, not text. These never reach Gemini. Cheap to detect, and every
  one avoided is a wasted API call and a meaningless result not created.
* **Warnings** are inputs that are usable but degraded - thin detail,
  instruction-like content, heavy truncation. These proceed, but they are
  carried forward so the routing layer can lean towards human review. Silently
  accepting a poor input and returning a confident answer is the failure this
  prevents.

Oversized input is truncated rather than rejected. An operator pasting a
50,000-line log dump still has a real incident; refusing it helps nobody, and
the first characters carry most of the triage signal.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from .config import SETTINGS, Settings

# Delimiter used to fence the incident inside the prompt. Any occurrence in
# user input is neutralised so the text cannot close its own fence and have the
# remainder read as instructions.
INCIDENT_OPEN = "<incident_report>"
INCIDENT_CLOSE = "</incident_report>"

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Phrasings that try to address the model rather than describe an incident.
# Detection is a signal, not a filter: the defence is the prompt's instruction
# hierarchy plus schema-constrained decoding. Flagging simply means a human
# looks at anything trying to steer the classifier.
_INJECTION_PATTERNS = [
    re.compile(r"(?i)ignore\s+(?:all\s+|any\s+)?(?:previous|prior|above)\s+instructions"),
    re.compile(r"(?i)disregard\s+(?:the\s+)?(?:above|previous|system)"),
    re.compile(r"(?i)\byou\s+are\s+now\b"),
    re.compile(r"(?i)\bsystem\s*prompt\b"),
    re.compile(r"(?i)\bnew\s+instructions?\s*:"),
    re.compile(r"(?i)(?:set|mark|classify)\s+(?:this\s+)?(?:as\s+)?(?:priority\s*)?p1\b"),
    re.compile(r"(?i)\brespond\s+only\s+with\b"),
    re.compile(r"(?i)</?(?:incident_report|system|instructions?)>"),
]


@dataclass
class ValidationOutcome:
    """Result of validating one incident submission."""

    ok: bool
    text: str = ""
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    truncated: bool = False
    suspected_injection: bool = False

    @property
    def low_quality(self) -> bool:
        """Whether the routing layer should treat this input as degraded."""
        return bool(self.warnings)


def sanitise(text: str) -> str:
    """Normalise text without changing its meaning."""
    # NFKC folds look-alike unicode into canonical forms, which stops homoglyph
    # tricks from slipping past the keyword checks downstream.
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_CHARS.sub(" ", text)
    # Neutralise the prompt fence rather than deleting it, so a reviewer can
    # still see what the submitter actually wrote.
    text = text.replace(INCIDENT_OPEN, "(incident_report)")
    text = text.replace(INCIDENT_CLOSE, "(/incident_report)")
    # Collapse runaway blank lines; keeps token cost down on pasted log dumps.
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


def _alpha_ratio(text: str) -> float:
    if not text:
        return 0.0
    return sum(char.isalpha() or char.isspace() for char in text) / len(text)


def validate_incident(raw: object, settings: Settings = SETTINGS) -> ValidationOutcome:
    """Validate and sanitise a raw incident submission."""
    errors: list[str] = []
    warnings: list[str] = []

    if raw is None:
        return ValidationOutcome(ok=False, errors=["Incident text is required."])
    if not isinstance(raw, str):
        return ValidationOutcome(
            ok=False, errors=[f"Incident text must be a string, got {type(raw).__name__}."]
        )

    text = sanitise(raw)

    if not text:
        return ValidationOutcome(ok=False, errors=["Incident text is empty."])
    if len(text) < settings.min_input_chars:
        return ValidationOutcome(
            ok=False,
            errors=[
                f"Incident text is too short to triage "
                f"({len(text)} characters, minimum {settings.min_input_chars})."
            ],
        )

    truncated = False
    if len(text) > settings.max_input_chars:
        text = text[: settings.max_input_chars]
        truncated = True
        warnings.append(
            f"Input truncated to {settings.max_input_chars} characters; "
            "later detail was not seen by the model."
        )

    # A report that is mostly punctuation, hex or base64 is probably a pasted
    # artefact rather than a description; the model will hallucinate structure
    # from it if left unflagged.
    if _alpha_ratio(text) < 0.55:
        warnings.append("Input is largely non-prose; triage confidence is likely to be poor.")

    if len(text) < 40:
        warnings.append("Very short report; limited detail available for triage.")

    words = re.findall(r"\w+", text.lower())
    if words and len(set(words)) / len(words) < 0.25 and len(words) > 20:
        warnings.append("Input is highly repetitive; it may be machine-generated noise.")

    suspected_injection = any(pattern.search(text) for pattern in _INJECTION_PATTERNS)
    if suspected_injection:
        warnings.append(
            "Input contains instruction-like content; flagged for human review."
        )

    return ValidationOutcome(
        ok=not errors,
        text=text,
        errors=errors,
        warnings=warnings,
        truncated=truncated,
        suspected_injection=suspected_injection,
    )
