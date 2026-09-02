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

Owner: `synthetic-data-generator`. Everything below is about the *data*, not the
pipeline — where the synthetic corpus is calibrated, where the calibration is a
stretch, and what real-world structure it still cannot contain.

### Which real dataset (or published statistic) calibrates which parameter

| Parameter | Calibrated against | How the number was obtained |
|---|---|---|
| Transaction-graph structure — directed, timestamped event log, small illicit minority, currency amounts, hex-ish account ids | **IBM AML** `ealtman2019/ibm-transactions-for-anti-money-laundering-aml` | Opened via the dataset's croissant metadata endpoint (Kaggle blocks remote HTML rendering; the croissant JSON is reachable). HI-Small: ~515K accounts, ~5M transactions, 10–97 days in 2022, columns `Timestamp / From Bank / Account / To Bank / Account / Amount Received / Receiving Currency / Amount Paid / Payment Currency / Payment Format / Is Laundering`, laundering rate ~1 per 981 (≈0.10%). We mirror the *shape* at demo scale (305 accounts, ~3.7K edges, 90 days). |
| Planted-mule prevalence | IBM AML laundering-txn rate (~0.10%) | Deliberately **inflated** to ~1% of accounts (3 planted mules / 305). See mapping gaps. |
| Behavioural-feature families — counting, velocity/time-delta, match/mismatch | **IEEE-CIS Fraud Detection** (Vesta) `C*` counting, `D*` time-delta, `M1..M9` match | From the competition's well-documented data dictionary (prior knowledge, not re-fetched). Drove: every graph edge carries a real per-second timestamp (velocity/recency derivable); `evidence_availability` carries `billing_shipping_address_match`, `email_domain_consistent`, `avs_match`. |
| Deliberate evidence missingness (full 58% / partial 30% / severe 12%) | **IEEE-CIS** (many `D*`/`V*`/`id_*` columns >50% null) | Motivation only — the exact bucket split is a design choice to exercise the "report missing honestly" path, not a measured rate. |
| Dispute amount distribution — lognormal, median ≈ ₹1,900, tail to ₹92K | **ULB Credit Card Fraud** `mlg-ulb/creditcardfraud` for tail *shape*; **RBI DBIE** for scale sanity | ULB published stats (prior knowledge): 284,807 transactions, 492 fraud (0.172%), `Amount` mean ≈ 88, heavy right skew. RBI Database on Indian Economy: average UPI ticket ≈ ₹1,300–1,600 through 2024–25. |
| India-specific transaction feature vocabulary — night/weekend flags, retry `attempt_count`, `amount_slab`, `razorpay_payment_id` shape, `device_fingerprint`, `ip_address` | **Pattern-Based UPI Transaction Risk Dataset** `kalpitlabs/upi-fraud-detection-dataset-india-synthetic` | **Opened and checked** via croissant metadata. 20 columns incl. `id, razorpay_payment_id, timestamp, agent_type, amount, currency (INR only), payment_method (UPI), upi_app, bank, status, error_code, device_fingerprint, ip_address, fraud_score, is_suspicious, fraud_reasons, amount_slab, hour_of_day, is_night_transaction, is_weekend, attempt_count`. Class balance 55% normal / 45% risk-flagged (11% each: impatient / bot / fraud / dormant). Used for feature vocabulary and the payment-row shape — **not** for a fraud base rate. |
| COD / returns fields — `return_rate_pct`, `total_orders_lifetime`, `total_returns_lifetime`, `account_age_days`, `customer_segment`, `multiple_accounts_flag`, `refund_to_different_account`, `previous_dispute_count`, `delivery_refusals`, `high_return_density` addresses | **E-commerce Return Abuse Detection** `sarveshchhetri/e-commerce-return-abuse-detection-dataset` | **Opened and checked** via croissant metadata. 60,000 rows × 35 columns, 0 missing. Published class balance: Legitimate 70.1% / Policy Abuser 11.9% / Fraudulent Return 10.2% / Wardrobing 7.7%. Field names mirrored directly; return-rate centres (legit ≈ 9%, abuse cohort ≈ 42%) chosen from this plus cited India e-commerce return rates (~8–12%). |
| Reason codes + Razorpay Dispute/evidence schema | Card-network rulebooks + Razorpay docs (already real, via research docs) | Not Kaggle. `src/common/reason_codes.py` is the shared source of truth; the generator only *uses* it. |

