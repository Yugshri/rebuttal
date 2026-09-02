# Synthetic data corpus (Track 02 AI Risk Manager)

Regenerate everything deterministically:

    .venv/Scripts/python.exe -m src.synthetic.build

Single seed: **20260902** (defined in `src/synthetic/__init__.py`, nowhere else).

## Artifacts

| Path | What it is |
|---|---|
| `data/external.db` | Read-only "issuer / Razorpay-side" store. `src/common/db.py::external_engine()` opens this `mode=ro`. Tables: `account_nodes`, `transaction_edges`, `payments`, `orders`, `shipments`, `communications`, `addresses`, `customer_return_history`, `evidence_availability`. |
| `data/webhooks/*.json` | 135 synthetic `dispute.created` payloads (Razorpay-shaped) across 129 disputes. Reopen chains emit multiple events for one dispute id. |
| `data/heldout/*.json` | Labelled ground truth. **The pipeline never reads this** — only `qa-evaluator`. |

## Corpus shape

* Accounts (graph nodes): **305**  ·  transaction edges: **3715** (every edge carries a real ISO-8601 timestamp)
* Disputes: **129**  ·  webhook events: **135**  ·  genuine reopen chains: **6**
* Reason-code category coverage: `{'authorization': 24, 'consumer_dispute': 40, 'fraud': 34, 'needs_manual_classification': 11, 'processing_error': 20}`
* Phase coverage: `['arbitration', 'chargeback', 'fraud', 'pre_arbitration', 'retrieval']`
* Graph window: 2026-05-01T00:00:00 .. 2026-07-30T00:00:00 ; mule behavioural shift begins ~day 52; dispute "now" = 2026-09-03T12:00:00

## Evidence incompleteness (deliberate, documented)

| bucket | meaning | fraction |
|---|---|---|
| full | every required evidence slot has a real backing record | 0.581 |
| partial | exactly one non-critical required slot has no record | 0.302 |
| severe | a critical slot (or 2+ slots) has no record | 0.116 |

This is a designed test condition for the "never fabricate, report missing
honestly" path — not an oversight. Rationale: real fraud/dispute feature stores
(IEEE-CIS Fraud Detection) ship with many columns >50% null.

Held-out expected `defer_to_human` fraction: **0.473**

## Planted accounts (ground truth — full rationale in `data/heldout/account_labels.json`)

| account_id | role | pattern |
|---|---|---|
| `ACC_MULE_FANOUT` | planted mule | high-trust inflow for ~7 weeks, then late rapid fan-out to ~25 fringe accounts |
| `ACC_MULE_BRIDGE` | planted mule | operates in one community, then late small irregular transfers bridging two communities (betweenness spike) |
| `ACC_MULE_PASSTHRU` | planted mule | thin history then bursts: receives and forwards ~85-95% onward within hours (velocity + short time-to-forward) |
| `ACC_BURSTY_SEASONAL` | bursty control (legit) | ~5x festival-season volume spike, **same** customers and suppliers — no fringe, no bridge. The false-positive target. |
| `ACC_BURSTY_PAYDAY` | bursty control (legit) | monthly payday out-degree spike to the **same** ~40 recipients |

Returns-abuse cohort: **67** accounts (high `return_rate_pct`,
delivery refusals, multi-account / refund-to-other-account flags).

## Calibration provenance (which real dataset backs which parameter)

| Parameter | Calibrated against | Notes |
|---|---|---|
| Transaction-graph structure (directed, timestamped event log, small illicit minority) | **IBM AML** `ealtman2019/ibm-transactions-for-anti-money-laundering-aml` (opened via croissant metadata: HI-Small ~515K accounts / ~5M txns / 10-97 days 2022, `Timestamp` + hex account ids, laundering ~1 per 981 ≈ 0.1%) | We mirror the *shape* at demo scale. Planted-mule share here (~1% of accounts) is deliberately inflated above IBM's ~0.1% txn rate for visible demo signal. |
| Behavioural-feature families (counting, velocity/time-delta, match/mismatch flags) | **IEEE-CIS Fraud Detection** (Vesta) — `C*` counting, `D*` time-delta, `M1..M9` match families | `evidence_availability` carries `billing_shipping_address_match`, `email_domain_consistent`, `avs_match`; graph edges carry real timestamps for velocity/recency. |
| Deliberate evidence missingness | **IEEE-CIS** (many `D*`/`V*`/`id_*` columns >50% null) | Motivates the full/partial/severe buckets. |
| Dispute amount distribution (right-skewed lognormal, median ~₹1,900, tail to ~₹92K) | **ULB Credit Card Fraud** `mlg-ulb/creditcardfraud` (published: 284,807 txns, mean amount ≈ 88, strong right skew) for tail *shape*; **RBI DBIE** (avg UPI ticket ≈ ₹1,300-1,600 through 2024-25) for scale sanity | ULB is EUR card-present; only the shape transfers, not the absolute scale. |
| India-specific transaction features (night/weekend flags, retry `attempt_count`, `amount_slab`, Razorpay payment-id shape) | **Pattern-Based UPI Transaction Risk Dataset** `kalpitlabs/upi-fraud-detection-dataset-india-synthetic` (opened via croissant: columns incl. `razorpay_payment_id`, `timestamp`, `amount`, `upi_app`, `device_fingerprint`, `ip_address`, `attempt_count`, `is_night_transaction`, `is_weekend`; class balance 55/45) | India-specific dataset that **held up on inspection** — used for feature *vocabulary*, not for fraud base rate (its 45% is synthetic oversampling, not a real rate). |
| COD / returns fields (`return_rate_pct`, `total_orders_lifetime`, `total_returns_lifetime`, `account_age_days`, `customer_segment`, `multiple_accounts_flag`, `refund_to_different_account`, `previous_dispute_count`) | **E-commerce Return Abuse Detection** `sarveshchhetri/e-commerce-return-abuse-detection-dataset` (opened via croissant: 60,000 rows × 35 cols, 0 missing; published class balance Legitimate 70.1% / Policy Abuser 11.9% / Fraudulent Return 10.2% / Wardrobing 7.7%) | Field names mirrored directly. Our account-level abuse cohort (~22%) is below the dataset's ~30% *return-level* rate — deliberate, non-measured adjustment. |
| Reason codes (Visa 10.4, MC 4863, Amex F24, …) and Razorpay Dispute/evidence schema fields | Card-network rulebooks + Razorpay docs (already real, from the research docs) | Not from Kaggle. `src/common/reason_codes.py` is the shared source of truth. |

India-specific datasets checked: `kalpitlabs/upi-fraud-detection-dataset-india-synthetic`
(**used** — feature vocabulary) and `kumarperiya/comprehensive-indian-online-fraud-dataset`
(opened via croissant: ~1,200 rows, columns `transaction_id, customer_id, merchant_id,
amount, transaction_time, is_fraudulent, card_type, location, purchase_category,
customer_age, fraud_type`; **ruled out for calibration** — too small, no published
class balance or distribution stats, no graph/velocity structure). See
`docs/failure-taxonomy.md` for the full mapping-gap discussion.
