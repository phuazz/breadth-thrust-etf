"""Universe-expansion experiment — add commodity-SPOT ETFs to Strategies B and C.

Background
----------
The contractor handoff asked to widen "Strategy D" (a supposed Donchian trend-
follower) with commodities. Strategy D in this repo is NOT that — it is Europe-
sector top-K-by-breadth rotation, and its breadth signal cannot be computed on a
commodity ETF (no equity constituents). The trend/commodity thesis the handoff
describes maps onto the two PRICE-MOMENTUM rotation sleeves:

  * Strategy B — asset-class momentum rotation. Already holds GLD + DBC.
  * Strategy C — thematic momentum rotation. Already holds commodity-EQUITY
    (GDX, COPX, MOO, XME, WOOD, REMX) but NO commodity-SPOT.

Both rank on (close - MA200)/MA200, which is well-defined for any price series,
so the universe expansion is structurally valid for B and C (unlike A/D).

This script implements the expansion as VARIANTS, leaving the deployed strategies
untouched. It imports the real deployed engine functions (signal, weight, cash
floor, gate) so signal/sizing logic is identical — only the universe changes.

Instrument choice: ETFs, not futures. B and C trade ETFs; consistency matters,
and the strategy infrastructure has no futures-roll machinery. Commodity ETFs
embed roll/contango internally — handled by using broad/sector DB funds that run
an optimum-yield roll, and by EXCLUDING the single-commodity contango disasters
(USO, UNG) from the headline basket. They are tested only in the
narrow-to-strongest step. Roll cost is therefore inside the ETF NAV (adjusted
close), not modelled separately — correct for an ETF backtest.

Costs: per-ticker. Deployed base costs retained (B 2 bps, C 5 bps); commodity-
spot additions charged a conservative 10 bps one-way (wider spreads on DBA/DBB/
DBE than the ultra-liquid broad-asset ETFs). Cost sensitivity reported.

No look-ahead: yesterday's signal -> today's rebalance; yesterday's weights *
today's returns (identical to the deployed run_rotation). Baseline and variant
are run on the SAME date window and eligible_start so the comparison isolates the
universe change and nothing else.

Output: data/commodity_expansion.json  + a printed summary.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

import run_asset_class_rotation as B  # noqa: E402
import run_thematic_rotation as C      # noqa: E402

DATA = ROOT / "data"
COMMOD_CACHE = DATA / "commodity_expansion_prices.parquet"
OUT = DATA / "commodity_expansion.json"

MA = 200

# Commodity-SPOT additions. (ticker -> (label, inception, one-way cost bps))
# Inception dates verified against issuer factsheet + yfinance first trade date.
COMMOD_META = {
    "DBC": ("Invesco DB Commodity (broad)",        "2006-02-06", 6),
    "DBA": ("Invesco DB Agriculture",              "2007-01-05", 10),
    "DBB": ("Invesco DB Base Metals",              "2007-01-05", 12),
    "DBE": ("Invesco DB Energy",                   "2007-01-05", 12),
    "GSG": ("iShares S&P GSCI (broad, energy-wt)", "2006-07-21", 8),
    "USO": ("US Oil (WTI front, contango-prone)",  "2006-04-10", 5),
    "UNG": ("US NatGas (front, contango-prone)",   "2007-04-18", 8),
    "SLV": ("iShares Silver",                      "2006-04-28", 5),
}

# Headline commodity baskets added to each sleeve. Broad + 3 DB sectors give
# energy / base-metals / ags independent drivers with clean 2007+ history and a
# rules-based roll. USO/UNG/SLV/GSG held back for the narrow-to-strongest probe.
B_ADD = ["DBA", "DBB", "DBE"]          # B already has GLD + DBC
C_ADD = ["DBC", "DBA", "DBB", "DBE"]   # C has no commodity-spot at all

B_BASE_COST_BPS = 2
C_BASE_COST_BPS = 5
COMMOD_COST_BPS = 10   # conservative blended one-way for DB sector funds


# =========================================================================
# Engine — faithful re-implementation of the deployed run_rotation, extended
# to a PER-TICKER cost vector. Validated against the module engine below.
# =========================================================================
def run_rotation(closes, signal, weight_fn, eligible, cost_vec, freq="W-FRI"):
    rb_target = pd.date_range(eligible, closes.index[-1], freq=freq)
    rb_dates = closes.index[closes.index.isin(rb_target)]
    rbw = pd.DataFrame(index=rb_dates, columns=closes.columns, dtype=float)
    for rd in rb_dates:
        pi = closes.index.get_loc(rd) - 1
        if pi < 0:
            continue
        rbw.loc[rd] = weight_fn(signal.iloc[pi]).reindex(closes.columns).fillna(0.0)
    wp = rbw.reindex(closes.index).ffill().fillna(0.0)
    wp.loc[wp.index < eligible] = 0.0
    rets = closes.pct_change().fillna(0)
    gross = (wp.shift(1).fillna(0) * rets).sum(axis=1)
    tno_per = wp.diff().abs().fillna(0)
    cost_daily = (tno_per * cost_vec.reindex(closes.columns).fillna(0)).sum(axis=1)
    net = gross - cost_daily
    eq = (1.0 + net).cumprod()
    return {
        "equity": eq, "weights": wp, "daily_ret": net,
        "turnover": wp.diff().abs().sum(axis=1).fillna(0),
        "cost_daily": cost_daily,
    }


def metrics(equity, eligible):
    eq = equity.loc[equity.index >= eligible].copy()
    eq = eq / eq.iloc[0]
    daily = eq.pct_change().fillna(0)
    n_years = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = eq.iloc[-1] ** (1.0 / n_years) - 1.0 if n_years > 0 else 0.0
    vol = daily.std() * math.sqrt(252)
    sharpe = daily.mean() / daily.std() * math.sqrt(252) if daily.std() > 0 else 0.0
    rollmax = eq.cummax()
    dd = (eq - rollmax) / rollmax
    maxdd = float(dd.min())
    mar = cagr / abs(maxdd) if maxdd < 0 else float("nan")
    # Longest drawdown duration (calendar days from peak to recovery of that peak)
    longest = 0
    peak_date = eq.index[0]
    peak_val = eq.iloc[0]
    for dt, v in eq.items():
        if v >= peak_val:
            peak_val = v
            peak_date = dt
        else:
            longest = max(longest, (dt - peak_date).days)
    return {
        "cagr": cagr, "vol": vol, "sharpe": sharpe, "maxdd": maxdd,
        "mar": mar, "dd_dur_days": longest, "skew": float(daily.skew()),
        "total_return": float(eq.iloc[-1] - 1.0), "n_years": n_years,
    }


def ann_turnover(weights, eligible):
    wp = weights.loc[weights.index >= eligible]
    diff = wp.diff().abs().sum(axis=1).fillna(0)
    n_years = (wp.index[-1] - wp.index[0]).days / 365.25
    return float(diff.sum() / n_years) if n_years > 0 else 0.0


def fmt(m, to=None, cd=None):
    s = (f"CAGR {m['cagr']*100:+5.1f}%  MaxDD {m['maxdd']*100:6.1f}%  "
         f"MAR {m['mar']:.2f}  DDdur {m['dd_dur_days']:4d}d  "
         f"Sharpe {m['sharpe']:+.2f}  skew {m['skew']:+.2f}")
    if to is not None:
        s += f"  turn/yr {to:4.1f}x"
    if cd is not None:
        s += f"  cost/yr {cd*100:.2f}%"
    return s


def cost_series(cols, base_bps, add_map):
    cv = pd.Series(base_bps / 1e4, index=cols, dtype=float)
    for t, bps in add_map.items():
        if t in cv.index:
            cv[t] = bps / 1e4
    return cv


def common_window(*panels):
    """Inner-join on dates where every column of every panel is present."""
    df = pd.concat(panels, axis=1, sort=False)
    df = df.dropna()
    return df


def run_pair(name, base_closes, signal_fn, weight_factory, K, base_bps,
             add_closes, add_cost_bps, cost_drag_report):
    """Run baseline vs widened on a COMMON window/eligible. Returns dict."""
    widened = common_window(base_closes, add_closes)
    if len(widened) <= MA + 10:
        raise RuntimeError(f"{name}: window too short ({len(widened)} rows)")
    eligible = widened.index[MA]
    base_on_window = base_closes.reindex(widened.index)  # identical dates

    add_cost_map = {t: add_cost_bps for t in add_closes.columns}

    # Baseline
    sig_b = signal_fn(base_on_window)
    cv_b = cost_series(base_on_window.columns, base_bps, {})
    rb = run_rotation(base_on_window, sig_b, weight_factory(K), eligible, cv_b)
    mb = metrics(rb["equity"], eligible)
    tob = ann_turnover(rb["weights"], eligible)
    cdb = rb["cost_daily"].loc[rb["cost_daily"].index >= eligible].sum() / mb["n_years"]

    # Widened
    sig_w = signal_fn(widened)
    cv_w = cost_series(widened.columns, base_bps, add_cost_map)
    rw = run_rotation(widened, sig_w, weight_factory(K), eligible, cv_w)
    mw = metrics(rw["equity"], eligible)
    tow = ann_turnover(rw["weights"], eligible)
    cdw = rw["cost_daily"].loc[rw["cost_daily"].index >= eligible].sum() / mw["n_years"]

    print(f"\n=== {name}  (K={K}, weekly, window {eligible.date()} -> "
          f"{widened.index[-1].date()}, {mb['n_years']:.1f}y) ===")
    print(f"  baseline ({len(base_on_window.columns)-1} ETFs+cash): "
          f"{fmt(mb, tob, cdb)}")
    print(f"  +commod  ({len(widened.columns)-1} ETFs+cash, +{list(add_closes.columns)}): "
          f"{fmt(mw, tow, cdw)}")
    dmar = mw["mar"] - mb["mar"]
    ddd = (mw["maxdd"] - mb["maxdd"]) * 100
    print(f"  delta:  MAR {dmar:+.2f}   MaxDD {ddd:+.1f}pp   "
          f"CAGR {(mw['cagr']-mb['cagr'])*100:+.1f}pp   "
          f"Sharpe {mw['sharpe']-mb['sharpe']:+.2f}")

    return {
        "name": name, "K": K, "eligible": str(eligible.date()),
        "end": str(widened.index[-1].date()),
        "baseline": {**mb, "turnover": tob, "cost_drag": cdb},
        "widened": {**mw, "turnover": tow, "cost_drag": cdw,
                    "added": list(add_closes.columns)},
        "_eq_base": rb["equity"], "_eq_wide": rw["equity"], "_eligible": eligible,
        "_widened_closes": widened,
    }


# =========================================================================
# Diversification: commodity sleeve vs equity sleeve, calm vs stress
# =========================================================================
def sleeve_returns(closes, signal_fn, weight_factory, K, eligible, base_bps):
    sig = signal_fn(closes)
    cv = cost_series(closes.columns, base_bps, {})
    r = run_rotation(closes, signal_fn(closes), weight_factory(K), eligible, cv)
    return r["daily_ret"].loc[r["daily_ret"].index >= eligible]


def diversification(spy, comm_ret, eq_ret, label):
    idx = comm_ret.index.intersection(eq_ret.index).intersection(spy.index)
    comm_ret = comm_ret.reindex(idx)
    eq_ret = eq_ret.reindex(idx)
    spy_eq = (spy.reindex(idx).pct_change().fillna(0) + 1).cumprod()
    dd = (spy_eq - spy_eq.cummax()) / spy_eq.cummax()
    stress = dd < -0.10          # SPY > 10% underwater
    calm = ~stress
    def c(mask):
        a, b = comm_ret[mask], eq_ret[mask]
        if len(a) < 20 or a.std() == 0 or b.std() == 0:
            return float("nan")
        return float(np.corrcoef(a, b)[0, 1])
    out = {
        "corr_full": c(pd.Series(True, index=idx)),
        "corr_calm": c(calm), "corr_stress": c(stress),
        "n_stress_days": int(stress.sum()), "n_total": int(len(idx)),
    }
    print(f"\n--- Diversification [{label}] commodity-sleeve vs equity-sleeve "
          f"daily-return correlation ---")
    print(f"    full {out['corr_full']:+.2f}   calm {out['corr_calm']:+.2f}   "
          f"stress(SPY>10%DD) {out['corr_stress']:+.2f}   "
          f"({out['n_stress_days']}/{out['n_total']} stress days)")
    return out


# =========================================================================
# Start-date sensitivity / entry-point discipline
# =========================================================================
def start_sensitivity(name, base_closes, signal_fn, weight_factory, K, base_bps,
                      add_closes, add_cost_bps, dbc, start_years):
    widened = common_window(base_closes, add_closes)
    base_on_window = base_closes.reindex(widened.index)
    add_cost_map = {t: add_cost_bps for t in add_closes.columns}
    sig_b = signal_fn(base_on_window)
    sig_w = signal_fn(widened)
    cv_b = cost_series(base_on_window.columns, base_bps, {})
    cv_w = cost_series(widened.columns, base_bps, add_cost_map)
    rows = []
    print(f"\n--- Start-date sensitivity [{name}] (MAR baseline -> +commod) ---")
    print("    start      DBC_12m   baseMAR  wideMAR   dMAR   note")
    for y in start_years:
        cand = widened.index[widened.index >= pd.Timestamp(f"{y}-01-01")]
        if len(cand) < 252:
            continue
        elig = cand[0]
        if widened.index.get_loc(elig) < MA:
            elig = widened.index[MA]
        rb = run_rotation(base_on_window, sig_b, weight_factory(K), elig, cv_b)
        rw = run_rotation(widened, sig_w, weight_factory(K), elig, cv_w)
        mb, mw = metrics(rb["equity"], elig), metrics(rw["equity"], elig)
        # DBC trailing-12m return at entry — entry-point discipline flag
        dser = dbc.reindex(widened.index).ffill()
        pos = dser.index.get_loc(dser.index[dser.index <= elig][-1])
        d12 = (dser.iloc[pos] / dser.iloc[max(0, pos - 252)] - 1.0) if pos >= 252 else float("nan")
        note = "after FLAT/down commod" if (d12 == d12 and d12 <= 0) else "after commod run-up"
        rows.append({"start": str(elig.date()), "dbc_12m": d12,
                     "base_mar": mb["mar"], "wide_mar": mw["mar"],
                     "dmar": mw["mar"] - mb["mar"], "note": note})
        print(f"    {elig.date()}  {d12*100:+6.1f}%   {mb['mar']:5.2f}   "
              f"{mw['mar']:5.2f}   {mw['mar']-mb['mar']:+5.2f}  {note}")
    return rows


def main():
    print("Loading panels ...")
    ac = pd.read_parquet(DATA / "asset_class_prices_cache.parquet")
    th = pd.read_parquet(DATA / "thematic_prices_cache.parquet")
    cm = pd.read_parquet(COMMOD_CACHE)
    for df in (ac, th, cm):
        df.index = pd.to_datetime(df.index).tz_localize(None)

    # Deployed universes (read straight from the modules — no transcription)
    b_cols = [t for t in B.TICKERS if t in ac.columns] + [B.CASH_PROXY]
    c_cols = [t for t in C.TICKERS if t in th.columns] + [C.CASH_PROXY]
    B_base = ac[b_cols].dropna(how="all")
    C_base = th[c_cols].dropna(how="all")
    print(f"  B deployed universe: {len(b_cols)-1} ETFs + cash")
    print(f"  C deployed universe: {len(c_cols)-1} ETFs + cash")

    # ---- Validate my engine reproduces the deployed module engine ----
    closes_v = B_base.dropna()
    elig_v = closes_v.index[MA]
    sig_v = B.compute_signal(closes_v)
    cv_v = cost_series(closes_v.columns, B_BASE_COST_BPS, {})
    mine = run_rotation(closes_v, sig_v, B.top_k_by_signal(B.HEADLINE_K), elig_v, cv_v)
    theirs = B.run_rotation(closes_v, sig_v, B.top_k_by_signal(B.HEADLINE_K), elig_v)
    diff = (mine["equity"] - theirs["equity"]).abs().max()
    print(f"  engine self-check (B): max equity diff vs deployed = {diff:.2e}")
    assert diff < 1e-9, "engine replication mismatch — abort"

    results = {}

    # ================= Strategy B =================
    rB = run_pair("Strategy B (asset-class)", B_base, B.compute_signal,
                  B.top_k_by_signal, B.HEADLINE_K, B_BASE_COST_BPS,
                  cm[B_ADD], COMMOD_COST_BPS, True)
    results["B"] = {k: v for k, v in rB.items() if not k.startswith("_")}

    # ================= Strategy C =================
    rC = run_pair("Strategy C (thematic)", C_base, C.compute_signal,
                  C.top_k_equal_weight, C.HEADLINE_K, C_BASE_COST_BPS,
                  cm[C_ADD], COMMOD_COST_BPS, True)
    results["C"] = {k: v for k, v in rC.items() if not k.startswith("_")}

    # ================= Diversification =================
    # B sleeves
    bw = rB["_widened_closes"]; eligB = rB["_eligible"]
    comm_uni_B = bw[["GLD", "DBC", "DBA", "DBB", "DBE", B.CASH_PROXY]]
    eq_uni_B = bw[["SPY", "IJR", "QQQ", "EFA", "VGK", "EWJ", "EEM", "VNQ", B.CASH_PROXY]]
    comm_ret_B = sleeve_returns(comm_uni_B, B.compute_signal, B.top_k_by_signal, 3, eligB, B_BASE_COST_BPS)
    eq_ret_B = sleeve_returns(eq_uni_B, B.compute_signal, B.top_k_by_signal, 3, eligB, B_BASE_COST_BPS)
    results["div_B"] = diversification(bw["SPY"], comm_ret_B, eq_ret_B, "Strategy B")

    # C sleeves: commodity-spot sleeve vs the thematic equity sleeve
    cw = rC["_widened_closes"]; eligC = rC["_eligible"]
    comm_uni_C = cw[["DBC", "DBA", "DBB", "DBE", C.CASH_PROXY]]
    eq_names_C = [t for t in C.TICKERS if t in cw.columns] + [C.CASH_PROXY]
    eq_uni_C = cw[eq_names_C]
    comm_ret_C = sleeve_returns(comm_uni_C, C.compute_signal, C.top_k_equal_weight, 3, eligC, C_BASE_COST_BPS)
    eq_ret_C = sleeve_returns(eq_uni_C, C.compute_signal, C.top_k_equal_weight, C.HEADLINE_K, eligC, C_BASE_COST_BPS)
    spy_for_C = ac["SPY"].reindex(cw.index).ffill()
    results["div_C"] = diversification(spy_for_C, comm_ret_C, eq_ret_C, "Strategy C")

    # ================= Start-date sensitivity =================
    results["sens_B"] = start_sensitivity(
        "Strategy B", B_base, B.compute_signal, B.top_k_by_signal, B.HEADLINE_K,
        B_BASE_COST_BPS, cm[B_ADD], COMMOD_COST_BPS, cm["DBC"],
        [2008, 2010, 2012, 2014, 2016, 2018, 2020, 2022])
    results["sens_C"] = start_sensitivity(
        "Strategy C", C_base, C.compute_signal, C.top_k_equal_weight, C.HEADLINE_K,
        C_BASE_COST_BPS, cm[C_ADD], COMMOD_COST_BPS, cm["DBC"],
        [2019, 2020, 2021, 2022, 2023])

    # ================= Narrow-to-strongest probe (B) =================
    # If broad expansion does not help, test single-sector commodity subsets to
    # find where breakout-trend behaviour is cleanest.
    print("\n=== Narrow-to-strongest probe (Strategy B, MAR) ===")
    subsets = {
        "energy(DBE)": ["DBE"], "basemetals(DBB)": ["DBB"], "ags(DBA)": ["DBA"],
        "energy+metals": ["DBE", "DBB"], "DB-sectors(DBA+DBB+DBE)": ["DBA", "DBB", "DBE"],
        "broad+sectors+GSG": ["DBA", "DBB", "DBE", "GSG"],
        "+USO+UNG(contango)": ["DBA", "DBB", "DBE", "USO", "UNG"],
    }
    base_mar = rB["baseline"]["mar"] if isinstance(rB["baseline"], dict) else None
    results["narrow_B"] = {}
    for lab, add in subsets.items():
        add_df = cm[add]
        widened = common_window(B_base, add_df)
        elig = widened.index[MA]
        base_w = B_base.reindex(widened.index)
        cvw = cost_series(widened.columns, B_BASE_COST_BPS, {t: COMMOD_COST_BPS for t in add})
        cvb = cost_series(base_w.columns, B_BASE_COST_BPS, {})
        rwid = run_rotation(widened, B.compute_signal(widened), B.top_k_by_signal(B.HEADLINE_K), elig, cvw)
        rbase = run_rotation(base_w, B.compute_signal(base_w), B.top_k_by_signal(B.HEADLINE_K), elig, cvb)
        mw, mb = metrics(rwid["equity"], elig), metrics(rbase["equity"], elig)
        results["narrow_B"][lab] = {"base_mar": mb["mar"], "wide_mar": mw["mar"],
                                     "dmar": mw["mar"] - mb["mar"], "wide_maxdd": mw["maxdd"]}
        print(f"    {lab:28s} baseMAR {mb['mar']:.2f} -> wideMAR {mw['mar']:.2f}  "
              f"(dMAR {mw['mar']-mb['mar']:+.2f}, MaxDD {mw['maxdd']*100:.1f}%)")

    # ================= Cost sensitivity on the B headline =================
    print("\n=== Cost sensitivity (Strategy B +DB-sectors, one-way bps on adds) ===")
    results["cost_sens_B"] = {}
    widened = common_window(B_base, cm[B_ADD]); elig = widened.index[MA]
    for bps in [0, 5, 10, 20, 40]:
        cvw = cost_series(widened.columns, B_BASE_COST_BPS, {t: bps for t in B_ADD})
        rwid = run_rotation(widened, B.compute_signal(widened), B.top_k_by_signal(B.HEADLINE_K), elig, cvw)
        mw = metrics(rwid["equity"], elig)
        results["cost_sens_B"][bps] = mw["mar"]
        print(f"    adds @ {bps:2d} bps:  MAR {mw['mar']:.2f}  CAGR {mw['cagr']*100:+.1f}%  MaxDD {mw['maxdd']*100:.1f}%")

    # ---- persist (strip non-serialisable) ----
    def clean(o):
        if isinstance(o, dict):
            return {k: clean(v) for k, v in o.items()
                    if not (isinstance(k, str) and k.startswith("_"))}
        if isinstance(o, float) and (math.isnan(o) or math.isinf(o)):
            return None
        if isinstance(o, (np.floating,)):
            return float(o)
        if isinstance(o, (np.integer,)):
            return int(o)
        return o
    OUT.write_text(json.dumps(clean(results), indent=2, default=str))
    print(f"\nSaved {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
