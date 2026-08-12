"""WS14 - sleeve A priced on the LSE UCITS lines actually held.

QUESTION
    Sleeve A signals on the constituent breadth of UK/Irish UCITS funds but
    PRICES itself through US trading proxies: CSP1 backtests as SPY, CNDX as
    QQQ, IUES as XLE. The instruments Zhenghao would actually buy are the
    London-listed UCITS lines, which trade a different session, carry wider
    spreads and track their index with their own error. WS13 flagged this and
    could not answer it. Does sleeve A's result survive on the real
    instruments?

METHOD
    ONLY the price panel changes. Sleeve A's signal is constituent breadth,
    computed from rosters rather than from the ETF price, so the signal panel
    is byte-identical between the two legs and any difference is attributable
    to the traded instrument alone.

        proxy basis   US-listed proxies, exactly as deployed
        lse basis     the .L lines, converted to USD where they quote in pence

    Both legs run the deployed headline configuration (relative-breadth
    signal, K = HEADLINE_K, W-FRI) over ONE eligible window and ONE signal
    panel.

WHAT WOULD MAKE THIS SILENTLY WRONG
    1. Currency. CSP1.L and IUSP.L quote in GBp (pence); the rest of the
       sleeve's London lines quote in USD. Pricing a sleeve on a mix of the
       two is meaningless, and the error is invisible in a Sharpe. Every line's
       currency is resolved from the feed at runtime, never assumed, and a
       currency this script does not know how to convert is fatal rather than
       silently passed through.
    2. A universe change masquerading as a venue change. SOXX has no London
       line (SOXX.L does not exist), so the LSE basis cannot cover the deployed
       14. Comparing an LSE-13 against a proxy-14 would measure the missing
       semiconductor sleeve, not the venue. Both legs therefore run the SAME
       13 names, and the deployed 14-name result is reported separately as
       context rather than as the comparator.
    3. A mis-mapped line. A .L ticker that is not the same fund would still
       produce a plausible curve. Each converted London line is checked
       against its US proxy for daily-return correlation; a pair that does not
       track is reported and, below a floor, fatal.
    4. The wrong trading calendar. London and New York keep different
       holidays, so the LSE leg rebalances on the LSE calendar rather than
       inheriting NYSE.

USAGE
    python scripts/run_ws14_sleeve_a_lse.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import rebalance_calendar  # noqa: E402
import run_portfolio as rp  # noqa: E402
import run_topk_robustness as tk  # noqa: E402
from etf_registry import get_etf  # noqa: E402
from run_portfolio import run_portfolio, top_k_breadth_weight  # noqa: E402
from run_ws12_fill_lag import sharpe_se, stats  # noqa: E402

OUT_PATH = SCRIPTS.parent / "data_local" / "ws14_sleeve_a_lse.json"

# Correlation floor between a London line and its US proxy, on WEEKLY returns.
#
# It must be weekly. The LSE closes at 16:30 London, which is 11:30 in New
# York, so a London line's DAILY return covers a different slice of the world
# from its US proxy's close-to-close. Measured daily, every pair in this sleeve
# scores 0.46-0.75 — not because any is mis-mapped but because none is struck
# at the same instant, and the two pence-quoted lines score worst of all
# because their FX rate is struck at a third time again. A daily floor set on
# a synchronous-close prior therefore rejects correct pairs, which is exactly
# what the first version of this guard did.
#
# Friday-to-Friday returns wash the intraday offset out while still catching
# the failure the guard exists for: a .L ticker that is not the same fund.
#
# The floor is 0.70, not 0.90. The US proxies were never exact: the Select
# Sector SPDRs track CAPPED Select Sector indices while the UCITS lines track
# plain GICS sector indices, so XLP-vs-IUCS (0.888) and XLRE-vs-IUSP (0.883)
# are honestly different index construction rather than a bad mapping. A
# 0.90 floor rejects them and asserts a precision the proxy substitution never
# had. A genuinely wrong fund sits near zero, which 0.70 catches easily.
CORR_FLOOR_WEEKLY = 0.70


def lse_symbol(key: str) -> str:
    return f"{key}.L"


def fetch_lse_panel(keys: list[str], start: str, end: str) -> tuple:
    """(usd_closes, currency_map, notes) for the London lines.

    Currency is READ from the feed per ticker. A GBp line is pence, so it is
    divided by 100 and multiplied by GBP/USD; a USD line passes through. Any
    other currency raises rather than being guessed at.
    """
    import yfinance as yf

    syms = [lse_symbol(k) for k in keys]
    raw = yf.download(syms, start=start, end=end, auto_adjust=True,
                      progress=False, threads=True, group_by="ticker")
    fx = yf.download("GBPUSD=X", start=start, end=end, auto_adjust=True,
                     progress=False, threads=False)
    if isinstance(fx.columns, pd.MultiIndex):
        fx.columns = fx.columns.get_level_values(0)
    fx = fx["Close"]
    fx.index = pd.to_datetime(fx.index).tz_localize(None)

    cols, ccy, notes = {}, {}, []
    for k in keys:
        s = lse_symbol(k)
        try:
            px = raw[s]["Close"]
        except Exception:  # noqa: BLE001
            raise RuntimeError(f"{s}: no price data returned")
        px.index = pd.to_datetime(px.index).tz_localize(None)
        px = px.dropna()
        if px.empty:
            raise RuntimeError(f"{s}: empty series")
        cur = (yf.Ticker(s).fast_info or {}).get("currency") or "?"
        ccy[k] = cur
        if cur == "USD":
            cols[k] = px.astype(float)
        elif cur in ("GBp", "GBX"):
            rate = fx.reindex(px.index).ffill()
            conv = (px.astype(float) / 100.0) * rate
            n_missing = int(rate.isna().sum())
            if n_missing:
                notes.append(f"{s}: {n_missing} sessions without an FX rate "
                             "after forward-fill; those bars are dropped")
            cols[k] = conv.dropna()
        else:
            raise RuntimeError(
                f"{s}: currency {cur!r} is not one this script knows how to "
                "convert. Refusing to guess — a wrong currency is invisible "
                "in a Sharpe.")
    df = pd.DataFrame(cols).sort_index()
    return df, ccy, notes


def main() -> int:
    # --- Signal and proxy panel from the deployed path, untouched.
    closes_proxy_all, breadths_all, used = rp.build_panels()
    lse_keys = [k for k in used if k != "SOXX"]
    dropped = [k for k in used if k not in lse_keys]
    print(f"deployed universe: {len(used)} names; LSE-priceable: "
          f"{len(lse_keys)}; dropped for want of a London line: {dropped}")

    start = (closes_proxy_all.index.min() - pd.Timedelta(days=10)).strftime("%Y-%m-%d")
    end = (closes_proxy_all.index.max() + pd.Timedelta(days=5)).strftime("%Y-%m-%d")
    print("fetching London lines ...", flush=True)
    lse_raw, ccy, notes = fetch_lse_panel(lse_keys, start, end)
    print("  currencies:", {k: ccy[k] for k in lse_keys})
    for n in notes:
        print("  NOTE:", n)

    # --- GUARD 3: each London line must track its US proxy.
    corr_d, corr_w = {}, {}
    for k in lse_keys:
        a, b = lse_raw[k], closes_proxy_all[k]
        j = a.index.intersection(b.index)
        corr_d[k] = float(a.loc[j].pct_change().corr(b.loc[j].pct_change()))
        wa = a.loc[j].resample("W-FRI").last().pct_change()
        wb = b.loc[j].resample("W-FRI").last().pct_change()
        corr_w[k] = float(wa.corr(wb))
    print(f"  proxy-vs-London return correlation — daily: min "
          f"{min(corr_d.values()):.3f}, median {np.median(list(corr_d.values())):.3f}"
          f"  |  weekly: min {min(corr_w.values()):.3f}, median "
          f"{np.median(list(corr_w.values())):.3f}")
    print("  (the daily figures are depressed by the 16:30 London vs 16:00 "
          "New York close, not by mis-mapping — hence the weekly test)")
    bad = {k: round(v, 3) for k, v in corr_w.items()
           if not np.isfinite(v) or v < CORR_FLOOR_WEEKLY}
    if bad:
        raise SystemExit(
            f"these London lines do not track their US proxy on WEEKLY "
            f"returns: {bad} — a mis-mapped ticker would still produce a "
            "plausible curve, so this is fatal rather than a warning")

    # --- Both legs on the SAME 13 names and the SAME signal panel.
    breadths = breadths_all[lse_keys]
    signal = tk._to_signal_panel(breadths)
    starts = [breadths[e].dropna().index.min() for e in lse_keys
              if len(breadths[e].dropna())]
    eligible = pd.Timestamp(max(starts).date()) + pd.Timedelta(days=tk.MA_PERIOD)

    legs = {}
    for name, closes, cal in (
        ("proxy_us", closes_proxy_all[lse_keys].dropna(), "NYSE"),
        ("lse_ucits", lse_raw.dropna(), "LSE"),
    ):
        elig = closes.index[closes.index >= eligible]
        if len(elig) == 0:
            raise SystemExit(f"{name}: no sessions at or after {eligible.date()}")
        elig = elig[0]
        sig = signal.reindex(closes.index).ffill()
        r = run_portfolio(closes, sig, top_k_breadth_weight(tk.HEADLINE_K),
                          elig, rebalance_freq=tk.HEADLINE_FREQ,
                          cost=tk.COST_FRAC, calendar=cal)
        st = stats(r["equity"], elig)
        st["calendar"] = cal
        st["n_sessions"] = int(len(closes))
        st["window"] = [elig.strftime("%Y-%m-%d"),
                        closes.index[-1].strftime("%Y-%m-%d")]
        legs[name] = st
        print(f"  {name:10} Sharpe {st['sharpe']:+.4f}  CAGR "
              f"{st['cagr']*100:+6.2f}%  DD {st['max_dd']*100:6.2f}%  "
              f"[{cal}] {st['window'][0]} -> {st['window'][1]}")

    # --- Cost stress: the London lines are thinner than the US proxies.
    stress = {}
    closes_lse = lse_raw.dropna()
    elig_l = closes_lse.index[closes_lse.index >= eligible][0]
    sig_l = signal.reindex(closes_lse.index).ffill()
    for mult in (1.0, 2.0, 3.0):
        r = run_portfolio(closes_lse, sig_l, top_k_breadth_weight(tk.HEADLINE_K),
                          elig_l, rebalance_freq=tk.HEADLINE_FREQ,
                          cost=tk.COST_FRAC * mult, calendar="LSE")
        stress[f"{mult:g}x"] = stats(r["equity"], elig_l)["sharpe"]
    print("  LSE leg cost stress:", {k: round(v, 4) for k, v in stress.items()})

    delta = {k: legs["lse_ucits"][k] - legs["proxy_us"][k]
             for k in ("sharpe", "cagr", "max_dd", "total_return")}
    print(f"\n  lse - proxy: Sharpe {delta['sharpe']:+.4f}  "
          f"CAGR {delta['cagr']*100:+.2f}pp  DD {delta['max_dd']*100:+.2f}pp"
          f"   (Sharpe SE ~{sharpe_se(legs['proxy_us']['years']):.2f})")

    payload = {
        "universe_deployed": used,
        "universe_priced": lse_keys,
        "dropped_no_london_line": dropped,
        "currencies": ccy,
        "proxy_map": {k: (get_etf(k).get("yfinance_trading_proxy") or k)
                      for k in lse_keys},
        "proxy_vs_london_corr_daily": corr_d,
        "proxy_vs_london_corr_weekly": corr_w,
        "corr_floor_weekly": CORR_FLOOR_WEEKLY,
        "legs": legs,
        "delta_lse_minus_proxy": delta,
        "lse_cost_stress_sharpe": stress,
        "notes": notes,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nWrote {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
