"""End-to-end evaluation harness for the Track 02 pipeline.

What it does, in one pass:

1. Point the app at the committed **read-only** ``data/external.db`` plus a
   throwaway ``system.db``.
2. Replay every ``data/webhooks/*.json`` through ``ingest_dispute_event`` and run
   each resulting dispute through ``run_pipeline`` (assembly -> risk enrichment ->
   scoring -> routing), at the corpus's own frozen "now" (2026-09-03T12:00:00).
3. Score the pipeline's independently-computed output against ``data/heldout/``:
   * category classification precision / recall (incl. ``needs_manual_classification``);
   * risk flagging — do the 3 planted mules get a HIGH ``baseline_deviation`` and
     the 2 legitimately-bursty controls NOT;
   * routing — ``draft_for_submit`` vs ``human_review`` against the held-out
     ``dispute_dispositions.json``;
   * **false-positive cost** — count + a stated cost model for legitimate cases
     pushed to unnecessary human review.
4. Emit a readable report to stdout and to ``docs/evaluation-report.md``.

``data/heldout/`` is read **only here**. Nothing under ``src/`` outside this
package imports it (asserted by ``tests/qa/test_defense_only_boundary.py``).
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field
from datetime import timezone
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import text

from src.common import db

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"
WEBHOOK_DIR = DATA_DIR / "webhooks"
EXTERNAL_DB = DATA_DIR / "external.db"
HELDOUT_DIR = DATA_DIR / "heldout"
REPORT_PATH = REPO_ROOT / "docs" / "evaluation-report.md"

# Risk band cut, kept in sync with risk-graph-service.
from src.risk.batch import ELEVATED_DEVIATION_THRESHOLD, HIGH_DEVIATION_THRESHOLD


def _dispute_now_epoch() -> int:
    from src.synthetic import NOW
    from src.synthetic.rng import parse_iso

    return int(parse_iso(NOW).replace(tzinfo=timezone.utc).timestamp())


DISPUTE_NOW_EPOCH = _dispute_now_epoch()


# --------------------------------------------------------------------------- #
# held-out ground truth — the only reader of data/heldout/
# --------------------------------------------------------------------------- #
@dataclass
class Heldout:
    account_labels: dict[str, Any]
    dispositions: dict[str, Any]

    @classmethod
    def load(cls, heldout_dir: Path = HELDOUT_DIR) -> "Heldout":
        acc = json.loads((heldout_dir / "account_labels.json").read_text("utf-8"))
        disp = json.loads(
            (heldout_dir / "dispute_dispositions.json").read_text("utf-8")
        )
        return cls(account_labels=acc, dispositions=disp)

    @property
    def planted_mules(self) -> set[str]:
        return set(self.account_labels["planted_mules"])

    @property
    def bursty_controls(self) -> set[str]:
        return set(self.account_labels["bursty_controls"])

    @property
    def labels(self) -> dict[str, dict]:
        return self.account_labels["labels"]

    @property
    def disp(self) -> dict[str, dict]:
        return self.dispositions["dispositions"]


# --------------------------------------------------------------------------- #
# corpus run
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def corpus_env(system_db_path: Path):
    """Point ``src.common.db`` at real external.db + a throwaway system.db."""
    if not EXTERNAL_DB.exists():
        raise FileNotFoundError(
            f"{EXTERNAL_DB} missing — run `.venv/Scripts/python.exe -m src.synthetic.build`"
        )
    saved = (db.EXTERNAL_DB_PATH, db.EXTERNAL_DB_URL, db.SYSTEM_DB_PATH, db.SYSTEM_DB_URL)
    db.EXTERNAL_DB_PATH = EXTERNAL_DB.resolve()
    db.EXTERNAL_DB_URL = (
        f"sqlite:///file:{db.EXTERNAL_DB_PATH.as_posix()}?mode=ro&uri=true"
    )
    db.SYSTEM_DB_PATH = Path(system_db_path).resolve()
    db.SYSTEM_DB_URL = f"sqlite:///{db.SYSTEM_DB_PATH.as_posix()}"
    db.reset_engines_for_tests()
    try:
        yield
    finally:
        (db.EXTERNAL_DB_PATH, db.EXTERNAL_DB_URL, db.SYSTEM_DB_PATH, db.SYSTEM_DB_URL) = saved
        db.reset_engines_for_tests()


@dataclass
class CorpusRun:
    n_webhooks: int
    n_disputes: int
    n_risk_profiles: int
    results: dict[str, dict] = field(default_factory=dict)
    risk_profiles: dict[str, dict] = field(default_factory=dict)

    def account_for(self, dispute_id: str) -> str | None:
        return self.results.get(dispute_id, {}).get("account_id")


def _load_payloads() -> list[dict]:
    return [
        json.loads(p.read_text("utf-8"))
        for p in sorted(WEBHOOK_DIR.glob("*.json"))
    ]


def _resolve_accounts(payment_ids: Iterable[str]) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    with db.read_only_session() as s:
        for pid in payment_ids:
            row = s.execute(
                text("SELECT account_id FROM payments WHERE payment_id = :p"),
                {"p": pid},
            ).fetchone()
            if row is None:
                row = s.execute(
                    text("SELECT account_id FROM orders WHERE payment_id = :p"),
                    {"p": pid},
                ).fetchone()
            out[pid] = row[0] if row is not None else None
    return out


def run_corpus(system_db_path: Path) -> CorpusRun:
    """Replay the whole webhook corpus through the real pipeline. No heldout here."""
    from src.evidence import get_evidence_bundle
    from src.ingestion import get_dispute_case, ingest_dispute_event
    from src.pipeline import ensure_risk_profiles, init_all_tables, run_pipeline
    from src.risk.models import AccountRiskProfile
    from src.scoring.routing import get_active_entry

    payloads = _load_payloads()

    with corpus_env(system_db_path):
        init_all_tables()
        n_profiles = ensure_risk_profiles()

        dispute_ids: list[str] = []
        for payload in payloads:
            outcome = ingest_dispute_event(payload)
            if outcome.dispute_id not in dispute_ids:
                dispute_ids.append(outcome.dispute_id)

        results: dict[str, dict] = {}
        for did in dispute_ids:
            case = get_dispute_case(did)
            pr = run_pipeline(did, now_epoch=DISPUTE_NOW_EPOCH)
            bundle = get_evidence_bundle(did) or {}
            entry = get_active_entry(did) or {}
            results[did] = {
                "dispute_id": did,
                "payment_id": case["payment_id"],
                "phase": case["phase"],
                "status": case["status"],
                "reason_code": case["reason_code"],
                "reopen_count": case["reopen_count"],
                "category": pr.category,
                "needs_manual_classification": pr.needs_manual_classification,
                "completeness": pr.evidence_completeness,
                "assembly_status": pr.evidence_assembly_status,
                "confidence_score": pr.confidence_score,
                "recommended_action": pr.recommended_action,
                "queue": pr.queue,
                "priority": pr.priority,
                "hard_gates": list(entry.get("hard_gates") or []),
                "bundle_compliance_citations": bundle.get("compliance_citations") or {},
                "entry_compliance_citations": entry.get("compliance_citations") or [],
                "rationale": pr.rationale,
            }

        acc_by_pay = _resolve_accounts({r["payment_id"] for r in results.values()})
        for r in results.values():
            r["account_id"] = acc_by_pay.get(r["payment_id"])

        risk_profiles: dict[str, dict] = {}
        with db.system_session() as s:
            for row in s.query(AccountRiskProfile).all():
                risk_profiles[row.account_id] = row.as_dict()

    return CorpusRun(
        n_webhooks=len(payloads),
        n_disputes=len(dispute_ids),
        n_risk_profiles=n_profiles,
        results=results,
        risk_profiles=risk_profiles,
    )


# --------------------------------------------------------------------------- #
# metrics
# --------------------------------------------------------------------------- #
@dataclass
class PRF:
    label: str
    tp: int
    fp: int
    fn: int

    @property
    def precision(self) -> float | None:
        d = self.tp + self.fp
        return round(self.tp / d, 4) if d else None

    @property
    def recall(self) -> float | None:
        d = self.tp + self.fn
        return round(self.tp / d, 4) if d else None

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        if not p or not r:
            return None
        return round(2 * p * r / (p + r), 4)

    def as_row(self) -> dict:
        return {
            "label": self.label,
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "precision": self.precision,
            "recall": self.recall,
            "f1": self.f1,
        }


def _prf_multiclass(pairs: list[tuple[str, str]]) -> dict[str, PRF]:
    """pairs = [(gold, pred), ...] -> per-class PRF."""
    classes = sorted({g for g, _ in pairs} | {p for _, p in pairs})
    out: dict[str, PRF] = {}
    for c in classes:
        tp = sum(1 for g, p in pairs if g == c and p == c)
        fp = sum(1 for g, p in pairs if g != c and p == c)
        fn = sum(1 for g, p in pairs if g == c and p != c)
        out[c] = PRF(c, tp, fp, fn)
    return out


def _macro(prfs: dict[str, PRF]) -> dict[str, float]:
    ps = [x.precision for x in prfs.values() if x.precision is not None]
    rs = [x.recall for x in prfs.values() if x.recall is not None]
    return {
        "macro_precision": round(sum(ps) / len(ps), 4) if ps else None,
        "macro_recall": round(sum(rs) / len(rs), 4) if rs else None,
        "accuracy": None,
    }


# canonical held-out disposition -> pipeline queue
_DEFER = "defer_to_human"
_CLEAN = "assemble_clean"
_DRAFT = "draft_for_submit"
_HUMAN = "human_review"


@dataclass
class Evaluation:
    corpus: CorpusRun
    classification: dict[str, PRF]
    classification_macro: dict[str, float]
    classification_accuracy: float
    manual_bucket: PRF
    risk_flag_strict: PRF
    risk_flag_detail: dict[str, Any]
    routing: PRF
    routing_confusion: dict[str, int]
    routing_accuracy: float
    false_positive_cost: dict[str, Any]
    false_negatives: dict[str, Any]
    notes: list[str] = field(default_factory=list)

    # ---- rendering ------------------------------------------------------- #
    def render_table(self) -> str:
        c = self.corpus
        L: list[str] = []
        L.append("=" * 78)
        L.append("TRACK 02 — PIPELINE EVALUATION (synthetic held-out set)")
        L.append("=" * 78)
        L.append(
            f"corpus: {c.n_webhooks} webhook events -> {c.n_disputes} disputes; "
            f"{c.n_risk_profiles} account risk profiles; "
            f"frozen now = {DISPUTE_NOW_EPOCH} (2026-09-03T12:00:00Z)"
        )
        L.append("")
        L.append("-- 1. CATEGORY CLASSIFICATION -------------------------------------------")
        L.append(f"{'class':<30}{'prec':>8}{'recall':>8}{'f1':>8}{'tp':>6}{'fp':>6}{'fn':>6}")
        for name, prf in self.classification.items():
            L.append(
                f"{name:<30}{_f(prf.precision):>8}{_f(prf.recall):>8}"
                f"{_f(prf.f1):>8}{prf.tp:>6}{prf.fp:>6}{prf.fn:>6}"
            )
        L.append(
            f"{'MACRO':<30}{_f(self.classification_macro['macro_precision']):>8}"
            f"{_f(self.classification_macro['macro_recall']):>8}"
        )
        L.append(f"overall accuracy: {self.classification_accuracy:.3f}")
        L.append(
            f"needs_manual_classification bucket: "
            f"P={_f(self.manual_bucket.precision)} R={_f(self.manual_bucket.recall)} "
            f"(tp={self.manual_bucket.tp} fp={self.manual_bucket.fp} fn={self.manual_bucket.fn})"
        )
        L.append("")
        L.append("-- 2. RISK FLAGGING (planted mules vs bursty controls) ----------------")
        rf = self.risk_flag_strict
        L.append(
            f"strict (only the 3 planted mules are positives): "
            f"P={_f(rf.precision)} R={_f(rf.recall)} "
            f"(tp={rf.tp} fp={rf.fp} fn={rf.fn})"
        )
        d = self.risk_flag_detail
        L.append(f"  planted mules flagged HIGH:      {d['mules_flagged']}/3  {d['mule_devs']}")
        L.append(f"  bursty controls flagged HIGH:    {d['bursty_flagged']}/2  {d['bursty_devs']}")
        L.append(f"  total accounts at HIGH band:     {d['n_high']}  (>= {HIGH_DEVIATION_THRESHOLD})")
        L.append(f"  of those, planted mules:         {d['high_mules']}")
        L.append(f"  of those, 'normal'-labelled:     {d['high_normal']}  (structural false positives)")
        L.append("")
        L.append("-- 3. ROUTING (draft_for_submit vs human_review) ---------------------")
        rt = self.routing
        L.append(
            f"positive class = assemble_clean/draft_for_submit: "
            f"P={_f(rt.precision)} R={_f(rt.recall)} F1={_f(rt.f1)}"
        )
        cf = self.routing_confusion
        L.append(
            f"  confusion: draft&clean={cf['tp']}  human&defer={cf['tn']}  "
            f"draft-but-should-defer={cf['fp']}  human-but-should-draft={cf['fn']}"
        )
        L.append(f"  agreement with held-out dispositions: {self.routing_accuracy:.3f}")
        L.append("")
        L.append("-- 4. FALSE-POSITIVE COST -------------------------------------------")
        fpc = self.false_positive_cost
        L.append(
            f"legit cases (held-out assemble_clean, non-borderline) pushed to "
            f"unnecessary human review: {fpc['n_unnecessary_review']}"
        )
        L.append(f"  driven by a risk-flag FP on a 'normal' account: {fpc['from_risk_fp']}")
        L.append(f"  driven by an evidence/threshold miss:           {fpc['from_evidence']}")
        L.append(f"  bursty-control disputes deferred via a risk flag: {fpc['bursty_linked_flagged']}")
        L.append(f"  cost model: {fpc['cost_model']}")
        L.append(f"  => {fpc['analyst_minutes']} analyst-minutes / {c.n_disputes}-dispute corpus "
                 f"(~{fpc['analyst_hours']:.1f} h); {fpc['cost_rupees']}")
        L.append(f"  bounded harm: {fpc['bounded_harm']}")
        L.append("")
        fn = self.false_negatives
        L.append(
            f"reverse error (held-out defer, pipeline drafted): {fn['n']} "
            f"— cost {fn['cost']}"
        )
        if self.notes:
            L.append("")
            L.append("-- notes --")
            for n in self.notes:
                L.append(f"  * {n}")
        L.append("=" * 78)
        return "\n".join(L)

    def headline(self) -> dict[str, Any]:
        return {
            "classification_macro_precision": self.classification_macro["macro_precision"],
            "classification_macro_recall": self.classification_macro["macro_recall"],
            "classification_accuracy": round(self.classification_accuracy, 4),
            "risk_flag_precision": self.risk_flag_strict.precision,
            "risk_flag_recall": self.risk_flag_strict.recall,
            "routing_precision": self.routing.precision,
            "routing_recall": self.routing.recall,
            "routing_accuracy": round(self.routing_accuracy, 4),
            "false_positive_cost_cases": self.false_positive_cost["n_unnecessary_review"],
            "false_positive_cost_analyst_hours": round(
                self.false_positive_cost["analyst_hours"], 2
            ),
        }

    def write_markdown(self, path: Path = REPORT_PATH) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_render_markdown(self), "utf-8")
        return path


def _f(v: float | None) -> str:
    return "  n/a" if v is None else f"{v:.3f}"


def evaluate(corpus: CorpusRun, heldout: Heldout) -> Evaluation:
    disp = heldout.disp
    res = corpus.results
    notes: list[str] = []

    # -- 1. classification ------------------------------------------------- #
    cls_pairs: list[tuple[str, str]] = []
    for did, gt in disp.items():
        if did not in res:
            continue
        gold = gt["category_hint"]
        pred = res[did]["category"]
        cls_pairs.append((gold, pred))
    classification = _prf_multiclass(cls_pairs)
    classification_macro = _macro(classification)
    cls_acc = (
        sum(1 for g, p in cls_pairs if g == p) / len(cls_pairs) if cls_pairs else 0.0
    )
    manual_bucket = classification.get(
        "needs_manual_classification", PRF("needs_manual_classification", 0, 0, 0)
    )

    # -- 2. risk flagging ------------------------------------------------- #
    def dev(acc: str) -> float | None:
        p = corpus.risk_profiles.get(acc)
        return None if p is None else p["baseline_deviation"]

    def is_high(acc: str) -> bool:
        d = dev(acc)
        return d is not None and d >= HIGH_DEVIATION_THRESHOLD

    mules = sorted(heldout.planted_mules)
    bursty = sorted(heldout.bursty_controls)
    mule_devs = {m: dev(m) for m in mules}
    bursty_devs = {b: dev(b) for b in bursty}
    mules_flagged = sum(1 for m in mules if is_high(m))
    bursty_flagged = sum(1 for b in bursty if is_high(b))

    labels = heldout.labels
    high_accounts = [
        a for a, p in corpus.risk_profiles.items()
        if p["baseline_deviation"] >= HIGH_DEVIATION_THRESHOLD
    ]
    high_mules = sum(1 for a in high_accounts if a in heldout.planted_mules)
    high_normal = sum(
        1 for a in high_accounts
        if labels.get(a, {}).get("label") == "normal"
    )
    # strict: positives = the 3 planted mules; everything else with a profile negative
    strict = PRF(
        "planted_mule",
        tp=high_mules,
        fp=len(high_accounts) - high_mules,
        fn=len(heldout.planted_mules) - high_mules,
    )
    risk_flag_detail = {
        "mules_flagged": mules_flagged,
        "bursty_flagged": bursty_flagged,
        "mule_devs": {k: (round(v, 2) if v is not None else None) for k, v in mule_devs.items()},
        "bursty_devs": {k: (round(v, 2) if v is not None else None) for k, v in bursty_devs.items()},
        "n_high": len(high_accounts),
        "high_mules": high_mules,
        "high_normal": high_normal,
        "high_accounts": sorted(high_accounts),
    }
    if bursty_flagged:
        notes.append(
            f"{bursty_flagged} bursty control(s) wrongly flagged HIGH — "
            "the false-positive target fired."
        )

    # -- 3. routing ------------------------------------------------------- #
    def gold_defer(did: str) -> bool:
        return disp[did]["expected_disposition"] == _DEFER

    def pred_defer(did: str) -> bool:
        return res[did]["queue"] != _DRAFT  # human_review or anything non-draft

    tp = fp = fn = tn = 0  # positive class = assemble_clean
    for did in disp:
        if did not in res:
            continue
        g_clean = not gold_defer(did)
        p_clean = not pred_defer(did)
        if g_clean and p_clean:
            tp += 1
        elif not g_clean and not p_clean:
            tn += 1
        elif not g_clean and p_clean:
            fp += 1  # pipeline drafted, held-out says defer
        else:
            fn += 1  # pipeline deferred, held-out says clean
    routing = PRF(_CLEAN, tp=tp, fp=fp, fn=fn)
    routing_confusion = {"tp": tp, "tn": tn, "fp": fp, "fn": fn}
    routing_accuracy = (tp + tn) / (tp + tn + fp + fn) if (tp + tn + fp + fn) else 0.0

    # -- 4. false-positive cost ----------------------------------------- #
    # legit == held-out assemble_clean AND not a hand-flagged borderline flip
    unnecessary: list[str] = []
    from_risk_fp: list[str] = []
    from_evidence: list[str] = []
    for did in disp:
        if did not in res:
            continue
        gt = disp[did]
        if gt["expected_disposition"] != _CLEAN:
            continue
        if gt.get("borderline_flip"):
            continue
        if not pred_defer(did):
            continue  # correctly drafted
        unnecessary.append(did)
        acc = res[did]["account_id"]
        acc_label = labels.get(acc, {}).get("label")
        gates = res[did]["hard_gates"]
        if (
            "risk_profile_unknown" not in gates
            and acc_label == "normal"
            and (dev(acc) or 0.0) >= ELEVATED_DEVIATION_THRESHOLD
        ):
            from_risk_fp.append(did)
        else:
            from_evidence.append(did)

    bursty_acc = set(heldout.bursty_controls)
    # the false-positive target: a dispute on a legitimately-bursty account that
    # got deferred *because risk-graph-service flagged that account* (dev >= ELEVATED).
    bursty_linked_flagged = [
        did for did in res
        if res[did]["account_id"] in bursty_acc
        and (dev(res[did]["account_id"]) or 0.0) >= ELEVATED_DEVIATION_THRESHOLD
    ]

    # cost model (stated, not hidden):
    #   * ~12 min of a dispute analyst's time to open, read the assembled bundle,
    #     confirm it is clean, and dispatch — a case that per ground truth needed
    #     no human judgement at all.
    #   * fully-loaded Indian dispute-ops analyst ~ Rs 600 / hour (Rs 10 / min).
    #   * NO case here costs frozen funds or an auto-action: the defense-only
    #     boundary means the worst outcome is analyst minutes + a few hours of
    #     added latency before the merchant's evidence is dispatched.
    minutes_per_case = 12
    rupees_per_minute = 10
    n = len(unnecessary)
    analyst_minutes = n * minutes_per_case
    analyst_hours = analyst_minutes / 60.0
    false_positive_cost = {
        "n_unnecessary_review": n,
        "unnecessary_ids": sorted(unnecessary),
        "from_risk_fp": sorted(from_risk_fp),
        "from_evidence": sorted(from_evidence),
        "bursty_linked_flagged": sorted(bursty_linked_flagged),
        "cost_model": (
            f"{minutes_per_case} analyst-min/case to open+verify+dispatch a "
            f"case ground truth says needed no human judgement; "
            f"Rs {rupees_per_minute}/min fully-loaded"
        ),
        "analyst_minutes": analyst_minutes,
        "analyst_hours": analyst_hours,
        "cost_rupees": f"~Rs {analyst_minutes * rupees_per_minute:,} of avoidable analyst time",
        "bounded_harm": (
            "no frozen funds, no auto-contact, no auto-submit — the defense-only "
            "boundary caps the harm at analyst time + dispatch latency"
        ),
    }

    # -- reverse error -------------------------------------------------- #
    fn_ids = [
        did for did in disp
        if did in res and gold_defer(did) and not pred_defer(did)
    ]
    false_negatives = {
        "n": len(fn_ids),
        "ids": sorted(fn_ids),
        "cost": (
            "~0 operational — every one still lands in the draft_for_submit "
            "queue with dispatched=False, so a human reads it before anything "
            "is submitted; the miss is 'not escalated louder', not 'auto-sent'"
        ),
    }

    notes.append(
        "planted-mule prevalence in the synthetic graph (~1% of accounts) is "
        "~10x a real AML base rate — risk precision/recall is inflated vs a real "
        "deployment (inherited calibration note)."
    )
    notes.append(
        "held-out dispositions encode what the pipeline SHOULD do, not what "
        "would actually win a dispute — routing accuracy is agreement with "
        "designer intent, not a card-network outcome."
    )

    return Evaluation(
        corpus=corpus,
        classification=classification,
        classification_macro=classification_macro,
        classification_accuracy=cls_acc,
        manual_bucket=manual_bucket,
        risk_flag_strict=strict,
        risk_flag_detail=risk_flag_detail,
        routing=routing,
        routing_confusion=routing_confusion,
        routing_accuracy=routing_accuracy,
        false_positive_cost=false_positive_cost,
        false_negatives=false_negatives,
        notes=notes,
    )


# --------------------------------------------------------------------------- #
# markdown report
# --------------------------------------------------------------------------- #
def _render_markdown(ev: Evaluation) -> str:
    c = ev.corpus
    fpc = ev.false_positive_cost
    d = ev.risk_flag_detail
    lines: list[str] = []
    lines.append("# Track 02 — Evaluation Report")
    lines.append("")
    lines.append(
        "> Generated by `python -m src.qa.harness`. Scores the pipeline's "
        "independently-computed output against `data/heldout/` (which the "
        "pipeline path never reads). Honest metrics, including false-positive "
        "cost, per the track rubric."
    )
    lines.append("")
    lines.append(
        f"- Corpus: **{c.n_webhooks}** webhook events -> **{c.n_disputes}** disputes; "
        f"**{c.n_risk_profiles}** account risk profiles."
    )
    lines.append(
        f"- Frozen evaluation clock: `{DISPUTE_NOW_EPOCH}` (2026-09-03T12:00:00Z), "
        "the corpus's own \"now\"."
    )
    lines.append("")

    lines.append("## 1. Category classification")
    lines.append("")
    lines.append("| class | precision | recall | f1 | tp | fp | fn |")
    lines.append("|---|---|---|---|---|---|---|")
    for name, prf in ev.classification.items():
        lines.append(
            f"| `{name}` | {_f(prf.precision)} | {_f(prf.recall)} | {_f(prf.f1)} "
            f"| {prf.tp} | {prf.fp} | {prf.fn} |"
        )
    lines.append(
        f"| **macro** | {_f(ev.classification_macro['macro_precision'])} "
        f"| {_f(ev.classification_macro['macro_recall'])} | | | | |"
    )
    lines.append("")
    lines.append(f"Overall accuracy: **{ev.classification_accuracy:.3f}**.")
    lines.append("")
    lines.append(
        f"`needs_manual_classification` bucket (the one most chargeback bots skip): "
        f"precision **{_f(ev.manual_bucket.precision)}**, recall "
        f"**{_f(ev.manual_bucket.recall)}** "
        f"(tp={ev.manual_bucket.tp}, fp={ev.manual_bucket.fp}, fn={ev.manual_bucket.fn}). "
        "An unmapped real network code is routed here explicitly, never guessed "
        "into the nearest category."
    )
    lines.append("")

    lines.append("## 2. Risk flagging — planted mules vs. legitimately-bursty controls")
    lines.append("")
    lines.append(
        f"Strict scoring (only the 3 planted mules count as positives, every "
        f"other profiled account is a negative): precision "
        f"**{_f(ev.risk_flag_strict.precision)}**, recall "
        f"**{_f(ev.risk_flag_strict.recall)}**."
    )
    lines.append("")
    lines.append("| account | held-out label | baseline_deviation | flagged HIGH? |")
    lines.append("|---|---|---|---|")
    for m, v in d["mule_devs"].items():
        lines.append(f"| `{m}` | planted_mule | {v} | {'yes' if (v or 0) >= HIGH_DEVIATION_THRESHOLD else 'NO'} |")
    for b, v in d["bursty_devs"].items():
        lines.append(f"| `{b}` | bursty_control | {v} | {'YES (FP)' if (v or 0) >= HIGH_DEVIATION_THRESHOLD else 'no'} |")
    lines.append("")
    lines.append(
        f"- Planted mules flagged HIGH: **{d['mules_flagged']}/3**. "
        f"Bursty controls flagged HIGH: **{d['bursty_flagged']}/2**."
    )
    lines.append(
        f"- {d['n_high']} accounts total sit at the HIGH band "
        f"(`baseline_deviation >= {HIGH_DEVIATION_THRESHOLD}`): "
        f"{d['high_mules']} planted mules, {d['high_normal']} `normal`-labelled "
        "accounts (the documented cross-cluster / fan-out-recipient structural "
        "false positives — see `docs/failure-taxonomy.md`, risk-graph-service)."
    )
    lines.append("")

    lines.append("## 3. Routing — draft-for-submit vs. human review")
    lines.append("")
    cf = ev.routing_confusion
    lines.append(
        f"Positive class = `assemble_clean` / `draft_for_submit`. Precision "
        f"**{_f(ev.routing.precision)}**, recall **{_f(ev.routing.recall)}**, "
        f"F1 **{_f(ev.routing.f1)}**. Agreement with held-out dispositions: "
        f"**{ev.routing_accuracy:.3f}** ({cf['tp'] + cf['tn']}/"
        f"{cf['tp'] + cf['tn'] + cf['fp'] + cf['fn']})."
    )
    lines.append("")
    lines.append("| | held-out: assemble_clean | held-out: defer_to_human |")
    lines.append("|---|---|---|")
    lines.append(f"| pipeline: draft_for_submit | {cf['tp']} | {cf['fp']} |")
    lines.append(f"| pipeline: human_review | {cf['fn']} | {cf['tn']} |")
    lines.append("")

    lines.append("## 4. False-positive cost")
    lines.append("")
    lines.append(
        f"**{fpc['n_unnecessary_review']}** legitimate cases (held-out "
        "`assemble_clean`, not a hand-flagged borderline flip) were pushed to "
        "unnecessary human review."
    )
    lines.append("")
    lines.append(f"- from a risk-flag false positive on a `normal` account: **{len(fpc['from_risk_fp'])}** ({', '.join('`'+i+'`' for i in fpc['from_risk_fp']) or 'none'})")
    lines.append(f"- from an evidence-completeness / threshold miss: **{len(fpc['from_evidence'])}** ({', '.join('`'+i+'`' for i in fpc['from_evidence']) or 'none'})")
    lines.append(f"- bursty-control disputes deferred because risk flagged that account: **{len(fpc['bursty_linked_flagged'])}** (the false-positive target — 0 is the goal)")
    lines.append("")
    lines.append(f"**Cost model.** {fpc['cost_model']}.")
    lines.append("")
    lines.append(
        f"=> **{fpc['analyst_minutes']} analyst-minutes** "
        f"(~{fpc['analyst_hours']:.1f} h) across a {c.n_disputes}-dispute corpus; "
        f"{fpc['cost_rupees']}."
    )
    lines.append("")
    lines.append(
        f"**Bounded harm.** {fpc['bounded_harm']}. The reverse error "
        f"({ev.false_negatives['n']} held-out `defer_to_human` cases the pipeline "
        f"drafted) costs {ev.false_negatives['cost']}."
    )
    lines.append("")
    lines.append("## Caveats")
    lines.append("")
    for n in ev.notes:
        lines.append(f"- {n}")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# entrypoint
# --------------------------------------------------------------------------- #
def run_full_evaluation(system_db_path: Path | None = None) -> Evaluation:
    import tempfile

    if system_db_path is None:
        tmpdir = Path(tempfile.mkdtemp(prefix="qa_harness_"))
        system_db_path = tmpdir / "system.db"
    corpus = run_corpus(system_db_path)
    return evaluate(corpus, Heldout.load())


def main() -> None:
    ev = run_full_evaluation()
    print(ev.render_table())
    path = ev.write_markdown()
    print(f"\nwritten: {path}")


if __name__ == "__main__":
    main()
