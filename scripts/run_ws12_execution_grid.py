"""WS12 - execution grid: which weekday, and open or close?

QUESTION
    WS11 established that the deployed Thu-signal / Fri-close convention
    carries no look-ahead, and that moving the whole grid a day is worth about
    a tenth of a Sharpe SE. It left two operational questions unanswered.

    1. The deployed fill is a CLOSE. Every sleeve except D prices on the US
       session, whose close is 04:00 SGT in summer and 05:00 SGT in winter.
       A Singapore operator cannot reliably trade that. The US OPEN is 21:30
       SGT (22:30 winter) - an ordinary evening. What does filling at the open
       instead of the close cost?
    2. Does the choice of weekday matter at all? If the surface is flat the
       day can be chosen on operational grounds, which is the answer that
       actually helps. If it has a peak, someone will be tempted to fit it.

METHOD
    For each weekday grid W-MON .. W-FRI under holiday_aware_next, run each
    deployed sleeve's HEADLINE configuration once and take its daily weight
    panel. From that ONE panel compute two equity curves:

        close fill  W[t-1] . (C_t/C_{t-1})            - the engine's own basis
        open  fill  on the fill day f, the book earns the OLD weights from
                    C_{f-1} to O_f, rebalances at O_f, then earns the NEW
                    weights from O_f to C_f:
                        g_f = growth(w_old, O_f/C_{f-1}) * growth(w_new, C_f/O_f)
                    where growth(w, r) = sum_i w_i r_i + (1 - sum_i w_i), so
                    an under-invested book holds the residual in cash exactly
                    as the engine does.

    The signal is untouched: the engine still reads get_loc(rd) - 1, the prior
    session's close. An open fill therefore uses the SAME decision as the close
    fill on that date and merely executes it about six and a half hours earlier.

    Open prices come through the engines' OWN fetch paths - download_soxx_ohlc
    already returns full OHLC and build_panels simply discards all but Close -
    so nothing is reimplemented. NO engine source is modified and NO deployed
    artefact is written.

WHAT WOULD MAKE THIS SILENTLY WRONG
    1. An open panel that is not the engine's own prices. Mirroring the panel
       builder to keep Open instead of Close could drift from the real one.
       Guarded by rebuilding the CLOSE panel through the same mirror and
       asserting it reproduces the engine's panel exactly; only then is the
       Open panel from that mirror trusted.
    2. A two-stage return formula that does not collapse. Setting O = C must
       make the open leg identical to the close leg, because a rebalance at a
       price equal to the close IS a close fill. Asserted per sleeve.
    3. Costing the open like the close. Opening auctions are wider than
       closing auctions, so a cost stress at 1.5x and 2x is reported beside
       the headline rather than left to the reader.
    4. Reading a weekday peak as signal. Five grids over one history is a
       small surface; the spread across weekdays is reported against the
       Sharpe SE so a 0.05 wiggle is not mistaken for a preference.
    5. Pretending a rebalance can be crossed when it cannot. A rotation sells
       one ETF and buys another; if they sit on different venues the legs are
       hours apart. The venue of every traded symbol is resolved and reported
       per sleeve, so C's US + Shenzhen + crypto mix is visible rather than
       assumed away.

USAGE
    python scripts/run_ws12_execution_grid.py               # all sleeves
    python scripts/run_ws12_execution_grid.py --sleeve b    # one (repeatable)
    python scripts/run_ws12_execution_grid.py --days MON,FRI
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
from run_ws11_fill_lag import stats, sharpe_se, weekly_close_check  # noqa: E402

OUT_PATH = SCRIPTS.parent / "data_local" / "ws12_execution_grid.json"
DASH_PATH = SCRIPTS.parent / "data" / "execution_timing.json"

GRID_DAYS = ["MON", "TUE", "WED", "THU", "FRI"]
FILL_CLOSE, FILL_OPEN = "close", "open"
COST_STRESS = [1.0, 1.5, 2.0]


# ---------------------------------------------------------------------------
# Venue resolution - can a rebalance be crossed at a single moment?
# ---------------------------------------------------------------------------
VENUE_HOURS = {
    # venue: (local open, local close, IANA zone)
    "US": ("09:30", "16:00", "America/New_York"),
    "XETR": ("09:00", "17:30", "Europe/Berlin"),
    "LSE": ("08:00", "16:30", "Europe/London"),
    "SZSE": ("09:30", "15:00", "Asia/Shanghai"),
    "CRYPTO": (None, None, None),
}


def venue_of(symbol: str) -> str:
    if symbol.endswith("-USD"):
        return "CRYPTO"
    if symbol.endswith(".DE"):
        return "XETR"
    if symbol.endswith(".L"):
        return "LSE"
    if symbol.endswith((".SZ", ".SS")):
        return "SZSE"
    return "US"


def traded_symbol_map(key: str, columns) -> dict:
    """Panel column -> the symbol whose prices the backtest actually uses.

    Sleeves A and D carry registry keys as columns (CSP1, EXV1) but price
    through a trading proxy (SPY, EXV1.DE). Reporting the venue of the KEY
    rather than the proxy would say sleeve A trades in London when the
    backtest prices it in New York.
    """
    if key in ("a", "d"):
        from etf_registry import get_etf
        out = {}
        for c in columns:
            try:
                out[c] = get_etf(c).get("yfinance_trading_proxy") or c
            except Exception:  # noqa: BLE001
                out[c] = c
        return out
    return {c: c for c in columns}


def venue_report(key: str, columns) -> dict:
    sym = traded_symbol_map(key, columns)
    venues = {c: venue_of(s) for c, s in sym.items()}
    distinct = sorted(set(venues.values()))
    return {
        "traded_symbols": sym,
        "venues": venues,
        "distinct_venues": distinct,
        "crosses_at_one_moment": len(distinct) == 1,
        "note": ("all legs on one venue, so a rotation crosses at a single "
                 "moment" if len(distinct) == 1 else
                 "legs span " + ", ".join(distinct) + " - a rotation cannot "
                 "cross at a single moment and carries an intraday gap"),
    }


# ---------------------------------------------------------------------------
# Open panels, built through the engines' own fetch paths
# ---------------------------------------------------------------------------
def _ohlc_panel_for(universe: list[str], field: str) -> pd.DataFrame:
    """Mirror of run_portfolio._build_panels_for, keeping `field` not Close.

    Deliberately a mirror rather than a refactor of the engine: the engine is
    deployed and this study must not be able to change it. The mirror is
    verified against the engine's own Close panel before its Open panel is
    used for anything.
    """
    from etf_registry import get_etf
    from run_portfolio import download_soxx_ohlc, load_constituent_prices
    out = {}
    for etf in universe:
        proxy = get_etf(etf).get("yfinance_trading_proxy") or etf
        try:
            cp = load_constituent_prices(etf)
        except FileNotFoundError:
            continue
        dl_start = (cp.index.min() - pd.Timedelta(days=10)).strftime("%Y-%m-%d")
        dl_end = (cp.index.max() + pd.Timedelta(days=5)).strftime("%Y-%m-%d")
        ohlc = download_soxx_ohlc(dl_start, dl_end, etf=proxy, yf_symbol=proxy)
        ohlc = ohlc[~ohlc.index.duplicated(keep="first")]
        out[etf] = ohlc[field].astype(float)
    return pd.DataFrame(out).sort_index()


def _yf_field_panel(tickers: list[str], start: str, end: str,
                    field: str) -> pd.DataFrame:
    import yfinance as yf
    raw = yf.download(tickers, start=start, end=end, auto_adjust=True,
                      progress=False, threads=True, group_by="ticker")
    cols = {}
    for t in tickers:
        if (t, field) in raw.columns:
            cols[t] = raw[(t, field)]
        elif field in raw.columns:
            cols[t] = raw[field]
    df = pd.DataFrame(cols)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df.sort_index()


def open_and_close_mirror(key: str, module) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(open_panel, mirrored_close_panel) on the sleeve's own basis.

    The mirrored close panel exists solely to be checked against the engine's
    real one. If they match, the open panel came off the same path.
    """
    if key == "a":
        import run_portfolio as pm
        return (_ohlc_panel_for(pm.ETFS, "Open"),
                _ohlc_panel_for(pm.ETFS, "Close"))

    if key == "d":
        uni = module.UNIVERSE_EUROPE_SECTORS
        o = module._fx_convert_eur_to_usd(_ohlc_panel_for(uni, "Open"))
        c = module._fx_convert_eur_to_usd(_ohlc_panel_for(uni, "Close"))
        return o, c

    if key == "b":
        needed = module.TICKERS + module.CASH_ONLY_TICKERS
        o = _yf_field_panel(needed, module.START_DATE, module.END_DATE, "Open")
        c = _yf_field_panel(needed, module.START_DATE, module.END_DATE, "Close")
        return o[needed].dropna(), c[needed].dropna()

    if key == "c":
        needed = module.TICKERS + [module.CASH_PROXY]
        out = []
        for field in ("Open", "Close"):
            df = _yf_field_panel(needed, module.START_DATE, module.END_DATE,
                                 field)
            # The engine's OWN transform chain, in the engine's order, so the
            # open panel carries the same crypto calendar, the same FX and the
            # same expense-ratio drag as the close panel it is compared with.
            df = module.cap_to_last_completed_session(df)
            df = module._reindex_crypto_to_equity_calendar(df)
            df = module._fx_convert_to_usd(df)
            df = module._apply_expense_ratio_drag(df)
            out.append(df.dropna(axis=1, how="all"))
        return out[0], out[1]

    raise ValueError(f"no open-panel builder for sleeve {key!r}")


