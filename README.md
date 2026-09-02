# Track 02 — AI Risk Manager

**Chargeback evidence responder + graph-based counterparty risk enrichment.**
Razorpay AI Buildathon 2026. Full design rationale and the module contracts are in
[`CLAUDE.md`](CLAUDE.md); the honest write-up of where the system is wrong is in
[`docs/failure-taxonomy.md`](docs/failure-taxonomy.md).

## What it does

A Razorpay dispute webhook comes in. The system classifies it by network reason
code, assembles the evidence bundle from order/shipping/comms records (reporting
missing evidence honestly — it never fabricates a slot), enriches it with a
graph-based counterparty risk signal, scores a transparent confidence number, and
routes the case to either a **draft-for-submit** queue or a **priority-sorted
human review** queue. Every recommendation carries the India regulatory provisions
it is grounded in (RBI PA-PG Master Direction, DPDP Act, Consumer Protection
E-Commerce Rules).

Nothing auto-submits. Nothing can move money, freeze an account, or contact a
customer — see "Defense-only" below.

## Quick start

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt      # Windows
# source .venv/bin/activate && pip install -r requirements.txt   # POSIX
```

**1. Build the synthetic data** (deterministic, fixed seed — regenerable any time):

```bash
python -m src.synthetic.build
```

Writes `data/external.db` (the read-only payment/order/shipping/comms store plus
the transaction graph), `data/webhooks/*.json` (135 dispute events), and
`data/heldout/` (ground-truth labels the pipeline never reads).

**2. Run the tests:**

```bash
python -m pytest -q
```

135 tests. The QA suite prints a precision / recall / false-positive-cost report.

**3. Run the API:**

```bash
uvicorn src.main:app --reload
```

Startup creates the system-store tables and warms the risk profiles (one graph
batch pass). Then:

| Method + path | Purpose |
|---|---|
| `POST /webhook/dispute-created` | Ingest a Razorpay dispute webhook |
| `POST /pipeline/run/{dispute_id}` | Assemble → risk-enrich → score → route one ingested dispute |
| `GET /disputes/{dispute_id}/recommendation` | The recommendation, with regulatory citations |
| `POST /disputes/{dispute_id}/review` | Record a human decision — the **only** path that dispatches |
| `GET /accounts/{account_id}/risk-profile` | Latest precomputed counterparty risk profile |

Example:

```bash
curl -X POST localhost:8000/webhook/dispute-created \
  -H 'content-type: application/json' -d @data/webhooks/disp_0010__00__chargeback.json
curl -X POST localhost:8000/pipeline/run/disp_0010
```

## Batch jobs (schedulable; no live cron needed for the demo)

```bash
python -m src.risk.batch        # recompute AccountRiskProfile for every account
python -m src.scoring.deadline  # flag unresolved disputes within 48h of respond_by
python -m src.qa.harness        # regenerate docs/evaluation-report.md
```

## Defense-only, structurally enforced

Not a policy — a capability boundary:

- **Two databases.** `external.db` (payments, orders, shipping, comms, the
  transaction graph) is opened **read-only** (`mode=ro` + `PRAGMA query_only`); any
  write raises `OperationalError` at the driver. `system.db` (the system's own
  recommendation / review / audit tables) is the only thing it can write.
- **`RecommendedAction` has no submitted / dispatched member.** Only
  `src/scoring/api.py` (`POST /disputes/{id}/review`) writes the dispatch
  transition, and it requires a human decision record.
- **The only outbound HTTP client in the codebase is `src/common/llm.py`**, and it
  talks only to Sarvam, only to draft the `explanation_letter` narrative from
  evidence already assembled (deterministic template fallback when no key is set).

`tests/qa/test_defense_only_boundary.py` asserts all of this against the real code.

## Honest metrics (synthetic held-out set, 129 disputes / 305 accounts)

| Task | Result |
|---|---|
| Reason-code classification | P/R 1.00 — a deterministic view over the shared code table; confirms consistency, **not** real-world coverage |
| Risk flagging (3 planted mules = positives) | P 0.30 / R 1.00 — all 3 mules flagged HIGH, both legitimately-bursty controls correctly not; 7 structural false positives |
| Routing vs. held-out disposition | P 0.94 / R 0.87 / F1 0.90; 0.90 agreement |
| False-positive cost | ~9 legit cases pushed to needless human review ≈ 1.8 analyst-hours ≈ ₹1,080 per corpus; no case risks funds / contact / auto-submit |

Every synthetic number is calibrated against a named public dataset — see
[`data/README.md`](data/README.md). Where the system is uncertain, defers, or is
wrong is documented per-module in
[`docs/failure-taxonomy.md`](docs/failure-taxonomy.md), the primary deliverable.

## Layout

```
src/
  common/     shared models, the read-only/read-write DB split, the LLM wrapper
  synthetic/  deterministic data generators (build-time, outside the credential model)
  ingestion/  webhook -> DisputeCase, reason-code classification, phase/reopen tracking
  evidence/   EvidenceBundle assembly, present/missing/not_applicable, DPDP citations
  risk/       transaction graph, PageRank/betweenness, rolling-window baseline_deviation
  scoring/    confidence score, draft-vs-review routing, priority queue, deadline scan
  compliance/ curated India regulatory graph + citation lookup
  qa/         metrics harness (precision / recall / false-positive cost)
  pipeline.py end-to-end glue          main.py  FastAPI app
```

## How it was built

Orchestrated with Claude Code: an Opus session sequenced seven Sonnet module
workers against the contracts in `CLAUDE.md`, one commit per module. Each worker
wrote its own tests and its own section of the failure taxonomy as it went.
