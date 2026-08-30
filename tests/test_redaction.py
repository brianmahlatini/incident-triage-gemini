"""Redaction: what must be removed, and what must survive."""

from __future__ import annotations

from triage.redaction import redact, rehydrate


def test_email_is_redacted():
    result = redact("Contact thabo.mokoena@example.co.za about the outage.")
    assert "thabo.mokoena@example.co.za" not in result.text
    assert "[EMAIL_1]" in result.text
    assert result.counts["EMAIL"] == 1


def test_south_african_id_number_is_redacted():
    # Valid SA ID (passes the Luhn check and has a plausible date).
    result = redact("Client ID 8001015009087 reports a problem.")
    assert "8001015009087" not in result.text
    assert result.counts.get("SA_ID") == 1


def test_thirteen_digit_number_that_is_not_an_id_is_left_alone():
    """The checksum gate is what keeps reference numbers out of the redactor."""
    result = redact("Correlation reference 1234567890123 appears in the log.")
    assert "1234567890123" in result.text


def test_card_number_is_redacted_only_when_luhn_valid():
    valid = redact("Card 4539148803436467 was listed on the statement.")
    assert "4539148803436467" not in valid.text

    # Luhn-invalid: a 16-digit reference that must survive untouched.
    invalid = redact("Order number 1111222233334445 failed to process.")
    assert "1111222233334445" in invalid.text


def test_redaction_does_not_consume_surrounding_whitespace():
    """A placeholder must not run into the next word."""
    result = redact("Card 4539148803436467 was declined by the gateway.")
    assert "] was declined" in result.text


def test_phone_number_is_redacted():
    result = redact("Reporter can be reached on 082 555 1234 all day.")
    assert "082 555 1234" not in result.text


def test_secrets_and_credentials_are_redacted():
    result = redact(
        "Job failed. config had password=Sup3rSecret! and key AKIAIOSFODNN7EXAMPLE."
    )
    assert "Sup3rSecret" not in result.text
    assert "AKIAIOSFODNN7EXAMPLE" not in result.text


def test_private_key_block_is_redacted():
    text = (
        "Deploy failed with this in the log:\n"
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----"
    )
    result = redact(text)
    assert "MIIEowIBAAKCAQEA" not in result.text
    assert result.counts.get("PRIVATE_KEY") == 1


def test_operational_identifiers_are_preserved():
    """The deliberate non-redaction.

    Hostnames, IPs, error codes and timestamps are the signal the model needs.
    Stripping them would buy negligible privacy and cost real accuracy.
    """
    text = "Host prod-db-01 at 10.24.5.19 returned HTTP 503 at 06:14 on port 5432."
    result = redact(text)
    for token in ["prod-db-01", "10.24.5.19", "503", "06:14", "5432"]:
        assert token in result.text


def test_repeated_value_reuses_the_same_placeholder():
    """The model must still be able to tell it is the same person twice."""
    result = redact("Mail sarah@example.com. If no reply, mail sarah@example.com again.")
    assert result.text.count("[EMAIL_1]") == 2
    assert result.counts["EMAIL"] == 1


def test_clean_text_is_unchanged():
    text = "The claims portal is slow for the underwriting team since 14:00."
    result = redact(text)
    assert result.text == text
    assert result.total == 0


def test_rehydrate_restores_the_original():
    original = "Call 082 555 1234 or mail ops@example.com for detail on the outage."
    result = redact(original)
    assert rehydrate(result.text, result.token_map) == original
