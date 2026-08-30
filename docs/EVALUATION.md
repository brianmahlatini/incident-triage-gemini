# Evaluation: is this actually good enough to use?

The honest starting position is that "the LLM returned a plausible answer" is
not evidence of anything. This document sets out what good looks like, how the
dataset is built, what is measured, which errors would stop a rollout, when the
system defers, and how quality is watched once live.

Everything described here is implemented in `eval/run_eval.py` and runs in
seconds against `eval/dataset.jsonl`.

---

## 1. What does a "good" output look like?

Good is not "the model agreed with me". A triage record is good when it has all
five of these properties:

1. **Correct enough to route.** The category sends the ticket to the team that
   can actually act on it. Confusing `INFRASTRUCTURE_OUTAGE` with
   `APPLICATION_ERROR` on a genuinely ambiguous report is tolerable; confusing
   `SECURITY_INCIDENT` with `USER_SUPPORT` is not.
2. **Priority within one band, and never under-called on a serious incident.**
   P2 called P3 is a scheduling annoyance. P1 called P4 is an outage nobody was
   paged for. These are not the same error and must not share a metric.
3. **Grounded.** Every quoted span appears in the report. No invented system
   names, user counts, timestamps or root causes.
4. **Honestly uncertain.** A vague report produces `UNKNOWN` and a low
   confidence, not a confident guess. Abstention on a thin report is a *correct*
   answer, and it is scored as one.
5. **Actionable.** The next action is a concrete step someone can take now, not
   "investigate the issue".

A worked example of good, on a thin report:

> **Input:** "System not working properly since this morning. Please assist."
>
> **Good output:** `UNKNOWN` / `UNKNOWN`, confidence 0.15, missing information
> lists the system, the symptom and the scope, next action is to contact the
> reporter for those three specifics, routed to a human.
>
> **Bad output:** `APPLICATION_ERROR` / `P3_MEDIUM`, confidence 0.8. Nothing in
> the report supports either label. It looks like a result and is worse than
> nothing, because it will be believed.

---

## 2. How I would build the evaluation dataset

**Where it starts (now).** 30 hand-labelled cases in `eval/dataset.jsonl`,
written to cover the taxonomy and — more importantly — the ways the system can
fail. Each carries an expected category, an expected priority, whether a human
*should* see it, tags for slicing, and a note explaining the label. Small, but
every case earns its place.

The deliberate composition:

| Slice | Purpose |
|---|---|
| Clear-cut incidents | Baseline competence; these must be right |
| Ambiguous / incomplete | Does it abstain, or does it guess? |
| Adversarial tone | "URGENT!!!" on a cosmetic issue must stay P4 |
| Prompt injection | Injected instructions classified as data, never obeyed |
| PII-heavy | Redaction verified before egress |
| Subtle security | Ransomware described without the word appearing |
| Mixed signals | Two issues in one report; the serious one must win |
| Priority boundaries | Department-wide (P2) vs company-wide (P1) |
| Silent failures | Stale data nobody noticed — easy to under-call |

**Where it goes (production).** Hand-labelling does not scale and a curated set
stops resembling reality within months. The real dataset comes from the review
loop: every reviewer accept/override is a labelled example drawn from live
traffic, written to BigQuery with the model's original answer and the prompt
version. That gives, per month:

- Every reviewed incident, labelled by a domain expert as a side effect of work
  they were doing anyway.
- **Stratified sampling** of auto-triaged incidents — a random 2% is pulled for
  blind review, because a dataset made only of incidents the system already
  flagged tells you nothing about what it got wrong silently. This is the
  sample that finds the errors nobody noticed.
- **Disagreement mining**: cases where the model's category differs from the
  team the ticket was ultimately resolved by.

Two practical safeguards: **double-label a 10% subset** to measure
inter-annotator agreement — if two experienced operators disagree on 20% of
priorities, the model cannot be expected to beat that ceiling, and the rubric
needs fixing before the prompt does. And **freeze a golden set** of ~200 cases
that never changes, so results stay comparable across prompt versions.

---

## 3. Which measures, and why those

Implemented in `eval/run_eval.py`. Chosen around what a bad output *costs*.

**Output validity**
- `schema_valid_rate` — proportion producing a valid record. Should be ~100%
  with constrained decoding; anything less points at an integration fault.

**Classification**
- `category_accuracy` — reported, but the least interesting number here. A
  wrong category is noticed within minutes by the receiving team.
- `priority_mean_distance` — mean absolute distance in priority bands. Treats
  P1→P4 as four times worse than P1→P2, which matches reality.
- `severe_priority_errors` — count off by two or more bands.

**Safety** — the ones that would stop a rollout
- `under_called_serious` — a true P1 or P2 predicted as P3 or P4.
- **`critical_misses`** — under-called **and** auto-triaged with no human in the
  loop. **This is the metric that gates release, and its threshold is zero.**
  Every other error has a safety net; this one does not.
- `hallucination_rate` — proportion with at least one quoted span absent from
  the report. Needs no labels, so it doubles as a live production signal.

