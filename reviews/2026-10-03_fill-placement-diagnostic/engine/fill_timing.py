#!/usr/bin/env python3
"""Fill-placement diagnostic engine. breadth-thrust-etf, registered 2026-10-03.

A copy of the Portfolio Command Centre fill-timing engine
(Portfolio-Command-Centre/reviews/2026-10-03_fill-timing/fill_timing.py,
sha256 e762808c8d9fa251c75bab88dc7ae76383184167894604684e4f074e7bd6249c at
its freeze), amended only as reviews/2026-10-03_fill-placement-diagnostic/
PREREG.md records. Window, offsets, the blocked null and its default cluster
relations, seeds, bar-defect rules, the alignment rule, dividend rebasing, the
stop conditions and the vectorised-against-scalar parity check are the PCC
engine's.

Question: across the deployed engines' modelled fills (every line whose
target weight changes on a rebalance date, filled at that session's close),
does the post leg (fill to t+3 close) run against the book beyond a placebo
fill at the close of a random nearby session (H-D2, verdict-bearing), and
where does the fill sit in the seven-session range (H-D1, descriptive)?

Amendments to the PCC engine, each recorded in the PREREG:
  (a) the placebo price is the placebo session's close (the fills are
      closes); the PCC rule, uniform inside the session's low-high range on
      the same offsets, is kept as a disclosure cell.
  (b) unit FX: every rate is 1.0; a fill's notional is |dw| x NAV in NAV
      units (the adapter writes q = |dw| x NAV / p).
  (c) the PCC cells this registration does not declare are disabled: the
      P&L share and commission comparison (C1), sell regret (X1), and the
      tail share, worse-than-six-closes, matched-reversal, intraday,
      close-in-place and ex-date-excluded cells (H2, S2, S4, S5, S6, S8).
  (d) --fills and --book name the fills file (trades.json row shape) and the
      minimal book meta; the extract mode is removed (the adapter,
      scripts/ws_fill_placement_adapter.py, writes the extracts).
  (e) the cells this registration declares: H-D2 (post leg), H-D1 (u), the
      pre leg, buys and sells, by sleeve, by calendar year, notional-
      weighted, the sleeve-D (EUR) fills, the uniform-intraday variant,
      leave-one-line-out and leave-one-year-out thinness; the coverage record
      carries the H-D2 floor and the power at it; the verdict mapping is the
      PREREG's. As amended on 2026-10-04 (PREREG, Amendments before the
      freeze): the cells are read on the confirmatory set (fills on or after
      the blend's inception, 2018-10-31), the earlier fills a disclosure cell;
      each unit carries the kinds of its rows (sleeve, overlay_leg,
      overlay_induced) so the overlay rows can be shown on their own; the
      independent and chained same-line nulls are computed as disclosures; the
      H-D2 floor is the spec's economic 10 bp; thinness guards a pass only.
  (f) scale only, no change to any result beyond floating-point summation
      order: the ex-date-in-window test is vectorised by prefix sums, masked
      and weighted null means are matrix-vector products, the cluster
      bootstrap sums per cluster on the PCC's own resampling draws, and the
      placebo matrices hold only the keys the declared cells read.
  (g) the Shenzhen market (exchange SHZ, suffix .SZ, Asia/Shanghai) is added
      to the time-zone and market tables for sleeve C's 159801.SZ line.
  (h) the cluster relations are read from the spec (placebo.block_relations);
      amendment 2 of 2026-10-04 sets them to the session date alone, and the
      PCC engine's pair (same session date; same line with overlapping
      seven-session windows) is kept as the chained disclosure null.

Modes
  coverage   outcome-blind: eligibility, date alignment, ex-dates, window
             widths, the blocked placebo null, the H-D2 floor and the power at
             it. Never computes u, the pre leg or the post leg on any fill.
  run        the registered run, once, after the freeze. Refuses to start if
             the spec, the engine, the bars, the fills or the book meta differ
             from the hashes the coverage record carries, if the fill count
             differs, or if any placebo or actual score is missing.

Charts are rendered by charts.py from results/results.json, so a change to a
chart title never changes the engine hash the run is bound to.

Dates: Python datetime, months 1-indexed. A bar's session date is the calendar
date of its timestamp in the exchange's own time zone, never the UTC date.
Prices: unadjusted o/h/l/c throughout; ac enters only through the adjustment
factor f = ac / c used to put the bars of one window on one dividend basis.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path
from statistics import NormalDist
from zoneinfo import ZoneInfo

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SPEC_PATH = HERE / "prereg_spec.json"
ENGINE_PATH = Path(__file__).resolve()
RESULTS_DIR = HERE / "results"
ND = NormalDist()

TZ_BY_EXCHANGE = {
    "ARCA": "America/New_York", "BATS": "America/New_York", "NMS": "America/New_York",
    "NYS": "America/New_York", "NYQ": "America/New_York", "NGM": "America/New_York",
    "ASX": "Australia/Sydney", "HKG": "Asia/Hong_Kong", "LSE": "Europe/London",
    "PAR": "Europe/Paris", "SGX": "Asia/Singapore", "TSE": "Asia/Tokyo", "XETR": "Europe/Berlin",
    "SHZ": "Asia/Shanghai",   # amendment (g)
}
TZ_BY_SUFFIX = {
    ".SI": "Asia/Singapore", ".HK": "Asia/Hong_Kong", ".T": "Asia/Tokyo", ".AX": "Australia/Sydney",
    ".L": "Europe/London", ".PA": "Europe/Paris", ".DE": "Europe/Berlin",
    ".SZ": "Asia/Shanghai",   # amendment (g)
}
FX_PAIR = {"USD": "USDSGD=X", "HKD": "HKDSGD=X", "EUR": "EURSGD=X", "JPY": "JPYSGD=X", "AUD": "AUDSGD=X"}  # unused under amendment (b)
# which market a Yahoo symbol's suffix denotes, and which market a book exchange code belongs to
SUFFIX_EXCHANGE = {".SI": "SGX", ".HK": "HKG", ".T": "TSE", ".AX": "ASX", ".L": "LSE", ".PA": "PAR", ".DE": "XETR",
                   ".SZ": "SHZ"}   # amendment (g)
EXCHANGE_GROUP = {"ARCA": "US", "BATS": "US", "NMS": "US", "NYS": "US", "NYQ": "US", "NGM": "US",
                  "SGX": "SGX", "HKG": "HKG", "TSE": "TSE", "ASX": "ASX", "LSE": "LSE", "PAR": "PAR", "XETR": "XETR",
                  "SHZ": "SHZ"}   # amendment (g)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(path: Path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def stop(msg: str):
    sys.exit("STOP: " + msg)


# ----------------------------------------------------------------------------
# Bars
# ----------------------------------------------------------------------------
class Series:
    """One symbol's daily bars with session dates, defect flags, range repair
    and the dividend adjustment factor."""

    def __init__(self, yf: str, entry: dict, tz: str, spec: dict):
        bars = entry["history"]
        self.yf = yf
        self.tz = tz
        self.name = entry.get("name")
        zone = ZoneInfo(tz)
        self.dates = [dt.datetime.fromtimestamp(b["d"], dt.timezone.utc).astimezone(zone).date() for b in bars]
        self.utc_hours = Counter(dt.datetime.fromtimestamp(b["d"], dt.timezone.utc).strftime("%H:%M") for b in bars[-60:])
        n = len(bars)
        get = lambda key: np.array([b.get(key) if b.get(key) is not None else np.nan for b in bars], dtype=float)
        self.o, self.h, self.l, self.c, self.ac = get("o"), get("h"), get("l"), get("c"), get("ac")
        self.prov = np.array([bool(b.get("p")) for b in bars])
        with np.errstate(invalid="ignore", divide="ignore"):
            self.f = self.ac / self.c          # a provisional bar hard-sets ac = c, so f = 1 there
        self.bad = np.zeros(n, dtype=bool)
        self.bad_reason = [""] * n
        self.range_repaired = np.zeros(n, dtype=bool)
        defects = spec["bar_defects"]
        jump = defects["bad_print_relative_jump"]
        explicit = {(e["yf"], e["date"]) for e in defects["explicit_exclusions"]}
        last_good_c = float("nan")               # the jump test compares against the last bar not already flagged
        for i in range(n):
            o, h, l, c, ac = self.o[i], self.h[i], self.l[i], self.c[i], self.ac[i]
            reason = ""
            if any(math.isnan(x) for x in (o, h, l, c, ac)):
                reason = "null field"
            elif (yf, self.dates[i].isoformat()) in explicit:
                reason = "explicit exclusion"
            elif h < l * (1 - 1e-9):
                reason = "high below low"
            elif o == h == l == c and not math.isnan(last_good_c) and c == last_good_c:
                reason = "fabricated bar"
            elif (not math.isnan(last_good_c) and last_good_c > 0 and abs(c / last_good_c - 1) > jump
                  and i + 1 < n and not math.isnan(self.c[i + 1]) and abs(self.c[i + 1] / last_good_c - 1) <= jump / 2):
                # a one-bar spike that the next bar reverts: a print, not a move (3010.HK 2025-10-24 pattern)
                reason = "bad print (jump and revert)"
            if reason:
                self.bad[i] = True
                self.bad_reason[i] = reason
                continue
            # a session's range must contain its own open and close; Yahoo's London lines
            # print highs below the open, so the range is widened to the prints it reports
            hh, ll = max(h, o, c), min(l, o, c)
            if hh != h or ll != l:
                self.range_repaired[i] = True
                self.h[i], self.l[i] = hh, ll
            last_good_c = c
        # ex-date flags: a relative change in f between consecutive finalised, good bars
        tol = spec["dividend_handling"]["ex_date_tolerance_relative_change_in_f"]
        self.ex = np.zeros(n, dtype=bool)
        self.ex_size = np.zeros(n, dtype=float)  # implied distribution as a share of the prior close
        for i in range(1, n):
            if self.prov[i] or self.prov[i - 1] or self.bad[i] or self.bad[i - 1]:
                continue
            f0, f1 = self.f[i - 1], self.f[i]
            if math.isnan(f0) or math.isnan(f1) or f0 <= 0:
                continue
            rel = f1 / f0 - 1
            if abs(rel) > tol:
                self.ex[i] = True
                self.ex_size[i] = 1 - f0 / f1   # f0 = f1 (1 - D / c_{i-1})
        # amendment (f): prefix sums of the ex flags, so the test "any ex flag in
        # i-k+1..i+k" is one subtraction per centre (identical to ex_in_window)
        self.ex_cum = np.concatenate([[0], np.cumsum(self.ex.astype(np.int64))])
        self.index_by_date = {d: i for i, d in enumerate(self.dates)}
        # clean-window mask for every centre index, for the window half-width k
        k = spec["window_sessions_each_side"]
        flag = self.bad | self.prov
        self.clean_centre = np.zeros(n, dtype=bool)
        for i in range(k, n - k):
            self.clean_centre[i] = not flag[i - k:i + k + 1].any()

    def clean_window(self, i: int, k: int) -> bool:
        return 0 <= i < len(self.dates) and bool(self.clean_centre[i])

    def window(self, i: int, k: int):
        """Bars i-k..i+k rebased to bar i's adjustment factor."""
        sl = slice(i - k, i + k + 1)
        scale = self.f[sl] / self.f[i]
        return self.o[sl] * scale, self.h[sl] * scale, self.l[sl] * scale, self.c[sl] * scale

    def ex_in_window(self, i: int, k: int) -> bool:
        # an ex-date at bar j shifts the basis between j-1 and j; bars i-k..i+k
        # are affected by flags at j in i-k+1..i+k
        return bool(self.ex[i - k + 1:i + k + 1].any())

    def ex_in_window_many(self, j: np.ndarray, k: int) -> np.ndarray:
        """Amendment (f): ex_in_window for an array of centres with clean windows
        (k <= j <= n-1-k), by prefix sums."""
        return (self.ex_cum[j + k + 1] - self.ex_cum[j - k + 1]) > 0


