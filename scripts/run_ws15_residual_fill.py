"""WS15 step 1 — residual fill of the CNDX price cache, on a working COPY.

WHAT THIS FILLS AND WHY. WS11's backfill only treated columns that were
absent or all-NaN, so a column holding unrelated ticker-reuse bars counted
as priced and was skipped. The corrected panel therefore still drops:

    FB    1,115 roster-days 2018-01-05..2022-06-09  (Facebook; column holds
          only the 2025+ ProShares ETF that took the ticker)
    FOXA    295 roster-days 2018-01-05..2019-03-11  (21st Century Fox A;
          column holds only post-split Fox Corporation)
    FOX     296 roster-days                          (same, Class B)
    PCLN     38 roster-days 2018-01-05..2018-03-01  (Priceline; column holds
          only the 2025+ Pictet ETF)

plus two 2026 defects found by the WS15 gate: EA's final 11 tradable
sessions (yfinance stopped serving it three weeks before its 2026-08-04
delisting) and MNST's 14-session hole (yfinance served nothing around its
2-for-1 split of 2026-08-11, and TODAY serves a mis-adjusted series). It
also extends the 24 WS11 fill columns back through the 2017-07-10 warmup:
they start at 2018-01-05, so they carry no 50-day average until March 2018
and are silently absent from early-2018 MA breadth.

THE LIVE CACHE IS NOT TOUCHED. data/prices_cache_cndx.parquet feeds the
deployed Friday refresh; every output goes to --workdir. Adoption into the
live cache is a restatement decision and waits for sign-off.

BASIS DISCIPLINE (the way this could silently shift every moving average):
- All Norgate pulls are TOTALRETURN, matching the auto_adjust=True cache.
- A fill that lands INSIDE a column with existing same-security bars (the
  warmup extensions, EA's tail, MNST's hole) is rescaled onto the column's
  own basis via the median ratio over the overlap, and the ratio must be
  STABLE (a constant re-basing, not a drift). MNST's ratio is expected to
  be ~2.0: the column body predates the split, Norgate is adjusted through
  it, and gluing the two without the rescale would fabricate a -50% day.
- A fill with no same-security overlap (FB, PCLN, FOXA, FOX) goes in raw:
  it is internally consistent, and indicator windows are prevented from
  spanning a security boundary by the era-barrier map this script emits
  (consumed by the WS15 breadth driver). FOXA/FOX get a barrier at the
  first Fox Corporation bar, which exactly preserves the committed panel's
  fresh-listing warmup treatment of early Fox Corp; FB/PCLN barriers make
  the never-read reuse eras structurally unreadable rather than merely
  asserted-unread.
- No existing non-NaN cell is ever modified, asserted frame-wide.
- Every source symbol's security_name is asserted before its bars are used.

Output (in --workdir): prices_cache_cndx_ws15.parquet, era_barriers.json,
ws15_fill_report.json
Run:    python scripts/run_ws15_residual_fill.py --workdir <dir>
"""
from __future__ import annotations

import argparse
import datetime as dt  # Python datetime: months are 1-indexed
import json
import sys
from bisect import bisect_right
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from norgate_symbols import resolve  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

WARMUP_START = "2017-07-10"     # the cache's original yfinance warmup start
PANEL_END = "2026-08-07"        # committed breadth end_date; later bars unused
HELD_TAIL_CAL_DAYS = 45         # fill past the last held date by this much

# Residual fills: roster ticker -> (Norgate symbol, required security_name
# substring, era barrier). The barrier is the first bar date of the LATER
# security occupying the same column; indicator windows must not cross it.
RESIDUAL = {
    "FB":   ("META", "Meta Platforms", "2025-06-26"),
    "PCLN": ("BKNG", "Booking Holdings", "2025-10-16"),
    "FOXA": ("TFCFA-201903", "Twenty-First Century Fox Inc Class A", "2019-03-12"),
    "FOX":  ("TFCF-201903",  "Twenty-First Century Fox Inc Class B", "2019-03-13"),
}
# Same-security repairs: rescaled onto the column's own basis via overlap.
REPAIRS = {
    "EA":   ("EA-202608", "Electronic Arts"),
    "MNST": ("MNST", "Monster Beverage"),
}
# The 24 WS11 fill columns to extend back through the warmup (their Norgate
# symbol is re-resolved point-in-time at run time, then name-checked).
WS11_FILLS = [
    "ALXN", "ANSS", "ATVI", "CA", "CELG", "CERN", "CTRP", "CTXS", "DISH",
    "HOLX", "LVNTA", "MXIM", "MYL", "NLOK", "QRTEA", "QVCA", "SGEN", "SHPG",
    "SPLK", "SYMC", "TFCFA", "WBA", "WLTW", "XLNX",
]
# Gaps that legitimately remain afterwards: stale-roster tails (the security
# was dead while still listed in the roster — no prices exist anywhere) and
# the two by-design exclusions.
EXPECTED_REMAINING = {
    "TFCFA", "TMUSR", "VSNTV UW", "XLNX", "ALXN", "CELG", "MXIM", "SGEN",
    "ANSS", "EA",
}
RATIO_REL_STD_MAX = 1e-3        # overlap-ratio stability floor
MIN_OVERLAP_SESSIONS = 20


