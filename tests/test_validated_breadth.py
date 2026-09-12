"""The production engine and live card share a fail-closed signal cutoff."""
from pathlib import Path
import json
import sys

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from validated_breadth import validated_end, cap_signal
from run_portfolio import run_portfolio


def test_missing_boundary_fails_closed(tmp_path):
    with pytest.raises(FileNotFoundError):
        validated_end(tmp_path, "TEST")


@pytest.mark.parametrize("previous,newest", [
    ("2026-08-31", "2026-09-01"), ("2026-12-31", "2027-01-04")])
def test_cap_preserves_history_and_masks_newer_raw_row(tmp_path, previous, newest):
    # Python months are 1-indexed; pandas parses the boundary dates.
    (tmp_path / "breadth_test.json").write_text(json.dumps({"end_date": previous}))
    series = pd.Series([0.4, 1.0], index=pd.to_datetime([previous, newest]))
    out = cap_signal(series, validated_end(tmp_path, "TEST"))
    assert out.iloc[0] == series.iloc[0]
    assert pd.isna(out.iloc[1])
    assert series.iloc[1] == 1.0


def test_fully_validated_engine_has_exact_parity():
    dates = pd.bdate_range("2026-08-03", "2026-08-21")
    closes = pd.DataFrame({"X": range(100, 115), "Y": range(115, 130)}, index=dates)
    signals = pd.DataFrame({"X": 0.9, "Y": 0.1}, index=dates)
    rank = lambda row: pd.Series({"X": 1.0, "Y": 0.0})
    baseline = run_portfolio(closes, signals, rank, dates[0])
    bounded = signals.copy()
    bounded.attrs["validated_through"] = dates[-1].isoformat()
    actual = run_portfolio(closes, bounded, rank, dates[0])
    pd.testing.assert_series_equal(baseline["equity"], actual["equity"], check_exact=True)
    pd.testing.assert_frame_equal(baseline["weights"], actual["weights"], check_exact=True)


def test_unverified_tail_retains_previous_basket_not_stale_rank_or_cash():
    dates = pd.bdate_range("2026-08-03", "2026-08-24")
    closes = pd.DataFrame({"X": range(100, 116), "Y": range(116, 132)}, index=dates)
    signals = pd.DataFrame({"X": 0.9, "Y": 0.1}, index=dates)
    signals.loc["2026-08-21", ["X", "Y"]] = [0.0, 1.0]
    signals.attrs["validated_through"] = "2026-08-20"
    called = []

    def rank(row):
        called.append(row.name)
        return pd.Series({"X": float(row["X"] > row["Y"]),
                          "Y": float(row["Y"] >= row["X"])})

    result = run_portfolio(closes, signals, rank, dates[0])
    assert pd.Timestamp("2026-08-21") not in called
    assert result["weights"].loc["2026-08-24"].to_dict() == {"X": 1.0, "Y": 0.0}
    assert pd.Timestamp("2026-08-24") not in result["rebalance_dates"]
