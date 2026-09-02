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
