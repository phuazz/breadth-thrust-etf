"""WS6 T3 — registered run of the single-name implementation register.

Runs the frozen §2 eight-arm register ONCE (KICKOFF_ws6-single-name-
implementation.md, signed 2026-07-17) after the two FAIL_STOP gates, and writes
DERIVED statistics only to data/ws6_results.json. This harness computes and
reports; it does NOT interpret results or apply the verdict rule (that is T4, on
a different model). No configuration outside the eight-row register is computed,
and no arm definition, cost, window or gate threshold is tunable here — all are
frozen in the committed engine (scripts/single_name_impl.py, commit b12d0f9) and
the kickoff.

Order of execution (STOP protocol at each gate — no design-around):
  1. Data stage — Norgate TOTALRETURN member prices for all 11 single-named
     lines' full in-window membership union, cached under the git-ignored
     data_local/ws6/ tree (licence guard: raw vendor series never committed).
     NDU down or an empty fetch -> STOP.
  2. Gate G1 (coverage) — per line x calendar year, the share of snapshot
     member-weeks with a Norgate price must be >= 97%; unmapped names count
     against coverage. Any line-year below -> STOP (write gate report, no arms).
  3. Gate G2 (replication sanity) — I0-vs-E0 weekly return correlation per line
     over weeks the line is held, gated for lines held >= 26 weeks, must be
     >= 0.95. Any gated line below -> STOP.
  4. Register run — the eight arms at the frozen cost sweep {2, 5, 10, 20} bps
     one-way on the full name-level weight vector (E0 keeps the deployed cost
     model), window 2018-Q4 -> 2026-Q2, sleeve level, K = 7 canonical.

Architecture: every arm shares ONE deployed Phase 20.1 sector book
(deployed_sector_layer); arms differ only in how a held single-named line's
weight is expressed. E0 through the name-level machinery reproduces the deployed
sleeve to 0.0 (asserted below before any results are written — the parity
anchor). Norgate prices touch only the basket side, never the breadth signal.

Dates via pandas only; Python datetime months are 1-indexed (no month
arithmetic is performed here — the rebalance calendar and split boundary use
pandas Timestamp comparisons). British / Singapore English throughout.

Run: python scripts/run_ws6_single_name.py
Output: data/ws6_results.json
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
OUT_PATH = DATA_DIR / "ws6_results.json"

sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

from single_name_impl import (  # noqa: E402  (the committed T2 engine)
    ARM_REGISTER,
    ARM_BY_ID,
    BROAD_SLICES,
    SINGLE_NAMED_LINES,
    M_POOL,
    N_SELECT,
    N_NEIGHBOUR,
    MIN_PASS,
    K_DEPLOYED,
    TREND_MA,
    PLACEBO_MOM_DAYS,
    COST_SWEEP_BPS,
    BINDING_COST_BPS,
    DEPLOYED_COST_BPS,
    WINDOW_END,
    DATA_LOCAL_WS6,
    build_arm_name_weights,
    build_name_return_panel,
    deployed_sector_layer,
    fetch_member_prices,
    line_member_universe,
    load_constituents,
    member_cache_path,
    precompute_member_signals,
    simulate_arm,
)
from run_improvements import compute_stats  # noqa: E402  (deployed stats helper)

# ---------------------------------------------------------------------------
# Frozen run parameters (kickoff §2 [GATES] / verdict rule; nothing tunable)
# ---------------------------------------------------------------------------

ENGINE_COMMIT = "dbb6543"          # scripts/single_name_impl.py at T2 + A1 + A2
COVERAGE_MIN = 0.97                # G1 threshold
G2_CORR_MIN = 0.95                 # G2 threshold
G2_MIN_WEEKS = 26                  # lines held fewer than this are not gated
# G1 survivorship read: a member-week is covered iff its Norgate symbol has a
# non-NaN TOTALRETURN close on at least one of the trailing rows up to the
# snapshot's as-of date (robust to a member trading a slightly different day and
# to the delisting week). Unmapped names have no column -> counted uncovered.
COVERAGE_ASOF_ROWS = 5
FETCH_START = "2017-01-01"         # pre-window warm-up for SMA200 / 126d momentum
SPLIT_BOUNDARY = pd.Timestamp("2022-09-08")   # split-half boundary (kickoff)
TRADING_DAYS = 252
TRADING_WEEKS = 52

SCREENED_ARMS = ("I1", "I2", "P2", "I2-N15", "P2-N15", "I1-all")


def _safe(v):
    """JSON-safe float, or None for NaN/Inf/non-numeric (WS5 convention)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(f) or math.isinf(f)) else f


def _weekly_returns(daily: pd.Series) -> pd.Series:
    """Compound daily net returns to W-FRI weekly returns."""
    return (1.0 + daily).resample("W-FRI").prod() - 1.0


def _ann_sharpe(daily: pd.Series) -> float | None:
    """Annualised Sharpe on a daily net-return series (ddof=1, matching
    run_improvements.compute_stats)."""
    d = daily.dropna()
    if len(d) < 5:
        return None
    sd = float(d.std())
    if sd == 0.0:
        return None
    return float(d.mean() / sd * math.sqrt(TRADING_DAYS))


