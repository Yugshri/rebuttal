"""Deterministic per-component random streams.

One ``SeedSequence`` is spawned into named child generators so that adding or
reordering work inside one generator can't shift the numbers another generator
draws. ``qa-evaluator``'s metrics are only meaningful if the corpus is stable
run-to-run, so every draw in this package goes through here.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta

import numpy as np

_ISO = "%Y-%m-%dT%H:%M:%S"


def streams(seed: int, names: Iterable[str]) -> dict[str, np.random.Generator]:
    """Return one independent ``np.random.Generator`` per name.

    Deterministic in the names' iteration order: callers pass a fixed list.
    """
    names = list(names)
    root = np.random.SeedSequence(seed)
    children = root.spawn(len(names))
    return {name: np.random.default_rng(child) for name, child in zip(names, children)}


def parse_iso(value: str) -> datetime:
    return datetime.strptime(value, _ISO)


def to_iso(value: datetime) -> str:
    return value.strftime(_ISO)


def jitter_seconds(
    rng: np.random.Generator, base: datetime, low_s: int, high_s: int
) -> datetime:
    """``base`` shifted by a uniformly random whole-second offset in a range."""
    return base + timedelta(seconds=int(rng.integers(low_s, high_s + 1)))


def lognormal_amount(
    rng: np.random.Generator,
    *,
    median: float,
    sigma: float,
    lo: float,
    hi: float,
) -> float:
    """A right-skewed positive amount, clipped to a plausible band.

    ``median`` is the geometric centre (``exp(mu)``); ``sigma`` the log-space
    spread. Calibrated per call site — see the citing comment there.
    """
    value = float(rng.lognormal(mean=np.log(median), sigma=sigma))
    return round(min(max(value, lo), hi), 2)
