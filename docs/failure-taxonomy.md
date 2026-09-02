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

### Explicit non-claim (read before trusting anything this module emits)

This module is **decision-support grounding and explainability** — nothing more. It
attaches the name of the regulatory framework that bears on a decision another
module already made. It does **not** certify legal compliance, and it does not
replace legal/compliance review of a real deployment.

- Accurate claim: *"This system's recommendations cite the specific regulatory
  frameworks that apply to the decision."*
- Overclaim we do **not** make anywhere — not in code, not in API output, not here:
  *"This system is RBI-compliant / DPDP-compliant."*

Every `RequirementMatch` carries a `disclaimer` string saying this, so a caller
that embeds a citation in an API response ships the caveat with it.

### Coverage gap (deliberate, and worth stating plainly to a judge)

Five regulations and eleven requirement nodes is a hand-picked slice of a large
regulatory surface, chosen for what is load-bearing for a decision *this demo*
makes. What a real deployment would still need, not in this graph:

- **The rest of the RBI corpus.** RBI issues dozens of relevant circulars and FAQs
  (KYC Master Direction, storage-of-payment-data circular and its FAQs, tokenisation
  framework, digital-lending directions, the failed-transaction TAT circular in its
  own right). We cite the PA-PG Master Direction at regulation level and gesture at
  turnaround-time expectations; we do not encode the actual TAT table.
- **State-level and sectoral consumer law**, grievance-redressal specifics under the
  Consumer Protection Act itself, and the RBI Integrated Ombudsman Scheme.
- **Verified pinpoint citations.** Only PSS Act s. 4, DPDP Act ss. 6(1) and 8(7),
  and E-Commerce Rules rr. 4–6 are pinned to a section/rule, each checked against a
  primary source (indiacode.nic.in, meity.gov.in, Department of Consumer Affairs).
  The PA-PG Master Direction and DPIP are cited at regulation/initiative level on
  purpose — inventing a paragraph number we had not verified is the exact failure
  mode the spec forbids, so the graph structurally cannot express one for those two.
- **Currency.** These summaries are a point-in-time paraphrase (PA-PG Master
  Direction consolidated 15 Sep 2025; DPDP Rules still in draft as of this build).
  Nothing in this module re-checks them.

### Where this module is uncertain / defers

- It does not decide anything. If `lookup()` returns `[]` for an entity, that means
  "no curated grounding for this string", **not** "no regulation applies" — the
  calling module must not read absence as a green light.
- Exact-match only. A caller passing a slightly-off entity string (`"frauds"`,
  `"respond-by"`) silently gets `[]`. Mitigated by keying every edge on the shared
  constants in `src/common/reason_codes.py` and a fail-fast vocabulary check at
  import, but a typo on the caller's side still degrades to "no citation" rather
  than an error.

### Named upgrade paths

1. **Card-network rules as a fourth node type.** Visa Core Rules / VCR, Mastercard
   Chargeback Guide and Amex dispute rules are already implicitly encoded in
   `dispute-ingestion-router`'s reason-code table but are not linked into this
   graph. They are the natural next node type — `governs` edges from a network
   rulebook to the same reason-code-category and evidence-slot entities.
2. **RAG over verified primary-source text.** Replace hand-curated summary strings
   with retrieval over the actual circular / Act / rules text from official sources,
   the same retrieval pattern this system already assumes elsewhere for evidence and
   classification. This module is where that pattern extends next — an augmentation
   of the node contents, not a rebuild of the graph or the `lookup()` interface.

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
