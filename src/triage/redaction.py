"""Remove sensitive data before anything leaves the trust boundary.

Design position: redaction happens *before* the model call, not after. Once a
payload has been sent to a third-party endpoint, scrubbing the response is
theatre - the sensitive value has already left. So the pipeline redacts, sends
the redacted text, and logs only the redacted text.

What is deliberately **not** redacted matters as much as what is. IP addresses,
hostnames, service names, error codes and timestamps stay intact: they are the
operational signal the model needs to categorise correctly. Redacting them
would trade a real accuracy loss for very little privacy gain. What goes is
personal and credential data - identity numbers, contact details, payment card
numbers, secrets - which never help decide whether a database is down.

Placeholders are typed and numbered (``[EMAIL_1]``), so the model can still
tell that the same person appears twice in a report, and a reviewer can map a
placeholder back to the original value through the retained token map.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Pattern


@dataclass
class RedactionResult:
    """Redacted text plus everything needed to audit or reverse the change."""

    text: str
    counts: dict[str, int] = field(default_factory=dict)
    # placeholder -> original value. Held in memory for the request only, and
    # never serialised into an API response or a log line.
    token_map: dict[str, str] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def _luhn_ok(digits: str) -> bool:
    """Luhn checksum, used to keep card detection from eating ticket numbers.

    A bare 16-digit-number regex flags order references, serial numbers and
    correlation ids. Requiring a valid checksum removes almost all of those
    false positives at negligible cost.
    """
    total = 0
    parity = len(digits) % 2
    for index, char in enumerate(digits):
        digit = int(char)
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _sa_id_ok(digits: str) -> bool:
    """South African ID numbers are 13 digits with a Luhn-style check digit."""
    if len(digits) != 13:
        return False
    month = int(digits[2:4])
    day = int(digits[4:6])
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return False
    return _luhn_ok(digits)


# Ordered most-specific first: a South African ID number would otherwise be
# partially consumed by the phone-number pattern.
_RULES: list[tuple[str, Pattern[str]]] = [
    (
        "PRIVATE_KEY",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    (
        "SECRET",
        re.compile(
            r"\b(?:AKIA[0-9A-Z]{16}"  # AWS access key id
            r"|AIza[0-9A-Za-z_\-]{35}"  # Google API key
            r"|gh[pousr]_[0-9A-Za-z]{20,}"  # GitHub token
            r"|sk-[0-9A-Za-z]{20,})\b"  # generic secret key
        ),
    ),
    (
        "CREDENTIAL",
        re.compile(
            r"(?i)\b(?:password|passwd|pwd|secret|api[_\- ]?key|token|bearer)"
            r"\s*[:=]\s*(?:\"[^\"]+\"|'[^']+'|\S+)"
        ),
    ),
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    ("SA_ID", re.compile(r"\b\d{13}\b")),
    # Separators are allowed *between* digits only. An earlier form permitted a
    # trailing separator, which meant the match consumed the space after the
    # number and the placeholder ran into the following word.
    ("CARD", re.compile(r"\b\d(?:[ \-]?\d){12,18}\b")),
    (
        "PHONE",
        re.compile(r"(?:\+\d{1,3}[\s\-]?)?(?:0\d{2}|\(\d{2,3}\))[\s\-]?\d{3}[\s\-]?\d{4}\b"),
    ),
    ("ID_NUMBER_LABELLED", re.compile(r"(?i)\b(?:id|passport)\s*(?:no\.?|number)\s*[:=]?\s*\w{6,}")),
]


def redact(text: str) -> RedactionResult:
    """Return ``text`` with sensitive values replaced by typed placeholders."""
    counts: dict[str, int] = {}
    token_map: dict[str, str] = {}
    seen: dict[str, str] = {}  # original value -> placeholder, for stable reuse

    def substitute(match: re.Match[str], label: str) -> str:
        original = match.group(0)

        # Checksum gates: only apply where a bare pattern would over-match.
        digits = re.sub(r"\D", "", original)
        if label == "SA_ID" and not _sa_id_ok(digits):
            return original
        if label == "CARD" and not (13 <= len(digits) <= 19 and _luhn_ok(digits)):
            return original

        if original in seen:
            return seen[original]

        counts[label] = counts.get(label, 0) + 1
        placeholder = f"[{label}_{counts[label]}]"
        seen[original] = placeholder
        token_map[placeholder] = original
        return placeholder

    redacted = text
    for label, pattern in _RULES:
        redacted = pattern.sub(lambda m, lbl=label: substitute(m, lbl), redacted)

    return RedactionResult(text=redacted, counts=counts, token_map=token_map)


def rehydrate(text: str, token_map: dict[str, str]) -> str:
    """Restore original values. For authorised human review only.

    Kept separate from the response path on purpose: a reviewer with the right
    permission can see the real contact details, while the default API response
    and every log line stay redacted.
    """
    for placeholder, original in token_map.items():
        text = text.replace(placeholder, original)
    return text
