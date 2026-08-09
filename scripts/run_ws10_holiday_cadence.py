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
from rebalance_calendar import LAST_SESSION, SCHEDULED  # noqa: E402

OUT_PATH = SCRIPTS.parent / "data_local" / "ws10_holiday_cadence.json"


# ---------------------------------------------------------------------------
# Mode injection
# ---------------------------------------------------------------------------
def _patched(mode: str):
    """A drop-in replacement for weekly_rebalance_dates pinned to `mode`."""
    def f(trading_index, eligible_start, freq="W-FRI"):
        return rebalance_calendar.weekly_rebalance_dates(
            trading_index, eligible_start, freq, mode=mode
        )
    return f


def _run_mode(patch_module, mode: str, fn):
    """Call `fn()` with `patch_module.weekly_rebalance_dates` pinned to `mode`.

    `patch_module` is the module whose NAMESPACE holds the binding the engine
    actually calls, which is not always the engine you invoke: run_europe_
    rotation imports the run_portfolio FUNCTION, so its calendar call resolves
    inside run_portfolio's namespace. Patching the wrong module would be a
    silent no-op and would report a spurious null result, so the attribute
    must already exist.
    """
    if not hasattr(patch_module, "weekly_rebalance_dates"):
        raise AttributeError(
            f"{patch_module.__name__} has no weekly_rebalance_dates to patch "
            "- wrong patch target, results would be a silent no-op")
    original = patch_module.weekly_rebalance_dates
    patch_module.weekly_rebalance_dates = _patched(mode)
    try:
        return fn()
    finally:
        patch_module.weekly_rebalance_dates = original


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
    return m, m, closes, eligible, run, f"A - top-{K} US sector breadth"


def sleeve_b():
    import run_asset_class_rotation as m
    closes = m.download_prices().dropna()
    eligible = closes.index[m.MA_PERIOD]
    signal = m.compute_signal(closes)
    K = m.HEADLINE_K
    def run():
        return m.run_rotation(closes, signal, m.top_k_by_signal(K), eligible)
    return m, m, closes, eligible, run, f"B - top-{K} asset-class momentum"


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
    return m, m, closes, eligible, run, f"C - top-{K} thematic momentum"


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
    return m, pm, closes, eligible, run, f"D - top-{K} Europe sector breadth"


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
    module, patch_module, closes, eligible, run, label = SLEEVES[key]()

    subs = affected_weeks(closes.index, eligible)
    out = {"sleeve": key.upper(), "label": label,
           "eligible_start": eligible.strftime("%Y-%m-%d"),
           "last_close": closes.index[-1].strftime("%Y-%m-%d"),
           "n_substituted_weeks": len(subs), "substituted_sessions": subs,
           "modes": {}}

    equities = {}
    for mode in (SCHEDULED, LAST_SESSION):
        r = _run_mode(patch_module, mode, run)
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

    s, l = out["modes"][SCHEDULED], out["modes"][LAST_SESSION]
    out["delta"] = {k: (l[k] - s[k]) for k in ("sharpe", "cagr", "max_dd")
                    if k in s and k in l and s[k] is not None}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sleeve", choices=sorted(SLEEVES), action="append",
                    help="restrict to one sleeve (repeatable)")
    args = ap.parse_args()
    keys = args.sleeve or sorted(SLEEVES)

    results = []
    for k in keys:
        print(f"\n=== Sleeve {k.upper()} ===", flush=True)
        try:
            res = compare(k)
        except Exception as exc:  # noqa: BLE001
            print(f"  FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
            results.append({"sleeve": k.upper(), "error": str(exc)})
            continue
        results.append(res)
        print(f"  {res['label']}   window {res['eligible_start']} -> "
              f"{res['last_close']}")
        print(f"  substituted weeks: {res['n_substituted_weeks']}"
              + (f"   {res['substituted_sessions']}"
                 if res["n_substituted_weeks"] else ""))
        for mode in (SCHEDULED, LAST_SESSION):
            m = res["modes"][mode]
            print(f"    {mode:<13} Sharpe {m['sharpe']:+.4f}  "
                  f"CAGR {m['cagr']*100:+6.2f}%  DD {m['max_dd']*100:6.2f}%  "
                  f"turnover/yr {m.get('annual_turnover', float('nan')):.2f}")
        d = res["delta"]
        print(f"    delta         Sharpe {d.get('sharpe', 0):+.4f}  "
              f"CAGR {d.get('cagr', 0)*100:+6.2f}pp  "
              f"DD {d.get('max_dd', 0)*100:+6.2f}pp")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nWrote {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