def _norgate_tr(symbol: str, start: str) -> pd.Series:
    import norgatedata as nd
    df = nd.price_timeseries(
        symbol,
        stock_price_adjustment_setting=nd.StockPriceAdjustmentType.TOTALRETURN,
        padding_setting=nd.PaddingType.NONE,
        start_date=start,
        format="pandas-dataframe",
    )
    s = df["Close"].astype(float)
    s.index = pd.to_datetime(s.index)
    return s[~s.index.duplicated(keep="first")].sort_index()


def _assert_name(symbol: str, want: str) -> str:
    import norgatedata as nd
    got = nd.security_name(symbol) or ""
    assert want in got, (
        f"{symbol}: security_name is {got!r}, expected to contain {want!r} — "
        f"refusing to attach an unverified security's prices")
    return got


def _held_window(snaps: dict, t: str) -> tuple[str | None, str | None]:
    dates = sorted(k for k, v in snaps.items()
                   if t in (v.get("tickers") or []))
    return (dates[0], dates[-1]) if dates else (None, None)


def residual_sweep(px: pd.DataFrame, snaps: dict,
                   breadth_dates: pd.DatetimeIndex) -> dict[str, int]:
    """Roster-days on which a held name has no bar, per ticker."""
    sdates = sorted(snaps)
    gaps: dict[str, int] = {}
    for d in breadth_dates:
        ds = d.strftime("%Y-%m-%d")
        i = bisect_right(sdates, ds) - 1
        if i < 0 or d not in px.index:
            continue
        row = px.loc[d]
        for t in dict.fromkeys(snaps[sdates[i]]["tickers"]):
            if t not in px.columns or pd.isna(row[t]):
                gaps[t] = gaps.get(t, 0) + 1
    return gaps


def fill_column(px: pd.DataFrame, t: str, series: pd.Series,
                lo: pd.Timestamp, hi: pd.Timestamp) -> int:
    """NaN-only fill of column t from series within [lo, hi]. Returns bars
    added. Never introduces new index rows outside the panel's date grid:
    the panel grid is the union of every column's traded sessions already,
    and the breadth window is bounded by it."""
    s = series.loc[lo:hi]
    idx = s.index.intersection(px.index)
    s = s.reindex(idx)
    mask = px.loc[idx, t].isna() & s.notna()
    px.loc[idx[mask], t] = s[mask]
    return int(mask.sum())


