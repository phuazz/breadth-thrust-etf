"""Daily capture + metrics for the theme-constituent monitor.

Fetches each registered fund's current roster, writes an immutable daily
snapshot, prices the union of names, and emits the page payload.

    python scripts/run_holdings_monitor.py
    python scripts/run_holdings_monitor.py --etf ARKG
    python scripts/run_holdings_monitor.py --no-fetch    # reuse price cache

THIS CANNOT TOUCH THE BOOK. Deliberately self-contained: its own price
cache, its own output files, no import of any strategy engine and no write
to any path the engines read. The monitor is an idea-generation surface
feeding human discretion, and the vault rule is that discretionary inputs
stay named and separated from the rules-based sleeves. If a signal from
here is ever wanted in a strategy, it goes through a registered study
first, not through a shared file.

SNAPSHOTS ARE IMMUTABLE. ``data/holdings_monitor/<ETF>/<as_of>.json`` is
written once and never rewritten. Re-running on the same day is a no-op
against an identical file and a hard error against a differing one. The
flow view is a difference between two snapshots, so a snapshot that can be
silently rewritten turns yesterday's history into today's opinion.

WHY FLOW IS AN ACTIVE-WEIGHT CHANGE AND NOT A WEIGHT DIFFERENCE. A naive
``weight_now - weight_prev`` is dominated by price, not by trading: a name
that fell 20% shows a weight drop that reads exactly like selling, and a
name that rallied reads as buying. The decomposition below prices
YESTERDAY'S share counts at TODAY'S closes, renormalises, and subtracts
that counterfactual from the actual weight. What is left is the part that
only a trade could have produced. Creations and redemptions cancel because
both sides are normalised to 100%.

Flow is computed for every fund but PUBLISHED only for actively managed
ones (``active: True`` in the registry). In an index fund the same
arithmetic returns real numbers that mean nothing a reader should act on —
they are the index committee's rebalancing, not a manager's conviction.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from holdings_sources import (  # noqa: E402
    MONITOR_FUNDS, HoldingsSourceError, RosterSnapshot, fetch_roster,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
SNAP_DIR = DATA_DIR / "holdings_monitor"
PRICE_CACHE = DATA_DIR / "holdings_monitor_prices.parquet"
LATEST_PATH = DATA_DIR / "holdings_monitor_latest.json"
SERIES_PATH = PROJECT_ROOT / "docs" / "holdings-monitor-series.json"

# Two years of daily closes: 200-day averages need ~10 months of runway
# before the first valid point, and the charts want a clean year on top.
PRICE_PERIOD = "2y"

MA_WINDOWS = {"m50": 50, "m100": 100, "m200": 200}
CHART_WEEKS = 52
_PRICE_DP = 2

# Trailing windows in TRADING sessions, not calendar days, so a holiday
# does not quietly shift the lookback.
RETURN_WINDOWS = {"r1d": 1, "r5d": 5, "r1m": 21, "r3m": 63, "r6m": 126, "r1y": 252}

# Below this many sessions a 200-day average does not exist. The house
# convention (see the Data tab copy) is to show an em-dash rather than
# count the name as below its average, because a missing average is not a
# bearish reading.
MIN_SESSIONS_FOR_MA200 = 200


class MonitorError(RuntimeError):
    """Raised when the capture cannot complete safely."""


# ---------------------------------------------------------------------------
# Snapshot store
# ---------------------------------------------------------------------------


def snapshot_path(etf: str, as_of: date) -> Path:
    return SNAP_DIR / etf.upper() / f"{as_of.isoformat()}.json"


def write_snapshot(snap: RosterSnapshot) -> tuple[Path, str]:
    """Write the daily snapshot. Returns (path, action).

    action is "written" | "unchanged". A same-day re-run producing DIFFERENT
    content raises: the issuer restated a published file, which is a real
    event an operator must see rather than a diff to absorb silently.
    """
    p = snapshot_path(snap.etf, snap.as_of)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = snap.to_dict()
    # fetched_at_utc changes on every run by construction and is metadata
    # about the fetch, not about the roster, so it is excluded from the
    # equality test. Everything describing the holdings is included.
    comparable = {k: v for k, v in payload.items() if k != "fetched_at_utc"}
    if p.exists():
        try:
            prior = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise MonitorError(f"existing snapshot {p} is unreadable: {exc}") from exc
        prior_cmp = {k: v for k, v in prior.items() if k != "fetched_at_utc"}
        if prior_cmp == comparable:
            return p, "unchanged"
        raise MonitorError(
            f"{snap.etf} snapshot for {snap.as_of} already exists and DIFFERS "
            f"from what the issuer serves now. A published roster has been "
            f"restated. Inspect {p} and resolve deliberately; this script "
            f"will not overwrite history."
        )
    p.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
    return p, "written"


def previous_snapshot(etf: str, before: date) -> dict | None:
    """Most recent stored snapshot strictly before ``before``."""
    d = SNAP_DIR / etf.upper()
    if not d.exists():
        return None
    cands = []
    for f in d.glob("*.json"):
        try:
            when = date.fromisoformat(f.stem)
        except ValueError:
            continue
        if when < before:
            cands.append((when, f))
    if not cands:
        return None
    _, newest = max(cands, key=lambda t: t[0])
    return json.loads(newest.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Prices
# ---------------------------------------------------------------------------


def fetch_prices(tickers: list[str], use_cache_only: bool) -> pd.DataFrame:
    """Daily closes for the union of names, cached to parquet."""
    cached = None
    if PRICE_CACHE.exists():
        try:
            cached = pd.read_parquet(PRICE_CACHE)
        except Exception as exc:  # pragma: no cover - corrupt cache
            print(f"  WARN: price cache unreadable ({exc}); refetching")
    if use_cache_only:
        if cached is None:
            raise MonitorError("--no-fetch given but no price cache exists")
        return cached

    try:
        import yfinance as yf
    except ImportError as exc:  # pragma: no cover
        raise MonitorError(f"yfinance unavailable: {exc}") from exc

    print(f"  Fetching {len(tickers)} tickers ({PRICE_PERIOD})...")
    raw = yf.download(tickers, period=PRICE_PERIOD, auto_adjust=True,
                      progress=False, threads=True)
    if raw is None or raw.empty:
        if cached is not None:
            print("  WARN: fetch returned nothing; falling back to cache")
            return cached
        raise MonitorError("price fetch returned nothing and no cache exists")
    close = raw["Close"] if "Close" in raw.columns.get_level_values(0) else raw
    if isinstance(close, pd.Series):
        close = close.to_frame(tickers[0])
    close = close.dropna(how="all")
    PRICE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    close.to_parquet(PRICE_CACHE)
    return close


def _last_valid(s: pd.Series):
    s = s.dropna()
    return (s.index[-1].date(), float(s.iloc[-1])) if len(s) else (None, None)


def name_metrics(series: pd.Series) -> dict:
    """Trend, momentum and range statistics for one constituent."""
    s = series.dropna()
    n = len(s)
    out = {"n_sessions": n}
    if n == 0:
        return {**out, "px": None, "px_date": None, "state": None}
    last = float(s.iloc[-1])
    out["px"] = round(last, _PRICE_DP)
    out["px_date"] = s.index[-1].date().isoformat()

    for key, win in MA_WINDOWS.items():
        if n >= win:
            ma = float(s.rolling(win).mean().iloc[-1])
            out[f"vs_{key}"] = round((last / ma) - 1, 5) if ma else None
        else:
            out[f"vs_{key}"] = None

    for key, win in RETURN_WINDOWS.items():
        out[key] = round((last / float(s.iloc[-win - 1])) - 1, 5) if n > win else None

    yr = s.iloc[-min(n, 252):]
    lo, hi = float(yr.min()), float(yr.max())
    out["lo52"] = round(lo, _PRICE_DP)
    out["hi52"] = round(hi, _PRICE_DP)
    out["range52"] = round((last - lo) / (hi - lo), 4) if hi > lo else None
    out["off_high"] = round((last / hi) - 1, 5) if hi else None

    # State follows the 200-day, the average Strategy A ranks on. Fewer
    # than 200 sessions means no average exists, which is not "below".
    if n >= MIN_SESSIONS_FOR_MA200 and out.get("vs_m200") is not None:
        out["state"] = "above" if out["vs_m200"] >= 0 else "below"
    else:
        out["state"] = None
    return out


def weekly_series(series: pd.Series) -> dict | None:
    """52 weekly points plus moving averages, matching build_panel_series.

    The averages are computed on DAILY closes and only then sampled to the
    weekly grid. A 50-period average over weekly bars would be a 50-WEEK
    average — a different indicator that would not agree with the table.
    """
    s = series.dropna()
    if len(s) < 10:
        return None
    frame = pd.DataFrame({"px": s})
    for key, win in MA_WINDOWS.items():
        frame[key] = s.rolling(win).mean() if len(s) >= win else pd.NA
    wk = frame.resample("W-FRI").last().tail(CHART_WEEKS)

    def col(c):
        return [None if pd.isna(v) else round(float(v), _PRICE_DP) for v in wk[c]]

    return {
        "dates": [d.date().isoformat() for d in wk.index],
        "px": col("px"),
        **{k: col(k) for k in MA_WINDOWS},
    }


# ---------------------------------------------------------------------------
# Flow — the active-weight decomposition
# ---------------------------------------------------------------------------


def compute_flow(snap: RosterSnapshot, prev: dict | None,
                 px_now: dict[str, float]) -> dict[str, dict]:
    """Active weight change per name between ``prev`` and ``snap``.

    Counterfactual: hold yesterday's share counts, mark them at today's
    closes, renormalise to 100%. Subtracting that from today's actual
    weight isolates the trade, because price drift is present in both
    terms and cancels.

    A name missing a price cannot be marked, so it is excluded from the
    counterfactual basis entirely and reported as unavailable rather than
    given a flow of zero. Zero would read as "held", which is a claim.
    """
    if not prev:
        return {}
    prev_shares = {h["ticker"]: h.get("shares") for h in prev.get("holdings", [])
                   if h.get("shares") is not None}
    if not prev_shares:
        return {}

    cf_value, unpriced = {}, set()
    for tk, sh in prev_shares.items():
        p = px_now.get(tk)
        if p is None:
            unpriced.add(tk)
            continue
        cf_value[tk] = sh * p
    total_cf = sum(cf_value.values())
    if total_cf <= 0:
        return {}
    cf_weight = {tk: 100.0 * v / total_cf for tk, v in cf_value.items()}

    now_weight = {h.ticker: h.weight_pct for h in snap.holdings}
    now_shares = {h.ticker: h.shares for h in snap.holdings}

    flow: dict[str, dict] = {}
    for tk in set(now_weight) | set(prev_shares):
        if tk in unpriced:
            flow[tk] = {"status": "unpriced", "active_bp": None, "d_shares_pct": None}
            continue
        w_now = now_weight.get(tk, 0.0)
        w_cf = cf_weight.get(tk, 0.0)
        active_bp = round(100.0 * (w_now - w_cf), 1)   # percentage points -> bp

        sh_now, sh_prev = now_shares.get(tk), prev_shares.get(tk)
        d_sh = (round((sh_now / sh_prev) - 1, 5)
                if sh_now is not None and sh_prev not in (None, 0) else None)

        if tk not in prev_shares:
            status = "new"
        elif tk not in now_weight:
            status = "exited"
        elif d_sh is None:
            status = "held"
        elif d_sh > 0.005:
            status = "added"
        elif d_sh < -0.005:
            status = "trimmed"
        else:
            status = "held"
        flow[tk] = {"status": status, "active_bp": active_bp, "d_shares_pct": d_sh}
    return flow


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def build(etfs: list[str], use_cache_only: bool) -> dict:
    sys.stdout.reconfigure(encoding="utf-8")
    today = datetime.now(timezone.utc).date()
    snaps: dict[str, RosterSnapshot] = {}
    for etf in etfs:
        print(f"[{etf}] fetching roster...")
        snap = fetch_roster(etf)
        path, action = write_snapshot(snap)
        print(f"  as_of={snap.as_of} n={len(snap.holdings)} "
              f"weight={snap.weight_sum:.2f}% dropped={len(snap.dropped)} "
              f"snapshot={action}")
        snaps[etf] = snap

    universe = sorted({h.ticker for s in snaps.values() for h in s.holdings})
    print(f"[prices] union of {len(universe)} names")
    px = fetch_prices(universe, use_cache_only)

    metrics, series, px_now = {}, {}, {}
    for tk in universe:
        if tk not in px.columns:
            metrics[tk] = {"n_sessions": 0, "px": None, "px_date": None,
                           "state": None}
            continue
        m = name_metrics(px[tk])
        metrics[tk] = m
        if m["px"] is not None:
            px_now[tk] = m["px"]
        ws = weekly_series(px[tk])
        if ws:
            series[tk] = ws

    priced = sum(1 for tk in universe if metrics[tk].get("px") is not None)
    with_ma200 = sum(1 for tk in universe
                     if metrics[tk].get("vs_m200") is not None)
    print(f"[prices] priced {priced}/{len(universe)} "
          f"({priced/len(universe):.1%}), with 200d MA {with_ma200} "
          f"({with_ma200/len(universe):.1%})")

    funds = {}
    for etf, snap in snaps.items():
        cfg = MONITOR_FUNDS[etf]
        prev = previous_snapshot(etf, snap.as_of)
        flow = compute_flow(snap, prev, px_now)
        rows = []
        for h in snap.holdings:
            m = metrics.get(h.ticker, {})
            f = flow.get(h.ticker, {})
            rows.append({
                "t": h.ticker, "n": h.name, "w": round(h.weight_pct, 4),
                "sh": h.shares, "sec": h.sector,
                "px": m.get("px"), "pxd": m.get("px_date"),
                "m50": m.get("vs_m50"), "m100": m.get("vs_m100"),
                "m200": m.get("vs_m200"), "st": m.get("state"),
                "ns": m.get("n_sessions"),
                **{k: m.get(k) for k in RETURN_WINDOWS},
                "rng": m.get("range52"), "oh": m.get("off_high"),
                "lo": m.get("lo52"), "hi": m.get("hi52"),
                "fs": f.get("status"), "fbp": f.get("active_bp"),
                "fsh": f.get("d_shares_pct"),
            })
        # Exited names carry no current weight but are the loudest signal in
        # an active fund, so they ride along flagged rather than vanishing.
        exits = [{"t": tk, "n": "", "w": 0.0, "fs": "exited",
                  "fbp": f["active_bp"], "fsh": None,
                  **{k: metrics.get(tk, {}).get(k2)
                     for k, k2 in (("px", "px"), ("m200", "vs_m200"),
                                   ("st", "state"), ("r1m", "r1m"))}}
                 for tk, f in flow.items()
                 if f.get("status") == "exited"]
        n_priced = sum(1 for r in rows if r["px"] is not None)
        funds[etf] = {
            "etf": etf, "label": cfg["label"], "issuer": cfg["issuer"],
            "active": cfg["active"],
            "as_of": snap.as_of.isoformat(),
            "roster_age_days": (today - snap.as_of).days,
            "source": snap.source, "url": snap.url,
            "n_holdings": len(snap.holdings),
            "weight_sum_pct": snap.weight_sum,
            "dropped": snap.dropped,
            "price_coverage": round(n_priced / max(1, len(rows)), 4),
            "flow_basis": (prev.get("as_of") if prev else None),
            "rows": rows,
            "exits": exits,
        }
        fl = f"flow vs {prev['as_of']}" if prev else "flow unavailable (first snapshot)"
        print(f"[{etf}] {len(rows)} rows, coverage "
              f"{funds[etf]['price_coverage']:.1%}, {fl}")

    payload = {
        "built_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "funds": funds,
        "universe_size": len(universe),
    }
    LATEST_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8")
    SERIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    SERIES_PATH.write_text(
        json.dumps(series, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8")
    print(f"[out] {LATEST_PATH.name} "
          f"{LATEST_PATH.stat().st_size/1024:.0f}KB, "
          f"{SERIES_PATH.name} {SERIES_PATH.stat().st_size/1024:.0f}KB")
    return payload


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--etf", help="single registered fund")
    p.add_argument("--no-fetch", action="store_true",
                   help="reuse the price cache instead of downloading")
    a = p.parse_args(argv)
    etfs = [a.etf.upper()] if a.etf else sorted(MONITOR_FUNDS)
    try:
        build(etfs, a.no_fetch)
    except (MonitorError, HoldingsSourceError) as exc:
        print(f"FAIL: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