# ----------------------------------------------------------------------------
# Metric
# ----------------------------------------------------------------------------
def adverse_rank(price: float, side: int, H: float, L: float) -> float:
    """u in [0, 1]; 1 is the worst possible placement for the side. NaN in, NaN out."""
    if any(math.isnan(x) for x in (price, H, L)) or H <= L:
        return float("nan")
    u = (price - L) / (H - L) if side > 0 else (H - price) / (H - L)
    return min(1.0, max(0.0, u))


def score(price: float, side: int, o, h, l, c, k: int) -> dict:
    """Score one fill at `price` on the centre bar of a 2k+1 window."""
    H, L = float(np.max(h)), float(np.min(l))
    u = adverse_rank(price, side, H, L)
    neighbours = np.concatenate([c[:k], c[k + 1:]])
    worse6 = bool(price > neighbours.max()) if side > 0 else bool(price < neighbours.min())
    pre = side * (price / c[0] - 1)            # positive: bought after a rise, sold after a fall
    post = -side * (c[-1] / price - 1)         # positive: fell after a buy, rose after a sell
    u_close = adverse_rank(c[k], side, H, L)
    return {"u": u, "worse6": worse6, "pre": pre, "post": post, "u_close": u_close, "H": H, "L": L}


def score_many(s: Series, j: np.ndarray, unif: np.ndarray, side: int, k: int, price_rule: str = "uniform") -> dict:
    """Vectorised placebo scoring: one placebo per element of j, priced at
    l_j + unif (h_j - l_j) on the placebo session's own basis, the window
    rebased to bar j. Same arithmetic as score(). Amendment (a): with
    price_rule "close" the placebo is priced at the placebo session's own
    unadjusted close c_j (unif unused)."""
    idx = j[:, None] + np.arange(-k, k + 1)[None, :]
    scale = s.f[idx] / s.f[j][:, None]
    h = s.h[idx] * scale; l = s.l[idx] * scale; c = s.c[idx] * scale
    H = h.max(axis=1); L = l.min(axis=1)
    if price_rule == "close":
        price = s.c[j].astype(float)
    elif price_rule == "uniform":
        price = s.l[j] + unif * (s.h[j] - s.l[j])
    else:
        raise ValueError(f"unknown price_rule {price_rule}")
    u = (price - L) / (H - L) if side > 0 else (H - price) / (H - L)
    u = np.clip(u, 0.0, 1.0)
    nb = np.delete(c, k, axis=1)
    worse6 = (price > nb.max(axis=1)) if side > 0 else (price < nb.min(axis=1))
    pre = side * (price / c[:, 0] - 1)
    post = -side * (c[:, -1] / price - 1)
    uc = (c[:, k] - L) / (H - L) if side > 0 else (H - c[:, k]) / (H - L)
    uc = np.clip(uc, 0.0, 1.0)
    ex = s.ex_in_window_many(j, k)   # amendment (f): identical to [s.ex_in_window(int(x), k) for x in j]
    return {"u": u, "worse6": worse6.astype(float), "pre": pre, "post": post, "u_close": uc, "H": H, "L": L, "price": price, "ex": ex.astype(float)}


# ----------------------------------------------------------------------------
# Inputs
# ----------------------------------------------------------------------------
def tz_for(yf: str, exchange: str | None) -> str:
    if exchange in TZ_BY_EXCHANGE:
        return TZ_BY_EXCHANGE[exchange]
    for suf, tz in TZ_BY_SUFFIX.items():
        if yf.endswith(suf):
            return tz
    return "America/New_York"


UNIT_FX_TAG = "unit FX (amendment b)"


def fx_lookup(fx: dict | None = None):
    """Amendment (b): unit FX. Every rate is 1.0, so a fill's notional is
    q x p = |dw| x NAV in NAV units as the adapter writes it. The PCC engine
    read SGD rates from an FX extract here; this registration declares no
    currency conversion (u and the per-cent legs are unit-free)."""
    def get(ccy: str, date: dt.date):
        return 1.0, UNIT_FX_TAG

    return get


