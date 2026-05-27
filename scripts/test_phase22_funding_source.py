"""Phase 22 funding-source test — compare proportional vs from-A-only
funding for the EEM tilt.

User raised a design question: the original Phase 22 test tilts 10%
proportionally from all four sleeves (A 35->31.5, B 35->31.5, C 10->9,
D 20->18). Better to fund it from A only (US sectors are the natural
counter-exposure for an EM tilt; B already holds EEM in its universe;
C/D are smaller and need their full allocation).

Tests V2 (50d/200d golden cross) at 10% tilt under both funding methods.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
sys.stdout.reconfigure(encoding="utf-8")

WINDOWS = [
    ("Full",         None,         None),
    ("2022 only",    "2022-01-01", "2022-12-31"),
    ("2022-onwards", "2022-01-01", None),
]
SWITCH_COST_BPS = 5


def _stats(eq):
    if len(eq) < 5: return {"sharpe": None}
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


def compute_signal_v2(ratio: pd.Series) -> pd.Series:
    ma50 = ratio.rolling(50, min_periods=50).mean()
    ma200 = ratio.rolling(200, min_periods=200).mean()
    return (ma50 > ma200).astype(float)


def main():
    print("Loading deployed sleeves + gated blend + EEM/SPY ...")
    multi = json.loads((DATA_DIR / "multi_strategy.json").read_text(encoding="utf-8"))
    overlay = json.loads((DATA_DIR / "risk_overlay.json").read_text(encoding="utf-8"))

    # Per-sleeve equity series (these are the UNGATED sleeves)
    a = pd.Series(multi["strategies"]["strategy_a"]["equity"],
                   index=pd.to_datetime(multi["strategies"]["strategy_a"]["dates"]))
    b = pd.Series(multi["strategies"]["strategy_b"]["equity"],
                   index=pd.to_datetime(multi["strategies"]["strategy_b"]["dates"]))
    c = pd.Series(multi["strategies"]["strategy_c"]["equity"],
                   index=pd.to_datetime(multi["strategies"]["strategy_c"]["dates"]))
    d = pd.Series(multi["strategies"]["strategy_d"]["equity"],
                   index=pd.to_datetime(multi["strategies"]["strategy_d"]["dates"]))

    # Deployed gated blend (this is what Phase 22 currently tilts against)
    gated = overlay["gated_variants"]["blend_35_35_10_20_gated"]
    blend_eq = pd.Series(gated["equity"], index=pd.to_datetime(gated["dates"]))

    # Per-sleeve risk-overlay states (use the same gate logic as the overlay)
    # Simpler: derive from the gated equity returns vs ungated 4-way blend
    common = blend_eq.index.intersection(a.index).intersection(b.index).intersection(c.index).intersection(d.index)
    ar = a.reindex(common).pct_change().fillna(0)
    br = b.reindex(common).pct_change().fillna(0)
    cr = c.reindex(common).pct_change().fillna(0)
    dr = d.reindex(common).pct_change().fillna(0)

    # Reconstruct ungated 4-way blend daily returns
    ungated_ret = 0.35*ar + 0.35*br + 0.10*cr + 0.20*dr

    # Gated blend daily returns
    gated_ret = blend_eq.reindex(common).pct_change().fillna(0)

    # Imply the "blend weight" the overlay was using each day:
    # gated_ret ≈ blend_w * ungated_ret + (1 - blend_w) * shy_ret
    # Solve for blend_w using SHY returns.
    ac = pd.read_parquet(DATA_DIR / "asset_class_prices_cache.parquet")
    shy = ac["SHY"].reindex(common, method="ffill")
    shy_ret = shy.pct_change().fillna(0)
    # blend_w = (gated_ret - shy_ret) / (ungated_ret - shy_ret)
    # When ungated_ret == shy_ret this is unstable; clip and assume blend_w=1 in those cases
    denom = ungated_ret - shy_ret
    blend_w_implied = (gated_ret - shy_ret) / denom.replace(0, 1e-9)
    # In practice blend_w should be 1.0 (RISK_ON) or 0.5 (RISK_OFF). Snap to those.
    snap = blend_w_implied.apply(lambda x: 1.0 if abs(x - 1.0) < abs(x - 0.5) else 0.5)

    # EEM/SPY signal
    em_ctx = pd.read_parquet(DATA_DIR / "em_regime_context.parquet")
    ratio = (em_ctx["EEM"] / em_ctx["SPY"]).dropna()
    eem_prices = em_ctx["EEM"].dropna()
    sig = compute_signal_v2(ratio).reindex(common, method="ffill").fillna(0).shift(1).fillna(0)
    eem_ret = eem_prices.reindex(common, method="ffill").pct_change().fillna(0)

    TILT = 0.10

    def apply_funding(method: str) -> pd.Series:
        """Apply the EEM tilt with a specified funding method.

        method='proportional':  reduces all 4 sleeves uniformly by TILT
        method='from_A':        reduces only A by TILT (in absolute pp)
        method='from_C':        reduces only C — for comparison
        method='from_D':        reduces only D — for comparison
        """
        if method == "proportional":
            # Each sleeve scaled by (1 - TILT)
            wa, wb, wc, wd = 0.35*(1-TILT), 0.35*(1-TILT), 0.10*(1-TILT), 0.20*(1-TILT)
        elif method == "from_A":
            wa, wb, wc, wd = 0.35 - TILT, 0.35, 0.10, 0.20
        elif method == "from_B":
            wa, wb, wc, wd = 0.35, 0.35 - TILT, 0.10, 0.20
        elif method == "from_D":
            wa, wb, wc, wd = 0.35, 0.35, 0.10, 0.20 - TILT
        else:
            raise ValueError(method)
        # When sig=0 (tilt OFF): use baseline 35/35/10/20 (no EEM)
        # When sig=1 (tilt ON):  use new weights + EEM
        tilt_off_ret = 0.35*ar + 0.35*br + 0.10*cr + 0.20*dr
        tilt_on_ret = wa*ar + wb*br + wc*cr + wd*dr + TILT*eem_ret
        new_ungated_ret = sig * tilt_on_ret + (1.0 - sig) * tilt_off_ret
        # Apply the SAME risk overlay (snapped blend_w) to the new ungated
        new_gated_ret = snap * new_ungated_ret + (1.0 - snap) * shy_ret
        # Switch cost: state changes in tilt sig
        sw = sig.diff().fillna(0).abs() * (SWITCH_COST_BPS / 10_000)
        new_gated_ret = new_gated_ret - sw
        return (1.0 + new_gated_ret).cumprod()

    base_stats = {w[0]: _ws(blend_eq.reindex(common), w[1], w[2]) for w in WINDOWS}

    print("\n" + "=" * 110)
    print("Phase 22 funding-source comparison — V2 golden cross at 10% tilt")
    print("=" * 110)
    print(f"\nBASELINE (deployed gated 4-way, no EEM tilt):")
    for w in WINDOWS:
        s = base_stats[w[0]]
        if s["sharpe"] is None: continue
        print(f"  {w[0]:<14s}  Sharpe {s['sharpe']:+.3f}  Total {s['total']*100:+6.1f}%  "
              f"DD {s['dd']*100:.1f}%")

    for method in ["proportional", "from_A", "from_B", "from_D"]:
        eq = apply_funding(method)
        stats = {w[0]: _ws(eq, w[1], w[2]) for w in WINDOWS}
        print(f"\nFunding: {method}")
        for w in WINDOWS:
            s = stats[w[0]]; b = base_stats[w[0]]
            if s["sharpe"] is None: continue
            d_sh = s["sharpe"] - b["sharpe"]
            d_tot = (s["total"] - b["total"]) * 100
            d_dd = (s["dd"] - b["dd"]) * 100
            print(f"  {w[0]:<14s}  Sharpe {s['sharpe']:+.3f} (d{d_sh:+.3f})  "
                  f"Total {s['total']*100:+6.1f}% (d{d_tot:+.2f}pp)  "
                  f"DD {s['dd']*100:.1f}% (d{d_dd:+.2f}pp)")


if __name__ == "__main__":
    main()
