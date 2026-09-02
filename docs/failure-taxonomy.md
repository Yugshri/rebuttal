# Failure Taxonomy

This is the primary deliverable for this submission, not a postscript. It records —
precisely, with test backing — where this system is uncertain, where it defers to a
human, and what it got wrong on the held-out set.

Structure: one section per module (each module's owning subagent appends its own,
never overwriting another's), then a system-level section owned by `qa-evaluator`
with the measured false-positive cost and at least one fully walked-through case
where the system's recommendation and the actual/expected outcome disagreed.

> Scope note: `compliance-knowledge-graph` provides decision-support grounding and
> explainability. It is **not** a legal compliance certification and does not
> replace legal/compliance review of a real deployment.

---

## Module: synthetic-data-generator

_(pending — appended by the synthetic-data-generator subagent)_

## Module: compliance-knowledge-graph

_(pending — appended by the compliance-knowledge-graph subagent)_

## Module: dispute-ingestion-router

_(pending — appended by the dispute-ingestion-router subagent)_

## Module: risk-graph-service

_(pending — appended by the risk-graph-service subagent)_

## Module: evidence-assembler

_(pending — appended by the evidence-assembler subagent)_

## Module: confidence-scorer-review

_(pending — appended by the confidence-scorer-review subagent)_

## Module: qa-evaluator

_(pending — appended by the qa-evaluator subagent)_

---

## System-level

_(pending — owned by qa-evaluator: measured false-positive cost, integration-test
failures found and how they were resolved, and the centerpiece walked-through
disagreement example)_