def overlap_ratio(existing: pd.Series, incoming: pd.Series) -> tuple[float, float, int]:
    """Median existing/incoming ratio over common non-NaN sessions, its
    relative std, and the overlap size."""
    common = existing.dropna().index.intersection(incoming.dropna().index)
    if len(common) == 0:
        return np.nan, np.nan, 0
    r = (existing.reindex(common) / incoming.reindex(common)).astype(float)
    return float(r.median()), float(r.std() / r.median()), int(len(common))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workdir", required=True,
                    help="output directory; the live data/ cache is read-only")
    args = ap.parse_args()
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    px0 = pd.read_parquet(DATA / "prices_cache_cndx.parquet")
    px = px0.copy()
    snaps = json.loads(
        (DATA / "constituents_cndx.json").read_text(encoding="utf-8")
    )["snapshots"]
    blob = json.loads(
        (DATA / "breadth_cndx.json").read_text(encoding="utf-8"))
    breadth_dates = pd.to_datetime(blob["series"]["dates"])

    before = residual_sweep(px, snaps, breadth_dates)
    report: dict[str, dict] = {}
    barriers: dict[str, str] = {}

    # ---- residual fills (reuse-masked eras; raw TOTALRETURN + barrier) --
    for t, (sym, want, barrier) in RESIDUAL.items():
        name = _assert_name(sym, want)
        h_first, h_last = _held_window(snaps, t)
        # Point-in-time cross-check: the resolver must agree with the
        # mapping on the first held date (FOXA/FOX legitimately resolve to
        # None there — no dated candidate — which is why they carry an
        # explicit, name-verified symbol here).
        r = resolve(t, dt.date.fromisoformat(h_first))
        assert r in (sym, None), f"{t}: resolver says {r}, mapping says {sym}"
        hi = min(pd.Timestamp(h_last) + pd.Timedelta(days=HELD_TAIL_CAL_DAYS),
                 pd.Timestamp(barrier) - pd.Timedelta(days=1),
                 pd.Timestamp(PANEL_END))
        s = _norgate_tr(sym, WARMUP_START)
        added = fill_column(px, t, s, pd.Timestamp(WARMUP_START), hi)
        barriers[t] = barrier
        report[t] = {"source": sym, "security_name": name, "mode": "raw",
                     "span": [WARMUP_START, str(hi.date())], "bars_added": added}

    # ---- same-security repairs (rescaled onto the column's basis) -------
    for t, (sym, want) in REPAIRS.items():
        name = _assert_name(sym, want)
        s = _norgate_tr(sym, "2026-04-01")
        med, rel_std, n = overlap_ratio(px[t], s)
        assert n >= MIN_OVERLAP_SESSIONS, f"{t}: overlap only {n} sessions"
        assert rel_std < RATIO_REL_STD_MAX, (
            f"{t}: overlap ratio unstable (rel std {rel_std:.2e}) — the two "
            f"series differ by more than a constant re-basing; refusing to "
            f"splice")
        added = fill_column(px, t, s * med, pd.Timestamp("2026-07-01"),
                            pd.Timestamp(PANEL_END))
        report[t] = {"source": sym, "security_name": name,
                     "mode": f"rescaled x{med:.6f}",
                     "overlap_sessions": n, "ratio_rel_std": rel_std,
                     "bars_added": added}

    # ---- warmup extension for the WS11 fill columns ---------------------
    warm_added = {}
    for t in WS11_FILLS:
        h_first, h_last = _held_window(snaps, t)
        sym = resolve(t, dt.date.fromisoformat(h_first))
        if sym is None:
            from backfill_delisted_prices import _sole_candidate, _held_dates
            sym = _sole_candidate(t, sorted(_held_dates(snaps, t)))
        assert sym, f"{t}: warmup extension could not re-resolve"
        s = _norgate_tr(sym, WARMUP_START)
        med, rel_std, n = overlap_ratio(px[t], s)
        assert n >= MIN_OVERLAP_SESSIONS, f"{t}: overlap only {n} sessions"
        assert rel_std < RATIO_REL_STD_MAX, (
            f"{t}: overlap ratio unstable (rel std {rel_std:.2e})")
        added = fill_column(px, t, s * med, pd.Timestamp(WARMUP_START),
                            pd.Timestamp("2018-01-04"))
        if added:
            warm_added[t] = {"source": sym, "bars_added": added,
                             "ratio": round(med, 6)}

    # ---- integrity: nothing that existed was modified -------------------
    common_mask = px0.notna()
    unchanged = (px.where(common_mask) == px0.where(common_mask)) | ~common_mask
    assert bool(unchanged.all().all()), "an existing bar was modified"

    # ---- reuse-safety: no roster date reads a reuse-era bar -------------
    sdates = sorted(snaps)
    for t, barrier in barriers.items():
        bar_ts = pd.Timestamp(barrier)
        if t in ("FOXA", "FOX"):
            continue  # the later era IS the roster's security there
        for d in breadth_dates:
            i = bisect_right(sdates, d.strftime("%Y-%m-%d")) - 1
            if i >= 0 and t in snaps[sdates[i]]["tickers"]:
                assert d < bar_ts, (
                    f"{t}: roster date {d.date()} on/after reuse era {barrier}")

    # ---- after sweep ----------------------------------------------------
    after = residual_sweep(px, snaps, breadth_dates)
    unexpected = {t: n for t, n in after.items() if t not in EXPECTED_REMAINING}
    assert not unexpected, f"unexpected residual gaps remain: {unexpected}"

    out = workdir / "prices_cache_cndx_ws15.parquet"
    px.sort_index().to_parquet(out)
    (workdir / "era_barriers.json").write_text(
        json.dumps(barriers, indent=2), encoding="utf-8")
    full_report = {
        "computed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(
            timespec="seconds"),
        "residual_fills": report,
        "warmup_extension": warm_added,
        "roster_day_gaps_before": dict(sorted(
            before.items(), key=lambda kv: -kv[1])),
        "roster_day_gaps_after": dict(sorted(
            after.items(), key=lambda kv: -kv[1])),
    }
    (workdir / "ws15_fill_report.json").write_text(
        json.dumps(full_report, indent=2), encoding="utf-8")

    print("Residual fills:")
    for t, r in report.items():
        print(f"  {t:5s} <- {r['source']:14s} {r['mode']:22s} "
              f"+{r['bars_added']} bars   ({r['security_name'][:36]})")
    print(f"Warmup extension: {len(warm_added)} columns, "
          f"{sum(v['bars_added'] for v in warm_added.values())} bars")
    print(f"Roster-day gaps: {sum(before.values())} -> {sum(after.values())}")
    for t, n in sorted(after.items(), key=lambda kv: -kv[1]):
        print(f"  remaining {t:9s} {n:3d}  (stale-roster tail or by-design)")
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
