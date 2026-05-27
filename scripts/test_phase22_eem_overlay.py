"""Phase 22 — EEM/SPY relative-strength overlay.

Origin: Idea 3 (country momentum rotation) was rejected with 23 years
of data showing it fails in every regime. The user pointed out the
EM-cycle-turn thesis is still valid (EEM/SPY at 24% of 2010 peak — deep
underperformance) but momentum-on-country-ETFs is the wrong tool to
capture it. This phase tries a mechanical alternative: passively tilt
into EEM when its relative-strength vs SPY breaks out.

Signal variants tested:
  V1: Ratio > 200d MA               (price > MA, standard trend follow)
  V2: 50d MA > 200d MA              (golden cross — slower, less whipsaw)
  V3: 200d MA slope positive        (2nd-derivative; "trend is forming")
  V4: V1 AND V3                     (price above MA AND MA rising)

Tilt sizing tested: 5%, 10%, 15% of blend reallocated to EEM during
tilt-on days. The other (1-w) stays in the deployed 4-way gated blend.

Headline: best variant should improve 2022-onwards return without
materially degrading full Sharpe / DD.

Usage: python scripts/test_phase22_eem_overlay.py
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
sys.stdout.reconfigure(encoding="utf-8")

WINDOWS = [
    ("Full",         None,         None),
    ("2022 only",    "2022-01-01", "2022-12-31"),
    ("2022-onwards", "2022-01-01", None),
]

SWITCH_COST_BPS = 5  # cost per transition into/out of EEM tilt


def _stats(eq: pd.Series) -> dict:
    if len(eq) < 5:
        return {"sharpe": None, "cagr": None, "total": None, "dd": None}
    e = eq.dropna() / eq.dropna().iloc[0]
    d = e.pct_change().fillna(0)
    n = (e.index[-1] - e.index[0]).days / 365.25
    return {
        "sharpe": d.mean() / d.std() * math.sqrt(252) if d.std() > 0 else 0,
        "cagr": e.iloc[-1] ** (1 / n) - 1 if n > 0 else 0,
        "total": e.iloc[-1] - 1,
        "dd": ((e - e.cummax()) / e.cummax()).min(),
    }


def _ws(eq, start, end):
    w = eq.loc[start:end] if (start or end) else eq
    return _stats(w)


def load_eem_spy_ratio() -> pd.Series:
    """EEM/SPY price ratio, USD-adjusted both sides. Cached if available."""
    cache = DATA_DIR / "em_regime_context.parquet"
    if cache.exists():
        df = pd.read_parquet(cache)
        if "EEM" in df.columns and "SPY" in df.columns:
            stale = (pd.Timestamp.utcnow().tz_localize(None) - df.index.max()).days
            if stale <= 7:
                ratio = df["EEM"] / df["SPY"]
                return ratio.dropna()
    print("  Downloading EEM + SPY ...")
    raw = yf.download(["EEM", "SPY"], start="2003-01-01", auto_adjust=True,
                       progress=False, threads=True, group_by="ticker")
    closes = {t: raw[(t, "Close")] for t in ["EEM", "SPY"]
              if (t, "Close") in raw.columns}
    df = pd.DataFrame(closes)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df.to_parquet(cache)
    return (df["EEM"] / df["SPY"]).dropna()


def load_eem_prices() -> pd.Series:
    """EEM price for the tilt return computation."""
    cache = DATA_DIR / "em_regime_context.parquet"
    df = pd.read_parquet(cache)
    return df["EEM"].dropna()


def compute_signal(ratio: pd.Series, variant: str) -> pd.Series:
    """Return a 0/1 series: 1 = EM-favoured (tilt ON), 0 = US-favoured (tilt OFF)."""
    ma200 = ratio.rolling(200, min_periods=200).mean()
    ma50 = ratio.rolling(50, min_periods=50).mean()
    if variant == "V1_ratio_above_ma200":
        return (ratio > ma200).astype(float)
    if variant == "V2_ma50_above_ma200":
        return (ma50 > ma200).astype(float)
    if variant == "V3_ma200_slope_pos":
        # MA today > MA 30 days ago
        return (ma200 > ma200.shift(30)).astype(float)
    if variant == "V4_above_ma_and_rising":
        cond_a = ratio > ma200
        cond_b = ma200 > ma200.shift(30)
        return (cond_a & cond_b).astype(float)
    raise ValueError(f"Unknown variant: {variant}")


def apply_tilt(blend_eq: pd.Series, eem_prices: pd.Series,
                 tilt_signal: pd.Series, tilt_weight: float,
                 switch_cost_bps: float = SWITCH_COST_BPS) -> pd.Series:
    """Build a new equity curve where, on tilt-ON days, (1-w) is in the
    deployed blend and w is in EEM. On tilt-OFF days, 100% in blend.

    The tilt is applied to YESTERDAY's signal (no look-ahead), and we
    charge switch_cost_bps every time the tilt state flips."""
    common = blend_eq.index
    eem = eem_prices.reindex(common, method="ffill")
    blend_ret = blend_eq.pct_change().fillna(0)
    eem_ret = eem.pct_change().fillna(0)
    sig = tilt_signal.reindex(common, method="ffill").fillna(0).shift(1).fillna(0)
    sw_changes = sig.diff().fillna(0).abs()
    sw_cost = sw_changes * (switch_cost_bps / 10_000.0)
    # tilt-ON: w in EEM, (1-w) in blend; tilt-OFF: 100% blend
    eem_w = sig * tilt_weight
    blend_w = 1.0 - eem_w
    tilted_ret = blend_w * blend_ret + eem_w * eem_ret - sw_cost
    return (1.0 + tilted_ret).cumprod()


def main():
    print("Loading deployed gated blend + EEM/SPY ratio ...")
    overlay = json.loads((DATA_DIR / "risk_overlay.json").read_text(encoding="utf-8"))
    gated = overlay["gated_variants"]["blend_35_35_10_20_gated"]
    blend_eq = pd.Series(gated["equity"], index=pd.to_datetime(gated["dates"]))
    print(f"  Blend: {blend_eq.index[0].date()} -> {blend_eq.index[-1].date()}  "
          f"({len(blend_eq)} days)")

    ratio = load_eem_spy_ratio()
    eem_prices = load_eem_prices()
    print(f"  EEM/SPY ratio: {ratio.index[0].date()} -> {ratio.index[-1].date()}")
    print(f"  Current ratio: {ratio.iloc[-1]:.4f}  (peak {ratio.max():.4f} on "
          f"{ratio.idxmax().date()}, trough {ratio.min():.4f} on "
          f"{ratio.idxmin().date()})")
    print(f"  Currently at {ratio.iloc[-1]/ratio.max()*100:.1f}% of peak, "
          f"{ratio.iloc[-1]/ratio.min()*100:.1f}% of trough")

    # Baseline stats
    base_stats = {w[0]: _ws(blend_eq, w[1], w[2]) for w in WINDOWS}
    print(f"\nBASELINE (deployed gated 4-way blend):")
    for w in WINDOWS:
        s = base_stats[w[0]]
        if s["sharpe"] is None: continue
        print(f"  {w[0]:<14s}  Sharpe {s['sharpe']:+.3f}  "
              f"Total {s['total']*100:+6.1f}%  DD {s['dd']*100:.1f}%")

    # Sweep variants × tilt weights
    variants = ["V1_ratio_above_ma200", "V2_ma50_above_ma200",
                 "V3_ma200_slope_pos", "V4_above_ma_and_rising"]
    weights = [0.05, 0.10, 0.15]
    rows = []
    for v in variants:
        sig = compute_signal(ratio, v)
        # Diagnostic: % of days tilt-ON, n switches
        sig_aligned = sig.reindex(blend_eq.index, method="ffill").fillna(0)
        pct_on = sig_aligned.mean() * 100
        n_sw = int(sig_aligned.diff().fillna(0).abs().sum())
        for w in weights:
            tilted = apply_tilt(blend_eq, eem_prices, sig, w)
            wstats = {win[0]: _ws(tilted, win[1], win[2]) for win in WINDOWS}
            rows.append({
                "variant": v, "tilt": w,
                "stats": wstats, "pct_on": pct_on, "n_sw": n_sw,
            })

    # Print: variant × weight sweep
    print("\n" + "=" * 130)
    print("PHASE 22 EEM/SPY RELATIVE-STRENGTH OVERLAY — variant x tilt-weight sweep")
    print("=" * 130)
    print(f"  {'Variant':<28s} {'Tilt':>5s}  {'Full Sh':>7s} {'dSh':>6s}  "
          f"{'Full DD':>7s} {'dDD':>6s}  {'22 Tot':>7s} {'d':>5s}  "
          f"{'22-on Tot':>9s} {'d':>5s}  {'%on':>4s} {'sw':>3s}")
    for r in rows:
        s_f = r["stats"]["Full"]; s_22 = r["stats"]["2022 only"]
        s_22on = r["stats"]["2022-onwards"]
        b_f = base_stats["Full"]; b_22 = base_stats["2022 only"]
        b_22on = base_stats["2022-onwards"]
        if s_f["sharpe"] is None: continue
        d_sh = s_f["sharpe"] - b_f["sharpe"]
        d_dd = (s_f["dd"] - b_f["dd"]) * 100
        d_22 = (s_22["total"] - b_22["total"]) * 100
        d_22on = (s_22on["total"] - b_22on["total"]) * 100
        print(f"  {r['variant']:<28s} {int(r['tilt']*100):>4d}%  "
              f"{s_f['sharpe']:>+6.3f} {d_sh:>+5.3f}  "
              f"{s_f['dd']*100:>+6.1f}% {d_dd:>+5.1f}pp  "
              f"{s_22['total']*100:>+6.1f}% {d_22:>+5.1f}pp  "
              f"{s_22on['total']*100:>+8.1f}% {d_22on:>+5.1f}pp  "
              f"{r['pct_on']:>3.0f}% {r['n_sw']:>3d}")

    # Find best by 2022-onwards Total without degrading Full Sharpe meaningfully
    print(f"\n{'=' * 100}")
    print(f"Best variants by (2022-on dTotal + 5 x Full dSharpe):")
    scored = []
    for r in rows:
        if r["stats"]["Full"]["sharpe"] is None: continue
        d_sh = r["stats"]["Full"]["sharpe"] - base_stats["Full"]["sharpe"]
        d_22on = (r["stats"]["2022-onwards"]["total"] -
                   base_stats["2022-onwards"]["total"]) * 100
        scored.append((d_22on + 5*d_sh, r, d_sh, d_22on))
    scored.sort(reverse=True)
    for score, r, d_sh, d_22on in scored[:5]:
        s_22on = r["stats"]["2022-onwards"]
        print(f"  {r['variant']:<28s} tilt {int(r['tilt']*100):>2d}%  "
              f"score {score:+.2f}  Full dSh {d_sh:+.3f}  "
              f"22-on Total {s_22on['total']*100:+.1f}% (d {d_22on:+.1f}pp)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
