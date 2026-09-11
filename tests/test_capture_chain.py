"""End-to-end capture regressions; all vendor responses are local fixtures.

Python datetime months are 1-indexed. Boundary dates use exchange calendars.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import compute_breadth as cb
import vendor_tail as vt
import strategy_freshness as sf
import capture_status as cs
import fetch_constituents as fc
import pipeline as pl
import live_targets as lt
import check_pretrade_ready as pt


NAMES = [f"N{i}" for i in range(20)]


def frame(end="2026-09-04"):
    idx = pd.bdate_range(end=end, periods=210)
    return pd.DataFrame({n: np.linspace(100, 110, len(idx)) for n in NAMES}, index=idx)


@pytest.mark.parametrize("end,calendar", [("2026-09-01", "NYSE"),
                                         ("2027-01-04", "NYSE"),
                                         ("2026-09-07", "XETR")])
def test_absent_session_is_recovered_across_boundaries(end, calendar):
    full = frame(end)
    short = full.iloc[:-1]
    expected = vt.expected_tail(short.index, end, calendar)
    out, record = cb.verify_price_tail(short, NAMES, expected_sessions=expected,
                                       fetch_single=lambda n: full[n])
    pd.testing.assert_series_equal(out.loc[end], full.loc[end])
    assert record["rows"][-1]["verdict"] == "healed"


def test_a_single_missing_live_member_is_retried_above_coverage_floor():
    full = frame()
    partial = full.copy()
    partial.iloc[-1, 0] = np.nan
    assert partial.index[-1] in cb.priced_sessions(partial, NAMES)
    out, record = cb.verify_price_tail(partial, NAMES,
        expected_sessions=[full.index[-1]], fetch_single=lambda n: full[n])
    assert out.iloc[-1, 0] == full.iloc[-1, 0]
    assert record["rows"][-1]["filled"] == 1


def test_partly_unanswered_probe_cannot_declare_vendor_unavailable():
    full = frame()
    partial = full.copy()
    partial.iloc[-1] = np.nan
    out, record = cb.verify_price_tail(partial, NAMES,
        fetch_single=lambda n: full[n].iloc[:-1] if n == NAMES[0] else None)
    assert full.index[-1] in out.index
    assert record["rows"][-1]["verdict"] == "unverifiable"


def test_engine_recovers_entire_missing_session():
    full = frame()
    out, record = vt.heal_hollow_tail(full.iloc[:-1], NAMES,
        through=full.index[-1].date(), fetch_single=lambda n: full[n])
    pd.testing.assert_series_equal(out.loc[full.index[-1]], full.iloc[-1])
    assert len(record["rows"][-1]["filled"]) == len(NAMES)


def test_future_row_does_not_hide_a_missing_required_close():
    full = frame()
    required = full.index[-2]
    partial = full.copy()
    partial.loc[required, NAMES[0]] = np.nan
    assert vt.cache_current_through(partial, NAMES) > required.date()
    assert not vt.has_required_session(partial, NAMES, required)
    out, record = vt.heal_hollow_tail(partial, NAMES, through=required.date(),
                                      fetch_single=lambda n: full[n])
    assert vt.has_required_session(out, NAMES, required)
    assert record["rows"][-1]["date"] == str(required.date())


def test_missing_roster_provenance_blocks_a_changed_live_roster(tmp_path, monkeypatch):
    monkeypatch.setattr(lt, "DATA_DIR", tmp_path)
    monkeypatch.setattr(lt, "load_constituent_prices", lambda e: frame())
    original = roster()
    panel = {"end_date": "2026-09-04", "current_capture": {
        "roster_fingerprint": cs.roster_fingerprint(original)}}
    (tmp_path / "breadth_good.json").write_text(json.dumps(panel))
    original["snapshots"]["2026-09-04"]["actual_date"] = "2026-09-03"
    (tmp_path / "constituents_good.json").write_text(json.dumps(original))
    signal, _ = lt._breadth_panel(["GOOD"])
    assert signal["GOOD"].isna().all()


@pytest.mark.parametrize("missing", ["absent", "empty"])
def test_required_column_is_never_ignored(tmp_path, missing):
    prices = frame()
    if missing == "absent":
        prices = prices.drop(columns=NAMES[0])
    else:
        prices[NAMES[0]] = np.nan
    assert vt.cache_current_through(prices, NAMES) is None
    p = tmp_path / "cache.parquet"
    prices.to_parquet(p)
    assert sf.cache_reach(p, NAMES) == (None, [NAMES[0]])


def test_missing_column_retry_restores_warmup_and_respects_source():
    full = frame()
    partial = full.drop(columns=[NAMES[0], NAMES[1]])
    asked = []
    def fetch(n):
        asked.append(n)
        return full[n]
    out, record = vt.recover_missing_columns(partial, NAMES,
        exclude=[NAMES[1]], fetch_single=fetch)
    assert asked == [NAMES[0]]
    pd.testing.assert_series_equal(out[NAMES[0]], full[NAMES[0]])
    assert record["recovered"] == [NAMES[0]]


def test_missing_column_timeout_records_unattempted():
    full = frame().drop(columns=[NAMES[0]])
    ticks = iter([0, 2])
    out, record = vt.recover_missing_columns(full, NAMES, budget_s=1,
        clock=lambda: next(ticks), fetch_single=lambda n: pytest.fail("over budget"))
    assert record["not_attempted"] == [NAMES[0]]
    assert NAMES[0] not in out


def roster():
    return {"snapshots": {"2026-09-04": {"actual_date": "2026-09-04", "tickers": NAMES}},
            "endpoint_health": {"status": "ok"}, "staleness": {"status": "fresh"}}


def test_capture_report_names_the_unpriced_members_and_binds_roster():
    prices = frame()
    prices.iloc[-1, 0] = np.nan
    prices[NAMES[1]] = np.nan
    capture = cs.describe_capture(roster(), prices, "2026-09-04", "2026-09-03", "auto")
    assert capture["panel_current"] is False
    assert capture["no_price_history"] == [NAMES[1]]
    assert capture["behind_required_session"] == {NAMES[0]: "2026-09-03"}
    assert capture["n_priced_through_required"] == 18
    changed = roster()
    changed["snapshots"]["2026-09-04"]["tickers"] = NAMES + ["NEW"]
    assert cs.roster_fingerprint(changed) != capture["roster_fingerprint"]


def test_data_health_does_not_excuse_a_declared_price_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(pl, "DATA_DIR", tmp_path)
    monkeypatch.setattr(pl, "DATA_HEALTH_CHECKS", [])
    (tmp_path / "breadth_exh1.json").write_text(json.dumps({
        "end_date": "2026-09-04", "tail_cap": {"venue_last_completed": "2026-09-08"}}))
    health = pl._compute_data_health(pd.Timestamp("2026-09-11").date())
    assert health["overall_status"] == "warn"
    assert health["rows"][0]["status"] == "warn"
    assert "required at capture 2026-09-08" in health["rows"][0]["note"]


def test_data_health_names_missing_member_even_when_panel_is_current(tmp_path, monkeypatch):
    monkeypatch.setattr(pl, "DATA_DIR", tmp_path)
    monkeypatch.setattr(pl, "DATA_HEALTH_CHECKS", [])
    (tmp_path / "breadth_test.json").write_text(json.dumps({
        "end_date": "2026-09-10", "current_capture": {
            "panel_current": True, "required_price_session": "2026-09-10",
            "no_price_history": ["NEW"], "behind_required_session": {"OLD": "2026-09-04"}}}))
    health = pl._compute_data_health(pd.Timestamp("2026-09-11").date())
    assert health["rows"][0]["status"] == "warn"
    assert "NEW: no history" in health["rows"][0]["note"]
    assert "OLD: 2026-09-04" in health["rows"][0]["note"]


def test_endpoint_warning_cannot_downgrade_an_already_stale_roster(tmp_path, monkeypatch):
    monkeypatch.setattr(pl, "DATA_DIR", tmp_path)
    monkeypatch.setattr(pl, "DATA_HEALTH_CHECKS", [])
    (tmp_path / "constituents_test.json").write_text(json.dumps({
        "end_friday": "2026-01-02",
        "snapshots": {"2026-01-02": {"actual_date": "2026-01-02"}},
        "endpoint_health": {"status": "unavailable"}}))
    health = pl._compute_data_health(pd.Timestamp("2026-09-11").date())
    assert health["rows"][0]["status"] == "stale"


def test_missing_live_panel_holds_the_sleeve(tmp_path, monkeypatch):
    monkeypatch.setattr(lt, "DATA_DIR", tmp_path)
    full = frame()
    monkeypatch.setattr(lt, "load_constituent_prices", lambda e: full)
    (tmp_path / "breadth_good.json").write_text(json.dumps({"end_date": "2026-09-04"}))
    panel, used = lt._breadth_panel(["GOOD", "MISSING"])
    assert used == ["GOOD", "MISSING"]
    assert panel["MISSING"].isna().all()
    rank = lt._rank(panel, lambda row: pytest.fail("must not rank"), "NYSE",
                    datetime(2026, 9, 6, tzinfo=timezone.utc), "A")
    assert rank["status"] == "HOLD"


def test_pretrade_refuses_a_current_panel_without_instruction(tmp_path):
    p = tmp_path / "breadth_csp1.json"
    p.write_text(json.dumps({"end_date": "2026-09-04"}))
    report = pt.build_book_report(p, datetime(2026, 9, 6, 6, tzinfo=timezone.utc))
    assert report["warn"] == "true"
    assert "verification failed" in report["detail"]


@pytest.mark.parametrize("now,end", [("2026-09-06", "2026-09-04"),
                                    ("2027-01-03", "2026-12-31")])
def test_pretrade_checks_complete_book_and_rejects_one_hold(tmp_path, now, end):
    from etf_registry import UNIVERSE_ETFS, UNIVERSE_EUROPE_SECTORS
    instant = datetime.fromisoformat(now).replace(hour=6, tzinfo=timezone.utc)
    import pandas_market_calendars as mcal
    from session_bounds import last_completed_session_on
    ends = {v: str(last_completed_session_on(mcal.get_calendar(v), instant).date())
            for v in ("NYSE", "XETR")}
    for etf in set(UNIVERSE_ETFS) | set(UNIVERSE_EUROPE_SECTORS) | {"CSP1"}:
        venue = "XETR" if etf in UNIVERSE_EUROPE_SECTORS else "NYSE"
        (tmp_path / f"breadth_{etf.lower()}.json").write_text(json.dumps({"end_date": ends[venue]}))
    sleeves = [{"sleeve": sl, "status": "READY", "decision_session": ends["XETR" if sl == "D" else "NYSE"],
                "decision_session_for_fill": ends["XETR" if sl == "D" else "NYSE"],
                "fill_date": lt.next_fill_date("XETR" if sl == "D" else "NYSE", instant)}
               for sl in "ABCD"]
    book = {"targets_final": True, "sleeves": sleeves, "computed_at_utc": instant.isoformat()}
    path = tmp_path / "live_targets.json"
    path.write_text(json.dumps(book))
    p = tmp_path / "breadth_csp1.json"
    assert pt.build_book_report(p, instant)["warn"] == "false"
    sleeves[-1]["status"] = "HOLD"
    path.write_text(json.dumps(book))
    assert pt.build_book_report(p, instant)["warn"] == "true"


def test_failed_roster_capture_stops_downstream(monkeypatch):
    import refresh_all as refresh
    monkeypatch.setattr(sys, "argv", ["refresh_all.py", "--throttle", "0", "--no-tests"])
    monkeypatch.setattr(refresh, "ETFS_REFRESH", ["CSP1"])
    calls = []
    def fail(label, cmd, **kwargs):
        calls.append(cmd)
        return False, 0
    monkeypatch.setattr(refresh, "run_step", fail)
    assert refresh.main() == 1
    assert len(calls) == 1 and "scripts/fetch_constituents.py" in calls[0]
