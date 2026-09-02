# Buildathon — Track 02 build (AI Risk Manager)

Chargeback evidence responder + graph-based risk enrichment, for the Razorpay AI
Buildathon 2026 (submission close: 5 September 2026). Full context, architecture,
and definition of done live in `CLAUDE.md` — read that first, it's project memory
and Claude Code loads it automatically anyway.

## Quick start

From this folder:

```
claude --model opus
```

That starts the orchestrator on Opus. The six subagents in `.claude/agents/` each
pin `model: sonnet` in their own frontmatter, so they run on Sonnet automatically
regardless of what the top-level session is on — you don't need to do anything extra
per-agent. (If your installed Claude Code version doesn't resolve the short model
aliases `opus`/`sonnet`, swap in the full model IDs it expects — the frontmatter
field is the same either way.)

Once it's running, just talk to it about the build — it'll route to the right
subagent by matching your request against each agent file's `description`. To force
a specific one: "Use the synthetic-data-generator subagent to build the transaction
graph dataset."

**Keep Opus for planning, not for poking around.** Small routine checks — did a
file get created, does a test pass, does this one function work — should happen
inside the subagent doing the work, not get bounced back to the Opus orchestrator
to look at directly. If you notice yourself asking the top-level session to
re-check something a subagent already reported on, that's a sign to just trust the
subagent's report instead. Save the orchestrator's attention for sequencing,
integration between modules, and the definition-of-done review.

## Build order

Follow the sequence in `CLAUDE.md` → "Build sequence." Short version:

1. `synthetic-data-generator` first — nothing else has data to build against otherwise.
   `compliance-knowledge-graph` builds in parallel — it has no dependency on the data.
2. `dispute-ingestion-router` and `risk-graph-service` in parallel.
3. `evidence-assembler` once the router's data model exists.
4. `confidence-scorer-review` last — it consumes evidence, risk, and compliance output.
5. `qa-evaluator` runs throughout, not just at the end.

## The seven subagents

| File | Owns |
|---|---|
| `dispute-ingestion-router.md` | Webhook ingestion, `DisputeCase` model, reason-code routing, phase-transition tracking |
| `evidence-assembler.md` | Building `EvidenceBundle` from synthetic order/shipping/comms records per reason code |
| `risk-graph-service.md` | Transaction graph, PageRank + betweenness centrality, `AccountRiskProfile`, mule/COD fraud signals |
| `confidence-scorer-review.md` | Confidence scoring, draft-vs-human-review routing, deadline monitor, outcome feedback loop |
| `synthetic-data-generator.md` | All synthetic datasets — disputes, orders/shipping/comms, transaction graph, labeled eval set |
| `qa-evaluator.md` | Precision/recall/false-positive-cost measurement, defense-only boundary tests, failure taxonomy |
| `compliance-knowledge-graph.md` | Curated India regulatory graph (PSS Act, RBI PA-PG Master Direction, DPIP, DPDP Act, Consumer Protection E-Commerce Rules) — grounds recommendations in named authority |

## Running it once built

```
uvicorn src.main:app --reload
pytest tests/
```

(Exact entrypoint may shift slightly as the orchestrator wires modules together —
this is the intended shape, not a promise about file paths that don't exist yet.)

## Non-negotiable while building

The system must never hold credentials capable of moving money, freezing an account,
or messaging a customer — read-only against payment/order/dispute data, write-only
against its own tables. Every recommendation lands in a human-dispatched queue.
Nothing auto-submits, ever. See `CLAUDE.md` for why this is a differentiator, not
just a constraint.

## Deeper reference material

Three Google Docs carry the full research and system design this repo implements —
links are in `CLAUDE.md` under "Reference docs." Worth skimming before you start if
you haven't already; the subagent files below are self-contained enough to build
from directly, but the docs have the reasoning behind each decision.
