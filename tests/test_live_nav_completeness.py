"""Daily NAV publication requires observed prices and FX, with no vendor calls.

All prices are synthetic. Python/Pandas months are 1-indexed.
"""
import json
from datetime import date, datetime

import pandas as pd
import pytest

from scripts import mark_to_market_live as mtm
from scripts.check_anchor_consistency import check_anchor
from scripts.check_capture_integrity import evaluate_target
from scripts.nyse_sessions import last_completed_session, sessions_between


def project(frame, weights=None, anchor="2026-11-03", cap="2026-11-04"):
    return mtm._project_daily_equity(
        weights or {"SPY": 0.8, "EXH1": 0.2}, 2.0, frame,
        pd.Timestamp(anchor), session_cap=pd.Timestamp(cap),
        valid_sessions=sessions_between(date.fromisoformat(anchor), date.fromisoformat(cap)))


@pytest.mark.parametrize("missing", [None, float("nan"), float("inf"), 0, -1])
def test_a_populated_us_close_cannot_hide_a_missing_europe_close(missing):
    frame = pd.DataFrame({"SPY": [100, 110], "EXH1": [50, missing]},
                         index=pd.to_datetime(["2026-11-03", "2026-11-04"]))
    assert project(frame) == ([], [])


@pytest.mark.parametrize("kind", ["column", "row", "nulls"])
def test_absent_completed_session_data_never_advances_nav(kind):
    frame = pd.DataFrame({"SPY": [100, 110], "EXH1": [50, 55]},
                         index=pd.to_datetime(["2026-11-03", "2026-11-04"]))
    if kind == "column":
        frame = frame.drop(columns="EXH1")
    elif kind == "row":
        frame = frame.iloc[:1]
    else:
        frame.loc["2026-11-04"] = float("nan")
    assert project(frame) == ([], [])


def test_later_valid_prices_recover_from_the_same_anchor_without_mutating_inputs():
    frame = pd.DataFrame({"SPY": [100, 110, 120], "EXH1": [50, None, 60]},
                         index=pd.to_datetime(["2026-11-03", "2026-11-04", "2026-11-05"]))
    before = frame.copy(deep=True)
    assert project(frame, cap="2026-11-05") == ([], [])
    pd.testing.assert_frame_equal(before, frame)
    frame.loc["2026-11-04", "EXH1"] = 55
    dates, nav = project(frame, cap="2026-11-05")
    assert dates == ["2026-11-04", "2026-11-05"]
    assert nav == pytest.approx([2.2, 2.4])
    assert project(frame, cap="2026-11-05") == (dates, nav)


def test_missing_anchor_is_not_silently_shifted_to_an_earlier_trading_day():
    frame = pd.DataFrame({"SPY": [100, 110]},
                         index=pd.to_datetime(["2026-11-02", "2026-11-04"]))
    assert project(frame, {"SPY": 1}) == ([], [])


def test_zero_weight_instrument_does_not_block_a_valid_mark_or_change_cash():
    frame = pd.DataFrame({"SPY": [100, 110]},
                         index=pd.to_datetime(["2026-11-03", "2026-11-04"]))
    dates, nav = project(frame, {"SPY": 0.5, "ABSENT": 0})
    assert dates == ["2026-11-04"]
    assert nav == pytest.approx([2.1])


@pytest.mark.parametrize("anchor,cap", [("2026-04-30", "2026-05-01"),
                                       ("2025-12-30", "2025-12-31")])
def test_calendar_proven_xetra_holiday_carries_the_last_required_mark(anchor, cap):
    frame = pd.DataFrame({"SPY": [100, 110], "EXH1": [50, None]},
                         index=pd.to_datetime([anchor, cap]))
    dates, nav = project(frame, anchor=anchor, cap=cap)
    assert dates == [cap]
    assert nav == pytest.approx([2.16])


@pytest.mark.parametrize("instant,expected", [
    ("2026-10-30T19:59:00+00:00", "2026-10-29"),
    ("2026-10-30T20:00:00+00:00", "2026-10-30"),
    ("2026-11-02T20:59:00+00:00", "2026-10-30"),
    ("2026-11-02T21:00:00+00:00", "2026-11-02"),
    ("2027-01-01T23:00:00+00:00", "2026-12-31"),
])
def test_completed_session_boundaries_cover_dst_month_and_year(instant, expected):
    assert last_completed_session(datetime.fromisoformat(instant)).isoformat() == expected


@pytest.mark.parametrize("fx,symbol,ticker", [("EURUSD=X", "EXH1.DE", "EXH1"),
                                            ("USDCNY=X", "159801.SZ", "159801.SZ")])
