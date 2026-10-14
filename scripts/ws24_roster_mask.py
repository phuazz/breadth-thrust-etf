"""WS24 — point-in-time roster masking of the 200-day constituent breadth.

Pure functions only: no file I/O, no network, no engine imports. The runner
(scripts/run_ws24_roster_mask.py) owns the snapshot, the engines and the
output. Registration: KICKOFF_ws24-pit-roster-mask.md, countersigned
2026-10-14, frozen at 04607653 (on main as 07889864, merge c1bb52e8). This
module implements §4 and nothing else.

Arms (§4)
---------
  M0  deployed: rolling(200, min_periods=180) on the shared date grid over
      EVERY column of the cache. Reproduces run_ma200_sweep
      .compute_ma200_breadth exactly (G0).
  M1  the same moving average, then the count restricted on each session to
      the roster known at the decision (O7), resolved through ALIAS_MAP.
  M2  M1 with every ERA_BARRIERS column split into two era columns on the
      SAME shared grid, so no window spans two securities. Nothing else
      changes: per_ticker_apply's own-session window is NOT adopted.

Roster known at the decision (O7)
---------------------------------
On signal session d the roster is the latest snapshot whose key is on or
before d - 1 day, i.e. strictly before d. On a Friday signal bar (the W-MON
grid ranks on the Friday close) that is the previous Friday's snapshot, which
was complete when the Saturday decision ran. The 50-day panel's own rule
(key on or before d) is kept here only for G1, which compares like with like.

Dates are pandas Timestamps; snapshot keys are ISO 'YYYY-MM-DD' strings
(Python months are 1-indexed throughout).
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import datetime, time

import numpy as np
import pandas as pd

MA_PERIOD = 200
MIN_PERIODS = int(MA_PERIOD * 0.9)          # 180, as deployed

# Roster symbol -> cache column, applied in the mask ONLY (it selects a
# column, it never changes a price). Registered in §4. A symbol not listed
# maps to the column of the same name.
ALIAS_MAP: dict[str, str] = {
    "BRK B": "BRK-B",
    "BF B": "BF-B",
    "RTX US": "RTX",
    "HOLX US": "HOLX",
}

# G4 identity rule, fixed at build before any snapshot exists (recorded in
# the registration's §12 as a build clarification): a (column, window) cell
# holds the roster member's own security when the cache column's daily
# returns correlate with the dated resolver's Norgate series at or above
# IDENTITY_MIN_CORR on at least IDENTITY_MIN_OBS common returns. Fewer common
# returns, or no resolvable symbol, is UNVERIFIED, which is listed and not
# excluded.
IDENTITY_MIN_CORR = 0.98
IDENTITY_MIN_OBS = 20

# D1 duplicate-lineage rule: two columns carry the same security in a
# calendar year when their daily returns agree within DUP_RET_TOL on at
# least DUP_MIN_SHARE of at least DUP_MIN_OBS common sessions.
DUP_RET_TOL = 1e-4
DUP_MIN_SHARE = 0.95
DUP_MIN_OBS = 60

# Machine-load rule (§7): BreadthThrust-WeeklyRefresh runs Saturday and
# Sunday, BreadthThrust-PostFillRefresh Tuesday and Wednesday, 09:00-15:00
# SGT. datetime.weekday(): Monday 0 ... Sunday 6.
REFRESH_WEEKDAYS = frozenset({1, 2, 5, 6})
REFRESH_START = time(9, 0)
REFRESH_END = time(15, 0)


# ---------------------------------------------------------------------------
# Roster resolution
# ---------------------------------------------------------------------------

def resolve_key(snapshot_dates: list[str], d: pd.Timestamp,
                rule: str = "strict") -> str | None:
    """The snapshot key governing session ``d``.

    ``strict`` (O7, the arms): latest key on or before d - 1 day.
    ``le`` (the 50-day panel's active_roster_at, used only by G1): latest key
    on or before d.
    """
    if rule == "strict":
        bound = (pd.Timestamp(d) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    elif rule == "le":
        bound = pd.Timestamp(d).strftime("%Y-%m-%d")
    else:
        raise ValueError(f"unknown roster rule {rule!r}")
    i = bisect_right(snapshot_dates, bound) - 1
    return snapshot_dates[i] if i >= 0 else None


def roster_symbols(snapshot_map: dict, key: str | None) -> list[str]:
    if key is None:
        return []
    return list(dict.fromkeys(snapshot_map[key]["tickers"]))


def column_for(symbol: str, columns, aliases: dict[str, str] | None = None
               ) -> str | None:
    """Cache column a roster symbol selects, or None when the cache has none."""
    aliases = ALIAS_MAP if aliases is None else aliases
    col = aliases.get(symbol, symbol)
    return col if col in columns else None


def alias_candidates(symbol: str) -> list[str]:
    """Spellings a roster symbol might carry in the cache (G1's search set).

    Separators (' ', '.', '/') become '-' and a trailing exchange suffix
    (' US') is dropped. The symbol itself is excluded.
    """
    s = symbol.strip()
    out = []
    base = s[:-3] if s.endswith(" US") else s
    for cand in (base, base.replace(" ", "-"), base.replace(".", "-"),
                 base.replace("/", "-"), base.replace(" ", ".")):
        if cand and cand != symbol and cand not in out:
            out.append(cand)
    return out


# ---------------------------------------------------------------------------
# Era split (M2)
# ---------------------------------------------------------------------------

def era_column(col: str, barrier: str) -> str:
    return f"{col}@{barrier}"


def era_split(prices: pd.DataFrame, barriers: dict[str, str]
              ) -> tuple[pd.DataFrame, dict[str, str]]:
    """Split each barrier column into two era columns on the SAME grid.

    The earlier era keeps the column name and holds bars strictly before the
    barrier; the later era is ``col@barrier`` and holds bars from the barrier
    on. Returns the split frame and the barriers actually applied.
    """
    applied = {c: b for c, b in barriers.items() if c in prices.columns}
    if not applied:
        return prices, {}
    out = prices.copy()
    for col, b in applied.items():
        cut = pd.Timestamp(b)
        later = out[col].where(out.index >= cut)
        out[col] = out[col].where(out.index < cut)
        out[era_column(col, b)] = later
    return out, applied


def era_resolved_column(col: str, d: pd.Timestamp,
                        applied: dict[str, str]) -> str:
    b = applied.get(col)
    if b is None:
        return col
    return era_column(col, b) if pd.Timestamp(d) >= pd.Timestamp(b) else col


# ---------------------------------------------------------------------------
# Membership and exclusion matrices
# ---------------------------------------------------------------------------

def membership_matrix(index: pd.DatetimeIndex, columns, snapshot_dates,
                      snapshot_map: dict, rule: str = "strict",
                      applied_barriers: dict[str, str] | None = None,
                      aliases: dict[str, str] | None = None) -> pd.DataFrame:
    """Boolean (session x column): column is a member on that session."""
    applied_barriers = applied_barriers or {}
    cols = list(columns)
    pos = {c: j for j, c in enumerate(cols)}
    arr = np.zeros((len(index), len(cols)), dtype=bool)
    keys = [resolve_key(snapshot_dates, d, rule) for d in index]
    cache: dict[str, list[str]] = {}
    for i, (d, key) in enumerate(zip(index, keys)):
        if key is None:
            continue
        if key not in cache:
            mapped = []
            for t in roster_symbols(snapshot_map, key):
                c = column_for(t, pos, aliases)
                if c is not None:
                    mapped.append(c)
            cache[key] = mapped
        for c in cache[key]:
            j = pos.get(era_resolved_column(c, d, applied_barriers))
            if j is not None:
                arr[i, j] = True
    return pd.DataFrame(arr, index=index, columns=cols)


def exclusion_matrix(index: pd.DatetimeIndex, columns, cells: list[dict],
                     applied_barriers: dict[str, str] | None = None
                     ) -> pd.DataFrame | None:
    """Boolean (session x column): cell excluded from every arm (O8).

    ``cells`` carry ``column``, ``start`` and ``end`` (inclusive ISO dates)
    on the ORIGINAL column name; after an era split each session maps to the
    era column live on that date.
    """
    if not cells:
        return None
    applied_barriers = applied_barriers or {}
    cols = list(columns)
    pos = {c: j for j, c in enumerate(cols)}
    arr = np.zeros((len(index), len(cols)), dtype=bool)
    for cell in cells:
        lo, hi = pd.Timestamp(cell["start"]), pd.Timestamp(cell["end"])
        rows = np.where((index >= lo) & (index <= hi))[0]
        for i in rows:
            j = pos.get(era_resolved_column(cell["column"], index[i],
                                            applied_barriers))
            if j is not None:
                arr[i, j] = True
    return pd.DataFrame(arr, index=index, columns=cols)


# ---------------------------------------------------------------------------
# Breadth
# ---------------------------------------------------------------------------

def masked_breadth(prices: pd.DataFrame, member: pd.DataFrame | None = None,
                   exclude: pd.DataFrame | None = None,
                   period: int = MA_PERIOD) -> pd.Series:
    """Share of counted columns above their ``period``-day mean.

    With ``member`` and ``exclude`` both None this is, operation for
    operation, run_ma200_sweep.compute_ma200_breadth (G0 asserts exact
    equality). The moving average is computed on each column's full history
    BEFORE any mask, so a joiner's first counted session carries an average
    built on pre-entry prices.
    """
    min_p = max(1, int(period * 0.9))
    ma = prices.rolling(period, min_periods=min_p).mean()
    both_valid = prices.notna() & ma.notna()
    if member is not None:
        both_valid = both_valid & member.reindex(
            index=prices.index, columns=prices.columns, fill_value=False)
    if exclude is not None:
        both_valid = both_valid & ~exclude.reindex(
            index=prices.index, columns=prices.columns, fill_value=False)
    above = (prices > ma) & both_valid
    n_above = above.sum(axis=1)
    n_valid = both_valid.sum(axis=1)
    return n_above / n_valid.replace(0, np.nan)


def arm_breadth(arm: str, prices: pd.DataFrame, snapshot_dates, snapshot_map,
                barriers: dict[str, str], cells: list[dict] | None = None,
                aliases: dict[str, str] | None = None) -> pd.Series:
    """Breadth series for one registered arm.

    ``M0`` (deployed, every column), ``M0s`` (M0 with the G4 cells excluded,
    the symmetric basis), ``M1`` and ``M2``. ``cells`` are the G4 failures,
    excluded from every arm except ``M0``.
    """
    cells = cells or []
    if arm == "M0":
        return masked_breadth(prices)
    if arm == "M0s":
        return masked_breadth(prices, None,
                              exclusion_matrix(prices.index, prices.columns,
                                               cells))
    if arm == "M1":
        member = membership_matrix(prices.index, prices.columns,
                                   snapshot_dates, snapshot_map, "strict",
                                   None, aliases)
        return masked_breadth(prices, member,
                              exclusion_matrix(prices.index, prices.columns,
                                               cells))
    if arm == "M2":
        split, applied = era_split(prices, barriers)
        member = membership_matrix(split.index, split.columns,
                                   snapshot_dates, snapshot_map, "strict",
                                   applied, aliases)
        return masked_breadth(split, member,
                              exclusion_matrix(split.index, split.columns,
                                               cells, applied))
    raise ValueError(f"unregistered arm {arm!r}")


# ---------------------------------------------------------------------------
# G1 — roster reconciliation on a priced basis
# ---------------------------------------------------------------------------

def g1_reconcile(prices: pd.DataFrame, snapshot_dates, snapshot_map: dict,
                 n_with_price: pd.Series,
                 aliases: dict[str, str] | None = None) -> dict:
    """G1 for one panel.

    On every session the 50-day panel reports, the count of roster members
    (50-day rule, key on or before d) that resolve to a column PRICED on that
    session must be at least the 50-day panel's n_with_price. The gate also
    fails on any roster member whose resolved column is unpriced while a
    priced alias candidate exists that the map does not cover.
    """
    aliases = ALIAS_MAP if aliases is None else aliases
    pos = {c: j for j, c in enumerate(prices.columns)}
    rowpos = {d: i for i, d in enumerate(prices.index)}
    priced = prices.notna().to_numpy()
    shortfalls, uncovered = [], {}
    unmatched: dict[str, int] = {}
    for d, n50 in n_with_price.dropna().items():
        i = rowpos.get(d)
        if i is None:
            continue
        key = resolve_key(snapshot_dates, d, "le")
        matched = set()
        for t in roster_symbols(snapshot_map, key):
            c = column_for(t, pos, aliases)
            if c is None:
                unmatched[t] = unmatched.get(t, 0) + 1
            elif priced[i, pos[c]]:
                matched.add(c)
                continue
            for cand in alias_candidates(t):
                j = pos.get(cand)
                if j is not None and priced[i, j] and aliases.get(t) != cand:
                    uncovered.setdefault(t, set()).add(cand)
        if len(matched) < int(n50):
            shortfalls.append({"date": d.strftime("%Y-%m-%d"),
                               "matched_priced": len(matched),
                               "n_with_price_50d": int(n50)})
    return {
        "pass": not shortfalls and not uncovered,
        "shortfall_days": len(shortfalls),
        "shortfalls_head": shortfalls[:20],
        "uncovered_aliases": {k: sorted(v) for k, v in uncovered.items()},
        "unmatched_symbols": dict(sorted(unmatched.items())),
    }


# ---------------------------------------------------------------------------
# G4 — security identity
# ---------------------------------------------------------------------------

def membership_runs(snapshot_dates: list[str], snapshot_map: dict,
                    symbol: str) -> list[tuple[str, str]]:
    """Runs of consecutive snapshot keys whose roster carries ``symbol``."""
    runs, start, prev = [], None, None
    for key in snapshot_dates:
        present = symbol in snapshot_map[key]["tickers"]
        if present and start is None:
            start = key
        if not present and start is not None:
            runs.append((start, prev))
            start = None
        prev = key
    if start is not None:
        runs.append((start, prev))
    return runs


def identity_test(cache_close: pd.Series, ref_close: pd.Series) -> dict:
    """Return correlation of a cache column against a reference security."""
    both = pd.concat([cache_close, ref_close], axis=1, keys=["c", "r"]).dropna()
    rets = both.pct_change().dropna()
    n = int(len(rets))
    if n < IDENTITY_MIN_OBS or rets["c"].std() == 0 or rets["r"].std() == 0:
        return {"status": "unverified", "n": n, "corr": None}
    corr = float(rets["c"].corr(rets["r"]))
    return {"status": "pass" if corr >= IDENTITY_MIN_CORR else "fail",
            "n": n, "corr": round(corr, 4)}


# ---------------------------------------------------------------------------
# D1 / D2 — decomposition of the M0s-to-M1 difference by contamination class
# ---------------------------------------------------------------------------

def contamination_classes(member: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Non-member cells by class: pre_entry, post_exit, between_windows.

    A column never a member on the grid is pre_entry throughout.
    """
    m = member.to_numpy()
    n, k = m.shape
    pre = np.zeros_like(m)
    post = np.zeros_like(m)
    for j in range(k):
        rows = np.flatnonzero(m[:, j])
        if rows.size == 0:
            pre[:, j] = True
            continue
        pre[: rows[0], j] = True
        post[rows[-1] + 1:, j] = True
    between = ~m & ~pre & ~post
    mk = lambda a: pd.DataFrame(a, index=member.index, columns=member.columns)  # noqa: E731
    return {"pre_entry": mk(pre), "post_exit": mk(post),
            "between_windows": mk(between)}


def duplicate_lineage(prices: pd.DataFrame, member: pd.DataFrame
                      ) -> pd.DataFrame:
    """Non-member cells whose column duplicates a member column's security.

    Per calendar year, a non-member column whose daily returns match some
    column that is a member in that year (DUP_RET_TOL on DUP_MIN_SHARE of at
    least DUP_MIN_OBS common sessions) is flagged for its non-member sessions
    of that year. Descriptive (D1), never an exclusion.
    """
    rets = prices.pct_change(fill_method=None)
    flag = pd.DataFrame(False, index=prices.index, columns=prices.columns)
    for year in sorted(set(prices.index.year)):
        rows = prices.index.year == year
        r = rets.loc[rows]
        mem = member.loc[rows]
        cand = [c for c in r.columns
                if (~mem[c]).any() and r[c].notna().sum() >= DUP_MIN_OBS]
        refs = [c for c in r.columns if mem[c].any()
                and r[c].notna().sum() >= DUP_MIN_OBS]
        if not cand or not refs:
            continue
        R = r[refs].to_numpy()
        for c in cand:
            x = r[c].to_numpy()[:, None]
            both = ~np.isnan(x) & ~np.isnan(R)
            nobs = both.sum(axis=0)
            close = (np.abs(x - R) <= DUP_RET_TOL) & both
            share = np.where(nobs > 0, close.sum(axis=0) / np.maximum(nobs, 1), 0)
            ok = (nobs >= DUP_MIN_OBS) & (share >= DUP_MIN_SHARE)
            ok &= np.array([ref != c for ref in refs])
            if ok.any():
                flag.loc[rows, c] = ~mem[c].to_numpy()
    return flag


def class_contributions(prices: pd.DataFrame, member: pd.DataFrame,
                        exclude: pd.DataFrame | None,
                        classes: dict[str, pd.DataFrame],
                        window: pd.DatetimeIndex) -> dict:
    """Leave-one-class-out contribution of each class to M0s minus M1.

    For class k: mean over ``window`` of b(M0s) - b(M0s without class k), in
    percentage points, plus the mean absolute value. The classes overlap only
    where duplicate_lineage flags cells already in another class, so the
    pre/post/between contributions are reported with their sum and the
    duplicate flag separately.
    """
    base = masked_breadth(prices, None, exclude)
    m1 = masked_breadth(prices, member, exclude)
    out = {"total_M0s_minus_M1": _pp(base - m1, window)}
    for name, cls in classes.items():
        drop = cls if exclude is None else (
            cls | exclude.reindex(index=cls.index, columns=cls.columns,
                                  fill_value=False))
        without = masked_breadth(prices, None, drop)
        out[name] = _pp(base - without, window)
    return out


def _pp(diff: pd.Series, window: pd.DatetimeIndex) -> dict:
    d = diff.reindex(window).dropna() * 100
    if d.empty:
        return {"mean_pp": None, "mean_abs_pp": None, "max_abs_pp": None,
                "n": 0}
    return {"mean_pp": round(float(d.mean()), 3),
            "mean_abs_pp": round(float(d.abs().mean()), 3),
            "max_abs_pp": round(float(d.abs().max()), 3), "n": int(len(d))}


# ---------------------------------------------------------------------------
# D3 — selection agreement
# ---------------------------------------------------------------------------

def selection_agreement(w_a: pd.DataFrame, w_b: pd.DataFrame,
                        rebalance_dates: pd.DatetimeIndex) -> dict:
    """Share of rebalance dates with an identical held set, and the mean sum
    of absolute weight differences on those dates."""
    idx = rebalance_dates.intersection(w_a.index).intersection(w_b.index)
    if len(idx) == 0:
        return {"n": 0, "identical_share": None, "mean_abs_weight_diff": None}
    cols = w_a.columns.union(w_b.columns)
    a = w_a.reindex(index=idx, columns=cols, fill_value=0.0).fillna(0.0)
    b = w_b.reindex(index=idx, columns=cols, fill_value=0.0).fillna(0.0)
    same = ((a > 1e-9) == (b > 1e-9)).all(axis=1)
    return {"n": int(len(idx)),
            "identical_share": round(float(same.mean()), 4),
            "mean_abs_weight_diff": round(float((a - b).abs().sum(axis=1)
                                                .mean()), 4)}


# ---------------------------------------------------------------------------
# Machine-load guard
# ---------------------------------------------------------------------------

def refresh_window_open(now_sgt: datetime) -> bool:
    """True inside a BreadthThrust refresh window (SGT wall clock)."""
    return (now_sgt.weekday() in REFRESH_WEEKDAYS
            and REFRESH_START <= now_sgt.time() < REFRESH_END)