### India-specific datasets: opened and checked

- `kalpitlabs/upi-fraud-detection-dataset-india-synthetic` — **opened via croissant metadata, held up, used** for feature vocabulary (see table). Not used for fraud rate: its 45% positive rate is synthetic rule-flag oversampling, not a real base rate.
- `kumarperiya/comprehensive-indian-online-fraud-dataset` — **opened via croissant metadata, ruled out for calibration.** Only ~1,200 rows; columns `transaction_id, customer_id, merchant_id, amount, transaction_time, is_fraudulent, card_type, location, purchase_category, customer_age, fraud_type`; no published class balance, no distribution statistics, no account-linkage or velocity structure. Too thin to calibrate anything a global dataset doesn't already cover better.
- Other India/UPI candidates named in the spec (`devildyno`, `iamravi11`, `bijitda`, `skullagos5246`) — not individually opened once the two closest-match datasets above resolved the India-specific need (feature vocabulary from one, and a documented "ruled out" from the other). Flagged here as an incomplete sweep rather than claimed as vetted.

### Mapping gaps (named honestly)

- **IBM AML is bank-transfer-shaped, not card-dispute-shaped.** Its edges are wire/ACH/cheque between bank accounts; our disputes are card-not-present e-commerce chargebacks. We borrowed the *graph topology and timestamp discipline*, not the transaction semantics.
- **ULB's published stats are EUR card-present, not India card-not-present e-commerce.** Only the amount-distribution *shape* (heavy right skew, most transactions small) transfers; the absolute rupee scale comes from RBI macro figures, not from ULB.
- **Planted-mule prevalence (~1%) is ~10x IBM AML's ~0.1%.** Deliberate: a held-out set with 0.1% positives gives `risk-graph-service` almost nothing to be scored on at demo scale. This inflates recall-at-fixed-precision relative to a real deployment — `qa-evaluator` should state the planted prevalence next to any risk metric.
- **The returns-abuse cohort (~22% of accounts) is a designer number.** The source dataset's ~30% is a *return-level* rate (share of flagged returns), not an *account-level* rate. We adjusted downward on the assumption that abusers are a smaller share of account holders than of suspicious returns — this assumption is not backed by a public figure.
- **`needs_manual_classification` share (~8.5%) is not calibrated to anything.** It is a plausible "codes our table doesn't map yet" rate for a young system. The codes themselves are real (Visa 13.7 / 10.1, MC 4841 / 4846, Amex C14, RuPay 121, Discover 4752).
- **Held-out `defer_to_human` fraction ≈ 0.47.** Higher than a mature dispute team would defer. Driven by treating every pre-arbitration/arbitration case, every reopen, every severe-incompleteness case, and every high-amount mule-linked case as a defer. Deliberately conservative so the "where we chose not to use AI" boundary is well-exercised; it is not a calibrated production defer rate.
- **India-dataset sweep is incomplete** (see above) — four named candidates were not opened.

### What this synthetic data still does NOT capture (it can't — it's synthetic)

