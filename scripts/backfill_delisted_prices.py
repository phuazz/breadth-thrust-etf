"""Backfill delisted/renamed constituents into the price caches from Norgate.

THE PROBLEM. Constituent prices come from yfinance, which serves history only
under a security's CURRENT symbol. Every name later acquired, taken private or
renamed therefore has no history under the ticker it carried while it was in
the index, and compute_breadth drops it from BOTH numerator and denominator.
CNDX coverage is 81.6% in 2018 and only clears 99% in 2025 for that reason, so
the early backtest is measured on survivors.

WHAT THIS DOES. For every historical constituent with no usable price, resolve
the Norgate symbol that was quoting it ON A DATE IT WAS ACTUALLY HELD (see
norgate_symbols for why point-in-time matters — resolving by ticker alone
attaches whoever holds it now), pull the close series, and write it into the
panel's price cache under the ROSTER's ticker so downstream code needs no
change.

SCOPE. Norgate here is US Equities + US Equities Delisted, so this addresses
US-constituent panels only. Panels whose CONSTITUENTS are foreign — ICHN,
IJPN, ITWN, NDIA, IDP6, and the XETR-listed Europe sector panels — are out of
reach regardless of the ETF's own trading calendar, and are skipped explicitly
rather than half-filled.

PRICE BASIS. compute_breadth downloads with yfinance ``auto_adjust=True``,
so the closes already in each cache are adjusted for splits AND dividends —
a total-return series. Norgate is therefore asked for TOTALRETURN too. Do not
"fix" this to a capital-only adjustment: it looks more like a price series,
but it would put a price series beside dividend-reinvested ones in the same
panel and shift every moving average computed across them. This was written
the wrong way round first time and caught only by reading auto_adjust.

LOCAL ONLY. Norgate is a local Windows service; CI has neither it nor the
gitignored parquet caches, which is already true of the whole constituent
refresh.

    python scripts/backfill_delisted_prices.py --etf CNDX --dry-run
    python scripts/backfill_delisted_prices.py --all
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
sys.path.insert(0, str(Path(__file__).resolve().parent))

from norgate_symbols import NOT_EQUITY, NorgateUnavailable, resolve  # noqa: E402

# Panels whose CONSTITUENTS are US equities. The ETF's own trading_calendar
# does not decide this: ICHN and NDIA are NYSE-listed funds holding Chinese
# and Indian equities, which Norgate's US databases do not carry.
US_CONSTITUENT_PANELS = {
    "CNDX", "CSP1", "SOXX",
    "IUCD", "IUCM", "IUCS", "IUES", "IUFS", "IUHC",
    "IUIS", "IUIT", "IUMS", "IUSP", "IUUS",
}


def _unpriced(px: pd.DataFrame, universe: set[str],
              snapshots: dict | None = None,
              min_held_coverage: float = 0.5) -> set[str]:
    """Constituents with no usable close over the window they were HELD.

    Absent and all-NaN columns, as before — plus (WS15) columns that look
    priced only because an unrelated reuse of the ticker put bars OUTSIDE
    the held window. FB held 2018-2022 with bars only from the 2025 ETF that
    took the ticker is unpriced where it matters; the original all-NaN test
    judged it served and skipped it. A name is therefore also unpriced when
    fewer than ``min_held_coverage`` of its held snapshot dates carry a bar.
    The threshold is deliberately loose: a genuinely priced name covers
    ~100% of its held dates and a reuse-masked one ~0%, so anything near
    the line deserves the resolver's attention rather than silence.
    """
    absent = universe - set(px.columns)
    empty = {t for t in universe & set(px.columns) if px[t].dropna().empty}
    masked: set[str] = set()
    if snapshots:
        idx = set(px.index)
        for t in universe & set(px.columns) - empty:
            held = [pd.Timestamp(d) for d in _held_dates(snapshots, t)]
            if not held:
                continue
            col = px[t]
            covered = sum(1 for d in held if d in idx and pd.notna(col.loc[d]))
            if covered / len(held) < min_held_coverage:
                masked.add(t)
    return absent | empty | masked


def _held_dates(snapshots: dict, ticker: str) -> list[str]:
    return [k for k, v in snapshots.items() if ticker in (v.get("tickers") or [])]


def _sole_candidate(ticker: str, held: list[str],
                     tolerance_days: int = 400) -> str | None:
    """The one Norgate symbol ever using this ticker, if it plausibly IS the
    held security. None when there are 0 or >1 candidates, or when the only
    candidate stopped quoting long before the name was held.

    Used when a roster outlived its security so no held date resolves.
    Uniqueness alone is not enough: the roster carries "FI" because Fiserv
    took that ticker in 2024, while Norgate's only FI is FI-199808, a company
    that died in 1998. Matching those would attach 1990s prices to a 2024
    constituent. Nothing was written in that case only because the series
    predates the fetch window — luck, not a guard.

    The tolerance covers the real case this exists for: a roster lagging a
    delisting by days or weeks (TFCFA, three weeks), not by decades.
    """
    from norgate_symbols import _candidates, _window
    cands = _candidates().get(ticker, [])
    if len(cands) != 1 or not held:
        return None
    sym = cands[0]
    first, last = _window(sym)
    if not first:
        return None
    held_first = date.fromisoformat(held[0])
    held_last = date.fromisoformat(held[-1])
    if first > held_last:
        return None                       # listed only after it was held
    if last is not None and (held_first - last).days > tolerance_days:
        return None                       # died long before it was held
    return sym


def _norgate_close(symbol: str, start: str) -> pd.Series | None:
    import norgatedata as nd
    try:
        df = nd.price_timeseries(
            symbol,
            stock_price_adjustment_setting=nd.StockPriceAdjustmentType.TOTALRETURN,
            padding_setting=nd.PaddingType.NONE,
            start_date=start,
            format="pandas-dataframe",
        )
    except Exception:
        return None
    if df is None or df.empty or "Close" not in df:
        return None
    s = df["Close"].astype(float)
    s.index = pd.to_datetime(s.index)
    return s[~s.index.duplicated(keep="first")].sort_index()


def backfill(etf: str, dry_run: bool = False) -> dict:
    cpath = DATA_DIR / f"constituents_{etf.lower()}.json"
    ppath = DATA_DIR / f"prices_cache_{etf.lower()}.parquet"
    if not cpath.exists() or not ppath.exists():
        return {"etf": etf, "skipped": "no constituents or price cache"}

    snaps = json.loads(cpath.read_text(encoding="utf-8")).get("snapshots", {})
    universe: set[str] = set()
    for v in snaps.values():
        universe |= set(v.get("tickers") or [])
    px = pd.read_parquet(ppath)
    todo = sorted(_unpriced(px, universe, snaps))
    start = min(snaps) if snaps else "2018-01-01"

    filled, unresolved, nodata, ambiguous, stale_roster = {}, [], [], [], []
    excluded: list[str] = []
    for t in todo:
        held = sorted(_held_dates(snaps, t))
        if not held:
            continue
        # Resolve on the FIRST date held, not the last. A roster can outlive
        # the security: iShares carried TFCFA for three weeks after 21st
        # Century Fox stopped quoting on 2019-03-19, so asking on the last
        # held date asks for a security that was already dead.
        first_sym = resolve(t, date.fromisoformat(held[0]))
        # But one ticker can cover two different companies across the panel's
        # life, and a single column cannot represent both. Compare the ends:
        # if they disagree, say so rather than silently picking one era's
        # prices and labelling them with the whole history.
        last_sym = resolve(t, date.fromisoformat(held[-1]))
        if first_sym and last_sym and first_sym != last_sym:
            ambiguous.append(f"{t}: {first_sym} @{held[0]} vs {last_sym} @{held[-1]}")
            continue
        if t in NOT_EQUITY:
            excluded.append(t)
            continue
        sym = first_sym or last_sym
        if not sym:
            # Every held date can post-date the delisting: iShares only listed
            # TFCFA in the roster AFTER 21st Century Fox stopped quoting, so
            # there is no date on which it was both held and alive. Falling
            # back to the security's own window is safe ONLY when the ticker
            # is unambiguous across all time — with two candidates there is no
            # basis to choose, and guessing is what this whole module avoids.
            sym = _sole_candidate(t, held)
            if sym:
                stale_roster.append(f"{t}->{sym}")
        if not sym:
            unresolved.append(t)
            continue
        s = _norgate_close(sym, start)
        if s is None or s.empty:
            nodata.append(f"{t}->{sym}")
            continue
        filled[t] = (sym, s)

    if not dry_run and filled:
        # NaN-only cell writes (WS15): a fill must never overwrite an
        # existing bar — neither a live era sharing the column with the
        # recovered security, nor an earlier fill. Replacing the whole
        # column was how a Fox Corporation era could be deleted by filling
        # the 21st Century Fox era beneath it.
        out = px.copy()
        for t, (_sym, s) in filled.items():
            out = out.reindex(out.index.union(s.index)).sort_index()
            if t not in out.columns:
                out[t] = float("nan")
            aligned = s.reindex(out.index)
            mask = out[t].isna() & aligned.notna()
            out.loc[mask, t] = aligned[mask]
        out = out.sort_index()
        out.to_parquet(ppath)

    return {
        "etf": etf,
        "universe": len(universe),
        "unpriced_before": len(todo),
        "filled": len(filled),
        "unresolved": unresolved,
        "excluded": excluded,
        "ambiguous": ambiguous,
        "stale_roster": stale_roster,
        "no_norgate_data": nodata,
        "rows_before": len(px),
        "rows_after": (len(px) if dry_run or not filled
                        else len(pd.read_parquet(ppath))),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--etf", action="append", help="panel(s) to backfill")
    ap.add_argument("--all", action="store_true",
                    help="every US-constituent panel")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.all:
        etfs = sorted(US_CONSTITUENT_PANELS)
    elif args.etf:
        etfs = [e.upper() for e in args.etf]
        outside = [e for e in etfs if e not in US_CONSTITUENT_PANELS]
        if outside:
            print(f"REFUSING {outside}: constituents are not US equities, so "
                  f"Norgate's US databases cannot price them. Filling these "
                  f"partially would look like improved coverage while leaving "
                  f"the panel just as survivor-biased.", file=sys.stderr)
            return 2
    else:
        ap.error("pass --etf or --all")

    try:
        results = [backfill(e, args.dry_run) for e in etfs]
    except NorgateUnavailable as exc:
        print(f"Norgate unavailable: {exc}", file=sys.stderr)
        return 1

    tot_before = tot_filled = 0
    print(f"{'etf':7s}{'universe':>9s}{'unpriced':>10s}{'filled':>8s}{'left':>7s}")
    for r in results:
        if "skipped" in r:
            print(f"{r['etf']:7s}  skipped: {r['skipped']}")
            continue
        left = r["unpriced_before"] - r["filled"]
        tot_before += r["unpriced_before"]; tot_filled += r["filled"]
        print(f"{r['etf']:7s}{r['universe']:9d}{r['unpriced_before']:10d}"
              f"{r['filled']:8d}{left:7d}")
        if r.get("excluded"):
            print(f"         excluded by design (not ordinary listings): "
                  f"{len(r['excluded'])}")
        if r["unresolved"]:
            print(f"         unresolved: {r['unresolved'][:8]}")
        if r.get("stale_roster"):
            print(f"         roster outlived the listing, resolved by "
                  f"uniqueness: {r['stale_roster'][:5]}")
        if r.get("ambiguous"):
            print(f"         AMBIGUOUS (ticker covers two securities, skipped):")
            for a in r["ambiguous"][:5]:
                print(f"           {a}")
        if r["no_norgate_data"]:
            print(f"         resolved but no series: {r['no_norgate_data'][:6]}")
    print(f"\ntotal unpriced {tot_before} -> filled {tot_filled} "
          f"({tot_before - tot_filled} still unpriced)"
          + ("   [DRY RUN — nothing written]" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