# ---------------------------------------------------------------------------
# Stage 1 — Norgate member-price data (licence-guarded cache under data_local)
# ---------------------------------------------------------------------------

def _meta_path(line: str) -> Path:
    return DATA_LOCAL_WS6 / f"prices_{line.lower()}.meta.json"


def load_or_fetch_member_prices(lines: tuple[str, ...]):
    """For each single-named line, fetch (or reuse a cache of) Norgate
    TOTALRETURN closes for the FULL in-window membership union, delisted names
    included — requested at INSTRUMENT level (amendment A1: delisted members
    under their suffixed symbols, recycled tickers era-disambiguated). Cache +
    sidecar meta live under the git-ignored data_local/ws6/.

    A cache is reused only when its meta records the identical requested-symbol
    union and fetch window, so a stale pre-A1 cache (base-ticker union) is
    correctly re-fetched. Returns (prices_by_line, mapping_reports,
    fetch_reports, resolution_by_line); the resolution maps feed select_basket
    and G1. Raises on an empty fetch (NDU down surfaces via _norgate())."""
    DATA_LOCAL_WS6.mkdir(parents=True, exist_ok=True)
    end = WINDOW_END.strftime("%Y-%m-%d")
    prices: dict[str, pd.DataFrame] = {}
    mapping_reports: dict[str, dict] = {}
    fetch_reports: dict[str, dict] = {}
    resolution_by_line: dict[str, dict] = {}
    for line in lines:
        symbols, mrep = line_member_universe(line, WINDOW_END)
        resolution_by_line[line] = mrep["resolution"]
        mapping_reports[line] = {
            "n_ishares_unique": mrep["n_ishares_unique"],
            "n_instruments": mrep["n_instruments"],
            "n_member_weeks": mrep["n_member_weeks"],
            "n_resolved_weeks": mrep["n_resolved_weeks"],
            "unresolved": mrep["unresolved"],
        }
        cache = member_cache_path(line)
        meta = _meta_path(line)
        reused = False
        if cache.exists() and meta.exists():
            m = json.loads(meta.read_text(encoding="utf-8"))
            if (m.get("requested_symbols") == symbols
                    and m.get("fetch_start") == FETCH_START
                    and m.get("fetch_end") == end):
                prices[line] = pd.read_parquet(cache)
                fetch_reports[line] = {**m, "source": "cache"}
                reused = True
                print(f"  {line:<6} cache reuse  cols={prices[line].shape[1]:>4} "
                      f"req={len(symbols)} uncovered={len(m.get('uncovered', []))}",
                      flush=True)
        if not reused:
            rep: dict = {}
            panel = fetch_member_prices(symbols, FETCH_START, end, report=rep)
            if panel.empty:
                raise RuntimeError(
                    f"{line}: Norgate returned an empty panel for "
                    f"{len(symbols)} requested symbols")
            panel.to_parquet(cache)
            fr = {"source": "fetch", "n_requested": len(symbols),
                  "n_resolved": len(rep.get("resolved", [])),
                  "n_uncovered": len(rep.get("uncovered", [])),
                  "uncovered": rep.get("uncovered", []),
                  "requested_symbols": symbols,
                  "fetch_start": FETCH_START, "fetch_end": end,
                  "panel_shape": list(panel.shape),
                  "panel_start": str(panel.index.min().date()),
                  "panel_end": str(panel.index.max().date())}
            meta.write_text(json.dumps(fr, indent=2), encoding="utf-8")
            prices[line] = panel
            fetch_reports[line] = fr
            print(f"  {line:<6} FETCH        cols={panel.shape[1]:>4} "
                  f"req={len(symbols)} uncovered={fr['n_uncovered']} "
                  f"unresolved_names={len(mrep['unresolved'])}", flush=True)
    return prices, mapping_reports, fetch_reports, resolution_by_line


# ---------------------------------------------------------------------------
# Gate G1 — coverage (survivorship through the feed and the ticker mapping)
# ---------------------------------------------------------------------------

def compute_g1(lines, membership, prices_by_line, eligible, resolution_by_line):
    """Per line x calendar year, the share of snapshot member-weeks (snapshot
    date in [eligible, WINDOW_END]) whose RESOLVED Norgate instrument (amendment
    A1) has a non-NaN close within the trailing COVERAGE_ASOF_ROWS rows up to
    the snapshot as-of date. Unresolved/ambiguous names resolve to None ->
    uncovered. Returns (table, failing_cells).
    table[line][str(year)] = {covered, total, share}."""
    table: dict[str, dict] = {}
    failing: list[dict] = []
    for line in lines:
        panel = prices_by_line[line]
        idx = panel.index
        cols = set(panel.columns)
        snaps = membership[line]
        res_line = resolution_by_line[line]
        per_year: dict[int, dict] = {}
        for key in sorted(snaps.keys()):
            W = pd.Timestamp(key)
            if W < eligible or W > WINDOW_END:
                continue
            yr = W.year
            pos = int(idx.searchsorted(W, side="right")) - 1
            d = per_year.setdefault(yr, {"covered": 0, "total": 0})
            if pos >= 0:
                lo = max(0, pos - (COVERAGE_ASOF_ROWS - 1))
                window = panel.iloc[lo:pos + 1]
            else:
                window = panel.iloc[0:0]
            row_res = res_line.get(W, {})
            for ish in snaps[key].get("tickers", []):
                sym = row_res.get(ish)
                d["total"] += 1
                if (sym is not None and sym in cols and pos >= 0
                        and bool(window[sym].notna().any())):
                    d["covered"] += 1
        line_tbl = {}
        for yr, v in sorted(per_year.items()):
            share = (v["covered"] / v["total"]) if v["total"] else None
            line_tbl[str(yr)] = {"covered": int(v["covered"]),
                                 "total": int(v["total"]), "share": _safe(share)}
            if share is not None and share < COVERAGE_MIN:
                failing.append({"line": line, "year": int(yr),
                                "covered": int(v["covered"]),
                                "total": int(v["total"]), "share": _safe(share)})
        table[line] = line_tbl
    return table, failing


