"""The basis guard: a re-based constituent history is chosen, not suffered.

THE FAILURE THESE PIN (2026-10-03). Corteva separated on 2026-10-01. Norgate
served the spin-adjusted CTVA history (every pre-separation close / 6.6652),
yfinance served the raw series with a 6.6x cliff and no split event, and the
01:21 UTC weekend refresh wrote whichever basis the overlay handed it into
the CSP1 and IUMS caches with nothing but a 60-row "ambiguous" count in the
ledger. The guard runs on the finished frame before the write: a column
re-scaled by one split-sized ratio over its shared history is admitted only
on a declaration or a matching vendor split; otherwise the cached history is
kept and new sessions are appended across an exact seam only.

Python datetime months are 1-indexed (January = 1). Every index below is
built by pandas from an explicit business-day START or a business-day END,
never from a weekend end (pandas 3.0.0 returns n-1 dates for that), and the
boundary dates are asserted rather than assumed. Nothing here touches the
network: the vendor download, the Norgate feed and the split calendar are
all stubbed.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import basis_guard as bg          # noqa: E402
import compute_breadth as cb      # noqa: E402
import price_revisions as pr      # noqa: E402
import price_source as ps         # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures: a raw series with a cliff at the event, and its adjusted twin
# ---------------------------------------------------------------------------
def _sessions_ending(end: str, n: int) -> pd.DatetimeIndex:
    """``n`` business days ending on the business day ``end``."""
    idx = pd.bdate_range(end=end, periods=n)
    assert len(idx) == n and idx[-1] == pd.Timestamp(end)
    return idx


def _sessions_from(start: str, n: int) -> pd.DatetimeIndex:
    idx = pd.bdate_range(start, periods=n)
    assert len(idx) == n and idx[0] == pd.Timestamp(start)
    return idx


def _pair(pre_end="2026-09-30", post_start="2026-10-01", n_pre=300, n_post=2,
          level=80.0, factor=4.0):
    """``(raw, adjusted)`` on one index: ``n_pre`` sessions before the event
    at ``level`` drifting up 10%, then ``n_post`` sessions on the new basis.
    ``raw`` carries the cliff at the event; ``adjusted`` divides the
    pre-event history by ``factor`` so the series is continuous."""
    pre = _sessions_ending(pre_end, n_pre)
    post = _sessions_from(post_start, n_post)
    assert pre[-1] < post[0]
    idx = pre.append(post)
    before = np.linspace(level, level * 1.1, n_pre)
    after = level * 1.1 / factor * (1.0 + 0.01 * np.arange(1, n_post + 1))
    raw = pd.Series(np.concatenate([before, after]), index=idx, dtype=float)
    adjusted = raw.copy()
    adjusted.iloc[:n_pre] = raw.iloc[:n_pre] / factor
    return raw, adjusted


def _multi(frame: pd.DataFrame) -> pd.DataFrame:
    """Shape yf.download returns under group_by='column'."""
    out = frame.copy()
    out.columns = pd.MultiIndex.from_product([["Close"], frame.columns])
    return out


def _decl(ticker="AAA", effective="2026-10-01", factor=4.0, basis="adjusted",
          **extra) -> dict:
    return {"ticker": ticker, "effective": effective, "factor": factor,
            "basis": basis, "event": "test event", "source": "declaration",
            **extra}


def _write_declarations(path: Path, *entries) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema": 1, "declarations": list(entries)}),
                    encoding="utf-8")
    return path


def _no_calendar(ticker):
    return None


def _calendar(date, ratio):
    def stub(ticker):
        return pd.Series([float(ratio)], index=[pd.Timestamp(date)])
    return stub


def _must_not_consult(ticker):
    raise AssertionError("the split calendar must not be consulted here")


# ---------------------------------------------------------------------------
# 0. Wiring: one split-sized threshold, a committed declaration file
# ---------------------------------------------------------------------------
def test_split_sized_threshold_is_shared_with_the_ws15_guard():
    assert cb.VENDOR_STEP_LOG_RETURN is bg.SPLIT_SIZED_LOG_RATIO
    assert bg.SPLIT_SIZED_LOG_RATIO == 0.20


def test_the_committed_declaration_file_loads_and_declares_ctva():
    decls, warnings = bg.load_declarations(cb.DECLARATIONS_PATH)
    assert warnings == []
    ctva = [d for d in decls if d["ticker"] == "CTVA"]
    assert len(ctva) == 1
    assert ctva[0]["basis"] == bg.ADJUSTED
    assert ctva[0]["effective"] == "2026-10-01"
    assert ctva[0]["factor"] == pytest.approx(6.6652, rel=1e-6)


def test_the_declaration_file_is_tracked_not_ignored():
    rel = cb.DECLARATIONS_PATH.relative_to(ROOT).as_posix()
    assert "corporate_action_basis" not in (ROOT / ".gitignore").read_text(
        encoding="utf-8")
    try:
        proc = subprocess.run(["git", "check-ignore", "-q", rel], cwd=str(ROOT),
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pytest.skip("git not available")
    assert proc.returncode == 1, f"{rel} is gitignored; the decision would not travel"


# ---------------------------------------------------------------------------
# 1. Detect
# ---------------------------------------------------------------------------
def test_prefix_rebasing_is_detected_with_its_boundary_dates():
    raw, adjusted = _pair()
    f = bg.detect_rebasing(adjusted, raw)       # adjusted cache, raw incoming
    assert f is not None
    assert f["ratio"] == pytest.approx(4.0, rel=1e-9)
    assert f["cells"] == 300 and f["shared_cells"] == 302
    assert f["shape"] == "prefix" and f["suffix_cells"] == 2
    assert (f["first"], f["through"], f["first_suffix"]) == (
        str(raw.index[0].date()), "2026-09-30", "2026-10-01")
    assert f["prefix_fill"] == 1.0 and f["log_dispersion"] < 1e-9


def test_whole_rebasing_is_detected_when_the_cache_ends_before_the_event():
    raw, adjusted = _pair()
    cache = raw.loc[:"2026-09-30"]              # raw cache, written before the event
    f = bg.detect_rebasing(cache, adjusted)     # the adjusted series arrives
    assert f is not None
    assert f["ratio"] == pytest.approx(0.25, rel=1e-9)
    assert f["shape"] == "whole" and f["first_suffix"] is None
    assert f["cells"] == f["shared_cells"] == 300
    assert f["through"] == "2026-09-30"


def test_dividend_drift_is_not_a_rebasing():
    raw, _ = _pair()
    assert bg.detect_rebasing(raw, raw * 0.992) is None   # a 0.8% TR adjustment
    assert bg.detect_rebasing(raw, raw * 1.03) is None


def test_scattered_restatements_are_not_a_rebasing():
    raw, _ = _pair(n_pre=400)
    revised = raw.copy()
    for pos in range(10, 400, 33):          # 12 cells halved, spread through
        revised.iloc[pos] = raw.iloc[pos] * 0.5
    assert bg.detect_rebasing(raw, revised) is None


def test_fewer_than_three_rebased_cells_is_not_a_rebasing():
    raw, _ = _pair()
    revised = raw.copy()
    revised.iloc[:2] = raw.iloc[:2] * 0.5
    assert bg.detect_rebasing(raw, revised) is None


def test_a_few_preserved_cells_inside_the_prefix_do_not_hide_it():
    """The cell-preservation merge can leave cached (ratio-one) cells inside
    a re-based prefix where the source served nothing. Up to 20% of the
    prefix is tolerated; past that the changes are not one re-basing."""
    raw, adjusted = _pair()
    mixed = raw.copy()
    mixed.iloc[:300:10] = adjusted.iloc[:300:10]     # 10% of the prefix unchanged
    f = bg.detect_rebasing(adjusted, mixed)
    assert f is not None and f["cells"] == 270 and f["prefix_fill"] == 0.9
    mixed = raw.copy()
    mixed.iloc[:300:3] = adjusted.iloc[:300:3]       # a third unchanged
    assert bg.detect_rebasing(adjusted, mixed) is None


def test_reverse_split_direction_and_mixed_signs():
    raw, _ = _pair()
    f = bg.detect_rebasing(raw, raw * 10.0)         # 1-for-10 reverse, raw -> adjusted
    assert f is not None and f["ratio"] == pytest.approx(10.0)
    mixed = raw.copy()
    mixed.iloc[:150] = raw.iloc[:150] * 2.0
    mixed.iloc[150:300] = raw.iloc[150:300] * 0.5    # two ratios: not one re-basing
    assert bg.detect_rebasing(raw, mixed) is None


def test_missing_cells_on_either_side_are_simply_not_shared():
    raw, adjusted = _pair()
    holey_cache = adjusted.copy()
    holey_cache.iloc[5:40] = np.nan
    holey_incoming = raw.copy()
    holey_incoming.iloc[100:120] = np.nan
    f = bg.detect_rebasing(holey_cache, holey_incoming)
    assert f is not None
    assert f["shared_cells"] == 302 - 35 - 20
    assert f["cells"] == 300 - 35 - 20 and f["shape"] == "prefix"


# ---------------------------------------------------------------------------
# 2. Decide: the event window at a month boundary and at a year boundary
# ---------------------------------------------------------------------------
def test_month_boundary_declaration_decides_both_directions():
    """CTVA's own shape: last re-based session 30 Sep, first session on the
    new basis 1 Oct. A declaration for 1 Oct with basis 'adjusted' refuses
    the raw series and admits the adjusted one."""
    raw, adjusted = _pair()                       # 2026-09-30 | 2026-10-01
    f_raw = bg.detect_rebasing(adjusted, raw)     # raw arriving on an adjusted cache
    v = bg.decide("AAA", f_raw, declarations=[_decl()], splits_for=_must_not_consult)
    assert v["decision"] == bg.REFUSED_BY_DECLARATION
    assert v["evidence"]["incoming_is"] == bg.UNADJUSTED
    f_adj = bg.detect_rebasing(raw, adjusted)     # adjusted arriving on a raw cache
    v = bg.decide("AAA", f_adj, declarations=[_decl()], splits_for=_must_not_consult)
    assert v["decision"] == bg.ADMITTED_BY_DECLARATION
    assert v["evidence"]["incoming_is"] == bg.ADJUSTED


def test_year_boundary_window_is_strictly_after_through_and_at_most_first_suffix():
    """Last re-based session Wed 31 Dec 2025, first session on the new basis
    Fri 2 Jan 2026 (1 Jan is a holiday, so the index has no bar for it).
    The event date must sit in (31 Dec, 2 Jan]."""
    raw, adjusted = _pair(pre_end="2025-12-31", post_start="2026-01-02")
    assert raw.index[-2] == pd.Timestamp("2026-01-02")
    f = bg.detect_rebasing(adjusted, raw)
    assert (f["through"], f["first_suffix"]) == ("2025-12-31", "2026-01-02")
    after, through = bg.event_window(f, incoming_end=raw.index.max())
    assert (after, through) == (pd.Timestamp("2025-12-31"), pd.Timestamp("2026-01-02"))
    for effective, expected in (("2026-01-01", bg.REFUSED_BY_DECLARATION),
                                ("2026-01-02", bg.REFUSED_BY_DECLARATION),
                                ("2025-12-31", bg.REFUSED),       # == through: no
                                ("2026-01-05", bg.REFUSED)):      # past the suffix: no
        v = bg.decide("AAA", f, declarations=[_decl(effective=effective)],
                      splits_for=_no_calendar)
        assert v["decision"] == expected, effective
        if expected == bg.REFUSED:
            assert "none matches" in v["reason"]


def test_whole_rebasing_window_runs_to_the_newest_bar_or_a_short_grace():
    raw, adjusted = _pair()
    cache = raw.loc[:"2026-09-30"]
    f = bg.detect_rebasing(cache, adjusted)
    assert f["shape"] == "whole"
    # With every shared cell re-based nothing dates the event beyond "after
    # 30 September": the bound is the source's newest bar or the grace past
    # the last re-based cell, whichever is later (pandas arithmetic).
    grace = pd.Timestamp("2026-09-30") + pd.Timedelta(days=bg.PRE_APPLIED_GRACE_DAYS)
    assert grace == pd.Timestamp("2026-10-07")
    after, through = bg.event_window(f, incoming_end=adjusted.index.max())
    assert (after, through) == (pd.Timestamp("2026-09-30"), grace)
    after, through = bg.event_window(f, incoming_end=pd.Timestamp("2026-10-20"))
    assert through == pd.Timestamp("2026-10-20")
    # No newer bar at all: a factor pre-applied on the ex-date morning may
    # declare an event up to the grace past the last re-based cell.
    after, through = bg.event_window(f, incoming_end=None)
    assert through == grace
    v = bg.decide("AAA", f, declarations=[_decl(effective="2026-10-07")],
                  splits_for=_no_calendar, incoming_end=None)
    assert v["decision"] == bg.ADMITTED_BY_DECLARATION
    v = bg.decide("AAA", f, declarations=[_decl(effective="2026-10-08")],
                  splits_for=_no_calendar, incoming_end=None)
    assert v["decision"] == bg.REFUSED


def test_a_declaration_with_the_wrong_factor_does_not_apply():
    raw, adjusted = _pair(factor=4.0)
    f = bg.detect_rebasing(adjusted, raw)
    v = bg.decide("AAA", f, declarations=[_decl(factor=2.0)], splits_for=_no_calendar)
    assert v["decision"] == bg.REFUSED
    assert "1 declaration(s) for AAA exist but none matches" in v["reason"]
    # ...and one for another ticker is not even counted.
    v = bg.decide("AAA", f, declarations=[_decl(ticker="BBB")], splits_for=_no_calendar)
    assert v["decision"] == bg.REFUSED and "exist but none" not in v["reason"]


def test_declared_unadjusted_basis_mirrors_the_adjusted_one():
    raw, adjusted = _pair()
    f_raw = bg.detect_rebasing(adjusted, raw)
    f_adj = bg.detect_rebasing(raw, adjusted)
    keep_raw = [_decl(basis="unadjusted")]
    assert bg.decide("AAA", f_raw, declarations=keep_raw,
                     splits_for=_must_not_consult)["decision"] == bg.ADMITTED_BY_DECLARATION
    assert bg.decide("AAA", f_adj, declarations=keep_raw,
                     splits_for=_must_not_consult)["decision"] == bg.REFUSED_BY_DECLARATION


def test_split_calendar_admits_the_adjusted_series_and_refuses_the_raw_one():
    raw, adjusted = _pair(factor=2.0)
    cal = _calendar("2026-10-01", 2.0)
    f_adj = bg.detect_rebasing(raw, adjusted)
    v = bg.decide("AAA", f_adj, declarations=[], splits_for=cal)
    assert v["decision"] == bg.ADMITTED_BY_SPLIT_CALENDAR
    assert v["evidence"]["source"] == "vendor split calendar"
    f_raw = bg.detect_rebasing(adjusted, raw)         # the split served unapplied
    v = bg.decide("AAA", f_raw, declarations=[], splits_for=cal)
    assert v["decision"] == bg.REFUSED_BY_SPLIT_CALENDAR and "WS15" in v["reason"]


def test_without_calendar_or_declaration_the_rebasing_is_refused_not_accepted():
    """The WS15 guard fails OPEN when the calendar has nothing; this one
    fails CLOSED, which is the hole CTVA fell through."""
    raw, adjusted = _pair()
    f = bg.detect_rebasing(raw, adjusted)
    for cal, phrase in ((_no_calendar, "unavailable"),
                        (lambda t: pd.Series(dtype=float), "carries no split"),
                        (_calendar("2026-10-01", 3.0), "no matching split"),
                        (_calendar("2026-06-01", 4.0), "no matching split")):
        v = bg.decide("AAA", f, declarations=[], splits_for=cal)
        assert v["decision"] == bg.REFUSED and phrase in v["reason"], phrase


def test_a_declaration_wins_over_the_calendar_and_spares_the_lookup():
    raw, adjusted = _pair(factor=2.0)
    f_adj = bg.detect_rebasing(raw, adjusted)
    v = bg.decide("AAA", f_adj, declarations=[_decl(factor=2.0, basis="unadjusted")],
                  splits_for=_must_not_consult)
    assert v["decision"] == bg.REFUSED_BY_DECLARATION


# ---------------------------------------------------------------------------
# 3. Hold: the seam
# ---------------------------------------------------------------------------
def test_matching_seam_keeps_history_and_appends_only_what_the_source_serves():
    raw, adjusted = _pair(n_post=1)
    cache = adjusted                                   # ends 2026-10-01
    newer = _sessions_from("2026-10-02", 4)            # 2, 5, 6, 7 October
    incoming = pd.concat([raw, pd.Series([21.0, 21.5, np.nan, 22.0], index=newer)])
    incoming = pd.concat([pd.Series([1.0], index=[cache.index[0] - pd.Timedelta(days=7)]),
                          incoming])                   # a bar before the cached history
    held, seam = bg.hold_column(cache, incoming, incoming.index)
    assert seam["matched"] and seam["seam_date"] == "2026-10-01"
    assert seam["appended_sessions"] == ["2026-10-02", "2026-10-05", "2026-10-07"]
    # Every cached cell as it was.
    assert np.array_equal(held.reindex(cache.index).to_numpy(), cache.to_numpy())
    # Appended values are the source's, the gap stays a gap, nothing earlier taken.
    assert held.loc["2026-10-02"] == 21.0 and held.loc["2026-10-07"] == 22.0
    assert np.isnan(held.loc["2026-10-06"])
    assert np.isnan(held.iloc[0])


def test_mismatching_seam_appends_nothing_and_loses_nothing():
    raw, adjusted = _pair()
    cache = raw.loc[:"2026-09-30"]                     # raw history
    held, seam = bg.hold_column(cache, adjusted, adjusted.index)
    assert seam["matched"] is False and "differs" in seam["reason"]
    assert seam["appended_sessions"] == []
    assert held.dropna().index.max() == pd.Timestamp("2026-09-30")
    assert np.array_equal(held.reindex(cache.index).to_numpy(), cache.to_numpy())


def test_source_lacking_the_seam_session_appends_nothing():
    raw, adjusted = _pair()
    incoming = raw.copy()
    incoming.loc["2026-10-02"] = np.nan                # the seam session withheld
    held, seam = bg.hold_column(adjusted, incoming, incoming.index)
    assert seam["matched"] is False and "does not serve the seam" in seam["reason"]


def test_year_boundary_seam_appends_the_new_year_sessions():
    raw, adjusted = _pair(pre_end="2025-12-31", post_start="2026-01-02", n_post=1)
    cache = adjusted.loc[:"2025-12-31"]
    newer = pd.DatetimeIndex(["2026-01-02", "2026-01-05", "2026-01-06"])
    incoming = pd.concat([adjusted.loc[:"2025-12-31"],
                          pd.Series([19.0, 19.5, 20.0], index=newer)])
    held, seam = bg.hold_column(cache, incoming, incoming.index)
    assert seam["matched"] and seam["seam_date"] == "2025-12-31"
    assert seam["appended_sessions"] == ["2026-01-02", "2026-01-05", "2026-01-06"]
    assert held.loc["2026-01-06"] == 20.0


def test_held_column_keeps_the_cached_dtype_unless_an_appended_value_needs_width():
    raw, adjusted = _pair(n_post=1)
    cache32 = adjusted.astype(np.float32)
    held, seam = bg.hold_column(cache32, raw.astype(np.float32), raw.index)
    assert held.dtype == np.float32 and seam["matched"]
    newer = _sessions_from("2026-10-02", 1)
    # The same float32 closes served as float64 (the seam still compares
    # equal) plus one value that needs float64 width.
    incoming64 = pd.concat([raw.astype(np.float32).astype(np.float64),
                            pd.Series([21.123456789], index=newer)])
    held, seam = bg.hold_column(cache32, incoming64, incoming64.index)
    assert seam["matched"] and seam["appended_sessions"] == ["2026-10-02"]
    assert held.dtype == np.float64 and held.loc["2026-10-02"] == 21.123456789


# ---------------------------------------------------------------------------
# 4. The frame: hold, provenance, the log
# ---------------------------------------------------------------------------
def test_only_the_rebased_column_is_held_and_the_record_says_so(capsys):
    raw, adjusted = _pair()
    other = pd.Series(np.linspace(50.0, 55.0, len(raw)), index=raw.index)
    prior = pd.DataFrame({"AAA": adjusted, "BBB": other})
    close = pd.DataFrame({"AAA": raw, "BBB": other * 1.001})   # BBB: dividend drift
    out, rec = bg.hold_rebased_columns(close, prior, declarations=[_decl()],
                                       splits_for=_must_not_consult,
                                       declarations_path=Path("x.json"))
    assert rec["columns_checked"] == 2
    assert [r["column"] for r in rec["refused"]] == ["AAA"] and rec["admitted"] == []
    assert rec["refused"][0]["decision"] == bg.REFUSED_BY_DECLARATION
    assert rec["refused"][0]["seam"]["matched"] is True
    assert np.array_equal(out["AAA"].to_numpy(), adjusted.to_numpy())
    assert np.array_equal(out["BBB"].to_numpy(), close["BBB"].to_numpy())
    assert np.array_equal(close["AAA"].to_numpy(), raw.to_numpy()), "caller's frame mutated"
    bg.report(rec, label="prices_cache_t")
    text = capsys.readouterr().out
    assert "REFUSED AAA" in text and "1 of 2 column(s) re-based" in text
    assert "declared basis 'adjusted'" in text


def test_admitted_column_is_taken_whole_and_logged(capsys):
    raw, adjusted = _pair()
    prior = pd.DataFrame({"AAA": raw.loc[:"2026-09-30"]})
    close = pd.DataFrame({"AAA": adjusted})
    out, rec = bg.hold_rebased_columns(close, prior, declarations=[_decl()],
                                       splits_for=_must_not_consult)
    assert [r["column"] for r in rec["admitted"]] == ["AAA"] and rec["refused"] == []
    assert np.array_equal(out["AAA"].to_numpy(), adjusted.to_numpy())
    bg.report(rec)
    assert "ADMITTED AAA" in capsys.readouterr().out


def test_undeclared_refusal_prints_the_declaration_hint(capsys):
    raw, adjusted = _pair()
    out, rec = bg.hold_rebased_columns(pd.DataFrame({"AAA": raw}),
                                       pd.DataFrame({"AAA": adjusted}),
                                       declarations=[], splits_for=_no_calendar)
    bg.report(rec)
    text = capsys.readouterr().out
    assert '"ticker": "AAA"' in text and '"factor": 4' in text
    assert "'unadjusted' admits this series, 'adjusted' keeps the cached one" in text


def test_quiet_line_when_nothing_is_rebased_and_silence_without_a_cache(capsys):
    raw, _ = _pair()
    frame = pd.DataFrame({"AAA": raw})
    _, rec = bg.hold_rebased_columns(frame, frame * 1.001, splits_for=_must_not_consult)
    bg.report(rec, label="p")
    assert "p: Basis guard: no re-based column among 1" in capsys.readouterr().out
    _, rec = bg.hold_rebased_columns(frame, None, splits_for=_must_not_consult)
    bg.report(rec)
    assert capsys.readouterr().out == "" and rec["columns_checked"] == 0


def test_split_provenance_records_a_refused_overlay_column_under_its_cached_basis():
    rec = {"refused": [{"column": "AAA", "prior_basis": pr.YFINANCE},
                       {"column": "BBB", "prior_basis": pr.NORGATE}]}
    taken, held = bg.split_provenance(rec, ["AAA", "BBB", "CCC"])
    assert taken == ["BBB", "CCC"] and held == ["AAA"]
    assert bg.split_provenance(None, ["AAA"]) == (["AAA"], [])


def test_declarations_loader_skips_what_it_cannot_trust(tmp_path):
    assert bg.load_declarations(tmp_path / "absent.json") == ([], [])
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    decls, warns = bg.load_declarations(bad)
    assert decls == [] and "could not be read" in warns[0]
    mixed = _write_declarations(
        tmp_path / "mixed.json",
        _decl(),                                   # fine
        {"ticker": "BBB", "effective": "2026-13-01", "factor": 2, "basis": "adjusted"},
        {"ticker": "CCC", "effective": "2026-10-01", "factor": 1.0, "basis": "adjusted"},
        {"ticker": "DDD", "effective": "2026-10-01", "factor": 2.0, "basis": "raw"},
        {"ticker": "EEE"})
    decls, warns = bg.load_declarations(mixed)
    assert [d["ticker"] for d in decls] == ["AAA"]
    assert len(warns) == 4 and all("skipped" in w for w in warns)
    assert any("entry 1" in w for w in warns) and any("entry 4" in w for w in warns)


# ---------------------------------------------------------------------------
# 5. Through download_prices: the cache on disk, the ledger, the sidecar
# ---------------------------------------------------------------------------
@pytest.fixture
def site(tmp_path, monkeypatch):
    """A cache under tmp/data so the ledger lands under tmp/logs; the split
    calendar unavailable; no declaration unless a test writes one."""
    cache = tmp_path / "data" / "prices_cache_t.parquet"
    cache.parent.mkdir(parents=True)
    monkeypatch.setattr(cb, "_splits_for", _no_calendar)
    monkeypatch.setattr(cb, "DECLARATIONS_PATH", tmp_path / "data" / "none.json")
    return tmp_path, cache


def _run(monkeypatch, cache, frame, **kw):
    monkeypatch.setattr(cb.yf, "download", lambda *a, **k: _multi(frame),
                        raising=False)
    start = str(frame.index[0].date())
    end = str((frame.index[-1] + pd.Timedelta(days=3)).date())
    kw.setdefault("price_source", "yfinance")
    return cb.download_prices(["AAA"], start, end, cache_path=cache, roster=None,
                              tail_probe=False, **kw)


def _ledger_last(root: Path) -> dict:
    lines = pr.cache_ledger_path(root).read_text(encoding="utf-8").splitlines()
    return json.loads(lines[-1])


def test_download_prices_holds_the_cached_basis_against_the_raw_cliff(site, monkeypatch, capsys):
    """The 2026-10-10 shape under a yfinance fallback: the cache carries the
    adjusted history and the source serves the raw cliff plus a new session.
    The written cache keeps every adjusted cell and gains the new session."""
    root, cache = site
    raw, adjusted = _pair(n_post=1)                          # through 2026-10-01
    pd.DataFrame({"AAA": adjusted}).to_parquet(cache)
    newer = _sessions_from("2026-10-02", 1)
    served = pd.DataFrame({"AAA": pd.concat([raw, pd.Series([21.0], index=newer)])})
    monkeypatch.setattr(cb, "DECLARATIONS_PATH",
                        _write_declarations(root / "data" / "decl.json", _decl()))
    out = _run(monkeypatch, cache, served)
    written = pd.read_parquet(cache)["AAA"]
    assert np.array_equal(written.reindex(adjusted.index).to_numpy(), adjusted.to_numpy())
    assert written.loc["2026-10-02"] == 21.0 and out.loc["2026-10-02", "AAA"] == 21.0
    text = capsys.readouterr().out
    assert "REFUSED AAA" in text and "1 new session(s) appended across an exact seam at 2026-10-01" in text
    rec = _ledger_last(root)
    assert rec["kind"] == "cache_diff" and rec["panel"] == "prices_cache_t"
    guard = rec["basis_guard"]
    assert [r["column"] for r in guard["refused"]] == ["AAA"]
    assert guard["refused"][0]["decision"] == bg.REFUSED_BY_DECLARATION
    assert guard["refused"][0]["cells"] == 300 and guard["refused"][0]["ratio"] == pytest.approx(4.0)
    assert guard["refused"][0]["seam"]["appended_sessions"] == ["2026-10-02"]
    sidecar = json.loads(ps.sidecar_path(cache).read_text(encoding="utf-8"))
    assert sidecar["basis_guard"]["refused"][0]["column"] == "AAA"
    assert pr.summarise(pr.cache_ledger_path(root))["prices_cache_t"]["basis_refused"] == 1


def test_download_prices_refuses_an_undeclared_rebasing_and_freezes_at_the_seam(site, monkeypatch, capsys):
    """The 01:21 UTC 2026-10-03 shape with the guard in place: a raw cache,
    the adjusted series arriving, nothing declared. The cache is unchanged
    byte for byte and the log says what would decide it."""
    root, cache = site
    raw, adjusted = _pair()
    old = pd.DataFrame({"AAA": raw.loc[:"2026-09-30"]})
    old.to_parquet(cache)
    out = _run(monkeypatch, cache, pd.DataFrame({"AAA": adjusted}))
    written = pd.read_parquet(cache)
    assert np.array_equal(written["AAA"].reindex(old.index).to_numpy(), old["AAA"].to_numpy())
    assert written["AAA"].dropna().index.max() == pd.Timestamp("2026-09-30")
    assert out["AAA"].dropna().index.max() == pd.Timestamp("2026-09-30")
    text = capsys.readouterr().out
    assert "REFUSED AAA" in text and "does not match" in text and "To decide it, declare" in text
    guard = _ledger_last(root)["basis_guard"]
    assert guard["refused"][0]["decision"] == bg.REFUSED
    assert guard["refused"][0]["seam"]["matched"] is False


def test_download_prices_admits_the_declared_basis_whole(site, monkeypatch, capsys):
    root, cache = site
    raw, adjusted = _pair()
    pd.DataFrame({"AAA": raw.loc[:"2026-09-30"]}).to_parquet(cache)
    monkeypatch.setattr(cb, "DECLARATIONS_PATH",
                        _write_declarations(root / "data" / "decl.json", _decl()))
    _run(monkeypatch, cache, pd.DataFrame({"AAA": adjusted}))
    written = pd.read_parquet(cache)["AAA"]
    assert np.array_equal(written.to_numpy(), adjusted.to_numpy())
    assert "ADMITTED AAA" in capsys.readouterr().out
    guard = _ledger_last(root)["basis_guard"]
    assert guard["admitted"][0]["decision"] == bg.ADMITTED_BY_DECLARATION and guard["refused"] == []


def test_download_prices_catches_a_rebasing_that_arrives_through_the_norgate_overlay(site, monkeypatch, capsys):
    """How CTVA actually reached the production caches: the yfinance column
    agreed with the cache and Norgate's adjusted column replaced it whole
    under the superset rule. The guard runs after the overlay, so it sees it,
    and a refused overlay column is recorded under its CACHED basis."""
    root, cache = site
    raw, adjusted = _pair()
    old = pd.DataFrame({"AAA": raw.loc[:"2026-09-30"]})
    old.to_parquet(cache)
    import norgate_prices as npx
    monkeypatch.setattr(npx, "available", lambda: True)
    monkeypatch.setattr(npx, "fetch_closes",
                        lambda tickers, start, end, verbose=True: (
                            pd.DataFrame({"AAA": adjusted}), ["AAA"], []))
    _run(monkeypatch, cache, pd.DataFrame({"AAA": raw}), price_source="auto")
    written = pd.read_parquet(cache)["AAA"]
    assert np.array_equal(written.reindex(old.index).to_numpy(), old["AAA"].to_numpy())
    assert written.dropna().index.max() == pd.Timestamp("2026-09-30"), \
        "the raw cliff must not be appended onto the kept history either"
    text = capsys.readouterr().out
    assert "taken from Norgate" in text and "REFUSED AAA" in text
    guard = _ledger_last(root)["basis_guard"]
    assert guard["refused"][0]["incoming_basis"] == pr.NORGATE
    assert guard["refused"][0]["prior_basis"] == pr.YFINANCE
    sidecar = json.loads(ps.sidecar_path(cache).read_text(encoding="utf-8"))
    assert sidecar["columns_from_norgate"] == []
    assert sidecar["columns_kept_on_incumbent"] == ["AAA"]


def test_download_prices_never_loses_a_populated_cached_cell(site, monkeypatch):
    """The never-go-backwards property, over both refusal outcomes."""
    root, cache = site
    raw, adjusted = _pair(n_post=1)
    for prior_col, served_col in ((adjusted, raw), (raw.loc[:"2026-09-30"], adjusted)):
        old = pd.DataFrame({"AAA": prior_col})
        old.to_parquet(cache)
        ps.sidecar_path(cache).unlink(missing_ok=True)
        _run(monkeypatch, cache, pd.DataFrame({"AAA": served_col}))
        new = pd.read_parquet(cache)["AAA"]
        had = old["AAA"].dropna()
        assert had.index.isin(new.dropna().index).all()
        assert np.array_equal(new.reindex(had.index).to_numpy(), had.to_numpy())


def test_a_first_write_with_no_cache_is_not_guarded(site, monkeypatch, capsys):
    root, cache = site
    raw, _ = _pair()
    _run(monkeypatch, cache, pd.DataFrame({"AAA": raw}))
    assert np.array_equal(pd.read_parquet(cache)["AAA"].to_numpy(), raw.to_numpy())
    assert "Basis guard" not in capsys.readouterr().out
    assert "basis_guard" not in _ledger_last(root)