- **Real adversarial adaptation.** Planted mules follow three fixed archetypes (late fan-out, cluster bridge, rapid pass-through). Real mule networks co-adapt to detection, blend archetypes, age accounts deliberately, and stay under thresholds that were tuned on last quarter's data.
- **Realistic evidence *content*.** `communications.summary` is drawn from six templates; `explanation_letter` is generated downstream. A real evidence bundle's persuasiveness depends on specifics (does the tracking event actually place the parcel at the billing address, does the chat transcript contain an admission) that a synthetic corpus cannot fake convincingly.
- **True outcome labels.** There is no real issuer deciding won/lost. `dispute_dispositions.json` encodes *what the pipeline should do*, which is not the same as *what would actually win the dispute*. Any "win rate" `qa-evaluator` reports is against designer intent, not a card network.
- **Population drift and seasonality beyond the two planted bursty controls.** The background graph is stationary apart from what we planted; a real transaction graph drifts continuously (festival cycles, merchant onboarding waves, UPI feature launches).
- **Correlated missingness.** Our incompleteness buckets are assigned by a permutation (uniform-at-random over the corpus). Real missing evidence correlates with dispute type, merchant maturity, and how long ago the order was — the cases where evidence is missing are exactly the cases that are hard.
- **PII / DPDP-realistic data.** Names, emails, addresses are obviously fake by construction. Any data-minimisation or consent behaviour downstream is tested against a toy shape, not a realistic one.

This explicit naming is the "demo-scale simplification" trade-off the system
design doc calls for — better stated here than found by a judge.

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

Owner: `dispute-ingestion-router`. Scope: webhook ingestion, the `DisputeCase`
model/table, reason-code classification, the category→required-evidence mapping
exposure, phase-transition tracking. Not evidence content, risk, or scoring.

### What actually happens on an unrecognised reason code

The classifier (`src/ingestion/classification.py`) is a pure view over
`src/common/reason_codes.py`. On a `(network, code)` pair with no entry in
`REASON_CODE_TO_CATEGORY` it returns `category = "needs_manual_classification"`
with `needs_manual_classification = True` and a `reason` string that names the
code and states it was "routed to manual classification rather than guessed".
There is **no nearest-neighbour / fuzzy fallback** — this is deliberate. A real
network code we don't map yet (Visa 13.7 / 10.1, MC 4841 / 4846, Amex C14, RuPay
121, Discover 4752 in the corpus) is a known, expected gap, not an error.

Concrete consequences and residual risk:

- **The dispute is still ingested and tracked.** A `DisputeCase` row is written
  with `category = "needs_manual_classification"`, full phase history, and a
  correct `respond_by` — so the deadline clock is not lost while a human
  classifies it. It just carries no `required_slots` / `evidence_types`, so
  `evidence-assembler` has nothing to assemble against and must treat it as
  defer-to-human. That hand-off contract is asserted here but *enforced*
  downstream; if a future scorer forgets to check the flag, a manual-bucket case
  could silently get a low-information recommendation. Flagged for `qa-evaluator`.
- **Missing `network` is folded into the same bucket** with a distinct `reason`
  ("missing network or reason_code … cannot look up a category"). A caller that
  cannot tell "unmapped code" from "malformed payload" apart without reading the
  `reason` string — the machine-readable signal is the same boolean for both.
- **A code could be *wrong* rather than *unmapped*** (issuer sends `4863` on a
  Visa dispute). We would classify it as whatever `visa:4863` maps to — here,
  nothing, so manual — but a collision with a real Visa code would be
  mis-categorised with no signal. We do not validate code-belongs-to-network.
- ~8.5% of the synthetic corpus (11 / 129) hits this bucket by construction; that
  fraction is not calibrated to any real system's mapping-coverage rate.

### Confidence of the phase-transition logic on out-of-order delivery

Razorpay's webhook docs do not guarantee ordered delivery. The handler
(`src/ingestion/service.py`) is built around **monotonic phase rank** as the
single source of truth for "where is this dispute now", using
`phase_rank()` / `PHASE_ORDER` from `models_base`:

- **Redelivery (same `event_id`, or same phase+status+`respond_by`)** → true
  no-op. No history row, no `event_count` bump, `DisputeCase` untouched. Solid —
  covered by tests replaying the entire 135-event corpus twice and asserting 129
  rows.
