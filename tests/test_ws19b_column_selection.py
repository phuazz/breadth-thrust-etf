"""WS19b — a price column may never mix two sources.

THE DEFECT THIS PINS (WS19, 2026-08-30). The first attempt filled Norgate's
missing cells from yfinance PER CELL. On a name where the two sources disagree
on level — AZN's ratio spans 0.96 to 1.12 about a 1.011 median — every junction
between them fabricates a day-to-day return of several per cent, out of nothing.
Those are not prices; they are artefacts of the join. A price basis may not
change part-way down a column.

THE RULE. Take Norgate's column only when its observed dates are a SUPERSET of
the incumbent's, and then take the WHOLE column. Otherwise keep the incumbent
whole. Two consequences, and both are the point:

  C1  no output column contains values from both sources — by construction,
      which is what these tests hold to
  C2  n_with_price can never fall, since a replacement column covers every date
      the incumbent covered — also by construction, not by measurement

The superset test is STRONGER than WS19b as registered ("at least as
complete"). A count comparison would let a column with more observations still
drop dates the incumbent had, which would break C2. Tightening a
pre-registered criterion is disclosed in the record; it makes strictly fewer
columns qualify.

Precedent for the column as the unit: the WS15 vendor step-defect guard already
reverts whole columns, for exactly this reason.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import compute_breadth as cb  # noqa: E402

DATES = pd.bdate_range("2024-01-01", periods=12)


def _yf_frame(cols: dict[str, pd.Series]) -> pd.DataFrame:
    """Shape yf.download returns under group_by='column'."""
    return pd.concat({"Close": pd.DataFrame(cols)}, axis=1)


@pytest.fixture
def sources(monkeypatch, tmp_path):
    """yfinance has every date; Norgate is a superset on A, short a date on B,
    and absent on C. Levels differ by 10x so any mix is unmissable."""
    yf_cols = {t: pd.Series(np.linspace(100.0, 111.0, len(DATES)), index=DATES)
               for t in ("A", "B", "C")}

    ng = pd.DataFrame({
        "A": pd.Series(np.linspace(1000.0, 1110.0, len(DATES)), index=DATES),
        "B": pd.Series(np.linspace(1000.0, 1110.0, len(DATES)), index=DATES),
    })
    ng.loc[DATES[5], "B"] = np.nan          # one date the incumbent has

    monkeypatch.setattr(cb, "yf", type("Y", (), {
        "download": staticmethod(lambda *a, **k: _yf_frame(yf_cols))})())

    import norgate_prices
    monkeypatch.setattr(norgate_prices, "available", lambda: True)
    monkeypatch.setattr(
        norgate_prices, "fetch_closes",
        lambda tickers, start, end, verbose=True: (ng, ["A", "B"], ["C"]))
    return tmp_path / "cache.parquet"


def _run(cache):
    return cb.download_prices(["A", "B", "C"], "2024-01-01", "2024-01-20",
                              cache_path=cache, price_source="auto")


def test_superset_column_is_taken_whole(sources):
    out = _run(sources)
    # A: Norgate covers every incumbent date, so the WHOLE column is Norgate.
    assert out["A"].dropna().between(999, 1111).all(), \
        "column A mixes sources — a yfinance-level value survived"


def test_non_superset_column_is_left_alone(sources):
    out = _run(sources)
    # B: Norgate is short one date the incumbent had, so B stays yfinance
    # ENTIRELY. The tempting move -- take Norgate's 11 dates and fill the 12th
    # from yfinance -- is the defect.
    assert out["B"].dropna().between(99, 112).all(), \
        "column B mixes sources — this is the WS19 per-cell defect returning"
    assert out["B"].notna().sum() == len(DATES)


def test_unresolved_column_is_untouched(sources):
    out = _run(sources)
    assert out["C"].dropna().between(99, 112).all()


def test_no_column_contains_both_sources(sources):
    """C1 stated directly, over every column at once."""
    out = _run(sources)
    for t in out.columns:
        v = out[t].dropna()
        if v.empty:
            continue
        low = v.between(99, 112)      # yfinance band
        high = v.between(999, 1111)   # Norgate band
        assert low.all() or high.all(), (
            f"column {t} draws from BOTH sources: "
            f"{int(low.sum())} yfinance-band and {int(high.sum())} "
            f"Norgate-band values in one column")


def test_priced_count_never_falls(sources):
    """C2 by construction: no date loses a price to the swap."""
    out = _run(sources)
    for t in ("A", "B", "C"):
        assert out[t].notna().sum() >= len(DATES), \
            f"{t} lost observations to the source selection"


def test_default_source_leaves_the_frame_alone(sources):
    """The deployed path must be untouched by any of this."""
    out = cb.download_prices(["A", "B", "C"], "2024-01-01", "2024-01-20",
                             cache_path=sources, price_source="yfinance")
    for t in ("A", "B", "C"):
        assert out[t].dropna().between(99, 112).all(), \
            "default run reached the Norgate path"


# ---------------------------------------------------------------------------
# The shared rule, as used by sleeves B and C (2026-08-30). Same superset
# logic, exercised through norgate_prices.select_columns rather than through
# compute_breadth's frame, because the two engines call it directly.
# ---------------------------------------------------------------------------

def test_select_columns_takes_superset_and_keeps_the_rest(monkeypatch):
    import norgate_prices as npx

    base = pd.DataFrame({
        "TAKE": pd.Series(np.linspace(100.0, 111.0, len(DATES)), index=DATES),
        "KEEP": pd.Series(np.linspace(100.0, 111.0, len(DATES)), index=DATES),
        "NONE": pd.Series(np.linspace(100.0, 111.0, len(DATES)), index=DATES),
    })
    ng = pd.DataFrame({
        "TAKE": pd.Series(np.linspace(1000.0, 1110.0, len(DATES)), index=DATES),
        "KEEP": pd.Series(np.linspace(1000.0, 1110.0, len(DATES)), index=DATES),
    })
    ng.loc[DATES[3], "KEEP"] = np.nan     # one date the incumbent has

    monkeypatch.setattr(npx, "available", lambda: True)
    monkeypatch.setattr(npx, "fetch_closes",
                        lambda t, s, e, verbose=True: (ng, ["TAKE", "KEEP"], ["NONE"]))

    out, rep = npx.select_columns(base, list(base.columns), "2024-01-01",
                                  "2024-01-20", verbose=False)
    assert rep["replaced"] == ["TAKE"]
    assert rep["kept"] == ["KEEP"]
    assert rep["unresolved"] == ["NONE"]
    assert out["TAKE"].dropna().between(999, 1111).all()
    assert out["KEEP"].dropna().between(99, 112).all(), \
        "KEEP mixed sources — the per-cell defect in the shared helper"
    assert out["NONE"].dropna().between(99, 112).all()


def test_select_columns_is_inert_without_the_feed(monkeypatch):
    """Every CI runner takes this path; it must change nothing."""
    import norgate_prices as npx
    base = pd.DataFrame({"A": pd.Series(np.arange(len(DATES), dtype=float),
                                        index=DATES)})
    monkeypatch.setattr(npx, "available", lambda: False)
    out, rep = npx.select_columns(base, ["A"], "2024-01-01", "2024-01-20",
                                  verbose=False)
    assert rep["status"] == "unavailable"
    pd.testing.assert_frame_equal(out, base)


def test_priced_count_cannot_fall_through_select_columns(monkeypatch):
    """C2, stated on the shared helper."""
    import norgate_prices as npx
    base = pd.DataFrame({
        "A": pd.Series(np.linspace(100.0, 111.0, len(DATES)), index=DATES),
    })
    ng = pd.DataFrame({
        "A": pd.Series(np.linspace(1000.0, 1110.0, len(DATES)), index=DATES),
    })
    monkeypatch.setattr(npx, "available", lambda: True)
    monkeypatch.setattr(npx, "fetch_closes",
                        lambda t, s, e, verbose=True: (ng, ["A"], []))
    out, _ = npx.select_columns(base, ["A"], "2024-01-01", "2024-01-20",
                                verbose=False)
    assert out["A"].notna().sum() >= base["A"].notna().sum()


# ---------------------------------------------------------------------------
# Sleeve A / D OHLC site (backtest.download_soxx_ohlc, 2026-08-30). Whole
# frame or nothing, on the same superset test. This one site serves both
# sleeves: D's Xetra lines resolve to None at Norgate, so they keep yfinance
# with no sleeve-specific branch. That is the isolated treatment, and these
# tests are what hold it in place.
# ---------------------------------------------------------------------------

def _ohlc(idx, level):
    """A VARYING series -- backtest's degenerate-price guard rightly refuses a
    flat one, so the fixture has to be a real price path. Levels are an order
    of magnitude apart so the source of any bar is unmistakable."""
    base = np.linspace(float(level), float(level) * 1.1, len(idx))
    return pd.DataFrame({"Open": base, "High": base * 1.01,
                         "Low": base * 0.99, "Close": base}, index=idx)


@pytest.fixture
def ohlc_site(monkeypatch, tmp_path):
    import backtest as bt
    yf_frame = _ohlc(DATES, 100.0)
    monkeypatch.setattr(bt, "yf", type("Y", (), {
        "download": staticmethod(lambda *a, **k: yf_frame.copy())})())
    monkeypatch.setattr(bt, "paths_for",
                        lambda etf: {"ohlc_cache": tmp_path / f"{etf}.parquet"})
    return bt


def test_ohlc_default_stays_on_yfinance(ohlc_site, monkeypatch):
    monkeypatch.delenv("BTE_PRICE_SOURCE", raising=False)
    out = ohlc_site.download_soxx_ohlc("2024-01-01", "2024-01-20", etf="TEST")
    assert out["Close"].between(99, 112).all()


def test_ohlc_superset_is_taken_whole(ohlc_site, monkeypatch):
    import norgate_prices as npx
    monkeypatch.setenv("BTE_PRICE_SOURCE", "norgate")
    monkeypatch.setattr(npx, "fetch_ohlc",
                        lambda t, s, e: _ohlc(DATES, 1000.0))
    out = ohlc_site.download_soxx_ohlc("2024-01-01", "2024-01-20", etf="TEST")
    assert out["Close"].between(999, 1111).all(), "Norgate frame was not taken"


def test_ohlc_non_superset_is_refused(ohlc_site, monkeypatch):
    """One missing date and the whole frame is refused -- no splicing."""
    import norgate_prices as npx
    monkeypatch.setenv("BTE_PRICE_SOURCE", "norgate")
    short = _ohlc(DATES.drop(DATES[4]), 1000.0)
    monkeypatch.setattr(npx, "fetch_ohlc", lambda t, s, e: short)
    out = ohlc_site.download_soxx_ohlc("2024-01-01", "2024-01-20", etf="TEST")
    assert out["Close"].between(99, 112).all(), \
        "a non-superset Norgate frame was spliced in"


def test_ohlc_unresolved_keeps_the_incumbent(ohlc_site, monkeypatch):
    """Sleeve D's case: Norgate has no such security, so nothing changes."""
    import norgate_prices as npx
    monkeypatch.setenv("BTE_PRICE_SOURCE", "norgate")
    monkeypatch.setattr(npx, "fetch_ohlc", lambda t, s, e: None)
    out = ohlc_site.download_soxx_ohlc("2024-01-01", "2024-01-20", etf="TEST")
    assert out["Close"].between(99, 112).all()
