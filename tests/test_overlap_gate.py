"""Guards for the book-wide overlap gate (scripts/check_universe_candidates.py).

The gate's failure mode is silence, not a crash: a mixed-calendar panel
run through a whole-frame rolling(200, min_periods=200) yields an all-NaN
signal panel, every pairwise correlation is then skipped for want of
overlapping observations, and every candidate comes back PASS having been
compared against nothing. These tests pin the two behaviours that stop
that: per-column signal computation, and a hard raise if the panel is
empty anyway.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from check_universe_candidates import (  # noqa: E402
    MA_PERIOD,
    OVERLAP_RULE_MAX_CORR,
    SIGNAL_GATE_MAX_CORR,
    STALE_TOL_DAYS,
    capture_integrity,
    pairwise,
    proxy_identity_pairs,
    weekly_returns,
    weekly_signal,
)


def _series(n: int, start: str, freq: str, seed: int) -> pd.Series:
    """A random-walk price series on a given calendar."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, periods=n, freq=freq)
    return pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.01, n))), index=idx)


def test_signal_survives_a_mixed_calendar_panel():
    """The regression this module exists for.

    Two columns on deliberately different trading calendars, unioned as
    the real book is (NYSE + Xetra + Shenzhen + 24x7 crypto). A whole-frame
    rolling(MA_PERIOD, min_periods=MA_PERIOD) returns all-NaN here; the
    per-column path must not.
    """
    a = _series(600, "2020-01-01", "C", seed=1)          # 5-day week
    b = _series(700, "2020-01-01", "D", seed=2)          # 7-day week
    union = a.index.union(b.index)
    panel = pd.DataFrame({"A": a.reindex(union), "B": b.reindex(union)})

    # Any column observed on a SUBSET of the union index is wiped by the
    # whole-frame path: its every 200-row window contains the other
    # calendar's dates as NaN, so min_periods is never satisfied. That is
    # the real book's situation for every NYSE line once a 24x7 crypto
    # line joins the panel.
    naive = panel.rolling(MA_PERIOD, min_periods=MA_PERIOD).mean()
    assert not naive["A"].notna().any(), (
        "premise broken: the whole-frame path was expected to collapse "
        "the subset-calendar column")

    sig = weekly_signal(panel)
    assert sig["A"].notna().sum() > 50
    assert sig["B"].notna().sum() > 50


def test_empty_signal_panel_raises_rather_than_returning_nan():
    """No column has MA_PERIOD observations -> refuse, do not gate on air."""
    short = _series(MA_PERIOD - 10, "2020-01-01", "C", seed=3)
    panel = short.rename("A").to_frame()
    with pytest.raises(RuntimeError, match="signal panel is empty"):
        weekly_signal(panel)


def test_pairwise_skips_pairs_without_enough_overlap():
    """A short-history line must not contribute a 3-point correlation."""
    long = _series(600, "2020-01-01", "C", seed=4).rename("LONG")
    short = long.iloc[-20:].rename("SHORT")
    panel = pd.concat([long, short], axis=1).sort_index()
    wret = weekly_returns(panel)
    assert pairwise(wret["SHORT"], wret[["LONG"]]) == []


def test_identical_series_correlate_at_one_on_both_bases():
    """Sanity anchor for the thresholds: a duplicated line saturates both."""
    s = _series(600, "2020-01-01", "C", seed=5)
    panel = pd.concat([s.rename("X"), s.rename("Y")], axis=1)
    assert pairwise(weekly_returns(panel)["X"],
                    weekly_returns(panel)[["Y"]])[0][1] == pytest.approx(1.0)
    assert pairwise(weekly_signal(panel)["X"],
                    weekly_signal(panel)[["Y"]])[0][1] == pytest.approx(1.0)
    assert SIGNAL_GATE_MAX_CORR < 1.0 and OVERLAP_RULE_MAX_CORR < 1.0