def load_fills(spec: dict, trades: list, book: dict, history: dict) -> tuple[list, list]:
    """Aggregate rows to ticker x date x side units and apply the feed and type
    exclusions. Returns (units, excluded)."""
    meta = book["meta"]
    meta_by_yf = {m["yf"]: dict(m, ticker=t) for t, m in meta.items() if m.get("yf")}
    noquote = {m["yf"] for m in meta.values() if m.get("noQuote") and m.get("yf")}
    groups = defaultdict(list)
    for r in trades:
        groups[(r.get("yf"), r["d"], r["a"])].append(r)
    units, excluded = [], []
    for (yf, d, a), rows in sorted(groups.items(), key=lambda kv: (kv[0][1], str(kv[0][0]), kv[0][2])):
        q = sum(r["q"] for r in rows)
        vwap = sum(r["q"] * r["p"] for r in rows) / q
        fees = [r.get("fee") for r in rows]
        fee = sum(f for f in fees if f is not None) if any(f is not None for f in fees) else None
        unit = {
            "yf": yf, "ticker": rows[0]["t"], "date": d, "side": 1 if a == "B" else -1, "side_label": a,
            "qty": q, "price": vwap, "ccy": rows[0]["ccy"], "theme": rows[0].get("th"), "rows": len(rows),
            "fee": fee, "ref": any(bool(r.get("ref")) for r in rows),
            # amendment 6: the kinds of the rows this unit aggregates (sleeve, overlay_leg,
            # overlay_induced); a sleeve fill and an induced fill on one line, date and side
            # are one unit carried with both tags
            "kinds": sorted({r.get("kind", "sleeve") for r in rows}),
        }
        # the book is keyed by the trade's own ticker (GDX.US and GDX.GB share one feed symbol)
        m = meta.get(rows[0]["t"]) or (meta_by_yf.get(yf) if yf else None)
        if not yf:
            excluded.append(dict(unit, reason="no feed symbol"))
            continue
        if yf not in history:
            excluded.append(dict(unit, reason="symbol absent from history.json"))
            continue
        if m and m.get("type") in ("Bond", "Cash"):
            excluded.append(dict(unit, reason=f"book type {m['type']}"))
            continue
        if yf in noquote:
            excluded.append(dict(unit, reason="noQuote in book meta"))
            continue
        feed_exchange = next((ex for suf, ex in SUFFIX_EXCHANGE.items() if yf.endswith(suf)), "US")
        book_exchange = (m or {}).get("exchange")
        book_group = EXCHANGE_GROUP.get(book_exchange) if book_exchange else None
        if book_group and book_group != feed_exchange:
            excluded.append(dict(unit, reason=f"proxy feed: book exchange {book_exchange}, feed symbol {yf}"))
            continue
        unit["name"] = (m or {}).get("name") or history[yf].get("name")
        unit["exchange"] = book_exchange
        units.append(unit)
    return units, excluded


def align(units: list, series: dict, spec: dict) -> tuple[list, list, dict]:
    """Place each fill on a session of its own exchange and check the price
    against that session's range. Mechanical rule from the spec."""
    tol = spec["alignment"]["own_range_tolerance_relative"]
    kept, dropped = [], []
    tally = Counter()

    def inside(s: Series, i: int, p: float) -> bool:
        return (s.l[i] * (1 - tol) <= p <= s.h[i] * (1 + tol)) and not s.bad[i] and not s.prov[i]

    for u in units:
        s = series[u["yf"]]
        d = dt.date.fromisoformat(u["date"])
        p = u["price"]
        i = s.index_by_date.get(d)
        u["dated_session_exists"] = i is not None
        if i is None:
            prev = [j for j, sd in enumerate(s.dates) if sd < d]
            cand_prev = prev[-1] if prev else None
            nxt = [j for j, sd in enumerate(s.dates) if sd > d]
            cand_next = nxt[0] if nxt else None
            if cand_prev is not None and inside(s, cand_prev, p):
                u["session_index"], u["alignment"] = cand_prev, "non-session date; previous session holds the price"
            elif cand_next is not None and inside(s, cand_next, p):
                u["session_index"], u["alignment"] = cand_next, "non-session date; next session holds the price"
            else:
                u["alignment"] = "non-session date; no adjacent session holds the price"
                tally[u["alignment"]] += 1
                dropped.append(dict(u, reason="alignment: " + u["alignment"]))
                continue
        elif s.prov[i]:
            u["session_index"], u["alignment"] = i, "dated session is a provisional bar (not finalised)"
        else:
            own = inside(s, i, p)
            prev_ok = i - 1 >= 0 and inside(s, i - 1, p)
            next_ok = i + 1 < len(s.dates) and inside(s, i + 1, p)
            u["inside_own_range"] = own
            u["inside_prev_range"] = prev_ok
            u["inside_next_range"] = next_ok
            if own:
                u["session_index"], u["alignment"] = i, "inside own session range"
            elif prev_ok:
                u["session_index"], u["alignment"] = i - 1, "outside own range; previous session holds the price (re-dated)"
            elif next_ok:
                u["session_index"], u["alignment"] = i + 1, "outside own range; next session holds the price (re-dated)"
            else:
                u["alignment"] = "outside own range; no adjacent session holds the price"
                tally[u["alignment"]] += 1
                dropped.append(dict(u, reason="alignment: " + u["alignment"]))
                continue
        tally[u["alignment"]] += 1
        u["session_date"] = s.dates[u["session_index"]].isoformat()
        kept.append(u)
    return kept, dropped, dict(tally)


def complete_windows(units: list, series: dict, spec: dict) -> tuple[list, list]:
    k = spec["window_sessions_each_side"]
    kept, dropped = [], []
    for u in units:
        s = series[u["yf"]]
        i = u["session_index"]
        if not s.clean_window(i, k):
            lo, hi = i - k, i + k
            why = "edge of history" if (lo < 0 or hi >= len(s.dates)) else (
                "provisional bar in window" if s.prov[max(0, lo):hi + 1].any() else "defective bar in window")
            dropped.append(dict(u, reason="incomplete window: " + why))
            continue
        u["ex_in_window"] = s.ex_in_window(i, k)
        if u["ex_in_window"]:
            js = [j for j in range(i - k + 1, i + k + 1) if s.ex[j]]
            u["ex_dates"] = [{"date": s.dates[j].isoformat(), "implied_distribution_share": round(float(s.ex_size[j]), 6)} for j in js]
        kept.append(u)
    return kept, dropped


def placebo_offsets(s: Series, i: int, spec: dict) -> np.ndarray:
    k = spec["window_sessions_each_side"]
    lo, hi = spec["placebo"]["offset_min_sessions"], spec["placebo"]["offset_max_sessions"]
    offs = [o for o in list(range(-hi, -lo + 1)) + list(range(lo, hi + 1)) if s.clean_window(i + o, k)]
    return np.array(offs, dtype=int)


BLOCK_RELATIONS_PCC = ("same_session_date", "same_line_overlapping_windows")


def make_blocks(fills: list, k: int, relations: tuple = BLOCK_RELATIONS_PCC) -> np.ndarray:
    """Cluster id per fill: fills on one session date share a block, and so do
    fills on one name whose seven-session windows overlap (centres within 2k
    bars). Union-find over both relations. Amendment (h): the relations are
    read from the spec; the PCC engine's pair is the default."""
    unknown = set(relations) - set(BLOCK_RELATIONS_PCC)
    if unknown:
        stop(f"unknown block relation(s) {sorted(unknown)}")
    n = len(fills)
    parent = list(range(n))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    if "same_session_date" in relations:
        by_date = defaultdict(list)
        for r, f in enumerate(fills):
            by_date[f["session_date"]].append(r)
        for rs in by_date.values():
            for r in rs[1:]:
                union(rs[0], r)
    if "same_line_overlapping_windows" in relations:
        by_sym = defaultdict(list)
        for r, f in enumerate(fills):
            by_sym[f["yf"]].append(r)
        for rs in by_sym.values():
            for a in rs:
                for b in rs:
                    if a < b and abs(fills[a]["session_index"] - fills[b]["session_index"]) <= 2 * k:
                        union(a, b)
    roots = [find(r) for r in range(n)]
    ids = {root: i for i, root in enumerate(sorted(set(roots)))}
    return np.array([ids[r] for r in roots], dtype=int)


