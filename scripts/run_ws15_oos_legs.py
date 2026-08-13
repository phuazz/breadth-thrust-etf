"""WS15 step 3 — the published CNDX OOS backtest, re-run leg by leg.

The published data/backtest_cndx_oos.json (computed 2026-05-17, inlined into
the live dashboard) rests on three stacked vintages: the code as of that day,
the breadth panel as of that day (87 signal-fire days, survivor prices), and
that day's OHLC pull. This script separates what each is worth:

    T1  May code    x May breadth (87 sig)   -> must reproduce the published
                                                table; proves the basis
    T2  today code  x May breadth            -> code evolution since May
    T3  today code  x Aug survivor (36 sig)  -> data refresh: the August
                                                roster rebuild + vendor
                                                re-basing + 3 more months
    T4  today code  x Aug corrected (43 sig) -> SURVIVORSHIP (the headline:
                                                same window, roster, config
                                                and code as T3; only the
                                                Norgate fill differs)
    T5  today code  x WS15 residual-fixed    -> the reuse-masked residual
                    (42 sig)                    (FB, FOXA/FOX, PCLN, EA,
                                                MNST, warmup)

Every leg trades the SAME freshly-pulled QQQ/SPY basis, held in the WS15
workdir — the live data/ caches are neither read past their guards nor
written. A dividend that went ex after a leg's window rescales that whole
window by one constant, which cancels in returns, stops and Sharpe; the T1
comparison against the published file measures whatever vendor revision
remains rather than assuming it away. Both code vintages seed their Monte
Carlo with the same MC_SEED, so MC percentiles are comparable too.

Run:
    python scripts/run_ws15_oos_legs.py --workdir <dir>
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

from run_etf_oos import _summarise  # noqa: E402  (pure dict shaping)


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _configs(bt) -> dict[str, dict]:
    """The three published variants, built on THIS module's DEFAULT_CONFIG."""
    return {
        "baseline_2xATR": {**bt.DEFAULT_CONFIG},
        "regime_time_only": {**bt.DEFAULT_CONFIG, "trailing_stop_k": None},
        "regime_time_only_delay5_trend": {
            **bt.DEFAULT_CONFIG,
            "trailing_stop_k": None,
            "entry_delay_bars": 5,
            "use_trend_filter": True,
        },
    }


def run_leg(bt, breadth_json: Path, qqq_cache: Path, spy_cache: Path) -> dict:
    """Replicates run_etf_oos.main()'s computation for one leg."""
    bt.paths_for = lambda etf: {"breadth": breadth_json,
                                "ohlc_cache": qqq_cache,
                                "out": Path("unused.json")}
    bt.SPY_CACHE = spy_cache

    breadth, signal_records = bt.load_breadth(etf="CNDX")
    signal_dates = [s["date"] for s in signal_records]
    dl_start = (breadth.index[0] - pd.Timedelta(days=60)).strftime("%Y-%m-%d")
    dl_end = (breadth.index[-1] + pd.Timedelta(days=5)).strftime("%Y-%m-%d")
    ohlc = bt.download_soxx_ohlc(dl_start, dl_end, etf="CNDX", yf_symbol="QQQ")
    spy_close = bt.download_spy_close(dl_start, dl_end)
    ohlc = ohlc[~ohlc.index.duplicated(keep="first")]
    eligible_start = (breadth.index[252] if len(breadth) > 252
                      else breadth.index[0])

    rows = []
    for label, conf in _configs(bt).items():
        trades = bt.run_strategy(signal_dates, ohlc, breadth, config=conf)
        daily_returns = bt.build_daily_returns(trades, ohlc)
        stats = bt.aggregate_stats(trades, daily_returns)
        mc = bt.monte_carlo_null(trades, ohlc, eligible_start)
        rows.append(_summarise(label, stats, mc))
    return {
        "breadth_file": breadth_json.name,
        "n_signal_fire_days": len(signal_dates),
        "window": [str(breadth.index[0].date()), str(breadth.index[-1].date())],
        "summary_table": rows,
    }


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workdir", required=True)
    args = ap.parse_args()
    W = Path(args.workdir)

    qqq_cache = W / "qqq_ohlc_cache_ws15.parquet"
    spy_cache = W / "spy_close_cache_ws15.parquet"

    bt_now = _load_module("backtest_now", SCRIPTS / "backtest.py")
    bt_may = _load_module("backtest_may", W / "backtest_may.py")

    legs = {
        "T1_published_repro": (bt_may, W / "breadth_may_survivor.json"),
        "T2_code_today":      (bt_now, W / "breadth_may_survivor.json"),
        "T3_aug_survivor":    (bt_now, W / "breadth_aug_survivor.json"),
        "T4_aug_corrected":   (bt_now, ROOT / "data" / "breadth_cndx.json"),
        "T5_ws15_residual":   (bt_now, W / "breadth_ws15.json"),
    }
    results = {}
    for name, (bt, breadth_json) in legs.items():
        print(f"[{name}] {breadth_json.name}", flush=True)
        results[name] = run_leg(bt, breadth_json, qqq_cache, spy_cache)

    published = json.loads(
        (ROOT / "data" / "backtest_cndx_oos.json").read_text(encoding="utf-8"))
    pub_rows = {r["variant"]: r for r in published["summary_table"]}

    # ---- T1 vs published: field-by-field reproduction check -------------
    repro = {}
    for row in results["T1_published_repro"]["summary_table"]:
        v = row["variant"]
        diffs = {}
        for k, new in row.items():
            if k == "variant":
                continue
            old = pub_rows[v].get(k)
            if old is None and new is None:
                continue
            if old is None or new is None or (isinstance(old, (int, float))
                                              and isinstance(new, (int, float))):
                same = (old == new if not isinstance(old, float)
                        else abs((new or 0) - (old or 0)) <= 1e-9 + 1e-6 * abs(old))
                if not same:
                    diffs[k] = {"published": old, "reproduced": new}
        repro[v] = diffs or "EXACT"

    out = {
        "computed_at_utc": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "published_computed_at": published["computed_at_utc"],
        "published_n_signal_fire_days": published["n_signal_fire_days"],
        "reproduction_check": repro,
        "legs": results,
    }
    (W / "ws15_oos_legs.json").write_text(json.dumps(out, indent=2),
                                          encoding="utf-8")

    print("\n=== T1 reproduction vs published ===")
    for v, d in repro.items():
        print(f"  {v}: {'EXACT' if d == 'EXACT' else d}")
    print("\n=== Legs (regime_time_only_delay5_trend, the dashboard's "
          "headline variant) ===")
    hdr = f"{'leg':22s}{'sig':>5s}{'n':>4s}{'win%':>7s}{'totRet%':>9s}{'Sharpe':>8s}{'MC%':>7s}"
    print(hdr)
    for name, r in results.items():
        row = next(x for x in r["summary_table"]
                   if x["variant"] == "regime_time_only_delay5_trend")
        print(f"{name:22s}{r['n_signal_fire_days']:>5d}{row['n_trades'] or 0:>4d}"
              f"{(row['win_rate'] or 0)*100:>7.1f}"
              f"{(row['equity_curve_total_return'] or 0)*100:>+9.1f}"
              f"{(row['sharpe_annualised'] or 0):>+8.2f}"
              f"{(row['mc_strategy_total_return_percentile'] or 0):>7.1f}")
    print(f"\nWrote {W / 'ws15_oos_legs.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
