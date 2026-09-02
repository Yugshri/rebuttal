"""The corpus must be byte-for-byte reproducible from the single seed.

``qa-evaluator``'s precision/recall/false-positive-cost numbers are only
meaningful if the data they are computed against is stable run-to-run.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from src.synthetic import SEED
from src.synthetic.build import main
from src.synthetic.schema import dump_all


def _hash_dir(path: Path) -> dict[str, str]:
    out = {}
    for f in sorted(path.glob("*.json")):
        out[f.name] = hashlib.sha256(f.read_bytes()).hexdigest()
    return out


def test_build_is_deterministic(tmp_path: Path):
    a = tmp_path / "run_a"
    b = tmp_path / "run_b"
    summary_a = main(a)
    summary_b = main(b)

    # Everything except the (dir-dependent) db path must match exactly.
    assert {k: v for k, v in summary_a.items() if k != "external_db"} == {
        k: v for k, v in summary_b.items() if k != "external_db"
    }
    assert summary_a["seed"] == SEED

    # external.db: compare table contents (not raw file bytes).
    dump_a = dump_all(a / "external.db")
    dump_b = dump_all(b / "external.db")
    assert dump_a.keys() == dump_b.keys()
    for table in dump_a:
        assert dump_a[table] == dump_b[table], f"table {table} differs across runs"

    # webhooks + heldout JSON: byte-identical.
    assert _hash_dir(a / "webhooks") == _hash_dir(b / "webhooks")
    assert _hash_dir(a / "heldout") == _hash_dir(b / "heldout")


def test_second_build_overwrites_not_appends(tmp_path: Path):
    main(tmp_path)
    first = dump_all(tmp_path / "external.db")
    main(tmp_path)
    second = dump_all(tmp_path / "external.db")
    for table in first:
        assert len(first[table]) == len(second[table]), f"{table} row count changed"


def test_heldout_json_parses(tmp_path: Path):
    main(tmp_path)
    for name in ("account_labels.json", "dispute_dispositions.json"):
        json.loads((tmp_path / "heldout" / name).read_text(encoding="utf-8"))
