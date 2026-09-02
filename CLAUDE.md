# Razorpay AI Buildathon 2026 — Track 02: AI Risk Manager

**Chargeback Evidence Responder + Risk Enrichment System**

This file is project memory. It loads automatically into every Claude Code session
rooted in this folder — orchestrator and subagents both. Read it before writing code.

## Deadline reality

Submission closes **5 September 2026**. Build sequencing below assumes you're starting
from today with roughly three days on the clock — scope is already cut to fit that,
not to a leisurely week. Do not add scope back in. If a subagent proposes "while I'm
in here, let me also build X," the answer is no unless X is on the definition-of-done
list below.

## What this system is

Razorpay's Track 02 asks for an AI Risk Manager. The specific wedge, chosen after
comparing this track against the funded YC landscape (Finic, DisputeNinja, Trench —
see research doc): those three cluster on fraud **scoring**. Chargeback **evidence
response** — actually assembling and submitting what wins a dispute — is comparatively
open. So this system is scoped as one thing, not two: a chargeback evidence responder
as the primary, demoable capability, with graph-based counterparty risk as an
enrichment signal feeding into it rather than a separate product bolted on the side.

Track 04 (AI Finance Controller / reconciliation) was researched and designed in
parallel as a backup direction — that material lives in the reference docs linked
below (System Design doc, Part A) but is **explicitly out of scope for this repo**.
Nothing in this folder should import, reference, or build toward it. If it's ever
worth building, it gets its own folder.

## The actual bar (read this twice)

Razorpay's own language across every track's rubric: *explainable, bounded, and
gated*. For Track 02 specifically: *honest metrics including false-positive cost*.
Evaluation criteria are problem taste, build quality, AI judgment, and — this is the
one most student submissions skip — **failure recovery**: what broke, and how you got
out of it.

