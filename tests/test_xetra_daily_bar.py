"""The one-session daily chart closes a real gap only with dated, matching evidence."""
import copy
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from native_sessions import effective_calendar
from xetra_daily_bar import recover_daily, verified_bar

SYMBOL = "EXH1.DE"
DATA = Path(__file__).resolve().parents[1] / "data"
CHART = json.loads((DATA / "yahoo_xetra_daily/2026-11-25/EXH1.DE.json").read_text())["chart"]


@pytest.mark.parametrize("before,day", [("2026-10-30","2026-11-02"), ("2025-12-30","2026-01-02")])
def test_capture_and_replay_without_vendor(tmp_path, monkeypatch, before, day):
    # Python and pandas months are 1-indexed.
    schedule = effective_calendar("XETR").schedule(before, day)
    day = pd.Timestamp(day)
    closing = schedule.loc[day, "market_close"]
    now = closing + pd.Timedelta(hours=2)
    chart = copy.deepcopy(CHART)
    stamp = int((closing + pd.Timedelta(minutes=5)).timestamp())
    chart["timestamp"] = [stamp]
    chart["meta"]["regularMarketTime"] = stamp
    monkeypatch.setattr("xetra_daily_bar.fetch_chart", lambda symbol: chart)
    for run in range(2):
        out = pd.DataFrame({"Close":[53.]}, index=pd.to_datetime([before]))
        record = {"refusals": [], "retried_dates": []}
        lineage = {}
        recover_daily(out, [day], schedule, day, now, SYMBOL, tmp_path, lineage, record)
        assert out.loc[day, "Close"] == chart["indicators"]["adjclose"][0]["adjclose"][0]
        assert lineage[str(day.date())]["method"] == "yahoo-one-session"
        assert not record["refusals"]
        monkeypatch.setattr("xetra_daily_bar.fetch_chart", lambda *a: pytest.fail("archive missed"))


@pytest.mark.parametrize("fault", ["early", "future", "session", "venue", "basis", "split", "adjusted", "volume"])
def test_bad_daily_evidence_cannot_advance_nav(fault):
    chart = copy.deepcopy(CHART)
    close_time = pd.Timestamp("2026-11-25T16:30Z")
    now = close_time + pd.Timedelta(hours=2)
    previous = 53.
    if fault == "early":
        chart["meta"]["regularMarketTime"] = int(close_time.timestamp()) - 1
        chart["timestamp"] = [chart["meta"]["regularMarketTime"]]
    elif fault == "future": now = close_time
    elif fault == "session": close_time -= pd.Timedelta(days=1)
    elif fault == "venue": chart["meta"]["currency"] = "USD"
    elif fault == "basis": previous = 52.
    elif fault == "split": chart["events"] = {"splits": {"1": {}}}
    elif fault == "adjusted": chart["indicators"]["adjclose"][0]["adjclose"] = [51.]
    else: chart["indicators"]["quote"][0]["volume"] = [0]
    with pytest.raises(ValueError):
        verified_bar(chart, SYMBOL, close_time, now, previous)


def test_conflicting_partial_bar_is_preserved(tmp_path, monkeypatch):
    day = pd.Timestamp("2026-11-25")
    schedule = effective_calendar("XETR").schedule("2026-11-24", day)
    out = pd.DataFrame({"Open":[53., 52.], "High":[53., None], "Low":[53., None],
                        "Close":[53., None]},index=schedule.index)
    record = {"refusals": [], "retried_dates": []}
    monkeypatch.setattr("xetra_daily_bar.fetch_chart", lambda symbol: CHART)
    recover_daily(out, [day], schedule, day, pd.Timestamp("2026-11-26T01:00Z"),
                  SYMBOL, tmp_path, {}, record)
    assert record["refusals"] and pd.isna(out.loc[day,"Close"])
    assert not list(tmp_path.rglob("*.json"))
