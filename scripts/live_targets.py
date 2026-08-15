"""Target weights for the NEXT fill, ranked on each sleeve's own signal.

WHY A SEPARATE STEP FROM THE ENGINES.

The engines are backtests. They emit a rebalance only where an execution BAR
exists, because a backtest has to mark the trade to a price. That is right for
a backtest and useless on a Friday morning: the fill has not happened, so the
bar does not exist, so no engine can tell you what to trade.

Worse, it silently diverges from what was tested. Historical vendor data is
essentially complete (514 of 516 Xetra sessions over two years), so a backtest
Friday always has Thursday beside it and ranks on Thursday. Live, the Xetra
.DE lines publish roughly a session late — Thursday 13 Aug 2026 was absent at
Friday decision time and had backfilled by Saturday. So the backtest ranks on
Thursday while the live book, waiting for a bar, cannot rank at all. Twenty per
cent of NAV, every week.

THE FIX, WHICH IS THE WHOLE POINT OF THIS MODULE.

Sleeve D's SIGNAL was never late. It is the share of European constituents
above their 200-day average, and those constituent prices carried Thursday
throughout. Only the ETF wrapper's price was missing, and the wrapper's price
is needed to ACCOUNT for a trade, not to DECIDE one — live, you fill at the
real market, not at the vendor's bar.

So this ranks each sleeve on its own signal at the last completed session on
its own venue, using the engines' own weight functions, and never collapses
the signal onto the execution calendar. run_portfolio._build_panels_for does
collapse it (align_breadth_to_index onto closes.index), which is exactly how
Thursday goes missing; this reads the constituent breadth directly instead.

WHAT IT REFUSES TO DO. If a sleeve's signal does not reach the last completed
session on its venue, it is reported HOLD rather than ranked on whatever came
before. Ranking a session early is how EXH3/EXV3 flipped on a 1.3pp margin.

Usage:
    python scripts/live_targets.py
    python scripts/live_targets.py --json data/live_targets.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pandas_market_calendars as mcal

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_factsheet import _collect_deployed_holdings, load_all  # noqa: E402
from etf_registry import (  # noqa: E402
    UNIVERSE_ETFS,
    UNIVERSE_EUROPE_SECTORS,
    display_ticker,
    get_etf,
)
from run_ma200_sweep import (  # noqa: E402
    MA_PERIOD,
    compute_ma200_breadth,
    load_constituent_prices,
)
from session_bounds import last_completed_session_on  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"


def _breadth_panel(universe: list[str]) -> tuple[pd.DataFrame, list[str]]:
    """Constituent-derived breadth, on its OWN index.

    Deliberately not _build_panels_for: that aligns breadth onto the execution
    calendar, which deletes a signal the vendor did publish whenever the ETF
    wrapper's own bar is missing.
    """
    cols, used = {}, []
    for etf in universe:
        try:
            cols[etf] = compute_ma200_breadth(load_constituent_prices(etf), MA_PERIOD)
        except FileNotFoundError:
            continue
        used.append(etf)
    return pd.DataFrame(cols).sort_index(), used


def _venue(universe: list[str]) -> str:
    cals = {get_etf(e).get("trading_calendar", "NYSE") for e in universe}
    return sorted(cals)[0] if len(cals) == 1 else "NYSE"


def _rank(signal: pd.DataFrame, weight_fn, venue: str, now_utc: datetime,
          label: str) -> dict:
    """Rank on the last signal session at or before the venue's last close."""
    cal = mcal.get_calendar(venue)
    lcs = last_completed_session_on(cal, now_utc)
    rows = signal.dropna(how="all")
    usable = rows.index[rows.index <= pd.Timestamp(lcs)] if lcs is not None else rows.index
    if len(usable) == 0:
        return {"sleeve": label, "venue": venue, "status": "HOLD",
                "reason": "no signal at or before the last completed session",
                "decision_session": None, "last_completed_session":
                    str(lcs.date()) if lcs is not None else None, "weights": {}}
    decided = usable[-1]
    reaches = lcs is not None and pd.Timestamp(decided) >= pd.Timestamp(lcs)
    w = weight_fn(signal.loc[decided])
    w = w[w > 0].sort_values(ascending=False)
    return {
        "sleeve": label,
        "venue": venue,
        "status": "READY" if reaches else "HOLD",
        "reason": None if reaches else (
            f"signal reaches {decided.date()} but the last completed {venue} "
            f"session is {lcs.date()} — ranking now would use an "
            f"earlier session"),
        "decision_session": str(decided.date()),
        "last_completed_session": str(lcs.date()) if lcs is not None else None,
        "weights": {k: round(float(v), 6) for k, v in w.items()},
    }


