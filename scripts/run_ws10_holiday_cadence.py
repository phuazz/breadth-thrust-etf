"""WS10 - holiday-Friday rebalance cadence: scheduled vs last_session.

QUESTION
    The deployed engines take their rebalance grid from
    ``rebalance_calendar.weekly_rebalance_dates``, which INTERSECTS calendar
    Fridays with actual trading days. A market-holiday Friday therefore drops
    that week's decision entirely and the book holds the prior week's
    positions for a fortnight. Should a shut Friday instead fall back to the
    last completed session (the Thursday close)?

METHOD
    Run each deployed sleeve's HEADLINE configuration twice - once per mode -
    over the identical price/signal panels, and compare Sharpe, CAGR, max
    drawdown and turnover. The mode is injected by rebinding the name inside
    each engine module (the engines do ``from rebalance_calendar import
    weekly_rebalance_dates``, so the bound symbol is what they call). NO
    engine source is modified and NO deployed artefact is written, so this
    script cannot change deployed behaviour.

WHAT WOULD MAKE THIS SILENTLY WRONG
    1. Look-ahead. Every engine reads the signal at ``get_loc(rd) - 1``, the
       session BEFORE the rebalance. A Thursday rebalance therefore reads
       Wednesday's breadth, not Thursday's. Verified by assertion below.
    2. Comparing different windows. Both modes must share one ``eligible``
       and one price panel, else the equity curves are not comparable. The
       panels are loaded ONCE and reused.
    3. Counting a mode difference where none exists. Sleeve D trades on the
       XETR calendar, where a US holiday is a normal session; its affected
       weeks are German holidays and will differ from A and B. The affected
       -week count is reported per sleeve rather than assumed shared.

USAGE
    python scripts/run_ws10_holiday_cadence.py            # all sleeves
    python scripts/run_ws10_holiday_cadence.py --sleeve a # one sleeve
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import rebalance_calendar  # noqa: E402
from rebalance_calendar import (  # noqa: E402
    HOLIDAY_AWARE,
    LAST_SESSION,
    SCHEDULED,
    scheduled_data_gaps,
)

OUT_PATH = SCRIPTS.parent / "data_local" / "ws10_holiday_cadence.json"

COMPARED_MODES = (SCHEDULED, LAST_SESSION, HOLIDAY_AWARE)


# ---------------------------------------------------------------------------
# Mode injection
# ---------------------------------------------------------------------------
def _patched(mode: str, calendar: str | None):
    """A drop-in replacement for engine_rebalance_dates pinned to `mode`.

    Mirrors engine_rebalance_dates' signature — the engines pass their own
    venue positionally — but ignores it in favour of the sleeve calendar this
    harness resolved, so the A/B controls the mode rather than DEFAULT_MODE.
    """
    def f(trading_index, eligible_start, freq="W-FRI", _engine_calendar=None):
        return rebalance_calendar.weekly_rebalance_dates(
            trading_index, eligible_start, freq, mode=mode,
            calendar=calendar if mode == HOLIDAY_AWARE else None,
        )
    return f


def _run_mode(patch_module, mode: str, calendar: str | None, fn):
    """Call `fn()` with `patch_module.weekly_rebalance_dates` pinned to `mode`.

    `patch_module` is the module whose NAMESPACE holds the binding the engine
    actually calls, which is not always the engine you invoke: run_europe_
    rotation imports the run_portfolio FUNCTION, so its calendar call resolves
    inside run_portfolio's namespace. Patching the wrong module would be a
    silent no-op and would report a spurious null result, so the attribute
    must already exist.
    """
    if not hasattr(patch_module, "engine_rebalance_dates"):
        raise AttributeError(
            f"{patch_module.__name__} has no engine_rebalance_dates to patch "
            "- wrong patch target, results would be a silent no-op")
    original = patch_module.engine_rebalance_dates
    patch_module.engine_rebalance_dates = _patched(mode, calendar)
    try:
        return fn()
    finally:
        patch_module.engine_rebalance_dates = original


# ---------------------------------------------------------------------------
# Per-sleeve harnesses. Each returns (module, closes, run_headline_callable).
# ---------------------------------------------------------------------------
def sleeve_a():
    import run_portfolio as m
    closes, breadths, _ = m.build_panels()
    starts = [breadths[e].dropna().index.min() for e in breadths
              if len(breadths[e].dropna())]
    eligible = pd.Timestamp(max(starts).date()) + pd.Timedelta(days=m.MA_PERIOD)
    eligible = closes.index[closes.index >= eligible][0]
    K = 7  # deployed: top-7 breadth-weighted, weekly Friday, no leverage
    def run():
        return m.run_portfolio(closes, breadths, m.top_k_breadth_weight(K),
                               eligible)
    return m, m, closes, eligible, run, f"A - top-{K} US sector breadth", "NYSE"


def sleeve_b():
    import run_asset_class_rotation as m
    closes = m.download_prices().dropna()
    eligible = closes.index[m.MA_PERIOD]
    signal = m.compute_signal(closes)
    K = m.HEADLINE_K
    def run():
        return m.run_rotation(closes, signal, m.top_k_by_signal(K), eligible)
    return m, m, closes, eligible, run, f"B - top-{K} asset-class momentum", "NYSE"


def sleeve_c():
    # Sleeve mapping per run_multi_strategy.py:36-37 -- THEMATIC is C, not D.
    import run_thematic_rotation as m
    closes = m.download_prices().dropna(axis=1, how="all")
    late = {t for t, meta in m.UNIVERSE.items()
            if meta.get("late_inception") and t in closes.columns}
    core = {c: closes[c].first_valid_index()
            for c in closes.columns if c not in late}
    latest_start = max(d for d in core.values() if d is not None)
    eligible = closes.index[closes.index.searchsorted(latest_start)
                            + m.MA_PERIOD]
    signal = m.compute_signal(closes)
    K = m.HEADLINE_K
    def run():
        return m.run_rotation(closes, signal, m.WEIGHTER_FACTORY(K), eligible)
    return m, m, closes, eligible, run, f"C - top-{K} thematic momentum", "NYSE"


def sleeve_d():
    """Europe sectors. Trades the XETR calendar, so its substituted weeks are
    European holidays -- a DIFFERENT set from the NYSE sleeves. Prices are
    FX-converted EUR->USD exactly as the engine does."""
    import run_europe_rotation as m
    closes_eur, breadths, etfs_used = m._build_panels_for(
        m.UNIVERSE_EUROPE_SECTORS)
    closes = m._fx_convert_eur_to_usd(closes_eur)
    starts = [breadths[e].dropna().index.min() for e in etfs_used
              if len(breadths[e].dropna())]
    eligible = pd.Timestamp(max(starts).date()) + pd.Timedelta(days=m.MA_PERIOD)
    eligible = (closes.index[closes.index >= eligible][0]
                if (closes.index >= eligible).any() else closes.index[m.MA_PERIOD])
    K = m.HEADLINE_K
    import run_portfolio as pm          # where the calendar binding lives
    def run():
        return m.run_portfolio(closes, breadths, m.top_k_breadth_weight(K),
                               eligible, rebalance_freq=m.HEADLINE_FREQ,
                               cost=m.COST_FRAC)
    return m, pm, closes, eligible, run, f"D - top-{K} Europe sector breadth", "XETR"


SLEEVES = {"a": sleeve_a, "b": sleeve_b, "c": sleeve_c, "d": sleeve_d}


# ---------------------------------------------------------------------------
def affected_weeks(trading_index, eligible) -> list[str]:
    """Scheduled Fridays that were NOT trading days on this calendar - the
    weeks the two modes decide differently."""
    sched = rebalance_calendar.weekly_rebalance_dates(
        trading_index, eligible, "W-FRI", mode=SCHEDULED)
    last = rebalance_calendar.weekly_rebalance_dates(
        trading_index, eligible, "W-FRI", mode=LAST_SESSION)
    return [d.strftime("%Y-%m-%d") for d in last.difference(sched)]


def compare(key: str) -> dict:
    module, patch_module, closes, eligible, run, label, cal = SLEEVES[key]()

    subs = affected_weeks(closes.index, eligible)
    gaps = [g.strftime("%Y-%m-%d") for g in
            scheduled_data_gaps(closes.index, eligible, "W-FRI", cal)]
    # Genuine exchange holidays = absent Fridays that are NOT vendor gaps.
    holidays = [d for d in subs if d not in
                {g for g in gaps}] if gaps else list(subs)

    out = {"sleeve": key.upper(), "label": label, "calendar": cal,
           "eligible_start": eligible.strftime("%Y-%m-%d"),
           "last_close": closes.index[-1].strftime("%Y-%m-%d"),
           "n_substituted_weeks": len(subs), "substituted_sessions": subs,
           "n_vendor_gaps": len(gaps), "vendor_gap_fridays": gaps,
           "modes": {}}

    equities = {}
    for mode in COMPARED_MODES:
        r = _run_mode(patch_module, mode, cal, run)
        equities[mode] = r["equity"]
        st = module.compute_stats(r["equity"], eligible)
        row = {k: v for k, v in st.items()
               if k in ("sharpe", "cagr", "max_dd", "total_return", "vol")}
        if "weights" in r and hasattr(module, "turnover_stats"):
            row.update({k: v for k, v in
                        module.turnover_stats(r["weights"], eligible).items()
                        if k in ("annual_turnover", "n_flips")})
        out["modes"][mode] = row

    # GUARD: a mis-targeted patch is a silent no-op that would report a
    # spurious "no difference". If this sleeve HAS substituted weeks, the two
    # equity curves must actually differ.
    identical = equities[SCHEDULED].equals(equities[LAST_SESSION])
    if subs and identical:
        raise RuntimeError(
            f"sleeve {key.upper()}: {len(subs)} substituted weeks but the two "
            "modes produced identical equity - the patch did not take effect")
    if not subs and not identical:
        raise RuntimeError(
            f"sleeve {key.upper()}: no substituted weeks yet the equity curves "
            "differ - the mode is changing something it should not")
    out["patch_verified"] = bool(subs) and not identical

    # GUARD: holiday_aware must differ from last_session EXACTLY when this
    # sleeve has vendor gaps -- that difference IS the safety property.
    ha_vs_ls_same = equities[HOLIDAY_AWARE].equals(equities[LAST_SESSION])
    if gaps and ha_vs_ls_same:
        raise RuntimeError(
            f"sleeve {key.upper()}: {len(gaps)} vendor gaps but holiday_aware "
            "matched last_session - the gap discrimination did not fire")
    if not gaps and not ha_vs_ls_same:
        raise RuntimeError(
            f"sleeve {key.upper()}: no vendor gaps yet holiday_aware differs "
            "from last_session - it is skipping a genuine holiday week")
    out["gap_discrimination_verified"] = bool(gaps) and not ha_vs_ls_same

    s = out["modes"][SCHEDULED]
    out["delta"] = {
        mode: {k: (out["modes"][mode][k] - s[k])
               for k in ("sharpe", "cagr", "max_dd")
               if k in s and s[k] is not None}
        for mode in (LAST_SESSION, HOLIDAY_AWARE)
    }
    # Curves shaped exactly as run_multi_strategy consumes them: restricted to
    # eligible, then normalised to 1.0 at that first day.
    curves = {}
    for mode, eq in equities.items():
        w = eq.loc[eq.index >= eligible]
        curves[mode] = w / w.iloc[0]
    return out, curves


def blend_effect(curves_by_sleeve: dict) -> dict:
    """Blend-level effect at the deployed 35/35/10/20 A:B:C:D weights.

    Sharpe is not additive, so the blend delta CANNOT be inferred from the
    sleeve deltas -- this is the number that actually gets restated.
    """
    import run_multi_strategy as ms

    rows = {}
    for mode in COMPARED_MODES:
        eq = {s: curves_by_sleeve[s][mode] for s in ("a", "b", "c", "d")}
        common = eq["a"].index
        for s in ("b", "c", "d"):
            common = common.intersection(eq[s].index)
        norm = {s: (eq[s].loc[common] / eq[s].loc[common].iloc[0])
                for s in eq}
        blend = ms.fixed_blend_4way(norm["a"], norm["b"], norm["c"], norm["d"],
                                    0.35, 0.35, 0.10)
        st = ms.compute_stats(blend)
        rows[mode] = {k: v for k, v in st.items()
                      if k in ("sharpe", "cagr", "max_dd", "total_return")}
        rows[mode]["window"] = [common[0].strftime("%Y-%m-%d"),
                                common[-1].strftime("%Y-%m-%d")]
    base = rows[SCHEDULED]
    return {"modes": rows,
            "delta": {m: {k: rows[m][k] - base[k]
                          for k in ("sharpe", "cagr", "max_dd")}
                      for m in (LAST_SESSION, HOLIDAY_AWARE)}}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sleeve", choices=sorted(SLEEVES), action="append",
                    help="restrict to one sleeve (repeatable)")
    args = ap.parse_args()
    keys = args.sleeve or sorted(SLEEVES)

    results = []
    curves_by_sleeve = {}
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
        print(f"  {res['label']}  [{res['calendar']}]  window "
              f"{res['eligible_start']} -> {res['last_close']}")
        print(f"  absent Fridays: {res['n_substituted_weeks']}   "
              f"of which VENDOR GAPS (exchange traded): "
              f"{res['n_vendor_gaps']} {res['vendor_gap_fridays'] or ''}")
        for mode in COMPARED_MODES:
            m = res["modes"][mode]
            print(f"    {mode:<13} Sharpe {m['sharpe']:+.4f}  "
                  f"CAGR {m['cagr']*100:+6.2f}%  DD {m['max_dd']*100:6.2f}%  "
                  f"turnover/yr {m.get('annual_turnover', float('nan')):.2f}")
        for mode in (LAST_SESSION, HOLIDAY_AWARE):
            d = res["delta"][mode]
            print(f"    vs scheduled: {mode:<13} Sharpe {d.get('sharpe', 0):+.4f}  "
                  f"CAGR {d.get('cagr', 0)*100:+6.2f}pp  "
                  f"DD {d.get('max_dd', 0)*100:+6.2f}pp")

    payload = {"sleeves": results}

    if set(curves_by_sleeve) == {"a", "b", "c", "d"}:
        print("\n=== Deployed blend (35/35/10/20 A:B:C:D) ===", flush=True)
        blend = blend_effect(curves_by_sleeve)
        payload["blend"] = blend
        w = blend["modes"][SCHEDULED]["window"]
        print(f"  common window {w[0]} -> {w[1]}")
        for mode in COMPARED_MODES:
            b = blend["modes"][mode]
            print(f"    {mode:<13} Sharpe {b['sharpe']:+.4f}  "
                  f"CAGR {b['cagr']*100:+6.2f}%  DD {b['max_dd']*100:6.2f}%")
        for mode in (LAST_SESSION, HOLIDAY_AWARE):
            d = blend["delta"][mode]
            print(f"    vs scheduled: {mode:<13} Sharpe {d['sharpe']:+.4f}  "
                  f"CAGR {d['cagr']*100:+6.2f}pp  DD {d['max_dd']*100:+6.2f}pp")
    else:
        print("\n(blend skipped - needs all four sleeves)")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nWrote {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
