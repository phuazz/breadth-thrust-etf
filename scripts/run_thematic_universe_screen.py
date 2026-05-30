"""Strategy C universe screening — systematic candidate ETF gate test.

Runs every plausible liquid US-listed thematic ETF through the
within-strategy correlation gate against Strategy C's existing
23 themes, then empirically backtests survivors to measure their
walk-forward Sharpe contribution.

Three prior attempts to expand C have failed the empirical gate
(Phase 5 sub-industries: −0.10 WF Sharpe; Phase 16 SLV: −0.18
B-sleeve Sharpe; Phase 17 KWEB: −0.13 WF Sharpe). This script makes
the next attempt SYSTEMATIC rather than discretionary: every
candidate is scored against the same two gates, with documented
pass/fail per candidate.

Two-stage gate (matches Phase 5 / Phase 17 conventions):

Stage 1 — CORRELATION GATE (cheap, runs for all candidates):
    Candidate's weekly Friday signal (distance above own 200d MA)
    max pairwise correlation with any existing Strategy C theme
    must be < 0.85. Failures named by cousin.

Stage 2 — EMPIRICAL GATE (runs only for Stage-1 survivors):
    Add candidate to C's 23-ETF universe -> 24-ETF universe.
    Re-run Strategy C top-K rotation. Walk-forward Sharpe must
    not degrade by more than 0.03 (tighter than A's 0.05 because
    C is a smaller sleeve with thinner margin).

Usage:
    python scripts/run_thematic_universe_screen.py

Output:
    data/thematic_universe_screen.json
    Console summary with per-candidate verdict
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

# ---------------------------------------------------------------------------
# Candidate pool — liquid US-listed thematic ETFs not already in Strategy C.
# Curated to AVOID obvious cousins (e.g., HACK ~ CIBR, FDN ~ SKYY already
# Phase 5-rejected). Each grouping intentionally tests a different orthogonal
# axis vs the existing universe.
# ---------------------------------------------------------------------------
CANDIDATE_POOL = [
    # --- Tech / innovation alternatives (test vs ARKK / SKYY / BOTZ / CIBR) ---
    "ROBO",    # Robotics
    "KOMP",    # Kensho New Economies (broad innovation)
    "WCLD",    # WisdomTree Cloud Computing
    "FINX",    # Global X Fintech
    "HACK",    # ETFMG Prime Cyber Security (alt to CIBR)
    "ARKW",    # ARK Next Generation Internet
    "ARKF",    # ARK Fintech Innovation
    "ARKQ",    # ARK Autonomous Tech & Robotics
    "ARKX",    # ARK Space Exploration
    # --- Health / bio (test vs XBI / ARKG) ---
    "IBB",     # iShares Biotechnology (Eileen asked for this)
    "IHI",     # iShares US Medical Devices
    "IDNA",    # iShares Genomics Immunology
    # --- Industrial / mobility (test vs PAVE / ITA) ---
    "KARS",    # KraneShares Electric Vehicles & Future Mobility
    "DRIV",    # Global X Autonomous & EV
    "PRNT",    # ARK 3D Printing
    # --- Energy / climate alternatives (test vs ICLN / TAN / LIT / URA) ---
    "QCLN",    # First Trust Clean Energy
    "HYDR",    # Global X Hydrogen (short history)
    "GRID",    # First Trust Smart Grid Infrastructure
    "KRBN",    # KraneShares Carbon Credits (short history)
    # --- Defence / aerospace (alt to ITA) ---
    "PPA",     # Invesco Aerospace & Defense
    # --- Niche themes (high-vol satellites) ---
    "ESPO",    # VanEck Video Gaming & Esports
    "AWAY",    # ETFMG Travel Tech
    "BETZ",    # Roundhill Sports Betting & iGaming
    # --- Crypto extension (alt to BTC) ---
    "ETHA",    # iShares Ethereum Trust (short history)
    "BITQ",    # Bitwise Crypto Industry Innovators
    # --- Phase 5 controls (expected to fail correlation gate) ---
    "PHO",     # Invesco Water Resources — Phase 5 reject
    "PBW",     # Invesco WilderHill Clean Energy
]

MA_PERIOD = 200
CORR_THRESHOLD = 0.85
WF_DEGRADE_TOLERANCE = 0.03   # tighter than A's 0.05; C is the smaller sleeve
COST_BPS = 5
COST_FRAC = COST_BPS / 10_000
REBAL_FREQ = "W-FRI"
SIGNAL_FLOOR = 0.05            # Strategy C +5% floor
DEPLOYED_K = 4
K_GRID = [3, 4, 5, 6, 7]
MIN_HISTORY_DAYS = 252 * 5     # 5y minimum for walk-forward


def _safe(v):
    if v is None: return None
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)): return None
    return float(v)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_existing_c_universe() -> list[str]:
    """Strategy C's deployed universe (signal-generating, excl. SHY cash floor)."""
    d = json.loads((DATA_DIR / "thematic_rotation.json").read_text(encoding="utf-8"))
    universe = [u["etf"] for u in d.get("universe", [])]
    return [t for t in universe if t != "SHY"]


