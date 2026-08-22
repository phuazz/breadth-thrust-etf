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
import statistics
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
# A share count must move more than this, NET of the fund's own creation or
# redemption, before it is called a trade. The deadband absorbs the rounding
# in a published share count; the netting is what stops a creation reading as
# a portfolio-wide trade (see compute_flow).
FLOW_DEADBAND = 0.005
FLOW_SCALE_MIN_NAMES = 5
PRICE_CACHE = DATA_DIR / "holdings_monitor_prices.parquet"
LATEST_PATH = DATA_DIR / "holdings_monitor_latest.json"
SERIES_PATH = PROJECT_ROOT / "docs" / "holdings-monitor-series.json"

# Two years of daily closes: 200-day averages need ~10 months of runway
# before the first valid point, and the charts want a clean year on top.
PRICE_PERIOD = "2y"

MA_WINDOWS = {"m50": 50, "m100": 100, "m200": 200}
_PRICE_DP = 2

# Chart series are DAILY, on a shared date axis, price-only.
#
# The weekly grid this replaced was inherited from build_panel_series.py,
# whose reason was git history across 38 panels of constituents. It does not
# transfer: this page carries 168 names, and weekly sampling flattens exactly
# what the page exists to show — a name that gapped and then broke down reads
# as a gentle drift once you only keep Fridays.
#
# Three choices keep daily affordable, since this file is committed daily:
#   1. ONE shared date axis instead of one per ticker. Every holding is a US
#      listing on the same calendar, and per-ticker date arrays were 117KB of
#      the 307KB weekly file — 38% of it spent restating the same dates 168
#      times.
#   2. PRICE ONLY. The moving averages are recomputed in the browser from
#      these same daily closes, which is not an approximation: it is the same
#      arithmetic on the same inputs, so the chart and the table agree by
#      construction. Shipping three MA arrays as well would have cost 1.16MB
#      against 533KB.
#   3. Ship MA_LEAD_SESSIONS more history than the chart displays, so the
#      200-day average is defined at the LEFT edge of the visible window
#      rather than appearing 200 sessions in.
CHART_SESSIONS = 252        # displayed — one year
MA_LEAD_SESSIONS = 208      # extra runway so the 200d MA is valid from bar 1
SERIES_SESSIONS = CHART_SESSIONS + MA_LEAD_SESSIONS

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


def _round_sig(v: float) -> float:
    """Round to ~5 significant figures rather than to 2 decimal places.

    Flat 2dp is fine for a $400 stock and not fine for a $4 one, where it is
    a 0.1% error on every close. Averaged into a 100-day mean that error
    does not wash out, and it put the browser-computed moving average 0.14
    percentage points away from the server-computed figure in the table
    beside it — measured on MNKD, 2026-08-19. Two numbers describing the
    same thing on the same screen should not disagree in a digit the reader
    can see. Biotech rosters are full of low-priced names, so this is the
    common case here rather than an edge one.
    """
    if v == 0 or v != v:
        return 0.0
    from math import floor, log10
    digits = max(_PRICE_DP, min(4, 4 - int(floor(log10(abs(v))))))
    return round(v, digits)


def daily_panel(px: pd.DataFrame, tickers: list[str]) -> dict:
    """Daily closes for every name on ONE shared date axis.

    Returns ``{"dates": [...], "series": {ticker: [close|null, ...]}}`` with
    every array the same length as ``dates``. A null means the name did not
    trade that session (a mid-window listing, or a halt), which the browser
    must treat as "no observation" rather than as a zero — and which the
    moving average must skip rather than average in.

    Only the last SERIES_SESSIONS sessions are shipped. The chart displays
    the last CHART_SESSIONS of those; the remainder is lead-in so the
    200-day average is defined at the left edge of what the reader sees.
    """
    frame = px.tail(SERIES_SESSIONS)
    dates = [d.date().isoformat() for d in frame.index]
    out: dict[str, list] = {}
    for tk in tickers:
        if tk not in frame.columns:
            continue
        col = frame[tk]
        if col.notna().sum() < 10:
            continue
        out[tk] = [None if pd.isna(v) else _round_sig(float(v)) for v in col]
    return {"dates": dates, "series": out,
            "chart_sessions": CHART_SESSIONS,
            "ma_windows": MA_WINDOWS}


