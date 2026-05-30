"""Daily mark-to-market overlay for the deployed blend.

Why this exists
---------------
The full pipeline runs weekly on Saturday (after Friday US close). The
deployed-blend equity series in ``data/risk_overlay.json`` therefore
ends on Friday's close and stays static all week. For a CIO doing live
deployment tracking on Tue / Wed / Thu, this is one or more weekdays
behind.

The strategy itself rebalances Friday close — the holdings selected
at Friday close apply Mon→Fri of the following week, with no intra-
week trading. So computing mid-week NAV does NOT require re-running
signals, walk-forward, bootstrap, or any other expensive logic. It is
a simple weighted-buy-and-hold of the latest holdings against this
week's daily ETF closes.

This script does exactly that and writes ``data/live_track.json`` —
a small JSON the dashboard appends to the deployed blend equity series
before computing the WTD hero card and drawing the Performance chart.

Architecture invariant
----------------------
This script NEVER touches the backtest engine state. The Friday-anchor
equity in ``risk_overlay.json`` is the source of truth; ``live_track``
is a strictly forward-only extension from that anchor. If anything
disagrees, ``risk_overlay.json`` wins.

Run
---
    python scripts/mark_to_market_live.py

Output
------
    data/live_track.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

DEPLOYED_KEY = "blend_35_35_10_20_gated_eem_tilted"

# Europe sleeve UCITS on Xetra — need .DE suffix and EUR/USD conversion.
EUROPE_TICKERS = {"EXV1", "EXH1", "EXV3", "EXH3", "EXH9"}

# China A-share tickers — already include the .SZ/.SS suffix in the
# trade_history; needs USDCNY=X conversion.
CN_FX_SUFFIXES = (".SZ", ".SS")


def _load_registry() -> dict:
    """Import the canonical ETF registry as a dict.

    Used to look up the ``yfinance_trading_proxy`` for Strategy A's
    iShares UCITS (e.g. IUES → XLE), so the mark-to-market uses the
    liquid US-listed equivalent rather than the .L UCITS.
    """
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    from etf_registry import ETF_REGISTRY  # type: ignore[import]
    return ETF_REGISTRY


def _resolve_yf_symbol(ticker: str, registry: dict) -> tuple[str, str]:
    """Map a holdings ticker to (yfinance_symbol, fx_handling).

    fx_handling ∈ {'none', 'eur_to_usd', 'cny_to_usd'} tells the caller
    how to post-process the raw yfinance series into USD.

    Strategy A iShares UCITS use the US-listed trading proxy from the
    registry. Europe-sleeve UCITS get the .DE suffix + EUR/USD. China
    A-shares get CNY/USD. Everything else is assumed to be a direct
    USD-denominated yfinance ticker (SPY, EEM, BTC-USD, ...).
    """
    if ticker in EUROPE_TICKERS:
        return (f"{ticker}.DE", "eur_to_usd")
    if ticker.endswith(CN_FX_SUFFIXES):
        return (ticker, "cny_to_usd")
    if ticker in registry and registry[ticker].get("yfinance_trading_proxy"):
        proxy = registry[ticker]["yfinance_trading_proxy"]
        # If the ticker IS the proxy (e.g. SPY -> SPY) just use it.
        return (proxy, "none")
    return (ticker, "none")


def _yf_close_series(symbols: list[str], start: str, end: str) -> pd.DataFrame:
    """Download adj close for ``symbols`` over [start, end] as a single
    DataFrame indexed by date with one column per symbol. Returns an
    empty DataFrame if all symbols fail (graceful for CI without
    network)."""
    if not symbols:
        return pd.DataFrame()
    import yfinance as yf
    raw = yf.download(
        symbols, start=start, end=end,
        auto_adjust=True, progress=False, threads=False,
        group_by="ticker" if len(symbols) > 1 else None,
    )
    if raw is None or raw.empty:
        return pd.DataFrame()
    out = {}
    if isinstance(raw.columns, pd.MultiIndex):
        for sym in symbols:
            if sym in raw.columns.get_level_values(0):
                series = raw[sym]["Close"] if "Close" in raw[sym].columns else None
                if series is not None and not series.empty:
                    out[sym] = series
    else:
        if "Close" in raw.columns:
            out[symbols[0]] = raw["Close"]
    df = pd.DataFrame(out)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df.sort_index()


def _build_effective_weights(
    sleeves: dict, p22_active: bool, regime_state: str
) -> dict[str, float]:
    """Mirror the dashboard's renderPositionsPreview NAV-weight logic so
    the mark-to-market uses the same holdings the live panel shows.

    Returns ``{ticker: nav_weight}`` summing to 1.0 (after the cash
    residual if any). Sleeve weights apply the EEM-tilt adjustment when
    active. The RISK_OFF regime gate is honoured by halving the
    equity-side weights and adding a 50% SHY weight — same as the
    deployed blend equity construction.
    """
    base_wts = {
        "a": 0.35,
        "b": 0.25 if p22_active else 0.35,
        "c": 0.10,
        "d": 0.20,
    }
    # RISK_OFF: half NAV in SHY, half in original blend
    equity_scaler = 0.5 if regime_state == "RISK_OFF" else 1.0
    sleeve_wts = {k: v * equity_scaler for k, v in base_wts.items()}

    weights: dict[str, float] = {}
    for key, sleeve in sleeves.items():
        sw = sleeve_wts[key]
        trades = (sleeve.get("headline", {}) or {}).get("trade_history", [])
        if not trades:
            continue
        for h in trades[-1].get("holdings", []):
            etf = h.get("etf")
            wt = h.get("weight", 0)
            if etf and wt > 0:
                weights[etf] = weights.get(etf, 0) + wt * sw

    # EEM tilt — 10% NAV (also scaled by RISK_OFF)
    if p22_active:
        weights["EEM"] = weights.get("EEM", 0) + 0.10 * equity_scaler

    # Cash residual goes into SHY (also when RISK_OFF allocates 50%).
    cash_wt = 1.0 - sum(weights.values())
    if cash_wt > 1e-6:
        weights["SHY"] = weights.get("SHY", 0) + cash_wt

    return weights


def _fetch_usd_prices(
    weights: dict[str, float], anchor_date: str, registry: dict
) -> pd.DataFrame:
    """Fetch USD-denominated close series for each held ticker from
    ``anchor_date`` forward. Returns a DataFrame indexed by date with
    one column per ETF ticker (NOT yfinance symbol — we map back).

    Applies EUR/USD or CNY/USD conversion where needed so every series
    is in USD terms, matching the deployed blend equity construction.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Pad start by 2 calendar days to ensure anchor_date is included
    start = (pd.Timestamp(anchor_date) - pd.Timedelta(days=2)).strftime("%Y-%m-%d")
    end = (pd.Timestamp(today) + pd.Timedelta(days=2)).strftime("%Y-%m-%d")

    # Resolve each ETF -> (yfinance_symbol, fx_handling)
    resolutions = {etf: _resolve_yf_symbol(etf, registry) for etf in weights}
    all_yf_syms = sorted(set(s for s, _ in resolutions.values()))
    needs_eur_fx = any(fx == "eur_to_usd" for _, fx in resolutions.values())
    needs_cny_fx = any(fx == "cny_to_usd" for _, fx in resolutions.values())

    fx_syms = []
    if needs_eur_fx:
        fx_syms.append("EURUSD=X")
    if needs_cny_fx:
        fx_syms.append("USDCNY=X")

    print(f"  Fetching {len(all_yf_syms)} ETF symbol(s) + {len(fx_syms)} FX "
          f"from {start} to {end} ...")
    raw = _yf_close_series(all_yf_syms + fx_syms, start, end)
    if raw.empty:
        return pd.DataFrame()

    # Normalise the index (drop time component) and forward-fill FX so it
    # exists on every trading day even when FX has weekend gaps.
    raw.index = pd.to_datetime(raw.index).normalize()
    eur_usd = raw["EURUSD=X"].ffill() if "EURUSD=X" in raw.columns else None
    usd_cny = raw["USDCNY=X"].ffill() if "USDCNY=X" in raw.columns else None

    out = {}
    for etf, (sym, fx) in resolutions.items():
        if sym not in raw.columns:
            continue
        series = raw[sym].dropna()
        if series.empty:
            continue
        if fx == "eur_to_usd" and eur_usd is not None:
            # EUR price * (USD/EUR) -> USD price
            fx_aligned = eur_usd.reindex(series.index, method="ffill")
            series = series * fx_aligned
        elif fx == "cny_to_usd" and usd_cny is not None:
            # CNY price * (1 / (CNY/USD)) -> USD price
            fx_aligned = usd_cny.reindex(series.index, method="ffill")
            series = series / fx_aligned
        out[etf] = series

    df = pd.DataFrame(out).sort_index()
    return df


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default=str(DATA_DIR / "live_track.json"))
    args = p.parse_args()

    print("=== Daily mark-to-market overlay ===")
    print(f"Now (UTC): {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

    # Load all sleeve JSONs + risk overlay
    sleeves = {
        "a": json.loads((DATA_DIR / "topk_robustness.json").read_text(encoding="utf-8")),
        "b": json.loads((DATA_DIR / "asset_class_rotation.json").read_text(encoding="utf-8")),
        "c": json.loads((DATA_DIR / "thematic_rotation.json").read_text(encoding="utf-8")),
        "d": json.loads((DATA_DIR / "europe_rotation.json").read_text(encoding="utf-8")),
    }
    overlay = json.loads((DATA_DIR / "risk_overlay.json").read_text(encoding="utf-8"))
    blend = overlay["gated_variants"][DEPLOYED_KEY]
    anchor_date = blend["dates"][-1]
    anchor_equity = blend["equity"][-1]
    print(f"Deployed blend anchor: {anchor_date}  equity = {anchor_equity:.6f}")

    p22_active = (overlay.get("phase22_eem_tilt", {})
                   .get("current_state") == "EM_TILT_ON")
    regime_state = overlay.get("current_state", "RISK_ON")
    print(f"Regime: {regime_state} | EEM tilt: {'ON' if p22_active else 'OFF'}")

    registry = _load_registry()
    weights = _build_effective_weights(sleeves, p22_active, regime_state)
    wt_sum = sum(weights.values())
    print(f"\nEffective NAV weights ({len(weights)} positions, sum={wt_sum:.4f}):")
    for etf, w in sorted(weights.items(), key=lambda x: -x[1]):
        print(f"  {etf:<12} {w * 100:6.2f}%")
    if abs(wt_sum - 1.0) > 0.01:
        print(f"  WARNING: weights do not sum to 1.0 (sum={wt_sum:.4f})")

    prices = _fetch_usd_prices(weights, anchor_date, registry)
    if prices.empty:
        print("\nERROR: no price data downloaded. Cannot compute mark-to-market.",
              file=sys.stderr)
        return 1

    # Filter to dates strictly AFTER anchor_date and only where we have
    # an anchor-date price for the holdings (so the return calculation
    # has a valid baseline). Anchor date itself MUST be present in the
    # series for every held ticker — otherwise we cannot compute a
    # baseline return.
    anchor_ts = pd.Timestamp(anchor_date)
    if anchor_ts not in prices.index:
        # Find the most recent date at-or-before anchor (handles cases
        # where the anchor was a holiday or yfinance lag).
        valid = prices.index[prices.index <= anchor_ts]
        if len(valid) == 0:
            print(f"\nERROR: no prices at-or-before anchor date {anchor_date}",
                  file=sys.stderr)
            return 1
        actual_anchor = valid.max()
        print(f"\nNote: shifted anchor from {anchor_date} to "
              f"{actual_anchor.strftime('%Y-%m-%d')} (closest prior trading day)")
        anchor_ts = actual_anchor

    # For each ETF that has an anchor-date price, record the baseline.
    baselines = {etf: prices[etf].loc[:anchor_ts].dropna().iloc[-1]
                  for etf in prices.columns
                  if not prices[etf].loc[:anchor_ts].dropna().empty}
    held_with_baseline = [e for e in weights if e in baselines]
    held_without = [e for e in weights if e not in baselines]
    if held_without:
        print(f"\nWARNING: no anchor-date price for {held_without} — "
              "these holdings will be assumed flat (0% return) for the overlay.")

    # Walk forward day by day from anchor + 1 onwards
    post_dates = prices.index[prices.index > anchor_ts]
    daily_dates: list[str] = []
    daily_equity: list[float] = []
    for d in post_dates:
        # nav_factor = sum_i (weight_i * P_i(d) / P_i(anchor))
        factor = 0.0
        used_wt = 0.0
        for etf, w in weights.items():
            if etf not in baselines:
                continue
            p_d = prices[etf].get(d)
            if p_d is None or pd.isna(p_d):
                # Use most recent prior price for this ETF
                series = prices[etf].loc[:d].dropna()
                if series.empty:
                    continue
                p_d = series.iloc[-1]
            factor += w * (p_d / baselines[etf])
            used_wt += w
        # Holdings without prices contribute 0% return (i.e., they stay
        # at their baseline weight, equivalent to adding w to factor).
        missing_wt = sum(weights[e] for e in held_without)
        factor += missing_wt
        if used_wt + missing_wt < 0.99:
            # Should never happen since weights sum to 1, but log if so
            print(f"  WARN {d.strftime('%Y-%m-%d')}: only {used_wt+missing_wt:.3f} "
                  "weight covered — skipping day")
            continue
        daily_dates.append(d.strftime("%Y-%m-%d"))
        daily_equity.append(anchor_equity * factor)

    if not daily_dates:
        print("\nNo new daily data beyond anchor — nothing to write.")
        # Still write a header file so the dashboard can detect zero-extension
    else:
        print(f"\nGenerated {len(daily_dates)} intra-week NAV point(s):")
        for d, e in zip(daily_dates, daily_equity):
            pct_from_anchor = (e / anchor_equity - 1) * 100
            print(f"  {d}: equity {e:.6f} ({pct_from_anchor:+.3f}% vs anchor)")

    payload = {
        "computed_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "deployed_key": DEPLOYED_KEY,
        "anchor_date": anchor_date,
        "anchor_equity": anchor_equity,
        "regime_state": regime_state,
        "eem_tilt_active": p22_active,
        "effective_weights": {k: round(v, 6) for k, v in
                                sorted(weights.items(), key=lambda x: -x[1])},
        "live_dates": daily_dates,
        "live_equity": [round(v, 6) for v in daily_equity],
        "notes": (
            "Daily mark-to-market overlay of the deployed blend. "
            "Holdings = latest weekly rebalance from each sleeve's "
            "trade_history, scaled by sleeve NAV weights (with EEM "
            "tilt and breadth-gate adjustments). Daily NAV computed "
            "as weighted buy-and-hold from anchor_date forward."
        ),
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nWrote {out_path.relative_to(PROJECT_ROOT)} "
          f"({out_path.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