- **Genuine forward advance (`incoming_rank > current_rank`)** → update in place,
  append a `phase_advance` history row, and if the prior status was terminal
  (`won`/`lost`/`closed`) mark `is_reopen` and bump `reopen_count`. Confident —
  the 6 real reopen chains plus a synthetic `open→won→pre_arbitration` chain are
  tested end to end, and the reopen stays queryable via `get_phase_history()` /
  `was_reopened()`.
- **Out-of-order / stale (`incoming_rank < current_rank`, or an `event_created_at`
  older than the newest one already recorded)** → recorded as an `out_of_order`
  history row and **otherwise ignored**. The case keeps its furthest-advanced
  phase and status; it is never rolled back. This is the safe direction to be
  wrong in, but it has real limits:

  **Where this logic is NOT confident:**

  1. **A late event carrying a genuine field correction is dropped.** If the
     `chargeback` event was delayed and actually contains the corrected
     `respond_by` or `amount`, we ignore it because a later-phase event already
     landed. We keep the newer-phase event's values, which are usually right, but
     not guaranteed. No reconciliation pass.
  2. **Same-rank reordering is decided purely by timestamp.** Two events at the
     same phase with a status change (`under_review` → `won`) arriving reversed:
     we apply whichever has the newer `event_created_at`. If timestamps are equal
     or missing, last-write-wins by arrival order — a coin flip.
  3. **First-contact at a late phase looks normal.** If the very first webhook we
     ever see for a dispute is its `arbitration` event (earlier ones lost, never
     delivered), we create the case at `arbitration` with a one-row history and
     **no reopen flag** — the escalation trail before that point is simply gone,
     and nothing signals that it is missing.
  4. **`phase_rank` collapses distinct clocks.** Each phase has its own deadline;
     we track the current `respond_by` but not per-phase SLA history beyond what
     is frozen in each history row. A dispute that skips a phase entirely (rank
     jump of 2+) is treated as a normal advance with no note that a phase was
     skipped.
  5. **Wall-clock `recorded_at` is `int(time.time())` at ingest**, so history
     ordering within a burst relies on the autoincrement `seq`, not on time.
     Fine for a single-process demo; a concurrent deployment would need the DB to
     serialise the read-modify-write on a dispute id (currently no row lock).

## Module: risk-graph-service

Owner: `risk-graph-service`. This module produces `AccountRiskProfile` (graph
signals + COD/returns signals) as **enrichment** for `confidence-scorer-review`.
It does not classify disputes, assemble evidence, or make the routing decision.

### What it computes, and the window scheme

`baseline_deviation` is a real change-over-time measure, not a cross-account
percentile at a single instant:

- The timestamped edge log (`transaction_edges`, ~3,715 edges, 2026-05-01 →
  2026-07-29) is sliced into **14-day windows advanced by 7 days** → 13
  overlapping windows. Overlap gives each account a smoother centrality series to
  build a baseline from at demo scale, where any single window is sparse.
- Per window: weighted PageRank (numpy power iteration — networkx 3.x delegates
  PageRank to SciPy, which is not a project dependency) and **undirected**
  betweenness centrality.
- Per account: its first ~40% of active windows are the "establishment
  baseline". Every later window is scored for how far its centrality has moved
  from that baseline (MAD-based robust z, capped at 15 for betweenness / 8 for
  PageRank), **gated by** `illicit_counterparty_fraction` — the share of that
  window's counterparties that are thin-file `fringe` accounts or sit in a
  different `cluster` than the account was established in.
- Thin-history accounts (≤1 active window before the timeline midpoint) have no
  establishment baseline, so they get a separate **emergence** term: late
  appearance as a high-betweenness, high-throughput pass-through hub.
- Velocity/recency features (recent-window txn count, rolling velocity,
  days-since-last-txn, first-time-counterparty rate, fan-out ratio) are stored on
  the same row.

Measured on `data/external.db` (`HIGH` band ≥ 8.0, `ELEVATED` ≥ 4.0):

