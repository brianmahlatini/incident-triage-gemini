"""Runtime configuration, read once from the environment.

Configuration is loaded into a frozen dataclass rather than read via
``os.getenv`` at call sites, so that every tunable is discoverable in one
place, is type-checked on startup, and can be substituted wholesale in tests.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

try:  # pragma: no cover - convenience only
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        # Bad config should be loud but not fatal at import time; the warning
        # surfaces in the startup log and the safe default keeps the app up.
        return default


def _env_int(name: str, default: int) -> int:
    return int(_env_float(name, float(default)))


@dataclass(frozen=True)
class Settings:
    """All knobs for the workflow."""

    provider: str = "mock"
    model: str = "gemini-3.5-flash"
    temperature: float = 0.1
    max_retries: int = 3
    timeout_seconds: int = 30

    # Output budget. This must accommodate *thinking* tokens as well as the
    # answer: Gemini 3.x models reason before responding by default, and those
    # tokens are charged against this limit. At 1024 a single triage record
    # came back truncated mid-string, having spent 980 tokens thinking and 29
    # on the answer - valid JSON is impossible at that point, and constrained
    # decoding does not save you.
    max_output_tokens: int = 2048

    # Thinking budget, when the model supports setting one. None leaves the
    # model's default in place. 0 disables reasoning, which roughly quarters
    # latency on a classification task like this - but not every model accepts
    # it (gemini-3.6-flash returns 400), so it is opt-in and failures fall back.
    thinking_budget: int | None = None

    # Routing thresholds.
    confidence_threshold: float = 0.70
    grounding_threshold: float = 0.60

    # Input validation bounds.
    min_input_chars: int = 15
    max_input_chars: int = 20_000

    log_level: str = "INFO"

    # Indicative pricing (USD per 1M tokens) for a rough per-incident cost
    # signal in logs and dashboards. NOT verified against a current price list -
    # set these from the published rates for whichever model you deploy before
    # using the figure for budgeting. Note that thinking tokens bill as output.
    price_per_1m_input: float = 0.10
    price_per_1m_output: float = 0.40

    # Terms that force human review regardless of what the model concludes.
    # This is a deliberate policy backstop: the business accepts extra review
    # cost on these topics rather than any chance of an automated miss.
    escalation_keywords: tuple[str, ...] = (
        "ransomware",
        "data breach",
        "breach of data",
        "exfiltrat",
        "unauthorised access",
        "unauthorized access",
        "credential leak",
        "credentials leaked",
        "malware",
        "phishing",
        "popia",
        "gdpr",
        "regulator",
        "legal action",
        "injury",
        "safety incident",
        "life threatening",
        "fraud",
    )

    def estimate_cost(self, input_tokens: int | None, output_tokens: int | None) -> float | None:
        if input_tokens is None and output_tokens is None:
            return None
        cost = (input_tokens or 0) / 1_000_000 * self.price_per_1m_input
        cost += (output_tokens or 0) / 1_000_000 * self.price_per_1m_output
        return round(cost, 8)


def load_settings() -> Settings:
    """Build settings from the process environment."""
    return Settings(
        provider=os.getenv("TRIAGE_PROVIDER", "mock").strip().lower(),
        model=os.getenv("TRIAGE_MODEL", "gemini-3.5-flash").strip(),
        temperature=_env_float("TRIAGE_TEMPERATURE", 0.1),
        max_retries=_env_int("TRIAGE_MAX_RETRIES", 3),
        timeout_seconds=_env_int("TRIAGE_TIMEOUT_SECONDS", 30),
        max_output_tokens=_env_int("TRIAGE_MAX_OUTPUT_TOKENS", 2048),
        thinking_budget=(
            _env_int("TRIAGE_THINKING_BUDGET", 0)
            if os.getenv("TRIAGE_THINKING_BUDGET", "").strip()
            else None
        ),
        confidence_threshold=_env_float("TRIAGE_CONFIDENCE_THRESHOLD", 0.70),
        grounding_threshold=_env_float("TRIAGE_GROUNDING_THRESHOLD", 0.60),
        log_level=os.getenv("TRIAGE_LOG_LEVEL", "INFO").strip().upper(),
    )


SETTINGS = load_settings()
