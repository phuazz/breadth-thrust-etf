"""Constituent closes from the local Norgate feed, shaped like the yfinance path.

WHY (2026-08-30, owner decision). The breadth panels are the pipeline's
largest exposure to a single unofficial data source. They fetch several
hundred constituents per panel from yfinance, and constituent-level data is
where that source has gone wrong most often and most expensively: the MNST
split served unapplied (a fabricated -49.6% day, WS15), delisted names coming
back all-NaN and wiping earlier fills, reused tickers returning only the later
occupant's bars, the 2026-08-04 stub writes, and on 2026-08-28/29 a retracted
close field that cost two full refreshes. Norgate is a paid, licensed product
covering exactly this universe.

MEASURED COVERAGE (2026-08-28 rosters, via norgate_symbols.audit):
    CSP1  504 names -> 501 resolve  99.4%
    CNDX  102 names -> 102 resolve 100.0%
    IUIT   73 names ->  73 resolve 100.0%
    ITWN   78 names ->   0 resolve   0.0%
    ICHN  576 names ->   8 resolve   1.4%
The split is clean along US / non-US, because the licensed databases are US
Equities, US Equities Delisted, US Indices, World Indices, Forex Spot, Cash
Commodities, Continuous Futures and Economic. There is no European or Chinese
equity product, so the non-US panels stay on yfinance permanently. This module
is therefore a SOURCE FOR PART OF THE UNIVERSE, never a replacement.

ADJUSTMENT. Explicitly TOTALRETURN — dividends and splits — which is what
yfinance auto_adjust=True returns, not left to the package default even though
the default happens to match today. Measured agreement between the two on
2026-08-30, over 125 sessions each spanning a dividend ex-date:

    TLT ex 2026-08-03   ratio yf/ng  median 1.000005  max 1.000063
    XLF ex 2026-06-22                       1.000002      1.000012
    IJR ex 2026-06-15                       1.000000      1.000001
    XLU ex 2026-06-22                       1.000008      1.000009
    EEM ex 2026-06-15                       0.999997  min 0.999997

Worst disagreement 6.3e-5. The two feeds are interchangeable at panel
tolerance, which is what makes a mixed-source frame safe to build.

LICENCE. Norgate's terms allow derived values to be published but not the raw
series. data/prices_cache_*.parquet is gitignored (.gitignore:12), so closes
sourced here stay on the machine; what reaches the repo is the breadth
percentage computed from them. Any future caller that would COMMIT a series
built from this module is outside that arrangement and must be checked against
the licence first.

POINT-IN-TIME SYMBOLS. Resolution goes through norgate_symbols.resolve, which
returns the symbol quoting a ticker on a given date — including the delisted
databases. A name is tried at the window's end and then at its start, so a
constituent that was live in 2019 and acquired in 2022 still resolves. This is
the property the yfinance path cannot offer at all, and it is why the repo
already reaches for Norgate when backfilling delisted names.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

# Explicit rather than inherited. Norgate's package default is TOTALRETURN
# today; pinning it here means a future default change cannot silently move
# the panel onto a capital-only basis, which would diverge from the yfinance
# history already in the cache by the cumulative dividend yield.
_ADJUSTMENT = "TOTALRETURN"


class NorgateUnavailable(RuntimeError):
    """Re-exported so callers need not import norgate_symbols to catch it."""


def available() -> bool:
    """True when the local Norgate service can be reached.

    Every CI runner returns False — the feed is a locally licensed Windows
    service and no cloud runner has it. Callers must treat False as "use the
    other source", never as an error.
    """
    try:
        import norgate_symbols
        norgate_symbols._nd()
        return True
    except Exception:
        return False


def resolve_all(tickers: list[str], start: str, end: str
                ) -> tuple[dict[str, str], list[str]]:
    """Split ``tickers`` into {ticker: norgate symbol} and the unresolvable.

    Tried at the window's end first, then its start. A name delisted mid-window
    does not quote on the end date, and resolving only there would push every
    acquired constituent onto the fallback source — which is the population the
    yfinance path handles worst.
    """
    import norgate_symbols

    end_d = date.fromisoformat(str(end)[:10])
    start_d = date.fromisoformat(str(start)[:10])
    resolved: dict[str, str] = {}
    missing: list[str] = []
    for t in tickers:
        sym = None
        for as_of in (end_d, start_d):
            try:
                sym = norgate_symbols.resolve(t, as_of)
            except Exception:
                sym = None
            if sym:
                break
        if sym:
            resolved[t] = sym
        else:
            missing.append(t)
    return resolved, missing


def fetch_closes(tickers: list[str], start: str, end: str,
                 verbose: bool = True
                 ) -> tuple[pd.DataFrame, list[str], list[str]]:
    """Adjusted closes for whatever Norgate covers.

    Returns ``(frame, served, unserved)``. The frame's columns are the ORIGINAL
    tickers, not Norgate symbols, so it drops into the yfinance path's shape
    unchanged. ``unserved`` is every ticker the caller must source elsewhere:
    unresolvable symbols and resolvable ones that returned nothing.

    Never raises for a missing name. A partial frame is the expected result on
    any mixed universe, and the caller composes the remainder.
    """
    if not tickers:
        return pd.DataFrame(), [], []
    import norgatedata

    adjust = getattr(norgatedata.StockPriceAdjustmentType, _ADJUSTMENT)
    resolved, unserved = resolve_all(tickers, start, end)
    if verbose:
        print(f"  Norgate resolves {len(resolved)}/{len(tickers)} ticker(s); "
              f"{len(unserved)} to the fallback source", flush=True)

    cols: dict[str, pd.Series] = {}
    for ticker, symbol in resolved.items():
        try:
            df = norgatedata.price_timeseries(
                symbol,
                stock_price_adjustment_setting=adjust,
                start_date=str(start)[:10],
                end_date=str(end)[:10],
                format="pandas-dataframe",
            )
        except Exception:
            unserved.append(ticker)
            continue
        if df is None or len(df) == 0 or "Close" not in df.columns:
            unserved.append(ticker)
            continue
        ser = df["Close"]
        if isinstance(ser, pd.DataFrame):
            ser = ser.iloc[:, 0]
        ser = ser.dropna()
        if ser.empty:
            unserved.append(ticker)
            continue
        idx = pd.to_datetime(ser.index)
        if getattr(idx, "tz", None) is not None:
            # Keep the venue's own calendar date. Converting to UTC first would
            # move a 00:00 local bar across the date line for any venue east of
            # Greenwich, which is the mistake that made a Friday bar read as
            # Thursday on 2026-08-29.
            idx = idx.tz_localize(None)
        ser.index = idx.normalize()
        cols[ticker] = ser

    frame = pd.DataFrame(cols).sort_index() if cols else pd.DataFrame()
    served = sorted(cols)
    return frame, served, sorted(set(unserved))
