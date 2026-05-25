"""Live SOXX signal — emit today's recommended allocation under the 50/150 strategy.

Reads the latest backtest output (data/backtest_soxx_oos.json) and the
breadth panel (data/breadth_soxx.json), determines whether a thrust
trade is currently open, and writes data/live_signal.json with:

  - latest_data_date     : the date of the most recent equity-curve point
  - state                : "IN-TRADE (allocation 150%)" or "OUT (allocation 50%)"
  - current_allocation   : 0.50 or 1.50
  - current_trade        : the active trade record if any
  - composite_z_today    : breadth strength at latest date
  - days_since_state_change : how long the current state has held
  - recent_signal_fires  : last 5 signal-fire days for context
  - last_completed_trade : the most recent closed trade

This is a NOTIFICATION script — it does NOT refresh data. To get a
genuinely live read, first re-run:
    python scripts/fetch_constituents.py --etf SOXX
    python scripts/compute_breadth.py --etf SOXX
    python scripts/run_etf_oos.py --etf SOXX
then run this script. (The latter pair are cheap if price/constituent
caches are warm; the first is bottlenecked on iShares throttling.)

Run:
    python scripts/live_signal.py
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
OUT_PATH = DATA_DIR / "live_signal.json"

ETF = "SOXX"
BASE_ALLOC = 0.50
THRUST_ALLOC = 1.50


def _safe(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(f) or math.isinf(f)) else f


def _current_trade_at(trades: list[dict], latest_date: pd.Timestamp) -> dict | None:
    """Return the trade that is open as of the latest close, if any.

    Exits execute AT the close of exit_date — a notification generated
    after that close should report the next-session allocation as
    OUT/base. So we test `entry <= latest_date < exit_` (strict <),
    not the previous `<=` which double-counted the exit day.
    """
    for t in trades:
        entry = pd.Timestamp(t["entry_date"])
        exit_ = pd.Timestamp(t["exit_date"])
        if entry <= latest_date < exit_:
            return t
    return None


def main() -> int:
    bt_path = DATA_DIR / f"backtest_{ETF.lower()}_oos.json"
    br_path = DATA_DIR / f"breadth_{ETF.lower()}.json"
    if not bt_path.exists():
        print(f"ERROR: missing {bt_path}", file=sys.stderr)
        return 1
    bt = json.loads(bt_path.read_text(encoding="utf-8"))
    br = json.loads(br_path.read_text(encoding="utf-8")) if br_path.exists() else None

    triple = bt.get("variants", {}).get("regime_time_only_delay5_trend")
    if not triple:
        print("ERROR: triple-combo variant not in backtest file", file=sys.stderr)
        return 1
    trades = triple.get("trades", [])
    eq = triple.get("equity_curve", {})
    if not eq.get("dates"):
        print("ERROR: equity curve missing", file=sys.stderr)
        return 1

    latest_date = pd.Timestamp(eq["dates"][-1])

    current_trade = _current_trade_at(trades, latest_date)
    in_trade = current_trade is not None
    current_alloc = THRUST_ALLOC if in_trade else BASE_ALLOC
    state = ("IN-TRADE — allocation 150% (50% base + 100% leveraged signal layer)"
             if in_trade
             else "OUT — allocation 50% (base only; thrust signal not active)")

    # Days since state change.
    days_since = 0
    if in_trade:
        days_since = (latest_date - pd.Timestamp(current_trade["entry_date"])).days
    else:
        # Look back at the most recent completed trade's exit_date.
        last_completed = trades[-1] if trades else None
        if last_completed and pd.Timestamp(last_completed["exit_date"]) <= latest_date:
            days_since = (latest_date - pd.Timestamp(last_completed["exit_date"])).days

    # Breadth context — composite z, breadth percentages on latest date.
    breadth_context = None
    recent_signal_fires: list[dict] = []
    if br:
        ser = br["series"]
        dates = ser["dates"]
        if dates and dates[-1] == latest_date.strftime("%Y-%m-%d"):
            i = -1
        else:
            try:
                i = dates.index(latest_date.strftime("%Y-%m-%d"))
            except ValueError:
                i = None
        if i is not None:
            breadth_context = {
                "composite_z": _safe(ser["composite_z"][i]),
                "composite_p90": _safe(ser["composite_p90"][i]),
                "composite_p10": _safe(ser["composite_p10"][i]),
                "ma_breadth": _safe(ser["ma_breadth"][i]),
                "rsi_breadth": _safe(ser["rsi_breadth"][i]),
                "highs_breadth": _safe(ser["highs_breadth"][i]),
                "composite_above_p90": bool(ser["composite_above_p90"][i]) if ser.get("composite_above_p90") else None,
                "trigger_count": ser["trigger_count"][i] if ser.get("trigger_count") else None,
                "rsi_trigger": bool(ser["rsi_trigger"][i]) if ser.get("rsi_trigger") else None,
                "ma_zweig_trigger": bool(ser["ma_zweig_trigger"][i]) if ser.get("ma_zweig_trigger") else None,
                "highs_trigger": bool(ser["highs_trigger"][i]) if ser.get("highs_trigger") else None,
            }
        # Recent signal fires from the breadth signals list.
        signals = br.get("signals", [])
        recent_signal_fires = signals[-5:]

    last_completed_trade = None
    for t in reversed(trades):
        if pd.Timestamp(t["exit_date"]) <= latest_date:
            if not (current_trade and t.get("entry_date") == current_trade.get("entry_date")):
                last_completed_trade = t
                break

    payload = {
        "computed_at_utc": datetime.now(timezone.utc).isoformat(),
        "etf": ETF,
        "strategy": "SOXX 50/150 (base 50% always-on + 100% leveraged when triple-combo thrust signal is active)",
        "latest_data_date": latest_date.strftime("%Y-%m-%d"),
        "state": state,
        "in_trade": in_trade,
        "current_allocation_pct": int(current_alloc * 100),
        "base_allocation_pct": int(BASE_ALLOC * 100),
        "thrust_allocation_pct": int(THRUST_ALLOC * 100),
        "days_since_state_change": int(days_since),
        "current_trade": current_trade,
        "last_completed_trade": last_completed_trade,
        "breadth_context_latest": breadth_context,
        "recent_signal_fires": recent_signal_fires,
        "refresh_note": (
            "This is a snapshot of state as of latest_data_date. To get a "
            "right-now read, re-run the upstream pipeline first: "
            "fetch_constituents.py --etf SOXX -> compute_breadth.py --etf SOXX "
            "-> run_etf_oos.py --etf SOXX, then live_signal.py."
        ),
    }

    OUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # Console report
    print("=" * 72)
    print(f"  LIVE SIGNAL — {ETF} 50/150 strategy")
    print("=" * 72)
    print(f"  As of           : {latest_date.strftime('%Y-%m-%d')}")
    print(f"  State           : {state}")
    print(f"  Allocation today: {int(current_alloc * 100)}%  (base {int(BASE_ALLOC*100)}%, on-signal {int(THRUST_ALLOC*100)}%)")
    print(f"  Days in state   : {days_since}")
    if breadth_context:
        cz = breadth_context.get("composite_z")
        p90 = breadth_context.get("composite_p90")
        tc = breadth_context.get("trigger_count")
        print(f"  Breadth today   : composite_z = {cz:+.2f}  (p90 = {p90:+.2f}, "
              f"{'above' if cz is not None and p90 is not None and cz >= p90 else 'below'})")
        print(f"                    triggers active: {tc}/3  "
              f"(rsi={breadth_context.get('rsi_trigger')} "
              f"ma_zweig={breadth_context.get('ma_zweig_trigger')} "
              f"highs={breadth_context.get('highs_trigger')})")
    if current_trade:
        print(f"  Current trade   : entered {current_trade['entry_date']}  "
              f"@ ${current_trade['entry_open']:.2f}  "
              f"current return ~ (mark-to-market not shown)")
    elif last_completed_trade:
        print(f"  Last trade      : {last_completed_trade['entry_date']} -> "
              f"{last_completed_trade['exit_date']}  "
              f"return {last_completed_trade['trade_return']*100:+.1f}%  "
              f"({last_completed_trade['exit_reason']})")
    print()
    print(f"  Wrote {OUT_PATH.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
