"""Tail verification (2026-09-05): a placeholder row is not a capture.

THE FAILURE THESE PIN. yfinance withdraws every European session's close
overnight and serves a row dated correctly with NaN closes in its place,
restoring it about two calendar days after the session. The batch download
therefore lands a HOLLOW tail row on every Saturday and Tuesday morning
refresh — the same shape as the 2026-08-30 failure, where the batch served
empty Fridays while single-ticker requests returned real bars. On 2026-09-05
the hollow-tail refresh guard refused the whole Saturday run over sleeve D's
five panels, for a row the vendor was simply not serving.

The writer now asks the vendor single-ticker before writing the cache:
served means the batch was defective and the row is healed; not served means
the placeholder row is dropped, so the cache ends on the last priced session
(the clean-short vendor-lag shape every reader already handles); no answer
keeps the row and the guard fails closed. Every case, and the cases that must
stay untouched, are here. All vendor calls are stubbed: nothing hits the
network.

Python datetime months are 1-indexed (January = 1); pandas builds the
business-day indexes, never a hand-computed offset.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import compute_breadth as cb  # noqa: E402

ROSTER = [f"N{i:02d}" for i in range(10)]   # priced floor: max(5, int(0.9*10)+1) = 10
WIDE = [f"P{i:02d}" for i in range(20)]     # priced floor: 19


def _frame(roster=ROSTER, n_days=6, end="2026-09-04", hollow_last=True,
           dead=()):
    """``n_days`` business days ending on ``end``; every roster name priced on
    every row except the last, which is NaN for the roster when
    ``hollow_last`` (the vendor's placeholder). ``dead`` names are NaN
    everywhere — a delisted column."""
    idx = pd.bdate_range(end=end, periods=n_days)
    data = {}
    for i, t in enumerate(roster):
        col = pd.Series(100.0 + i + np.arange(n_days, dtype=float), index=idx)
        if t in dead:
            col[:] = np.nan
        elif hollow_last:
            col.iloc[-1] = np.nan
        data[t] = col
    return pd.DataFrame(data)


def _serving(frame_like: pd.DataFrame, through: str | None = None):
    """A single-ticker stub. It serves the column's own history, carried
    forward so a healed bar is continuous with the bar before it (no
    split-sized step to send the WS15 guard to the calendar), up to
    ``through`` inclusive — None serves every date, i.e. the tail row too."""
    def fetch(t):
        s = frame_like[t].ffill().copy()
        if through is not None:
            s = s.loc[:pd.Timestamp(through)]
        return s
    return fetch


def _last_ok(f: pd.DataFrame) -> str:
    return str(f.index[-2].date())


# ---------------------------------------------------------------------------
# The three verdicts
# ---------------------------------------------------------------------------
def test_unserved_placeholder_row_is_dropped():
    """The Saturday-morning shape: every live name NaN on the Friday row and
    single-ticker says the vendor has no Friday bar either."""
    f = _frame()
    out, v = cb.verify_price_tail(f, ROSTER,
                                  fetch_single=_serving(f, through=_last_ok(f)))
    assert str(out.index.max().date()) == _last_ok(f)
    assert v["dropped"] == ["2026-09-04"]
    assert v["last_populated"] == _last_ok(f)
    r = v["rows"][0]
    assert r["verdict"] == "unserved"
    assert r["roster_priced"] == 0 and r["live_unpriced"] == 10
    assert r["sample_answered"] == 5 and r["sample_served"] == 0
    # The caller's frame is not mutated.
    assert f.index.max() == pd.Timestamp("2026-09-04")


def test_batch_defect_is_healed_from_single_ticker():
    """The 2026-08-30 shape: the batch served an empty Friday, single-ticker
    requests have the bar. Every unpriced live name is re-requested."""
    f = _frame()
    out, v = cb.verify_price_tail(f, ROSTER, fetch_single=_serving(f))
    friday = pd.Timestamp("2026-09-04")
    assert out.index.max() == friday
    assert out.loc[friday, ROSTER].notna().all()
    r = v["rows"][0]
    assert r["verdict"] == "healed" and r["filled"] == 10 and r["refused"] == []
    assert v["dropped"] == []
    assert friday in cb.priced_sessions(out, ROSTER)


def test_partial_heal_below_the_floor_keeps_the_row():
    """Half the names come back single-ticker: the row is healed as far as
    the vendor allows, stays below the priced floor, and is KEPT — it is the
    refresh guard's to judge, not this step's to hide."""
    f = _frame(roster=WIDE)
    served = set(WIDE[:10])
    base = _serving(f)

    def fetch(t):
        s = base(t)
        return s if t in served else s.iloc[:-1]

    out, v = cb.verify_price_tail(f, WIDE, fetch_single=fetch)
    r = v["rows"][0]
    assert r["verdict"] == "partial" and r["filled"] == 10
    assert out.index.max() == pd.Timestamp("2026-09-04")
    assert pd.Timestamp("2026-09-04") not in cb.priced_sessions(out, WIDE)
    assert v["dropped"] == []


def test_no_answer_keeps_the_row_unverifiable():
    """Fail closed: a probe that cannot reach the vendor decides nothing."""
    f = _frame()
    out, v = cb.verify_price_tail(f, ROSTER, fetch_single=lambda t: None)
    assert out.index.max() == pd.Timestamp("2026-09-04")
    assert v["rows"][0]["verdict"] == "unverifiable"
    assert v["dropped"] == []


def test_a_raising_probe_is_no_answer():
    def fetch(t):
        raise RuntimeError("rate limited")

    f = _frame()
    out, v = cb.verify_price_tail(f, ROSTER, fetch_single=fetch)
    assert v["rows"][0]["verdict"] == "unverifiable"
    assert out.index.max() == pd.Timestamp("2026-09-04")


# ---------------------------------------------------------------------------
# Who may vote, and what may be touched
# ---------------------------------------------------------------------------
def test_dead_names_never_vote():
    """A delisted column is NaN on the tail row too, but it is NaN on the
    last populated row as well: it is never sampled, so it cannot say 'not
    served' on the vendor's behalf."""
    roster = WIDE + ["DEAD1", "DEAD2"]           # floor: int(0.9*22)+1 = 20
    f = _frame(roster=roster, dead=("DEAD1", "DEAD2"))
    asked: list[str] = []
    base = _serving(f, through=_last_ok(f))

    def fetch(t):
        asked.append(t)
        return base(t)

    _, v = cb.verify_price_tail(f, roster, fetch_single=fetch)
    assert not {"DEAD1", "DEAD2"} & set(asked)
    assert v["rows"][0]["verdict"] == "unserved"
    assert v["rows"][0]["live_unpriced"] == 20


def test_excluded_columns_are_neither_probed_nor_filled():
    """A column taken whole from Norgate is not this step's to touch — a
    price basis may not change mid-column (WS19b) — so its lag stays, and
    the row it leaves short is the guard's to judge."""
    f = _frame()
    asked: list[str] = []
    base = _serving(f)

    def fetch(t):
        asked.append(t)
        return base(t)

    out, v = cb.verify_price_tail(f, ROSTER, exclude=["N00"], fetch_single=fetch)
    friday = pd.Timestamp("2026-09-04")
    assert "N00" not in asked
    assert pd.isna(out.loc[friday, "N00"])
    assert out.loc[friday, ROSTER[1:]].notna().all()
    assert v["rows"][0]["verdict"] == "partial" and v["rows"][0]["filled"] == 9


def test_sample_is_bounded_and_spread_through_the_roster():
    """Five names, first and last included, not the first five: on a
    pan-European roster in index order the first five can all sit on one
    venue."""
    f = _frame(roster=WIDE)
    asked: list[str] = []
    base = _serving(f, through=_last_ok(f))

    def fetch(t):
        asked.append(t)
        return base(t)

    _, v = cb.verify_price_tail(f, WIDE, fetch_single=fetch)
    assert len(asked) == 5
    assert asked[0] == "P00" and asked[-1] == "P19"
    assert v["rows"][0]["sampled"] == asked


def test_fully_priced_tail_is_left_alone():
    f = _frame(hollow_last=False)

    def must_not_probe(t):
        raise AssertionError("a priced tail must not be probed")

    out, v = cb.verify_price_tail(f, ROSTER, fetch_single=must_not_probe)
    assert v is None
    pd.testing.assert_frame_equal(out, f)


def test_no_populated_row_at_all_is_not_this_steps_problem():
    """An all-NaN frame is the n_with_any_data == 0 case, refused upstream in
    main(); the tail step has no populated row to measure from and steps
    aside rather than dropping everything."""
    f = _frame()
    f[:] = np.nan
    out, v = cb.verify_price_tail(f, ROSTER, fetch_single=lambda t: None)
    assert v is None and len(out) == len(f)


def test_two_placeholder_rows_are_settled_independently():
    """Thursday unserved, Friday served single-ticker: Thursday is dropped
    and Friday healed — a vendor hole on one session (the 2026-08-13 class)
    beside a batch defect on the next."""
    f = _frame(n_days=7)
    thu, fri = f.index[-2], f.index[-1]
    f.loc[thu, ROSTER] = np.nan
    full = _serving(f)

    def fetch(t):
        return full(t).drop(thu)

    out, v = cb.verify_price_tail(f, ROSTER, fetch_single=fetch)
    assert thu not in out.index and fri in out.index
    assert out.loc[fri, ROSTER].notna().all()
    assert [r["verdict"] for r in v["rows"]] == ["unserved", "healed"]
    assert v["dropped"] == [str(thu.date())]


def test_healed_bar_served_with_a_split_unapplied_is_refused(monkeypatch):
    """WS15 applies to a healed bar exactly as to a batch column: a 2-for-1
    served unapplied is a split-sized step the vendor's own calendar
    explains, and the cell is refused."""
    f = _frame()
    friday = pd.Timestamp("2026-09-04")
    base = _serving(f)

    def fetch(t):
        s = base(t)
        s.loc[friday] = s.loc[friday] / 2.0
        return s

    monkeypatch.setattr(cb, "_splits_for",
                        lambda t: pd.Series([2.0], index=[friday]))
    out, v = cb.verify_price_tail(f, ROSTER, fetch_single=fetch)
    r = v["rows"][0]
    assert r["refused"] == ROSTER and r["filled"] == 0
    assert r["verdict"] == "partial"
    assert out.loc[friday, ROSTER].isna().all()


# ---------------------------------------------------------------------------
# Date edge cases CLAUDE.md requires of any date logic
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("end, last_priced", [
    ("2026-09-01", "2026-08-31"),   # month boundary: Tue 1 Sep after Mon 31 Aug
    ("2027-01-01", "2026-12-31"),   # year boundary: Fri 1 Jan after Thu 31 Dec
])
def test_placeholder_drop_across_month_and_year_boundaries(end, last_priced):
    f = _frame(end=end)
    out, v = cb.verify_price_tail(f, ROSTER,
                                  fetch_single=_serving(f, through=last_priced))
    assert v["dropped"] == [end]
    assert str(out.index.max().date()) == last_priced
    assert v["last_populated"] == last_priced


# ---------------------------------------------------------------------------
# Through download_prices: the cache on disk, the prior-cache union, the
# switches, and the Norgate seam
# ---------------------------------------------------------------------------
def _yf(frame: pd.DataFrame):
    """Shape yf.download returns under group_by='column'."""
    return type("Y", (), {
        "download": staticmethod(
            lambda *a, **k: pd.concat({"Close": frame.copy()}, axis=1))})()


def _span(f: pd.DataFrame) -> tuple[str, str]:
    """A request the prior cache cannot cover, so the download must run."""
    return (str(f.index[0].date()),
            str((f.index[-1] + pd.Timedelta(days=3)).date()))


def test_download_prices_drops_the_placeholder_even_when_the_prior_cache_carries_it(
        tmp_path, monkeypatch, capsys):
    """The Saturday retry: the 09:00 run wrote the hollow row into the cache,
    and the cell-preservation merge re-unions that index on the 10:00 retry.
    The drop has to act on the finished frame or the row never leaves."""
    f = _frame()
    cache = tmp_path / "prices_cache_exv1.parquet"
    f.to_parquet(cache)
    monkeypatch.setattr(cb, "yf", _yf(f))
    monkeypatch.setattr(cb, "_single_ticker_closes",
                        _serving(f, through=_last_ok(f)))
    start, end = _span(f)
    out = cb.download_prices(ROSTER, start, end, cache_path=cache, roster=ROSTER)
    assert str(out.index.max().date()) == _last_ok(f)
    assert str(pd.read_parquet(cache).index.max().date()) == _last_ok(f)
    assert out.attrs["tail_verification"]["dropped"] == ["2026-09-04"]
    assert "placeholder row dropped" in capsys.readouterr().out


def test_download_prices_heals_and_writes_the_healed_row(tmp_path, monkeypatch, capsys):
    f = _frame()
    cache = tmp_path / "px.parquet"
    monkeypatch.setattr(cb, "yf", _yf(f))
    monkeypatch.setattr(cb, "_single_ticker_closes", _serving(f))
    start, end = _span(f)
    out = cb.download_prices(ROSTER, start, end, cache_path=cache, roster=ROSTER)
    friday = pd.Timestamp("2026-09-04")
    assert out.loc[friday, ROSTER].notna().all()
    assert pd.read_parquet(cache).loc[friday, ROSTER].notna().all()
    assert out.attrs["tail_verification"]["rows"][0]["verdict"] == "healed"
    assert "Row now populated" in capsys.readouterr().out


def test_download_prices_without_a_roster_is_unchanged(tmp_path, monkeypatch):
    """Callers that pass no roster get the frame as downloaded, hollow row
    and all — the pre-2026-09-05 behaviour, byte for byte."""
    f = _frame()
    cache = tmp_path / "px.parquet"
    monkeypatch.setattr(cb, "yf", _yf(f))

    def must_not_probe(t):
        raise AssertionError("no roster, no probe")

    monkeypatch.setattr(cb, "_single_ticker_closes", must_not_probe)
    start, end = _span(f)
    out = cb.download_prices(ROSTER, start, end, cache_path=cache)
    assert out.index.max() == pd.Timestamp("2026-09-04")
    assert out.attrs["tail_verification"] is None


def test_tail_probe_can_be_switched_off(tmp_path, monkeypatch):
    f = _frame()
    cache = tmp_path / "px.parquet"
    monkeypatch.setattr(cb, "yf", _yf(f))

    def must_not_probe(t):
        raise AssertionError("tail_probe=False, no probe")

    monkeypatch.setattr(cb, "_single_ticker_closes", must_not_probe)
    start, end = _span(f)
    out = cb.download_prices(ROSTER, start, end, cache_path=cache,
                             roster=ROSTER, tail_probe=False)
    assert out.index.max() == pd.Timestamp("2026-09-04")
    assert out.attrs["tail_verification"] is None


def test_norgate_columns_are_left_to_norgate(tmp_path, monkeypatch):
    """auto mode: a column taken whole from Norgate is never probed or filled
    from yfinance. Norgate a session behind on it leaves the row short, and
    that stays the guard's to judge — fail closed on a Norgate lag."""
    f = _frame()
    cache = tmp_path / "px.parquet"
    monkeypatch.setattr(cb, "yf", _yf(f))
    ng = pd.DataFrame({"N00": f["N00"] * 10.0})      # superset of the incumbent's dates
    import norgate_prices
    monkeypatch.setattr(norgate_prices, "available", lambda: True)
    monkeypatch.setattr(norgate_prices, "fetch_closes",
                        lambda tickers, start, end, verbose=True: (ng, ["N00"], ROSTER[1:]))
    asked: list[str] = []
    base = _serving(f)

    def fetch(t):
        asked.append(t)
        return base(t)

    monkeypatch.setattr(cb, "_single_ticker_closes", fetch)
    start, end = _span(f)
    out = cb.download_prices(ROSTER, start, end, cache_path=cache,
                             roster=ROSTER, price_source="auto")
    friday = pd.Timestamp("2026-09-04")
    assert "N00" not in asked
    assert pd.isna(out.loc[friday, "N00"])
    assert out.loc[friday, ROSTER[1:]].notna().all()
    assert out["N00"].dropna().between(990, 1200).all(), "N00 must stay Norgate's"
    assert out.attrs["tail_verification"]["rows"][0]["verdict"] == "partial"


# ---------------------------------------------------------------------------
# main() carries the record into the panel: provenance beside the number
# ---------------------------------------------------------------------------
def test_main_records_the_verification_in_the_panel(tmp_path, monkeypatch):
    # Six names: MIN_BREADTH_NAMES is 5, so a smaller roster can never clear
    # the priced floor and the tail cap would never engage.
    names = list("ABCDEF")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    constituents = {
        "etf": "TEST",
        "start_friday": "2024-01-05",
        "end_friday": "2024-04-30",
        "snapshots": {
            "2024-01-05": {"actual_date": "2024-01-05", "n_tickers": 6,
                           "tickers": names},
            "2024-01-12": {"actual_date": "2024-01-12", "n_tickers": 6,
                           "tickers": names},
        },
    }
    (data_dir / "constituents_test.json").write_text(
        json.dumps(constituents), encoding="utf-8")
    idx = pd.bdate_range("2023-07-01", "2024-05-10")
    prices = pd.DataFrame({t: np.linspace(50.0 + i, 120.0 + i, len(idx))
                           for i, t in enumerate(names)}, index=idx)
    record = {"checked_at_utc": "2026-09-05T01:12:00+00:00",
              "probe": "stub", "last_populated": "2024-05-10",
              "rows": [{"date": "2024-05-13", "verdict": "unserved"}],
              "dropped": ["2024-05-13"]}

    def fake_download_prices(tickers, start, end, cache_path, force=False,
                             price_source="yfinance", **kwargs):
        assert kwargs.get("roster") == names, "main() must pass the newest roster"
        out = prices.loc[pd.Timestamp(start):pd.Timestamp(end), tickers].copy()
        out.attrs["tail_verification"] = record
        return out

    monkeypatch.setattr(cb, "DATA_DIR", data_dir)
    monkeypatch.setattr(cb, "download_prices", fake_download_prices)
    monkeypatch.setattr(sys, "argv", ["compute_breadth.py", "--etf", "TEST"])
    assert cb.main() == 0

    out = json.loads((data_dir / "breadth_test.json").read_text(encoding="utf-8"))
    assert out["tail_verification"] == record
    # The prices stop in May 2024, far short of the venue's last completed
    # session, so a cap results — and it carries the probe's record.
    assert out["tail_cap"] is not None
    assert out["tail_cap"]["vendor_probe"] == record
