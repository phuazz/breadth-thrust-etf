"""WS3 Item 3 — entry-point discipline check (CLAUDE.md rule: deploy after
flat or negative stretches, not after strong runs), plus two small
decision-support diagnostics that need the same final-track curve:
  - S2 vs deployed sub-period consistency (the last open S2 datum), and
  - a blend-without-C composition (DIAGNOSTIC ONLY, not a proposal: C's
    universe membership is CLOSED per WS2; this informs the keep/demote
    verdict on the C sleeve's 10% blend seat).

Three ways this could be silently wrong, and the defences:
  1. WRONG TRACK — entry-point statements must describe the LIVE system.
     The curve used is the composed post-Phase-29 gated+tilted track,
     regression-checked against the committed live track inside
     ws3_common; the data-as-of date is printed with every statement.
  2. ROLLING-WINDOW OFF-BY-ONE — worst-12m uses 252 TRADING days via
     ws1_common.dd_metrics (same convention as WS1's surface), not
     calendar-day arithmetic.
  3. RECENCY MISREAD — "strong run" is judged against the track's OWN
     history: the trailing 3m/6m/12m returns are placed as percentiles of
     the full-window distribution of same-length rolling returns, so the
     verdict is quantitative, not eyeballed.

Output: data/ws3_entrypoint.json
Run:    python scripts/run_ws3_entrypoint.py
"""
from __future__ import annotations

import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

import ws1_common as W  # noqa: E402
import ws3_common as W3  # noqa: E402

OUT = W.DATA / "ws3_entrypoint.json"


def main() -> int:
    base = W3.build_ws3_baselines()
    idx, end = base["idx"], base["common_end"]
    final = base["final_track_returns"]
    eq = (1 + final).cumprod()
    eq = eq / eq.iloc[0]

    ddm = W.dd_metrics(eq)
    r12 = eq.pct_change(252).dropna()
    worst12_date = str(r12.idxmin().date())

    windows = {"3m": 63, "6m": 126, "12m": 252}
    recent = {}
    for label, n in windows.items():
        roll = eq.pct_change(n).dropna()
        latest = float(roll.iloc[-1])
        pct = float((roll < latest).mean() * 100)
        recent[label] = {"return": latest, "percentile_of_history": pct}

    dd_now = float(eq.iloc[-1] / eq.cummax().iloc[-1] - 1)
    ath_date = eq.idxmax()
    days_since_ath = int((eq.index[-1] - ath_date).days)

    sub = W.sub_period_sharpes(eq)

    # verdict rule (stated): a "strong run" = trailing 6m AND 12m returns
    # both above the 75th percentile of their own history.
    strong_run = (recent["6m"]["percentile_of_history"] > 75
                  and recent["12m"]["percentile_of_history"] > 75)
    verdict = ("AFTER A STRONG RUN — entry-point discipline says do not add"
               " capital now; schedule adds after a flat/negative stretch"
               if strong_run else
               "NOT after a strong run — entry acceptable under the"
               " discipline rule")

    # ---- S2 consistency (decision datum) --------------------------------
    s2_final = W3.gated_returns(
        W3.tilted_blend_returns(
            {"A": base["rets"]["A"], "B": base["rets"]["B_S2"],
             "C": base["rets"]["C"], "D": base["rets"]["D"]},
            base["eem_ret"], base["tilt_sig_lagged"]),
        base["shy_ret"], base["gate_state_lagged"])
    eq_s2 = (1 + s2_final).cumprod()
    sub_s2 = W.sub_period_sharpes(eq_s2)
    s2_consistency = W.consistency_count(sub_s2, sub)

    # ---- blend-without-C diagnostic (NOT a proposal) ---------------------
    wts_noc = (0.35 / 0.90, 0.35 / 0.90, 0.0, 0.20 / 0.90)
    noc = W3.gated_returns(
        W3.tilted_blend_returns(
            {"A": base["rets"]["A"], "B": base["rets"]["B"],
             "C": base["rets"]["C"], "D": base["rets"]["D"]},
            base["eem_ret"], base["tilt_sig_lagged"], w=wts_noc),
        base["shy_ret"], base["gate_state_lagged"])
    eq_noc = (1 + noc).cumprod()
    st_noc = W.window_stats(eq_noc, idx[0], end)
    st_dep = W.window_stats(eq, idx[0], end)
    sub_noc = W.sub_period_sharpes(eq_noc)
    noc_consistency = W.consistency_count(sub_noc, sub)

    print(f"data as of {end.date()} (EU-constituent bound)")
    print(f"worst rolling 12m: {ddm['worst_rolling_12m_return'] * 100:+.1f}%"
          f" (ending {worst12_date}); longest underwater "
          f"{ddm['longest_underwater_days']} trading days")
    for label in ("3m", "6m", "12m"):
        r = recent[label]
        print(f"trailing {label}: {r['return'] * 100:+.1f}% "
              f"(p{r['percentile_of_history']:.0f} of own history)")
    print(f"drawdown now {dd_now * 100:+.2f}% ({days_since_ath} days since "
          f"high) -> {verdict}")
    print(f"S2 final-track consistency vs deployed: {s2_consistency}/6")
    print(f"blend without C (diagnostic): Sharpe {st_noc['sharpe']:+.4f} vs "
          f"deployed {st_dep['sharpe']:+.4f}, DD {st_noc['max_dd'] * 100:.1f}%"
          f" vs {st_dep['max_dd'] * 100:.1f}%, consistency "
          f"{noc_consistency}/6")

    W.write_json(OUT, {
        "computed_at_utc": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "data_as_of": str(end.date()),
        "worst_rolling_12m_return": ddm["worst_rolling_12m_return"],
        "worst_rolling_12m_end_date": worst12_date,
        "longest_underwater_days": ddm["longest_underwater_days"],
        "dd_2020_covid": ddm["dd_2020_covid"],
        "dd_2022": ddm["dd_2022"],
        "trailing": recent,
        "drawdown_now": dd_now,
        "days_since_ath": days_since_ath,
        "sub_period_sharpe_final_track": sub,
        "strong_run_rule": "6m AND 12m trailing above p75 of own history",
        "strong_run": bool(strong_run),
        "verdict": verdict,
        "s2_consistency_vs_deployed": s2_consistency,
        "s2_sub_period_sharpe": sub_s2,
        "blend_without_C_diagnostic": {
            "note": ("diagnostic only — C universe membership CLOSED "
                     "(WS2); informs the C-sleeve blend-seat verdict"),
            "weights": list(wts_noc),
            "stats": st_noc, "consistency_vs_deployed": noc_consistency,
            "sub_period_sharpe": sub_noc,
        },
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
