"""Wrapper eligibility gate — which route to an exposure leaks least per year.

There are three routes to any equity exposure the book wants, not two:

    1. an Irish-domiciled UCITS ETF, traded on LSE
    2. a US-domiciled ETF, traded on a US venue
    3. the constituent stocks, held directly

Total annual leakage for a route is

    TER  +  tax drag on the gross dividend  +  2 * turnover * (commission + half-spread)

and the winner depends on the sleeve's turnover, because the tax term is fixed
per year while the trading term scales with it. That is the whole point of this
script: WS6 and WS6b compared routes at ONE turnover and could not see the
crossover.

What this is NOT
----------------
It carries no return estimate and no Sharpe. The 2026-09-18 revalidation
(register 2026-09-18-breadth-thrust-etf-2..-5) found the return edge from
holding constituents is not measurable at this power and is the same order as
the frictions below. Every quantity here is mechanical and knowable in advance,
which is the only reason a decision may rest on it.

Inputs
------
Fee and tax inputs come from the FROZEN WS6b T1 stack in data/ws6b_params.json,
where each figure carries its source and an `uncertain` flag. Nothing is
re-derived here and nothing unsourced is invented: a route whose inputs are not
present is reported BLOCKED, never estimated.

Concentration comes from the iShares holdings archive in data/raw_ishares.

Usage
-----
    python scripts/wrapper_gate.py                 # PARTIAL-5, at book turnover
    python scripts/wrapper_gate.py --all           # every panel in the archive
    python scripts/wrapper_gate.py --turnover 2.0  # what-if at higher turnover
    python scripts/wrapper_gate.py --crossover     # turnover at which routes swap
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import pathlib
import re
from dataclasses import dataclass

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parent.parent
PARAMS = ROOT / "data" / "ws6b_params.json"
RAW = ROOT / "data" / "raw_ishares"

# Default turnover: the quarterly top-coverage basket measured in the
# 2026-09-18 revalidation, one-way, 0.29x a year. Overridden with --turnover.
DEFAULT_TURNOVER = 0.29

# Coverage target for a direct basket. Fixed here, never tuned per panel.
COVERAGE = 0.90


@dataclass
class Route:
    name: str
    ter: float                 # annual, as a fraction
    tax_rate: float            # total leakage rate applied to the gross dividend
    commission_bps: float      # per side
    half_spread_bps: float     # per side
    us_situs: bool
    uncertain: list            # names of inputs that are not verified

    def leakage(self, gross_yield: float, turnover: float) -> float:
        trading = 2.0 * turnover * (self.commission_bps + self.half_spread_bps) / 10_000.0
        return self.ter + self.tax_rate * gross_yield + trading


def load_params() -> dict:
    if not PARAMS.exists():
        raise SystemExit(f"missing {PARAMS} — the WS6b T1 stack is the only "
                         "sourced input; this script will not invent one")
    return json.loads(PARAMS.read_text(encoding="utf-8"))


def _val(node, key):
    """Return (value, uncertain) for a WS6b parameter node, or (None, True)."""
    n = node.get(key)
    if n is None:
        return None, True
    if isinstance(n, dict):
        return n.get("value"), bool(n.get("uncertain", False))
    return n, False


def read_holdings(f: pathlib.Path) -> dict:
    txt = f.read_bytes().decode("utf-8-sig", errors="replace").splitlines()
    hi = next((i for i, l in enumerate(txt)
               if l.lower().startswith(("ticker,", '"ticker'))), None)
    if hi is None:
        return {}
    out: dict = {}
    for r in csv.DictReader(io.StringIO("\n".join(txt[hi:]))):
        t = (r.get("Ticker") or "").strip().upper()
        if not t or t == "-" or len(t) > 12:
            continue
        if (r.get("Asset Class") or "").strip().lower() != "equity":
            continue
        try:
            w = float((r.get("Weight (%)") or "0").replace(",", ""))
        except ValueError:
            w = 0.0
        if w > 0:
            out[t] = out.get(t, 0.0) + w
    return out


def concentration(panel: str, sample: int = 60) -> tuple:
    """Mean name count, top-20 weight share, and names needed for COVERAGE.

    Sampled evenly across the archive rather than read whole: the figure is a
    structural property of the fund and does not move week to week, and the
    full archive is 10,473 files.
    """
    files = sorted(f for f in RAW.glob(f"{panel}_*.csv")
                   if re.match(rf"^{panel}_\d{{8}}\.csv$", f.name))
    if not files:
        return None, None, None
    step = max(1, len(files) // sample)
    n, t20, ncov = [], [], []
    for f in files[::step]:
        h = read_holdings(f)
        if not h:
            continue
        ws = sorted(h.values(), reverse=True)
        tot = sum(ws)
        n.append(len(ws))
        t20.append(sum(ws[:20]) / tot * 100.0)
        c = 0.0
        for i, w in enumerate(ws, 1):
            c += w
            if c / tot >= COVERAGE:
                ncov.append(i)
                break
    if not n:
        return None, None, None
    return float(np.mean(n)), float(np.mean(t20)), float(np.mean(ncov))


def build_routes(line: dict, sched: dict, spreads: dict, code: str) -> list:
    """The routes actually available for one line, from sourced inputs only."""
    ter, ter_u = _val(line, "held_ter")
    fund_wht, _ = _val(line, "fund_level_wht")
    inv_direct, _ = _val(line, "investor_wht_direct")
    inv_on_e0, _ = _val(line, "investor_wht_on_e0")
    situs = line.get("us_situs")

    if None in (ter, fund_wht, inv_direct, inv_on_e0):
        return []

    # Total dividend leakage holding the FUND: what the fund loses, then what
    # the investor loses on whatever it distributes.
    held_tax = fund_wht + inv_on_e0 * (1.0 - fund_wht)

    us_comm, us_comm_u = _val(sched["ibkr_fixed_us_stocks"], "per_share")
    lse_pct, lse_pct_u = _val(sched["ibkr_lse_etf"], "pct_of_value")

    # Per-share commission expressed in bps needs a share price; the schedule
    # caps at 1% of value and a typical large-cap print puts this near 0.5 bp.
    # Stated as an explicit, flagged assumption rather than silently embedded.
    us_comm_bps = 0.5
    lse_comm_bps = (lse_pct or 0.0) * 10_000.0

    sp_line, sp_line_u = _val(spreads, code)
    sp_def, sp_def_u = _val(spreads, "__default__")
    etf_spread = sp_line if sp_line is not None else sp_def
    etf_spread_u = sp_line_u if sp_line is not None else sp_def_u

    held_unc = []
    if ter_u:
        held_unc.append("TER")
    if lse_pct_u:
        held_unc.append("LSE commission schedule")
    if etf_spread_u:
        held_unc.append("ETF half-spread")

    stock_unc = ["US stock half-spread", "US commission in bps (price-dependent)"]

    held_is_us = bool(situs)
    routes = [
        Route(
            name=("US-domiciled ETF" if held_is_us else "Irish UCITS on LSE"),
            ter=ter,
            tax_rate=held_tax,
            commission_bps=(us_comm_bps if held_is_us else lse_comm_bps),
            half_spread_bps=(sp_def if held_is_us else etf_spread) or 0.0,
            us_situs=held_is_us,
            uncertain=held_unc,
        ),
        Route(
            name="Direct stocks",
            ter=0.0,
            tax_rate=inv_direct,
            commission_bps=us_comm_bps,
            half_spread_bps=sp_def or 0.0,
            us_situs=True,
            uncertain=stock_unc,
        ),
    ]
    return routes


def crossover_turnover(a: Route, b: Route, gross_yield: float):
    """Turnover at which route `a` and route `b` leak equally, or None."""
    fixed = (a.ter + a.tax_rate * gross_yield) - (b.ter + b.tax_rate * gross_yield)
    trade = 2.0 * ((b.commission_bps + b.half_spread_bps)
                   - (a.commission_bps + a.half_spread_bps)) / 10_000.0
    if abs(trade) < 1e-12:
        return None
    t = fixed / trade
    return t if t > 0 else None


def run_exposure(path: pathlib.Path, turnover: float) -> None:
    """Rank N candidate wrappers for ONE exposure, from a T1-format file.

    Used where the choice is between funds rather than between a fund and its
    constituents — the EM tilt, where the direct route is infeasible and the
    file says so rather than leaving it implied.
    """
    p = json.loads(path.read_text(encoding="utf-8"))
    print(f"Exposure: {p['exposure']}")
    print(f"Context : {p['context']}\n")

    us_comm, _ = _val(p, "us_commission_bps")
    scored = []
    for code, r in p["routes"].items():
        ter, ter_u = _val(r, "ter")
        y, y_u = _val(r, "distribution_yield_12m")
        wht, _ = _val(r, "investor_wht_on_distribution")
        spread, spread_u = _val(r, "half_spread_bps")
        comm, comm_u = _val(r, "commission_bps")
        if comm is None:
            comm, comm_u = us_comm, True
        y = y or 0.0
        tax = wht * y
        trade = 2.0 * turnover * (comm + spread) / 10_000.0
        total = ter + tax + trade
        unc = [n for n, u in (("TER", ter_u), ("yield", y_u),
                              ("spread", spread_u), ("commission", comm_u)) if u]
        scored.append((total, code, r, ter, tax, trade, unc))
    scored.sort()
    best = scored[0][0]

    hdr = (f"{'Route':7} {'Domicile':9} {'TER':>6} {'Investor tax':>13} "
           f"{'Trading':>8} {'TOTAL':>7} {'vs best':>8} {'situs':>6}")
    print(hdr)
    print("-" * len(hdr))
    for total, code, r, ter, tax, trade, unc in scored:
        print(f"{code:7} {r['domicile'][:9]:9} {ter*100:5.2f}% {tax*100:12.3f}% "
              f"{trade*100:7.3f}% {total*100:6.3f}% {(total-best)*100:+7.3f}% "
              f"{str(r['us_situs']):>6}")
        if unc:
            print(f"{'':7} {'':9} unverified: {', '.join(unc)}")
    print(f"\nAdvantage of {scored[0][1]} over {scored[-1][1]}: "
          f"{(scored[-1][0]-best)*100:.2f}% a year at {turnover:.2f}x turnover.")

    # Turnover at which the ranking could flip, so the result is not quoted
    # as if it held everywhere.
    a, b = scored[0], scored[-1]
    fixed = (b[3] + b[4]) - (a[3] + a[4])
    ra = a[2]
    rb = b[2]
    ca = (_val(ra, "commission_bps")[0] or us_comm) + _val(ra, "half_spread_bps")[0]
    cb = (_val(rb, "commission_bps")[0] or us_comm) + _val(rb, "half_spread_bps")[0]
    per_turn = 2.0 * (ca - cb) / 10_000.0
    if per_turn > 0:
        print(f"Ranking flips above {fixed / per_turn:.1f}x one-way turnover a year.")
    else:
        print("No crossover: the ranking holds at every turnover.")

    if "direct_route_rejected" in p:
        d = p["direct_route_rejected"]
        print(f"\nDirect replication: {d['verdict']} — {d['note']}")
    if "not_quantified" in p:
        print("\nDeliberately NOT netted into the figures above:")
        for k, v in p["not_quantified"].items():
            print(f"  - {k}: {v['note']}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--turnover", type=float, default=DEFAULT_TURNOVER,
                    help=f"one-way turnover a year (default {DEFAULT_TURNOVER})")
    ap.add_argument("--all", action="store_true",
                    help="include every panel in the holdings archive")
    ap.add_argument("--crossover", action="store_true",
                    help="report the turnover at which the routes swap rank")
    ap.add_argument("--exposure", type=str, default=None,
                    help="path to a T1 exposure file comparing wrappers "
                         "(e.g. data/em_wrapper_params.json)")
    args = ap.parse_args()

    if args.exposure:
        run_exposure(pathlib.Path(args.exposure), args.turnover)
        return

    p = load_params()
    lines, sched, spreads = p["lines"], p["broker_schedules"], p["half_spread_bps"]

    codes = sorted(lines)
    blocked = []
    if args.all:
        found = sorted({m.group(1) for m in
                        (re.match(r"^([A-Z0-9]+)_\d{8}\.csv$", f.name)
                         for f in RAW.glob("*.csv")) if m})
        blocked = [c for c in found if c not in lines]

    print(f"Wrapper gate — total annual leakage at {args.turnover:.2f}x one-way "
          f"turnover, {COVERAGE:.0%} coverage baskets")
    print("Fee/tax inputs: frozen WS6b T1 stack. No return estimate is used.\n")

    hdr = (f"{'Line':6} {'Route':20} {'TER':>6} {'Tax':>7} {'Trade':>7} "
           f"{'TOTAL':>7} {'vs best':>8} {'situs':>6}  notes")
    print(hdr)
    print("-" * len(hdr))

    for code in codes:
        line = lines[code]
        y, y_u = _val(line, "gross_yield")
        routes = build_routes(line, sched, spreads, code)
        if not routes or y is None:
            print(f"{code:6} BLOCKED — inputs missing from the T1 stack")
            continue
        scored = sorted(((r, r.leakage(y, args.turnover)) for r in routes),
                        key=lambda t: t[1])
        best = scored[0][1]
        for r, lk in scored:
            trade = 2.0 * args.turnover * (r.commission_bps + r.half_spread_bps) / 1e4
            note = ""
            if r.uncertain:
                note = "unverified: " + ", ".join(r.uncertain)
            if r.us_situs and not routes[0].us_situs and r.name == "Direct stocks":
                note = ("ADDS US-situs estate exposure. " + note).strip()
            print(f"{code:6} {r.name:20} {r.ter*100:5.2f}% "
                  f"{r.tax_rate*y*100:6.2f}% {trade*100:6.2f}% {lk*100:6.2f}% "
                  f"{(lk-best)*100:+7.2f}% {str(r.us_situs):>6}  {note}")
        if y_u:
            print(f"{'':6} {'':20} gross yield UNVERIFIED — "
                  f"{line['gross_yield'].get('note','').split('.')[0][:70]}")
        if args.crossover:
            t = crossover_turnover(scored[0][0], scored[1][0], y)
            if t is not None:
                print(f"{'':6} {'':20} routes swap at {t:.2f}x one-way turnover a year")
            else:
                print(f"{'':6} {'':20} no crossover — rank holds at every turnover")
        nm, t20, ncov = concentration(code)
        if nm:
            print(f"{'':6} {'':20} basket: {nm:.0f} names, top-20 = {t20:.1f}% "
                  f"of weight, {ncov:.0f} names to {COVERAGE:.0%}")
        print()

    if blocked:
        print("Panels in the archive with NO sourced fee/tax inputs — a T1-style "
              "pull is required\nbefore any of these can be ranked. Concentration "
              "is structural and is shown because\nit needs no fee input:\n")
        print(f"{'Panel':8} {'Names':>6} {'Top20':>8} {'n@' + f'{COVERAGE:.0%}':>7}")
        print("-" * 33)
        for c in blocked:
            nm, t20, ncov = concentration(c)
            if nm:
                print(f"{c:8} {nm:6.0f} {t20:7.1f}% {ncov:7.0f}")
        print("\nNo TER, gross yield, domicile or situs flag is available for the "
              "panels above.\nThey are BLOCKED, not assumed — see the WS6b T1 "
              "method for what a pull requires.")


if __name__ == "__main__":
    main()
