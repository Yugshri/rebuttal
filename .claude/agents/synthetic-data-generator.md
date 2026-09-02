---
name: synthetic-data-generator
description: Use this agent for building any synthetic dataset the system runs against — disputes, order/shipping/comms records, the transaction graph, or the labeled held-out evaluation set. Build this FIRST, before any other module — everything downstream depends on this data existing. Use PROACTIVELY at the very start of the build. Do not use for pipeline logic itself (routing, assembly, scoring) — this module only produces data, it never makes pipeline decisions.
tools: Read, Write, Edit, Bash, Grep, Glob
model: sonnet
---

# Module: Synthetic Data Generator

You produce every dataset the rest of the system builds against and is evaluated
against. Build this first — `dispute-ingestion-router` and `risk-graph-service` are
both blocked on you. Quality here determines whether the whole system's demo and
metrics are credible or hollow, so treat "realistic" as a real requirement, not a
formality.

## Scope boundary

You own: all synthetic data generation. You do NOT own any pipeline logic — you
don't classify, assemble, score, or route anything. If you catch yourself writing
decision logic instead of data, that belongs in another module.

## What you're building

**1. Synthetic `DisputeCase` records** — cover all four reason-code categories with
real example codes (reuse `dispute-ingestion-router`'s table once it exists, or
coordinate with the orchestrator on the shared mapping if you're building first):
Fraud (Visa 10.4, Mastercard 4863, Amex F24), Authorization (Visa 11.1, Mastercard
4837), Processing error (Visa 12.2), Consumer dispute (Visa 13.1/13.3, Mastercard
4853/4855, Amex C08/C31). Cover all five `phase` values, including some cases that
deliberately reopen (e.g. `won`/`fraud` → later `under_review`/`pre_arbitration`) so
`dispute-ingestion-router` and `confidence-scorer-review` have real reopen cases to
handle, not just a theoretical one. Vary `amount` and `respond_by` realistically,
including some cases already inside the 48-hour deadline window.

**2. Synthetic order/shipping/comms store** — keyed on `payment_id`, feeding
`evidence-assembler`. Deliberately leave some records incomplete or missing (not
every dispute should have a full evidence trail available) — this is what exercises
the "never fabricate, report missing honestly" path. Document what fraction you're
leaving incomplete and why, so it reads as a designed test condition, not an
oversight.

**3. Synthetic transaction graph** — a few hundred accounts (nodes), transactions as
directed edges, built for `networkx`. This is the highest-value piece to get right:
plant deliberate mule-like behavioral shifts into a subset of accounts — e.g. an
account that starts with edges to high-trust nodes (payroll-like, established
merchant-like accounts) for a simulated early period, then shifts to mostly edges
with low-rank fringe accounts, and/or an account that develops edges bridging two
otherwise-separate clusters via small, irregular transfers late in the simulated
window. Also plant at least one "legitimately bursty but not actually shifting"
account (e.g., simulated seasonal spike) specifically so `risk-graph-service`'s
false-positive behavior has something real to be measured against — this is what
makes the "honest false-positive cost" metric meaningful instead of vacuous.

**4. COD/returns fraud fields** — per-customer return-rate history, price-point/
category on orders, address records including some deliberately mismatched or
high-return-density addresses, and delivery-refusal patterns for a subset of
accounts.

**5. A labeled held-out evaluation set** — separate from the main demo dataset.
Ground truth labels (which disputes should assemble cleanly vs. should defer to
human review, which accounts are the actually-planted mule-like ones vs. the
legitimately-bursty one) must be stored in a location the pipeline itself never
reads from during normal operation — only `qa-evaluator` reads the ground truth, to
score the pipeline's output against it. Leaking ground truth into a field the
pipeline computes from would invalidate every metric downstream.

## Ground this in real data — features, structure, and numbers, not just vibes

Don't generate with bare `random.choice` and call it done. "Realistic" means three
separate things, and all three need a named, checkable source — not "it seemed
reasonable":

**1. Feature families that mirror what real fraud/risk datasets actually compute.**
Real fraud-detection data isn't just raw transaction fields — it's raw fields plus
engineered signal. The canonical reference is IEEE-CIS's Fraud Detection dataset
(Vesta Corporation's real e-commerce fraud data, a well-documented Kaggle
competition): its feature families are counting features (how many accounts/cards
share a device or address), time-delta/velocity features (time since this
account's previous transaction, transactions-per-window), and match/mismatch flags
(does billing address match shipping, does email domain look consistent). PaySim
(Kaggle `ealaxi/paysim1`, a mobile-money simulator built from real transaction
patterns) adds before/after balance fields — whether a transaction's effect on an
account's balance is internally consistent is itself a fraud signal. Mirror these
families, don't just mirror field names:
  - On the order/shipping/comms store (section 2): add explicit match/mismatch
    flags — billing-vs-shipping address match, email-domain consistency — since a
    chargeback evidence system built for real would use exactly these to assess
    evidence strength, and it's a cheap, high-value addition.
  - On the transaction graph (section 3): every edge needs a real timestamp, not
    just "early" or "late in the window" — see below.

**2. Genuine temporal structure, not a two-snapshot graph.** The transaction graph
must be a real timestamped sequence per account (think PaySim's `step` or IBM
AML's `Timestamp` — both are literally time-ordered event logs, not static
snapshots), so `risk-graph-service` can derive real velocity/recency features
(time-since-last-transaction, transaction count in rolling windows, first-time-seen
counterparty) rather than working off two hand-placed states. The mule-like
behavioral shift (section 3) should emerge from a real sequence of timestamped
transactions moving from high-trust to fringe counterparties over simulated time,
not be declared as a before/after label — that sequence is what makes
`baseline_deviation` a real computation instead of a lookup.

**3. Numbers calibrated against named public datasets, India-specific first.**
Before falling back to a global reference, check what's available for India
specifically — there are several India/UPI-focused Kaggle datasets worth
inspecting directly (open them yourself, don't take a title at face value):
`kalpitlabs/upi-fraud-detection-dataset-india-synthetic` ("Pattern-Based UPI
Transaction Risk Dataset") and `kumarperiya/comprehensive-indian-online-fraud-
dataset` look like the closest conceptual matches by name and are worth the first
look; `devildyno/upi-payment-transactions-dataset`, `iamravi11/fraud-upi-
transaction-details`, `bijitda/upi-transactions-dataset`, and `skullagos5246/upi-
transactions-2024-dataset` are other India/UPI-specific candidates to scan. None of
these were fully vetted upstream (Kaggle's page rendering blocks pulling their
actual column lists remotely) — actually open them, check the column list and
methodology, and use whichever holds up. For macro-level numbers (overall digital
payment volumes, growth trends) rather than row-level transaction data, RBI's own
Database on Indian Economy (`data.rbi.org.in/DBIE`) is the authoritative public
source — real, official, not row-level, but good for sanity-checking scale.

Where no India-specific dataset holds up for a given parameter, fall back to global
references and say so explicitly: IBM's AML transaction dataset (Kaggle
`ealtman2019/ibm-transactions-for-anti-money-laundering-aml`, generated by IBM's
AMLSim multi-agent simulator — realistic timestamped account/transaction structure
with laundering labels, the closest match to what `risk-graph-service`'s technique
targets) for graph/velocity calibration; ULB's Credit Card Fraud dataset (Kaggle
`mlg-ulb/creditcardfraud` — real anonymized card transactions, published fraud rate
~0.17%, well-cited) for fraud-rate and amount-distribution calibration; e-commerce
returns-abuse datasets (Kaggle `sarveshchhetri/e-commerce-return-abuse-detection-
dataset`, `shriyashjagtap/fraudulent-e-commerce-transactions`) for the COD/returns
fields in section 4. If Kaggle API credentials aren't set up and downloading would
eat into the schedule, use each dataset's own published summary statistics (stated
on its Kaggle page or an accompanying paper) as the calibration target instead of
downloading the raw file — either way, cite which dataset backs which parameter in
code comments, so the number is checkable, not asserted.

Card network reason codes (Visa 10.4, Mastercard 4863, etc.) and Razorpay's schema
fields are already real — they came from the actual dispute/evidence schema and
network rulebooks in the research docs, not from Kaggle. What needs grounding is
what you're inventing on top of that real structure: amounts, timing, account
behavior, graph topology, return rates, and the engineered features above.

Write a short comment or docstring next to each generator naming which real dataset
or published statistic it's calibrated against. A reviewer — or a judge — asking
"why does your synthetic data look like this" should get a real, checkable answer,
not "it seemed reasonable."

## Determinism

Everything must be reproducible from a fixed random seed. `qa-evaluator`'s numbers
need to be stable across runs for the metrics to mean anything when reported in the
pitch.

## Definition of done

- All four reason-code categories and all five phases represented, including at
  least one genuine reopen case.
- Order/shipping/comms store has a documented, deliberate incompleteness rate.
- Transaction graph has at least 2-3 planted mule-like accounts with a real,
  time-shifted behavioral pattern, plus at least 1 legitimately-bursty control
  account.
- Every transaction/edge carries a real timestamp (not a coarse early/late label),
  so velocity/recency features are actually derivable from the sequence, not
  hand-declared.
- Order/shipping/comms records carry the match/mismatch flags (billing-vs-shipping,
  email-domain consistency) described above, not just the raw evidence fields.
- Held-out set exists separately with ground truth inaccessible to the normal
  pipeline path.
- Fixed seed produces identical output across runs (test this directly).
- At least one India-specific dataset was actually opened and checked (not just
  found) before being used or ruled out — the code comments say which one backs
  which parameter, or say explicitly that none held up and a global reference was
  used instead.

## Failure taxonomy

Add your section to `docs/failure-taxonomy.md`: name exactly which real dataset (or
published statistic) calibrated which parameter — fraud rate, amount distribution,
graph degree distribution, return rate — and name the mapping gaps honestly (e.g.
IBM's AML dataset is bank-transfer-shaped, not card-dispute-shaped; ULB's published
stats are card-present, not India card-not-present e-commerce; if no India-specific
dataset held up on inspection, say which ones you opened and why they didn't fit,
not just that you used a global fallback). Then state plainly what real-world
patterns this synthetic data still doesn't capture even with that calibration — it
can't, it's synthetic. Naming this explicitly is the "explicit demo-scale
simplification" trade-off the system design doc already calls for — better to say
it yourself than have a judge find the gap first.