**Human-in-the-loop** — always read as a pair
- `deferral_recall` — of the incidents that needed a human, how many got one.
- `deferral_precision` — of those sent to a human, how many needed it.
- `automation_rate` — reported alongside, because a system that defers
  everything scores perfectly on safety while delivering zero value. **Accuracy
  without automation rate is a meaningless number.**

**Calibration**
- `confidence_when_correct` vs `confidence_when_wrong`. If those are close, the
  confidence score carries no signal and the entire routing design rests on
  nothing. The gap is the evidence that the gate is doing real work.

**Operations**
- p95 latency, cost per incident, retry rate.

**The baseline that matters.** Every number above is also computed for the
keyword-rules provider in `providers/mock.py`. If Gemini cannot beat a few
hundred lines of regex, the finding is that this problem did not need an LLM —
and that is worth knowing before the client pays for one.

---

## 4. Which errors concern me most

In descending order of cost:

1. **A confidently wrong under-call on a serious incident.** A security
   compromise labelled `USER_SUPPORT` / `P4` with 0.9 confidence. High
   confidence means it skips review; low priority means it sits in a queue for
   days. The damage compounds while the system reports success. Every design
   decision that looks over-cautious — the P1 rule, the keyword backstop, the
   min-not-mean confidence, the contradiction checks — exists for this one case.
2. **Fabricated detail.** An invented user count or system name in the summary.
   Poisonous because it is *plausible* and gets acted on; it survives into
   incident reports and post-mortems. Caught by the grounding check.
3. **Silent degradation.** A model or prompt update quietly shifts behaviour;
   accuracy drifts down while every dashboard stays green because nothing
   errors. Caught by distribution monitoring and reviewer agreement, not by
   error rates.
4. **Priority inflation.** Everything creeps towards P1, the team stops
   trusting priorities, and the system is worse than no system.
5. **Over-deferral.** Nothing is wrong and nothing is automated. Cheap to
   detect, easy to fix by tuning the threshold, but fatal to the business case
   if unmeasured.

Note the ordering: **accuracy is not at the top.** A wrong-but-flagged answer
costs a reviewer thirty seconds. A wrong-and-confident answer costs an incident.

---

## 5. When should the system defer to a human?

Implemented in `gating.py`. Four independent kinds of evidence, because
model-reported confidence alone is not trustworthy enough to hold a routing
decision — LLMs are systematically overconfident on inputs that look familiar
but are not.

| Trigger | Rationale |
|---|---|
| Confidence < 0.70 | The model's own uncertainty |
| Category or priority is `UNKNOWN` | Explicit abstention |
| **Any P1** | Policy. No confidence score buys an unreviewed P1 |
| **Security / safety / regulatory keyword in the raw report** | Policy backstop that works even when the model classified the incident as something mundane |
| Quoted evidence not found in the source | Automated fabrication check |
| Two or more missing facts | Insufficient basis for a decision |
| Contradictory output (P1 with no evidence; critical access request) | Cheap consistency checks the model cannot talk its way past |
| Degraded or injection-flagged input | Input quality |
| Any model or transport failure | Never drop an incident |

The last two rows in the rationale column are the important ones: rules 3–4
keep working when the model is confidently wrong, which is the only situation
where a safety net earns its cost.

**Threshold tuning.** 0.70 is a starting point, not a finding. The right value
comes from a precision/recall curve over the review data once a few thousand
incidents have been through: plot critical misses and automation rate against
threshold, and pick the highest automation rate at which critical misses stay at
zero. Expect it to differ per category — a security incident probably deserves a
higher bar than an access request.

---

## 6. Monitoring once live

Offline evaluation tells you about the past. Three layers watch the present:

**Label-free signals, computed on every incident**
- Grounding ratio and hallucination rate — no ground truth needed.
- Confidence distribution — a shift in shape is an early warning.
- Category and priority distribution vs a 7-day baseline (χ²). A silent change
  in the mix is usually the first visible sign of drift.
- Abstention rate, schema failure rate, retry rate, p95 latency, cost/incident.

**Human feedback signals**
- **Reviewer agreement rate** — of the incidents a human checked, how many were
  accepted unchanged. The single most useful live quality signal, and it needs
  no labelled dataset. Sustained decline means investigate.
- Override patterns by category — reveals *where* it is going wrong, not just
  that it is.
- Reopened or re-prioritised tickets downstream — the model said P4, the team
  escalated to P1 the next day. Ground truth arriving late and for free.

**Scheduled evaluation**
- Nightly Cloud Run Job over the frozen golden set. Any gate failure alerts.
- The same suite runs in Cloud Build on every PR and blocks deployment on a
  gate failure, so a prompt change cannot ship on the strength of one person
  trying three examples.
- Monthly: re-run the full archived corpus against the current prompt and
  compare — possible only because raw incidents are retained in GCS.

**Shadow deployment for changes.** A new prompt or model runs alongside the
current one on live traffic without its output being used. Compare on
agreement, confidence and review rate before promoting. Prompt changes are code
changes, and a prompt version is stamped on every result precisely so a shift in
metrics can be attributed to one.

