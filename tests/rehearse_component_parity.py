"""Read-only historical parity against an explicit pre-change Git revision.

Uses existing constituent and OHLC caches only. No downloads, output data edits,
strategy changes or simulated new prices. Python months are 1-indexed.
"""
import argparse
import ast
from pathlib import Path
import subprocess
import sys
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import run_portfolio as current
import run_europe_rotation as europe
from etf_registry import UNIVERSE_EUROPE_SECTORS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True)
    args = parser.parse_args()
    source = subprocess.run(["git", "show", f"{args.baseline}:scripts/run_portfolio.py"],
                            cwd=ROOT, capture_output=True, text=True, check=True).stdout
    def cached(*args, etf, **kwargs):
        return pd.read_parquet(ROOT / "data" / f"{etf.lower()}_ohlc_cache.parquet")
    current.download_soxx_ohlc = cached
    original = dict(vars(current))
    functions = [n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef)
                 and n.name in {"_build_panels_for", "run_portfolio"}]
    exec(compile(ast.Module(body=functions, type_ignores=[]), "git-baseline", "exec"), original)
    old_closes, old_signals, old_used = original["_build_panels_for"](UNIVERSE_EUROPE_SECTORS)
    new_closes, new_signals, new_used = current._build_panels_for(UNIVERSE_EUROPE_SECTORS)
    assert old_used == new_used
    pd.testing.assert_frame_equal(old_closes, new_closes, check_exact=True)
    bound = min(pd.Timestamp(new_signals.attrs["validated_through"]), new_closes.index[-1])
    pd.testing.assert_frame_equal(old_signals.loc[:bound], new_signals.loc[:bound], check_exact=True)
    # Compare through the validated source boundary, not an unverified tail.
    closes = new_closes.loc[:bound]
    old_signals = old_signals.loc[:bound]
    new_signals = new_signals.loc[:bound]
    eligible = max(old_signals[e].dropna().index.min() for e in old_used) + pd.Timedelta(days=europe.MA_PERIOD)
    eligible = closes.index[closes.index >= eligible][0]
    old = original["run_portfolio"](closes, old_signals, current.top_k_breadth_weight(europe.HEADLINE_K),
                                    eligible, cost=europe.COST_FRAC, rebalance_freq=europe.HEADLINE_FREQ, calendar="XETR")
    new = current.run_portfolio(closes, new_signals, current.top_k_breadth_weight(europe.HEADLINE_K),
                               eligible, cost=europe.COST_FRAC, rebalance_freq=europe.HEADLINE_FREQ, calendar="XETR")
    pd.testing.assert_frame_equal(old["weights"], new["weights"], check_exact=True)
    pd.testing.assert_series_equal(old["equity"], new["equity"], check_exact=True)
    print(f"EXACT PARITY: {len(closes)} sessions, {len(new_used)} European panels; "
          f"{closes.index[0].date()} to {closes.index[-1].date()}; "
          "validated signals, weights and EUR-panel equity unchanged with registered K/cost/cadence. "
          "The USD FX layer is unchanged and not recomputed. No download or production write.")


if __name__ == "__main__":
    main()
