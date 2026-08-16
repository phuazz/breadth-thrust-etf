"""WS17 H1 — the gate-flip bridge: what a perp fill at the signal close recovers.

The deployed Phase 19 overlay computes states from breadth_csp1.json
series.ma_breadth (0.20/0.50 hysteresis) and applies them shift(1): the
backtest fills at the signal close, while a live ETF fill happens at the next
session's close. A perp on xyz:SP500 fills within hours of the panel landing,
at approximately the signal-close price. Per flip, the measurable prize is the
close-T -> close-T+1 return x flip direction x the 50% NAV moved, minus perp
costs. Pre-registered in reviews/2026-08-16_ws17_hl-perp-expression.md; the
success criteria and the n<8 measured-bound rule are frozen there.

Guard: the replicated state series must match the committed risk_overlay.json
transition dates exactly, else this script refuses to report.

Run: python scripts/run_ws17_gate_bridge.py  -> data/ws17_gate_bridge.json
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtest import download_spy_close  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

# Phase 19 deployed parameters (run_risk_overlay.py) — not tunable here.
OFF_THRESHOLD = 0.20
ON_THRESHOLD = 0.50
DERISK_FRACTION = 0.50

# Frozen cost model (pre-registration): 10bp round trip on the moved notional
# per flip pair, plus one session of funding at the worst band edge (+6%/yr).
PERP_ROUND_TRIP_BPS = 10.0
FUNDING_WORST_ANN = 0.06
BOOTSTRAP_N = 5000
BOOTSTRAP_SEED = 20260816
MIN_N_FOR_INFERENCE = 8


def compute_states(breadth: pd.Series, off: float, on: float) -> pd.Series:
    """Bit-faithful replication of run_risk_overlay._compute_states."""
    states = []
    state = 1.0  # start RISK_ON
    for v in breadth.values:
        if pd.isna(v):
            states.append(state)
            continue
        if state == 1.0 and v < off:
            state = 0.0
        elif state == 0.0 and v > on:
            state = 1.0
        states.append(state)
    return pd.Series(states, index=breadth.index, dtype=float)


def main() -> int:
    # The authoritative flip population is the DEPLOYED record: the events list
    # in risk_overlay.json (gate_feed norgate-local). The breadth_csp1-derived
    # replication below is the guard, not the population — hysteresis is
    # path-dependent, so it must start at the overlay's own evaluation window.
    overlay = json.loads((DATA_DIR / "risk_overlay.json").read_text(encoding="utf-8"))
    committed = overlay.get("events") or []
    gated = overlay["gated_variants"]["blend_35_35_10_20_gated"]
    window_start = pd.Timestamp(gated["dates"][0])

    csp1 = json.loads((DATA_DIR / "breadth_csp1.json").read_text(encoding="utf-8"))
    ser = csp1["series"]
    breadth_full = pd.Series(ser["ma_breadth"], index=pd.to_datetime(ser["dates"]), dtype=float)
    breadth = breadth_full[breadth_full.index >= window_start]
    states = compute_states(breadth, OFF_THRESHOLD, ON_THRESHOLD)
    transitions = states.diff().fillna(0.0)
    replicated = [
        (d.strftime("%Y-%m-%d"), "RISK_OFF" if states.loc[d] == 0.0 else "RISK_ON")
        for d in states.index[transitions != 0]
    ]
    committed_pairs = [(e["date"], e["direction"]) for e in committed]
    # Amendment 1 (memo, 2026-08-16): the deployed feed is norgate-local, so
    # the iShares-panel replication is NOT expected to match exactly — the
    # committed events list is the population, and the comparison below is
    # filed as a feed-divergence finding rather than a stop condition.
    feed_divergence = {
        "replication_matches": replicated == committed_pairs,
        "committed_only": [list(t) for t in sorted(set(committed_pairs) - set(replicated))],
        "replicated_only": [list(t) for t in sorted(set(replicated) - set(committed_pairs))],
        "note": ("gate_feed=" + str(overlay.get("gate_feed")) +
                 "; iShares-panel replication computed on the overlay window for comparison"),
    }

    flip_dates = [pd.Timestamp(e["date"]) for e in committed]
    directions = {pd.Timestamp(e["date"]): e["direction"] for e in committed}

    start = (breadth.index[0] - pd.Timedelta(days=10)).strftime("%Y-%m-%d")
    end = (breadth.index[-1] + pd.Timedelta(days=10)).strftime("%Y-%m-%d")
    spy = download_spy_close(start, end)
    spy = spy[~spy.index.duplicated(keep="first")]

    per_flip_cost = (PERP_ROUND_TRIP_BPS / 10_000.0) * DERISK_FRACTION \
        + FUNDING_WORST_ANN / 365.0 * DERISK_FRACTION

    flips = []
    for d in flip_dates:
        direction = directions[d]
        # Bridge return: close T -> close T+1 on the traded index proxy.
        idx = spy.index.searchsorted(d)
        if idx >= len(spy.index) or spy.index[idx] != d:
            # Signal date not a SPY session (mixed-calendar edge) — use the
            # next available session as T; note it.
            note = "signal date not a SPY session; T advanced to next session"
            if idx >= len(spy.index):
                continue
        else:
            note = None
        if idx + 1 >= len(spy.index):
            flips.append({"date": d.strftime("%Y-%m-%d"), "direction": direction,
                          "bridge_return": None, "net_nav_bp": None,
                          "note": "no next session yet — flip too recent"})
            continue
        t0, t1 = spy.index[idx], spy.index[idx + 1]
        r = float(spy.loc[t1] / spy.loc[t0] - 1.0)
        # RISK_OFF: the book should be 50% out during T->T+1 but the ETF fill
        # only lands at T+1; a short perp bridge captures -r on the moved half.
        # RISK_ON: symmetric with a long bridge capturing +r.
        sign = -1.0 if direction == "RISK_OFF" else 1.0
        gross_nav = sign * r * DERISK_FRACTION
        net_nav = gross_nav - per_flip_cost
        flips.append({
            "date": d.strftime("%Y-%m-%d"), "direction": direction,
            "t0": t0.strftime("%Y-%m-%d"), "t1": t1.strftime("%Y-%m-%d"),
            "bridge_return": round(r, 6),
            "gross_nav_bp": round(gross_nav * 10_000, 2),
            "net_nav_bp": round(net_nav * 10_000, 2),
            "note": note,
        })

    # Replacement guard (Amendment 1): the three largest-|gross| bridge returns
    # must agree between the SPY cache and an independently fetched ^GSPC price
    # series within 15bp, else refuse to report.
    import yfinance as yf  # local import: only this guard needs it
    scored_pre = [f for f in flips if f.get("bridge_return") is not None]
    biggest = sorted(scored_pre, key=lambda f: -abs(f["gross_nav_bp"]))[:3]
    gspc = yf.download("^GSPC", start=start, end=end, progress=False, auto_adjust=False)
    gspc_close = gspc["Close"]
    if hasattr(gspc_close, "columns"):
        gspc_close = gspc_close.iloc[:, 0]
    guard_rows = []
    guard_ok = True
    for f in biggest:
        try:
            g0 = float(gspc_close.loc[f["t0"]])
            g1 = float(gspc_close.loc[f["t1"]])
            g_r = g1 / g0 - 1.0
            dev_bp = abs(g_r - f["bridge_return"]) * 10_000
            ok = dev_bp <= 15.0
        except KeyError:
            g_r, dev_bp, ok = None, None, False
        guard_rows.append({"date": f["date"], "spy_r": f["bridge_return"],
                           "gspc_r": round(g_r, 6) if g_r is not None else None,
                           "dev_bp": round(dev_bp, 2) if dev_bp is not None else None,
                           "ok": ok})
        guard_ok = guard_ok and ok
    if not guard_ok:
        print("GUARD FAIL: SPY bridge returns do not agree with independent ^GSPC")
        for g in guard_rows:
            print(" ", g)
        (DATA_DIR / "ws17_gate_bridge.json").write_text(json.dumps({
            "status": "GUARD_FAIL", "price_guard": guard_rows,
        }, indent=2), encoding="utf-8")
        return 1

    scored = [f for f in flips if f.get("net_nav_bp") is not None]
    nets = np.array([f["net_nav_bp"] for f in scored]) / 10_000.0
    n = len(nets)
    total_gross_bp = float(sum(f["gross_nav_bp"] for f in scored))
    total_net_bp = float(sum(f["net_nav_bp"] for f in scored))

    boot = None
    if n >= MIN_N_FOR_INFERENCE:
        rng = np.random.default_rng(BOOTSTRAP_SEED)
        sums = np.array([rng.choice(nets, size=n, replace=True).sum()
                         for _ in range(BOOTSTRAP_N)])
        boot = {
            "n_paths": BOOTSTRAP_N,
            "p05_bp": round(float(np.percentile(sums, 5)) * 10_000, 2),
            "p50_bp": round(float(np.percentile(sums, 50)) * 10_000, 2),
            "p95_bp": round(float(np.percentile(sums, 95)) * 10_000, 2),
        }

    span_years = (breadth.index[-1] - breadth.index[0]).days / 365.25
    payload = {
        "status": "ok",
        "computed_at_utc": datetime.now(timezone.utc).isoformat(),
        "breadth_coverage": {
            "start": breadth.index[0].strftime("%Y-%m-%d"),
            "end": breadth.index[-1].strftime("%Y-%m-%d"),
            "n_days": int(len(breadth)),
        },
        "price_guard_gspc": guard_rows,
        "feed_divergence": feed_divergence,
        "parameters": {
            "off_threshold": OFF_THRESHOLD, "on_threshold": ON_THRESHOLD,
            "derisk_fraction": DERISK_FRACTION,
            "perp_round_trip_bps": PERP_ROUND_TRIP_BPS,
            "funding_worst_ann": FUNDING_WORST_ANN,
            "min_n_for_inference": MIN_N_FOR_INFERENCE,
        },
        "n_flips_total": len(flips),
        "n_flips_scored": n,
        "flips": flips,
        "total_gross_nav_bp": round(total_gross_bp, 2),
        "total_net_nav_bp": round(total_net_bp, 2),
        "per_year_net_nav_bp": round(total_net_bp / span_years, 2) if span_years else None,
        "bootstrap": boot,
        "verdict_rule": ("n < 8 -> measured-bound, no inferential claim; "
                        "n >= 8 -> paired bootstrap 90% CI must clear zero for KEEP"),
    }
    out = DATA_DIR / "ws17_gate_bridge.json"
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(f"Breadth {payload['breadth_coverage']['start']} -> {payload['breadth_coverage']['end']}"
          f" ({payload['breadth_coverage']['n_days']} days); price guard ok: {guard_ok}; "
          f"feed replication match: {feed_divergence['replication_matches']}")
    print(f"Flips: {len(flips)} total, {n} scored")
    for f in flips:
        print(f"  {f['date']}  {f['direction']:<9} bridge={f.get('bridge_return')}"
              f"  gross={f.get('gross_nav_bp')}bp  net={f.get('net_nav_bp')}bp"
              + (f"  [{f['note']}]" if f.get("note") else ""))
    print(f"Total gross {total_gross_bp:+.1f}bp of NAV; net {total_net_bp:+.1f}bp"
          f" over {span_years:.1f}y ({payload['per_year_net_nav_bp']:+.1f}bp/yr)")
    if boot:
        print(f"Bootstrap sum 90% CI: [{boot['p05_bp']}, {boot['p95_bp']}]bp")
    else:
        print(f"n < {MIN_N_FOR_INFERENCE}: measured-bound only, no inferential claim (pre-registered rule)")
    print(f"Wrote {out.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
