"""Tier-2 #3 — Norgate breadth-feed reconciliation. REVIEW-AND-PROPOSE:
this script changes NOTHING deployed; it measures whether Norgate's
precomputed S&P 500 breadth could replace the scrape-built gate input.

What is compared
  DEPLOYED : data/breadth_csp1.json -> series.ma_breadth — share of
             CSP1-roster constituents above their 50d SMA (rosters
             scraped weekly from iShares; prices from yfinance adjusted
             closes; NYSE calendar).
  CANDIDATE: Norgate #SPX%MA50 — "S&P 500 % Stocks above MA50",
             precomputed by the vendor from official point-in-time
             membership (values 0-100; scaled to 0-1 here).
Both series then drive the DEPLOYED Phase 19 hysteresis — imported from
run_risk_overlay (_compute_states, OFF 0.20 / ON 0.50), not
re-implemented — and the resulting regime-state series and every flip
are compared. The candidate's full available depth is also reported
(the deployed panel starts 2018-01-05; the vendor series reaches
decades further back — relevant to future research, not to this swap).

LICENCE GUARD (Norgate: personal use, no redistribution): no vendor
series VALUES are written to any committed path. The joined daily panel
goes to git-ignored data_local/ only; the committed output
(data/norgate_feed_reconciliation.json) carries statistics, dates and
DERIVED gate states — the same class of derived output the public
dashboard is allowed to show.

Three ways this could be silently wrong, and the defences:
  1. BASIS MISMATCH READ AS DATA ERROR — yfinance adjusted closes vs the
     vendor's price basis make near-MA names flip differently by
     construction. Defence: systematic bias (median signed diff) is
     reported separately from noise (IQR, p95), a threshold-zone
     disagreement count isolates the only region where basis matters to
     the gate, and NEITHER series is adjusted toward the other.
  2. HYSTERESIS DRIFT — a re-implemented state machine could diverge
     from production. Defence: _compute_states and both thresholds are
     imported from the deployed module; an inline fallback copy is used
     ONLY if the import fails, and the run records which path executed.
  3. OVERLAP ILLUSION — the deployed panel is weekly-refreshed and
     forward-filled downstream. Defence: comparison runs on an inner
     join of RAW series dates (no forward-fill), and per-source
     missing-day counts on the joint NYSE calendar are reported.

Output: data/norgate_feed_reconciliation.json (stats + derived states),
        data_local/norgate_feed_panel.parquet (git-ignored, vendor data)
Run:    python scripts/run_norgate_feed_reconciliation.py
"""
from __future__ import annotations

import datetime as dt  # Python datetime: months are 1-indexed
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATA_LOCAL = ROOT / "data_local"          # git-ignored (licence guard)
sys.path.insert(0, str(ROOT / "scripts"))

NORGATE_SYMBOL = "#SPX%MA50"
THRESHOLD_ZONE = 0.02                      # flip-risk band around 0.20/0.50

try:
    from run_risk_overlay import (_compute_states, OFF_THRESHOLD,
                                  ON_THRESHOLD)
    STATE_MACHINE_SOURCE = "imported from run_risk_overlay (deployed)"
except Exception as exc:  # pragma: no cover — fallback, recorded if used
    OFF_THRESHOLD, ON_THRESHOLD = 0.20, 0.50
    STATE_MACHINE_SOURCE = f"inline fallback copy (import failed: {exc!r})"

    def _compute_states(breadth: pd.Series, off: float,
                        on: float) -> pd.Series:
        states, state = [], 1.0
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