# ---------------------------------------------------------------------------
# Return accounting
# ---------------------------------------------------------------------------
def growth(w: pd.Series, ratio: pd.Series) -> float:
    """Portfolio growth factor for weights w over price ratios `ratio`.

    The residual 1 - sum(w) sits in cash at zero return, matching the engines'
    (W.shift(1) * rets).sum(axis=1) convention exactly.
    """
    invested = float((w * ratio).sum())
    return invested + (1.0 - float(w.sum()))


def open_fill_gross(W: pd.DataFrame, closes: pd.DataFrame,
                    opens: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """(gross growth factor per session, turnover) for an OPEN-fill book.

    Non-fill sessions are the ordinary close-to-close hold and are computed
    vectorised. Only the fill sessions - a few hundred - need the two-stage
    treatment, so this does not loop the whole panel.
    """
    idx = W.index
    cc = (closes / closes.shift(1)).fillna(1.0)
    W_prev = W.shift(1).fillna(0.0)
    gross = (W_prev * cc).sum(axis=1) + (1.0 - W_prev.sum(axis=1))

    turn = W.diff().abs().sum(axis=1).fillna(0.0)
    for t in idx[turn > 1e-12]:
        i = idx.get_loc(t)
        if i == 0:
            continue
        overnight = (opens.iloc[i] / closes.iloc[i - 1]).fillna(1.0)
        intraday = (closes.iloc[i] / opens.iloc[i]).fillna(1.0)
        gross.iloc[i] = (growth(W.iloc[i - 1], overnight)
                         * growth(W.iloc[i], intraday))
    return gross, turn


def equity_from_gross(gross: pd.Series, turn: pd.Series,
                      cost: float) -> pd.Series:
    """Net the trading cost off a gross growth series and compound.

    Split from the gross computation so a cost stress re-runs the arithmetic
    rather than the whole fill model - the trades are identical, only their
    price is being varied.
    """
    return (1.0 + (gross - 1.0) - turn * cost).cumprod()


def equity_open_fill(W: pd.DataFrame, closes: pd.DataFrame,
                     opens: pd.DataFrame, cost: float) -> pd.Series:
    gross, turn = open_fill_gross(W, closes, opens)
    return equity_from_gross(gross, turn, cost)


def equity_close_fill(W: pd.DataFrame, closes: pd.DataFrame,
                      cost: float) -> pd.Series:
    rets = closes.pct_change().fillna(0)
    port = (W.shift(1).fillna(0.0) * rets).sum(axis=1)
    turn = W.diff().abs().sum(axis=1).fillna(0.0)
    return (1.0 + port - turn * cost).cumprod()


# ---------------------------------------------------------------------------
def grid_patch(calendar: str, freq: str):
    """engine_rebalance_dates rebound to `freq` under the forward-roll mode."""
    def f(trading_index, eligible_start, _freq="W-FRI", _cal=None):
        return rebalance_calendar.weekly_rebalance_dates(
            trading_index, eligible_start, freq,
            mode=rebalance_calendar.HOLIDAY_AWARE_NEXT, calendar=calendar)
    return f


def run_on_grid(patch_module, calendar: str, freq: str, run):
    if not hasattr(patch_module, "engine_rebalance_dates"):
        raise AttributeError(
            f"{patch_module.__name__} has no engine_rebalance_dates to patch")
    original = patch_module.engine_rebalance_dates
    patch_module.engine_rebalance_dates = grid_patch(calendar, freq)
    try:
        return run()
    finally:
        patch_module.engine_rebalance_dates = original


def compare(key: str, days: list[str]) -> tuple[dict, dict]:
    module, patch_module, closes, eligible, run, label, cal = SLEEVES[key]()
    cost = module.COST_FRAC

    opens, close_mirror = open_and_close_mirror(key, module)
    opens = opens.reindex(index=closes.index, columns=closes.columns)
    close_mirror = close_mirror.reindex(index=closes.index,
                                        columns=closes.columns)

    # GUARD 1 - the mirror must BE the engine's panel, else its Open sibling
    # is some other series and every open number below is fiction.
    #
    # RELATIVE, not absolute. yfinance recomputes its auto_adjust
    # back-adjustment factors between fetches, so a panel read from the
    # engine's parquet cache and one downloaded now agree to about 2e-6
    # RELATIVE on old bars and exactly on recent ones. An absolute 1e-8 test
    # rejects that as a data difference when it is a rounding vintage; a
    # genuinely wrong series - unadjusted, wrong ticker, wrong currency -
    # differs by orders of magnitude more than the 1e-5 used here.
    err = float((close_mirror - closes).abs().max().max())
    rel = float(((close_mirror - closes).abs() / closes.abs()).max().max())
    if not np.allclose(close_mirror.values, closes.values,
                       rtol=1e-5, atol=0, equal_nan=True):
        raise RuntimeError(
            f"sleeve {key.upper()}: mirrored close panel differs from the "
            f"engine's by {rel:.3e} relative ({err:.3e} absolute) - the open "
            "panel cannot be trusted")

    missing = int(opens.isna().sum().sum() - closes.isna().sum().sum())
    if missing > 0:
        raise RuntimeError(
            f"sleeve {key.upper()}: {missing} open prices missing where a "
            "close exists - the open leg would silently hold those flat")

    out = {
        "sleeve": key.upper(), "label": label, "calendar": cal,
        "cost_bps": round(cost * 10_000, 2),
        "eligible_start": eligible.strftime("%Y-%m-%d"),
        "last_close": closes.index[-1].strftime("%Y-%m-%d"),
        "mirror_max_abs_err": err,
        "mirror_max_rel_err": rel,
        "venue": venue_report(key, closes.columns),
        "grid": {},
    }
    curves: dict = {}

    for day in days:
        r = run_on_grid(patch_module, cal, f"W-{day}", run)
        W = r["weights"]
        rd = r["rebalance_dates"]
        rd = rd[rd >= eligible]

        eq_close = equity_close_fill(W, closes, cost)
        ref_err = float((eq_close - r["equity"]).abs().max())
        if not np.isclose(ref_err, 0.0, atol=1e-10):
            raise RuntimeError(
                f"sleeve {key.upper()} W-{day}: close-fill reconstruction "
                f"differs from the engine by {ref_err:.3e}")

        # GUARD 2 - with O == C the open leg IS the close leg. If this does
        # not hold to floating error the two-stage formula is wrong.
        collapse = equity_open_fill(W, closes, closes, cost)
        c_err = float((collapse - eq_close).abs().max())
        if not np.isclose(c_err, 0.0, atol=1e-10):
            raise RuntimeError(
                f"sleeve {key.upper()} W-{day}: open-fill formula does not "
                f"collapse to the close fill when O=C (max err {c_err:.3e})")

        gross, turn = open_fill_gross(W, closes, opens)
        eq_open = equity_from_gross(gross, turn, cost)
        wc = weekly_close_check(closes.index, rd)

        row = {
            "n_rebalances": int(len(rd)),
            "n_on_scheduled_day": int((rd.weekday ==
                                       GRID_DAYS.index(day)).sum()),
            "signal_bar_on_weekly_close": f"{wc['on_weekly_close']}/"
                                          f"{wc['checked']}",
            "legs": {FILL_CLOSE: stats(eq_close, eligible),
                     FILL_OPEN: stats(eq_open, eligible)},
        }
        row["open_minus_close"] = {
            k: row["legs"][FILL_OPEN][k] - row["legs"][FILL_CLOSE][k]
            for k in ("sharpe", "cagr", "max_dd")}
        # Cost stress on the OPEN leg only: the opening auction is the wider
        # book, and the close leg's cost is the one already in the record.
        row["open_cost_stress"] = {
            f"{m:g}x": stats(equity_from_gross(gross, turn, cost * m),
                             eligible)["sharpe"] for m in COST_STRESS}
        out["grid"][day] = row

        # The 2x-cost open curve is carried through to the blend as well: the
        # whole operational case for an open fill turns on whether the wider
        # opening auction eats the benefit, and that cannot be answered from
        # sleeve-level stress numbers because Sharpe is not additive.
        eq_open_2x = equity_from_gross(gross, turn, cost * 2.0)
        for fill, eq in ((FILL_CLOSE, eq_close), (FILL_OPEN, eq_open),
                         (f"{FILL_OPEN}_2x", eq_open_2x)):
            w = eq.loc[eq.index >= eligible]
            curves[(day, fill)] = w / w.iloc[0]

    eligible_idx = closes.index[closes.index >= eligible]
    out["sharpe_se"] = sharpe_se(
        (eligible_idx[-1] - eligible_idx[0]).days / 365.25)
    return out, curves


def blend_grid(curves_by_sleeve: dict, days: list[str]) -> dict:
    import run_multi_strategy as ms
    rows = {}
    for day in days:
        rows[day] = {}
        for fill in (FILL_CLOSE, FILL_OPEN, f"{FILL_OPEN}_2x"):
            eq = {s: curves_by_sleeve[s][(day, fill)]
                  for s in ("a", "b", "c", "d")}
            common = eq["a"].index
            for s in ("b", "c", "d"):
                common = common.intersection(eq[s].index)
            norm = {s: eq[s].loc[common] / eq[s].loc[common].iloc[0]
                    for s in eq}
            blend = ms.fixed_blend_4way(norm["a"], norm["b"], norm["c"],
                                        norm["d"], 0.35, 0.35, 0.10)
            st = ms.compute_stats(blend)
            rows[day][fill] = {k: v for k, v in st.items()
                               if k in ("sharpe", "cagr", "max_dd",
                                        "total_return")}
            rows[day][fill]["window"] = [common[0].strftime("%Y-%m-%d"),
                                         common[-1].strftime("%Y-%m-%d")]
        rows[day]["open_minus_close"] = {
            k: rows[day][FILL_OPEN][k] - rows[day][FILL_CLOSE][k]
            for k in ("sharpe", "cagr", "max_dd")}
    return rows


def paired_tests(curves_by_sleeve: dict, days: list[str]) -> dict:
    """Paired block-bootstrap tests on the comparisons that drive the decision.

    WHY THIS IS HERE. Elsewhere in this book a Sharpe difference is sanity-
    checked against the ~0.36 UNPAIRED standard error of an absolute Sharpe.
    That is the wrong yardstick for these comparisons: two weekday grids, or
    an open and a close fill, run on the SAME history and are enormously
    correlated, so the standard error of their DIFFERENCE is far smaller than
    the standard error of either level. Judging a paired difference against an
    unpaired SE would wave through a real effect as noise - and, in the other
    direction, invite someone to claim a difference is 'well inside the SE'
    when the pairing was never accounted for.

    Reuses run_phase7_bootstrap's moving-block machinery (block 60 trading
    days, 2000 samples, seed 42) rather than restating a bootstrap convention.
    """
    from run_phase7_bootstrap import (BLOCK_SIZE, N_SAMPLES, RNG_SEED,
                                      paired_bootstrap_diff)
    import run_multi_strategy as ms

    def blend_returns(day: str, fill: str) -> pd.Series:
        eq = {s: curves_by_sleeve[s][(day, fill)] for s in ("a", "b", "c", "d")}
        common = eq["a"].index
        for s in ("b", "c", "d"):
            common = common.intersection(eq[s].index)
        norm = {s: eq[s].loc[common] / eq[s].loc[common].iloc[0] for s in eq}
        blend = ms.fixed_blend_4way(norm["a"], norm["b"], norm["c"],
                                    norm["d"], 0.35, 0.35, 0.10)
        return blend.pct_change().fillna(0.0)

    rng = np.random.default_rng(RNG_SEED)
    out = {"block_size_days": BLOCK_SIZE, "n_samples": N_SAMPLES,
           "seed": RNG_SEED, "tests": {}}

    # 1. Open versus close, on each weekday grid.
    for day in days:
        a = blend_returns(day, FILL_OPEN)
        b = blend_returns(day, FILL_CLOSE)
        common = a.index.intersection(b.index)
        out["tests"][f"{day}: open minus close"] = paired_bootstrap_diff(
            a.loc[common].to_numpy(), b.loc[common].to_numpy(),
            BLOCK_SIZE, N_SAMPLES, rng)

    # 2. The best weekday against the deployed Friday grid, close fill. This
    #    is the comparison someone would be tempted to fit to.
    close_sh = {d: _sharpe_of(blend_returns(d, FILL_CLOSE)) for d in days}
    best = max(close_sh, key=close_sh.get)
    if "FRI" in days and best != "FRI":
        a = blend_returns(best, FILL_CLOSE)
        b = blend_returns("FRI", FILL_CLOSE)
        common = a.index.intersection(b.index)
        out["tests"][f"{best} minus FRI (close, best weekday vs deployed)"] = \
            paired_bootstrap_diff(a.loc[common].to_numpy(),
                                  b.loc[common].to_numpy(),
                                  BLOCK_SIZE, N_SAMPLES, rng)
    out["best_close_weekday"] = best
    out["close_sharpe_by_day"] = close_sh
    return out


def _sharpe_of(daily: pd.Series) -> float:
    sd = float(daily.std())
    return float(daily.mean() / sd * math.sqrt(252)) if sd > 0 else 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sleeve", choices=sorted(SLEEVES), action="append")
    ap.add_argument("--days", default=",".join(GRID_DAYS))
    args = ap.parse_args()
    keys = args.sleeve or sorted(SLEEVES)
    days = [d.strip().upper() for d in args.days.split(",") if d.strip()]
    bad = [d for d in days if d not in GRID_DAYS]
    if bad:
        raise SystemExit(f"unknown grid day(s): {bad}; choose from {GRID_DAYS}")

    results, curves_by_sleeve = [], {}
    for k in keys:
        print(f"\n=== Sleeve {k.upper()} ===", flush=True)
        try:
            res, curves = compare(k, days)
        except Exception as exc:  # noqa: BLE001
            print(f"  FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
            results.append({"sleeve": k.upper(), "error": str(exc)})
            continue
        results.append(res)
        curves_by_sleeve[k] = curves
        v = res["venue"]
        print(f"  {res['label']}  {res['cost_bps']:.0f}bps  "
              f"{res['eligible_start']} -> {res['last_close']}")
        print(f"  venues: {'/'.join(v['distinct_venues'])} - "
              f"{'CROSSES CLEANLY' if v['crosses_at_one_moment'] else 'SPLIT'}")
        print(f"  mirror err {res['mirror_max_rel_err']:.2e} relative "
              f"({res['mirror_max_abs_err']:.2e} abs)")
        print(f"    {'day':4} {'close Sh':>9} {'open Sh':>9} {'delta':>8} "
              f"{'CAGR d':>8} {'open 2x':>8}")
        for day in days:
            g = res["grid"][day]
            print(f"    {day:4} {g['legs'][FILL_CLOSE]['sharpe']:>9.4f} "
                  f"{g['legs'][FILL_OPEN]['sharpe']:>9.4f} "
                  f"{g['open_minus_close']['sharpe']:>+8.4f} "
                  f"{g['open_minus_close']['cagr']*100:>+7.2f}pp "
                  f"{g['open_cost_stress']['2x']:>8.4f}")

    payload = {"sleeves": results, "days": days,
               "cost_stress_multipliers": COST_STRESS}

    if set(curves_by_sleeve) == {"a", "b", "c", "d"}:
        print("\n=== Blend (35/35/10/20 A:B:C:D, pre-overlay) ===", flush=True)
        bl = blend_grid(curves_by_sleeve, days)
        payload["blend"] = bl
        print(f"    {'day':4} {'close Sh':>9} {'open Sh':>9} {'delta':>8} "
              f"{'maxDD open':>11}")
        for day in days:
            print(f"    {day:4} {bl[day][FILL_CLOSE]['sharpe']:>9.4f} "
                  f"{bl[day][FILL_OPEN]['sharpe']:>9.4f} "
                  f"{bl[day]['open_minus_close']['sharpe']:>+8.4f} "
                  f"{bl[day][FILL_OPEN]['max_dd']*100:>10.2f}%")
        sharpes = [bl[d][FILL_OPEN]["sharpe"] for d in days]
        print(f"  weekday spread on the open leg: "
              f"{max(sharpes) - min(sharpes):.4f} Sharpe "
              f"(best {days[int(np.argmax(sharpes))]}, "
              f"worst {days[int(np.argmin(sharpes))]})")
        print(f"  open @2x cost: " + "  ".join(
            f"{d} {bl[d][FILL_OPEN + '_2x']['sharpe']:.4f}" for d in days))

        print("\n=== Paired block bootstrap (the right yardstick) ===",
              flush=True)
        pt = paired_tests(curves_by_sleeve, days)
        payload["paired_tests"] = pt
        print(f"  block {pt['block_size_days']}d, {pt['n_samples']} samples, "
              f"seed {pt['seed']}")
        for name, r in pt["tests"].items():
            if r.get("delta_point") is None:
                print(f"    {name:<52} (no valid samples)")
                continue
            print(f"    {name:<52} delta {r['delta_point']:+.4f}  "
                  f"90% CI [{r['delta_p5']:+.4f}, {r['delta_p95']:+.4f}]  "
                  f"p_better {r['p_better']:.2f}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nWrote {OUT_PATH}")

    write_dashboard_payload(payload)
    return 0


# ---------------------------------------------------------------------------
# Dashboard feed
# ---------------------------------------------------------------------------
# Local session times per venue, resolved with zoneinfo at build time rather
# than written down: the US and Europe shift daylight saving on different
# dates, so the SGT offset is not a constant and hard-coding one is how a
# schedule note goes quietly wrong for several weeks a year.
VENUE_SESSION = {
    "US": ("America/New_York", (9, 30), (16, 0)),
    "XETR": ("Europe/Berlin", (9, 0), (17, 30)),
    "LSE": ("Europe/London", (8, 0), (16, 30)),
    "SZSE": ("Asia/Shanghai", (9, 30), (15, 0)),
}


def _sgt_session_table() -> dict:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    sg = ZoneInfo("Asia/Singapore")
    # One summer and one winter sample date, so both offsets are published
    # instead of whichever happens to apply on the build date.
    samples = {"summer": (2026, 8, 10), "winter": (2026, 1, 12)}
    out = {}
    for venue, (tz, o, c) in VENUE_SESSION.items():
        row = {"timezone": tz,
               "local_open": f"{o[0]:02d}:{o[1]:02d}",
               "local_close": f"{c[0]:02d}:{c[1]:02d}"}
        for season, (y, m, d) in samples.items():
            op = datetime(y, m, d, o[0], o[1], tzinfo=ZoneInfo(tz)).astimezone(sg)
            cl = datetime(y, m, d, c[0], c[1], tzinfo=ZoneInfo(tz)).astimezone(sg)
            row[f"sgt_open_{season}"] = op.strftime("%H:%M")
            row[f"sgt_close_{season}"] = cl.strftime("%H:%M")
            row[f"sgt_close_rolls_{season}"] = cl.date() != op.date()
        out[venue] = row
    return out


def write_dashboard_payload(payload: dict) -> None:
    """Trimmed feed for the dashboard's execution-timing tab.

    Deliberately a projection of the study output, not a second computation:
    every number here is copied from `payload`, so the tab cannot drift from
    the record it claims to display.
    """
    sleeves = [s for s in payload["sleeves"] if "error" not in s]
    if not sleeves:
        print("  (no sleeve results - dashboard payload not written)")
        return
    feed = {
        "as_of": max(s["last_close"] for s in sleeves),
        "days": payload["days"],
        "cost_stress_multipliers": payload["cost_stress_multipliers"],
        "sessions_sgt": _sgt_session_table(),
        "sleeves": [{
            "sleeve": s["sleeve"], "label": s["label"],
            "cost_bps": s["cost_bps"],
            "window": [s["eligible_start"], s["last_close"]],
            "venues": s["venue"]["distinct_venues"],
            "crosses_at_one_moment": s["venue"]["crosses_at_one_moment"],
            "venue_note": s["venue"]["note"],
            "traded_symbols": s["venue"]["traded_symbols"],
            "grid": {d: {
                "close_sharpe": g["legs"][FILL_CLOSE]["sharpe"],
                "open_sharpe": g["legs"][FILL_OPEN]["sharpe"],
                "open_minus_close_sharpe": g["open_minus_close"]["sharpe"],
                "open_minus_close_cagr": g["open_minus_close"]["cagr"],
                "open_sharpe_2x_cost": g["open_cost_stress"]["2x"],
            } for d, g in s["grid"].items()},
        } for s in sleeves],
    }
    if "blend" in payload:
        feed["blend"] = payload["blend"]
    if "paired_tests" in payload:
        feed["paired_tests"] = payload["paired_tests"]
    DASH_PATH.parent.mkdir(parents=True, exist_ok=True)
    DASH_PATH.write_text(json.dumps(feed, indent=2), encoding="utf-8")
    print(f"Wrote {DASH_PATH}")


if __name__ == "__main__":
    raise SystemExit(main())
