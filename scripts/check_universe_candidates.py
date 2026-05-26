"""Generic within-strategy correlation + history gate for new ETF candidates.

For each candidate, compute the max pairwise weekly-return correlation
against every current member of the target strategy's universe. Pass if
max-corr < 0.85 AND >=5 years of overlapping history. Defers when corr
passes but history is short. Fails otherwise.

Usage:
    python scripts/check_universe_candidates.py --strategy B SLV CPER
    python scripts/check_universe_candidates.py --strategy C XME PICK WOOD REMX

The --strategy flag selects which incumbent universe to gate against:
    B   = run_asset_class_rotation.UNIVERSE  (asset-class momentum)
    C   = run_thematic_rotation.UNIVERSE     (thematic momentum)
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from run_asset_class_rotation import UNIVERSE as B_UNIVERSE  # noqa: E402
from run_thematic_rotation import UNIVERSE as C_UNIVERSE  # noqa: E402

GATE_MAX_CORR = 0.85
MIN_YEARS_HISTORY = 5
DEFAULT_START = "2018-01-01"
DEFAULT_END = date.today().isoformat()


def fetch_weekly_close(tickers: list[str], start: str, end: str) -> pd.DataFrame:
    raw = yf.download(tickers, start=start, end=end, auto_adjust=True,
                       progress=False, group_by="ticker", threads=True)
    out = pd.DataFrame()
    for t in tickers:
        try:
            out[t] = raw[t]["Close"]
        except Exception:
            print(f"  WARN: no data for {t}")
    weekly = out.resample("W-FRI").last()
    return weekly.dropna(how="all")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", required=True, choices=["B", "C"])
    parser.add_argument("candidates", nargs="+")
    args = parser.parse_args()

    incumbent_universe = B_UNIVERSE if args.strategy == "B" else C_UNIVERSE
    incumbents = list(incumbent_universe.keys())
    universe = incumbents + args.candidates

    print(f"Strategy {args.strategy} gate: candidates {args.candidates} vs "
          f"{len(incumbents)} incumbents")
    print(f"Fetching weekly close for {len(universe)} tickers "
          f"({DEFAULT_START} → {DEFAULT_END}) ...")
    weekly = fetch_weekly_close(universe, DEFAULT_START, DEFAULT_END)
    rets = weekly.pct_change().dropna(how="all")

    print(f"\nHistory range per candidate:")
    for c in args.candidates:
        if c not in rets.columns:
            print(f"  {c:6s}  NO DATA")
            continue
        valid = rets[c].dropna()
        first = valid.index.min().date() if len(valid) else None
        last = valid.index.max().date() if len(valid) else None
        years = (last - first).days / 365.25 if first else 0
        print(f"  {c:6s}  {first} → {last}   {years:.1f} years   "
              f"{len(valid)} weekly obs")

    print(f"\nWithin-strategy correlation gate (cap = {GATE_MAX_CORR}, "
          f"min history = {MIN_YEARS_HISTORY}y):\n")
    verdicts = []
    for c in args.candidates:
        if c not in rets.columns:
            verdicts.append((c, "FAIL", "no yfinance data"))
            print(f"  {c}: FAIL (no data)")
            continue
        valid = rets[c].dropna()
        years = (valid.index.max() - valid.index.min()).days / 365.25 if len(valid) else 0
        corrs = []
        for inc in incumbents:
            if inc not in rets.columns:
                continue
            paired = pd.concat([rets[c], rets[inc]], axis=1).dropna()
            if len(paired) < 52:
                continue
            corr = paired.corr().iloc[0, 1]
            corrs.append((inc, corr))
        corrs.sort(key=lambda x: -x[1])
        max_corr_inc, max_corr = corrs[0] if corrs else ("—", float("nan"))
        passes_corr = max_corr < GATE_MAX_CORR
        passes_history = years >= MIN_YEARS_HISTORY
        if passes_corr and passes_history:
            verdict = "PASS"
            reason = "ok"
        elif passes_corr and not passes_history:
            verdict = "DEFER"
            reason = f"history only {years:.1f}y < {MIN_YEARS_HISTORY}y"
        else:
            verdict = "FAIL"
            reason = (f"max-corr {max_corr:.2f} vs {max_corr_inc} "
                      f"≥ {GATE_MAX_CORR}")
        verdicts.append((c, verdict, reason))
        print(f"  {c}: {verdict}  ({reason})")
        print(f"    top-5 correlations vs incumbents:")
        for inc, corr in corrs[:5]:
            print(f"      {inc:6s}  {corr:+.3f}")

    print("\nSummary:")
    for c, v, r in verdicts:
        print(f"  {c:6s}  {v:6s}  {r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
