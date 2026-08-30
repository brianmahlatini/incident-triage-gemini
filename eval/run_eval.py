"""Evaluation harness.

Run against the mock provider (default) or live Gemini:

    python eval/run_eval.py
    TRIAGE_PROVIDER=gemini python eval/run_eval.py --out eval/results/gemini.json

The measures are chosen around one question - *what does a bad output cost?* -
rather than around what is easy to compute:

* **Category accuracy** is reported, but it is the least interesting number
  here. A wrong category sends a ticket to the wrong queue, and a person
  notices within minutes.
* **Priority distance** matters more than priority accuracy. Calling a P2 a P3
  is a scheduling annoyance; calling a P1 a P4 is an outage nobody was paged
  for. Mean absolute rank distance and a severe-error count separate the two.
* **Critical misses** is the metric that would actually block a release: a
  true P1 or P2 that was called P3 or P4 *and* was auto-triaged with no human
  in the loop. Every other error has a safety net. This one does not.
* **Deferral quality** is measured as precision and recall, because a system
  that sends everything to a human scores perfectly on accuracy while
  delivering no value at all. Automation rate is reported alongside so the two
  are always read together.
* **Grounding** gives a fabrication rate that needs no labels, which means it
  also works as a live production signal, not just an offline one.
* **Calibration** compares confidence on right answers against confidence on
  wrong ones. If those are similar, the confidence gate is decorative and the
  whole routing design rests on nothing.

Exit code is non-zero when a release gate fails, so this runs in CI as a check
rather than as a report someone has to remember to read.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from triage.pipeline import TriagePipeline  # noqa: E402
from triage.schema import PRIORITY_RANK, Priority  # noqa: E402

DATASET = Path(__file__).parent / "dataset.jsonl"

# Release gates. Deliberately few, and all of them tied to a cost rather than
# to a round number that looks reassuring on a slide.
GATES: dict[str, tuple[str, float]] = {
    "critical_misses": ("<=", 0),  # non-negotiable
    "category_accuracy": (">=", 0.70),
    "severe_priority_errors": ("<=", 2),
    "hallucination_rate": ("<=", 0.05),
    "automation_rate": (">=", 0.15),  # below this the system is not earning its cost
}


def load_dataset(path: Path = DATASET) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _rank(priority: str) -> int | None:
    try:
        return PRIORITY_RANK.get(Priority(priority))
    except ValueError:
        return None


def evaluate(cases: list[dict[str, Any]], pipeline: TriagePipeline) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    started = time.perf_counter()

    for case in cases:
        result = pipeline.run(case["text"], incident_id=case["id"])

        predicted_category = result.category.value
        predicted_priority = result.priority.value
        expected_category = case["expected_category"]
        expected_priority = case["expected_priority"]

        expected_rank = _rank(expected_priority)
        predicted_rank = _rank(predicted_priority)
        # Distance is only defined when both sides name a real priority. An
        # abstention is scored separately, as a deferral, not as a wrong answer.
        distance = (
            abs(expected_rank - predicted_rank)
            if expected_rank is not None and predicted_rank is not None
            else None
        )

        under_called = (
            expected_rank is not None
            and predicted_rank is not None
            and expected_rank <= 2
            and predicted_rank >= 3
        )

        rows.append(
            {
                "id": case["id"],
                "tags": case.get("tags", []),
                "status": result.status.value,
                "expected_category": expected_category,
                "predicted_category": predicted_category,
                "category_correct": predicted_category == expected_category,
                "expected_priority": expected_priority,
                "predicted_priority": predicted_priority,
                "priority_correct": predicted_priority == expected_priority,
                "priority_distance": distance,
                "under_called": under_called,
                "expects_review": case["expects_review"],
                "routed_to_review": result.routing.requires_human_review,
                # The one that matters: a serious incident downgraded *and*
                # nobody asked to look at it.
                "critical_miss": under_called and not result.routing.requires_human_review,
                "confidence": result.overall_confidence,
                "grounding_ratio": result.grounding.ratio,
                "hallucinated": bool(result.grounding.ungrounded_spans),
                "review_reasons": [r.value for r in result.routing.reasons],
                "latency_ms": result.meta.latency_ms,
                "cost_usd": result.meta.estimated_cost_usd or 0.0,
                "note": case.get("note", ""),
            }
        )

    total = len(rows)
    scored = [row for row in rows if row["status"] == "OK"]
    with_distance = [row for row in rows if row["priority_distance"] is not None]

    # Deferral precision/recall against the "a human should see this" label.
    true_positive = sum(r["routed_to_review"] and r["expects_review"] for r in rows)
    false_positive = sum(r["routed_to_review"] and not r["expects_review"] for r in rows)
    false_negative = sum(not r["routed_to_review"] and r["expects_review"] for r in rows)

    correct = [r["confidence"] for r in scored if r["category_correct"]]
    wrong = [r["confidence"] for r in scored if not r["category_correct"]]

    def mean(values: list[float]) -> float:
        return round(sum(values) / len(values), 3) if values else 0.0

    metrics = {
        "cases": total,
        "schema_valid_rate": round(len(scored) / total, 3) if total else 0.0,
        "rejected": sum(r["status"] == "REJECTED" for r in rows),
        "failed": sum(r["status"] == "FAILED" for r in rows),
        # Classification accuracy is scored over rows the model actually
        # answered. A call that failed on a rate limit is an availability
        # problem, not a wrong answer, and counting it as a misclassification
        # made a quota exhaustion look like a model that could not classify.
        # The failures are not hidden - they carry their own counters above,
        # and they still count against the safety and deferral metrics, where
        # what matters is the outcome for the incident rather than the cause.
        "scored": len(scored),
        "category_accuracy": round(
            sum(r["category_correct"] for r in scored) / len(scored), 3
        )
        if scored
        else 0.0,
        "priority_accuracy": round(
            sum(r["priority_correct"] for r in scored) / len(scored), 3
        )
        if scored
        else 0.0,
        "priority_mean_distance": mean([float(r["priority_distance"]) for r in with_distance]),
        "severe_priority_errors": sum(
            1 for r in with_distance if (r["priority_distance"] or 0) >= 2
        ),
        "under_called_serious": sum(r["under_called"] for r in rows),
        "critical_misses": sum(r["critical_miss"] for r in rows),
        "review_rate": round(sum(r["routed_to_review"] for r in rows) / total, 3)
        if total
        else 0.0,
        "automation_rate": round(
            sum(not r["routed_to_review"] for r in rows) / total, 3
        )
        if total
        else 0.0,
        "deferral_precision": round(true_positive / (true_positive + false_positive), 3)
        if (true_positive + false_positive)
        else 0.0,
        "deferral_recall": round(true_positive / (true_positive + false_negative), 3)
        if (true_positive + false_negative)
        else 0.0,
        "missed_reviews": false_negative,
        "hallucination_rate": round(sum(r["hallucinated"] for r in rows) / total, 3)
        if total
        else 0.0,
        "mean_grounding_ratio": mean([r["grounding_ratio"] for r in rows]),
        "confidence_when_correct": mean(correct),
        "confidence_when_wrong": mean(wrong),
        "calibration_gap": round(mean(correct) - mean(wrong), 3),
        "mean_latency_ms": int(mean([float(r["latency_ms"]) for r in rows])),
        "total_cost_usd": round(sum(r["cost_usd"] for r in rows), 6),
        "wall_clock_s": round(time.perf_counter() - started, 2),
    }

    confusion: dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        confusion[row["expected_category"]][row["predicted_category"]] += 1

    by_tag: dict[str, dict[str, Any]] = {}
    for tag in sorted({tag for row in rows for tag in row["tags"]}):
        tagged = [row for row in rows if tag in row["tags"]]
        answered = [row for row in tagged if row["status"] == "OK"]
        by_tag[tag] = {
            "n": len(tagged),
            "scored": len(answered),
            "category_accuracy": round(
                sum(r["category_correct"] for r in answered) / len(answered), 3
            )
            if answered
            else 0.0,
            "critical_misses": sum(r["critical_miss"] for r in tagged),
        }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "provider": pipeline.provider.name,
        "model": pipeline.settings.model,
        "prompt_version": pipeline.health()["prompt_version"],
        "confidence_threshold": pipeline.settings.confidence_threshold,
        "metrics": metrics,
        "by_tag": by_tag,
        "confusion": {k: dict(v) for k, v in confusion.items()},
        "rows": rows,
    }


# Tolerances for a regression check against a recorded run. The absolute gates
# above answer "is this good enough to deploy?"; these answer a different and
# equally important question - "did this change make things worse?" The offline
# baseline is never expected to clear production gates, so gating CI on them
# would leave the build permanently red and the signal permanently ignored.
# Comparing the baseline against its own recorded numbers catches a genuine
# regression in the pipeline without pretending a keyword engine is shippable.
REGRESSION_TOLERANCES: dict[str, tuple[str, float]] = {
    "category_accuracy": (">=", 0.05),        # may not drop more than 5pp
    "priority_mean_distance": ("<=", 0.10),   # may not worsen by 0.1 bands
    "critical_misses": ("<=", 0),             # may not increase at all
    "severe_priority_errors": ("<=", 0),
    "hallucination_rate": ("<=", 0.02),
    "deferral_recall": (">=", 0.05),
}


def check_regression(metrics: dict[str, Any], baseline: dict[str, Any]) -> list[str]:
    """Compare a run against a recorded one and report material regressions."""
    failures = []
    for name, (direction, tolerance) in REGRESSION_TOLERANCES.items():
        current = metrics.get(name)
        previous = baseline.get(name)
        if current is None or previous is None:
            continue
        if direction == ">=" and current < previous - tolerance:
            failures.append(f"{name} regressed: {previous} -> {current} (tolerance {tolerance})")
        if direction == "<=" and current > previous + tolerance:
            failures.append(f"{name} regressed: {previous} -> {current} (tolerance {tolerance})")
    return failures


def check_gates(metrics: dict[str, Any]) -> list[str]:
    failures = []
    for name, (operator, bound) in GATES.items():
        value = metrics.get(name)
        if value is None:
            continue
        if operator == ">=" and value < bound:
            failures.append(f"{name} = {value} (gate: >= {bound})")
        if operator == "<=" and value > bound:
            failures.append(f"{name} = {value} (gate: <= {bound})")
    return failures


def render(report: dict[str, Any], verbose: bool = False) -> str:
    metrics = report["metrics"]
    lines = [
        "",
        "=" * 74,
        f" Incident triage evaluation - {report['provider']}/{report['model']}",
        f" prompt {report['prompt_version']}  |  threshold "
        f"{report['confidence_threshold']}  |  {metrics['cases']} cases, "
        f"{metrics['scored']} scored",
        "=" * 74,
        "",
        " OUTPUT VALIDITY",
        f"   schema valid rate          {metrics['schema_valid_rate']:.1%}"
        f"   (rejected {metrics['rejected']}, failed {metrics['failed']})",
        "",
        " CLASSIFICATION",
        f"   category accuracy          {metrics['category_accuracy']:.1%}"
        f"   (of {metrics['scored']} answered)",
        f"   priority accuracy          {metrics['priority_accuracy']:.1%}",
        f"   priority mean distance     {metrics['priority_mean_distance']}"
        "   (rank steps; lower is better)",
        f"   severe priority errors     {metrics['severe_priority_errors']}"
        "   (off by 2+ bands)",
        "",
        " SAFETY",
        f"   serious incidents downgraded  {metrics['under_called_serious']}",
        f"   CRITICAL MISSES               {metrics['critical_misses']}"
        "   (downgraded AND auto-triaged)",
        f"   hallucination rate            {metrics['hallucination_rate']:.1%}",
        f"   mean grounding ratio          {metrics['mean_grounding_ratio']}",
        "",
        " HUMAN-IN-THE-LOOP",
        f"   review rate                {metrics['review_rate']:.1%}",
        f"   automation rate            {metrics['automation_rate']:.1%}",
        f"   deferral precision         {metrics['deferral_precision']:.1%}"
        "   (of those reviewed, how many needed it)",
        f"   deferral recall            {metrics['deferral_recall']:.1%}"
        "   (of those needing review, how many got it)",
        f"   missed reviews             {metrics['missed_reviews']}",
        "",
        " CALIBRATION",
        f"   confidence when correct    {metrics['confidence_when_correct']}",
        f"   confidence when wrong      {metrics['confidence_when_wrong']}",
        f"   gap                        {metrics['calibration_gap']}"
        "   (near zero means confidence carries no signal)",
        "",
        " COST AND LATENCY",
        f"   mean latency               {metrics['mean_latency_ms']} ms",
        f"   total cost                 ${metrics['total_cost_usd']:.6f}"
        f"   (${metrics['total_cost_usd'] / max(1, metrics['cases']):.6f}/incident)",
        "",
    ]

    lines.append(" BY SLICE")
    for tag, stats in report["by_tag"].items():
        flag = "  <-- misses" if stats["critical_misses"] else ""
        lines.append(
            f"   {tag:<18} n={stats['scored']}/{stats['n']:<4} "
            f"category acc {stats['category_accuracy']:.0%}{flag}"
        )
    lines.append("")

    # Critical misses are listed unconditionally. An early version filtered on
    # "wrong category or off by 2+ bands", which hid a P2 auto-triaged as P3 -
    # a correct category, a single band of distance, and the single most
    # serious failure in the run.
    errors = [
        row
        for row in report["rows"]
        if row["critical_miss"]
        or not row["category_correct"]
        or (row["priority_distance"] or 0) >= 2
    ]
    if errors:
        lines.append(f" ERRORS ({len(errors)})")
        for row in errors:
            marker = "!!" if row["critical_miss"] else ("!" if row["under_called"] else " ")
            lines.append(
                f"  {marker} {row['id']}  {row['expected_category']} -> "
                f"{row['predicted_category']}  |  {row['expected_priority']} -> "
                f"{row['predicted_priority']}  |  conf {row['confidence']:.2f}"
                f"  |  {'reviewed' if row['routed_to_review'] else 'AUTO'}"
            )
            if verbose and row["note"]:
                lines.append(f"       {row['note']}")
        lines.append("")

    failures = check_gates(metrics)
    if failures:
        lines.append(" RELEASE GATES: FAILED")
        lines.extend(f"   - {failure}" for failure in failures)
    else:
        lines.append(" RELEASE GATES: PASSED")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate the triage workflow.")
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--out", type=Path, help="Write the full JSON report here.")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of a report.")
    parser.add_argument(
        "--compare",
        type=Path,
        help=(
            "Compare against a recorded report and fail on regression instead of "
            "applying the absolute release gates. This is what CI runs: the offline "
            "baseline is not expected to clear production gates, but it must not be "
            "allowed to get quietly worse."
        ),
    )
    parser.add_argument("--verbose", action="store_true", help="Show notes on failing cases.")
    parser.add_argument(
        "--limit",
        type=int,
        help=(
            "Score only the first N cases. Free-tier Gemini quota is 20 requests "
            "per day per model, so a full 30-case run cannot complete on it; this "
            "gives a clean partial run instead of one padded with quota failures."
        ),
    )
    args = parser.parse_args(argv)

    cases = load_dataset(args.dataset)
    if args.limit:
        cases = cases[: args.limit]
    report = evaluate(cases, TriagePipeline())

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render(report, verbose=args.verbose))

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f" Full report written to {args.out}\n")

    if args.compare:
        baseline = json.loads(args.compare.read_text(encoding="utf-8"))["metrics"]
        regressions = check_regression(report["metrics"], baseline)
        if regressions:
            print(f" REGRESSION vs {args.compare}:")
            for failure in regressions:
                print(f"   - {failure}")
            print()
            return 1
        print(f" No regression against {args.compare}.")
        print()
        return 0

    return 1 if check_gates(report["metrics"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