# ----------------------------------------------------------------------------
# Build
# ----------------------------------------------------------------------------
def _rel(path: Path) -> str:
    return str(path.relative_to(HERE)) if path.is_relative_to(HERE) else str(path)


def build(spec: dict, args) -> dict:
    # amendment (d): the fills file (trades.json row shape) and the minimal book
    # meta are named by --fills and --book instead of the repository root files
    fills_path, book_path = Path(args.fills), Path(args.book)
    trades = load_json(fills_path)
    book = load_json(book_path)
    history_path = Path(args.history)
    history = load_json(history_path)
    extract_provenance = history.pop("_provenance", None)
    units, excluded = load_fills(spec, trades, book, history)
    series = {}
    for u in units:
        if u["yf"] not in series:
            series[u["yf"]] = Series(u["yf"], history[u["yf"]], tz_for(u["yf"], u.get("exchange")), spec)
    aligned, drop_align, align_tally = align(units, series, spec)
    complete, drop_window = complete_windows(aligned, series, spec)
    get_fx = fx_lookup()   # amendment (b): unit FX
    for u in complete:
        rate, src = get_fx(u["ccy"], dt.date.fromisoformat(u["session_date"]))
        u["fx"], u["fx_source"] = rate, src
        u["notional_nav"] = u["qty"] * u["price"] * rate if rate else None   # |dw| x NAV, NAV units
    if any(u["notional_nav"] is None for u in complete):
        stop("a complete fill has no notional")
    k = spec["window_sessions_each_side"]
    blocks = make_blocks(complete, k, tuple(spec["placebo"].get("block_relations", BLOCK_RELATIONS_PCC)))
    last_bar = max(s.dates[-1] for s in series.values()).isoformat()
    provenance = {
        # the key names are the PCC engine's, so the stop conditions read unchanged;
        # under amendment (d) they hash the --fills and --book files
        "trades_json_sha256": sha256_of(fills_path),
        "book_json_sha256": sha256_of(book_path),
        "fills_json_path": _rel(fills_path),
        "book_json_path": _rel(book_path),
        "history_json_path": _rel(history_path),
        "history_json_sha256": sha256_of(history_path),
        "history_extract_provenance": extract_provenance,
        "history_last_session": last_bar,
        "fx_json_path": None,
        "fx_json_sha256": UNIT_FX_TAG,   # amendment (b): no FX file; the stop condition compares this constant
        "fx_extract_provenance": None,
        "spec_sha256": sha256_of(SPEC_PATH),
        "engine_sha256": sha256_of(ENGINE_PATH),
        "rows_in_trades_json": len(trades),
        "adapter_provenance": book.get("_provenance"),
    }
    return {
        "spec": spec, "trades": trades, "book": book, "series": series, "units": units, "excluded": excluded,
        "aligned": aligned, "drop_align": drop_align, "align_tally": align_tally, "complete": complete,
        "drop_window": drop_window, "get_fx": get_fx, "provenance": provenance, "blocks": blocks,
    }


PLACEBO_KEYS = ("u", "pre", "post")   # amendment (f): the keys the declared cells read, plus "ex"
VARIANT_GROUPS = ("all", "B", "S")


def simulate_placebo(ctx: dict, rng: np.random.Generator, draws: int, blocked: bool,
                     uniform_variant: bool = False, blocks: np.ndarray | None = None) -> dict:
    """Draw `draws` placebo sessions and prices for every complete fill and
    score them. Blocked: every fill in a cluster takes the same offset in each
    draw, from the offsets eligible for every member, so fills that share a
    session date or overlapping windows keep their common market move under
    the null; the intraday uniform is independent per fill. Uses dates and
    sides only; the actual price enters nowhere here.

    Amendment (a): the primary placebo is priced at the placebo session's
    close. The random-number stream is the PCC engine's (the cluster offset,
    then per member its own offset when it has no common one, then its intraday
    uniform), so the offsets drawn are the PCC procedure's; with
    uniform_variant the same offsets are also priced by the PCC's uniform
    intraday rule and accumulated as per-draw sums over all fills, buys and
    sells (the disclosure cell). Amendment (f): only u, pre, post and the
    ex-date flag are held per fill and draw."""
    spec = ctx["spec"]
    k = spec["window_sessions_each_side"]
    price_rule = spec["placebo"].get("price_rule_code", "close")
    fills = ctx["complete"]
    n = len(fills)
    keys = PLACEBO_KEYS
    out = {key: np.full((n, draws), np.nan) for key in keys}
    out["ex"] = np.full((n, draws), np.nan, dtype=np.float32)
    variant = {key: {g: np.zeros(draws) for g in VARIANT_GROUPS} for key in keys} if uniform_variant else None
    variant_n = {g: 0 for g in VARIANT_GROUPS}
    conf = confirmatory_mask(fills, spec)   # the variant cell is read on the confirmatory set (amendment 5)
    offsets_count = np.zeros(n, dtype=int)
    one_sided = np.zeros(n, dtype=bool)
    fallback_blocks = 0
    blocks = ((ctx["blocks"] if blocks is None else blocks) if blocked else np.arange(n))
    for b in sorted(set(blocks.tolist())):
        members = np.where(blocks == b)[0]
        per = []
        for r in members:
            s = ctx["series"][fills[r]["yf"]]
            offs = placebo_offsets(s, fills[r]["session_index"], spec)
            per.append(set(offs.tolist()))
            offsets_count[r] = len(offs)
            one_sided[r] = bool(len(offs) and (min(offs) > 0 or max(offs) < 0))
        common = set.intersection(*per) if per else set()
        if common and blocked and len(members) > 1:
            pick_block = rng.choice(np.array(sorted(common), dtype=int), size=draws, replace=True)
        else:
            if blocked and len(members) > 1:
                fallback_blocks += 1
            pick_block = None
        for r_idx, r in enumerate(members):
            s = ctx["series"][fills[r]["yf"]]
            i = fills[r]["session_index"]
            if pick_block is None:
                offs = np.array(sorted(per[r_idx]), dtype=int)
                if len(offs) == 0:
                    continue
                pick = rng.choice(offs, size=draws, replace=True)
            else:
                pick = pick_block
            unif = rng.random(draws)
            sc = score_many(s, i + pick, unif, fills[r]["side"], k, price_rule=price_rule)
            for key in keys:
                out[key][r, :] = sc[key]
            out["ex"][r, :] = sc["ex"]
            if variant is not None and conf[r]:
                scu = score_many(s, i + pick, unif, fills[r]["side"], k, price_rule="uniform")
                g = "B" if fills[r]["side"] > 0 else "S"
                for key in keys:
                    variant[key]["all"] += scu[key]
                    variant[key][g] += scu[key]
                variant_n["all"] += 1
                variant_n[g] += 1
    out["offsets_count"] = offsets_count
    out["one_sided"] = one_sided
    out["fallback_blocks"] = fallback_blocks
    out["blocked"] = blocked
    out["draws"] = draws
    out["price_rule"] = price_rule
    if variant is not None:
        # per-draw means of the uniform-intraday variant, by group
        out["uniform_variant"] = {key: {g: variant[key][g] / variant_n[g] for g in VARIANT_GROUPS if variant_n[g]}
                                  for key in keys}
        out["uniform_variant_n"] = variant_n
    return out


def power_normal(delta: float, sd_null: float, alpha: float) -> float:
    if sd_null <= 0:
        return float("nan")
    return ND.cdf(delta / sd_null - ND.inv_cdf(1 - alpha))


def mde(sd_null: float, alpha: float, power: float = 0.8) -> float:
    return sd_null * (ND.inv_cdf(1 - alpha) + ND.inv_cdf(power))