| account | role | `baseline_deviation` | band | driver |
|---|---|---|---|---|
| `ACC_MULE_FANOUT` | planted mule | **22.6** | high | betweenness z capped (15), 100% new fringe counterparties, illicit frac 0.93 |
| `ACC_MULE_PASSTHRU` | planted mule | **20.5** | high | emergence term — no pre-midpoint history, late betweenness 0.23 vs floor 0.0026, illicit frac 0.76 |
| `ACC_MULE_BRIDGE` | planted mule | **14.3** | high | betweenness z capped (15) in late windows vs a near-zero market-A-only baseline, 100% counterparty turnover, illicit frac 0.71 (market-B bridge) |
| `ACC_BURSTY_SEASONAL` | legit control | **2.25** | low | betweenness z is also capped at 15 (festival hub), but `illicit_counterparty_fraction == 0.00` zeroes the gate — same market-A regulars and suppliers throughout |
| `ACC_BURSTY_PAYDAY` | legit control | **0.00** | low | betweenness is *high but flat* (~0.11 every active month), so z ≈ 0; same 40 recipients, zero turnover |

All three planted mules land in `high`; both bursty controls land in `low`, well
below every mule. Truncating the edge log to the pre-shift period
(`test_baseline_deviation_needs_the_post_shift_history`) collapses
`ACC_MULE_FANOUT`/`ACC_MULE_BRIDGE` back under the `elevated` threshold — the
signal is genuinely temporal.

### The honest false-positive source (this is the point, not a flaw to hide)

**A legitimate account with a real, large, structural behavioural change is
structurally indistinguishable from a shifting mule.** The detector keys on
exactly the thing a genuine pivot also produces: a betweenness/PageRank move
away from an established baseline, toward counterparties the account hasn't used
before. A new merchant onboarding a different supplier network, a consumer who
starts shopping across a city/market boundary, a small business pivoting
product lines — all of these look like `ACC_MULE_BRIDGE` to this module.

What we actually observed on the held-in synthetic graph, at the `high` band
(deviation ≥ 8.0), 10 accounts total:

- **3 planted mules** — true positives.
- **`ACC_FRINGE_017` / `_035` / `_036` / `_037`** (dev 9–18) — these are the
  fan-out *recipients* of `ACC_MULE_FANOUT`. Arguably true positives (they are
  receiving mule proceeds), but the module has no ground truth saying so; it is
  flagging them for the same structural reason (sudden inflow from a single new
  source, thin prior history). A human reviewer would want to see them anyway.
- **`ACC_CONS_119` / `_163` / `_173`** (dev 8.3–10.1) — **genuine false
  positives.** Ordinary market-A consumers whose synthetic merchant payments
  happen to cross into market-B merchants. Because the `cluster` label is coarse,
  "counterparty in a different cluster" fires, and their sparse per-window
  history (4 inbound payroll credits, ~8–12 outbound) makes a single active
  window look like 50–100% counterparty turnover. Cost: each is a review-queue
  item that a human closes in seconds — the false-positive *cost* here is
  analyst time, not a frozen account (the defense-only boundary guarantees the
  module cannot act), but at production scale this class would dominate the
  queue.

The `illicit_counterparty_fraction` gate is what keeps `ACC_BURSTY_SEASONAL`
(betweenness z = 15, same as the mules) from being flagged — but that gate is
only as good as the `cluster` / `account_type` metadata behind it. Which leads
to the known weaknesses:

### Known weaknesses / where this module is uncertain

1. **`cluster` is a given label, not graph-derived.** In production the
   community assignment should come from running community detection (Louvain /
   label propagation) on the graph itself and refreshing it, so "cross-community
   bridge" is measured, not read from a column. We tried Louvain on the full
   graph (`networkx.community.louvain_communities`): at this scale it split the
   232 consumers into 5 communities that did **not** line up with the
   market-A/market-B structure, making the cross-community signal noisier than
   the synthetic `cluster` label. So this build uses `cluster`. Named here rather
   than hidden: the detector leans on a piece of metadata a real deployment
   would have to earn.
