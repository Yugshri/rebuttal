"""Precision / recall / false-positive-cost harness — run + headline assertions.

`pytest tests/ -q` surfaces the numbers: this test prints the full report table
and writes `docs/evaluation-report.md`, then asserts the headline metrics sit in
a sane band. It is a measurement test, not a pass/fail gate on the pipeline —
the bands are wide on purpose; the report is the deliverable.
"""

from __future__ import annotations

from src.qa.harness import REPORT_PATH, HIGH_DEVIATION_THRESHOLD


def test_full_evaluation_report(evaluation, capsys):
    ev = evaluation
    with capsys.disabled():
        print("\n" + ev.render_table())
    written = ev.write_markdown()
    assert written == REPORT_PATH
    assert written.exists() and written.stat().st_size > 500

    hl = ev.headline()
    for k, v in hl.items():
        assert v is not None, k

    # -- classification: the mapping is a deterministic view over the shared
    #    reason-code table, so it should be ~perfect on synthetic data.
    assert ev.classification_accuracy >= 0.95
    assert ev.classification_macro["macro_recall"] >= 0.9
    # the needs_manual_classification bucket every chargeback bot skips
    assert ev.manual_bucket.recall == 1.0
    assert ev.manual_bucket.precision >= 0.9
    assert ev.manual_bucket.tp >= 8

    # -- risk flagging: all 3 planted mules HIGH, neither bursty control HIGH.
    assert ev.risk_flag_strict.recall == 1.0
    assert ev.risk_flag_detail["mules_flagged"] == 3
    assert ev.risk_flag_detail["bursty_flagged"] == 0
    for acc, d in ev.risk_flag_detail["mule_devs"].items():
        assert d >= HIGH_DEVIATION_THRESHOLD, acc
    for acc, d in ev.risk_flag_detail["bursty_devs"].items():
        assert d < HIGH_DEVIATION_THRESHOLD, acc
    # strict precision is LOW by design (structural FPs) — assert it is reported,
    # not that it is high. This is the honest-metrics point.
    assert 0.0 < ev.risk_flag_strict.precision <= 0.6
    assert ev.risk_flag_detail["high_normal"] >= 1

    # -- routing vs held-out dispositions
    assert ev.routing_accuracy >= 0.80
    assert ev.routing.precision >= 0.85
    assert ev.routing.recall >= 0.80

    # -- false-positive cost: quantified, concrete, bounded.
    fpc = ev.false_positive_cost
    assert fpc["n_unnecessary_review"] == len(fpc["unnecessary_ids"])
    assert 0 <= fpc["n_unnecessary_review"] <= 25
    assert fpc["analyst_minutes"] == fpc["n_unnecessary_review"] * 12
    assert "defense-only" in fpc["bounded_harm"]
    # the designed false-positive target: no bursty control's dispute is deferred
    # *because risk flagged that account*.
    assert fpc["bursty_linked_flagged"] == []


def test_pipeline_never_reads_heldout(corpus):
    """The corpus run above imported and executed the whole pipeline. If any
    pipeline module had opened data/heldout/, that module would now be in
    sys.modules holding a reference — and the static scan in
    test_defense_only_boundary would also fail. Here: a runtime cross-check that
    the run completed without the heldout dir being needed by anything but us."""
    import sys

    offenders = []
    for name, mod in list(sys.modules.items()):
        if not name.startswith("src.") or name.startswith("src.qa"):
            continue
        src_file = getattr(mod, "__file__", "") or ""
        try:
            text = open(src_file, encoding="utf-8").read() if src_file.endswith(".py") else ""
        except OSError:
            text = ""
        if "heldout" in text or "dispute_dispositions" in text or "account_labels" in text:
            # src/synthetic writes the files at build time — that is allowed; it
            # is not on the pipeline path. Anything else is a leak.
            if not name.startswith("src.synthetic"):
                offenders.append(name)
    assert not offenders, f"pipeline modules reference held-out data: {offenders}"
    assert corpus.n_disputes == 129