@pytest.mark.parametrize("hole", ["column", "null", "zero", "infinite"])
def test_missing_or_invalid_fx_cannot_establish_a_fresh_nav(monkeypatch, fx, symbol, ticker, hole):
    raw = pd.DataFrame({symbol: [100, 110], fx: [1.1, 1.2]},
                       index=pd.to_datetime(["2026-11-03", "2026-11-04"]))
    if hole == "column":
        raw = raw.drop(columns=fx)
    else:
        raw.loc["2026-11-04", fx] = {"null": float("nan"), "zero": 0, "infinite": float("inf")}[hole]
    monkeypatch.setattr(mtm, "_yf_close_series", lambda *a: raw.copy())
    prices = mtm._fetch_usd_prices({ticker: 1}, "2026-11-03", {})
    assert project(prices, {ticker: 1}) == ([], [])


def test_restored_fx_allows_conversion_and_later_recovery(monkeypatch):
    raw = pd.DataFrame({"EXH1.DE": [100, 110], "EURUSD=X": [1.1, None]},
                       index=pd.to_datetime(["2026-11-03", "2026-11-04"]))
    monkeypatch.setattr(mtm, "_yf_close_series", lambda *a: raw.copy())
    prices = mtm._fetch_usd_prices({"EXH1": 1}, "2026-11-03", {})
    # FX provenance must also survive a caller slicing the price frame.
    prices = prices.loc[prices.index <= pd.Timestamp("2026-11-04")]
    assert project(prices, {"EXH1": 1}) == ([], [])
    raw.loc["2026-11-04", "EURUSD=X"] = 1.2
    prices = mtm._fetch_usd_prices({"EXH1": 1}, "2026-11-03", {})
    dates, nav = project(prices, {"EXH1": 1})
    assert dates == ["2026-11-04"]
    assert nav == pytest.approx([2.4])


def test_incomplete_mark_keeps_capture_warning_and_anchor_protection(tmp_path):
    frame = pd.DataFrame({"SPY": [100, 110], "EXH1": [50, None]},
                         index=pd.to_datetime(["2026-11-03", "2026-11-04"]))
    dates, nav = project(frame)
    live = {"anchor_date": "2026-11-03", "anchor_equity": 2.0,
            "deployed_key": "test", "live_dates": dates, "live_equity": nav}
    path = tmp_path / "live.json"
    path.write_text(json.dumps(live))
    verdict = evaluate_target("live", path, ("live_dates",), ("live_equity",),
                              date(2026, 11, 4), anchor_path=("anchor_date",))
    assert verdict["status"] == "warn"
    overlay = {"gated_variants": {"test": {"dates": ["2026-11-03"], "equity": [2.0]}}}
    assert check_anchor(live, overlay)[0] == "ok"
    live["anchor_equity"] = 2.01
    assert check_anchor(live, overlay)[0] == "fail"


def test_unknown_venue_cannot_authorise_carry():
    frame = pd.DataFrame({"UNKNOWN?": [100, None]},
                         index=pd.to_datetime(["2026-11-03", "2026-11-04"]))
    assert project(frame, {"UNKNOWN?": 1}) == ([], [])


@pytest.mark.parametrize("anchor,cap", [("2026-04-30", "2026-05-01"),
                                       ("2025-12-30", "2025-12-31")])
def test_populated_xetra_holiday_row_cannot_replace_the_required_close(anchor, cap):
    frame = pd.DataFrame({"SPY": [100, 110], "EXH1": [50, 500]},
                         index=pd.to_datetime([anchor, cap]))
    assert project(frame, anchor=anchor, cap=cap)[1] == pytest.approx([2.16])
    frame.loc[anchor, "EXH1"] = float("nan")
    assert project(frame, anchor=anchor, cap=cap) == ([], [])


def test_native_calendar_conflict_cannot_authorise_a_mark(monkeypatch):
    import native_sessions

    def unavailable(*args, **kwargs):
        raise ValueError("calendar pin conflict")

    monkeypatch.setattr(native_sessions, "native_schedule", unavailable)
    frame = pd.DataFrame({"EXH1": [50, 55]},
                         index=pd.to_datetime(["2026-11-03", "2026-11-04"]))
    assert project(frame, {"EXH1": 1}) == ([], [])