2. **The `HIGH_DEVIATION_THRESHOLD = 8.0` is fitted to this synthetic graph.**
   It sits in the empirical gap between the bursty controls (≤ 2.25) and the
   mules (≥ 14.3). On real data this gap will not be this clean and the
   threshold would need recalibration against a labelled sample — and the
   band is advisory only; `confidence-scorer-review` owns the actual routing
   cut.
3. **`baseline_deviation` is unbounded-ish and then capped.** Betweenness z is
   clipped at 15, so `ACC_MULE_FANOUT` and `ACC_MULE_BRIDGE` both report a
   capped component — beyond the cap, "how big" stops mattering and only the
   illicit-context gate separates them. A more principled transform (rank-based,
   or IQR/Tukey outlier score) would avoid the cap; deferred for the deadline.
4. **`ACC_MULE_PASSTHRU` is caught by the emergence heuristic, not by the
   PageRank/betweenness-vs-baseline path** — its betweenness z is only 2.5
   because by the time it has 3 active windows the pass-through behaviour is
   already its "normal". The emergence term (`btw / global_floor`, capped at 20,
   gated by illicit fraction) is a second, coarser detector bolted alongside the
   primary one; it would fire on any legitimate account that genuinely starts
   life late in the observation window as a hub (a newly-onboarded payment
   aggregator, say).
5. **Planted-mule prevalence (~1% of accounts) is ~10× a real AML base rate**
   (inherited from `synthetic-data-generator`'s calibration note). Any
   precision/recall `qa-evaluator` reports for this module should be read next to
   that inflation — the clean 3-of-3 / 0-of-2 separation here is partly a
   property of a demo-scale graph with loud planted signal.
6. **Batch is whole-graph and single-threaded.** `run_nightly_batch()` recomputes
   every account from scratch (~a few seconds for 305 accounts / 3.7K edges).
   That is fine at demo scale and deliberately structured as a schedulable
   callable with no daemon, but it does not do incremental / windowed
   re-computation — a real graph would need that.
7. **`days_since_last_txn` scans the full edge list per account** (O(accounts ×
   edges)). Acceptable at this scale; would need an index/pre-aggregation
   otherwise.

### COD / returns signal

`returns_risk_score` (0–1) is a weighted composite of `return_rate_pct`,
lifetime return ratio, `delivery_refusals`, `previous_dispute_count`,
`multiple_accounts_flag`, `refund_to_different_account`, and whether any of the
account's addresses is a `high_return_density` hotspot. It is a **signal, not a
decision** — banded low/elevated/high for the scorer to weigh. Weakness: the
weights (0.45 on return rate, etc.) are hand-set, not fitted to outcomes, and
the synthetic returns-abuse cohort (~22% of accounts) is itself a designer
number (see `synthetic-data-generator`'s note). The score reliably ranks the
planted abuse cohort above the median account, which is all it claims to do.

### Defense-only boundary

This module reads `transaction_edges`, `account_nodes`, `customer_return_history`
and `addresses` through `read_only_session()` (driver-level read-only) and writes
**only** `account_risk_profile` in the system store through `system_session()`.
`GET /accounts/{id}/risk-profile` calls `src.risk.service.get_risk_profile` and
nothing else — it holds no reference to `src.risk.batch` or `src.risk.graph`
(asserted two ways in `test_risk_graph_service.py`: a runtime test that patches
every graph-compute function to raise and still gets a 200, and a static test
that inspects the module namespaces). A lookup for an unknown account is a 404,
never an on-request graph build.

### DPIP framing (cited, not asserted)

The profile shape — per-account, with named graph signals and the explicit
baseline each was measured against — is deliberately structured so it could feed
or be enriched from a shared mule-intelligence layer. This aligns with RBI's
Digital Payments Intelligence Platform direction; `compliance-knowledge-graph`
node `dpip_shared_intelligence_alignment` (RBI DPIP, cited at initiative level)
is attached to every API response as `regulatory_grounding` rather than left as
an uncited code comment.

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