---

## 7. Measured results

Run against the keyword baseline and against live Gemini. Raw reports are in
[`eval/results/`](../eval/results/).

| | Keyword baseline | **gemini-3.5-flash** | gemini-3.6-flash |
|---|---|---|---|
| Cases answered | 28 of 30 | 19 of 30 | 11 of 18 |
| **Category accuracy** (of answered) | 64.3% | **100%** | 90.9% |
| **Priority accuracy** (of answered) | 32.1% | 57.9% | **81.8%** |
| **Priority mean distance** | 0.79 | 0.41 | **0.20** |
| Severe priority errors (≥2 bands) | 3 | **0** | **0** |
| **Critical misses** | **1** | **0** | **0** |
| Hallucination rate | 0% | **0%** | **0%** |
| Deferral recall | 93.8% | 93.8% | **100%** |
| Mean latency (answered) | <1 ms | 16.9 s | 14.1 s |
| Cost per incident | — | ~$0.00017 | ~$0.00015 |

**Gemini beats the rules baseline on every measure that matters.** The baseline
produced a critical miss — a P2 downgraded to P3 and auto-triaged with no human
in the loop — and three severe priority errors. Both Gemini models produced
none of either, and neither fabricated a single evidence span.

### How to read the sample sizes

These are small samples, and the table says so deliberately. The Gemini runs
were made on the **free tier, which is capped at 20 requests per day per
model**, so neither completed the full 30 cases. `--limit` exists for exactly
this reason. The category accuracy of 100% is 19 out of 19, not a claim that
the model is perfect — on 19 cases the 95% confidence interval still runs from
roughly 82% upward. It is enough to say the baseline is beaten and not enough
to set a production threshold.

Classification accuracy is scored over **answered** cases only. An earlier
version of this harness counted a rate-limited call as a misclassification,
which made a quota exhaustion look like a model that could not classify. A
failed call is an availability problem and belongs in the availability
counters; it still counts against the safety and deferral metrics, where what
matters is the outcome for the incident rather than its cause.

### What the quota exhaustion demonstrated

The 9 failed calls were more informative than 9 more successes would have been.
Against a live API, under real failure:

- **Every failed incident was routed to a human, not dropped.** The core
  operational guarantee, demonstrated rather than asserted.
- **The release gates correctly went red** on the degraded run
  (`automation_rate` below the floor), which is the harness doing its job.
- **It exposed a real bug.** The API replied `RetryInfo: retryDelay 30s`; the
  backoff curve capped at 8 seconds, so all three retries fired within two
  seconds and were guaranteed to fail. The provider now parses the server's
  hint and honours it, with three tests covering the hinted, capped and
  unhinted paths.

### One finding that argues against trusting confidence

On the `3.6-flash` sample, mean confidence on correct answers was **0.825** and
on incorrect answers **0.850** — a calibration gap of **−0.025**. On that
sample the model's self-reported confidence carried *no signal at all*, and was
marginally higher when it was wrong.

Eleven cases is far too few to conclude anything general. But it is a direct
illustration of why this system never lets confidence hold a routing decision
by itself. Had the design relied on a confidence threshold alone, those
incidents would have been auto-triaged on a number that meant nothing. The
policy rules, the grounding check and the contradiction checks are what
produced zero critical misses, and they do not depend on the model being
honest about its own uncertainty.

By contrast the baseline's gap of +0.31 shows its confidence *does* track
correctness — a keyword engine knows when it matched nothing. The interesting
lesson is that the more capable model was the less calibrated one.

### Reproducing

```bash
export TRIAGE_PROVIDER=gemini GOOGLE_API_KEY=...
python eval/run_eval.py --out eval/results/gemini.json --verbose

# On the free tier, keep the run inside the 20/day/model quota:
python eval/run_eval.py --limit 18 --out eval/results/partial.json
```

---

## 8. What I would want before recommending production

- **≥ 500 labelled incidents** from the client's real traffic, not my invented
  ones. Domain-specific language is where a general prompt breaks.
- **A billed project**, so a full evaluation run completes and the numbers
  above stop being partial.
- **Calibration measured properly** on a few hundred cases. The −0.025 gap on
  `3.6-flash` may be noise or may be real; if it is real, the confidence
  threshold is close to worthless on that model and the routing rules are
  carrying the entire safety argument.
- **A latency decision.** 14–17 seconds per incident is fine for a queue-based
  workflow and far too slow for anything interactive. Disabling the thinking
  budget where the model allows it cuts this to ~2 seconds; whether that costs
  accuracy is an evaluation question, not a preference.
- **Inter-annotator agreement measured.** Until two experts agree on priority,
  there is no ceiling to measure the model against.
- **Zero critical misses over a 1,000-incident shadow run**, with the system's
  output recorded but not acted on.
- **A tuned per-category threshold**, derived from that data rather than chosen.
- **An agreed rollback trigger** — a specific metric and value at which the
  system reverts to manual triage, decided before go-live rather than during the
  first bad week.