def flips(states: pd.Series) -> list[dict]:
    d = states.diff().fillna(0)
    out = []
    for ts in states.index[d != 0]:
        out.append({"date": str(ts.date()),
                    "direction": "OFF" if states.loc[ts] == 0.0 else "ON"})
    return out


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")

    csp1 = json.loads((DATA / "breadth_csp1.json").read_text(
        encoding="utf-8"))
    dep = pd.Series(csp1["series"]["ma_breadth"],
                    index=pd.to_datetime(csp1["series"]["dates"]),
                    name="deployed").dropna()

    import norgatedata as nd
    assert nd.status(), "NDU not running"
    df = nd.price_timeseries(
        NORGATE_SYMBOL,
        stock_price_adjustment_setting=nd.StockPriceAdjustmentType.TOTALRETURN,
        padding_setting=nd.PaddingType.NONE,
        timeseriesformat="pandas-dataframe",
    )
    cand_raw = df["Close"]
    scale = 100.0 if cand_raw.max() > 1.5 else 1.0
    cand = (cand_raw / scale).rename("norgate")

    j = pd.concat([dep, cand], axis=1, join="inner").dropna()
    assert len(j) > 500, f"overlap too short: {len(j)}"

    # licence guard: vendor values only into git-ignored data_local/
    DATA_LOCAL.mkdir(exist_ok=True)
    j.to_parquet(DATA_LOCAL / "norgate_feed_panel.parquet")

    diff = j["norgate"] - j["deployed"]
    # missing-day accounting on the union calendar of the overlap span
    union = dep.index.union(cand.index)
    span = union[(union >= j.index[0]) & (union <= j.index[-1])]
    miss_dep = int(len(span.difference(dep.index)))
    miss_cand = int(len(span.difference(cand.index)))

    zone = ((j - OFF_THRESHOLD).abs().min(axis=1) < THRESHOLD_ZONE) | \
           ((j - ON_THRESHOLD).abs().min(axis=1) < THRESHOLD_ZONE)
    side_off = (j["norgate"] < OFF_THRESHOLD) != (j["deployed"]
                                                  < OFF_THRESHOLD)
    side_on = (j["norgate"] > ON_THRESHOLD) != (j["deployed"]
                                                > ON_THRESHOLD)

    s_dep = _compute_states(j["deployed"], OFF_THRESHOLD, ON_THRESHOLD)
    s_cand = _compute_states(j["norgate"], OFF_THRESHOLD, ON_THRESHOLD)
    agree = float((s_dep == s_cand).mean())
    f_dep, f_cand = flips(s_dep), flips(s_cand)

    # pair flips: nearest same-direction candidate flip within 15 tdays
    cand_idx = {i: f for i, f in enumerate(f_cand)}
    used = set()
    pairs, unmatched_dep = [], []
    for f in f_dep:
        best, best_d = None, None
        fd = pd.Timestamp(f["date"])
        for i, g in cand_idx.items():
            if i in used or g["direction"] != f["direction"]:
                continue
            dd = abs((pd.Timestamp(g["date"]) - fd).days)
            if best is None or dd < best_d:
                best, best_d = i, dd
        if best is not None and best_d <= 21:
            used.add(best)
            pairs.append({"deployed": f, "norgate": cand_idx[best],
                          "calendar_day_delta":
                          (pd.Timestamp(cand_idx[best]["date"])
                           - fd).days})
        else:
            unmatched_dep.append(f)
    unmatched_cand = [g for i, g in cand_idx.items() if i not in used]

    result = {
        "computed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(
            timespec="seconds"),
        "review_and_propose": "no deployed change; measurement only",
        "state_machine_source": STATE_MACHINE_SOURCE,
        "thresholds": {"off": OFF_THRESHOLD, "on": ON_THRESHOLD},
        "candidate": {
            "symbol": NORGATE_SYMBOL,
            "scale_detected": scale,
            "full_depth_start": str(cand.index[0].date()),
            "full_depth_end": str(cand.index[-1].date()),
            "full_depth_days": int(len(cand)),
        },
        "overlap": {
            "start": str(j.index[0].date()), "end": str(j.index[-1].date()),
            "days": int(len(j)),
            "missing_days_deployed": miss_dep,
            "missing_days_norgate": miss_cand,
        },
        "level_reconciliation": {
            "correlation": round(float(j["norgate"].corr(j["deployed"])), 6),
            "median_signed_diff_pp": round(float(diff.median()) * 100, 3),
            "iqr_pp": round(float(diff.quantile(0.75)
                                  - diff.quantile(0.25)) * 100, 3),
            "mean_abs_diff_pp": round(float(diff.abs().mean()) * 100, 3),
            "p95_abs_diff_pp": round(float(diff.abs().quantile(0.95))
                                     * 100, 3),
            "max_abs_diff_pp": round(float(diff.abs().max()) * 100, 3),
            "max_abs_diff_date": str(diff.abs().idxmax().date()),
        },
        "threshold_zone": {
            "band_pp": THRESHOLD_ZONE * 100,
            "days_in_zone_either_series": int(zone.sum()),
            "days_side_disagree_off_0.20": int(side_off.sum()),
            "days_side_disagree_on_0.50": int(side_on.sum()),
        },
        "gate_states": {
            "agreement_share": round(agree, 6),
            "disagreement_days": int((s_dep != s_cand).sum()),
            "flips_deployed": f_dep,
            "flips_norgate": f_cand,
            "paired_flips": pairs,
            "unmatched_deployed_flips": unmatched_dep,
            "unmatched_norgate_flips": unmatched_cand,
        },
    }
    (DATA / "norgate_feed_reconciliation.json").write_text(
        json.dumps(result, indent=1), encoding="utf-8")

    lr = result["level_reconciliation"]
    gs = result["gate_states"]
    print(f"overlap {result['overlap']['start']} -> "
          f"{result['overlap']['end']} ({result['overlap']['days']} days); "
          f"candidate depth from {result['candidate']['full_depth_start']}")
    print(f"levels: corr {lr['correlation']:.4f}, median signed "
          f"{lr['median_signed_diff_pp']:+.2f}pp, p95 abs "
          f"{lr['p95_abs_diff_pp']:.2f}pp, max {lr['max_abs_diff_pp']:.2f}pp "
          f"({lr['max_abs_diff_date']})")
    print(f"states: agreement {gs['agreement_share']:.2%}, "
          f"flips deployed {len(f_dep)} vs norgate {len(f_cand)}, "
          f"paired {len(pairs)}, unmatched dep/cand "
          f"{len(unmatched_dep)}/{len(unmatched_cand)}")
    print(f"state machine: {STATE_MACHINE_SOURCE}")
    print("wrote data/norgate_feed_reconciliation.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