def download_closes(tickers: list[str], start: str, end: str) -> pd.DataFrame:
    """Daily adjusted-close panel."""
    import yfinance as yf
    print(f"  Downloading {len(tickers)} tickers from {start} to {end} ...",
          flush=True)
    raw = yf.download(tickers, start=start, end=end, auto_adjust=True,
                       progress=False, threads=False,
                       group_by="ticker" if len(tickers) > 1 else None)
    out = {}
    if isinstance(raw.columns, pd.MultiIndex):
        for t in tickers:
            if t in raw.columns.get_level_values(0):
                col = raw[t]["Close"] if "Close" in raw[t].columns else None
                if col is not None and not col.dropna().empty:
                    out[t] = col
    else:
        if "Close" in raw.columns:
            out[tickers[0]] = raw["Close"]
    df = pd.DataFrame(out).sort_index()
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df


def compute_ma_signal(closes: pd.DataFrame) -> pd.DataFrame:
    """Distance above own 200d MA per ETF — matches Strategy C's signal.

    NOTE: must compute the rolling MA per ticker on that ticker's own
    clean trading calendar, then reindex back to the union calendar.
    The naive ``closes.rolling(MA_PERIOD).mean()`` runs against the
    union index of all tickers in the panel, which is BROKEN when the
    panel mixes calendars (e.g. US-listed IBB and Shenzhen-listed
    159801.SZ). On those joined dates, every column carries ~1 NaN
    per week from the foreign calendar, and the rolling window never
    accumulates 200 consecutive non-NaN values for ANY ticker — the
    entire signal collapses to NaN. Per-ticker computation avoids
    that cross-contamination."""
    out = {}
    for col in closes.columns:
        s = closes[col].dropna()
        if len(s) < MA_PERIOD:
            out[col] = pd.Series(index=closes.index, dtype=float)
            continue
        ma = s.rolling(MA_PERIOD, min_periods=MA_PERIOD).mean()
        sig = (s - ma) / ma
        out[col] = sig.reindex(closes.index)
    return pd.DataFrame(out, index=closes.index)


# ---------------------------------------------------------------------------
# Stage 1 — correlation gate
# ---------------------------------------------------------------------------

def correlation_gate(signal: pd.DataFrame, candidates: list[str],
                      existing: list[str]) -> list[dict]:
    """For each candidate, compute weekly Friday signal correlation vs each
    existing C ETF on the PER-PAIR overlap window (not a forced
    universe-wide intersection — that gets wiped by short-history members
    like 159801.SZ). Pass if max corr < 0.85."""
    weekly = signal.resample("W-FRI").last()
    verdicts = []
    for cand in candidates:
        if cand not in weekly.columns:
            verdicts.append({
                "candidate": cand, "passed": False,
                "reason": "no yfinance data",
                "max_corr": None, "max_corr_with": None, "top_5": [],
            })
            continue
        cand_series = weekly[cand].dropna()
        if len(cand_series) < 52:
            verdicts.append({
                "candidate": cand, "passed": False,
                "reason": f"insufficient history ({len(cand_series)} weekly obs)",
                "max_corr": None, "max_corr_with": None, "top_5": [],
            })
            continue
        # Per-pair correlation on each pair's own overlap window
        corrs = {}
        for ex in existing:
            if ex not in weekly.columns: continue
            ex_series = weekly[ex].dropna()
            common = cand_series.index.intersection(ex_series.index)
            if len(common) < 26: continue   # < 6 months overlap = unreliable
            corrs[ex] = (float(cand_series.loc[common].corr(ex_series.loc[common])),
                          len(common))
        if not corrs:
            verdicts.append({
                "candidate": cand, "passed": False,
                "reason": "no valid correlation pairs",
                "max_corr": None, "max_corr_with": None, "top_5": [],
            })
            continue
        sorted_corrs = sorted(corrs.items(), key=lambda x: -x[1][0])
        max_corr = sorted_corrs[0][1][0]
        max_corr_with = sorted_corrs[0][0]
        passed = max_corr < CORR_THRESHOLD
        verdicts.append({
            "candidate": cand,
            "passed": passed,
            "reason": ("passes corr gate" if passed
                       else f"too correlated with {max_corr_with} ({max_corr:+.2f})"),
            "max_corr": _safe(max_corr),
            "max_corr_with": max_corr_with,
            "top_5": [{"ticker": t, "corr": _safe(c[0]), "n_obs": c[1]}
                       for t, c in sorted_corrs[:5]],
            "history_weekly_obs": len(cand_series),
        })
    return verdicts