# ---------------------------------------------------------------------------
# Line-isolated books — the shared machinery for G2 and per-line breakdowns
# ---------------------------------------------------------------------------

def _line_book(sector_weights: pd.DataFrame, line: str, normalise: bool) -> pd.DataFrame:
    """A one-column sector book for ``line``: 1.0 where the sector book holds the
    line (normalise=True, a pure 100%-into-the-line probe for replication) or the
    line's actual sector weight (normalise=False, the additive P&L contribution)."""
    if line in sector_weights.columns:
        col = sector_weights[line]
    else:
        col = pd.Series(0.0, index=sector_weights.index)
    vals = (col > 0).astype(float) if normalise else col.clip(lower=0.0)
    return pd.DataFrame({line: vals})


def line_isolated_daily(spec, sector, membership, member_signals, member_prices,
                        returns_panel, line, normalise, resolution):
    """Daily 0-cost return series of a single line's book (E0 = the ETF line,
    else the arm's member basket). Returns (daily, invested_mask)."""
    book = _line_book(sector["weights"], line, normalise)
    mm, ms, mp, mr = ({}, {}, {}, {}) if spec.is_etf_baseline else (
        membership, member_signals, member_prices, resolution)
    build = build_arm_name_weights(spec, book, sector["closes"],
                                   sector["rebal_dates"], sector["eligible"],
                                   mm, ms, mp, member_resolution=mr)
    sim = simulate_arm(build.name_weights, returns_panel, cost_bps=0.0)
    invested = build.name_weights.abs().sum(axis=1) > 0
    return sim["daily"], invested


def _held_weekly(daily: pd.Series, invested: pd.Series, eligible) -> pd.Series:
    """Weekly returns over weeks the book is invested (return-active =
    invested.shift(1)), restricted to the evaluation window."""
    d = daily.loc[daily.index >= eligible]
    active = invested.shift(1).fillna(False).loc[d.index]
    wk = _weekly_returns(d)
    held = active.resample("W-FRI").max()
    held = held.reindex(wk.index).fillna(False).astype(bool)
    return wk.loc[held]


def compute_g2(lines, sector, membership, member_signals, member_prices,
               returns_panel, resolution):
    """Per line, weekly return correlation of the I0 basket vs the E0 ETF over
    weeks the line is held. Lines held >= G2_MIN_WEEKS are gated at G2_CORR_MIN.
    Returns (table, failing_cells)."""
    e0, i0 = ARM_BY_ID["E0"], ARM_BY_ID["I0"]
    table: dict[str, dict] = {}
    failing: list[dict] = []
    for line in lines:
        e_daily, e_inv = line_isolated_daily(e0, sector, membership, member_signals,
                                             member_prices, returns_panel, line,
                                             True, resolution)
        i_daily, i_inv = line_isolated_daily(i0, sector, membership, member_signals,
                                             member_prices, returns_panel, line,
                                             True, resolution)
        e_wk = _held_weekly(e_daily, e_inv, sector["eligible"])
        i_wk = _held_weekly(i_daily, i_inv, sector["eligible"])
        common = e_wk.index.intersection(i_wk.index)
        n = int(len(common))
        corr = (float(i_wk.loc[common].corr(e_wk.loc[common]))
                if n >= 3 else None)
        gated = n >= G2_MIN_WEEKS
        table[line] = {"n_held_weeks": n, "corr_i0_vs_e0": _safe(corr),
                       "gated": bool(gated)}
        if gated and (corr is None or corr < G2_CORR_MIN):
            failing.append({"line": line, "n_held_weeks": n,
                            "corr_i0_vs_e0": _safe(corr)})
    return table, failing


# ---------------------------------------------------------------------------
# Register run helpers
# ---------------------------------------------------------------------------

def build_arm(spec, sector, membership, member_signals, member_prices, resolution):
    """Build one arm's daily name-level weight panel (E0 ignores the member
    inputs)."""
    mm, ms, mp, mr = ({}, {}, {}, {}) if spec.is_etf_baseline else (
        membership, member_signals, member_prices, resolution)
    return build_arm_name_weights(spec, sector["weights"], sector["closes"],
                                  sector["rebal_dates"], sector["eligible"],
                                  mm, ms, mp, member_resolution=mr)


