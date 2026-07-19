"""WS6b T1 — cost stage: apply the verified friction stack to the trade ledger.

Consumes the stage-1 mechanics cached under ``data_local/ws6b/`` and the
verified published parameters in ``data/ws6b_params.json``, and reports all-in
drag per line and for the PARTIAL-5 set in the WS6 net-Sharpe terms, against
the signed floors (-0.05 base / -0.10 at 2x frictions), plus minimum viable NAV.

Interpretation of the "2x frictions" stress (stated, because the kickoff does
not disambiguate it): the stress doubles the MODELLED TRADING frictions —
commission and half-spread — which is the direct analogue of WS6's 5 bps -> 10
bps sweep. It does NOT double the withholding rates or TERs: those are verified
statutory and published figures, not estimates with error bars, and doubling
them would model a world that does not exist. A supplementary "all-in 2x"
variant that doubles the income leg too is reported alongside, so the reader can
see both.

NAV convention: drag is computed at a CONSTANT notional NAV. The book compounds,
but expressing per-order minimums as a constant-NAV rate is what makes the
"feasibility as a function of NAV" answer legible, and it is the conservative
direction for a book that grows.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from ws6b_friction import (
    PARTIAL_5,
    TRADING_DAYS,
    BrokerSchedule,
    LineEconomics,
    Uncertain,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_LOCAL = PROJECT_ROOT / "data_local" / "ws6b"
PARAMS_PATH = PROJECT_ROOT / "data" / "ws6b_params.json"


def _u(node: dict) -> Uncertain:
    return Uncertain(node["value"], node.get("source", ""),
                     bool(node.get("uncertain", False)), node.get("note", ""))


def load_params(path: Path = PARAMS_PATH) -> dict:
    raw = json.loads(path.read_text(encoding="utf-8"))
    schedules = {
        k: BrokerSchedule(
            name=k,
            per_share=_u(v["per_share"]),
            min_order=_u(v["min_order"]),
            max_pct_value=_u(v["max_pct_value"]),
            fractional_min_applies=bool(v["fractional_min_applies"]),
            source=v.get("source", ""),
            pct_of_value=(_u(v["pct_of_value"]) if v.get("pct_of_value") else None),
            has_max=bool(v.get("has_max", True)),
        )
        for k, v in raw["broker_schedules"].items()
    }
    lines = {
        k: LineEconomics(
            line=k,
            held_instrument=v["held_instrument"],
            proxy_instrument=v["proxy_instrument"],
            held_ter=_u(v["held_ter"]),
            proxy_ter=_u(v["proxy_ter"]),
            gross_yield=_u(v["gross_yield"]),
            fund_level_wht=_u(v["fund_level_wht"]),
            investor_wht_direct=_u(v["investor_wht_direct"]),
            investor_wht_on_e0=_u(v["investor_wht_on_e0"]),
            us_situs=bool(v["us_situs"]),
        )
        for k, v in raw["lines"].items()
    }
    spreads = {k: _u(v) for k, v in raw["half_spread_bps"].items()}
    return {"raw": raw, "schedules": schedules, "lines": lines, "spreads": spreads}


@dataclass
class CostResult:
    arm: str
    nav: float
    commission_usd_total: float
    spread_usd_total: float
    n_orders: int
    n_orders_at_minimum: int
    daily_trading_cost: pd.Series      # as a fraction of NAV
    daily_income_cost: pd.Series
    annual_commission_drag: float
    annual_spread_drag: float
    annual_income_drag: float


def schedule_resolver(schedules: dict[str, BrokerSchedule], lse_lines: set[str],
                      us_schedule: str, lse_schedule: str):
    """Map an INSTRUMENT to the schedule its venue actually charges.

    Commission must follow the instrument, never the ledger. Both arms trade
    the same LSE-listed UCITS lines for the part of the book that is not
    basketed — I0-PARTIAL5 still holds six sector lines and three broad slices
    as ETFs — and both may trade US-listed SOXX. Charging by ledger let the
    same order cost 0.5 bp inside one arm and 5 bp inside the other, which
    flattered whichever arm was assigned the cheaper schedule.
    """
    us, lse = schedules[us_schedule], schedules[lse_schedule]

    def _for(name: str) -> BrokerSchedule:
        return lse if name in lse_lines else us

    return _for


def trading_costs(trades: pd.DataFrame, prices: pd.DataFrame,
                  schedule, half_spread_bps: pd.Series,
                  nav: float, calendar: pd.DatetimeIndex,
                  stress: float = 1.0) -> tuple[pd.Series, dict]:
    """Commission plus half-spread on every order in the ledger.

    ``half_spread_bps`` is indexed by name; a name with no entry uses the
    ledger's ``__default__`` row, and the count of names falling back is
    returned so it can never be a silent substitution.

    The returned series is reindexed onto the FULL trading ``calendar`` with
    zeros on non-rebalance days. Both reasons are load-bearing:
      * an annualised mean taken over rebalance dates alone would treat 389
        weekly dates as if they were 252 trading days a year, inflating every
        drag figure by roughly five times;
      * a rebalance-indexed series added to a daily-indexed one aligns to the
        union and yields NaN on every non-rebalance day, which a downstream
        fillna(0) then silently converts into "no cost" — deleting most of the
        income leg rather than reporting it.
    """
    t = trades.copy()
    px = prices.stack().rename("price").reset_index()
    px.columns = ["date", "name", "price"]
    t = t.merge(px, on=["date", "name"], how="left")

    missing_price = int(t["price"].isna().sum())
    # A missing traded price cannot be guessed. Charge such orders the
    # per-order MINIMUM only (the per-share leg is unknowable) and count them.
    t["price"] = t["price"].fillna(0.0)

    notional = (t["abs_delta"] * nav).to_numpy()
    price = t["price"].to_numpy()

    # ``schedule`` is either a single BrokerSchedule or a callable resolving
    # each instrument to its venue's schedule. The callable form is the correct
    # one; the scalar form is retained for the unit tests.
    if callable(schedule):
        comm = np.zeros(len(t), dtype=float)
        names = t["name"].to_numpy()
        by_sched: dict[int, BrokerSchedule] = {}
        for i, nm in enumerate(names):
            by_sched.setdefault(id(schedule(nm)), schedule(nm))
        for sched in by_sched.values():
            mask = np.array([schedule(nm) is sched for nm in names])
            if mask.any():
                comm[mask] = sched.commission_usd(notional[mask], price[mask])
        min_order = min(float(s.min_order) for s in by_sched.values())
    else:
        comm = schedule.commission_usd(notional, price)
        min_order = float(schedule.min_order)
    comm = comm * stress

    default_hs = float(half_spread_bps.get("__default__", np.nan))
    hs = t["name"].map(half_spread_bps).astype(float)
    n_default = int(hs.isna().sum())
    hs = hs.fillna(default_hs)
    spread = notional * (hs.to_numpy() / 1e4) * stress

    t["commission"] = comm
    t["spread"] = spread
    at_min = int(np.sum(np.isclose(comm / max(stress, 1e-12), min_order)))

    def _daily(col: str) -> pd.Series:
        return (t.groupby("date")[col].sum() / nav
                ).reindex(calendar).fillna(0.0)

    d_comm, d_spread = _daily("commission"), _daily("spread")
    detail = {
        "commission_usd_total": float(comm.sum()),
        "spread_usd_total": float(spread.sum()),
        "n_orders": int(len(t)),
        "n_orders_at_minimum": at_min,
        "n_orders_missing_price": missing_price,
        "n_orders_default_spread": n_default,
        "daily_commission": d_comm,
        "daily_spread": d_spread,
    }
    return d_comm + d_spread, detail


def income_costs(line_books: dict[str, pd.DataFrame],
                 dividends: pd.DataFrame,
                 lines: dict[str, LineEconomics],
                 index: pd.DatetimeIndex) -> tuple[pd.Series, dict]:
    """Daily incremental income/fee drag on I0 vs E0, applied line by line.

    The withholding leg is charged on the ACTUAL daily dividend flow of each
    line's basket (Norgate TOTALRETURN minus CAPITAL return), not on an
    annualised yield assumption, so the timing of the drag matches the timing of
    the income that causes it. The TER leg accrues continuously on the line's
    held weight.
    """
    total = pd.Series(0.0, index=index)
    per_line: dict[str, dict] = {}
    for L, econ in lines.items():
        book = line_books[L].reindex(index).fillna(0.0)
        names = [c for c in book.columns if c in dividends.columns]
        div = dividends.reindex(index)[names].fillna(0.0)
        flow = (book[names] * div).sum(axis=1)          # daily dividend earned
        line_w = book[names].sum(axis=1)

        # Withholding leg: I0 suffers investor_wht_direct; E0 suffers
        # fund_level_wht inside the wrapper plus investor_wht_on_e0 on whatever
        # it distributes (zero for an accumulating UCITS).
        #
        # The distributed fraction is an ANNUAL property of the line — the fund
        # pays its TER out of a year's income, not out of each dividend as it
        # lands. Deriving it daily and clipping at zero would zero the TER
        # offset on every non-dividend day and understate it by roughly the
        # ratio of dividend days to all days.
        e0_income_loss = float(econ.fund_level_wht) * flow
        post_fund_yield = float(econ.gross_yield) * (1.0 - float(econ.fund_level_wht))
        distributed_fraction = (
            max(0.0, 1.0 - float(econ.held_ter) / post_fund_yield)
            if post_fund_yield > 0 else 0.0)
        distribution = flow * (1.0 - float(econ.fund_level_wht)) * distributed_fraction
        e0_income_loss = e0_income_loss + float(econ.investor_wht_on_e0) * distribution
        i0_income_loss = float(econ.investor_wht_direct) * flow
        wht_leg = i0_income_loss - e0_income_loss

        # Fee leg: I0 gives up nothing, E0 pays its held TER but the register
        # already charged it the PROXY's TER, so only the difference is new.
        ter_leg = -line_w * (float(econ.held_ter) - float(econ.proxy_ter)) / TRADING_DAYS

        leg = wht_leg + ter_leg
        total = total.add(leg, fill_value=0.0)
        per_line[L] = {
            "annual_wht_drag": float(wht_leg.mean() * TRADING_DAYS),
            "annual_ter_credit": float(ter_leg.mean() * TRADING_DAYS),
            "annual_total_drag": float(leg.mean() * TRADING_DAYS),
            "mean_line_weight": float(line_w.mean()),
            "gross_yield_on_line_computed": float(
                (flow[line_w > 1e-12] / line_w[line_w > 1e-12]).mean() * TRADING_DAYS),
            "held_instrument": econ.held_instrument,
            "proxy_instrument": econ.proxy_instrument,
            "us_situs_e0": econ.us_situs,
        }
    return total, per_line


def net_sharpe_pair(gross: pd.DataFrame, cost_e0: pd.Series,
                    cost_i0: pd.Series) -> dict:
    def _s(x: pd.Series) -> float:
        sd = x.std(ddof=1)
        return float(x.mean() / sd * np.sqrt(TRADING_DAYS)) if sd else float("nan")

    net_e0 = gross["e0"] - cost_e0.reindex(gross.index).fillna(0.0)
    net_i0 = gross["i0_partial5"] - cost_i0.reindex(gross.index).fillna(0.0)
    s_e0, s_i0 = _s(net_e0), _s(net_i0)
    return {"E0": s_e0, "I0_PARTIAL5": s_i0, "drag": s_e0 - s_i0}
