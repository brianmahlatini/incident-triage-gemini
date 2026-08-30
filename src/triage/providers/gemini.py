"""Live Gemini provider, via the ``google-genai`` SDK.

The same SDK reaches both backends, which is why this file has no Vertex-vs-
Developer-API branching beyond client construction:

* **Gemini Developer API** - an API key. Quickest way to try the workflow.
* **Vertex AI** - project and location, authenticated with Application Default
  Credentials. This is what production would use: IAM instead of a shared key,
  VPC Service Controls, CMEK, regional data residency, and billing that lands
  in the same project as everything else.

Structured output is enforced with ``response_schema``, so the model decodes
under a grammar constraint rather than being asked politely for JSON. Prompt
-only JSON requests fail a few times in every thousand calls - a stray prefix,
a trailing comma, a fenced code block - and at thousands of incidents a day
that is a steady stream of parse errors for no benefit.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from ..config import SETTINGS, Settings
from .base import (
    ContentBlockedError,
    PermanentProviderError,
    Provider,
    ProviderResponse,
    TransientProviderError,
)

# Status codes that mean "the same request may succeed shortly".
_TRANSIENT_CODES = {408, 429, 500, 502, 503, 504}
_TRANSIENT_MARKERS = (
    "deadline",
    "timeout",
    "timed out",
    "unavailable",
    "resource exhausted",
    "resource_exhausted",
    "internal error",
    "connection reset",
    "temporarily",
)
_PERMANENT_MARKERS = (
    "api key not valid",
    "permission denied",
    "unauthenticated",
    "invalid argument",
    "not found",
    "billing",
)


def _is_invalid_argument(exc: Exception) -> bool:
    """Whether an SDK error is a 400 rejecting one of our config fields."""
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    message = str(exc).lower()
    return code == 400 or "invalid_argument" in message or "invalid argument" in message


class GeminiProvider(Provider):
    """Calls Gemini and returns a parsed, schema-constrained object."""

    name = "gemini"

    def __init__(self, settings: Settings = SETTINGS) -> None:
        self.settings = settings
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:  # pragma: no cover - depends on install
            raise PermanentProviderError(
                "google-genai is not installed. Run 'pip install google-genai', "
                "or set TRIAGE_PROVIDER=mock to run the workflow offline."
            ) from exc

        self._types = types
        self._client = self._build_client(genai)
        # Cleared permanently if the model rejects a thinking budget.
        self._thinking_enabled = settings.thinking_budget is not None

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #

    def _build_client(self, genai: Any) -> Any:
        use_vertex = os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "").lower() in {"1", "true", "yes"}
        if use_vertex:
            project = os.getenv("GOOGLE_CLOUD_PROJECT")
            location = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
            if not project:
                raise PermanentProviderError(
                    "GOOGLE_CLOUD_PROJECT must be set when GOOGLE_GENAI_USE_VERTEXAI is true."
                )
            return genai.Client(vertexai=True, project=project, location=location)

        api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise PermanentProviderError(
                "No credentials found. Set GOOGLE_API_KEY for the Gemini Developer API, "
                "or GOOGLE_GENAI_USE_VERTEXAI=true with GOOGLE_CLOUD_PROJECT for Vertex AI."
            )
        return genai.Client(api_key=api_key)

    def _safety_settings(self) -> list[Any]:
        """Loosen the default filters to BLOCK_ONLY_HIGH.

        Genuine operational reports discuss attacks, exploits, malware, threats
        and occasionally physical injury. At default thresholds a real security
        incident is among the likeliest things to be blocked - precisely the
        report that must not be dropped. Blocks are still surfaced as errors
        and routed to a human; this only stops routine ops language from
        tripping the filter.
        """
        types = self._types
        categories = [
            "HARM_CATEGORY_HARASSMENT",
            "HARM_CATEGORY_HATE_SPEECH",
            "HARM_CATEGORY_SEXUALLY_EXPLICIT",
            "HARM_CATEGORY_DANGEROUS_CONTENT",
        ]
        return [
            types.SafetySetting(category=category, threshold="BLOCK_ONLY_HIGH")
            for category in categories
        ]

    # ------------------------------------------------------------------ #
    # Generation
    # ------------------------------------------------------------------ #

    def generate(
        self,
        system_instruction: str,
        prompt: str,
        response_schema: dict[str, Any],
    ) -> ProviderResponse:
        try:
            response = self._client.models.generate_content(
                model=self.settings.model,
                contents=prompt,
                config=self._config(system_instruction, response_schema),
            )
        except Exception as exc:  # noqa: BLE001 - SDK raises a wide range
            # Not every model accepts a thinking budget - gemini-3.6-flash
            # returns 400 for it. Rather than maintain a hardcoded list of
            # which models support what (a list that goes stale the week a new
            # model ships), retry once without the setting and remember not to
            # send it again for the life of this client.
            if self._thinking_enabled and _is_invalid_argument(exc):
                self._thinking_enabled = False
                try:
                    response = self._client.models.generate_content(
                        model=self.settings.model,
                        contents=prompt,
                        config=self._config(system_instruction, response_schema),
                    )
                except Exception as retry_exc:  # noqa: BLE001
                    raise self._classify(retry_exc) from retry_exc
            else:
                raise self._classify(exc) from exc

        return self._parse(response)

    def _config(self, system_instruction: str, response_schema: dict[str, Any]) -> Any:
        types = self._types
        kwargs: dict[str, Any] = dict(
            system_instruction=system_instruction,
            temperature=self.settings.temperature,
            # Classification wants the most probable token, not a creative one.
            # top_p is pinned alongside temperature so decoding is as close to
            # reproducible as the API allows - which is what makes an
            # evaluation run comparable to the previous one.
            top_p=0.95,
            max_output_tokens=self.settings.max_output_tokens,
            response_mime_type="application/json",
            response_schema=response_schema,
            safety_settings=self._safety_settings(),
        )
        if self._thinking_enabled and self.settings.thinking_budget is not None:
            kwargs["thinking_config"] = types.ThinkingConfig(
                thinking_budget=self.settings.thinking_budget
            )
        return types.GenerateContentConfig(**kwargs)

    # ------------------------------------------------------------------ #
    # Response handling
    # ------------------------------------------------------------------ #

    def _parse(self, response: Any) -> ProviderResponse:
        warnings: list[str] = []

        feedback = getattr(response, "prompt_feedback", None)
        if feedback is not None and getattr(feedback, "block_reason", None):
            raise ContentBlockedError(
                f"Prompt blocked by safety filter: {feedback.block_reason}"
            )

        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            raise TransientProviderError("Model returned no candidates.")

        finish_reason = str(getattr(candidates[0], "finish_reason", "") or "")
        if "SAFETY" in finish_reason.upper():
            raise ContentBlockedError(f"Response blocked by safety filter: {finish_reason}")
        if "MAX_TOKENS" in finish_reason.upper():
            # Truncated JSON will not parse, and retrying the identical request
            # produces the identical truncation, so this fails hard and routes
            # to a human. The thinking-token count is included because that is
            # almost always the cause: reasoning consumes the same budget as
            # the answer, and the fix is configuration, not a retry.
            usage = getattr(response, "usage_metadata", None)
            thoughts = getattr(usage, "thoughts_token_count", None)
            detail = f" ({thoughts} tokens spent on reasoning)" if thoughts else ""
            raise PermanentProviderError(
                f"Model response hit the {self.settings.max_output_tokens}-token output "
                f"limit and is incomplete{detail}. Raise TRIAGE_MAX_OUTPUT_TOKENS or "
                "lower TRIAGE_THINKING_BUDGET."
            )

        text = (getattr(response, "text", None) or "").strip()
        if not text:
            raise TransientProviderError("Model returned an empty response body.")

        payload = self._loads(text)

        usage = getattr(response, "usage_metadata", None)
        return ProviderResponse(
            payload=payload,
            model=self.settings.model,
            input_tokens=getattr(usage, "prompt_token_count", None),
            output_tokens=getattr(usage, "candidates_token_count", None),
            finish_reason=finish_reason or None,
            raw_text=text,
            warnings=warnings,
        )

    @staticmethod
    def _loads(text: str) -> dict[str, Any]:
        """Parse JSON, tolerating a fenced block.

        Schema-constrained decoding should make the fence impossible. The
        fallback costs three lines and removes a whole class of 2am incident,
        so it stays.
        """
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            cleaned = text.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("```")[1]
                if cleaned.lstrip().lower().startswith("json"):
                    cleaned = cleaned.lstrip()[4:]
            try:
                payload = json.loads(cleaned)
            except json.JSONDecodeError as exc:
                raise TransientProviderError(
                    f"Model returned text that is not valid JSON: {text[:200]}"
                ) from exc

        if not isinstance(payload, dict):
            raise TransientProviderError("Model returned JSON that is not an object.")
        return payload

    @staticmethod
    def _retry_after(exc: Exception) -> float | None:
        """Extract the server's retry hint from a rate-limit error.

        Gemini returns it two ways depending on the transport - a structured
        ``RetryInfo`` detail and a "Please retry in 32.06s" sentence in the
        message. Both are parsed from the string form, since the SDK does not
        surface the detail objects consistently across versions.
        """
        message = str(exc)
        match = re.search(r"retry in (\d+(?:\.\d+)?)s", message, re.IGNORECASE)
        if match:
            return float(match.group(1))
        match = re.search(r"'retryDelay':\s*'(\d+(?:\.\d+)?)s'", message)
        if match:
            return float(match.group(1))
        return None

    @classmethod
    def _classify(cls, exc: Exception) -> Exception:
        """Map an SDK exception onto the retry taxonomy.

        Matching on both a status code attribute and message text because the
        SDK surfaces errors inconsistently across transports; an unrecognised
        error is treated as transient so a single retry gets a second chance
        rather than failing the incident outright.
        """
        retry_after = cls._retry_after(exc)
        code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        if isinstance(code, int):
            if code in _TRANSIENT_CODES:
                return TransientProviderError(
                    f"Gemini transient error {code}: {exc}", retry_after
                )
            if 400 <= code < 500:
                return PermanentProviderError(f"Gemini permanent error {code}: {exc}")

        message = str(exc).lower()
        if any(marker in message for marker in _PERMANENT_MARKERS):
            return PermanentProviderError(f"Gemini permanent error: {exc}")
        if any(marker in message for marker in _TRANSIENT_MARKERS):
            return TransientProviderError(f"Gemini transient error: {exc}", retry_after)
        if any(str(c) in message for c in _TRANSIENT_CODES):
            return TransientProviderError(f"Gemini transient error: {exc}", retry_after)
        return TransientProviderError(f"Unclassified Gemini error: {exc}", retry_after)

    def health(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "ready": self._client is not None,
            "model": self.settings.model,
            "backend": "vertex-ai"
            if os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "").lower() in {"1", "true", "yes"}
            else "developer-api",
        }