def held_name_sets(build, rebal_dates, eligible, line_codes):
    """Set of single member names held (weight > 1e-6, line codes excluded) on
    each in-window rebalance Friday."""
    nw = build.name_weights
    member_cols = [c for c in nw.columns if c not in line_codes]
    sets = {}
    for rd in rebal_dates:
        if rd < eligible:
            continue
        row = nw.loc[rd, member_cols]
        sel = frozenset(row[row > 1e-6].index)
        if sel:
            sets[rd] = sel
    return sets


def mean_jaccard(sel_a, sel_b):
    common = sorted(set(sel_a) & set(sel_b))
    if not common:
        return None
    js = []
    for d in common:
        a, b = sel_a[d], sel_b[d]
        u = a | b
        js.append(len(a & b) / len(u) if u else 1.0)
    return float(np.mean(js))


def worst_single_name_weeks(build, returns_panel, eligible, line_codes, top=10):
    """Top-``top`` most negative weekly single-name contributions (weight_{t-1} x
    return_t, summed to W-FRI weeks) in a book. Line codes excluded — single
    names only. Returns a list of {name, week, contribution}."""
    nw = build.name_weights
    member_cols = [c for c in nw.columns if c not in line_codes]
    if not member_cols:
        return []
    w = nw[member_cols]
    rets = returns_panel.reindex(columns=member_cols).fillna(0.0)
    contrib = w.shift(1).fillna(0.0) * rets
    contrib = contrib.loc[contrib.index >= eligible]
    wk = contrib.resample("W-FRI").sum()
    stacked = wk.stack()
    worst = stacked[stacked < 0].nsmallest(top)
    out = []
    for (week, name), val in worst.items():
        out.append({"name": str(name), "week": str(pd.Timestamp(week).date()),
                    "contribution": _safe(val)})
    return out