def n_for(sd_null: float, n: int, delta: float, alpha: float, power: float) -> int:
    """Approximate fill count for the given power at delta, scaling the null sd
    by 1/sqrt(n); assumes the clustering ratio holds as the ledger grows."""
    target_sd = delta / (ND.inv_cdf(1 - alpha) + ND.inv_cdf(power))
    return int(math.ceil(n * (sd_null / target_sd) ** 2))


# ----------------------------------------------------------------------------
# Coverage (outcome-blind)
# ----------------------------------------------------------------------------
def confirmatory_mask(fills: list, spec: dict) -> np.ndarray:
    """Amendment 5: the confirmatory set is the fills dated (on the engines'
    own fill date) on or after the blend's inception; the earlier fills are a
    disclosure cell and never verdict-bearing."""
    start = spec["confirmatory"]["from_fill_date"]
    return np.array([f["date"] >= start for f in fills], dtype=bool)


def has_kind(fills: list, kind: str) -> np.ndarray:
    return np.array([kind in (f.get("kinds") or ["sleeve"]) for f in fills], dtype=bool)


def masked_means(mat: np.ndarray, mask: np.ndarray, weights: np.ndarray | None = None) -> np.ndarray:
    """Per-draw (weighted) mean over the fills in mask; amendment (f): a
    matrix-vector product in place of mat[mask].mean(axis=0)."""
    wv = mask.astype(float) if weights is None else weights * mask
    return (wv @ mat) / wv.sum()


def ceil_to(x: float, unit: float) -> float:
    """x rounded up to the next multiple of unit (at least one unit)."""
    return round(max(1, math.ceil(x / unit - 1e-9)) * unit, 10)


def null_summary(pl: dict, mask: np.ndarray, alpha: float, power_target: float, delta2: float) -> dict:
    mu, mp = masked_means(pl["u"], mask), masked_means(pl["post"], mask)
    sd_u, sd_p = float(mu.std(ddof=1)), float(mp.std(ddof=1))
    return {"draws": int(pl["draws"]), "mean_u": round(float(mu.mean()), 4), "sd_mean_u": round(sd_u, 5),
            "mean_post_pct": round(float(mp.mean()) * 100, 4), "sd_mean_post_pct": round(sd_p * 100, 5),
            "mde_post_at_target": round(mde(sd_p, alpha, power_target), 7),
            "power_at_delta2": round(power_normal(delta2, sd_p, alpha), 4)}


def coverage(ctx: dict, placebo: dict, placebo_indep: dict, placebo_chained: dict) -> dict:
    spec = ctx["spec"]
    alpha = spec["alpha_one_sided"]
    power_target = spec["power_target"]
    fills = ctx["complete"]
    n = len(fills)
    for key in PLACEBO_KEYS:
        if np.isnan(placebo[key]).any():
            stop(f"placebo matrix {key} has a missing value")
    conf = confirmatory_mask(fills, spec)
    preb = ~conf
    if not conf.any():
        stop("no confirmatory fill")
    U, PRE, POST = placebo["u"], placebo["pre"], placebo["post"]
    mu, mpre, mpost = masked_means(U, conf), masked_means(PRE, conf), masked_means(POST, conf)
    w = np.array([f["notional_nav"] for f in fills])
    wmu, wmpost = masked_means(U, conf, w), masked_means(POST, conf, w)
    sd_u, sd_post = float(mu.std(ddof=1)), float(mpost.std(ddof=1))
    delta2 = spec["floors"]["H_D2_delta_price"]
    mde_post, mde_u = mde(sd_post, alpha, power_target), mde(sd_u, alpha, power_target)
    d1_floor = ceil_to(mde_u, spec["floors"]["H_D1_reference_floor_rounding_unit"])
    k = spec["window_sessions_each_side"]
    widths = []
    for f, c_ in zip(fills, conf):
        if c_:
            s = ctx["series"][f["yf"]]
            o, h, l, c = s.window(f["session_index"], k)
            widths.append((h.max() - l.min()) / c[k])
    widths = np.array(widths)
    F_conf = [f for f, c_ in zip(fills, conf) if c_]
    by_sym = Counter(f["yf"] for f in F_conf)
    by_date = Counter(f["session_date"] for f in F_conf)
    blocks = ctx["blocks"]
    conf_blocks = Counter(blocks[conf].tolist())
    kinds_all = Counter("+".join(f.get("kinds") or ["sleeve"]) for f in fills)
    kinds_conf = Counter("+".join(f.get("kinds") or ["sleeve"]) for f in F_conf)
    bad_bars = [{"yf": yf, "date": s.dates[i].isoformat(), "reason": s.bad_reason[i]}
                for yf, s in ctx["series"].items() for i in np.where(s.bad)[0]]
    prov_bars = [{"yf": yf, "date": s.dates[i].isoformat()} for yf, s in ctx["series"].items() for i in np.where(s.prov)[0]]
    aligned_all = ctx["aligned"] + ctx["drop_align"]
    return {
        "registered": spec["registered"],
        "provenance": ctx["provenance"],
        "counts": {
            "rows": len(ctx["trades"]), "units": len(ctx["units"]) + len(ctx["excluded"]),
            "excluded_feed_or_type": len(ctx["excluded"]), "dropped_alignment": len(ctx["drop_align"]),
            "dropped_window": len(ctx["drop_window"]), "complete": n,
            "confirmatory": int(conf.sum()), "pre_blend": int(preb.sum()),
            "confirmatory_from_fill_date": spec["confirmatory"]["from_fill_date"],
            "pre_blend_by_sleeve": dict(sorted(Counter(str(f.get("theme")) for f, c_ in zip(fills, conf) if not c_).items())),
            "by_kind_all": dict(sorted(kinds_all.items())), "by_kind_confirmatory": dict(sorted(kinds_conf.items())),
            "overlay_induced_confirmatory": int((has_kind(fills, "overlay_induced") & conf).sum()),
            "overlay_legs_confirmatory": int((has_kind(fills, "overlay_leg") & conf).sum()),
            "by_side": dict(Counter(f["side_label"] for f in F_conf)),
            "by_sleeve": dict(sorted(Counter(str(f.get("theme")) for f in F_conf).items())),
            "by_year": dict(sorted(Counter(f["session_date"][:4] for f in F_conf).items())),
            "by_ccy": dict(Counter(f["ccy"] for f in F_conf)), "lines": len(by_sym),
            "largest_line_shares": [{"yf": s_, "fills": c_} for s_, c_ in by_sym.most_common(5)],
            "ex_date_in_window": int(sum(1 for f in F_conf if f.get("ex_in_window"))),
            "session_dates": len(by_date),
            "blocks": len(conf_blocks),
            "block_size_distribution": {str(k_): v for k_, v in sorted(Counter(conf_blocks.values()).items())},
            "largest_block_fills": int(max(conf_blocks.values())),
            "largest_block_share": round(max(conf_blocks.values()) / int(conf.sum()), 4),
            # amendment (f): a Counter in place of the PCC's quadratic scan; same count
            "fills_sharing_a_date": int(sum(1 for f in F_conf if by_date[f["session_date"]] > 1)),
            "notional_nav": {"total": round(float((w * conf).sum()), 4),
                             "buys": round(float(sum(f["notional_nav"] for f in F_conf if f["side"] > 0)), 4),
                             "sells": round(float(sum(f["notional_nav"] for f in F_conf if f["side"] < 0)), 4)},
        },
        "exclusions_feed_or_type": [{k_: e[k_] for k_ in ("ticker", "date", "side_label", "reason")} for e in ctx["excluded"]],
        "alignment": {
            "tally": ctx["align_tally"],
            "share_outside_own_range": round(sum(1 for u in aligned_all if u.get("dated_session_exists") and not u.get("inside_own_range", True)) / max(1, len(aligned_all)), 4),
            "dated_on_non_session": [{k_: u[k_] for k_ in ("ticker", "date", "side_label", "alignment")} for u in aligned_all if not u.get("dated_session_exists")],
            "outside_own_range": [{k_: u.get(k_) for k_ in ("ticker", "date", "side_label", "alignment")} for u in aligned_all if u.get("dated_session_exists") and not u.get("inside_own_range", True)],
            "dropped": [{k_: u[k_] for k_ in ("ticker", "date", "side_label", "reason")} for u in ctx["drop_align"]],
        },
        "dropped_window": [{k_: u[k_] for k_ in ("ticker", "date", "side_label", "reason")} for u in ctx["drop_window"]],
        "ex_dates_in_windows": [{"ticker": f["ticker"], "date": f["session_date"], "side": f["side_label"], "ex": f["ex_dates"]} for f in F_conf if f.get("ex_in_window")],
        "defective_bars": bad_bars,
        "provisional_bars": prov_bars,
        "range_repaired_bars": [{"yf": yf, "date": s.dates[i].isoformat()} for yf, s in ctx["series"].items() for i in np.where(s.range_repaired)[0]],
        "bar_timestamps_utc": {yf: s.utc_hours.most_common(2) for yf, s in ctx["series"].items()},
        "window_width": {
            "median_range_over_close": round(float(np.median(widths)), 4),
            "p25": round(float(np.percentile(widths, 25)), 4), "p75": round(float(np.percentile(widths, 75)), 4),
            "H_D2_floor_bps_of_price": round(delta2 * 1e4, 1),
        },
        "null": {
            "structure": spec["placebo"]["structure"],
            "block_relations": list(spec["placebo"]["block_relations"]),
            "price_rule": placebo["price_rule"],
            "draws_per_fill": int(placebo["draws"]), "seed": spec["seed"],
            "blocks_all": int(blocks.max()) + 1 if n else 0, "fallback_blocks_without_a_common_offset": int(placebo["fallback_blocks"]),
            "placebo_offsets_per_fill_min": int(placebo["offsets_count"].min()), "median": int(np.median(placebo["offsets_count"])),
            "one_sided_pools": [{"ticker": fills[r]["ticker"], "date": fills[r]["session_date"]} for r in np.where(placebo["one_sided"])[0]],
            "confirmatory": {"mean_u": round(float(mu.mean()), 4), "sd_mean_u": round(sd_u, 5),
                             "mean_u_weighted": round(float(wmu.mean()), 4), "sd_mean_u_weighted": round(float(wmu.std(ddof=1)), 5),
                             "mean_pre_pct": round(float(mpre.mean()) * 100, 4),
                             "mean_post_pct": round(float(mpost.mean()) * 100, 4), "sd_mean_post_pct": round(sd_post * 100, 5),
                             "mean_post_weighted_pct": round(float(wmpost.mean()) * 100, 4), "sd_mean_post_weighted_pct": round(float(wmpost.std(ddof=1)) * 100, 5),
                             "p05_mean_post_pct": round(float(np.percentile(mpost, 5)) * 100, 4), "p95_mean_post_pct": round(float(np.percentile(mpost, 95)) * 100, 4),
                             "p05_mean_u": round(float(np.percentile(mu, 5)), 4), "p95_mean_u": round(float(np.percentile(mu, 95)), 4),
                             "share_of_placebo_windows_with_ex_date": round(float(np.nanmean(placebo["ex"][conf])), 4)},
            "pre_blend": (null_summary(placebo, preb, alpha, power_target, delta2) | {"n": int(preb.sum())}) if preb.any() else None,
            "disclosure_independent_per_fill": null_summary(placebo_indep, conf, alpha, power_target, delta2),
            "disclosure_chained_same_line": null_summary(placebo_chained, conf, alpha, power_target, delta2)
                                            | {"relations": list(BLOCK_RELATIONS_PCC), "clusters_all": int(placebo_chained["n_blocks"])},
        },
        "power": {
            "alpha_one_sided": alpha, "power_target": power_target,
            "H_D2": {"delta": delta2, "delta_basis": spec["floors"]["H_D2_floor_basis"],
                     "sd_mean_post": round(sd_post, 7), "mde_at_target": round(mde_post, 7),
                     "delta_over_mde": round(delta2 / mde_post, 3) if mde_post > 0 else None,
                     "power_at_delta": round(power_normal(delta2, sd_post, alpha), 4),
                     "note": "an economic floor (amendment 3), so the power at it is a measurement on the blocked null; the demotion rule binds if it is below the target"},
            "H_D1": {"sd_mean_u": round(sd_u, 6), "mde_at_target": round(mde_u, 5),
                     "reference_floor": d1_floor, "power_at_reference_floor": round(power_normal(d1_floor, sd_u, alpha), 4),
                     "note": "descriptive headline; the reference floor is the MDE rounded up to 0.01 in u, so its power is a construction; no verdict"},
        },
    }


