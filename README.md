# Incident Triage with Gemini

[![CI](https://github.com/brianmahlatini/incident-triage-gemini/actions/workflows/ci.yml/badge.svg)](https://github.com/brianmahlatini/incident-triage-gemini/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

A proof of concept that takes an unstructured operational incident report and
returns a validated, structured triage record — category, summary, priority,
recommended next action, and an explicit decision about whether a human needs to
look at it.

Python + FastAPI backend, React + TypeScript console, Gemini via the
`google-genai` SDK (Vertex AI or the Developer API), with a deterministic
offline provider so the whole thing runs with no credentials.

**Contents**

| | |
|---|---|
| [The one idea](#the-one-idea-worth-taking-from-this) | Why the model is not the workflow |
| [Measured results](#measured-results) | Gemini vs a keyword baseline, with sample sizes |
| [What it is worth](#what-it-is-worth) | The business case, and when not to build this |
| [Run it](#run-it) | Locally, in Docker, or against live Gemini |
| [How each requirement was addressed](#how-each-requirement-was-addressed) | The nine evaluation points, mapped to files |
| [Scope and what I would cut](#scope-and-what-i-would-cut) | Time spent, and the trade-off named |
| [Honest limitations](#honest-limitations) | What this does not yet prove |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | GCP production design, diagram, alternatives rejected |
| [docs/EVALUATION.md](docs/EVALUATION.md) | What "good" means and how it is measured |

---

## The one idea worth taking from this

**Gemini proposes. Deterministic rules decide. A human confirms anything that
matters.**

The model call is a single step in the middle of the workflow, not the workflow.
Everything before it exists to avoid pointless or unsafe calls; everything after
it exists because a model's answer is a proposal, not a verdict — and a model's
self-reported confidence is far too unreliable, on its own, to hold a routing
decision.

```
validate → redact → prompt → Gemini (retry) → parse & validate
        → check grounding → route → log & measure
```

Concretely, that means a report can be sent to a human because the model was
unsure, *or* because it said `UNKNOWN`, *or* because policy says every P1 gets
confirmed, *or* because the raw text mentions ransomware even though the model
classified it as a printer fault, *or* because it quoted a sentence that is not
in the report. The last three keep working when the model is confidently wrong,
which is the only situation where a safety net earns its cost.

The live evaluation turned out to justify that design directly: on one sample,
Gemini's mean confidence was *higher* when it was wrong (0.850) than when it
was right (0.825). Anything resting on a confidence threshold alone would have
auto-triaged those incidents on a number that meant nothing.

---

## Measured results

Against live Gemini, and against a keyword-rules baseline as the floor an LLM
has to clear. Full reports in [`eval/results/`](eval/results/).

| | Keyword baseline | **gemini-3.5-flash** | gemini-3.6-flash |
|---|---|---|---|
| Cases answered | 28 of 30 | 19 of 30 | 11 of 18 |
| Category accuracy (of answered) | 64.3% | **100%** | 90.9% |
| Priority mean distance | 0.79 | 0.41 | **0.20** |
| Severe priority errors | 3 | **0** | **0** |
| **Critical misses** | **1** | **0** | **0** |
| Hallucination rate | 0% | **0%** | **0%** |

Gemini beat the baseline on every measure that matters and fabricated no
evidence at all. The samples are small and deliberately labelled as such: the
Gemini runs were on the free tier, which is **capped at 20 requests per day per
model**, so neither completed the full set. 100% is 19 out of 19 — enough to
say the baseline is beaten, not enough to set a production threshold.

The quota exhaustion was more useful than more successes would have been. It
exercised the failure path against a live API and confirmed that **every failed
call was routed to a human rather than dropped**, that the release gates
correctly went red, and it exposed a real bug: the API replied `retryDelay:
30s` while the backoff capped at 8s, so all three retries were guaranteed to
fail. Fixed, and covered by tests. Details in
[docs/EVALUATION.md](docs/EVALUATION.md).

---

## What it is worth

The cost side is easy and small: **~$50/month** at 5,000 incidents/day, of
which the model is about half (measured at ~$0.00016 per incident). Working
through what that buys is more useful than the number itself.

**The value is not headcount.** At a conservative two minutes to read,
categorise and route a report, 5,000 a day is roughly **165 hours of reading
per day**. It would be dishonest to present that as the saving — nobody
removes twenty people because a classifier appeared, and a system that needs a
human on ~70% of incidents is not trying to. The return is in three narrower
places:

1. **The auto-triaged share never waits.** Those tickets reach the right queue
   in seconds instead of sitting until someone opens the mailbox. On a P1 at
   03:00 that difference is the entire value of the system. Measured
   automation rate here is **11–20%**, but read that with care: the evaluation
   set deliberately over-represents ambiguous, adversarial and
   security-adjacent reports, so it is close to a worst case. What a real
   incident stream produces is one of the first things I would measure, and
   the threshold is tunable against it.
2. **The reviewed share arrives pre-digested.** The rest still reach a person,
   but with a summary, a proposed category and priority, quoted evidence and a
   named reason for the referral. Confirming a proposal takes a fraction of
   the time of triaging from scratch — and because that is the *majority* of
   the volume, it is where most of the saving actually sits, not in the
   automated slice.
3. **Consistency.** The same report gets the same priority on a Tuesday
   morning and at the end of a night shift. Priority rubrics drift when tired
   people apply them; that drift is invisible and expensive.

**The metric that tracks the business case is `automation_rate`**, which is
why the evaluation harness treats it as a release gate rather than a
statistic. Accuracy with a 0% automation rate is a system that costs money and
delivers nothing; the harness fails a run that drops below 15% for exactly
that reason.

**When I would not build this.** At 50 incidents a day the arithmetic
collapses — two hours of reading, no queue, and a person who already knows the
estate will out-perform any classifier. This design earns its keep on volume,
on out-of-hours coverage, and where inconsistent prioritisation is already
causing harm. If none of those three apply, the honest recommendation is a
better intake form.

---

## Run it

No API key needed — the default provider is a deterministic rules engine that
exercises every path in the pipeline.

```bash
python -m venv .venv && .venv/Scripts/activate     # Windows
# python3 -m venv .venv && source .venv/bin/activate  # macOS / Linux
pip install -r requirements.txt

# 1. Command line, over the built-in examples
PYTHONPATH=src python -m triage --samples

# 2. One incident
PYTHONPATH=src python -m triage --text "The claims portal is down for all 200 branch users since 09:00"

# 3. Tests (99 backend, no network)
pytest

# 4. Evaluation harness
python eval/run_eval.py --verbose

# 5. Frontend tests (16)
cd frontend && npm install && npm test
```

### The web console

```bash
# terminal 1 — API
PYTHONPATH=src python -m uvicorn triage.api:app --reload --port 8000

# terminal 2 — frontend
cd frontend && npm install && npm run dev     # http://localhost:5173
```

Or build the frontend once (`cd frontend && npm run build`) and the FastAPI app
serves it directly from `http://localhost:8000`.

The console has three screens: **Triage** (paste a report, see the structured
result with per-quote grounding marks and the routing decision), **Review
queue** (the human-in-the-loop step — accept or override, and the decision is
recorded as an evaluation label), and **Metrics** (throughput, review rate, p95
latency, cost, and why incidents were sent to humans).

### Docker

One command builds the console and the API together and serves both:

```bash
docker compose up --build          # http://localhost:8000

# or plain docker
docker build -t incident-triage .
docker run -p 8000:8080 incident-triage
```

The image is a two-stage build: Node compiles the React console and is then
discarded, so the 298 MB runtime carries Python and the built static files
only. It runs as a non-root user, binds to `$PORT` (Cloud Run does not always
supply 8080), and has a health check.

To run the container against live Gemini, pass the key through from your
environment rather than baking it into a file:

```bash
GOOGLE_API_KEY=... TRIAGE_PROVIDER=gemini docker compose up --build
```

### Deploying to Cloud Run

[`cloudbuild.yaml`](cloudbuild.yaml) runs tests, then the evaluation gates,
then builds and deploys — in that order, and a non-zero exit from either check
fails the build. **A prompt change that degrades quality cannot ship**, which
is the reason `run_eval.py` returns an exit code rather than only printing a
report.

```bash
gcloud builds submit --config cloudbuild.yaml \
  --substitutions=_REGION=europe-west1,_SERVICE=triage-worker
```

The deploy step uses Vertex AI rather than an API key, a dedicated service
account rather than the default compute one, concurrency 40 (the work is
almost all waiting on the model), and no public ingress.

### Switching to live Gemini

```bash
cp .env.example .env
```

Then either set `TRIAGE_PROVIDER=gemini` with `GOOGLE_API_KEY=...` (Developer
API), or set `GOOGLE_GENAI_USE_VERTEXAI=true` with `GOOGLE_CLOUD_PROJECT` and
`GOOGLE_CLOUD_LOCATION` (Vertex AI, and what production would use). Nothing else
changes.

That is a deliberate design property, not a convenience: the pipeline depends on
the `Provider` interface in [`providers/base.py`](src/triage/providers/base.py),
never on a vendor SDK. Supporting a different backend — Bedrock, OpenAI, a
self-hosted model — is one class implementing `generate()`, and switching is an
environment variable. Everything that makes this system trustworthy (validation,
redaction, grounding, routing, retry) sits outside the model call and is
unaffected by which model answers.

**Pin an explicit model version.** `gemini-2.0-flash` was current when this was
written and now returns 404. A `-latest` alias would make evaluation runs
incomparable and turn a model upgrade into an unannounced production change.

---

## How each requirement was addressed

### Prompt design — [`prompt.py`](src/triage/prompt.py)

Versioned (`triage-prompt-v1`) and stamped on every result, so a change in
accuracy can be attributed to a change in wording. Four things do the work:
an **observable priority rubric** (scope and impact, not adjectives, so
"URGENT!!!" does not raise priority); an **explicit licence to abstain** —
`UNKNOWN` is described as a correct answer, because a model asked to pick from a
closed list always picks; a **grounding requirement** to quote the report
verbatim; and an **instruction hierarchy** that declares the fenced report to be
untrusted data. Three worked examples cover the clear case, the abstention and
the loaded-language case.

### Structured outputs — [`schema.py`](src/triage/schema.py)

Gemini's `response_schema` constrains decoding to the grammar, so "the model
returned prose instead of JSON" is not a failure mode that needs handling at
scale. Output is then validated against a Pydantic model with `extra="forbid"`,
enum-bounded fields and range checks. A test asserts the hand-written Gemini
schema and the Pydantic model cannot drift apart.

Two decisions worth pointing at. `overall_confidence` is the **minimum** of the
category and priority confidences, not the mean — a right category with a wildly
wrong priority is still a bad triage, and averaging lets one confident field
mask an uncertain one. And an `UNKNOWN` arriving with 0.99 confidence is
**clamped**, because that combination is incoherent and would otherwise sail
straight through the confidence gate.

### Input validation — [`validation.py`](src/triage/validation.py)

Rejections (empty, too short, not text) never reach Gemini. Warnings (thin
detail, non-prose, repetitive, instruction-like) proceed but are carried forward
so routing can lean towards review. Oversized input is **truncated rather than
rejected** — someone pasting a 50,000-line log dump still has a real incident.
Text is NFKC-normalised, control characters stripped, and the prompt fence
neutralised so input cannot close its own delimiter.

### Incomplete or ambiguous information

The model must populate `missing_information` and lower its confidence rather
than guess. Two or more missing facts triggers review on its own. The
`UNKNOWN`/`UNKNOWN` abstention is a first-class outcome, tested and evaluated as
correct behaviour on thin reports.

### Reducing hallucination — [`grounding.py`](src/triage/grounding.py)

The model must quote the report verbatim to justify its choices, and **those
quotes are then checked against the source**. That turns "please don't
hallucinate" from an unfalsifiable request into a number per incident that can
be thresholded, logged and alerted on. Matching is forgiving about form
(whitespace, casing, smart quotes) and strict about substance. A weaker
vocabulary-overlap check catches summaries describing a different incident,
skipped on abstentions where boilerplate is the correct output.

### Error handling — [`pipeline.py`](src/triage/pipeline.py)

Errors are classified by **what the caller should do about them**: transient
(429/503/timeout) retried with backoff; permanent (bad key, malformed request)
failed immediately, because retrying multiplies latency before the same
failure; safety blocks surfaced and routed to a human. A schema-violating
response is retried once with a repair instruction appended.

Backoff **honours the server's own `retryDelay` hint** where one is supplied,
falling back to exponential backoff with full jitter otherwise. That came
directly from live testing: against a real 429 saying "retry in 30s", the
computed curve spent all three attempts inside two seconds. A rate limiter that
tells you when to come back is better information than anything you can guess.

The core guarantee: **`run()` never raises and never drops an incident.** Every
path — rejected input, exhausted retries, safety block, unexpected exception —
returns a `TriageResult` with a status and a routing decision. A test asserts
this across every failure class. In operations, an incident that vanishes into a
stack trace is worse than one triaged badly, because nobody knows it is missing.

### Logging and observability — [`observability.py`](src/triage/observability.py)

One JSON object per line on stderr, in the shape Cloud Logging ingests natively
from Cloud Run (`severity` and friends promoted to first-class fields). Every
incident carries a correlation id, returned to the caller as well as logged, so
"this triage looks wrong" leads straight to the request. Latency, attempts,
tokens, estimated cost, redaction counts, grounding ratio and routing reasons
are all recorded.

**The incident text is never logged** — not the original, not the redacted
version. A SHA-256 fingerprint goes in instead, which answers the questions that
actually get asked ("is this the same report?", "did the input change between
retries?") without putting content into the least access-controlled copy of any
data in the system.

### Security and sensitive data — [`redaction.py`](src/triage/redaction.py)

PII is redacted **before** the model call, never after — once a payload has left
for a third-party endpoint, scrubbing the response is theatre. Emails, South
African ID numbers, phone numbers, payment cards, API keys, credential
assignments and private key blocks become typed, numbered placeholders
(`[EMAIL_1]`), so the model can still tell that the same person appears twice.
ID and card detection are checksum-gated so ticket references are not eaten.

What is **deliberately not redacted** matters as much: hostnames, IPs, error
codes and timestamps stay intact, because they are the operational signal the
model needs and removing them would trade real accuracy for negligible privacy
gain.

Prompt injection is handled as an input problem — fence, declare untrusted,
neutralise the delimiter, constrain decoding — and detected attempts are flagged
for review rather than blocked, since the report may still describe a genuine
incident.

### Testing and evaluation — [`tests/`](tests/), [`eval/`](eval/)

**115 tests, no network, all deterministic** — 99 backend (pytest) and 16
frontend (Vitest + Testing Library).

The backend tests cover the schema contract, validation, redaction (including
what must *survive* it), grounding, every routing rule, the full retry matrix
including the server retry hint, and the HTTP surface including the review
loop. The frontend tests cover the things that would actually mislead an
operator: that a flagged incident never renders as auto-triaged, that a
fabricated evidence span is visually distinct from a grounded one, that
redaction counts are surfaced, and that a dead backend produces a readable
message rather than a blank panel.

The evaluation harness (`eval/run_eval.py`) scores 30 labelled incidents on
accuracy, priority *distance*, critical misses, hallucination rate, deferral
precision/recall, automation rate and calibration. It has **two exit-code
modes**, which is a deliberate distinction:

- **Absolute gates** (default) answer *"is this good enough to deploy?"* They
  are calibrated for the deployed model, and the offline baseline is not
  expected to clear them.
- **Regression check** (`--compare`) answers *"did this change make things
  worse?"* by comparing a run against a recorded one. This is what CI runs.
  Gating CI on the absolute gates would leave the build permanently red and
  the signal permanently ignored; comparing the baseline against its own
  recorded numbers catches a genuine pipeline regression without pretending a
  keyword engine is shippable.

`--limit` keeps a run inside a free-tier quota. See
[docs/EVALUATION.md](docs/EVALUATION.md) for the reasoning behind each measure.

### CI

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs on every push and
pull request, needs no credentials, and has three jobs: **backend** (lint,
tests, evaluation regression check), **frontend** (tests, type-check, build),
and **container** — which builds the image, starts it, and triages a real
incident through the HTTP surface. Building an image proves it compiles;
starting it and getting a valid triage record back proves it works, which is a
different claim and the one that matters.

---

## Documents

- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — GCP production design with
  a diagram, service choices, the alternatives rejected and why, security,
  failure handling, and an indicative cost of ~$50/month at 5,000 incidents/day.
- **[docs/EVALUATION.md](docs/EVALUATION.md)** — what "good" means, how to build
  the dataset, which measures and why, the errors that would stop a rollout, the
  deferral rules, and live monitoring.

---

## Layout

```
src/triage/
  schema.py         data contract: enums, validation, Gemini response schema
  prompt.py         versioned prompt, rubric, few-shot examples
  validation.py     input rejection vs warning, sanitisation, injection flags
  redaction.py      PII removal before egress, checksum-gated
  grounding.py      verbatim-quote verification against the source
  gating.py         the routing rules that decide on human review
  pipeline.py       orchestration, retry, error taxonomy
  observability.py  structured logging and metrics
  providers/        Provider interface, Gemini client, deterministic mock
  api.py            FastAPI surface including the review loop
  cli.py            command line entry point
frontend/src/       React + TypeScript console, with tests beside components
eval/               labelled dataset and evaluation harness
tests/              99 backend tests
docs/               architecture and evaluation write-ups
Dockerfile          two-stage build: Node compiles the console, Python serves it
cloudbuild.yaml     test -> evaluate -> build -> deploy to Cloud Run
.github/workflows/  CI: backend, frontend, and a container smoke test
```

---

## Scope, and what I would cut

The brief asked for no more than 2.5 hours and said it was not looking for a
polished product. This repo is past that, and it is worth being straight about
which parts are which.

**The answer to the brief** is the pipeline and the thinking around it: the
schema contract, the prompt, validation, redaction, grounding, the routing
rules, the error handling, the backend tests, the evaluation harness, and the
two written documents. That is the part I would defend as necessary.

**Everything after that was me seeing how far the tooling would go** —
the React console, Docker, Cloud Build, GitHub Actions, the frontend tests.
Most of it was written with AI assistance, which is the honest reason there is
more here than a 2.5-hour budget normally produces, and the brief did say it
was interested in how I use those tools.

**Under a hard limit, the cut order would be:** the React console first (a CLI
demonstrates the same workflow and the console is the single largest piece of
non-essential code), then the container and CI configuration (valuable in a
real engagement, not evidence of anything the brief asked about), then the
frontend tests, which only exist because the console does.

**What I would not cut, at any budget:** the evaluation harness and the
routing rules. Without the harness there is no way to answer "is this good
enough", which is a third of the brief. Without the routing rules the system
is a classifier that occasionally sends a security incident to the printer
queue with high confidence. Those two are the difference between a demo and
something you could put in front of an operations team.

---

## Honest limitations

- **The 30-case dataset is mine, not the client's.** Domain-specific language is
  where a general prompt breaks, and no synthetic set will reveal that.
- **The Gemini runs are partial.** Free-tier quota (20/day/model) meant 19 and
  11 scored cases. The numbers are directional, not production evidence.
- **Latency is 14–17s per incident** with reasoning enabled — fine for a
  queue-based workflow, far too slow for anything interactive. Disabling the
  thinking budget cuts it to ~2s on models that allow it; whether that costs
  accuracy is an evaluation question I did not have quota to answer.
- **The mock provider is a keyword baseline, not a model.** It exists to make
  the pipeline demonstrable and testable offline, and to serve as the floor
  Gemini has to clear. It currently fails the release gates — which is the
  point.
- **In-process state.** The review queue and metrics live in memory and reset on
  restart. Production replacements (Firestore, BigQuery, Cloud Monitoring) are
  specified in the architecture document; the metric names already match.
- **The confidence threshold of 0.70 is a starting point**, not a finding. The
  right value comes from a precision/recall curve over real review data, and it
  will probably differ per category.
- **No retrieval over past incidents.** Grounding "recommended next action" in
  what actually resolved a similar incident before would likely be worth more
  than any further prompt tuning. It is the first thing I would build next.
- **The concurrency figure is reasoned, not measured.** Cloud Run concurrency
  40 follows from the work being IO-bound on the model call, but nothing here
  has been load-tested, and I would not defend the number without doing so.
- **The evaluation labels are my own.** No second annotator, so
  inter-annotator agreement is unmeasured — which means there is no established
  ceiling to judge the model against.