def per_line_breakdown(sector, membership, member_signals, member_prices,
                       returns_panel, resolution):
    """Per single-named line: I1-vs-E0 weekly held-week correlation and the
    compounded + additive P&L contribution of the line under E0 (ETF) and I1
    (screened basket)."""
    e0, i1 = ARM_BY_ID["E0"], ARM_BY_ID["I1"]
    elig = sector["eligible"]
    out: dict[str, dict] = {}
    for line in SINGLE_NAMED_LINES:
        # Replication correlation (normalised 100%-into-line books).
        e_daily, e_inv = line_isolated_daily(e0, sector, membership, member_signals,
                                             member_prices, returns_panel, line,
                                             True, resolution)
        i_daily, i_inv = line_isolated_daily(i1, sector, membership, member_signals,
                                             member_prices, returns_panel, line,
                                             True, resolution)
        e_wk = _held_weekly(e_daily, e_inv, elig)
        i_wk = _held_weekly(i_daily, i_inv, elig)
        common = e_wk.index.intersection(i_wk.index)
        corr = (float(i_wk.loc[common].corr(e_wk.loc[common]))
                if len(common) >= 3 else None)
        # Additive P&L contribution (actual-weight books; sum decomposes the
        # sleeve arithmetic return, compounded is the multiplicative growth add).
        e_con, _ = line_isolated_daily(e0, sector, membership, member_signals,
                                       member_prices, returns_panel, line,
                                       False, resolution)
        i_con, _ = line_isolated_daily(i1, sector, membership, member_signals,
                                       member_prices, returns_panel, line,
                                       False, resolution)
        e_con = e_con.loc[e_con.index >= elig]
        i_con = i_con.loc[i_con.index >= elig]
        out[line] = {
            "n_held_weeks": int(len(common)),
            "corr_i1_vs_e0": _safe(corr),
            "contribution_e0_compounded": _safe(float((1.0 + e_con).prod() - 1.0)),
            "contribution_i1_compounded": _safe(float((1.0 + i_con).prod() - 1.0)),
            "contribution_e0_sum": _safe(float(e_con.sum())),
            "contribution_i1_sum": _safe(float(i_con.sum())),
        }
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    started = datetime.now(timezone.utc)
    print("WS6 T3 — registered run. Building deployed sector book ...", flush=True)
    sector = deployed_sector_layer()
    eligible = sector["eligible"]
    closes = sector["closes"]
    used = list(sector["weights"].columns)
    line_codes = set(used)
    end = WINDOW_END.strftime("%Y-%m-%d")
    print(f"  lines {len(used)} | window {eligible.date()} .. {closes.index.max().date()} "
          f"| rebalances {len(sector['rebal_dates'])}", flush=True)

    window_meta = {"eligible": eligible.strftime("%Y-%m-%d"), "end": end,
                   "registered": "2018-Q4 -> 2026-Q2",
                   "n_rebalances": int(len(sector["rebal_dates"]))}
    register_meta = {
        "arms": [{"id": s.arm_id, "label": s.label, "pool": s.pool,
                  "screen": bool(s.screen), "rank_key": s.rank_key,
                  "select_n": s.select_n, "is_etf_baseline": bool(s.is_etf_baseline)}
                 for s in ARM_REGISTER],
        "constants": {"M_pool": M_POOL, "N_select": N_SELECT,
                      "N_neighbour": N_NEIGHBOUR, "min_pass": MIN_PASS,
                      "trend_ma_days": int(TREND_MA),
                      "placebo_mom_days": int(PLACEBO_MOM_DAYS),
                      "K_deployed": K_DEPLOYED},
        "cost_sweep_bps": list(COST_SWEEP_BPS), "binding_cost_bps": BINDING_COST_BPS,
        "deployed_cost_bps": DEPLOYED_COST_BPS,
        "window": window_meta, "split_boundary": SPLIT_BOUNDARY.strftime("%Y-%m-%d"),
        "engine_commit": ENGINE_COMMIT,
    }
    mechanics_notes = {
        "t_minus_1_read": (
            "Per-name trend state/rank AND the membership snapshot are read as of "
            "the prior trading day (t-1) of each W-FRI rebalance, identical to the "
            "deployed sector signal's shift(1); a Friday-D rebalance uses the "
            "snapshot effective <= D-1 and the price/state row on or before D-1."),
        "fallback_scope": (
            "The <3-passing fallback applies to every SCREENED arm (I1, I2, P2, "
            "I2-N15, P2-N15, I1-all): a single-named line with fewer than "
            f"MIN_PASS={MIN_PASS} members passing the trend state reverts to its "
            "ETF that week. I0 (unscreened) reverts only if it has no holdable "
            "name at all. E0 and the three broad slices (CSP1, CNDX, IDP6) are "
            "always their own ETF."),
        "delisting_handling": (
            "A member delisting mid-hold earns its final Norgate TOTALRETURN "
            "print; the return panel forward-fills its price so it sits flat until "
            "the next rebalance drops it (exit at final print)."),
        "renormalisation": (
            "Baskets are equal-weight and renormalise over the names present / "
            "passing each week; the name-level book preserves the sector line "
            "weight exactly, so the full-vector one-way turnover the cost model "
            "charges is the book's own churn (sector-rotation + screen + "
            "membership)."),
        "coverage_definition": (
            "G1 covers snapshot member-weeks with snapshot date in "
            f"[{eligible.date()}, {WINDOW_END.date()}] grouped by calendar year; a "
            "member-week is covered iff its RESOLVED Norgate instrument "
            "(amendment A1: (ticker, snapshot date) -> instrument, delisted "
            "suffixes enumerated, recycled bases era-disambiguated by life "
            "interval, verified renames as fallback) has a non-NaN TOTALRETURN "
            f"close on >= 1 of the {COVERAGE_ASOF_ROWS} trailing rows up to the "
            "snapshot as-of date; unresolved and ambiguous names count against "
            "coverage."),
        "e0_cost": (
            "E0 keeps the deployed cost model (2 bps on its line-level book) at "
            "every cost point; the sweep {2,5,10,20} bps applies to the "
            "constituent arms' full name-level vector."),
    }

    def write_payload(status, extra):
        payload = {"computed_at_utc": started.isoformat(), "status": status,
                   "register_meta": register_meta, "mechanics_notes": mechanics_notes}
        payload.update(extra)
        OUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nWrote {OUT_PATH.relative_to(PROJECT_ROOT)}  (status={status})")

    # --- Stage 1: data -----------------------------------------------------
    print("\nStage 1 — Norgate member prices (data_local/ws6, git-ignored):", flush=True)
    try:
        (prices_by_line, mapping_reports, fetch_reports,
         resolution_by_line) = load_or_fetch_member_prices(SINGLE_NAMED_LINES)
    except Exception as exc:  # noqa: BLE001 — NDU down / empty fetch = STOP
        print(f"\nSTOP_DATA: member-price stage failed: {type(exc).__name__}: {exc}")
        print("NDU not running or the fetch errored — no gate or arm results computed.")
        return 2

    membership = {L: load_constituents(L)["snapshots"] for L in SINGLE_NAMED_LINES}
    member_signals = {L: precompute_member_signals(prices_by_line[L])
                      for L in SINGLE_NAMED_LINES}
    fetch_summary = {L: {k: fetch_reports[L].get(k) for k in
                         ("source", "n_requested", "n_resolved", "n_uncovered",
                          "uncovered", "panel_shape", "panel_start", "panel_end")}
                     for L in SINGLE_NAMED_LINES}

    # Combined member-return-side panel (dedup shared names across lines).
    combined = pd.concat([prices_by_line[L] for L in SINGLE_NAMED_LINES], axis=1)
    combined = combined.loc[:, ~combined.columns.duplicated(keep="first")]
    returns_panel = build_name_return_panel(closes, combined)

    # --- Stage 2: Gate G1 --------------------------------------------------
    print("\nStage 2 — Gate G1 (coverage) ...", flush=True)
    g1_table, g1_failing = compute_g1(SINGLE_NAMED_LINES, membership,
                                      prices_by_line, eligible,
                                      resolution_by_line)
    worst_cell = min(
        (c for L in g1_table for c in g1_table[L].values() if c["share"] is not None),
        key=lambda c: c["share"], default=None)
    print(f"  worst line-year coverage: "
          f"{worst_cell['share']:.4f}" if worst_cell else "  (no cells)")
    if g1_failing:
        print(f"  G1 FAIL — {len(g1_failing)} line-year cell(s) below {COVERAGE_MIN}:")
        for c in g1_failing:
            print(f"    {c['line']} {c['year']}: {c['share']:.4f} "
                  f"({c['covered']}/{c['total']})")
        write_payload("STOP_G1", {
            "gates": {"G1": {"threshold": COVERAGE_MIN, "table": g1_table,
                             "failing": g1_failing, "passed": False}},
            "data_stage": {"fetch_summary": fetch_summary,
                           "mapping": mapping_reports}})
        return 1
    print(f"  G1 PASS — all in-window line-years >= {COVERAGE_MIN}.")

    # --- Stage 3: Gate G2 --------------------------------------------------
    print("\nStage 3 — Gate G2 (I0-vs-E0 replication) ...", flush=True)
    g2_table, g2_failing = compute_g2(SINGLE_NAMED_LINES, sector, membership,
                                      member_signals, prices_by_line,
                                      returns_panel, resolution_by_line)
    for L in SINGLE_NAMED_LINES:
        r = g2_table[L]
        print(f"    {L:<6} n={r['n_held_weeks']:>3} corr={r['corr_i0_vs_e0']} "
              f"gated={r['gated']}")
    if g2_failing:
        print(f"  G2 FAIL — {len(g2_failing)} gated line(s) below {G2_CORR_MIN}:")
        for c in g2_failing:
            print(f"    {c['line']}: corr={c['corr_i0_vs_e0']} n={c['n_held_weeks']}")
        write_payload("STOP_G2", {
            "gates": {"G1": {"threshold": COVERAGE_MIN, "table": g1_table,
                             "passed": True},
                      "G2": {"threshold": G2_CORR_MIN, "min_weeks": G2_MIN_WEEKS,
                             "table": g2_table, "failing": g2_failing,
                             "passed": False}},
            "data_stage": {"fetch_summary": fetch_summary,
                           "mapping": mapping_reports}})
        return 1
    print(f"  G2 PASS — every gated line >= {G2_CORR_MIN}.")

    # --- Stage 4: register run (once) --------------------------------------
    print("\nStage 4 — register run (8 arms x cost sweep) ...", flush=True)
    builds: dict[str, object] = {}
    sims: dict[str, dict] = {}
    for spec in ARM_REGISTER:
        builds[spec.arm_id] = build_arm(spec, sector, membership,
                                        member_signals, prices_by_line,
                                        resolution_by_line)
        sims[spec.arm_id] = {}
        for c in COST_SWEEP_BPS:
            cost = DEPLOYED_COST_BPS if spec.is_etf_baseline else c
            sims[spec.arm_id][c] = simulate_arm(
                builds[spec.arm_id].name_weights, returns_panel, cost_bps=cost)

    # Parity anchor — E0 at the deployed 2 bps must reproduce the deployed sleeve.
    e0_equity = sims["E0"][DEPLOYED_COST_BPS]["equity"]
    parity = float((e0_equity - sector["equity"]).abs().max())
    print(f"  E0 parity vs deployed sleeve: max|diff| = {parity:.2e}")
    if not parity < 1e-9:
        print("STOP_PARITY: E0 does not reproduce the deployed sleeve — harness bug.")
        return 3

    # E0 baseline weekly returns (fixed across cost points; the vs-E0 reference).
    e0_daily = sims["E0"][DEPLOYED_COST_BPS]["daily"]
    e0_daily = e0_daily.loc[e0_daily.index >= eligible]
    e0_wk = _weekly_returns(e0_daily)
    _e0_turn = sims["E0"][DEPLOYED_COST_BPS]["turnover"]
    e0_turnover = float(_e0_turn.loc[_e0_turn.index >= eligible].sum())

    # Per arm x cost: net Sharpe / CAGR / MaxDD, vs-E0 weekly corr + TE.
    register: dict[str, dict] = {}
    for spec in ARM_REGISTER:
        arm = spec.arm_id
        by_cost = {}
        for c in COST_SWEEP_BPS:
            sim = sims[arm][c]
            st = compute_stats(sim["equity"], eligible)
            daily = sim["daily"].loc[sim["daily"].index >= eligible]
            wk = _weekly_returns(daily)
            common = wk.index.intersection(e0_wk.index)
            if arm == "E0":
                corr, te = 1.0, 0.0
            else:
                corr = _safe(wk.loc[common].corr(e0_wk.loc[common]))
                te = _safe(float((wk.loc[common] - e0_wk.loc[common]).std()
                                 * math.sqrt(TRADING_WEEKS)))
            by_cost[str(c)] = {
                "net_sharpe": _safe(st["sharpe"]), "net_cagr": _safe(st["cagr"]),
                "max_dd": _safe(st["max_dd"]), "total_return": _safe(st["total_return"]),
                "corr_vs_e0_weekly": corr, "tracking_error_ann": te,
                "cost_bps_applied": DEPLOYED_COST_BPS if spec.is_etf_baseline else c,
            }
        _turn = sims[arm][COST_SWEEP_BPS[0]]["turnover"]
        arm_turnover = float(_turn.loc[_turn.index >= eligible].sum())
        by_cost_meta = {
            "by_cost": by_cost,
            "total_oneway_turnover": _safe(arm_turnover),
            "turnover_multiple_vs_e0": _safe(arm_turnover / e0_turnover
                                             if e0_turnover else None),
        }
        register[arm] = by_cost_meta

    # Fallback / revert-to-ETF frequency per non-E0 arm.
    fallback = {}
    for spec in ARM_REGISTER:
        if spec.is_etf_baseline:
            continue
        b = builds[spec.arm_id]
        total_fb = int(sum(b.fallback_weeks.values()))
        total_wk = int(sum(b.weeks_evaluated.values()))
        fallback[spec.arm_id] = {
            "per_line_fallback_weeks": {k: int(v) for k, v in b.fallback_weeks.items()},
            "per_line_weeks_evaluated": {k: int(v) for k, v in b.weeks_evaluated.items()},
            "total_fallback_line_weeks": total_fb,
            "total_line_weeks_evaluated": total_wk,
            "fallback_rate": _safe(total_fb / total_wk if total_wk else None),
            "is_screened": spec.arm_id in SCREENED_ARMS,
        }

    # Split-half net Sharpe at 5 bps (E0 at its deployed cost).
    split_half = {}
    for spec in ARM_REGISTER:
        arm = spec.arm_id
        sim = sims[arm][5]
        daily = sim["daily"].loc[sim["daily"].index >= eligible]
        h1 = daily.loc[daily.index <= SPLIT_BOUNDARY]
        h2 = daily.loc[daily.index > SPLIT_BOUNDARY]
        split_half[arm] = {
            "cost_bps": DEPLOYED_COST_BPS if spec.is_etf_baseline else 5,
            "first_half_sharpe": _safe(_ann_sharpe(h1)),
            "second_half_sharpe": _safe(_ann_sharpe(h2)),
            "first_half_days": int(len(h1)), "second_half_days": int(len(h2)),
        }

    # I2-vs-P2 selection Jaccard + return correlation.
    sel_i2 = held_name_sets(builds["I2"], sector["rebal_dates"], eligible, line_codes)
    sel_p2 = held_name_sets(builds["P2"], sector["rebal_dates"], eligible, line_codes)
    jac = mean_jaccard(sel_i2, sel_p2)
    i2_daily = sims["I2"][5]["daily"].loc[sims["I2"][5]["daily"].index >= eligible]
    p2_daily = sims["P2"][5]["daily"].loc[sims["P2"][5]["daily"].index >= eligible]
    i2_wk, p2_wk = _weekly_returns(i2_daily), _weekly_returns(p2_daily)
    cw = i2_wk.index.intersection(p2_wk.index)
    i2_vs_p2 = {
        "weekly_selection_jaccard": _safe(jac),
        "return_corr_weekly": _safe(float(i2_wk.loc[cw].corr(p2_wk.loc[cw]))),
        "return_corr_daily": _safe(float(i2_daily.corr(p2_daily))),
        "n_common_rebalances": int(len(set(sel_i2) & set(sel_p2))),
        "mean_selection_size_i2": _safe(np.mean([len(v) for v in sel_i2.values()])
                                        if sel_i2 else None),
        "mean_selection_size_p2": _safe(np.mean([len(v) for v in sel_p2.values()])
                                        if sel_p2 else None),
        "note": "Jaccard and sets are over single member names (line codes "
                "excluded); cost 5 bps for the return correlations.",
    }

    # Worst single-name weeks (I1, I2 books).
    worst = {
        "I1": worst_single_name_weeks(builds["I1"], returns_panel, eligible, line_codes),
        "I2": worst_single_name_weeks(builds["I2"], returns_panel, eligible, line_codes),
    }

    # Per-line breakdown (I1-vs-E0 corr + per-line contributions).
    print("  per-line breakdown ...", flush=True)
    per_line = per_line_breakdown(sector, membership, member_signals,
                                  prices_by_line, returns_panel,
                                  resolution_by_line)

    finished = datetime.now(timezone.utc)
    runtime_s = (finished - started).total_seconds()

    # G1 STOP history: the gate fired twice and was cleared each time by
    # data-layer completion (amendments A1 and A2, kickoff §5b) at the
    # UNCHANGED 97% bar — never by lowering it.
    g1_history = {
        "first_run": {
            "status": "STOP_G1", "engine_commit": "b12d0f9",
            "harness_commit": "24aa6d0",
            "n_failing_cells": 68,
            "worst_cell": {"line": "IUCM", "year": 2018, "share": 0.5769},
            "cause": ("Delisted members unresolved: Norgate stores delisted "
                      "instruments under delisting-dated -YYYYMM suffixes and "
                      "the b12d0f9 mapper emitted only plain tickers, so "
                      "delisted/renamed members counted against coverage."),
            "resolution": ("Amendment A1 (logged pre-results, kickoff 5b; "
                           "engine 54f0f14): (ticker, membership date) -> "
                           "instrument resolution with delisted-suffix "
                           "candidates, life-interval disambiguation and a "
                           "verified rename table. G1 re-tested in full at "
                           "the unchanged 97% bar."),
        },
        "second_run": {
            "status": "STOP_G1", "engine_commit": "54f0f14",
            "harness_commit": "05561d6",
            "n_failing_cells": 6,
            "worst_cell": {"line": "IUCM", "year": 2018, "share": 0.9231},
            "cause": ("Recycled-ticker ambiguity (HR, DOC, RPT, COR: dead REIT "
                      "and live recycled base both contain the membership "
                      "date) plus rename-at-death gaps (FOX/FOXA pre-2019, LB, "
                      "PCLN, CBL, RVI, OPI)."),
            "resolution": ("Amendment A2 (logged pre-results, kickoff 5b; "
                           "engine dbb6543): base-ticker tenure "
                           "disambiguation anchored to the predecessor's "
                           "last_quoted_date + 1 (TICKER_TENURE_OVERRIDES) "
                           "plus verified rename additions (FOX/FOXA -> "
                           "TFCF/TFCFA-201903, LB -> BBWI, PCLN -> BKNG, "
                           "CBL -> CBLAQ-202111, RVI -> RVIC-202304, OPI -> "
                           "OPITQ-202606). G1 re-tested in full at the "
                           "unchanged 97% bar."),
        },
    }

    write_payload("COMPLETE", {
        "runtime_seconds": _safe(runtime_s),
        "parity": {"e0_vs_deployed_maxdiff": _safe(parity)},
        "g1_history": g1_history,
        "gates": {
            "G1": {"threshold": COVERAGE_MIN, "asof_trailing_rows": COVERAGE_ASOF_ROWS,
                   "table": g1_table, "worst_cell": worst_cell, "passed": True},
            "G2": {"threshold": G2_CORR_MIN, "min_weeks": G2_MIN_WEEKS,
                   "table": g2_table, "passed": True},
        },
        "data_stage": {"fetch_summary": fetch_summary, "mapping": mapping_reports},
        "register": register,
        "fallback": fallback,
        "split_half": split_half,
        "i2_vs_p2": i2_vs_p2,
        "worst_single_name_weeks": worst,
        "per_line": per_line,
        "baseline_turnover_oneway": _safe(e0_turnover),
    })

    # --- Console summary (raw facts for the T3 report) ---------------------
    print("\n" + "=" * 78)
    print("WS6 T3 register — raw facts (no interpretation; verdict is T4)")
    print("=" * 78)
    print(f"runtime {runtime_s:.1f}s | E0 parity {parity:.1e} | eligible {eligible.date()}")
    print(f"\nG1 worst line-year coverage: {worst_cell['share']:.4f} "
          f"({worst_cell['covered']}/{worst_cell['total']})" if worst_cell else "G1 —")
    print("\nG2 (I0-vs-E0 held-week weekly corr):")
    for L in SINGLE_NAMED_LINES:
        r = g2_table[L]
        print(f"  {L:<6} n={r['n_held_weeks']:>3} corr={r['corr_i0_vs_e0']:.4f}")
    for cpt in (5, 10):
        print(f"\nPer-arm @ {cpt} bps  (Sharpe / CAGR / MaxDD / corr / TE / turn-mult):")
        for spec in ARM_REGISTER:
            r = register[spec.arm_id]["by_cost"][str(cpt)]
            tm = register[spec.arm_id]["turnover_multiple_vs_e0"]
            print(f"  {spec.arm_id:<7} {r['net_sharpe']:+.3f} "
                  f"{r['net_cagr']*100:+6.2f}% {r['max_dd']*100:5.1f}% "
                  f"corr {r['corr_vs_e0_weekly'] if r['corr_vs_e0_weekly'] is None else round(r['corr_vs_e0_weekly'],4)} "
                  f"TE {r['tracking_error_ann'] if r['tracking_error_ann'] is None else round(r['tracking_error_ann'],4)} "
                  f"x{tm if tm is None else round(tm,2)}")
    print("\nFallback rate per non-E0 arm:")
    for arm, f in fallback.items():
        print(f"  {arm:<7} {f['total_fallback_line_weeks']}/{f['total_line_weeks_evaluated']} "
              f"= {f['fallback_rate']:.4f}  screened={f['is_screened']}")
    print(f"\nI2-vs-P2: Jaccard {i2_vs_p2['weekly_selection_jaccard']} | "
          f"return corr (wk) {i2_vs_p2['return_corr_weekly']} | "
          f"(daily) {i2_vs_p2['return_corr_daily']}")
    print("\nSplit-half net Sharpe @ 5 bps (boundary 2022-09-08):")
    for spec in ARM_REGISTER:
        s = split_half[spec.arm_id]
        print(f"  {spec.arm_id:<7} H1 {s['first_half_sharpe']} | H2 {s['second_half_sharpe']}")
    print("\nThree worst single-name weeks (I1):")
    for w in worst["I1"][:3]:
        print(f"  {w['name']:<8} {w['week']}  {w['contribution']:+.5f}")
    print("Three worst single-name weeks (I2):")
    for w in worst["I2"][:3]:
        print(f"  {w['name']:<8} {w['week']}  {w['contribution']:+.5f}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