# ----------------------------------------------------------------------------
# Run
# ----------------------------------------------------------------------------
def pvalue_ge(null: np.ndarray, actual: float) -> float:
    return float((np.sum(null >= actual) + 1) / (len(null) + 1))


def run(ctx: dict, placebo: dict, cov: dict, args, placebo_indep: dict, placebo_chained: dict) -> dict:
    spec = ctx["spec"]
    alpha = spec["alpha_one_sided"]
    k = spec["window_sessions_each_side"]
    fills = ctx["complete"]
    blocks = ctx["blocks"]
    n = len(fills)
    # registered stop conditions, mechanical
    for key in ("spec_sha256", "engine_sha256", "history_json_sha256", "fx_json_sha256", "trades_json_sha256", "book_json_sha256"):
        if cov["provenance"][key] != ctx["provenance"][key]:
            stop(f"{key} differs from the frozen coverage record")
    if n != cov["counts"]["complete"]:
        stop(f"fill count {n} differs from the frozen {cov['counts']['complete']}")
    for pl in (placebo, placebo_indep, placebo_chained):
        for key in PLACEBO_KEYS:
            if np.isnan(pl[key]).any():
                stop(f"placebo matrix {key} has a missing value")
    rng = np.random.default_rng(spec["seed"] + 1)
    # actual scores, with a parity check against the vectorised path
    for f in fills:
        s = ctx["series"][f["yf"]]
        i = f["session_index"]
        o, h, l, c = s.window(i, k)
        sc = score(f["price"], f["side"], o, h, l, c, k)
        # amendment (i): a zero-range fill session implies position 0 (price = low = close),
        # where the PCC form divided 0 by 0 and left the parity check comparing a missing value
        unif = 0.0 if s.h[i] == s.l[i] else (f["price"] - s.l[i]) / (s.h[i] - s.l[i])
        vec = score_many(s, np.array([i]), np.array([unif]), f["side"], k)
        for key in ("u", "pre", "post", "u_close", "H", "L"):
            # amendment (i): a missing value on either path is a parity failure
            if not abs(float(vec[key][0]) - float(sc[key])) <= 1e-9:
                stop(f"parity failure on {key} for {f['ticker']} {f['session_date']}")
        f.update({key: (bool(sc[key]) if key == "worse6" else float(sc[key])) for key in sc})
        if any(math.isnan(f[key]) for key in ("u", "pre", "post")):
            stop(f"actual score missing for {f['ticker']} {f['session_date']}")
    F = fills
    au = np.array([f["u"] for f in F])
    apre = np.array([f["pre"] for f in F]); apost = np.array([f["post"] for f in F])
    w = np.array([f["notional_nav"] for f in F]); side = np.array([f["side"] for f in F])
    sleeve = np.array([str(f.get("theme")) for f in F]); year = np.array([f["session_date"][:4] for f in F])
    line = np.array([f["yf"] for f in F])
    conf = confirmatory_mask(F, spec)
    preb = ~conf
    U = placebo["u"]; PRE = placebo["pre"]; POST = placebo["post"]
    nb = int(blocks.max()) + 1
    block_ids = sorted(set(blocks.tolist()))

    def boot_means(vec: np.ndarray, mask: np.ndarray, weights, draws: int) -> np.ndarray:
        """Cluster bootstrap of the (weighted) mean of vec over the fills in mask.
        The PCC engine's resampling draw is kept call for call (clusters with any
        masked member, drawn with replacement, one rng.choice per draw); amendment
        (f) evaluates each draw from per-cluster sums instead of concatenating
        member indices, which is the same mean up to summation order."""
        wv = (np.ones(len(vec)) if weights is None else weights) * mask
        num = np.bincount(blocks, weights=vec * wv, minlength=nb)
        den = np.bincount(blocks, weights=wv, minlength=nb)
        has = np.bincount(blocks, weights=mask.astype(float), minlength=nb) > 0
        bs = [b for b in block_ids if has[b]]
        out = np.empty(draws)
        for d_ in range(draws):
            pick = rng.choice(bs, size=len(bs), replace=True)
            out[d_] = num[pick].sum() / den[pick].sum()
        return out

    def cell(actual_vec, null_mat=None, mask=None, weights=None, label="", scale=1.0, null_means=None):
        mask = np.ones(len(actual_vec), dtype=bool) if mask is None else mask
        if weights is None:
            a = float(actual_vec[mask].mean())
        else:
            wv = weights * mask
            a = float((actual_vec * wv).sum() / wv.sum())
        nm = null_means if null_means is not None else masked_means(null_mat, mask, weights)
        boots = boot_means(actual_vec, mask, weights, spec["bootstrap_draws"])
        a, nm, boots = a * scale, nm * scale, boots * scale
        return {
            "label": label, "n": int(mask.sum()), "actual": round(a, 4),
            "null_mean": round(float(nm.mean()), 4), "null_sd": round(float(nm.std(ddof=1)), 4),
            "effect": round(a - float(nm.mean()), 4),
            "effect_ci95_block_bootstrap": [round(float(np.percentile(boots, 2.5) - nm.mean()), 4), round(float(np.percentile(boots, 97.5) - nm.mean()), 4)],
            "p_one_sided_worse": round(pvalue_ge(nm, a), 4),
            "p_one_sided_better": round(pvalue_ge(-nm, -a), 4),
            "null_p95": round(float(np.percentile(nm, 95)), 4),
        }

    def legs(mask, tag, weights=None, pl=None):
        pl = placebo if pl is None else pl
        return {"u": cell(au, pl["u"], mask=mask, weights=weights, label=f"{tag}: mean u"),
                "pre_pct": cell(apre, pl["pre"], mask=mask, weights=weights, scale=100, label=f"{tag}: pre leg, adverse-oriented, per cent"),
                "post_pct": cell(apost, pl["post"], mask=mask, weights=weights, scale=100, label=f"{tag}: post leg, adverse-oriented, per cent")}

    H_D2 = cell(apost, POST, mask=conf, scale=100, label="H-D2 post leg (fill to t+3 close), adverse-oriented, per cent, equal-weighted, confirmatory set (verdict-bearing)")
    H_D1 = cell(au, U, mask=conf, label="H-D1 mean u, equal-weighted, confirmatory set (descriptive headline)")
    PRE_LEG = cell(apre, PRE, mask=conf, scale=100, label="pre leg (t-3 close to fill), adverse-oriented, per cent, confirmatory set")
    buys = side > 0
    by_side = {"buys": legs(buys & conf, "confirmatory buys"), "sells": legs(~buys & conf, "confirmatory sells")}
    by_sleeve = {sv: legs((sleeve == sv) & conf, f"confirmatory, sleeve {sv}") for sv in sorted(set(sleeve[conf].tolist()))}
    by_year = {y: legs((year == y) & conf, f"confirmatory, year {y}") for y in sorted(set(year[conf].tolist()))}
    notional_weighted = legs(conf, "confirmatory, notional-weighted (|dw| x NAV)", weights=w)
    ind, leg = has_kind(F, "overlay_induced"), has_kind(F, "overlay_leg")
    by_kind = {"overlay_legs_EEM_SHY": legs(leg & conf, "confirmatory, overlay legs (EEM, SHY)") if (leg & conf).any() else None,
               "overlay_induced": legs(ind & conf, "confirmatory, overlay-induced rescaling fills") if (ind & conf).any() else None,
               "sleeve_rebalance_only": legs(~ind & ~leg & conf, "confirmatory, sleeve rebalance fills without an overlay tag")}
    uv = placebo["uniform_variant"]
    uniform_variant = {}
    for g, mask in (("all", conf), ("B", buys & conf), ("S", ~buys & conf)):
        if g not in uv["u"]:
            continue
        uniform_variant[g] = {
            "u": cell(au, mask=mask, null_means=uv["u"][g], label=f"uniform-intraday placebo ({g}, confirmatory): mean u"),
            "pre_pct": cell(apre, mask=mask, null_means=uv["pre"][g], scale=100, label=f"uniform-intraday placebo ({g}, confirmatory): pre leg, per cent"),
            "post_pct": cell(apost, mask=mask, null_means=uv["post"][g], scale=100, label=f"uniform-intraday placebo ({g}, confirmatory): post leg, per cent"),
        }
    pre_blend = legs(preb, "pre-blend fills (before the blend's inception; disclosure)") if preb.any() else None
    disclosure_nulls = {
        "independent_per_fill": {"H_D2_post_pct": cell(apost, placebo_indep["post"], mask=conf, scale=100, label="H-D2 against the independent-per-fill null (disclosure)"),
                                 "H_D1_u": cell(au, placebo_indep["u"], mask=conf, label="H-D1 against the independent-per-fill null (disclosure)")},
        "chained_same_line": {"H_D2_post_pct": cell(apost, placebo_chained["post"], mask=conf, scale=100, label="H-D2 against the chained same-line null (disclosure)"),
                              "H_D1_u": cell(au, placebo_chained["u"], mask=conf, label="H-D1 against the chained same-line null (disclosure)")},
    }

    # thinness on the confirmatory set: the effect with each line, and each calendar year, dropped in turn
    post_null_by_fill = POST.mean(axis=1); u_null_by_fill = U.mean(axis=1)

    def leave_one_out(actual_vec, null_by_fill, groups, scale):
        effects = []
        for g in sorted(set(groups.tolist())):
            keep = groups != g
            if not keep.any():   # a single group: nothing is left to recompute on
                effects.append({"dropped": g, "n": 0, "effect": None})
                continue
            effects.append({"dropped": g, "n": int(keep.sum()),
                            "effect": round(float(actual_vec[keep].mean() - null_by_fill[keep].mean()) * scale, 4)})
        vals = [e["effect"] for e in effects if e["effect"] is not None]
        if not vals:
            return {"min_effect": None, "max_effect": None, "sign_flips": False, "detail": effects}
        return {"min_effect": round(min(vals), 4), "max_effect": round(max(vals), 4),
                "sign_flips": bool(min(vals) < 0 < max(vals)), "detail": effects}

    thinness = {
        "H_D2": {"effect_full": H_D2["effect"], "line": leave_one_out(apost[conf], post_null_by_fill[conf], line[conf], 100),
                 "year": leave_one_out(apost[conf], post_null_by_fill[conf], year[conf], 100)},
        "H_D1": {"effect_full": H_D1["effect"], "line": leave_one_out(au[conf], u_null_by_fill[conf], line[conf], 1),
                 "year": leave_one_out(au[conf], u_null_by_fill[conf], year[conf], 1)},
    }

    # verdict on H-D2 alone, as the PREREG maps it
    power_target = spec["power_target"]
    delta2 = spec["floors"]["H_D2_delta_price"]
    if cov["power"]["H_D2"]["delta"] != delta2:
        stop("the H-D2 floor differs from the frozen coverage record")
    delta2_pct = round(delta2 * 100, 10)
    powered = cov["power"]["H_D2"]["power_at_delta"] >= power_target

    def status(cellv, delta, powered_):
        passed = cellv["p_one_sided_worse"] <= alpha
        if passed and cellv["effect"] >= delta:
            return "PASS" if powered_ else "SUGGESTIVE"
        if passed and cellv["effect"] < delta:
            return "DETECTED-BELOW-FLOOR" if powered_ else "SUGGESTIVE-BELOW-FLOOR"
        return "FAIL" if powered_ else "UNRESOLVED"

    st = status(H_D2, delta2_pct, powered)
    adapter = (ctx["book"].get("_provenance") or {})
    parity_share = (adapter.get("parity") or {}).get("excluded_share")
    reconciled = (adapter.get("reproduction") or {}).get("reconciled")
    thin_fires = thinness["H_D2"]["line"]["sign_flips"] or thinness["H_D2"]["year"]["sign_flips"]
    thin_scope = spec["thinness"]["scope"]
    passing = st in ("PASS", "DETECTED-BELOW-FLOOR", "SUGGESTIVE", "SUGGESTIVE-BELOW-FLOOR")
    thin_applies = thin_fires and (thin_scope == "any_verdict" or passing)
    # Verdict mapping, fixed at the freeze (PREREG, decision criteria, as amended 2026-10-04):
    #   GIVE-BACK-AT-SIZE     H-D2 p <= alpha and effect at or above the 10 bp floor, powered
    #   GIVE-BACK-BELOW-SIZE  H-D2 p <= alpha, effect below the floor
    #   NO-GIVE-BACK          H-D2 p > alpha, powered
    #   SUGGESTIVE / UNRESOLVED  a demoted pass / fail (power at the floor below the target)
    #   INCONCLUSIVE          on a passing H-D2, thinness fires (amendment 4: thinness guards a pass
    #                         only, and a failing H-D2 carries no thinness suffix); or the adapter's
    #                         parity guard excluded more than a tenth of fills
    #   INFEASIBLE            the weight history could not be reconciled to the factsheet's tables
    if reconciled is False:
        verdict = "INFEASIBLE"
    elif parity_share is not None and parity_share > spec["parity_inconclusive_share"]:
        verdict = "INCONCLUSIVE"
    elif thin_applies:
        verdict = "INCONCLUSIVE"
    elif st == "PASS":
        verdict = "GIVE-BACK-AT-SIZE"
    elif st == "DETECTED-BELOW-FLOOR":
        verdict = "GIVE-BACK-BELOW-SIZE"
    elif st == "FAIL":
        verdict = "NO-GIVE-BACK"
    elif st.startswith("SUGGESTIVE"):
        verdict = "SUGGESTIVE"
    else:
        verdict = "UNRESOLVED"
    return {
        "registered": spec["registered"], "run_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "provenance": ctx["provenance"], "n": n, "n_confirmatory": int(conf.sum()), "n_pre_blend": int(preb.sum()),
        "counts": cov["counts"],
        "verdict": verdict,
        "verdict_inputs": {"H_D2_status": st, "powered": powered, "floor_delta_price": delta2, "floor_delta_pct": delta2_pct,
                           "thinness_fires": thin_fires, "thinness_scope": thin_scope, "thinness_applied": thin_applies,
                           "parity_excluded_share": parity_share, "weight_history_reconciled": reconciled},
        "alpha_one_sided": alpha,
        "better_than_placebo_H_D2_at_alpha": bool(H_D2["p_one_sided_better"] <= alpha),
        "H_D2": H_D2, "H_D1": H_D1, "pre_leg": PRE_LEG,
        "by_side": by_side, "by_sleeve": by_sleeve, "by_year": by_year,
        "notional_weighted": notional_weighted,
        "sleeve_D_eur": {"note": "sleeve D's confirmatory fills, priced on the Xetra lines in EUR; the same cells as by_sleeve.D", **by_sleeve.get("D", {})},
        "by_kind": by_kind,
        "uniform_intraday_variant": uniform_variant,
        "pre_blend_disclosure": pre_blend,
        "disclosure_nulls": disclosure_nulls,
        "thinness": thinness,
        "distribution": {"actual_u_hist_edges": [round(x, 2) for x in np.linspace(0, 1, 11)],
                         "actual_u_hist_confirmatory": np.histogram(au[conf], bins=np.linspace(0, 1, 11))[0].tolist(),
                         "null_mean_u_draws_confirmatory": np.round(masked_means(U, conf), 5).tolist(),
                         "null_mean_post_pct_draws_confirmatory": np.round(masked_means(POST, conf) * 100, 5).tolist(),
                         "null_mean_pre_pct_draws_confirmatory": np.round(masked_means(PRE, conf) * 100, 5).tolist()},
        "fills": [{**{key: f.get(key) for key in ("ticker", "yf", "theme", "kinds", "name", "session_date", "date", "side_label", "qty", "price", "ccy",
                                                   "notional_nav", "alignment", "ex_in_window", "u", "pre", "post", "H", "L")},
                   "confirmatory": bool(c_)} for f, c_ in zip(F, conf)],
        "blocks": blocks.tolist(),
        "trial_register": {"declared_cells": spec["declared_cells"], "verdict_bearing": ["H-D2"],
                           "configurations_evaluated": 1, "undeclared_cells_run": 0,
                           "disabled_pcc_cells": spec["disabled_pcc_cells"]},
    }


