"""Phase 8 — right-tail / optionality metrics for each strategy.

Motivation: bootstrap p(better) and Sharpe ratios are mean-variance
metrics — they treat positive vol and negative vol symmetrically. For
Strategy C in particular, this systematically underrates the value of
the strategy. C is structured as an optionality sleeve: capped 10%
sleeve weight (limits downside per year), unbounded upside if a
thematic bull fires. The right way to evaluate C is by RIGHT-TAIL
metrics, not by Sharpe contribution.

This script computes, for each of the 4 standalone strategies + the
deployed and baseline blends:

  - Sortino ratio (annualised mean / annualised downside-only std).
    Credits upside vol, only penalises drawdowns. Fair to convex
    strategies; Sharpe is not.
  - Skewness of monthly returns. Positive skew = right-tail bias =
    occasional big positive months. Negative skew = left-tail bias =
    occasional big drawdowns.
  - Best / worst rolling 12-month return (with the specific date
    windows). Shows the actual extreme of the distribution.
  - Per-regime performance across 4 sub-periods:
      * Q4 2018 Powell pivot sell-off (2018-10-01 -> 2018-12-31)
      * COVID + thematic boom (2020-03-23 -> 2021-02-15, ARKK peak)
      * 2022 inflation bear (2022-01-03 -> 2022-10-12)
      * 2024 AI surge (2024-01-02 -> 2024-12-31)
  - Per-strategy: % of months it was the top-performing sleeve
    (only meaningful for A/B/C/D on the common window).

Output: data/phase8_right_tail.json
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
OUT_PATH = DATA_DIR / "phase8_right_tail.json"

sys.stdout.reconfigure(encoding="utf-8")


REGIMES = [
    {
        "key": "q4_2018_pivot",
        "label": "Q4 2018 Powell pivot sell-off",
        "start": "2018-10-01",
        "end": "2018-12-31",
        "narrative": "Hawkish Powell + global growth scare. SPY fell ~14% in Q4. "
                     "Tested whether the strategies handled the first real risk-off "
                     "since launch.",
    },
    {
        "key": "covid_thematic_boom",
        "label": "COVID + thematic boom (Mar 2020 -> Feb 2021 ARKK peak)",
        "start": "2020-03-23",
        "end": "2021-02-15",
        "narrative": "Maximum stimulus + ZIRP environment. ARKK +152%, clean energy "
                     "doubled, biotech surged. The single best window for Strategy C-style "
                     "thematic exposure in living memory.",
    },
    {
        "key": "inflation_2022",
        "label": "2022 inflation crash (Jan -> Oct)",
        "start": "2022-01-03",
        "end": "2022-10-12",
        "narrative": "Fed pivot to aggressive hiking. SPY -25%, NASDAQ -35%, "
                     "thematic ETFs (ARKK, ICLN) -50%+. Test of downside protection.",
    },
    {
        "key": "ai_surge_2024",
        "label": "2024 AI surge",
        "start": "2024-01-02",
        "end": "2024-12-31",
        "narrative": "Nvidia +170%, large-cap AI plays drove the index. Test of "
                     "whether the rotation engines caught the move or chased it.",
    },
]


def _safe(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(f) or math.isinf(f)) else f


def sortino_ratio(daily_ret: np.ndarray, target: float = 0.0,
                    periods_per_year: int = 252) -> float:
    """Sortino = annualised mean excess return / annualised downside std.
    Only penalises returns below target (default 0). Credits upside vol.
    """
    if len(daily_ret) < 2:
        return float("nan")
    excess = daily_ret - target
    downside = excess[excess < 0]
    if len(downside) == 0:
        return float("inf")
    downside_std = np.sqrt((downside ** 2).mean()) * np.sqrt(periods_per_year)
    annual_excess = excess.mean() * periods_per_year
    if downside_std == 0:
        return 0.0
    return float(annual_excess / downside_std)


def rolling_12m_extremes(equity: pd.Series) -> dict:
    """Best and worst rolling 252-day return + their date windows."""
    if len(equity) < 252:
        return {"best": None, "worst": None,
                "best_start": None, "best_end": None,
                "worst_start": None, "worst_end": None}
    # Use the equity series directly; rolling return at date t = equity[t]/equity[t-252] - 1
    eq = equity.copy()
    shifted = eq.shift(252)
    roll = (eq / shifted - 1.0).dropna()
    best_idx = roll.idxmax()
    worst_idx = roll.idxmin()
    best_start = eq.index[eq.index.get_loc(best_idx) - 252]
    worst_start = eq.index[eq.index.get_loc(worst_idx) - 252]
    return {
        "best": float(roll.loc[best_idx]),
        "worst": float(roll.loc[worst_idx]),
        "best_start": best_start.strftime("%Y-%m-%d"),
        "best_end": best_idx.strftime("%Y-%m-%d"),
        "worst_start": worst_start.strftime("%Y-%m-%d"),
        "worst_end": worst_idx.strftime("%Y-%m-%d"),
    }


def monthly_returns(equity: pd.Series) -> pd.Series:
    """Resample equity curve to month-end and compute monthly returns."""
    me = equity.resample("ME").last()
    return me.pct_change().dropna()


def rolling_12m_hit_rate(equity: pd.Series) -> dict:
    """% of rolling 12-month windows (252 trading days) with positive return.

    Phase 11 (item B): answers the very-first-question-after-Sharpe an
    AI asks — 'how often does this strategy make money?'. A high Sharpe
    with 60% hit rate looks very different from the same Sharpe with
    85% hit rate even though average performance is identical: the
    high-hit-rate version compounds with fewer 'painful waiting'
    periods, which matters for client patience.
    """
    if len(equity) < 252:
        return {"hit_rate_pct": None, "n_windows": 0, "n_positive": 0}
    rolling = equity / equity.shift(252) - 1.0
    rolling = rolling.dropna()
    if len(rolling) == 0:
        return {"hit_rate_pct": None, "n_windows": 0, "n_positive": 0}
    n_positive = int((rolling > 0).sum())
    return {
        "hit_rate_pct": float(n_positive / len(rolling) * 100),
        "n_windows": int(len(rolling)),
        "n_positive": n_positive,
    }


def longest_drawdown(equity: pd.Series) -> dict:
    """Find the LONGEST drawdown by recovery duration (peak-to-recovery
    in days/months), not by depth.

    Phase 11 (item B): max DD tells you the worst loss; longest DD tells
    you how long you waited through it. Both matter for client
    expectations: 'we can lose -23%' is the loss tolerance question;
    'and it took 18 months to recover' is the patience question. Most
    clients underestimate the patience required and bail at month 9.
    """
    if len(equity) < 5:
        return {"depth": None, "duration_days": None, "duration_months": None,
                "peak_date": None, "trough_date": None, "recovery_date": None,
                "is_recovered": None}
    rmax = equity.cummax()
    dd = (equity - rmax) / rmax
    # Find every drawdown episode (peak → trough → recovery)
    in_dd = dd < 0
    episodes = []
    i = 0
    while i < len(equity):
        if in_dd.iloc[i]:
            # Start of a drawdown — peak was at i-1 (or earlier consecutive max)
            peak_idx = i - 1 if i > 0 else 0
            # Walk forward until equity recovers above the peak value
            peak_val = equity.iloc[peak_idx]
            trough_idx = i
            trough_val = equity.iloc[i]
            j = i
            while j < len(equity) and equity.iloc[j] < peak_val:
                if equity.iloc[j] < trough_val:
                    trough_idx = j
                    trough_val = equity.iloc[j]
                j += 1
            recovered = j < len(equity)
            recovery_idx = j if recovered else len(equity) - 1
            duration_days = (equity.index[recovery_idx] - equity.index[peak_idx]).days
            depth = (trough_val / peak_val) - 1.0
            episodes.append({
                "peak_idx": peak_idx, "trough_idx": trough_idx,
                "recovery_idx": recovery_idx, "duration_days": duration_days,
                "depth": depth, "recovered": recovered,
            })
            i = j + 1
        else:
            i += 1
    if not episodes:
        return {"depth": 0.0, "duration_days": 0, "duration_months": 0,
                "peak_date": None, "trough_date": None, "recovery_date": None,
                "is_recovered": True}
    # The "longest" drawdown — by duration. Ties broken by depth.
    longest = max(episodes, key=lambda e: (e["duration_days"], -e["depth"]))
    return {
        "depth": float(longest["depth"]),
        "duration_days": int(longest["duration_days"]),
        "duration_months": round(longest["duration_days"] / 30.44, 1),
        "peak_date": equity.index[longest["peak_idx"]].strftime("%Y-%m-%d"),
        "trough_date": equity.index[longest["trough_idx"]].strftime("%Y-%m-%d"),
        "recovery_date": (equity.index[longest["recovery_idx"]].strftime("%Y-%m-%d")
                            if longest["recovered"] else None),
        "is_recovered": bool(longest["recovered"]),
    }


def per_strategy_metrics(label: str, dates: pd.DatetimeIndex,
                           equity: pd.Series) -> dict:
    """Compute all right-tail metrics for one strategy."""
    daily = equity.pct_change().fillna(0).values
    # Sharpe (for sanity check vs other scripts)
    if daily.std() > 0:
        sharpe = daily.mean() / daily.std() * math.sqrt(252)
    else:
        sharpe = 0.0
    # Sortino
    sortino = sortino_ratio(daily)
    # Skewness — monthly is the more interpretable one
    mret = monthly_returns(equity)
    skew_monthly = float(stats.skew(mret.values)) if len(mret) > 2 else None
    skew_daily = float(stats.skew(daily)) if len(daily) > 2 else None
    # Rolling 12m extremes
    extremes = rolling_12m_extremes(equity)
    # Phase 11: rolling 12m hit rate + longest drawdown duration
    hit = rolling_12m_hit_rate(equity)
    long_dd = longest_drawdown(equity)
    # CAGR
    n_years = (equity.index[-1] - equity.index[0]).days / 365.25
    cagr = (float(equity.iloc[-1] / equity.iloc[0]) ** (1 / n_years) - 1
              if n_years > 0 else 0.0)
    # Max DD
    rmax = equity.cummax()
    dd = (equity - rmax) / rmax
    max_dd = float(dd.min())
    return {
        "label": label,
        "date_range": [dates[0].strftime("%Y-%m-%d"),
                        dates[-1].strftime("%Y-%m-%d")],
        "n_days": int(len(equity)),
        "n_months": int(len(mret)),
        "sharpe": _safe(sharpe),
        "sortino": _safe(sortino),
        "calmar": _safe(cagr / abs(max_dd)) if max_dd != 0 else None,
        "skew_daily": _safe(skew_daily),
        "skew_monthly": _safe(skew_monthly),
        "cagr": _safe(cagr),
        "max_dd": _safe(max_dd),
        "rolling_12m_best": _safe(extremes["best"]),
        "rolling_12m_worst": _safe(extremes["worst"]),
        "rolling_12m_best_window": [extremes["best_start"], extremes["best_end"]],
        "rolling_12m_worst_window": [extremes["worst_start"], extremes["worst_end"]],
        # Phase 11 additions
        "hit_rate_12m_pct": hit["hit_rate_pct"],
        "hit_rate_12m_n_windows": hit["n_windows"],
        "hit_rate_12m_n_positive": hit["n_positive"],
        "longest_dd_depth": _safe(long_dd["depth"]),
        "longest_dd_duration_days": long_dd["duration_days"],
        "longest_dd_duration_months": long_dd["duration_months"],
        "longest_dd_peak_date": long_dd["peak_date"],
        "longest_dd_trough_date": long_dd["trough_date"],
        "longest_dd_recovery_date": long_dd["recovery_date"],
        "longest_dd_is_recovered": long_dd["is_recovered"],
    }


def regime_stats(equity: pd.Series, start: str, end: str) -> dict:
    """Total return, max DD, and Sharpe within a sub-window."""
    win = equity.loc[(equity.index >= start) & (equity.index <= end)]
    if len(win) < 5:
        return {"total_return": None, "max_dd": None, "sharpe": None,
                "n_days": int(len(win))}
    win = win / float(win.iloc[0])
    daily = win.pct_change().fillna(0).values
    total_ret = float(win.iloc[-1] - 1.0)
    rmax = win.cummax()
    dd = (win - rmax) / rmax
    max_dd = float(dd.min())
    sharpe = (daily.mean() / daily.std() * math.sqrt(252)
                if daily.std() > 0 else 0.0)
    return {
        "total_return": _safe(total_ret),
        "max_dd": _safe(max_dd),
        "sharpe": _safe(sharpe),
        "n_days": int(len(win)),
    }


def load_equity(path: Path, equity_key: str = "headline_equity",
                  dates_key: str = "headline_equity_dates",
                  parent_key: str = "headline") -> tuple[pd.DatetimeIndex,
                                                          pd.Series]:
    """Load equity series from a strategy JSON."""
    blob = json.loads(path.read_text(encoding="utf-8"))
    if parent_key:
        h = blob[parent_key]
    else:
        h = blob
    dates = pd.to_datetime(h[dates_key])
    eq = pd.Series(h[equity_key], index=dates, dtype=float).sort_index()
    return dates, eq


def main() -> int:
    print("Computing Phase 8 right-tail metrics ...")

    # Load each strategy's standalone equity curve from its own JSON
    series = {}
    print("\nLoading standalone strategy equity curves ...")

    _, eq_a = load_equity(DATA_DIR / "topk_robustness.json")
    series["strategy_a"] = {"label": "Strategy A — US sector breadth (standalone)",
                              "equity": eq_a}
    print(f"  A: {eq_a.index[0].date()} -> {eq_a.index[-1].date()} ({len(eq_a)}d)")

    _, eq_b = load_equity(DATA_DIR / "asset_class_rotation.json")
    series["strategy_b"] = {"label": "Strategy B — asset-class momentum (standalone)",
                              "equity": eq_b}
    print(f"  B: {eq_b.index[0].date()} -> {eq_b.index[-1].date()} ({len(eq_b)}d)")

    _, eq_c = load_equity(DATA_DIR / "thematic_rotation.json")
    series["strategy_c"] = {"label": "Strategy C — thematic momentum (standalone, equal-weight)",
                              "equity": eq_c}
    print(f"  C: {eq_c.index[0].date()} -> {eq_c.index[-1].date()} ({len(eq_c)}d)")

    _, eq_d = load_equity(DATA_DIR / "europe_rotation.json")
    series["strategy_d"] = {"label": "Strategy D — Europe sector breadth (standalone)",
                              "equity": eq_d}
    print(f"  D: {eq_d.index[0].date()} -> {eq_d.index[-1].date()} ({len(eq_d)}d)")

    # Load blends from multi_strategy.json (these are normalised to common window)
    multi = json.loads((DATA_DIR / "multi_strategy.json").read_text(encoding="utf-8"))
    for k in ["blend_35_35_10_20", "blend_45_45_10", "blend_50_50"]:
        if k not in multi["strategies"]:
            continue
        v = multi["strategies"][k]
        dates = pd.to_datetime(v["dates"])
        eq = pd.Series(v["equity"], index=dates, dtype=float).sort_index()
        series[k] = {"label": v["label"], "equity": eq}
        print(f"  {k}: {eq.index[0].date()} -> {eq.index[-1].date()} ({len(eq)}d)")

    # ---------- Per-strategy metrics ----------
    print("\nComputing per-strategy right-tail metrics ...")
    per_strategy = {}
    for key, s in series.items():
        m = per_strategy_metrics(s["label"], s["equity"].index, s["equity"])
        per_strategy[key] = m
        print(f"  {key:<22}  sharpe {m['sharpe']:+.2f}  sortino {m['sortino']:+.2f}  "
              f"skew(m) {m['skew_monthly']:+.2f}  "
              f"12m best {m['rolling_12m_best']*100:+.0f}%  "
              f"12m worst {m['rolling_12m_worst']*100:+.0f}%")

    # ---------- Regime decomposition ----------
    print("\nComputing regime decomposition ...")
    regime_decomposition = {}
    for r in REGIMES:
        print(f"\n  {r['label']}:")
        regime_decomposition[r["key"]] = {
            "label": r["label"], "start": r["start"], "end": r["end"],
            "narrative": r["narrative"], "per_strategy": {},
        }
        for key, s in series.items():
            stats_ = regime_stats(s["equity"], r["start"], r["end"])
            regime_decomposition[r["key"]]["per_strategy"][key] = stats_
            if stats_["total_return"] is not None:
                print(f"    {key:<22}  totRet {stats_['total_return']*100:+6.1f}%  "
                      f"DD {stats_['max_dd']*100:+6.1f}%  "
                      f"Sharpe {stats_['sharpe']:+.2f}")
            else:
                print(f"    {key:<22}  (no data in window)")

    # ---------- % of months as top sleeve ----------
    print("\nComputing % of months each sleeve was the top performer ...")
    # Build monthly return panel for A/B/C/D on common window
    panel = {}
    for k in ["strategy_a", "strategy_b", "strategy_c", "strategy_d"]:
        if k in series:
            panel[k] = monthly_returns(series[k]["equity"])
    mret_df = pd.DataFrame(panel).dropna(how="any")
    print(f"  Common monthly panel: {mret_df.shape[0]} months, "
          f"{mret_df.index[0].date()} -> {mret_df.index[-1].date()}")
    # Per-month winner
    winners = mret_df.idxmax(axis=1)
    top_sleeve = {}
    for k in mret_df.columns:
        n_wins = int((winners == k).sum())
        top_sleeve[k] = {
            "n_months_top": n_wins,
            "n_months_total": int(len(winners)),
            "pct_top": _safe(n_wins / len(winners) * 100),
        }
        print(f"    {k:<22}  top in {n_wins}/{len(winners)} months "
              f"({n_wins/len(winners)*100:.1f}%)")

    # ---------- Cross-strategy return correlation matrix (Phase 11 item A) ----
    # 4x4 Pearson correlation matrix on the DAILY RETURN series of the four
    # sleeves over their common date window. Validates the diversification
    # thesis of the 4-sleeve blend — if correlations are low, the
    # blend's risk is genuinely diversified vs naive concatenation.
    print("\nComputing cross-strategy daily return correlation matrix ...")
    daily_panel = {}
    for k in ["strategy_a", "strategy_b", "strategy_c", "strategy_d"]:
        if k in series:
            daily_panel[k] = series[k]["equity"].pct_change().dropna()
    dret_df = pd.DataFrame(daily_panel).dropna(how="any")
    corr = dret_df.corr(method="pearson")
    # Format for JSON: dict-of-dicts so JS can render as a 4x4 heatmap
    cross_corr = {
        a: {b: round(float(corr.loc[a, b]), 3) for b in corr.columns}
        for a in corr.index
    }
    # Find lowest off-diagonal pair (the most-diversifying pair)
    pairs = []
    for i, a in enumerate(corr.index):
        for b in corr.index[i+1:]:
            pairs.append((a, b, float(corr.loc[a, b])))
    pairs.sort(key=lambda x: x[2])
    print(f"  Common daily panel: {dret_df.shape[0]} days, "
          f"{dret_df.index[0].date()} -> {dret_df.index[-1].date()}")
    print(f"  Correlation matrix:")
    print(corr.round(2).to_string())
    print(f"  Most-diversifying pair:  {pairs[0][0]} vs {pairs[0][1]} = {pairs[0][2]:+.2f}")
    print(f"  Least-diversifying pair: {pairs[-1][0]} vs {pairs[-1][1]} = {pairs[-1][2]:+.2f}")
    cross_correlation = {
        "matrix": cross_corr,
        "common_window": [dret_df.index[0].strftime("%Y-%m-%d"),
                          dret_df.index[-1].strftime("%Y-%m-%d")],
        "n_days": int(len(dret_df)),
        "most_diversifying_pair": {"a": pairs[0][0], "b": pairs[0][1],
                                     "corr": round(pairs[0][2], 3)},
        "least_diversifying_pair": {"a": pairs[-1][0], "b": pairs[-1][1],
                                      "corr": round(pairs[-1][2], 3)},
    }

    payload = {
        "computed_at_utc": pd.Timestamp.now(tz="UTC").isoformat(),
        "common_monthly_window": [mret_df.index[0].strftime("%Y-%m-%d"),
                                    mret_df.index[-1].strftime("%Y-%m-%d")],
        "per_strategy": per_strategy,
        "regime_decomposition": regime_decomposition,
        "top_sleeve_by_month": top_sleeve,
        "cross_correlation": cross_correlation,
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nWrote {OUT_PATH.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
