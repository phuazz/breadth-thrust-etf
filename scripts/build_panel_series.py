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

Run:
    python scripts/build_panel_series.py
    python scripts/build_panel_series.py --etf CSP1     # single panel
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from etf_registry import ETF_REGISTRY  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
OUT_DIR = PROJECT_ROOT / "docs" / "panel"

WEEKS = 52          # one year of weekly points
_PRICE_DP = 2       # prices rounded to 2dp; more is noise at this resolution


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


def build_panel(etf: str) -> dict | None:
    import pandas as pd
    import compute_breadth as cb

    pq = DATA_DIR / f"prices_cache_{etf.lower()}.parquet"
    if not pq.exists():
        return None
    px = pd.read_parquet(pq)
    if px.empty:
        return None

    # Daily MA first — see the module docstring. per_ticker_apply keeps each
    # ticker on its own traded sessions, so a European name is not voided by
    # US-only sessions in the union grid.
    ma = cb.per_ticker_apply(
        px, lambda s: s.rolling(cb.MA_PERIOD, min_periods=cb.MA_PERIOD).mean())

    # Weekly grid: last observation on or before each Friday. `.last()` skips
    # NaN within the week, so a market closed on the Friday still reports its
    # Thursday close rather than a hole.
    pw = px.resample("W-FRI").last().tail(WEEKS)
    mw = ma.resample("W-FRI").last().tail(WEEKS)

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
        series[sym] = {"p": p, "m": [_round(v) for v in mw[sym]]}

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
        raw = yf.download(list(proxies), period="1y", interval="1wk",
                          auto_adjust=True, progress=False, threads=True)
        close = raw["Close"] if "Close" in raw else raw
        if isinstance(close, pd.Series):
            close = close.to_frame(list(proxies)[0])
        out = {}
        for tick, etfs in proxies.items():
            if tick not in close.columns:
                continue
            s = close[tick].dropna()
            if s.empty:
                continue
            payload = {
                "ticker": tick,
                "dates": [d.strftime("%Y-%m-%d") for d in s.index],
                "p": [round(float(v), _PRICE_DP) for v in s],
            }
            for e in etfs:
                out[e] = payload
        return out
    except Exception as exc:
        print(f"  WARN: ETF-level series unavailable ({exc}) — "
              f"constituent charts are unaffected", flush=True)
        return {}


def write_all(only: str | None = None) -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    symbols = [only.upper()] if only else sorted(ETF_REGISTRY)
    proxy = _proxy_series(symbols)
    total = 0
    written = 0
    for etf in symbols:
        payload = build_panel(etf)
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
    print(f"\nWrote {written} panel files to "
          f"{OUT_DIR.relative_to(PROJECT_ROOT)} — {total/1024/1024:.2f} MB total")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--etf", default=None, help="Build a single panel.")
    args = p.parse_args()
    return write_all(args.etf)


if __name__ == "__main__":
    sys.exit(main())