@pytest.fixture
def offline_main(monkeypatch, tmp_path):
    """Exercise the real publication path with synthetic inputs, without fetching."""
    from datetime import timezone

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 5, 5, tzinfo=timezone.utc)

    monkeypatch.setattr(mtm, "datetime", Clock)
    monkeypatch.setattr(mtm, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(mtm, "DATA_DIR", tmp_path)
    monkeypatch.setattr(mtm, "tilt_display_state", lambda *a: {
        "active": False, "label": "off", "stale": False, "signal_as_of": "2026-11-03"})
    for name in ["topk_robustness", "asset_class_rotation", "thematic_rotation", "europe_rotation"]:
        (tmp_path / f"{name}.json").write_text(json.dumps({"headline": {"trade_history": [
            {"holdings": [{"etf": "SPY", "weight": 1}]}]}}))
    (tmp_path / "risk_overlay.json").write_text(json.dumps({"gated_variants": {
        mtm.DEPLOYED_KEY: {"dates": ["2026-11-03"], "equity": [2.0]}}}))
    (tmp_path / "multi_strategy.json").write_text(json.dumps({"strategies": {
        f"strategy_{k}": {"dates": ["2026-11-03"], "equity": [2.0]} for k in "abcd"}}))
    frame = pd.DataFrame({"SPY": [100, 110]},
                         index=pd.to_datetime(["2026-11-03", "2026-11-04"]))
    monkeypatch.setattr(mtm, "_fetch_usd_prices", lambda *a: frame.copy())
    path = tmp_path / "live_track.json"
    monkeypatch.setattr("sys.argv", ["mark_to_market_live.py", "--out", str(path)])
    return path, frame


def test_vendor_withdrawal_preserves_published_bytes_and_later_recovery(offline_main):
    path, frame = offline_main
    assert mtm.main() == 0
    original = path.read_bytes()
    frame.loc["2026-11-04", "SPY"] = float("nan")
    assert mtm.main() == 1
    assert path.read_bytes() == original
    frame.loc["2026-11-04", "SPY"] = 110
    assert mtm.main() == 0
    assert path.read_bytes() == original


def test_missing_data_keeps_empty_extension_stale_until_later_valid_close(offline_main):
    path, frame = offline_main
    frame.loc["2026-11-04", "SPY"] = float("nan")
    assert mtm.main() == 0
    stale = json.loads(path.read_text())
    assert stale["deployed_series_end"] == "2026-11-03"
    assert stale["live_dates"] == []
    frame.loc["2026-11-04", "SPY"] = 110
    assert mtm.main() == 0
    recovered = json.loads(path.read_text())
    assert recovered["deployed_series_end"] == "2026-11-04"
    assert recovered["live_equity"] == [2.2]
    assert recovered["anchor_equity"] == stale["anchor_equity"]


def test_sleeve_retreat_cannot_be_hidden_by_current_deployed_series(offline_main):
    path, frame = offline_main
    assert mtm.main() == 0
    previous = json.loads(path.read_text())
    previous["sleeve_extensions"]["strategy_a"]["dates"] = ["2026-11-05"]
    previous["sleeve_extensions"]["strategy_a"]["equity"] = [2.3]
    path.write_text(json.dumps(previous))
    original = path.read_bytes()
    assert mtm.main() == 1
    assert path.read_bytes() == original


@pytest.mark.parametrize("anchor", ["2026-05-01", "2026-01-02"])
def test_fetch_covers_native_anchor_lookback_across_month_and_year(monkeypatch, anchor):
    calls = []
    monkeypatch.setattr(mtm, "_yf_close_series", lambda symbols, start, end:
                        calls.append((start, end)) or pd.DataFrame())
    mtm._fetch_usd_prices({"EXH1": 1}, anchor, {})
    assert calls[0][0] == (pd.Timestamp(anchor) - pd.Timedelta(days=31)).strftime("%Y-%m-%d")


def test_main_fetch_includes_earlier_sleeve_anchor(offline_main, monkeypatch):
    path, frame = offline_main
    multi_path = path.parent / "multi_strategy.json"
    multi = json.loads(multi_path.read_text())
    multi["strategies"]["strategy_a"]["dates"] = ["2026-10-30"]
    multi_path.write_text(json.dumps(multi))
    requested = []
    monkeypatch.setattr(mtm, "_fetch_usd_prices", lambda weights, anchor, registry:
                        requested.append(anchor) or frame.copy())
    assert mtm.main() == 0
    assert requested == ["2026-10-30"]
    assert json.loads(path.read_text())["sleeve_extensions"]["strategy_a"]["dates"] == []


def test_valid_reanchor_absorbs_old_extension_without_a_false_retreat(offline_main):
    path, frame = offline_main
    assert mtm.main() == 0
    overlay_path = path.parent / "risk_overlay.json"
    overlay_path.write_text(json.dumps({"gated_variants": {
        mtm.DEPLOYED_KEY: {"dates": ["2026-11-04"], "equity": [2.2]}}}))
    assert mtm.main() == 0
    live = json.loads(path.read_text())
    assert live["live_dates"] == []
    assert live["anchor_date"] == live["deployed_series_end"] == "2026-11-04"
    assert check_anchor(live, json.loads(overlay_path.read_text()))[0] == "ok"


def test_anchor_cannot_move_backwards_even_with_the_same_end():
    previous = {"anchor_date": "2026-11-03", "live_dates": ["2026-11-04"]}
    candidate = {"anchor_date": "2026-11-02", "live_dates": ["2026-11-04"]}
    assert "deployed would retreat" in mtm._publication_retreat(previous, candidate)