The differentiator for this submission is not a slicker demo. It's a **documented
failure taxonomy** — this system should be able to say, precisely, where it is
uncertain, where it defers to a human, and what specifically it got wrong on the
held-out set, and that documentation is a first-class deliverable, not a postscript.
Every subagent's definition of done includes writing down its own failure modes as it
builds, in `docs/failure-taxonomy.md` (create it if it doesn't exist; append, don't
overwrite other agents' sections).

The other half of the differentiator: recommendations grounded in named regulatory
authority, not just an internal score. A risk manager that can classify disputes and
compute a graph signal but can't say which India framework its evidence standards or
timelines are actually grounded in isn't production-credible, however good the ML is.
`compliance-knowledge-graph` exists specifically for this — treat its citations as
part of what "explainable" means for this system, not a decorative add-on.

## Non-negotiable constraint: defense-only, structurally enforced

This system must be **structurally incapable** of moving money, freezing an account,
or contacting a customer autonomously — not "policy says it won't," but "the
credentials it holds cannot do that." Read-only against payment/order/dispute data,
write-only against its own recommendation/review/audit tables. Every high-confidence
recommendation still lands in a "draft for submit" queue that a human dispatches —
nothing auto-submits. This is architecture, not a comment in the code, and it is
itself part of the pitch: "where we chose not to use AI" is one of the four things
Razorpay evaluates.

## Orchestration model

- **Orchestrator (you, the top-level session): Opus.** Launch this project with
  `claude --model opus` from this folder. The orchestrator plans, sequences work,
  reviews subagent output, resolves conflicts between modules, and owns
  `docs/failure-taxonomy.md` and the definition-of-done checklist below.
- **Workers: Sonnet**, one per module, defined in `.claude/agents/*.md`. Each has
  `model: sonnet` pinned in its frontmatter so it runs on Sonnet regardless of what
  the orchestrator is running on. Each is scoped to exactly one module — don't ask
  a subagent to reach outside its file's stated ownership; if a task genuinely spans
  two modules, the orchestrator does the integration work directly rather than
  letting one subagent free-range into another's territory.
- Invoke a subagent either by letting the orchestrator route automatically (it
  matches your request against each file's `description`) or explicitly: *"Use the
  risk-graph-service subagent to implement the PageRank/betweenness pipeline."*
- **Cost discipline: Opus plans and integrates, it doesn't inspect.** Small, routine
  checks — does a file exist, does a test pass, does this one function do what it
  claims, a one-line fix — belong inside the subagent's own turn, as part of
  finishing its task, not bounced up for the orchestrator to look at directly. Every
  subagent's definition of done already requires it to run and report its own
  tests; trust that report by default rather than re-inspecting the same thing at
  the top level. Pull the orchestrator itself in only for: sequencing decisions,
  genuine cross-module integration, resolving a real conflict between two
  subagents' output, and the definition-of-done pass. If the same small inspection
  keeps recurring, that's a sign it belongs permanently in the owning subagent's own
  checklist — fix it there, don't keep absorbing it into the orchestrator's own
  turns.

## Architecture (condensed — full version in the System Design doc)

```
Razorpay Dispute Webhook
        |
        v
  Reason-Code Router  ---------------------------> [dispute-ingestion-router]
        |
        v
  Evidence Assembler <---- Order/Shipping/Comms ---> [evidence-assembler]
        |                  store (synthetic)
        v
  Risk Enrichment <-------- Graph Signal Service ---> [risk-graph-service]
  (counterparty lookup)     (PageRank + betweenness,
        |                    precomputed nightly batch)
        v
  Confidence Scorer ------------------------------> [confidence-scorer-review]
   |                    |
high conf          low conf
   |                    |
   v                    v
Draft-for-submit   Human Review Queue
   |                (priority = amount x time-to-deadline)
   +---------+----------+
             v
        Submission (human-dispatched, never automatic)
             v
        Outcome Tracker ----> feeds back into Confidence Scorer as labels

Cross-cutting (queried by Evidence Assembler and Confidence Scorer, not part of the
linear flow above — static reference, no batch job):
  Compliance Knowledge Graph — curated India regulatory anchors (PSS Act, RBI PA-PG
  Master Direction, DPIP, DPDP Act, Consumer Protection E-Commerce Rules)
  -> [compliance-knowledge-graph]
```

## Core data models

| Model | Key fields |
|---|---|
| `DisputeCase` | mirrors Razorpay's Dispute entity: `id`, `payment_id`, `amount`, `amount_deducted`, `reason_code`, `reason_description`, `respond_by`, `status` (open/under_review/won/lost/closed), `phase` (fraud/retrieval/chargeback/pre_arbitration/arbitration) — plus `assembled_evidence`, `confidence_score`, `recommended_action`, `reviewed_by` |
| `AccountRiskProfile` | `account_id`, `pagerank_score`, `betweenness_score`, `baseline_deviation`, `last_updated` |
| `EvidenceBundle` | mirrors Razorpay's evidence object: `shipping_proof`, `billing_proof`, `cancellation_proof`, `customer_communication`, `proof_of_service`, `explanation_letter`, `refund_confirmation`, `activity_log` |

The `phase` field is the thing most "chargeback bot" submissions miss: a dispute can
escalate through up to five stages, each with its own clock. A "won" dispute that
comes back as a pre-arbitration challenge has to be handled, not silently dropped.

Alongside these, `compliance-knowledge-graph` holds a separate, much smaller
reference graph — regulation and requirement nodes, not transactional records. It's
not part of the pipeline's data flow; other modules query it and attach what it
returns to their own output. See that agent file for its node/edge shape.

## API surface

`POST /webhook/dispute-created` · `GET /disputes/{id}/recommendation` ·
`POST /disputes/{id}/review` (records the human decision) ·
`GET /accounts/{id}/risk-profile`

## Stack

FastAPI, SQLAlchemy + SQLite (schema written Postgres-compatible, no SQLite-only
features), `networkx` for the transaction graph (no graph DB needed at demo scale —
a few hundred synthetic accounts runs fine in memory), Pydantic for all schemas,
`pytest` for tests. Resist the urge to add infra (Kafka, a real graph DB, containers
beyond a single Dockerfile) — that's over-engineering for a five-day solo demo and
actively hurts the "build quality" score by adding untested surface area.

## Data grounding

The synthetic data is not pure invention on three levels, and all three need a
named, checkable source: (1) feature families — mirror what real fraud/risk
datasets actually compute (velocity/time-delta features, counting/aggregation
features, match/mismatch flags), not just raw field names; (2) temporal structure —
the transaction graph is a real timestamped sequence per account, not a static
snapshot, so recency/velocity signals are actually derivable; (3) numbers — fraud
rates, amount distributions, graph shape calibrated against named public datasets,
checked for India-specific sources first (several UPI/India-fraud Kaggle datasets
exist and are worth inspecting directly) before falling back to global references
like IBM's AML dataset or the ULB credit card fraud benchmark. See
`synthetic-data-generator.md` and `risk-graph-service.md` for the full source list
and what each calibrates. This matters for the pitch: every synthetic number should
trace to a real, checkable source if a judge asks where it came from — "it seemed
reasonable" isn't an answer that survives that question.

## Repo layout

```
buildathon/
  CLAUDE.md                      <- this file
  README.md                      <- human quick-start
  docs/
    failure-taxonomy.md          <- living doc, every subagent appends its own section
  .claude/agents/
    dispute-ingestion-router.md
    evidence-assembler.md
    risk-graph-service.md
    confidence-scorer-review.md
    synthetic-data-generator.md
    qa-evaluator.md
    compliance-knowledge-graph.md
  src/                            <- created by the subagents as they build
    ingestion/
    evidence/
    risk/
    scoring/
    compliance/                    <- compliance-knowledge-graph's node/edge data + query fn
  data/                           <- synthetic datasets, created by synthetic-data-generator
  tests/
```

## Build sequence (definition of done, in order)

1. **synthetic-data-generator** runs first — everything downstream needs data to
   build against. Produces synthetic `DisputeCase` records across all 4 reason-code
   categories and all 5 phases, synthetic order/shipping/comms records, a synthetic
   transaction graph with deliberately planted mule-like behavioral shifts, and a
   labeled held-out set for precision/recall evaluation. **compliance-knowledge-graph**
   builds in parallel with this — it has no dependency on synthetic data, only on the
   fixed vocabulary already in this file.
2. **dispute-ingestion-router** and **risk-graph-service** can build in parallel once
   data exists — neither depends on the other.
3. **evidence-assembler** depends on the router's `DisputeCase` model, the
   reason-code-to-evidence mapping, and (for its DPDP notes on PII-bearing slots)
   `compliance-knowledge-graph`.
4. **confidence-scorer-review** comes last — it depends on evidence assembly and risk
   enrichment both being live, since it combines both signals, and calls
   `compliance-knowledge-graph` to attach citations to `recommended_action`.
5. **qa-evaluator** runs continuously alongside, not just at the end: precision/recall
   against the held-out set, false-positive cost, defense-only boundary verification
   (an actual test that the service account cannot call a money-moving endpoint — not
   just a design claim), a deadline-miss test (a dispute within 48 hours of
   `respond_by` and unresolved must get flagged), and confirmation that
   confidence-scorer-review's and evidence-assembler's output actually carries
   compliance citations, not just that the lookup function works in isolation.

Done means: the pipeline runs end-to-end on synthetic data, the confidence scorer
correctly routes high/low-confidence cases, the human review queue is priority-sorted,
recommendations and PII-bearing evidence slots carry real regulatory citations (not
fabricated clause numbers), `docs/failure-taxonomy.md` has a real, specific section
from every subagent — including compliance-knowledge-graph's explicit statement that
it is decision support, not legal certification — and the defense-only boundary is
verified by a test, not asserted in a comment.

## Reference docs (deeper detail lives here, not duplicated in this repo)

- Strategy & track analysis: https://docs.google.com/document/d/1dYISY3Yf3Tw9PtL-UEv6jiaLwpNfsVPhDx6Uaad-vcg/edit
- Track 2 & 4 technical research (Part B is Track 2 — Dispute schema, reason codes,
  evidence mapping, India fraud layer / DPIP):
  https://docs.google.com/document/d/1OiRZckArDbUc-RK2X4sjA2RxIkul-J1Y6p-qx7JLs9s/edit
- Track 2 & 4 system design (Part B is Track 2 — this is the architecture this repo
  implements): https://docs.google.com/document/d/1Ob5OtbOcdeG2JQmWLSsOkgM1d5-sgkyyDmzfI3nXAPY/edit

## Working rules for the orchestrator

- Keep every subagent inside its stated module boundary. Cross-module glue code is
  the orchestrator's job.
- Never let a subagent add autonomous-submission capability "to save a step." If it's
  tempting, that's the signal to write it down in the failure taxonomy instead.
- When a subagent finishes a module, check its self-reported test results and
  failure-taxonomy entry against this file's definition of done — that's a review
  of its evidence, not a re-run of its inspection. Only go verify something
  yourself when the report is missing, inconsistent, or touches a cross-module
  integration point another subagent also owns.
- Prioritize a thin, fully-working, end-to-end pipeline over any one module being
  gold-plated. A complete-but-simple system beats a partial-but-sophisticated one,
  both for the deadline and for the demo.
