"""The minimum-coverage floor on breadth bars.

On 2026-08-07 the price vendor had not yet published Friday European
closes. Every Europe panel computed its breadth from TWO constituents out
of 26-32 and published 0.0, 0.5 or 1.0, because the guard was
`if ma_valid.any()` — one single name was enough. Those bars reached the
factsheet, the weekly email and the Live Signal chart.

A proportion over two names can only take three values. It is not a breadth
reading, and it must be absent rather than wrong.

These tests pin both halves of the fix: that degenerate bars are suppressed,
and — just as important — that the floor is low enough not to delete the
genuinely thin panels the book actually trades.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import compute_breadth as cb  # noqa: E402

DATA = ROOT / "data"


def test_floor_is_an_absolute_count_not_a_share():
    """The floor must be an absolute number of names.

    A share-of-roster rule would delete IDP6, which legitimately carries
    ~332 unpriced names out of ~603 — roughly 45% coverage as normal
    operation, not as a fault.
    """
    assert isinstance(cb.MIN_BREADTH_NAMES, int)
    assert 0 < cb.MIN_BREADTH_NAMES <= 6


def test_floor_leaves_headroom_under_the_thinnest_deployed_panel():
    """Calibration guard.

    EXH1 is the thinnest deployed panel: its historical minimum coverage is
    8 names. A floor at or above that would delete real bars — measured at
    1,270 of them across 16 panels for a floor of 10. If someone raises the
    constant, this fails and tells them why.
    """
    p = DATA / "breadth_exh1.json"
    if not p.exists():
        pytest.skip("EXH1 panel not built")
    ser = json.loads(p.read_text(encoding="utf-8"))["series"]
    observed = [n for n, m in zip(ser["n_with_ma50"], ser["ma_breadth"])
                if m is not None]
    if not observed:
        pytest.skip("no populated EXH1 bars")
    assert cb.MIN_BREADTH_NAMES < min(observed), (
        f"floor {cb.MIN_BREADTH_NAMES} would delete genuine EXH1 bars "
        f"(thinnest observed coverage {min(observed)})"
    )


@pytest.mark.parametrize("n_valid,expect_value", [
    (1, False), (2, False), (4, False),          # degenerate — must be null
    (5, True), (8, True), (30, True),            # usable
])
def test_bar_emitted_only_at_or_above_the_floor(n_valid, expect_value):
    """The arithmetic of the guard itself, independent of any panel."""
    mask = pd.Series([True] * n_valid + [False] * (40 - n_valid))
    # bool() because pandas returns numpy.bool_, which is never `is True`.
    emitted = bool(mask.sum() >= cb.MIN_BREADTH_NAMES)
    assert emitted is expect_value, (
        f"{n_valid} valid names should {'emit' if expect_value else 'not emit'} a bar"
    )


def test_no_published_bar_sits_below_the_floor():
    """End-to-end: no built panel may carry a breadth value computed on
    fewer than MIN_BREADTH_NAMES names, for any of the three components."""
    offenders = []
    for p in sorted(DATA.glob("breadth_*.json")):
        ser = json.loads(p.read_text(encoding="utf-8")).get("series") or {}
        dates = ser.get("dates") or []
        for value_key, count_key in (("ma_breadth", "n_with_ma50"),
                                      ("rsi_breadth", "n_with_rsi"),
                                      ("highs_breadth", "n_with_high63")):
            vals, counts = ser.get(value_key), ser.get(count_key)
            if not vals or not counts:
                continue
            for i, v in enumerate(vals):
                if v is not None and counts[i] < cb.MIN_BREADTH_NAMES:
                    offenders.append(
                        f"{p.stem} {dates[i]} {value_key}={v} on {counts[i]} names")
    assert not offenders, (
        f"{len(offenders)} breadth bars computed on fewer than "
        f"{cb.MIN_BREADTH_NAMES} names: {offenders[:6]}"
    )


def test_suppressed_bar_is_null_not_zero():
    """Absence must serialise as null. Zero would read as "nothing above its
    average" — a maximally bearish reading — which is the opposite of "we do
    not know", and the pre-warmup rows already establish null as the
    representation consumers handle."""
    assert cb._safe_float(np.nan) is None
    assert cb._safe_float(float("inf")) is None
    assert cb._safe_float(0.0) == 0.0