# ---------------------------------------------------------------------------
# Stage 2 — empirical (walk-forward) gate, runs only for stage-1 survivors
# ---------------------------------------------------------------------------

def top_k_with_floor(K: int, floor: float = SIGNAL_FLOOR):
    """Strategy C weight function: rank by signal, drop anything below
    +floor%, take top K, equal-weight."""
    def f(s_row: pd.Series) -> pd.Series:
        valid = s_row.dropna()
        eligible = valid[valid >= floor]
        if len(eligible) == 0:
            return pd.Series(0.0, index=s_row.index)
        top = eligible.nlargest(min(K, len(eligible)))
        w = pd.Series(0.0, index=s_row.index)
        equal = 1.0 / K   # match deployed: equal-weight 1/K, slots <K go cash
        w.loc[top.index] = equal
        return w
    return f


def run_c_backtest(closes: pd.DataFrame, signal: pd.DataFrame,
                    K: int, eligible_start: pd.Timestamp) -> dict:
    """Strategy C top-K with +5% floor, equal-weight, weekly Friday."""
    weight_fn = top_k_with_floor(K)
    target = pd.date_range(eligible_start, closes.index[-1], freq=REBAL_FREQ)
    rebal_dates = closes.index[closes.index.isin(target)]
    rb = pd.DataFrame(index=rebal_dates, columns=closes.columns, dtype=float)
    for rd in rebal_dates:
        prev_idx = closes.index.get_loc(rd) - 1
        if prev_idx < 0: continue
        s_row = signal.iloc[prev_idx]
        rb.loc[rd] = weight_fn(s_row).reindex(closes.columns).fillna(0.0)
    weights = rb.reindex(closes.index, method="ffill").fillna(0.0)
    weights.loc[weights.index < eligible_start] = 0.0
    rets = closes.pct_change().fillna(0)
    port_ret = (weights.shift(1).fillna(0) * rets).sum(axis=1)
    turnover = weights.diff().abs().sum(axis=1).fillna(0)
    port_ret = port_ret - turnover * COST_FRAC
    equity = (1.0 + port_ret).cumprod()
    return {"equity": equity, "weights": weights, "turnover": turnover}


def walk_forward_K_c(closes: pd.DataFrame, signal: pd.DataFrame,
                      eligible_start: pd.Timestamp,
                      train_years: int = 5) -> dict:
    initial_train_end = eligible_start + pd.DateOffset(years=train_years)
    last_date = closes.index[-1]
    if initial_train_end >= last_date - pd.DateOffset(years=1):
        return {"walk_forward_sharpe": None, "segments": [],
                "note": "insufficient history"}
    refit_target = pd.date_range(initial_train_end, last_date, freq="YE")
    refit_ends = [closes.index[closes.index.searchsorted(r, side="right") - 1]
                   for r in refit_target]
    refit_ends = [r for r in refit_ends if r >= eligible_start]

    segments = []
    test_eq_pieces = []
    for i, train_end in enumerate(refit_ends):
        train_end_idx = closes.index.get_loc(train_end)
        test_end = refit_ends[i + 1] if i + 1 < len(refit_ends) else last_date
        test_start_idx = train_end_idx + 1
        if test_start_idx >= len(closes): break
        test_start = closes.index[test_start_idx]
        best_K, best_sh = None, -1e9
        for K in K_GRID:
            r = run_c_backtest(closes, signal, K, eligible_start)
            eq = r["equity"].loc[(r["equity"].index >= eligible_start)
                                  & (r["equity"].index <= train_end)]
            if len(eq) < 5: continue
            eq = eq / eq.iloc[0]
            d = eq.pct_change().fillna(0)
            sh = float(d.mean() / d.std() * math.sqrt(252)) if d.std() > 0 else 0
            if sh > best_sh: best_sh, best_K = sh, K
        if best_K is None: continue
        r = run_c_backtest(closes, signal, best_K, eligible_start)
        test_eq = r["equity"].loc[test_start:test_end]
        if len(test_eq) < 2: continue
        d = test_eq.pct_change().fillna(0)
        test_sh = float(d.mean() / d.std() * math.sqrt(252)) if d.std() > 0 else 0
        segments.append({
            "train_end": train_end.strftime("%Y-%m-%d"),
            "test_start": test_start.strftime("%Y-%m-%d"),
            "test_end": test_end.strftime("%Y-%m-%d"),
            "best_K": best_K,
            "train_sharpe": round(best_sh, 3),
            "test_sharpe": round(test_sh, 3),
        })
        last_val = test_eq_pieces[-1].iloc[-1] if test_eq_pieces else 1.0
        test_eq_pieces.append(test_eq * last_val / test_eq.iloc[0])
    if not test_eq_pieces:
        return {"walk_forward_sharpe": None, "segments": []}
    wf_eq = pd.concat(test_eq_pieces)
    d = wf_eq.pct_change().fillna(0)
    wf_sh = float(d.mean() / d.std() * math.sqrt(252)) if d.std() > 0 else 0
    return {"walk_forward_sharpe": round(wf_sh, 3), "segments": segments}


