"""Tests for the isolated-gap price repair.

The repair exists because the vendor served no BTC-USD bar for Fri
2026-08-21 and had not backfilled a day later. With the 200-session
amplification already fixed in price_panel_guard, the remaining cost is
bounded but real: a missing close on a ranking date drops the name from that
rebalance, and BTC-USD is held in 95 of 212 sleeve-C rebalances.

THE TEST THAT MATTERS MOST is test_a_level_splice_would_have_been_wrong.
These caches are not raw vendor prices -- crypto is reindexed onto the equity
calendar, non-USD lines are FX-converted, and synthetic proxies carry a
modelled expense ratio (BTC-USD, 25bps/yr since inception). By 2026-08-20 the
cached series sat 2.19% BELOW raw spot, deliberately and growing. Dropping a
raw secondary close in at LEVEL would have printed a +2.2% jump on a sleeve
whose eligibility floor is +5%. Splicing the RETURN inherits the basis and
cancels any constant offset.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from scripts.repair_price_gaps import (
    MAX_PLAUSIBLE_MOVE,
    RepairError,
    find_gaps,
    splice_value,
)


def _frame(n=40, holes=(), ticker="BTC-USD", n_peers=6):
    idx = pd.bdate_range("2026-06-01", periods=n)
    data = {ticker: np.linspace(100.0, 200.0, n)}
    for i in range(n_peers):
        data[f"PEER{i}"] = np.linspace(10.0 + i, 20.0 + i, n)
    df = pd.DataFrame(data, index=idx)
    for h in holes:
        df.iloc[h, df.columns.get_loc(ticker)] = np.nan
    return df


# ---------------------------------------------------------------------------
# find_gaps
# ---------------------------------------------------------------------------
def test_finds_an_isolated_gap_and_its_previous_session():
    df = _frame(holes=(30,))
    gaps = find_gaps(df, "BTC-USD")
    assert len(gaps) == 1
    gap, prev = gaps[0]
    assert gap == df.index[30]
    assert prev == df.index[29]


def test_a_run_of_holes_is_not_repaired():
    """A single absent print is a hiccup; a run is an outage or a delisting,
    and inventing a week of prices from a second venue is a different and much
    worse decision."""
    df = _frame(holes=(30, 31, 32))
    assert find_gaps(df, "BTC-USD") == []


def test_two_separate_isolated_gaps_are_both_found():
    df = _frame(holes=(25, 32))
    gaps = find_gaps(df, "BTC-USD")
    assert [g.date().isoformat() for g, _ in gaps] == \
        [df.index[25].date().isoformat(), df.index[32].date().isoformat()]


def test_adjacency_is_measured_on_sessions_not_calendar_days():
    """A weekend between two holes still makes them consecutive. Measuring in
    calendar days would call a Friday+Monday pair isolated and fill both."""
    df = _frame(n=40)
    idx = df.index
    fri = next(i for i, d in enumerate(idx) if d.dayofweek == 4 and i > 20)
    df.iloc[fri, df.columns.get_loc("BTC-USD")] = np.nan
    df.iloc[fri + 1, df.columns.get_loc("BTC-USD")] = np.nan   # the Monday
    assert (idx[fri + 1] - idx[fri]).days == 3                 # a weekend apart
    assert find_gaps(df, "BTC-USD") == []


def test_a_gap_with_no_prior_value_is_skipped():
    df = _frame(holes=(0,))
    assert find_gaps(df, "BTC-USD") == []


def test_no_gaps_when_the_series_is_complete():
    assert find_gaps(_frame(), "BTC-USD") == []


def test_a_session_nobody_priced_is_not_a_gap():
    """If the peers did not price either, the panel did not trade -- that is a
    holiday, not a hole in this ticker."""
    df = _frame(n=40)
    df.iloc[30, :] = np.nan
    assert find_gaps(df, "BTC-USD") == []


def test_lookback_bounds_how_far_back_it_will_reach():
    """Old gaps are already baked into a published record; repairing them
    would restate history silently."""
    df = _frame(n=60, holes=(5,))
    assert find_gaps(df, "BTC-USD", lookback=10) == []
    assert len(find_gaps(df, "BTC-USD", lookback=0)) == 1


def test_an_unknown_ticker_is_not_an_error():
    assert find_gaps(_frame(), "NOTHERE") == []


# ---------------------------------------------------------------------------
# splice_value — returns, never levels
# ---------------------------------------------------------------------------
def test_it_carries_the_return_not_the_level():
    got = splice_value(prev_value=100.0, sec_prev=200.0, sec_now=210.0)
    assert got == pytest.approx(105.0)


def test_a_level_splice_would_have_been_wrong():
    """THE REASON THIS MODULE IS BUILT THE WAY IT IS.

    The real 2026-08-21 numbers: the cached BTC-USD series sits 2.19% below
    raw spot because of the modelled expense ratio. A level splice prints the
    secondary's own price and injects that entire basis as a one-day move.
    """
    cached_thu, sec_thu, sec_fri = 71_471.02, 73_025.15, 78_338.03
    spliced = splice_value(cached_thu, sec_thu, sec_fri)

    implied_by_return = spliced / cached_thu - 1
    implied_by_level = sec_fri / cached_thu - 1
    assert implied_by_return == pytest.approx(0.0728, abs=5e-4)
    assert implied_by_level == pytest.approx(0.0961, abs=5e-4)
    # The level route overstates the move by roughly the fee basis.
    assert implied_by_level - implied_by_return == pytest.approx(0.0233, abs=1e-3)
    assert spliced < sec_fri, "the repair must stay on the cache's basis"


def test_a_constant_offset_between_sources_cancels():
    """Exchange spread, USDT peg, fee basis -- any constant factor drops out
    of the ratio, which is why the sources need not agree on level."""
    base = splice_value(100.0, 200.0, 210.0)
    for factor in (0.5, 1.03, 7.0):
        assert splice_value(100.0, 200.0 * factor, 210.0 * factor) == \
            pytest.approx(base)


def test_an_implausible_move_is_refused_rather_than_printed():
    with pytest.raises(RepairError, match="plausibility"):
        splice_value(100.0, 100.0, 100.0 * (1 + MAX_PLAUSIBLE_MOVE + 0.01))


def test_the_plausibility_bound_is_two_sided():
    with pytest.raises(RepairError, match="plausibility"):
        splice_value(100.0, 100.0, 100.0 * (1 - MAX_PLAUSIBLE_MOVE - 0.01))


def test_a_move_just_inside_the_bound_is_allowed():
    v = splice_value(100.0, 100.0, 100.0 * (1 + MAX_PLAUSIBLE_MOVE - 0.001))
    assert v == pytest.approx(100.0 * (1 + MAX_PLAUSIBLE_MOVE - 0.001))


def test_non_positive_inputs_are_refused():
    for args in ((0.0, 100.0, 110.0), (100.0, 0.0, 110.0), (100.0, 100.0, 0.0),
                 (-1.0, 100.0, 110.0)):
        with pytest.raises(RepairError):
            splice_value(*args)


def test_a_flat_secondary_reproduces_the_previous_value():
    assert splice_value(123.45, 500.0, 500.0) == pytest.approx(123.45)


# ---------------------------------------------------------------------------
# The write gate
# ---------------------------------------------------------------------------
def test_report_mode_writes_nothing(tmp_path, monkeypatch):
    """Filling a price the book ranks on is state-changing, so the default
    must be a report. This pins that the cache is untouched without --apply."""
    import scripts.repair_price_gaps as rp

    df = _frame(holes=(30,))
    cache = tmp_path / "thematic_prices_cache.parquet"
    df.to_parquet(cache)
    before = cache.read_bytes()

    monkeypatch.setattr(rp, "DATA_DIR", tmp_path)
    monkeypatch.setattr(rp, "fetch_primary",
                        lambda *a, **k: pd.Series(dtype=float))
    monkeypatch.setattr(rp, "fetch_secondary",
                        lambda *a, **k: (pd.Series(dtype=float), ""))
    rp.repair_cache("thematic", only_ticker="BTC-USD", apply=False)
    assert cache.read_bytes() == before


def test_a_gap_with_no_usable_source_is_reported_not_invented(tmp_path,
                                                              monkeypatch):
    import scripts.repair_price_gaps as rp

    df = _frame(holes=(30,))
    (tmp_path / "thematic_prices_cache.parquet").write_bytes(b"")
    df.to_parquet(tmp_path / "thematic_prices_cache.parquet")
    monkeypatch.setattr(rp, "DATA_DIR", tmp_path)
    monkeypatch.setattr(rp, "fetch_primary",
                        lambda *a, **k: pd.Series(dtype=float))
    monkeypatch.setattr(rp, "fetch_secondary",
                        lambda *a, **k: (pd.Series(dtype=float), ""))
    reps = rp.repair_cache("thematic", only_ticker="BTC-USD", apply=False)
    assert len(reps) == 1
    assert "refused" in reps[0]
    assert reps[0].get("value") is None


def test_the_primary_is_preferred_over_the_secondary(tmp_path, monkeypatch):
    """Most holes backfill. A secondary splice is a fallback, never the first
    answer, so a repair never introduces a second source unnecessarily."""
    import scripts.repair_price_gaps as rp

    df = _frame(holes=(30,))
    df.to_parquet(tmp_path / "thematic_prices_cache.parquet")
    gap = df.index[30]
    monkeypatch.setattr(rp, "DATA_DIR", tmp_path)
    monkeypatch.setattr(rp, "fetch_primary",
                        lambda *a, **k: pd.Series({gap: 4242.0}))
    monkeypatch.setattr(rp, "fetch_secondary",
                        lambda *a, **k: (pd.Series({gap: 1.0}), "binance:X"))
    (rec,) = rp.repair_cache("thematic", only_ticker="BTC-USD", apply=False)
    assert rec["source"] == "primary:yfinance"
    assert rec["value"] == 4242.0
