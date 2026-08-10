"""Resolve a point-in-time roster ticker to a Norgate symbol.

WHY THIS EXISTS. Constituent prices come from yfinance, which serves history
only under a security's CURRENT symbol. Every name that was later acquired,
taken private or renamed therefore has no history under the ticker it carried
while it was in the index, and is dropped from breadth entirely. CNDX coverage
is 81.6% in 2018 and only clears 99% in 2025 for that reason alone, so the
early backtest is computed on survivors. Norgate Platinum carries ~21k
delisted securities back to 1990 and can fill them in.

THE TRAP THIS MODULE EXISTS TO AVOID. Symbols are REUSED, so resolving by
ticker alone silently attaches the wrong company's prices:

    FB            today = ProShares S&P 500 Dynamic Buffer ETF (from 2025-06-26)
                  in 2018 = Facebook, whose history now lives under META
    CA-201811     = CA Inc, acquired by Broadcom 2018-11
    CA-202605     = Xtrackers California Municipal Bond ETF, a different thing
    SPLK-200004   = Spanlink Communications
    SPLK-202403   = Splunk Inc

That is not hypothetical: the committed yfinance cache ALREADY holds 281 bars
under "FB" starting 2025-06-26, which are the ProShares ETF's, not Facebook's.
They are harmless today only because FB left the index years before that date.

So resolution is by (ticker, as-of date) against each candidate's quotation
window, never by ticker alone.

RENAMES are a separate case. Norgate keeps a renamed security's full history
under its CURRENT symbol, so there is no dated candidate to match and an
explicit map is required. Each entry below was verified against Norgate's own
``security_name`` rather than assumed from the ticker.

Requires the norgatedata package and a local Norgate installation, so this is
LOCAL-ONLY: CI has neither, which is already true of the constituent refresh.
"""

from __future__ import annotations

from datetime import date
from functools import lru_cache

# Roster ticker -> the CURRENT Norgate symbol carrying that security's history.
# Renames only: the security survived, so Norgate has no dated delisted entry.
# Verified 2026-08-10 against norgatedata.security_name.
RENAMED: dict[str, str] = {
    "CTRP":  "TCOM",    # Trip.com Group ADR (renamed 2019)
    "MYL":   "VTRS",    # Mylan -> Viatris (2020)
    "SYMC":  "GEN",     # Symantec -> NortonLifeLock -> Gen Digital
    "NLOK":  "GEN",     # NortonLifeLock -> Gen Digital (2022)
    "WLTW":  "WTW",     # Willis Towers Watson (2022)
    # Liberty Interactive QVC Group A -> Qurate Retail -> QVC Group. Norgate
    # carries the whole lineage under one delisted symbol from 2006-05-04.
    "QVCA":  "QVCAQ-202608",
    "QRTEA": "QVCAQ-202608",
    # Renamed AND their old ticker later reused by an unrelated fund, which is
    # why resolution checks dated candidates before this table: FB must mean
    # Facebook in 2018 and the ProShares ETF that took the ticker in 2025.
    "FB":    "META",    # Facebook -> Meta Platforms (2022), history from 2012
    "PCLN":  "BKNG",    # Priceline -> Booking Holdings (2018)
}

# Roster entries that are NOT ordinary equity and cannot be priced as one.
# Both appeared in exactly ONE weekly snapshot, so neither is a real sustained
# constituent; excluding them is correct rather than a gap to be filled.
NOT_EQUITY: dict[str, str] = {
    # T-Mobile rights line, 2020-06-26 only (the SoftBank secondary).
    "TMUSR": "rights line, not an ordinary listing",
    # Bloomberg composite ("UW" = Nasdaq) that leaked through the iShares
    # ticker field, 2026-01-09 only. Now rejected upstream by
    # fetch_constituents._us_symbol; kept here so historical rosters resolve.
    "VSNTV UW": "Bloomberg composite identifier, not a ticker",
}


class NorgateUnavailable(RuntimeError):
    """norgatedata is absent or the local Norgate service is not running."""


def _nd():
    try:
        import norgatedata as nd
    except ImportError as e:  # pragma: no cover - environment-dependent
        raise NorgateUnavailable("norgatedata is not installed") from e
    if not nd.status():
        raise NorgateUnavailable("the local Norgate service is not running")
    return nd


@lru_cache(maxsize=1)
def _candidates() -> dict[str, list[str]]:
    """Ticker root -> every Norgate symbol sharing it, live and delisted."""
    nd = _nd()
    out: dict[str, list[str]] = {}
    for s in nd.database_symbols("US Equities"):
        out.setdefault(s, []).append(s)
    for s in nd.database_symbols("US Equities Delisted"):
        out.setdefault(s.split("-")[0], []).append(s)
    return out


def _as_date(v) -> date | None:
    """Norgate returns quotation dates as ISO strings, datetimes or None
    depending on the call; normalise before any comparison. Comparing a str
    to a date raises rather than silently mis-ordering, which is how this was
    caught, but normalising here keeps the resolver readable."""
    if v is None:
        return None
    if isinstance(v, date):
        return v if not hasattr(v, "date") else v.date()
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


@lru_cache(maxsize=8192)
def _window(symbol: str) -> tuple[date | None, date | None]:
    nd = _nd()
    try:
        return (_as_date(nd.first_quoted_date(symbol)),
                _as_date(nd.last_quoted_date(symbol)))
    except Exception:
        return None, None


def resolve(ticker: str, as_of: date) -> str | None:
    """The Norgate symbol quoting ``ticker`` on ``as_of``, or None.

    None means "deliberately unresolvable" as well as "not found": a rights
    line and a Bloomberg composite both return None, because inventing a
    security for them would be worse than leaving the name unpriced.
    """
    if ticker in NOT_EQUITY:
        return None

    # DATED CANDIDATES FIRST. A symbol that was actually quoting on as_of is
    # authoritative, and checking renames first would break the reused ones:
    # FB must resolve to Facebook in 2018 and to the ProShares ETF that took
    # the ticker in 2025, which one blanket rename entry cannot express.
    best: tuple[int, str] | None = None
    for sym in _candidates().get(ticker, []):
        first, last = _window(sym)
        if not first or as_of < first:
            continue
        if last is not None and as_of > last:
            continue
        # Prefer the candidate whose window ENDS soonest after as_of: a
        # delisted line that was quoting then beats a still-live reuse of the
        # same ticker that only started later.
        rank = (last - as_of).days if last is not None else 10 ** 6
        if best is None or rank < best[0]:
            best = (rank, sym)
    if best:
        return best[1]

    # No symbol was quoting under this ticker then, which is what a RENAME
    # looks like: the security lives on under a different symbol carrying the
    # whole history, so there is nothing dated to match.
    sym = RENAMED.get(ticker)
    if sym:
        first, last = _window(sym)
        if first and first <= as_of and (last is None or as_of <= last):
            return sym
    return None


def audit(tickers: list[str], as_of: date) -> dict[str, str | None]:
    """Resolve many at once — the shape the backfill and its tests both want."""
    return {t: resolve(t, as_of) for t in tickers}