def build(now_utc: datetime | None = None) -> dict:
    now = now_utc or datetime.now(timezone.utc)
    import run_asset_class_rotation as ac
    import run_europe_rotation as eu
    import run_thematic_rotation as th
    import run_topk_robustness as tk
    from run_portfolio import top_k_breadth_weight

    sleeves = []

    ba, used_a = _breadth_panel(UNIVERSE_ETFS)
    sleeves.append(_rank(tk._to_signal_panel(ba),
                         top_k_breadth_weight(tk.HEADLINE_K),
                         _venue(used_a), now, "A"))

    cb = ac.download_prices()
    sleeves.append(_rank(ac.compute_signal(cb),
                         ac.top_k_by_signal(ac.HEADLINE_K), "NYSE", now, "B"))

    cc = th.download_prices()
    sleeves.append(_rank(th.compute_signal(cc),
                         th.top_k_equal_weight(th.HEADLINE_K), "NYSE", now, "C"))

    bd, used_d = _breadth_panel(UNIVERSE_EUROPE_SECTORS)
    sleeves.append(_rank(bd, eu.top_k_breadth_weight(eu.HEADLINE_K),
                         _venue(used_d), now, "D"))

    # Effective NAV weights and the delta against what is currently held.
    out = load_all()
    overlay, sleeve_data = out[1], out[2]
    from build_factsheet import sleeve_nav_weights
    asof = max((s["decision_session"] for s in sleeves
                if s["decision_session"]), default=None)
    st = sleeve_nav_weights(overlay, asof)
    held = {}
    for h in _collect_deployed_holdings(sleeve_data, overlay, asof):
        held[(h["sleeve"], h["etf"])] = h["effective"]

    lines = []
    for s in sleeves:
        nav = st.get(s["sleeve"].lower(), 0.0)
        for etf, w in s["weights"].items():
            lines.append({"sleeve": s["sleeve"], "etf": etf,
                          "traded": _traded(etf), "within": w,
                          "target": w * nav,
                          "held": held.get((s["sleeve"], etf), 0.0),
                          "status": s["status"]})
    for (sl, etf), eff in held.items():
        if sl in {"TILT", "GATE"}:
            lines.append({"sleeve": sl, "etf": etf, "traded": _traded(etf),
                          "within": 1.0, "target": eff, "held": eff,
                          "status": "READY"})
        elif not any(x["sleeve"] == sl and x["etf"] == etf for x in lines):
            lines.append({"sleeve": sl, "etf": etf, "traded": _traded(etf),
                          "within": 0.0, "target": 0.0, "held": eff,
                          "status": next((s["status"] for s in sleeves
                                          if s["sleeve"] == sl), "READY")})
    for ln in lines:
        ln["delta"] = ln["target"] - ln["held"]
    lines.sort(key=lambda x: (x["sleeve"], -abs(x["delta"])))
    return {"computed_at_utc": now.isoformat(), "as_of": asof,
            "sleeves": sleeves, "lines": lines,
            "one_way_turnover": sum(abs(x["delta"]) for x in lines) / 2}


def _traded(etf: str) -> str:
    try:
        return display_ticker(etf)
    except Exception:  # noqa: BLE001
        return etf


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", default=str(DATA_DIR / "live_targets.json"))
    args = ap.parse_args(argv)
    r = build()

    print(f"\nTARGETS for the next fill — computed {r['computed_at_utc'][:16]}Z\n")
    print(f"  {'sleeve':7s} {'venue':6s} {'decided on':12s} {'vs last close':13s} status")
    for s in r["sleeves"]:
        print(f"  {s['sleeve']:7s} {s['venue']:6s} "
              f"{str(s['decision_session']):12s} "
              f"{str(s['last_completed_session']):13s} {s['status']}")
        if s["reason"]:
            print(f"          {s['reason']}")

    holds = [s["sleeve"] for s in r["sleeves"] if s["status"] != "READY"]
    print(f"\n  {'S':2s} {'key':10s} {'traded':10s} {'held':>7s} {'target':>7s} "
          f"{'delta':>8s}")
    for ln in r["lines"]:
        if abs(ln["delta"]) < 5e-5 and ln["held"] == 0:
            continue
        tag = ""
        if ln["status"] != "READY":
            tag = "  HOLD"
        elif ln["held"] == 0:
            tag = "  <== BUY (new)"
        elif ln["target"] == 0:
            tag = "  <== SELL ALL"
        print(f"  {ln['sleeve']:2s} {ln['etf']:10s} {ln['traded']:10s} "
              f"{ln['held']*100:6.2f}% {ln['target']*100:6.2f}% "
              f"{ln['delta']*100:+7.2f}%{tag}")
    print(f"\n  one-way turnover {r['one_way_turnover']*100:.2f}% of NAV")
    if holds:
        print(f"  HOLD (signal short of the last close): {', '.join(holds)} — "
              f"do not trade these; leave them as held.")

    Path(args.json).write_text(json.dumps(r, indent=1), encoding="utf-8")
    print(f"\n  wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