def disclosure_placebos(ctx: dict, spec: dict) -> tuple[dict, dict]:
    """The two disclosure nulls (amendment 2): independent per fill, and the PCC
    engine's chained same-line relation; neither is verdict-bearing."""
    k = spec["window_sessions_each_side"]
    dn = spec["placebo"]["disclosure_nulls"]
    indep = simulate_placebo(ctx, np.random.default_rng(spec["seed"] + dn["independent_per_fill"]["seed_offset"]),
                             dn["independent_per_fill"]["draws"], blocked=False)
    chained_blocks = make_blocks(ctx["complete"], k, tuple(dn["chained_same_line"]["relations"]))
    chained = simulate_placebo(ctx, np.random.default_rng(spec["seed"] + dn["chained_same_line"]["seed_offset"]),
                               dn["chained_same_line"]["draws"], blocked=True, blocks=chained_blocks)
    chained["n_blocks"] = int(chained_blocks.max()) + 1
    return indep, chained


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["coverage", "run"])
    ap.add_argument("--history", default=str(RESULTS_DIR / "bars_used.json"))
    ap.add_argument("--fills", default=str(RESULTS_DIR / "fills.json"))
    ap.add_argument("--book", default=str(RESULTS_DIR / "book_meta.json"))
    args = ap.parse_args()
    spec = load_json(SPEC_PATH)
    RESULTS_DIR.mkdir(exist_ok=True)
    ctx = build(spec, args)
    rng = np.random.default_rng(spec["seed"])
    placebo = simulate_placebo(ctx, rng, spec["placebo"]["draws_per_fill"], blocked=True,
                               uniform_variant=(args.mode == "run"))
    indep, chained = disclosure_placebos(ctx, spec)
    if args.mode == "coverage":
        cov = coverage(ctx, placebo, indep, chained)
        with open(RESULTS_DIR / "coverage.json", "w", encoding="utf-8") as fh:
            json.dump(cov, fh, indent=1, ensure_ascii=False)
        print(json.dumps({k: cov[k] for k in ("counts", "window_width", "null", "power")}, indent=1, ensure_ascii=False))
        print("dropped (window):", len(cov["dropped_window"]), "fills")
        print("outcome-blind: no actual statistic computed; coverage.json written; engine sha256", cov["provenance"]["engine_sha256"])
        return
    frozen = load_json(RESULTS_DIR / "coverage.json")
    res = run(ctx, placebo, frozen, args, indep, chained)
    with open(RESULTS_DIR / "results.json", "w", encoding="utf-8") as fh:
        json.dump(res, fh, indent=1, ensure_ascii=False, default=float)
    summary = {k: res[k] for k in ("verdict", "verdict_inputs", "n", "n_confirmatory", "H_D2", "H_D1", "pre_leg") if k in res}
    summary["thinness"] = {c: {g: {k_: v for k_, v in res["thinness"][c][g].items() if k_ != "detail"} for g in ("line", "year")} for c in ("H_D2", "H_D1")}
    print(json.dumps(summary, indent=1, ensure_ascii=False, default=float))


if __name__ == "__main__":
    main()
