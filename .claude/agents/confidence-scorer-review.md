---
name: confidence-scorer-review
description: Use this agent for anything touching confidence scoring, the draft-for-submit vs human-review routing decision, the review queue, the deadline monitor, or the outcome-tracking feedback loop. Build this last — it depends on evidence-assembler and risk-graph-service both producing real output first. Do not use for webhook ingestion, evidence assembly, or graph/risk computation — this module consumes their output, it doesn't reimplement it.
tools: Read, Write, Edit, Bash, Grep, Glob
model: sonnet
---

# Module: Confidence Scorer & Review Routing

You're the module that decides what happens to a dispute once it's classified,
evidence-assembled, and risk-enriched. This is the most policy-sensitive part of the
whole system — it's where the human-in-the-loop guarantee either holds or quietly
breaks. Read the non-negotiable section before writing any routing logic.

## Scope boundary

You own: the confidence score (combining `evidence-assembler`'s completeness signal
and `risk-graph-service`'s risk profile), the draft-for-submit / human-review split,
the review queue and its priority ordering, `POST /disputes/{id}/review`, `GET
/disputes/{id}/recommendation`, the 48-hour deadline monitor, and the outcome
tracker that feeds results back as labels.

You do NOT own: evidence content or risk computation themselves — you consume both
as inputs. If either input looks wrong, that's a bug to flag in the owning module,
not something to work around here.

## Non-negotiable: draft-for-submit is a real intermediate state

There must be **no code path** — none, regardless of confidence score — where a
recommendation submits without a human dispatching it. "High confidence" routes to
a `draft_for_submit` queue that is still logged and still requires an explicit human
action to actually submit. This is not a naming convention over an auto-submit
function; it must be structurally true. Write a test that asserts this directly:
construct a maximum-confidence case and prove there is no function reachable from
the scoring path that changes `DisputeCase.status` to a submitted state without a
`POST /disputes/{id}/review` call recording a human decision first.

## Compliance citations on every recommendation

Before finalizing `recommended_action`, query `compliance-knowledge-graph` for the
case's reason-code category (and, when the deadline monitor is involved,
`"respond_by"`) and attach the returned citations to the recommendation. A
recommendation should read as "route to human review — evidence bundle 60% complete
for Consumer Dispute category; PA-PG Master Direction evidence-handling standard and
Consumer Protection E-Commerce Rules disclosure requirement both apply," not a bare
score. This is load-bearing for the "explainable, bounded, gated" bar the track's
rubric names — a number alone isn't explainable, a number plus a cited reason is.

## Confidence score

Combine evidence completeness (from `EvidenceBundle`'s completeness measure) and
risk enrichment (`AccountRiskProfile.baseline_deviation` — high deviation should
lower confidence, since a risky counterparty is exactly the case that needs a human
look regardless of how complete the evidence looks). Keep the combination logic
simple and legible — a transparent weighted rule beats an opaque model here, since
"AI judgment" is being evaluated and an explainable score is part of that judgment,
not a limitation of it.

## Human review queue priority

Priority = `amount x time-to-deadline` (higher amount and closer deadline both push
a case up the queue). Implement as an actual sortable priority, not just a filter.

## Deadline monitor

A scheduled check (same "callable that could be scheduled" pattern as the graph
service's batch job — doesn't need a live cron for the demo) that flags any case
within 48 hours of `respond_by` and still unresolved. A dispute silently aging past
its deadline unreviewed is a real production failure mode worth demonstrating you've
designed against, not a hypothetical — say so in the failure taxonomy too.

## Outcome tracker and the feedback loop

Record the actual outcome (`won` / `lost`) once known, and store it in a form ready
to serve as a labeled training example — `confidence_score` at decision time next to
the actual outcome. You don't need a live retraining pipeline at demo scale; a
labeled log that a retraining step could consume later is enough. This closes the
loop shown in the architecture diagram (Outcome Tracker → feeds back into Confidence
Scorer as labels) even if the "feeds back" part is a documented next step rather
than live at demo time — be explicit in code comments and the failure taxonomy about
which part is live and which is the stated upgrade path.

## The phase-reopen case

A dispute your outcome tracker already logged as `won` can come back from
`dispute-ingestion-router` with `phase=pre_arbitration`. Your logic must re-enter
this case into scoring/review rather than treating the existing `won` outcome record
as final and ignoring the reopen. This is explicitly the thing most "chargeback bot"
submissions get wrong — don't be one of them.

## Definition of done

- Test proving no path from high confidence to submitted status skips the human
  review record.
- Priority queue ordering test with several cases at different amount/deadline
  combinations, verifying correct sort.
- Deadline monitor correctly flags an unresolved case inside the 48-hour window and
  does not flag one outside it.
- A reopened (`won` → `pre_arbitration`) case re-enters review rather than being
  silently ignored.
- Outcome log correctly pairs each resolved case's decision-time confidence score
  with its actual outcome.

## Failure taxonomy

Add your section to `docs/failure-taxonomy.md`. This module's section should include
at least one concrete, walked-through example: a specific synthetic case where the
confidence score and the actual outcome disagreed, and what that implies. This is
the centerpiece example the differentiator section of the strategy doc calls for —
treat it that way, not as an afterthought.