def empirical_gate(closes: pd.DataFrame, signal: pd.DataFrame,
                    existing: list[str], baseline_wf: float,
                    survivors: list[str]) -> list[dict]:
    """For each Stage-1 survivor: add to universe, re-run walk-forward,
    measure WF Sharpe delta vs baseline."""
    verdicts = []
    MIN_UNIVERSE_AT_START = 10
    for cand in survivors:
        proposed = existing + [cand]
        # Build aligned slice for this proposed universe
        cols = [c for c in proposed if c in closes.columns]
        sub_closes = closes[cols].dropna(how="all")
        sub_signal = signal[cols].reindex(sub_closes.index)
        # Same eligible_start rule as the baseline: start when the
        # universe is thick enough (>= MIN_UNIVERSE_AT_START themes
        # carry a valid signal). Anchoring on the latest-launching
        # theme would push the start to 2021 (159801.SZ) and leave
        # insufficient history for a 5y train + 1y test split.
        n_valid_per_date = sub_signal.notna().sum(axis=1)
        thick_enough = n_valid_per_date[n_valid_per_date >= MIN_UNIVERSE_AT_START]
        if thick_enough.empty:
            verdicts.append({"candidate": cand, "passed": False,
                              "reason": "universe never thick enough",
                              "wf_delta": None})
            continue
        eligible_start = thick_enough.index[0]
        # Need at least 5y train + 1y test from this eligible_start
        if (sub_closes.index[-1] - eligible_start).days < 365 * 6:
            verdicts.append({"candidate": cand, "passed": False,
                              "reason": "insufficient history for walk-forward",
                              "wf_delta": None,
                              "eligible_start": eligible_start.strftime("%Y-%m-%d")})
            continue
        wf = walk_forward_K_c(sub_closes, sub_signal, eligible_start)
        wf_sh = wf.get("walk_forward_sharpe")
        if wf_sh is None:
            verdicts.append({"candidate": cand, "passed": False,
                              "reason": wf.get("note", "wf failed"),
                              "wf_delta": None})
            continue
        delta = wf_sh - baseline_wf
        passed = delta > -WF_DEGRADE_TOLERANCE
        verdicts.append({
            "candidate": cand,
            "passed": passed,
            "wf_baseline": baseline_wf,
            "wf_with_candidate": wf_sh,
            "wf_delta": delta,
            "eligible_start": eligible_start.strftime("%Y-%m-%d"),
            "reason": (f"WF Sharpe {delta:+.3f} vs baseline ({wf_sh:+.2f} vs {baseline_wf:+.2f})"),
        })
    return verdicts


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("=" * 72)
    print("STRATEGY C UNIVERSE SCREENING — bulk gate test")
    print("=" * 72)

    existing = load_existing_c_universe()
    print(f"\nExisting Strategy C universe: {len(existing)} themes")
    print(f"  {existing}")

    candidates = [c for c in CANDIDATE_POOL if c not in existing]
    print(f"\nCandidate pool (excluding existing): {len(candidates)} ETFs")
    print(f"  {candidates}")

    # Download price history for the union
    union = sorted(set(existing) | set(candidates))
    end = pd.Timestamp.today().normalize().strftime("%Y-%m-%d")
    start = "2015-01-01"  # leave room for 200d MA burn-in + 5y train
    closes = download_closes(union, start, end)
    print(f"  Loaded {len(closes.columns)} of {len(union)} tickers "
          f"(date range {closes.index[0].date()} -> {closes.index[-1].date()})")
    missing = [t for t in union if t not in closes.columns]
    if missing:
        print(f"  Missing (no yfinance data): {missing}")

    # Compute signal panel
    signal = compute_ma_signal(closes)

    # --- Stage 1: correlation gate -------------------------------------
    print(f"\n{'=' * 72}\nSTAGE 1: WITHIN-STRATEGY CORRELATION GATE\n{'=' * 72}")
    available_candidates = [c for c in candidates if c in signal.columns]
    available_existing = [c for c in existing if c in signal.columns]
    corr_verdicts = correlation_gate(signal, available_candidates, available_existing)

    survivors = []
    fails = []
    for v in corr_verdicts:
        cand = v["candidate"]
        if v["passed"]:
            survivors.append(cand)
            print(f"  PASS  {cand:<8} max corr {v['max_corr']:+.2f} "
                  f"({v['max_corr_with']})  n={v.get('n_obs', 0)}")
        else:
            fails.append(cand)
            print(f"  FAIL  {cand:<8} {v['reason']}")

    print(f"\nStage 1 survivors: {len(survivors)} / {len(corr_verdicts)}  "
          f"({survivors})")

    # --- Stage 2: empirical walk-forward gate --------------------------
    print(f"\n{'=' * 72}\nSTAGE 2: EMPIRICAL WALK-FORWARD GATE\n{'=' * 72}")

    # Baseline walk-forward: existing universe only.
    # Do NOT anchor eligible_start on the latest-launching theme — that
    # pushes the start to 2021 because of 159801.SZ (Aug 2019 launch +
    # 200d MA burn-in), which leaves less than 5y train + 1y test in
    # the available history. Instead, start when the universe is
    # "thick enough" — at least MIN_UNIVERSE_AT_START themes carry a
    # valid signal. Late-launching themes simply do not appear in
    # early picks, which is exactly how the deployed engine handles
    # them in production.
    MIN_UNIVERSE_AT_START = 10
    bl_closes = closes[available_existing].dropna(how="all")
    bl_signal = signal[available_existing].reindex(bl_closes.index)
    n_valid_per_date = bl_signal.notna().sum(axis=1)
    thick_enough = n_valid_per_date[n_valid_per_date >= MIN_UNIVERSE_AT_START]
    if thick_enough.empty:
        print(f"  ERROR: universe never has {MIN_UNIVERSE_AT_START} valid signals at once")
        return 2
    bl_eligible = thick_enough.index[0]
    print(f"  Baseline eligible_start: {bl_eligible.date()}")
    print(f"  Computing baseline walk-forward (existing {len(available_existing)} themes)...")
    bl_wf = walk_forward_K_c(bl_closes, bl_signal, bl_eligible)
    baseline_wf = bl_wf.get("walk_forward_sharpe")
    print(f"  Baseline WF Sharpe: {baseline_wf:+.3f}")

    empirical_verdicts = []
    if survivors:
        print(f"\n  Testing {len(survivors)} stage-1 survivor(s) empirically ...")
        empirical_verdicts = empirical_gate(closes, signal, available_existing,
                                              baseline_wf, survivors)
        for v in empirical_verdicts:
            cand = v["candidate"]
            if v["passed"]:
                print(f"  PASS  {cand:<8} {v['reason']}")
            else:
                print(f"  FAIL  {cand:<8} {v['reason']}")
    else:
        print("  (no stage-1 survivors to test)")

    # --- Final shortlist ------------------------------------------------
    print(f"\n{'=' * 72}\nFINAL SHORTLIST — CANDIDATES PASSING BOTH GATES\n{'=' * 72}")
    final_passers = [v for v in empirical_verdicts if v["passed"]]
    if final_passers:
        for v in sorted(final_passers, key=lambda x: -(x.get("wf_delta") or 0)):
            print(f"  {v['candidate']:<8} WF lift {v['wf_delta']:+.3f}  "
                  f"(baseline {v['wf_baseline']:+.2f} -> {v['wf_with_candidate']:+.2f})")
    else:
        print("  None.")
        print("  Empirical evidence continues to say: more themes don't help C.")

    # --- Persist results -----------------------------------------------
    out = {
        "computed_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "existing_universe": existing,
        "candidate_pool": candidates,
        "missing_from_yfinance": missing,
        "thresholds": {
            "correlation": CORR_THRESHOLD,
            "wf_sharpe_degrade_tolerance": WF_DEGRADE_TOLERANCE,
        },
        "baseline_wf_sharpe": _safe(baseline_wf),
        "stage_1_correlation": corr_verdicts,
        "stage_2_empirical": empirical_verdicts,
        "final_shortlist": [v["candidate"] for v in final_passers],
    }
    out_path = DATA_DIR / "thematic_universe_screen.json"
    out_path.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(f"\nWrote {out_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
