"""Emit docs/panel/<ETF>.json — weekly price series for the Data tab charts.

One file per panel, fetched only when a reader opens that panel, so the
dashboard never pays for data nobody looks at.

WHY WEEKLY, NOT DAILY. Daily closes for every constituent of all 38 panels
come to ~9.2MB. GitHub Pages only serves committed files, so that would be
re-committed on every weekly refresh — roughly 480MB of git history a year
for chart data. Weekly sampling costs ~1.9MB and loses nothing a reader
would act on: these charts exist to show a name's trend against its moving
average, not to support intraday reading.

THE MOVING AVERAGE IS COMPUTED DAILY, THEN SAMPLED. Computing a 50-period
average on weekly bars would be a 50-WEEK average — a different indicator
that would not match the breadth panel. The daily MA is computed first,
exactly as compute_breadth does it (per ticker, on that ticker's own traded
sessions), and only then sampled to the weekly grid.

THE STALE-CACHE GUARD. ``data/prices_cache_*.parquet`` is gitignored, so
every machine holds its own copy and a local rebuild is only ever as fresh
as whatever that machine last fetched. The weekly resample below takes the
last observation WITHIN each week, which is deliberate — a market shut on
the Friday should report its Thursday close rather than a hole — but it
means a cache that simply stopped advancing is indistinguishable from a
holiday: the point still gets stamped with its Friday label and carries a
days-old price. On 2026-08-08 a local rebuild off a cache ending 08-04 was
about to commit ADI at 380.29 under a 2026-08-07 label against a true
389.93, across 25 panels, with nothing in the output saying so.

Nothing downstream can catch this. The date label is well-formed, the
series is complete, and ``_proxy_series`` below fetches the ETF line live —
so the chart would show a current ETF line against stale constituent lines
and look entirely healthy. The only place the truth exists is the cache's
own last bar, so that is what gets checked, before anything is written.

A stale panel is SKIPPED, never written: the committed file is newer than
what this run would produce, so leaving it alone is the correct outcome.
Set ``ALLOW_STALE_PANEL_CACHE=1`` for a one-off local rebuild — never in CI.

Run:
    python scripts/build_panel_series.py
    python scripts/build_panel_series.py --etf CSP1     # single panel
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from etf_registry import ETF_REGISTRY  # noqa: E402
from nyse_sessions import last_completed_session, sessions_behind  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
OUT_DIR = PROJECT_ROOT / "docs" / "panel"

WEEKS = 52          # one year of weekly points
_PRICE_DP = 2       # prices rounded to 2dp; more is noise at this resolution

# Moving averages carried alongside each price series, matching the Monitor
# tab's mini-charts so the two read the same way. The 50-day is the one the
# breadth panel counts; the 200-day is what Strategy A's selection ranks on.
# Each is computed on DAILY closes and then sampled to the weekly grid — a
# 50-period average over weekly bars would be a 50-WEEK average.
MA_WINDOWS = {"m50": 50, "m100": 100, "m200": 200}

# Budget, in NYSE sessions, between a cache's last bar and the last
# completed session. Not zero, because these panels span Xetra, LSE and
# Asian calendars against an NYSE yardstick: a Europe-only closure leaves a
# legitimately current cache one session short, and a two-day one (26 Dec,
# when NYSE trades and much of Europe does not) leaves it two. Three is
# already past any real cross-calendar gap — and three is exactly what the
# 2026-08-08 near-miss measured, so the budget has to sit below it.
MAX_CACHE_LAG_SESSIONS = 2

# Never set in CI. Lets a local rebuild proceed on caches it knows are
# behind, matching the ALLOW_STALE_REGIME escape hatch in pipeline.py.
OVERRIDE_ENV = "ALLOW_STALE_PANEL_CACHE"


class StalePriceCacheError(RuntimeError):
    """A panel's price cache is too far behind to be written safely.

    Carries ``etfs`` so a caller can report every stale panel from one run
    rather than only whichever failed first.
    """

    def __init__(self, message: str, etfs: list[str] | None = None):
        super().__init__(message)
        self.etfs = etfs or []


def check_cache_freshness(etf: str, last_bar: date,
                          expected: date | None) -> None:
    """Raise if ``etf``'s cache is too far behind ``expected`` to publish.

    Takes ``last_bar`` rather than a path so a caller that has already read
    the frame does not pay for a second parquet read. ``expected`` of None
    disables the check.

    Shared with build_data_audit, which reads the SAME gitignored caches and
    reports each name's last observed close as its current price — the same
    silent staleness in a different table. One budget, one exception type,
    so the two cannot drift apart.
    """
    if expected is None:
        return
    lag = sessions_behind(last_bar, expected)
    if lag > MAX_CACHE_LAG_SESSIONS:
        raise StalePriceCacheError(
            f"{etf}: cache ends {last_bar}, {lag} NYSE sessions behind the "
            f"last completed session ({expected}).",
            [etf],
        )


def _round(v):
    import pandas as pd
    return None if v is None or pd.isna(v) else round(float(v), _PRICE_DP)


def _current_roster(etf: str) -> set[str] | None:
    """Tickers in the panel's most recent snapshot, or None if unknown.

    None means "no filter" rather than "no names": if the roster cannot be
    read, emitting every series is the safe failure — a slightly larger file
    beats a chart that silently has no data behind it.
    """
    p = DATA_DIR / f"constituents_{etf.lower()}.json"
    if not p.exists():
        return None
    try:
        snaps = json.loads(p.read_text(encoding="utf-8")).get("snapshots", {})
        if not snaps:
            return None
        return set(snaps[max(snaps)].get("tickers") or [])
    except Exception:
        return None


def build_panel(etf: str, expected: date | None = None) -> dict | None:
    """Build one panel payload, or None when there is no usable cache.

    Args:
        etf: registry key, e.g. ``CSP1``.
        expected: last completed NYSE session to measure the cache against.
            None disables the check — used by callers that have already
            established freshness, and by the override path.

    Raises:
        StalePriceCacheError: the cache's last bar is more than
            MAX_CACHE_LAG_SESSIONS behind ``expected``. Raised BEFORE any
            resampling, so a stale bar never reaches a weekly label.
    """
    import pandas as pd
    import compute_breadth as cb

    pq = DATA_DIR / f"prices_cache_{etf.lower()}.parquet"
    if not pq.exists():
        return None
    px = pd.read_parquet(pq)
    if px.empty:
        return None

    # Before any resampling, so a stale bar never reaches a weekly label.
    check_cache_freshness(etf, px.index.max().date(), expected)

    # Daily MAs first — see the module docstring. per_ticker_apply keeps each
    # ticker on its own traded sessions, so a European name is not voided by
    # US-only sessions in the union grid.
    mas = {
        key: cb.per_ticker_apply(
            px, lambda s, w=win: s.rolling(w, min_periods=w).mean())
        for key, win in MA_WINDOWS.items()
    }

    # Weekly grid: last observation on or before each Friday. `.last()` skips
    # NaN within the week, so a market closed on the Friday still reports its
    # Thursday close rather than a hole.
    pw = px.resample("W-FRI").last().tail(WEEKS)
    mws = {k: m.resample("W-FRI").last().tail(WEEKS) for k, m in mas.items()}

    # Only CURRENT constituents get a series. The price cache carries every
    # name that has ever been in the index — CSP1's holds ~700 against a
    # 504-name roster — and a chart is only ever opened from a row in the
    # current holdings table. Emitting the rest inflated the payload by
    # roughly a third for series nothing can reach.
    wanted = _current_roster(etf)

    dates = [d.strftime("%Y-%m-%d") for d in pw.index]
    series: dict[str, dict] = {}
    for sym in pw.columns:
        if wanted is not None and sym not in wanted:
            continue
        p = [_round(v) for v in pw[sym]]
        if not any(v is not None for v in p):
            continue          # never traded in the window — no chart to draw
        entry = {"p": p}
        for key, mw in mws.items():
            col = [_round(v) for v in mw[sym]]
            # Omit an average with no points rather than shipping 52 nulls.
            # A name younger than 200 sessions has no 200-day average, and
            # that is a fact about the name, not a gap to pad.
            if any(v is not None for v in col):
                entry[key] = col
        series[sym] = entry

    return {
        "etf": etf,
        "built_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "resolution": "weekly (W-FRI); 50-day MA computed daily then sampled",
        "ma_period_days": cb.MA_PERIOD,
        "dates": dates,
        "series": series,
    }


def _proxy_series(symbols: list[str]) -> dict:
    """Weekly series for each panel's own traded ticker, for the ETF-level
    chart. Soft-fails to {}: a vendor outage should degrade the charts, not
    break the dashboard build (a DNS failure took out a compute_breadth step
    on 2026-08-08, which is the failure mode this guards against)."""
    proxies = {}
    for e in symbols:
        p = (ETF_REGISTRY.get(e) or {}).get("yfinance_trading_proxy") or e
        proxies.setdefault(p, []).append(e)
    if not proxies:
        return {}
    try:
        import pandas as pd
        import yfinance as yf
        import compute_breadth as cb

        # DAILY, not weekly, and over two years rather than one.
        #
        # The ETF line needs the same 50-day average the constituent lines
        # carry, and that cannot be derived from weekly bars: a 50-period
        # average over weekly data is a 50-WEEK average, a different
        # indicator. So the download is daily, the average is taken daily,
        # and only then is the result sampled to the weekly grid.
        #
        # Three years, not two: the 200-day average needs 200 sessions of
        # history BEFORE the first plotted week, and a 2-year window leaves
        # only ~300 sessions after that warmup — enough, but with no margin
        # for a proxy whose history starts late or has gaps. An absent
        # average reads as a data gap rather than as a warmup, so buy room.
        raw = yf.download(list(proxies), period="3y", interval="1d",
                          auto_adjust=True, progress=False, threads=True)
        close = raw["Close"] if "Close" in raw else raw
        if isinstance(close, pd.Series):
            close = close.to_frame(list(proxies)[0])

        # per_ticker_apply, NOT a plain rolling() — for the same reason the
        # constituent path uses it. This frame spans 38 proxies across US,
        # Xetra and Asian calendars, so its union index is NaN wherever a
        # given market was shut. With min_periods=N a single NaN entering
        # the window voids the next N rows, which showed up as an average
        # that existed only for the first 7 weeks and then vanished — a
        # rolling mean cannot legitimately disappear at the end of a series.
        pmas = {
            key: cb.per_ticker_apply(
                close, lambda s, w=win: s.rolling(w, min_periods=w).mean())
            for key, win in MA_WINDOWS.items()
        }
        pw = close.resample("W-FRI").last().tail(WEEKS)
        pmws = {k: m.resample("W-FRI").last().tail(WEEKS)
                for k, m in pmas.items()}

        out = {}
        for tick, etfs in proxies.items():
            if tick not in pw.columns:
                continue
            p = [_round(v) for v in pw[tick]]
            if not any(v is not None for v in p):
                continue
            payload = {
                "ticker": tick,
                "dates": [d.strftime("%Y-%m-%d") for d in pw.index],
                "p": p,
            }
            for key, mw in pmws.items():
                col = [_round(v) for v in mw[tick]]
                if any(v is not None for v in col):
                    payload[key] = col
            for e in etfs:
                out[e] = payload
        return out
    except Exception as exc:
        print(f"  WARN: ETF-level series unavailable ({exc}) — "
              f"constituent charts are unaffected", flush=True)
        return {}


def write_all(only: str | None = None, expected: date | None = None) -> int:
    """Write every panel whose cache is current enough to trust.

    Args:
        only: build a single panel instead of the whole registry.
        expected: last completed NYSE session. Resolved from the clock when
            omitted; forced to None (check disabled) by OVERRIDE_ENV.

    Raises:
        StalePriceCacheError: after writing the panels that were fresh, if
            any were skipped. Raised at the END so one stale cache cannot
            mask the state of the other 37.
    """
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    symbols = [only.upper()] if only else sorted(ETF_REGISTRY)
    overridden = bool(os.environ.get(OVERRIDE_ENV))
    if expected is None and not overridden:
        expected = last_completed_session(datetime.now(timezone.utc))
    if overridden:
        expected = None
        print(f"  {OVERRIDE_ENV} set — stale-cache guard DISABLED, panel "
              f"prices may be older than their date labels", flush=True)
    proxy = _proxy_series(symbols)
    total = 0
    written = 0
    stale: list[str] = []
    for etf in symbols:
        try:
            payload = build_panel(etf, expected=expected)
        except StalePriceCacheError as exc:
            # Leave the committed file alone: it is newer than anything this
            # run could produce, so overwriting is strictly a regression.
            stale.append(etf)
            print(f"  {etf:6s} SKIPPED (stale cache) — {exc}", flush=True)
            continue
        if payload is None:
            print(f"  {etf:6s} skipped — no price cache", flush=True)
            continue
        if etf in proxy:
            payload["etf_series"] = proxy[etf]
        text = json.dumps(payload, separators=(",", ":"))
        (OUT_DIR / f"{etf}.json").write_text(text, encoding="utf-8")
        total += len(text)
        written += 1
        print(f"  {etf:6s} {len(payload['series']):4d} names, "
              f"{len(payload['dates']):3d} weeks, {len(text)/1024:7.0f} KB",
              flush=True)
    try:
        where = OUT_DIR.relative_to(PROJECT_ROOT)
    except ValueError:
        where = OUT_DIR          # OUT_DIR redirected outside the repo (tests)
    print(f"\nWrote {written} panel files to "
          f"{where} — {total/1024/1024:.2f} MB total")
    if stale:
        raise StalePriceCacheError(
            f"{len(stale)} panel(s) NOT written — price cache behind the "
            f"last completed session by more than {MAX_CACHE_LAG_SESSIONS} "
            f"session(s): {', '.join(stale)}. The committed files were left "
            f"untouched, so nothing stale was published. Fix: refresh the "
            f"caches (`python scripts/refresh_all.py`) and rebuild; or set "
            f"{OVERRIDE_ENV}=1 for a one-off local rebuild that accepts "
            f"prices older than their date labels.",
            stale,
        )
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--etf", default=None, help="Build a single panel.")
    args = p.parse_args()
    return write_all(args.etf)


if __name__ == "__main__":
    sys.exit(main())
