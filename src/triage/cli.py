"""Command-line entry point.

Useful on its own, and useful as proof that the workflow does not depend on the
web layer: the same pipeline object serves HTTP, the CLI, the batch runner and
the evaluation harness.

    python -m triage --samples
    python -m triage --text "The claims portal is down for all users"
    echo "..." | python -m triage
"""

from __future__ import annotations

import argparse
import json
import sys

from .pipeline import TriagePipeline
from .samples import SAMPLE_INCIDENTS
from .schema import TriageResult

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_COLOURS = {
    "P1_CRITICAL": "\033[91m",
    "P2_HIGH": "\033[93m",
    "P3_MEDIUM": "\033[94m",
    "P4_LOW": "\033[92m",
    "UNKNOWN": "\033[95m",
}


def render(result: TriageResult, colour: bool = True) -> str:
    """Human-readable rendering of one result."""

    def paint(text: str, code: str) -> str:
        return f"{code}{text}{_RESET}" if colour else text

    priority = result.priority.value
    lines = [
        "",
        paint(f"  {result.incident_id}  [{result.status.value}]", _BOLD),
        f"  Category    : {result.category.value}",
        f"  Priority    : {paint(priority, _COLOURS.get(priority, ''))}",
        f"  Confidence  : {result.overall_confidence:.2f} "
        f"(category {result.category_confidence:.2f} / priority {result.priority_confidence:.2f})",
        f"  Summary     : {result.summary or '-'}",
        f"  Next action : {result.next_action or '-'}",
    ]

    if result.evidence:
        lines.append("  Evidence    :")
        lines.extend(f"      - \"{span}\"" for span in result.evidence)
        lines.append(
            f"  Grounding   : {result.grounding.grounded}/{result.grounding.checked} "
            f"spans found in the source text"
        )
    if result.missing_information:
        lines.append("  Missing     :")
        lines.extend(f"      - {item}" for item in result.missing_information)
    if result.meta.redactions:
        lines.append(f"  Redacted    : {result.meta.redactions}")

    verdict = "HUMAN REVIEW" if result.routing.requires_human_review else "AUTO-TRIAGED"
    code = "\033[93m" if result.routing.requires_human_review else "\033[92m"
    lines.append(f"  Routing     : {paint(verdict, code)}")
    lines.append(f"                {result.routing.explanation}")
    if result.error:
        lines.append(f"  Error       : {result.error}")
    lines.append(
        paint(
            f"  {result.meta.provider}/{result.meta.model} - {result.meta.latency_ms}ms, "
            f"{result.meta.attempts} attempt(s), correlation {result.meta.correlation_id}",
            _DIM,
        )
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Triage an operational incident.")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--text", help="Incident text to triage.")
    source.add_argument("--file", help="Read the incident from a file.")
    source.add_argument(
        "--samples", action="store_true", help="Run the built-in sample incidents."
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a report.")
    parser.add_argument("--no-colour", action="store_true", help="Disable ANSI colour.")
    args = parser.parse_args(argv)

    pipeline = TriagePipeline()

    if args.samples:
        results = []
        for sample in SAMPLE_INCIDENTS:
            result = pipeline.run(sample["text"], incident_id=f"SAMPLE-{sample['id']}")
            results.append(result)
            if not args.json:
                print(f"\n{_BOLD}=== {sample['label']} ==={_RESET}")
                print(f"{_DIM}{sample['demonstrates']}{_RESET}")
                print(render(result, colour=not args.no_colour))
        if args.json:
            print(json.dumps([r.model_dump(mode="json") for r in results], indent=2))
        return 0

    if args.text:
        text = args.text
    elif args.file:
        with open(args.file, encoding="utf-8") as handle:
            text = handle.read()
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
    else:
        parser.error("Provide --text, --file, --samples, or pipe text on stdin.")
        return 2

    result = pipeline.run(text)
    if args.json:
        print(json.dumps(result.model_dump(mode="json"), indent=2))
    else:
        print(render(result, colour=not args.no_colour))
    # Non-zero exit when a human is needed, so the CLI composes with shell
    # pipelines and CI checks.
    return 0 if not result.routing.requires_human_review else 1


if __name__ == "__main__":
    raise SystemExit(main())
