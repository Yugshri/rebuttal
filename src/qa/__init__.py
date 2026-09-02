"""qa-evaluator — measurement, not implementation.

This package turns "we built a risk manager" into "here is how well it works,
including where it is wrong". It is the ONLY code in the repo that reads
``data/heldout/`` — the pipeline path never does (verified by
``tests/qa/test_defense_only_boundary.py``).

Public surface::

    from src.qa.harness import run_corpus, evaluate, Heldout, DISPUTE_NOW_EPOCH

Run the whole thing and write the report::

    .venv/Scripts/python.exe -m src.qa.harness
"""

from __future__ import annotations