# ---------------------------------------------------------------------------
# Flow — the active-weight decomposition
# ---------------------------------------------------------------------------


def compute_flow(snap: RosterSnapshot, prev: dict | None,
                 px_now: dict[str, float]) -> tuple[dict[str, dict], float]:
    """Active weight change per name between ``prev`` and ``snap``.

    Returns the per-name flow and the fund's own share-count scale factor
    for the day — 1.0 when there was no creation or redemption.

    Counterfactual: hold yesterday's share counts, mark them at today's
    closes, renormalise to 100%. Subtracting that from today's actual
    weight isolates the trade, because price drift is present in both
    terms and cancels.

    A name missing a price cannot be marked, so it is excluded from the
    counterfactual basis entirely and reported as unavailable rather than
    given a flow of zero. Zero would read as "held", which is a claim.
    """
    if not prev:
        return {}, 1.0
    prev_shares = {h["ticker"]: h.get("shares") for h in prev.get("holdings", [])
                   if h.get("shares") is not None}
    if not prev_shares:
        return {}, 1.0

    cf_value, unpriced = {}, set()
    for tk, sh in prev_shares.items():
        p = px_now.get(tk)
        if p is None:
            unpriced.add(tk)
            continue
        cf_value[tk] = sh * p
    total_cf = sum(cf_value.values())
    if total_cf <= 0:
        return {}, 1.0
    cf_weight = {tk: 100.0 * v / total_cf for tk, v in cf_value.items()}

    now_weight = {h.ticker: h.weight_pct for h in snap.holdings}
    now_shares = {h.ticker: h.shares for h in snap.holdings}

    # Creations and redemptions scale EVERY position by the same factor, so a
    # raw share ratio reports a flow day as a complete portfolio turnover: XBI
    # moved all 147 names between +2.08% and +2.10% on 2026-08-19, which is
    # one creation, not 147 decisions. For any fund whose manager did not
    # trade most of the book that day, the MEDIAN ratio is that factor, so
    # dividing it out leaves the active change. The raw ratio is still
    # published beside it and the factor itself is reported, so nothing is
    # hidden — the flow simply stops being smeared across every row.
    ratios = sorted(
        now_shares[tk] / prev_shares[tk]
        for tk in set(now_shares) & set(prev_shares)
        if prev_shares.get(tk) and now_shares.get(tk) is not None
    )
    scale = (statistics.median(ratios)
             if len(ratios) >= FLOW_SCALE_MIN_NAMES else 1.0)

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
        d_net = (round((sh_now / sh_prev) / scale - 1, 5)
                 if d_sh is not None else None)

        if tk not in prev_shares:
            status = "new"
        elif tk not in now_weight:
            status = "exited"
        elif d_net is None:
            status = "held"
        elif d_net > FLOW_DEADBAND:
            status = "added"
        elif d_net < -FLOW_DEADBAND:
            status = "trimmed"
        else:
            status = "held"
        flow[tk] = {"status": status, "active_bp": active_bp,
                    "d_shares_pct": d_sh, "d_shares_net_pct": d_net}
    return flow, scale


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

    metrics, px_now = {}, {}
    for tk in universe:
        if tk not in px.columns:
            metrics[tk] = {"n_sessions": 0, "px": None, "px_date": None,
                           "state": None}
            continue
        m = name_metrics(px[tk])
        metrics[tk] = m
        if m["px"] is not None:
            px_now[tk] = m["px"]
    series = daily_panel(px, universe)

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
        flow, fund_scale = compute_flow(snap, prev, px_now)
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
                "fsh": f.get("d_shares_pct"), "fshn": f.get("d_shares_net_pct"),
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
            # The fund's own creation / redemption for the day, as a percent
            # of shares outstanding. Reported rather than netted away
            # silently: it is the difference between "the manager traded"
            # and "somebody bought the fund".
            "fund_flow_pct": round((fund_scale - 1.0) * 100.0, 3),
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
