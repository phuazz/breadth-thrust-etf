"""WS6b T1 — build the all-in friction / income / ops model (PARTIAL-5).

Stage 1 (this file's ``build_mechanics``) reconstructs the frozen WS6 I0
construction restricted to the signed PARTIAL-5 adoption set, alongside E0, and
caches the resulting trade ledger to ``data_local/ws6b/`` (git-ignored: the
ledger is derived from Norgate member prices, personal-licence only).

Stage 2 costs that ledger with the verified published friction stack and
reports all-in drag per line and per set in net-Sharpe terms against the signed
floors (-0.05 base / -0.10 at 2x), plus minimum viable NAV.

Run:  python scripts/run_ws6b_t1_friction.py --stage mechanics
      python scripts/run_ws6b_t1_friction.py --stage costs
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import single_name_impl as sni  # noqa: E402
from single_name_impl import (  # noqa: E402
    ARM_BY_ID,
    SINGLE_NAMED_LINES,
    WINDOW_END,
    build_arm_name_weights,
    build_name_return_panel,
    deployed_sector_layer,
    load_constituents,
    load_member_weights,
    precompute_member_signals,
    simulate_arm,
)
from run_ws6_single_name import load_or_fetch_member_prices  # noqa: E402
from ws6b_friction import PARTIAL_5, restricted_to, trade_ledger  # noqa: E402

OUT_DIR = PROJECT_ROOT / "data_local" / "ws6b"
MECHANICS_PATH = OUT_DIR / "book_mechanics.json"
TRADES_E0 = OUT_DIR / "trades_e0.parquet"
TRADES_I0 = OUT_DIR / "trades_i0_partial5.parquet"
GROSS_PATH = OUT_DIR / "gross_daily.parquet"
UNADJ_PATH = OUT_DIR / "prices_unadjusted.parquet"


def _sharpe(daily: pd.Series) -> float:
    sd = daily.std(ddof=1)
    if not np.isfinite(sd) or sd == 0:
        return float("nan")
    return float(daily.mean() / sd * np.sqrt(252))


def _norgate_field(symbols: list[str], start: str, end: str,
                   adjustment: str, fields: tuple[str, ...]
                   ) -> dict[str, pd.DataFrame]:
    """Pull ``fields`` for ``symbols`` under one Norgate adjustment setting.

    Returns {field: panel}. A symbol Norgate cannot resolve is COUNTED and
    reported, never silently dropped (data-integrity rule).
    """
    import norgatedata as nd

    setting = getattr(nd.StockPriceAdjustmentType, adjustment)
    out: dict[str, dict[str, pd.Series]] = {f: {} for f in fields}
    missing: list[str] = []
    for sym in symbols:
        try:
            df = nd.price_timeseries(
                sym, stock_price_adjustment_setting=setting,
                padding_setting=nd.PaddingType.NONE,
                start_date=start, end_date=end,
                timeseriesformat="pandas-dataframe",
            )
        except Exception:  # noqa: BLE001 — unresolvable symbol is counted
            df = None
        if df is None or len(df) == 0 or not set(fields) <= set(df.columns):
            missing.append(sym)
            continue
        idx = pd.to_datetime(df.index).tz_localize(None)
        for f in fields:
            s = pd.Series(df[f].astype(float).values, index=idx)
            out[f][sym] = s[~s.index.duplicated(keep="first")]
    if missing:
        print(f"  [{adjustment}] unresolved ({len(missing)}): {missing[:12]}"
              f"{' ...' if len(missing) > 12 else ''}", flush=True)
    return {f: (pd.DataFrame(c).sort_index() if c else pd.DataFrame())
            for f, c in out.items()}


def fetch_unadjusted_closes(symbols: list[str], start: str, end: str) -> pd.DataFrame:
    """Norgate closes with NO price adjustment — the actual traded price level.

    Share counts (and therefore per-share commission) must come from the price
    the order would really have been filled at, not from a total-return-adjusted
    level, which for a high-yield line like IUES sits materially below the real
    historical price and would overstate share counts and commission.
    """
    return _norgate_field(symbols, start, end, "NONE", ("Close",))["Close"]


def build_mechanics() -> dict:
    started = datetime.now(timezone.utc)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print("WS6b T1 stage 1 — deployed sector book ...", flush=True)
    sector = deployed_sector_layer()
    eligible = sector["eligible"]
    closes = sector["closes"]
    rebal_dates = sector["rebal_dates"]
    print(f"  lines {len(sector['weights'].columns)} | "
          f"{eligible.date()} .. {closes.index.max().date()} | "
          f"rebalances {len(rebal_dates)}", flush=True)

    print("\nStage 1 — member prices / weights (data_local, git-ignored) ...",
          flush=True)
    (prices_by_line, _mapping, _fetch,
     resolution_by_line) = load_or_fetch_member_prices(SINGLE_NAMED_LINES)
    membership = {L: load_constituents(L)["snapshots"] for L in SINGLE_NAMED_LINES}
    member_signals = {L: precompute_member_signals(prices_by_line[L])
                      for L in SINGLE_NAMED_LINES}
    member_weights = {L: load_member_weights(L) for L in SINGLE_NAMED_LINES}

    combined = pd.concat([prices_by_line[L] for L in SINGLE_NAMED_LINES], axis=1)
    combined = combined.loc[:, ~combined.columns.duplicated(keep="first")]
    returns_panel = build_name_return_panel(closes, combined)

    def _build(spec_id: str, adopted: tuple[str, ...] | None):
        spec = ARM_BY_ID[spec_id]
        ctx = restricted_to(adopted) if adopted is not None else _null_ctx()
        with ctx:
            return build_arm_name_weights(
                spec, sector["weights"], closes, rebal_dates, eligible,
                membership, member_signals, prices_by_line,
                member_resolution=resolution_by_line,
                member_weights=member_weights)

    print("\nStage 1 — arm builds ...", flush=True)
    e0 = _build("E0", None)
    i0p5 = _build("I0", PARTIAL_5)
    per_line = {L: _build("I0", (L,)) for L in PARTIAL_5}

    # Parity guard: with zero cost, E0 must reproduce the deployed sleeve equity.
    e0_sim = simulate_arm(e0.name_weights, returns_panel, 0.0)
    i0_sim = simulate_arm(i0p5.name_weights, returns_panel, 0.0)
    deployed = sector["equity"]
    e0_costed = simulate_arm(e0.name_weights, returns_panel, 2.0)["equity"]
    parity = float((e0_costed.reindex(deployed.index) - deployed).abs().max())
    print(f"  E0-vs-deployed parity maxdiff = {parity:.3e}", flush=True)
    if parity > 1e-10:
        raise SystemExit(f"STOP: E0 parity broken ({parity:.3e}) — the "
                         "restricted build is not reproducing the deployed book.")

    # Weight-preservation guard: a PARTIAL adoption set must leave the total
    # book weight identical to E0 on every day (no leakage into or out of the
    # basketed lines).
    leak = float((i0p5.name_weights.sum(axis=1)
                  - e0.name_weights.sum(axis=1)).abs().max())
    print(f"  PARTIAL-5 weight-preservation maxdiff = {leak:.3e}", flush=True)
    if leak > 1e-9:
        raise SystemExit(f"STOP: PARTIAL-5 book leaks weight ({leak:.3e}).")

    # Non-adopted lines must still be held as their own ETF, and the adopted
    # lines must NOT appear as ETF columns in the I0 book.
    held_lines = set(i0p5.name_weights.columns) & set(SINGLE_NAMED_LINES)
    still_etf = sorted(held_lines)
    unexpected = sorted(set(PARTIAL_5) & held_lines)
    print(f"  lines still expressed as ETFs in I0-PARTIAL5: {still_etf}", flush=True)

    print("\nStage 1 — trade ledgers ...", flush=True)
    tr_e0 = trade_ledger(e0.name_weights, rebal_dates)
    tr_i0 = trade_ledger(i0p5.name_weights, rebal_dates)
    tr_e0 = tr_e0[tr_e0["date"] >= eligible]
    tr_i0 = tr_i0[tr_i0["date"] >= eligible]
    print(f"  E0 orders {len(tr_e0):,} | I0-PARTIAL5 orders {len(tr_i0):,}",
          flush=True)

    # Unadjusted price panel for share counts (single names only; the ETF lines
    # are priced off the deployed close panel).
    names = sorted(set(tr_i0["name"]) - set(sector["weights"].columns))
    start = "2017-01-01"
    end = WINDOW_END.strftime("%Y-%m-%d")
    print(f"\nStage 1 — unadjusted Norgate closes for {len(names)} names ...",
          flush=True)
    unadj = fetch_unadjusted_closes(names, start, end)
    # SOXX is the one US-listed line, so it is the one ETF whose orders meet a
    # PER-SHARE schedule and therefore need a real traded price. Take it from
    # Norgate unadjusted; its 2024 15:1 split would otherwise inflate pre-split
    # share counts fifteenfold off the auto-adjusted proxy panel. The remaining
    # thirteen lines are LSE-listed and charged on trade VALUE, where the price
    # column never enters the commission.
    soxx_unadj = fetch_unadjusted_closes(["SOXX"], start, end)
    etf_px = closes.reindex(columns=[c for c in sector["weights"].columns])
    etf_px = etf_px.drop(columns=[c for c in ("SOXX",) if c in etf_px.columns])
    unadj = pd.concat([unadj, soxx_unadj, etf_px], axis=1)
    unadj = unadj.loc[:, ~unadj.columns.duplicated(keep="first")]

    # CAPITAL-adjusted OHLC: split/capital-adjusted but NOT dividend-adjusted.
    # Two uses, both needing this exact basis:
    #  * dividend yield  = daily TOTALRETURN return minus daily CAPITAL return,
    #    which isolates the cash dividend the basket actually throws off. This
    #    is computed from the price data the book itself uses, giving a second,
    #    independent check on the published distribution yields.
    #  * Corwin-Schultz half-spread, which reads high/low ratios and needs an
    #    adjustment basis free of dividend discontinuities.
    print(f"\nStage 1 — CAPITAL-adjusted OHLC for {len(names)} names ...",
          flush=True)
    cap = _norgate_field(names, start, end, "CAPITAL", ("High", "Low", "Close"))

    tr_e0.to_parquet(TRADES_E0)
    tr_i0.to_parquet(TRADES_I0)
    unadj.to_parquet(UNADJ_PATH)
    cap["Close"].to_parquet(OUT_DIR / "prices_capital_close.parquet")
    cap["High"].to_parquet(OUT_DIR / "prices_capital_high.parquet")
    cap["Low"].to_parquet(OUT_DIR / "prices_capital_low.parquet")
    combined.reindex(columns=names).to_parquet(OUT_DIR / "prices_totalreturn.parquet")
    pd.DataFrame({"e0": e0_sim["daily"], "i0_partial5": i0_sim["daily"]}
                 ).to_parquet(GROSS_PATH)

    # Per-line I0 books, so income and fee drags can be applied line by line
    # (SOXX is taxed on a different basis from the four UCITS lines) without
    # having to assume the adopted lines' name sets are disjoint.
    for L, b in per_line.items():
        cols = [c for c in b.name_weights.columns if c not in SINGLE_NAMED_LINES
                or c == L]
        b.name_weights[cols].to_parquet(OUT_DIR / f"book_line_{L.lower()}.parquet")
    e0.name_weights.to_parquet(OUT_DIR / "book_e0.parquet")
    i0p5.name_weights.to_parquet(OUT_DIR / "book_i0_partial5.parquet")

    # Time-weighted average held weight per line (drives the annual income and
    # fee drags — a line only bears its TER/withholding while it is held).
    w = e0.name_weights.loc[e0.name_weights.index >= eligible]
    line_weight_time = {L: float(w[L].mean()) for L in SINGLE_NAMED_LINES
                        if L in w.columns}
    held_share = {L: float((w[L] > 0).mean()) for L in SINGLE_NAMED_LINES
                  if L in w.columns}

    payload = {
        "computed_at_utc": started.isoformat(),
        "registration": "KICKOFF_ws6b-unscreened-replication.md (BINDING)",
        "adoption_set": {"name": "PARTIAL-5", "lines": list(PARTIAL_5)},
        "window": {"eligible": str(eligible.date()),
                   "end": str(closes.index.max().date()),
                   "n_rebalances": int(len(rebal_dates)),
                   "n_days": int(len(w))},
        "guards": {"e0_vs_deployed_parity_maxdiff": parity,
                   "partial5_weight_leak_maxdiff": leak,
                   "lines_still_etf_in_i0": still_etf,
                   "adopted_lines_leaked_as_etf": unexpected},
        "gross_sharpe_zero_cost": {"E0": _sharpe(e0_sim["daily"].loc[eligible:]),
                                   "I0_PARTIAL5": _sharpe(
                                       i0_sim["daily"].loc[eligible:])},
        "orders": {"E0": int(len(tr_e0)), "I0_PARTIAL5": int(len(tr_i0))},
        "line_weight_time": line_weight_time,
        "line_held_share_of_weeks": held_share,
        "fallback_weeks_i0_partial5": {L: int(v) for L, v
                                       in i0p5.fallback_weeks.items()},
        "weeks_evaluated_i0_partial5": {L: int(v) for L, v
                                        in i0p5.weeks_evaluated.items()},
        "weight_carry_weeks": {L: int(v) for L, v in i0p5.weight_carry_weeks.items()},
        "weight_ew_weeks": {L: int(v) for L, v in i0p5.weight_ew_weeks.items()},
        "basket_size_mean": {L: (float(np.mean(v)) if v else None)
                             for L, v in i0p5.basket_sizes.items()},
        "per_line_orders": {L: int(len(trade_ledger(b.name_weights, rebal_dates)
                                       .query("date >= @eligible")))
                            for L, b in per_line.items()},
        "unadjusted_price_coverage": {
            "names_requested": len(names),
            "names_resolved": int(len([c for c in unadj.columns if c in names])),
        },
    }
    MECHANICS_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nWrote {MECHANICS_PATH.relative_to(PROJECT_ROOT)}")
    return payload


class _null_ctx:
    def __enter__(self):
        return None

    def __exit__(self, *a):
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=("mechanics", "costs"), default="mechanics")
    args = ap.parse_args()
    if args.stage == "mechanics":
        p = build_mechanics()
        print("\n--- summary ---")
        print(json.dumps({k: p[k] for k in
                          ("window", "guards", "orders", "gross_sharpe_zero_cost",
                           "line_weight_time", "fallback_weeks_i0_partial5")},
                         indent=2))
        return 0
    return run_costs()


def dividend_panel(basis: str = "capital") -> pd.DataFrame:
    """Daily distribution contribution per name, from two Norgate settings.

    ``basis="capital"``         TOTALRETURN less CAPITAL — every distribution,
                                ordinary plus special plus stock spin-offs.
    ``basis="capitalspecial"``  TOTALRETURN less CAPITALSPECIAL — ordinary cash
                                dividends only.

    NEITHER is exactly the withholding base, and the difference is documented
    rather than hidden. Withholding is due on CASH dividends (ordinary and
    special alike) but not on a stock spin-off, and no Norgate adjustment
    setting splits cash specials from stock specials:

      * "capital" over-charges, by treating spin-offs as income. The three
        material cases in the held set are EQT/Equitrans (2018), EXC/
        Constellation (2022) and DTE/DT Midstream (2021).
      * "capitalspecial" under-charges, by stripping the shale variable
        dividends (DVN, FANG, COP, EOG, PXD) which are genuine cash and do
        bear the 30%.

    Published sector yields reconcile against both and confirm the reading:
    Utilities computes 3.60% on "capital" against a published 3.06%, and 3.15%
    on "capitalspecial"; Energy computes 3.94% and 3.51% against a published
    4.00%. Each basis is wrong on the line where its own artefact bites.

    The default is the CONSERVATIVE one: "capital" over-states I0's withholding
    and therefore penalises the arm under consideration. The two bases differ by
    roughly 0.021%/yr on a 0.205%/yr income leg (about 0.0014 net Sharpe), so
    the choice does not move the verdict. Exact treatment needs per-event
    cash-versus-stock classification and is a T4 refinement if the margin ever
    tightens.
    """
    tr = pd.read_parquet(OUT_DIR / "prices_totalreturn.parquet")
    fname = {"capital": "prices_capital_close.parquet",
             "capitalspecial": "prices_capitalspecial_close.parquet"}[basis]
    cp = pd.read_parquet(OUT_DIR / fname)
    common = sorted(set(tr.columns) & set(cp.columns))
    div = tr[common].pct_change() - cp[common].pct_change()
    return div.where(np.isfinite(div))


def run_costs() -> int:
    from ws6b_costs import (income_costs, load_params, net_sharpe_pair,
                            schedule_resolver, trading_costs)

    params = load_params()
    gross = pd.read_parquet(GROSS_PATH)
    mech = json.loads(MECHANICS_PATH.read_text(encoding="utf-8"))
    eligible = pd.Timestamp(mech["window"]["eligible"])
    gross = gross.loc[gross.index >= eligible]
    idx = gross.index

    tr_e0 = pd.read_parquet(TRADES_E0)
    tr_i0 = pd.read_parquet(TRADES_I0)
    prices = pd.read_parquet(UNADJ_PATH)
    div = dividend_panel()
    line_books = {L: pd.read_parquet(OUT_DIR / f"book_line_{L.lower()}.parquet")
                  for L in PARTIAL_5}

    hs = pd.Series({k: float(v) for k, v in params["spreads"].items()})
    # Commission follows the INSTRUMENT's venue, not the ledger it appears in.
    # Both arms trade the same LSE-listed UCITS lines for the un-basketed part
    # of the book, and both may trade US-listed SOXX.
    lse_lines = set(params["raw"]["venues"]["lse_listed"])
    resolve = schedule_resolver(params["schedules"], lse_lines,
                                params["raw"]["active_schedule"],
                                params["raw"]["active_schedule_etf"])
    schedule = etf_schedule = resolve

    income_daily, per_line = income_costs(line_books, div, params["lines"], idx)
    annual_income = float(income_daily.mean() * 252)
    alt_daily, alt_per_line = income_costs(
        line_books, dividend_panel("capitalspecial"), params["lines"], idx)
    annual_income_alt = float(alt_daily.mean() * 252)

    navs = params["raw"]["nav_grid"]
    rows = []
    for nav in navs:
        for stress, label in ((1.0, "base"), (2.0, "2x_trading"),
                              (2.0, "2x_all_in")):
            c_i0, d_i0 = trading_costs(tr_i0, prices, schedule, hs, nav, idx, stress)
            c_e0, d_e0 = trading_costs(tr_e0, prices, etf_schedule, hs, nav, idx, stress)
            inc = income_daily * (2.0 if label == "2x_all_in" else 1.0)
            sh = net_sharpe_pair(gross, c_e0, c_i0 + inc)
            floor = 0.05 if label == "base" else 0.10
            rows.append({
                "nav": nav, "stress": label,
                "sharpe_E0": sh["E0"], "sharpe_I0": sh["I0_PARTIAL5"],
                "drag": sh["drag"],
                "floor": floor,
                "passes": sh["drag"] <= floor,
                "ann_commission_drag_i0": float(d_i0["daily_commission"].mean() * 252),
                "ann_spread_drag_i0": float(d_i0["daily_spread"].mean() * 252),
                "ann_commission_drag_e0": float(d_e0["daily_commission"].mean() * 252),
                "ann_spread_drag_e0": float(d_e0["daily_spread"].mean() * 252),
                "ann_income_drag": annual_income,
                "orders_i0": d_i0["n_orders"],
                "orders_i0_at_minimum": d_i0["n_orders_at_minimum"],
                "orders_i0_missing_price": d_i0["n_orders_missing_price"],
                "orders_default_spread": d_i0["n_orders_default_spread"],
            })
    grid = pd.DataFrame(rows)

    # --- Schedule bracket ---------------------------------------------------
    # Whether IBKR's per-order minimum applies to a FRACTIONAL order is not
    # resolved by the published wording, and it is the single largest driver of
    # minimum viable NAV. Report the bracket rather than pick one.
    scen = []
    for sched_name in params["raw"]["scenarios"]["schedules"]:
        sc = params["schedules"][sched_name]
        for nav in params["raw"]["nav_grid"]:
            c_i0, di = trading_costs(tr_i0, prices, sc, hs, nav, idx, 1.0)
            c_e0, _ = trading_costs(tr_e0, prices, etf_schedule, hs, nav, idx, 1.0)
            sh = net_sharpe_pair(gross, c_e0, c_i0 + income_daily)
            scen.append({"schedule": sched_name, "nav": nav, "drag": sh["drag"],
                         "passes_base": sh["drag"] <= 0.05,
                         "orders_at_minimum": di["n_orders_at_minimum"],
                         "ann_commission_drag_i0": float(
                             di["daily_commission"].mean() * 252)})
    scen_df = pd.DataFrame(scen)
    min_viable_by_schedule = {}
    for s in scen_df["schedule"].unique():
        ok = scen_df[(scen_df["schedule"] == s) & scen_df["passes_base"]]
        min_viable_by_schedule[s] = (float(ok["nav"].min()) if len(ok) else None)

    def _min_viable(stress_label: str, floor: float) -> float | None:
        """Lowest NAV from which the floor holds AND keeps holding above it."""
        sub = grid[grid["stress"] == stress_label].sort_values("nav")
        ok = sub[sub["drag"] <= floor]
        return float(ok["nav"].iloc[0]) if len(ok) else None

    # --- Breakeven surface --------------------------------------------------
    # The verdict turns on a differential of two half-spreads that are the
    # least well verified numbers in the stack: the LSE UCITS lines E0 really
    # trades, and the US mega-cap names I0 really trades. Rather than let a
    # single assumed pair carry the answer, sweep both and report the frontier.
    etf_lines = [c for c in hs.index if c != "__default__"]
    sweep = []
    for name_hs in params["raw"]["sensitivity"]["name_half_spread_bps"]:
        for etf_hs in params["raw"]["sensitivity"]["ucits_half_spread_bps"]:
            h = hs.copy()
            h["__default__"] = name_hs
            for L in etf_lines:
                h[L] = etf_hs if L != "SOXX" else params["raw"]["sensitivity"][
                    "soxx_half_spread_bps"]
            for nav in params["raw"]["sensitivity"]["nav_points"]:
                c_i0, _ = trading_costs(tr_i0, prices, schedule, h, nav, idx, 1.0)
                c_e0, _ = trading_costs(tr_e0, prices, etf_schedule, h, nav, idx, 1.0)
                sh = net_sharpe_pair(gross, c_e0, c_i0 + income_daily)
                sweep.append({"nav": nav, "name_half_spread_bps": name_hs,
                              "ucits_half_spread_bps": etf_hs,
                              "drag": sh["drag"], "passes_base": sh["drag"] <= 0.05})
    sweep_df = pd.DataFrame(sweep)

    out = {
        "computed_at_utc": datetime.now(timezone.utc).isoformat(),
        "breakeven_sweep": sweep_df.to_dict(orient="records"),
        "registration": "KICKOFF_ws6b-unscreened-replication.md (BINDING)",
        "adoption_set": "PARTIAL-5",
        "floors": {"base": 0.05, "stress_2x": 0.10},
        "stress_interpretation": (
            "2x doubles the modelled TRADING frictions (commission + half-"
            "spread), the direct analogue of WS6's 5 -> 10 bps sweep. Verified "
            "statutory withholding rates and published TERs are NOT doubled."),
        "params_provenance": params["raw"].get("provenance", {}),
        "income_leg": {
            "basis": "capital (conservative: includes stock spin-offs)",
            "annual_drag_total": annual_income, "per_line": per_line,
            "alt_basis": "capitalspecial (ordinary cash dividends only)",
            "annual_drag_total_alt": annual_income_alt,
            "alt_per_line": alt_per_line,
            "basis_spread": annual_income - annual_income_alt},
        "nav_grid": grid.to_dict(orient="records"),
        "schedule_bracket": scen_df.to_dict(orient="records"),
        "min_viable_nav_by_schedule": min_viable_by_schedule,
        "min_viable_nav": {"base_floor_0.05": _min_viable("base", 0.05),
                           "stress_floor_0.10": _min_viable("2x_trading", 0.10),
                           "all_in_2x_floor_0.10": _min_viable("2x_all_in", 0.10)},
    }
    path = OUT_DIR / "t1_friction_results.json"
    path.write_text(json.dumps(out, indent=2, default=float), encoding="utf-8")
    print(f"Wrote {path}")
    print(grid[["nav", "stress", "sharpe_E0", "sharpe_I0", "drag", "floor",
                "passes"]].to_string(index=False))
    print("\nIncome leg per line:")
    print(pd.DataFrame(per_line).T.to_string())
    print(f"\nMinimum viable NAV: {out['min_viable_nav']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
