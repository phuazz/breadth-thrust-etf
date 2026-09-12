"""Current overlay decision independent of the blend valuation end date.

Uses the existing registered gate/tilt functions and parameters, not a new
strategy. Python months are 1-indexed. Missing inputs fail closed.
"""
import json
import math
from datetime import datetime, timezone
from pathlib import Path
import pandas as pd
import pandas_market_calendars as mcal

from session_bounds import last_completed_session_on
from overlay_state import sleeve_nav_weights

ROOT = Path(__file__).resolve().parent.parent


def build(now=None):
    import run_risk_overlay as ro
    now = now or datetime.now(timezone.utc)
    required = pd.Timestamp(last_completed_session_on(mcal.get_calendar("NYSE"), now))
    panel = json.loads((ROOT / "data/breadth_csp1.json").read_text(encoding="utf-8"))
    breadth = pd.Series(panel["series"]["ma_breadth"],
                        index=pd.to_datetime(panel["series"]["dates"]), dtype=float)
    if (panel["end_date"] != str(required.date()) or breadth.index.has_duplicates
            or not breadth.index.is_monotonic_increasing or breadth.index[-1] > required):
        raise ValueError("invalid breadth source dates")
    breadth = breadth.loc[:required].dropna()
    if any(not math.isfinite(v) or not 0 <= v <= 1 for v in breadth):
        raise ValueError("invalid breadth observations")
    if breadth.empty or breadth.index[-1] != required:
        raise ValueError("breadth gate input does not reach the required session")
    states, ng_last = ro._load_norgate_states(breadth.index)
    if states is None:
        if ro.NORGATE_STATES_PATH.exists():
            raise ValueError("preferred gate feed is present but invalid; no silent fallback")
        states = ro._compute_states(breadth, ro.OFF_THRESHOLD, ro.ON_THRESHOLD)
        gate_feed, gate_date = "csp1-scrape", str(required.date())
    else:
        gate_feed, gate_date = "norgate-local", str(pd.Timestamp(ng_last).date())
        if gate_date != str(required.date()):
            raise ValueError("preferred gate state feed is not current")
    _, ratio = ro._load_eem_data(required_session=required)
    if ratio is None:
        raise ValueError("EM tilt input is unavailable")
    if ratio.index.has_duplicates or not ratio.index.is_monotonic_increasing or ratio.index[-1] > required:
        raise ValueError("invalid tilt source dates")
    ratio = ratio.loc[:required].dropna()
    if any(not math.isfinite(v) or v <= 0 for v in ratio):
        raise ValueError("invalid tilt observations")
    if ratio.empty or ratio.index[-1] != required or len(ratio) < ro.EEM_TILT_SLOW_MA:
        raise ValueError("EM tilt input is incomplete")
    gate_on = not bool(states.iloc[-1])
    tilt_on = bool(ro._compute_eem_tilt_signal(ratio).iloc[-1])
    asof = str(required.date())
    overlay = {
        "gate_parameters": {"derisk_fraction": ro.DERISK_FRACTION},
        "events": [{"date": asof, "direction": "RISK_OFF" if gate_on else "RISK_ON"}],
        "phase22_eem_tilt": {"enabled": True, "parameters": {"tilt_weight": ro.EEM_TILT_WEIGHT},
            "signal_as_of": asof, "signal_stale": False,
            "events": [{"date": asof, "direction": "EM_TILT_ON" if tilt_on else "EM_TILT_OFF"}]},
    }
    return {"as_of": asof, "gate_feed": gate_feed, "gate_input_date": gate_date,
            "tilt_input_date": asof, "gate_on": gate_on, "tilt_on": tilt_on,
            "weights": sleeve_nav_weights(overlay, asof)}


if __name__ == "__main__":
    output = build()
    (ROOT / "data/overlay_decision.json").write_text(json.dumps(output, indent=2), encoding="utf-8")