def test_proxy_identity_pairs_cover_the_known_a_sleeve_duplicates():
    """CSP1/SPY, CNDX/QQQ, IDP6/IJR are priced as one series under two names.

    The audit must label these rather than report them as measured
    overlaps; if the registry's proxy mapping changes, this test is the
    thing that notices.
    """
    pairs = proxy_identity_pairs()
    for engine, proxy in (("CSP1", "SPY"), ("CNDX", "QQQ"), ("IDP6", "IJR"),
                          ("IUSP", "XLRE")):
        assert frozenset({engine, proxy}) in pairs, (
            f"{engine} -> {proxy} is no longer a registry trading proxy")


# ---------------------------------------------------------------------------
# Capture integrity (WS8 reopen condition: "a truncated panel reads as
# 'no breaches'"). These pin which failures stop the audit and which only
# annotate it — the distinction is the whole point of the guard.
# ---------------------------------------------------------------------------

def _panel(cols: dict[str, pd.Series]) -> pd.DataFrame:
    return pd.DataFrame(cols).sort_index()


def _book(n: int = 400, start: str = "2020-01-01") -> pd.DataFrame:
    return _panel({"AAA": _series(n, start, "C", seed=11),
                   "BBB": _series(n, start, "C", seed=12)})


def test_capture_integrity_passes_a_well_formed_panel():
    p = _book()
    rep = capture_integrity(p, weekly_returns(p), {"AAA": ["A"], "BBB": ["B"]})
    assert rep["n_lines_expected"] == rep["n_lines_in_panel"] == 2
    assert rep["pairs_skipped_for_thin_overlap"] == 0
    assert rep["warnings"] == []


def test_a_resolved_line_missing_from_the_panel_raises():
    """The pairs of an absent line vanish from the breach list in silence."""
    p = _book()
    sleeves = {"AAA": ["A"], "BBB": ["B"], "CCC": ["C"]}
    with pytest.raises(RuntimeError, match="absent from the panel"):
        capture_integrity(p, weekly_returns(p), sleeves)


def test_a_present_but_empty_line_raises():
    p = _book()
    p["CCC"] = np.nan
    with pytest.raises(RuntimeError, match="no observations"):
        capture_integrity(p, weekly_returns(p),
                          {"AAA": ["A"], "BBB": ["B"], "CCC": ["C"]})


def test_truncation_that_starves_a_long_pair_raises():
    """The WS8 failure mode itself.

    Both legs carry years of their own history, but they are truncated onto
    disjoint spans, so their overlap falls under MIN_PAIR_WEEKS and the pair
    is skipped. That is an alignment bug, not a young line, and the audit
    would silently report one fewer pair.
    """
    a = _series(400, "2015-01-01", "C", seed=21)
    b = _series(400, "2022-01-01", "C", seed=22)
    p = _panel({"AAA": a, "BBB": b})
    with pytest.raises(RuntimeError, match="both legs individually carry"):
        capture_integrity(p, weekly_returns(p), {"AAA": ["A"], "BBB": ["B"]})


def test_a_genuinely_young_line_is_reported_not_raised():
    """A short history is the legitimate reason to skip a pair."""
    a = _series(400, "2020-01-01", "C", seed=31)
    b = _series(20, "2020-01-01", "C", seed=32)      # under MIN_PAIR_WEEKS
    p = _panel({"AAA": a, "BBB": b})
    rep = capture_integrity(p, weekly_returns(p), {"AAA": ["A"], "BBB": ["B"]})
    assert rep["pairs_skipped_for_thin_overlap"] == 1
    assert rep["pairs_skipped_despite_both_legs_long"] == []


def test_staleness_warns_and_does_not_raise():
    """A line weeks behind loses a handful of weekly observations out of
    hundreds; it cannot push a pair below the floor, so it must not block the
    audit. It must still be visible — this is the live EEM case."""
    a = _series(400, "2020-01-01", "C", seed=41)
    b = _series(400 - 30, "2020-01-01", "C", seed=42)
    p = _panel({"AAA": a, "BBB": b})
    days_behind = (a.index.max() - b.index.max()).days
    assert days_behind > STALE_TOL_DAYS, "fixture must actually be stale"
    rep = capture_integrity(p, weekly_returns(p), {"AAA": ["A"], "BBB": ["B"]})
    assert [w["line"] for w in rep["stale_lines"]] == ["BBB"]
    assert len(rep["warnings"]) == 1 and "BBB" in rep["warnings"][0]
