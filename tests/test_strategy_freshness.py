"""Tests for the per-strategy data-freshness report.

The report exists because one `as_of` over four sleeves on three venues hides
the case that matters: on Sat 22 Aug 2026 sleeves A and B carried Friday's
data while C and D carried Thursday's, and nothing published said so.

Most of what follows pins the THREE NEAR-MISS SOURCES the module deliberately
does not read (see its docstring). Each of them sits one step from the answer
and describes something else, and each would have produced a confident wrong
number rather than a visible failure.

Session arithmetic is checked at a month boundary and a year boundary per the
vault date rules; every expected value was derived from the exchange calendar,
never from a weekday computed by hand.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from scripts.strategy_freshness import (
    BEHIND,
    CURRENT,
    UNKNOWN,
    _cap_reason,
    build,
    cache_reach,
    classify,
    panel_reach,
    sessions_between,
)


# ---------------------------------------------------------------------------
# sessions_between — the date arithmetic
# ---------------------------------------------------------------------------
def test_same_session_is_zero():
    assert sessions_between("NYSE", "2026-08-21", "2026-08-21") == 0


def test_one_session():
    assert sessions_between("NYSE", "2026-08-20", "2026-08-21") == 1


def test_a_weekend_is_one_session_not_three_days():
    """Fri 21 Aug to Mon 24 Aug 2026. Calendar days would say 3."""
    assert sessions_between("NYSE", "2026-08-21", "2026-08-24") == 1


def test_month_boundary():
    """Fri 31 Jul to Mon 3 Aug 2026 — one session across a month end."""
    assert sessions_between("NYSE", "2026-07-31", "2026-08-03") == 1


def test_year_boundary_with_a_holiday_in_it():
    """Wed 31 Dec 2025 to Fri 2 Jan 2026. 1 January is a market holiday, so
    the gap is ONE session even though two calendar days passed."""
    assert sessions_between("NYSE", "2025-12-31", "2026-01-02") == 1


def test_venues_are_not_interchangeable():
    """A US holiday that Xetra trades through, and the reverse, must not be
    counted on the wrong calendar — the whole point of a per-venue report.
    Thu 4 July 2024 is a NYSE holiday; Xetra was open."""
    assert sessions_between("NYSE", "2024-07-03", "2024-07-05") == 1
    assert sessions_between("XETR", "2024-07-03", "2024-07-05") == 2


def test_reversed_dates_go_negative_rather_than_absolute():
    """Data running PAST the venue's last close is a real (bad) state and must
    stay visible, not be folded into a positive gap."""
    assert sessions_between("NYSE", "2026-08-21", "2026-08-20") == -1


def test_missing_or_malformed_dates_are_unknown_not_zero():
    for a, b in ((None, "2026-08-21"), ("2026-08-21", None),
                 ("", "2026-08-21"), ("not-a-date", "2026-08-21")):
        assert sessions_between("NYSE", a, b) is None, (a, b)


# ---------------------------------------------------------------------------
# classify
# ---------------------------------------------------------------------------
def test_classify_current():
    assert classify("2026-08-21", "2026-08-21", "NYSE") == (CURRENT, 0)


def test_classify_behind():
    assert classify("2026-08-20", "2026-08-21", "NYSE") == (BEHIND, 1)


def test_classify_unknown_when_a_date_is_missing():
    assert classify(None, "2026-08-21", "NYSE") == (UNKNOWN, None)
    assert classify("2026-08-21", None, "NYSE") == (UNKNOWN, None)


def test_classify_does_not_call_future_data_current():
    """A panel dated past the venue's last completed session is a partial bar.
    It must NOT read as `current` — that would make the look-ahead case the
    refresh guard exists to catch look like the healthy one."""
    status, gap = classify("2026-08-24", "2026-08-21", "NYSE")
    assert status == BEHIND
    assert gap is not None and gap < 0


# ---------------------------------------------------------------------------
# panel_reach / cache_reach — stalest input wins, and gets named
# ---------------------------------------------------------------------------
def _write_panel(tmp_path, etf, end, cap=None):
    blob = {"end_date": end}
    if cap:
        blob["tail_cap"] = cap
    (tmp_path / f"breadth_{etf.lower()}.json").write_text(
        json.dumps(blob), encoding="utf-8")


def test_panel_reach_takes_the_stalest_and_names_it(tmp_path, monkeypatch):
    import scripts.strategy_freshness as sf
    monkeypatch.setattr(sf, "DATA_DIR", tmp_path)
    _write_panel(tmp_path, "IUES", "2026-08-21")
    _write_panel(tmp_path, "IUFS", "2026-08-21")
    _write_panel(tmp_path, "SOXX", "2026-08-19")
    reach, laggards, _ = sf.panel_reach(["IUES", "IUFS", "SOXX"])
    assert reach == "2026-08-19"
    assert laggards == ["SOXX"]


def test_panel_reach_names_nobody_when_the_sleeve_shares_one_date(tmp_path,
                                                                 monkeypatch):
    """Listing every member as a laggard is noise. A laggard is only a laggard
    relative to something fresher."""
    import scripts.strategy_freshness as sf
    monkeypatch.setattr(sf, "DATA_DIR", tmp_path)
    _write_panel(tmp_path, "EXV1", "2026-08-20")
    _write_panel(tmp_path, "EXH1", "2026-08-20")
    reach, laggards, _ = sf.panel_reach(["EXV1", "EXH1"])
    assert reach == "2026-08-20"
    assert laggards == []


def test_panel_reach_surfaces_the_declared_tail_cap(tmp_path, monkeypatch):
    import scripts.strategy_freshness as sf
    monkeypatch.setattr(sf, "DATA_DIR", tmp_path)
    cap = {"capped_at": "2026-08-20", "venue_last_completed": "2026-08-21",
           "constituents_priced_to": "2026-08-20",
           "roster_end_friday": "2026-08-21"}
    _write_panel(tmp_path, "EXV1", "2026-08-20", cap)
    reach, _, caps = sf.panel_reach(["EXV1"])
    technical, plain = _cap_reason(caps, reach)
    assert technical and "2026-08-20" in technical and "session late" in technical
    # Two registers: the plain one must carry the same facts without the
    # vocabulary ("constituents", "venue") the reduced public page refuses.
    assert plain and "2026-08-20" in plain
    for jargon in ("constituent", "venue", "vendor"):
        assert jargon not in plain.lower(), jargon


def test_a_missing_panel_is_skipped_not_counted_as_stale(tmp_path, monkeypatch):
    """An absent file is not evidence of staleness; treating it as one would
    report the whole sleeve behind on a file that was never written."""
    import scripts.strategy_freshness as sf
    monkeypatch.setattr(sf, "DATA_DIR", tmp_path)
    _write_panel(tmp_path, "IUES", "2026-08-21")
    reach, laggards, _ = sf.panel_reach(["IUES", "NOTHERE"])
    assert reach == "2026-08-21"
    assert laggards == []


def test_cache_reach_ignores_trailing_nans_per_ticker(tmp_path):
    """The BTC-USD case: one ticker missing the final bar must move that
    ticker's date, not the frame's."""
    import pandas as pd
    idx = pd.to_datetime(["2026-08-19", "2026-08-20", "2026-08-21"])
    df = pd.DataFrame({"SPY": [1.0, 2.0, 3.0], "BTC-USD": [1.0, 2.0, None]},
                      index=idx)
    p = tmp_path / "c.parquet"
    df.to_parquet(p)
    reach, laggards = cache_reach(p)
    assert reach == "2026-08-20"
    assert laggards == ["BTC-USD"]


def test_cache_reach_on_a_missing_file_is_unknown(tmp_path):
    reach, laggards = cache_reach(tmp_path / "absent.parquet")
    assert reach is None and laggards == []


# ---------------------------------------------------------------------------
# The near-miss sources — regression tests on the committed data
#
# These are the three fields that sit one step from the answer and describe
# something else. Each is checked against the live artefacts, so if a future
# edit reaches for the convenient field the suite says so.
# ---------------------------------------------------------------------------
def test_sleeve_d_reads_the_panel_not_the_forward_filled_engine_series():
    """europe_rotation forward-fills breadth onto the ETF price calendar, so
    its series can carry a date the constituents are not priced for. That fill
    is correct for the backtest and wrong as a freshness claim."""
    from pathlib import Path
    eu = json.loads((Path("data") / "europe_rotation.json")
                    .read_text(encoding="utf-8"))
    series_last = max(v["dates"][-1] for v in eu["per_etf_breadth"].values()
                      if v.get("dates"))
    r = build()
    d = next(s for s in r["strategies"] if s["sleeve"] == "D")
    panel_last, _, _ = panel_reach(list(_europe()))
    assert d["data_through"] == panel_last
    if series_last != panel_last:
        assert d["data_through"] != series_last, (
            "sleeve D freshness must come from the panel, not the filled series")


def test_no_strategy_reports_the_weekly_chart_series_date():
    """`per_etf_signal` is resampled WEEKLY for plotting: BTC-USD missing one
    daily bar made its series end a full week early. A freshness number must
    never be able to move by a week because one day is absent."""
    from pathlib import Path
    th = json.loads((Path("data") / "thematic_rotation.json")
                    .read_text(encoding="utf-8"))
    weekly_min = min(v["dates"][-1] for v in th["per_etf_signal"].values()
                     if v.get("dates"))
    r = build()
    c = next(s for s in r["strategies"] if s["sleeve"] == "C")
    if weekly_min != c["data_through"]:
        assert c["data_through"] != weekly_min


def _europe():
    from scripts.etf_registry import UNIVERSE_EUROPE_SECTORS
    return UNIVERSE_EUROPE_SECTORS


# ---------------------------------------------------------------------------
# build() — shape and invariants
# ---------------------------------------------------------------------------
def test_build_covers_all_four_strategies():
    r = build()
    assert [s["sleeve"] for s in r["strategies"]] == ["A", "B", "C", "D"]
    for s in r["strategies"]:
        assert s["label"], s
        assert s["venue"] in {"NYSE", "XETR"}
        assert s["status"] in {CURRENT, BEHIND, UNKNOWN}


def test_europe_is_measured_on_its_own_venue():
    """The defect this report exists for: judging a Xetra sleeve against the
    NYSE calendar is what produced a whole class of false verdicts."""
    r = build()
    assert next(s for s in r["strategies"] if s["sleeve"] == "D")["venue"] == "XETR"


def test_all_current_agrees_with_the_rows():
    r = build()
    behind = [s for s in r["strategies"] if s["status"] == BEHIND]
    assert r["all_current"] == (not behind)
    assert r["n_behind"] == len(behind)


def test_a_behind_strategy_carries_something_actionable():
    """A warning the reader cannot act on is not a warning: every behind row
    must name its laggard inputs or explain why it stops where it does."""
    r = build()
    for s in r["strategies"]:
        if s["status"] == BEHIND:
            assert s["laggards"] or s["why"], s


def test_the_report_is_injectable_so_it_can_be_tested_at_a_fixed_hour():
    early = build(now_utc=datetime(2026, 8, 21, 6, 0, tzinfo=timezone.utc))
    late = build(now_utc=datetime(2026, 8, 22, 6, 0, tzinfo=timezone.utc))
    a_early = next(s for s in early["strategies"] if s["sleeve"] == "A")
    a_late = next(s for s in late["strategies"] if s["sleeve"] == "A")
    # Same data, a day later: the venue bound may advance, the data cannot.
    assert a_early["data_through"] == a_late["data_through"]
    assert a_early["venue_last_session"] <= a_late["venue_last_session"]
