"""PROPOSAL ARTEFACT — Stage 1/2 publisher for the Norgate breadth feed.
NOT WIRED into any pipeline, workflow, or scheduled task. Runs only when
invoked manually (Stage 0 preview) or by the Stage-1 Task Scheduler job
once approved. See reviews/2026-07-17_norgate-feed-migration.md.

What it does per run:
  1. Pulls #SPX%MA50 from the local NDU (full history, padding NONE).
  2. Freshness: reads the actual last BAR date — never last_quoted_date,
     which NDU leaves unset on market-closed days (event-studies
     feed-gate lesson, 2026-07-04). If the last bar is older than
     STALE_TRADING_DAYS trading days, exits WITHOUT writing (the
     downstream staleness cap and scrape fallback then govern).
  3. Runs the DEPLOYED hysteresis (_compute_states imported from
     run_risk_overlay, OFF 0.20 / ON 0.50) over the FULL vendor history.
  4. Writes DERIVED GATE STATES ONLY (licence: vendor series values never
     enter the committed repo):
       Stage 0/1 -> data_local/gate_states_norgate.preview.json
       Stage 2   -> data/gate_states_norgate.json   (flag --commit-path;
                    only after approval #2)
  5. Stage-1 divergence check: compares its current state against the
     scrape-fed state in data/risk_overlay.json and prints/flags any
     mismatch or a level residing in the 0.20/0.50 threshold zone.

Run:    python scripts/publish_norgate_breadth.py [--commit-path]
"""
from __future__ import annotations

import argparse
import datetime as dt  # Python datetime: months are 1-indexed
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATA_LOCAL = ROOT / "data_local"
sys.path.insert(0, str(ROOT / "scripts"))

from run_risk_overlay import (_compute_states, OFF_THRESHOLD,  # noqa: E402
                              ON_THRESHOLD)

SYMBOL = "#SPX%MA50"
STALE_TRADING_DAYS = 3


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit-path", action="store_true",
                    help="write to data/ (Stage 2, post-approval only)")
    args = ap.parse_args()

    import norgatedata as nd
    if not nd.status():
        print("NDU not running — exiting without writing (fallback governs)")
        return 1
    df = nd.price_timeseries(
        SYMBOL,
        stock_price_adjustment_setting=nd.StockPriceAdjustmentType.TOTALRETURN,
        padding_setting=nd.PaddingType.NONE,
        timeseriesformat="pandas-dataframe",
    )
    close = df["Close"]
    scale = 100.0 if close.max() > 1.5 else 1.0
    breadth = close / scale

    # freshness on the ACTUAL last bar (never last_quoted_date)
    last_bar = breadth.index[-1].date()
    today = dt.date.today()
    # trading-day distance approximated by calendar days net of weekends;
    # holidays intentionally lenient (a holiday gap must not false-alarm)
    cal_gap = (today - last_bar).days
    if cal_gap > STALE_TRADING_DAYS + 4:  # 3 trading days + weekend slack
        print(f"stale: last bar {last_bar}, {cal_gap} calendar days old — "
              "exiting without writing")
        return 1

    states = _compute_states(breadth, OFF_THRESHOLD, ON_THRESHOLD)
    out = {
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(
            timespec="seconds"),
        "source": "norgate-local #SPX%MA50 (derived states only; raw "
                  "series never committed — licence)",
        "state_machine": "run_risk_overlay._compute_states "
                         f"(off {OFF_THRESHOLD}, on {ON_THRESHOLD})",
        "last_bar": str(last_bar),
        "current_state": "RISK_ON" if states.iloc[-1] == 1.0 else "DERISK",
        "series": {
            "dates": [str(d.date()) for d in states.index],
            "state": [int(s) for s in states.values],
        },
    }
    if args.commit_path:
        path = DATA / "gate_states_norgate.json"
    else:
        DATA_LOCAL.mkdir(exist_ok=True)
        path = DATA_LOCAL / "gate_states_norgate.preview.json"
    path.write_text(json.dumps(out, indent=1), encoding="utf-8")

    # Stage-1 divergence check vs the deployed scrape-fed state
    try:
        ro = json.loads((DATA / "risk_overlay.json").read_text(
            encoding="utf-8"))
        dep_state = str(ro.get("current_state", "")).upper()
        cand_state = out["current_state"]
        zone = (abs(breadth.iloc[-1] - OFF_THRESHOLD) < 0.02
                or abs(breadth.iloc[-1] - ON_THRESHOLD) < 0.02)
        flag = (dep_state and cand_state not in dep_state) or zone
        print(f"divergence check: deployed={dep_state or 'n/a'} "
              f"norgate={cand_state} zone={zone} -> "
              f"{'FLAG' if flag else 'ok'}")
    except Exception as exc:
        print(f"divergence check skipped ({type(exc).__name__})")

    print(f"wrote {path.relative_to(ROOT)} (last bar {last_bar}, "
          f"state {out['current_state']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
