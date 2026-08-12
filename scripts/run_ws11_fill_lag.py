"""WS11 - execution fill lag: Friday close (deployed) vs Monday close.

QUESTION
    Every deployed engine reads the signal at the session BEFORE the rebalance
    and fills at the rebalance close, so the book turns over at FRIDAY's close.
    The pipeline, however, runs on SATURDAY (mark_to_market_live.py:5), so the
    instruction does not exist until after that close and a real fill is the
    following MONDAY. What does the published record lose if the fill moves one
    session later?

    This is NOT a look-ahead question. Thursday's breadth is genuinely knowable
    on Thursday night, so a Friday-close fill is achievable if the signal run
    moves earlier in the week. It is a live-versus-backtest timing gap, and it
    is currently unmeasured.

METHOD
    Run each deployed sleeve's HEADLINE configuration ONCE under the deployed
    cadence, take the daily weight panel it returns, and recompute equity twice
    from that one panel:

        baseline  W        -> fill at the rebalance close (Friday)
        lagged    W.shift(1) -> fill one session later (Monday)

    Both legs therefore share one price panel, one signal panel, one set of
    rebalance dates and one weight path; the ONLY difference is the session on
    which those weights start earning. The sleeve harnesses are imported from
    run_ws10_holiday_cadence so the headline configurations are not restated
    here. NO engine source is modified and NO deployed artefact is written.

    Blend layer is the 35/35/10/20 A:B:C:D fixed blend, identical in both legs
    (same function, same cadence) and fed the lagged sleeve curves. This is the
    same pre-overlay blend WS10 restated, so its baseline is comparable with
    WS10's 1.1738. The EEM tilt and breadth gate sit ABOVE this layer and are
    not re-run; a full Monday-fill restatement would shift those too.

WHAT WOULD MAKE THIS SILENTLY WRONG
    1. A reconstruction that does not match the engine. The whole method rests
       on rebuilding equity from the weight panel outside the engine. If that
       rebuild does not reproduce the engine's own equity EXACTLY, the lagged
       leg measures my formula rather than the strategy. Asserted per sleeve.
    2. Shifting the wrong way. W.shift(-1) would fill one session EARLY - a
       genuine look-ahead - and would report a plausible-looking gain. Asserted
       by checking that every lagged fill date is the session immediately AFTER
       its baseline fill date, and that the fill-date sets are disjoint.
    3. Attributing to timing what is really cost. Turnover is unchanged by a
       fill lag (the same trades happen, one session later), so annual turnover
       must match between legs to rounding. Asserted.
    4. Reading one number where the sample is small. n is ~390 rebalances per
       sleeve; the per-rebalance gap is reported with its t-statistic so the
       drag can be told apart from weekend noise.

USAGE
    python scripts/run_ws11_fill_lag.py             # all sleeves + blend
    python scripts/run_ws11_fill_lag.py --sleeve a  # one sleeve (repeatable)
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import rebalance_calendar  # noqa: E402
from run_ws10_holiday_cadence import SLEEVES  # noqa: E402

OUT_PATH = SCRIPTS.parent / "data_local" / "ws11_fill_lag.json"

BASELINE = "friday_close"       # deployed: Thu signal -> Fri fill
LAGGED = "monday_close"         # same decision, one session later
MONDAY_GRID = "monday_grid"     # Fri signal -> Mon fill (the live schedule)
LEGS = (BASELINE, LAGGED, MONDAY_GRID)


def monday_grid_patch(calendar: str):
    """engine_rebalance_dates rebound to a W-MON grid, deployed mode intact.

    This is the only honest way to model the live workflow. The Saturday
    pipeline sees Friday's close, so a Monday fill should decide on FRIDAY's
    breadth, not Thursday's — the same one-session decision-to-fill lag the
    deployed engines already run, moved forward a day. Shifting the weight
    panel instead (the LAGGED leg) keeps Thursday's signal and so charges the
    strategy an extra session of staleness it would not actually suffer.

    No signal-indexing surgery: the engines still read `get_loc(rd) - 1`, so
    with rd on a Monday that IS Friday's close. Look-ahead is impossible by
    construction rather than by assertion.

    The grid uses ``holiday_aware_next``, NOT the deployed backward
    ``holiday_aware``. Backing a shut Monday up to the prior Friday would make
    the engine read Thursday and silently revert that week to the deployed
    W-FRI convention - on NYSE that is roughly one week in nine, which is
    enough to contaminate the measurement. Rolling forward to Tuesday keeps
    the signal bar on the prior weekly close in every week; the guard below
    asserts it rather than assuming it.
    """
    mode = rebalance_calendar.HOLIDAY_AWARE_NEXT

    def f(trading_index, eligible_start, freq="W-FRI", _engine_calendar=None):
        return rebalance_calendar.weekly_rebalance_dates(
            trading_index, eligible_start, "W-MON", mode=mode,
            calendar=calendar,
        )
    return f


def weekly_close_check(idx: pd.DatetimeIndex,
                       rebalance_dates: pd.DatetimeIndex) -> dict:
    """How many rebalances read the prior WEEKLY CLOSE as their signal bar.

    Usually a Friday; in a Good Friday week the week's closing session is the
    Thursday, so the test is "last session of its ISO week" rather than "is a
    Friday" - the weekday version fails at Easter for reasons unrelated to the
    grid.
    """
    last_of_week: dict[tuple[int, int], pd.Timestamp] = {}
    for ts in idx:                       # ascending, so the last write wins
        c = ts.isocalendar()
        last_of_week[(c.year, c.week)] = ts
    checked = closes = 0
    for d in rebalance_dates:
        i = idx.get_loc(d)
        if i == 0:
            continue
        prev = idx[i - 1]
        c = prev.isocalendar()
        checked += 1
        closes += int(last_of_week[(c.year, c.week)] == prev)
    return {"checked": checked, "on_weekly_close": closes}


# ---------------------------------------------------------------------------
# The engines' return accounting, lifted verbatim so it can be re-run on a
# modified weight panel. Identical in run_portfolio.py:171-177,
# run_asset_class_rotation.py:343-347 and run_thematic_rotation.py:709-713.
# ---------------------------------------------------------------------------
def equity_from_weights(W: pd.DataFrame, rets: pd.DataFrame,
                        cost: float) -> tuple[pd.Series, pd.Series]:
    """Return (equity, daily_ret) for a daily weight panel W.

    port_ret[t] = W[t-1] . rets[t], i.e. the weights set at t-1's close earn
    the t-1 -> t return. Turnover is charged on the session the weights change,
    which is the session the fill happens.
    """
    port_ret = (W.shift(1).fillna(0.0) * rets).sum(axis=1)
    turnover = W.diff().abs().sum(axis=1).fillna(0.0)
    port_ret = port_ret - turnover * cost
    return (1.0 + port_ret).cumprod(), port_ret


def lag_one_session(W: pd.DataFrame) -> pd.DataFrame:
    """The same weight path, reached one trading session later.

    The panel is indexed on trading days only, so a single row shift IS one
    session: a Friday rebalance becomes a Monday rebalance, and a Thursday one
    (holiday-Friday week, under the deployed holiday_aware cadence) becomes a
    Friday. The leading row goes flat rather than NaN.
    """
    return W.shift(1).fillna(0.0)


def fill_dates(W: pd.DataFrame) -> pd.DatetimeIndex:
    """Sessions on which the book actually trades under this panel."""
    turn = W.diff().abs().sum(axis=1).fillna(0.0)
    return W.index[turn > 1e-12]


def turnover_per_year(W: pd.DataFrame, eligible: pd.Timestamp) -> float:
    """Annualised one-way turnover over the eligible window.

    Difference on the FULL panel and restrict afterwards. Slicing first would
    hide whatever trade lands on the window's opening session behind the
    leading NaN of `.diff()`, and a fill lag moves that trade one row - so the
    naive order reports a turnover change where only the measurement moved.
    """
    turn = W.diff().abs().sum(axis=1).fillna(0.0)
    turn = turn.loc[turn.index >= eligible]
    years = (turn.index[-1] - turn.index[0]).days / 365.25
    return float(turn.sum()) / years if years > 0 else float("nan")


def stats(eq: pd.Series, eligible: pd.Timestamp) -> dict:
    """Sharpe / CAGR / maxDD on the eligible window, rf = 0.

    Matches the engines' own compute_stats so the baseline leg reproduces the
    published sleeve numbers rather than a parallel convention.
    """
    e = eq.loc[eq.index >= eligible]
    e = e / e.iloc[0]
    daily = e.pct_change().fillna(0)
    years = (e.index[-1] - e.index[0]).days / 365.25
    sharpe = (daily.mean() / daily.std() * math.sqrt(252)
              if daily.std() > 0 else 0.0)
    dd = (e - e.cummax()) / e.cummax()
    return {
        "sharpe": float(sharpe),
        "cagr": float(e.iloc[-1] ** (1.0 / years) - 1.0) if years > 0 else None,
        "total_return": float(e.iloc[-1] - 1.0),
        "vol": float(daily.std() * math.sqrt(252)),
        "max_dd": float(dd.min()),
        "n_days": int(len(e)),
        "years": round(years, 2),
    }


def sharpe_se(years: float) -> float:
    """Standard error of an annualised Sharpe under iid returns, ~sqrt(1/T)."""
    return 1.0 / math.sqrt(years) if years > 0 else float("nan")


# ---------------------------------------------------------------------------
def gap_decomposition(W: pd.DataFrame, rets: pd.DataFrame,
                      eligible: pd.Timestamp) -> dict:
    """The economics of the lag, per rebalance.

    On a fill date f the baseline holds the NEW weights over f -> f+1 while the
    lagged leg still holds the OLD ones, so the return difference is exactly
    (W_new - W_old) . r_{f+1}. Summed, that is the drag; its t-statistic says
    whether it is a systematic cost or weekend noise.
    """
    idx = W.index
    fills = [d for d in fill_dates(W) if d >= eligible]
    contribs, dates = [], []
    for f in fills:
        i = idx.get_loc(f)
        if i + 1 >= len(idx):
            continue                      # no session after the last fill
        dw = W.iloc[i] - W.iloc[i - 1] if i > 0 else W.iloc[i]
        contribs.append(float((dw * rets.iloc[i + 1]).sum()))
        dates.append(idx[i + 1])
    a = np.asarray(contribs, dtype=float)
    n = len(a)
    if n < 2:
        return {"n_rebalances": n}
    mean, sd = float(a.mean()), float(a.std(ddof=1))
    t = mean / (sd / math.sqrt(n)) if sd > 0 else float("nan")
    worst = int(np.argmin(a))
    best = int(np.argmax(a))
    # Tail concentration. A drag carried by a handful of gap days is an
    # exposure to weekend risk, not an expected cost, and must not be quoted
    # as a per-year number. Sum is reported with the largest single event and
    # the largest five removed, so the reader can see which it is.
    order = np.argsort(-np.abs(a))
    top5 = [{"date": dates[i].strftime("%Y-%m-%d"), "contribution": float(a[i])}
            for i in order[:5]]
    total = float(a.sum())
    return {
        "n_rebalances": n,
        "mean_per_rebalance": mean,
        "sd_per_rebalance": sd,
        "t_stat": t,
        "annualised_drag": mean * 52.0,
        "share_positive": float((a > 0).mean()),
        "total_gap": total,
        "total_ex_largest": float(total - a[order[0]]),
        "total_ex_largest5": float(total - a[order[:5]].sum()),
        "largest_abs_events": top5,
        "worst": {"date": dates[worst].strftime("%Y-%m-%d"),
                  "contribution": float(a[worst])},
        "best": {"date": dates[best].strftime("%Y-%m-%d"),
                 "contribution": float(a[best])},
    }


# ---------------------------------------------------------------------------
def run_monday_grid(patch_module, calendar: str, run, eligible,
                    idx: pd.DatetimeIndex) -> tuple:
    """Run the engine unmodified on a W-MON rebalance grid.

    Patches the binding in the module that actually calls it — for sleeve D
    that is run_portfolio, not run_europe_rotation. WS10 documents why; a
    mis-targeted patch is a silent no-op, so the attribute must already exist
    and the resulting grid is asserted to be Monday-dominated below.
    """
    if not hasattr(patch_module, "engine_rebalance_dates"):
        raise AttributeError(
            f"{patch_module.__name__} has no engine_rebalance_dates to patch "
            "- wrong patch target, the Monday grid would silently not apply")
    original = patch_module.engine_rebalance_dates
    patch_module.engine_rebalance_dates = monday_grid_patch(calendar)
    try:
        r = run()
    finally:
        patch_module.engine_rebalance_dates = original
    rd = r["rebalance_dates"]
    rd = rd[rd >= eligible]
    n_mon = int((rd.weekday == 0).sum())     # weekday(): Monday = 0
    if len(rd) == 0 or n_mon / len(rd) < 0.80:
        raise RuntimeError(
            f"Monday grid did not take effect: only {n_mon}/{len(rd)} "
            "rebalance dates are Mondays")
    # A roll must go FORWARD, and the test must not assume how long a closure
    # runs. An earlier version asserted "no later than Wednesday" and failed on
    # Xetra's Christmas 2018 (Mon 24 - Wed 26 Dec all shut, so Thu 27 Dec is
    # the correct next session) - a guard rejecting correct behaviour.
    #
    # The direction-sensitive invariant is ONE decision per ISO week. A
    # backward roll puts a shut Monday's decision on the prior Friday, which
    # lands a second decision in the previous week; a forward roll of any
    # length stays inside its own week.
    weeks = [(d.isocalendar().year, d.isocalendar().week) for d in rd]
    if len(set(weeks)) != len(rd):
        raise RuntimeError(
            f"{len(rd) - len(set(weeks))} ISO weeks carry more than one "
            "rebalance - a roll went backwards into the preceding week")
    wc = weekly_close_check(idx, rd)
    if wc["on_weekly_close"] != wc["checked"]:
        raise RuntimeError(
            f"{wc['checked'] - wc['on_weekly_close']} of {wc['checked']} "
            "rebalances read a mid-week bar rather than the prior weekly "
            "close - the forward-roll property does not hold")
    hist = {int(w): int((rd.weekday == w).sum()) for w in sorted(set(rd.weekday))}
    return r, {"n_rebalances": int(len(rd)), "n_on_monday": n_mon,
               "n_rolled_forward": int(len(rd) - n_mon),
               "weekday_histogram_mon0": hist,
               "signal_bar_on_weekly_close": f"{wc['on_weekly_close']}/"
                                             f"{wc['checked']}"}


def compare(key: str) -> tuple[dict, dict]:
    module, patch_module, closes, eligible, run, label, cal = SLEEVES[key]()
    cost = module.COST_FRAC
    r = run()
    W = r["weights"]
    rets = closes.pct_change().fillna(0)

    # GUARD 1 - the reconstruction must BE the engine, not merely resemble it.
    eq_base, _ = equity_from_weights(W, rets, cost)
    ref = r["equity"]
    common = eq_base.index.intersection(ref.index)
    err = float((eq_base.loc[common] - ref.loc[common]).abs().max())
    if not np.isclose(err, 0.0, atol=1e-10):
        raise RuntimeError(
            f"sleeve {key.upper()}: external reconstruction differs from the "
            f"engine's own equity by {err:.3e} - the return accounting here "
            "does not describe this engine, so the lagged leg is meaningless")

    W_lag = lag_one_session(W)
    eq_lag, _ = equity_from_weights(W_lag, rets, cost)

    # GUARD 2 - the lag must move fills strictly LATER. Shifting the other way
    # would be a look-ahead that flatters the result.
    f_base, f_lag = fill_dates(W), fill_dates(W_lag)
    if len(f_base) != len(f_lag):
        raise RuntimeError(
            f"sleeve {key.upper()}: {len(f_base)} baseline fills vs "
            f"{len(f_lag)} lagged - the shift dropped or created a trade")
    pos_base = [W.index.get_loc(d) for d in f_base]
    pos_lag = [W.index.get_loc(d) for d in f_lag]
    offsets = {b - a for a, b in zip(pos_base, pos_lag)}
    if offsets != {1}:
        raise RuntimeError(
            f"sleeve {key.upper()}: lagged fills are {sorted(offsets)} sessions "
            "after baseline, expected exactly 1 - wrong shift direction or size")

    # GUARD 3 - same trades, one session later, so turnover must not move.
    t_base = turnover_per_year(W, eligible)
    t_lag = turnover_per_year(W_lag, eligible)
    if abs(t_base - t_lag) > 0.02:
        raise RuntimeError(
            f"sleeve {key.upper()}: turnover/yr moved {t_base:.3f} -> "
            f"{t_lag:.3f} - the lag is changing trades, not just their timing")

    # Leg 3 - the live schedule: Friday's signal, Monday's fill.
    r_mon, grid_info = run_monday_grid(patch_module, cal, run, eligible,
                                       closes.index)
    eq_mon = r_mon["equity"]

    s_base = stats(eq_base, eligible)
    s_lag = stats(eq_lag, eligible)
    s_mon = stats(eq_mon, eligible)
    out = {
        "sleeve": key.upper(), "label": label, "calendar": cal,
        "cost_bps": round(cost * 10_000, 2),
        "eligible_start": eligible.strftime("%Y-%m-%d"),
        "last_close": closes.index[-1].strftime("%Y-%m-%d"),
        "reconstruction_max_abs_err": err,
        "annual_turnover": {
            BASELINE: t_base, LAGGED: t_lag,
            MONDAY_GRID: turnover_per_year(r_mon["weights"], eligible)},
        "monday_grid": grid_info,
        "legs": {BASELINE: s_base, LAGGED: s_lag, MONDAY_GRID: s_mon},
        "delta": {
            LAGGED: {k: s_lag[k] - s_base[k] for k in
                     ("sharpe", "cagr", "max_dd", "total_return", "vol")},
            MONDAY_GRID: {k: s_mon[k] - s_base[k] for k in
                          ("sharpe", "cagr", "max_dd", "total_return", "vol")},
        },
        "sharpe_se": sharpe_se(s_base["years"]),
        "gap": gap_decomposition(W, rets, eligible),
    }
    curves = {}
    for name, eq in ((BASELINE, eq_base), (LAGGED, eq_lag), (MONDAY_GRID, eq_mon)):
        w = eq.loc[eq.index >= eligible]
        curves[name] = w / w.iloc[0]
    return out, curves


def blend_effect(curves_by_sleeve: dict) -> dict:
    """35/35/10/20 A:B:C:D. Sharpe is not additive, so the blend delta cannot
    be inferred from the four sleeve deltas - this is the number that would be
    restated."""
    import run_multi_strategy as ms

    rows = {}
    for leg in LEGS:
        eq = {s: curves_by_sleeve[s][leg] for s in ("a", "b", "c", "d")}
        common = eq["a"].index
        for s in ("b", "c", "d"):
            common = common.intersection(eq[s].index)
        norm = {s: (eq[s].loc[common] / eq[s].loc[common].iloc[0]) for s in eq}
        blend = ms.fixed_blend_4way(norm["a"], norm["b"], norm["c"], norm["d"],
                                    0.35, 0.35, 0.10)
        st = ms.compute_stats(blend)
        rows[leg] = {k: v for k, v in st.items()
                     if k in ("sharpe", "cagr", "max_dd", "total_return")}
        years = (common[-1] - common[0]).days / 365.25
        rows[leg]["years"] = round(years, 2)
        rows[leg]["window"] = [common[0].strftime("%Y-%m-%d"),
                               common[-1].strftime("%Y-%m-%d")]
    base = rows[BASELINE]
    return {"legs": rows,
            "delta": {leg: {k: rows[leg][k] - base[k] for k in
                            ("sharpe", "cagr", "max_dd", "total_return")}
                      for leg in (LAGGED, MONDAY_GRID)},
            "sharpe_se": sharpe_se(base["years"])}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sleeve", choices=sorted(SLEEVES), action="append",
                    help="restrict to one sleeve (repeatable)")
    args = ap.parse_args()
    keys = args.sleeve or sorted(SLEEVES)

    results, curves_by_sleeve = [], {}
    for k in keys:
        print(f"\n=== Sleeve {k.upper()} ===", flush=True)
        try:
            res, curves = compare(k)
        except Exception as exc:  # noqa: BLE001
            print(f"  FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
            results.append({"sleeve": k.upper(), "error": str(exc)})
            continue
        results.append(res)
        curves_by_sleeve[k] = curves
        print(f"  {res['label']}  [{res['calendar']}]  "
              f"{res['cost_bps']:.0f}bps  window {res['eligible_start']} -> "
              f"{res['last_close']}")
        g_ = res["monday_grid"]
        print(f"  reconstruction err {res['reconstruction_max_abs_err']:.2e}  "
              f"turnover/yr {res['annual_turnover'][BASELINE]:.2f} / "
              f"{res['annual_turnover'][LAGGED]:.2f} / "
              f"{res['annual_turnover'][MONDAY_GRID]:.2f}")
        print(f"  monday grid: {g_['n_on_monday']}/{g_['n_rebalances']} on a "
              f"Monday, {g_['n_rolled_forward']} rolled forward (shut Monday); "
              f"weekdays {g_['weekday_histogram_mon0']}; signal bar on the "
              f"weekly close {g_['signal_bar_on_weekly_close']}")
        for leg in LEGS:
            s = res["legs"][leg]
            print(f"    {leg:<13} Sharpe {s['sharpe']:+.4f}  "
                  f"CAGR {s['cagr']*100:+6.2f}%  DD {s['max_dd']*100:6.2f}%")
        for leg in (LAGGED, MONDAY_GRID):
            d = res["delta"][leg]
            print(f"    vs deployed: {leg:<12} Sharpe {d['sharpe']:+.4f}  "
                  f"CAGR {d['cagr']*100:+6.2f}pp  DD {d['max_dd']*100:+6.2f}pp"
                  f"   (Sharpe SE ~{res['sharpe_se']:.2f})")
        g = res["gap"]
        if g.get("n_rebalances", 0) > 1:
            print(f"    gap/rebalance {g['mean_per_rebalance']*10_000:+.2f}bps "
                  f"(t={g['t_stat']:+.2f}, n={g['n_rebalances']}), "
                  f"annualised {g['annualised_drag']*100:+.2f}pp")
            print(f"    gap total {g['total_gap']*100:+.2f}%  "
                  f"ex-largest {g['total_ex_largest']*100:+.2f}%  "
                  f"ex-largest-5 {g['total_ex_largest5']*100:+.2f}%  "
                  f"| worst {g['worst']['date']} "
                  f"{g['worst']['contribution']*100:+.2f}%")

    payload = {"sleeves": results,
               "convention": {
                   BASELINE: "deployed: Thu-close signal -> Fri-close fill",
                   LAGGED: "same decision, filled one session later "
                           "(Thu signal -> Mon fill; two sessions stale)",
                   MONDAY_GRID: "live schedule: W-MON grid under "
                                "holiday_aware_next, so the weekly close "
                                "signals and the next session fills (one "
                                "session stale, same as deployed)",
               }}

    if set(curves_by_sleeve) == {"a", "b", "c", "d"}:
        print("\n=== Blend (35/35/10/20 A:B:C:D, pre-overlay) ===", flush=True)
        blend = blend_effect(curves_by_sleeve)
        payload["blend"] = blend
        w = blend["legs"][BASELINE]["window"]
        print(f"  common window {w[0]} -> {w[1]}")
        for leg in LEGS:
            b = blend["legs"][leg]
            print(f"    {leg:<13} Sharpe {b['sharpe']:+.4f}  "
                  f"CAGR {b['cagr']*100:+6.2f}%  DD {b['max_dd']*100:6.2f}%")
        for leg in (LAGGED, MONDAY_GRID):
            d = blend["delta"][leg]
            print(f"    vs deployed: {leg:<12} Sharpe {d['sharpe']:+.4f}  "
                  f"CAGR {d['cagr']*100:+6.2f}pp  DD {d['max_dd']*100:+6.2f}pp"
                  f"   (Sharpe SE ~{blend['sharpe_se']:.2f})")
    else:
        print("\n(blend skipped - needs all four sleeves)")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nWrote {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
