"""WS7 — Sleeve C seat watch: append-only OOS evidence accumulator.

Registered by KICKOFF_ws7-c-seat.md (2026-07-18). Measurement only — no
strategy behaviour changes here. Each weekly run appends one row per newly
completed week to ``data/c_seat_watch.json``:

  - the ROTATION leg: Sleeve C's own published headline equity (net of the
    deployed cost model);
  - the EW-25 leg: the same 25 risk names equal-weighted, re-equalised each
    week, USD-priced (159801.SZ via USDCNY), charged the WS3 per-line
    one-way spread vector at 1x on the re-equalisation turnover — the same
    benchmark definition WS3's break-even used;
  - the SEAT legs: the deployed blend's weekly return and the registered
    without-C counterfactual r_woC = (r_blend - w_C * r_C) / (1 - w_C),
    with w_C = 0.10 x equity_scaler from the overlay event log.

Rows are NEVER recomputed once written (append-only): the accumulated
series is itself point-in-time, so the review cannot be contaminated by
retroactive data revisions. Failures must be LOUD but must not block the
weekly publish — the workflow runs this soft-fail, and the email watch
line renders a STALE tag whenever the last row trails the latest Sleeve C
rebalance (guard-by-visibility, per the no-silent-failure house rule).

Python datetime months are 1-indexed. All date arithmetic via pandas.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from overlay_state import sleeve_nav_weights  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUT_PATH = DATA_DIR / "c_seat_watch.json"

# Registered constants (KICKOFF_ws7-c-seat.md sections 4-6). Do not tune.
ANCHOR_DATE = "2026-07-03"          # WS3 filing date — OOS window start
TRIPWIRE_PP = -5.0                  # rotation vs EW cumulative, one-sided
NOISE_BAND_PP = 2.0                 # +/- band inside which OOS is noise
CASH_FLOOR_TICKER = "SHY"           # excluded from the EW-25 risk basket
CNY_TICKER = "159801.SZ"            # USD-converted via USDCNY


def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def ew_week_return(p0: dict[str, float], p1: dict[str, float],
                   bps_one_way: dict[str, float]) -> tuple[float, int]:
    """Net weekly return of the equal-weight basket, re-equalised at t1.

    ``p0``/``p1`` map ticker -> USD close at the two consecutive rebalance
    Fridays; names missing either print are dropped from that week's mean
    (count returned so the row can disclose partial coverage). The cost
    charge is the WS3 convention: each week the basket re-equalises, so
    turnover per name is |equal - drifted| and the one-way spread applies
    to that turnover.
    """
    names = [t for t in p0 if t in p1
             and p0[t] is not None and p1[t] is not None and p0[t] > 0]
    if not names:
        return 0.0, 0
    rets = {t: p1[t] / p0[t] - 1.0 for t in names}
    n = len(names)
    w = 1.0 / n
    gross = sum(rets.values()) / n
    cost = 0.0
    for t in names:
        drifted = w * (1.0 + rets[t]) / (1.0 + gross)
        turnover = abs(w - drifted)
        cost += turnover * (bps_one_way.get(t, 12.0) / 10_000.0)
    return gross - cost, n


def without_c_return(r_blend: float, r_c: float, w_c: float) -> float:
    """Registered pro-rata counterfactual: the blend with C's slice removed
    and the remainder renormalised. Pure algebra on published returns."""
    if w_c >= 1.0:
        raise ValueError("w_c must be < 1")
    return (r_blend - w_c * r_c) / (1.0 - w_c)


def append_rows(existing: list[dict], new_rows: list[dict]) -> list[dict]:
    """Append-only merge: a week already recorded is NEVER replaced, even
    when recomputation would give a different number — that is the
    point-in-time property the review depends on."""
    have = {r["week_end"] for r in existing}
    out = list(existing)
    for r in new_rows:
        if r["week_end"] not in have:
            out.append(r)
    return sorted(out, key=lambda r: r["week_end"])


def _weekly_closes(series: pd.Series, fridays: list[pd.Timestamp]) -> pd.Series:
    """Last observation at/before each rebalance Friday."""
    out = {}
    s = series.dropna()
    for f in fridays:
        sub = s[s.index <= f]
        if len(sub):
            out[f] = float(sub.iloc[-1])
    return pd.Series(out)


def _panel_series(hp: dict, ticker: str) -> pd.Series | None:
    rec = hp.get(ticker)
    if not rec or not rec.get("dates"):
        return None
    return pd.Series(rec["prices"], index=pd.to_datetime(rec["dates"]),
                     dtype="float64")


def _fetch_usdcny(start: pd.Timestamp, end: pd.Timestamp) -> pd.Series | None:
    """USDCNY closes for the window; None on any failure (the caller
    degrades loudly per row rather than dying)."""
    try:
        import yfinance as yf
        raw = yf.download("USDCNY=X",
                          start=(start - pd.Timedelta(days=7)).strftime("%Y-%m-%d"),
                          end=(end + pd.Timedelta(days=2)).strftime("%Y-%m-%d"),
                          auto_adjust=True, progress=False, threads=False)
        if raw is None or raw.empty:
            return None
        close = raw["Close"]
        if isinstance(close, pd.DataFrame):
            close = close.iloc[:, 0]
        close.index = pd.to_datetime(close.index).tz_localize(None)
        return close.dropna()
    except Exception as exc:
        print(f"  WARN: USDCNY fetch failed ({exc}) — {CNY_TICKER} will use "
              f"a native-currency return this week (flagged per row)")
        return None


def main() -> int:
    print("=== WS7 Sleeve C seat watch ===")
    thematic = _load_json(DATA_DIR / "thematic_rotation.json")
    overlay = _load_json(DATA_DIR / "risk_overlay.json")
    hp = _load_json(DATA_DIR / "holdings_prices_1y.json")["prices"]
    cost_bps = _load_json(DATA_DIR / "ws3_cost_stress.json")[
        "per_line_vectors_bps"]["C"]

    universe = [u["etf"] for u in thematic.get("universe", [])
                if u.get("etf") and u["etf"] != CASH_FLOOR_TICKER]
    hl = thematic["headline"]
    c_eq = pd.Series(hl["headline_equity"],
                     index=pd.to_datetime(hl["headline_equity_dates"]))
    key = overlay["underlying_blend_key"] + "_gated_eem_tilted"
    bl = overlay["gated_variants"][key]
    blend_eq = pd.Series(bl["equity"], index=pd.to_datetime(bl["dates"]))

    existing = []
    if OUT_PATH.exists():
        existing = _load_json(OUT_PATH).get("weeks", [])
    have = {r["week_end"] for r in existing}

    # Weekly grid: W-FRI labels over the DAILY engine series (each label
    # valued at the last session at/before its Friday, so a US-holiday
    # Friday week reads its Thursday close — the engines' own cadence).
    # The anchor 2026-07-03 is itself a W-FRI label (a holiday Friday,
    # valued at the 07-02 close) — consistent across every leg.
    anchor = pd.Timestamp(ANCHOR_DATE)
    c_w = c_eq.resample("W-FRI").last().dropna()
    b_w = blend_eq.resample("W-FRI").last().dropna()
    common_w = c_w.index.intersection(b_w.index)
    if anchor not in common_w:
        print(f"  ERROR: anchor week {ANCHOR_DATE} not covered by the "
              f"engine series — cannot anchor the OOS window",
              file=sys.stderr)
        return 1
    fridays = [d for d in common_w if d > anchor]
    grid = [anchor] + fridays
    todo = [f for f in fridays if f.strftime("%Y-%m-%d") not in have]
    if not todo:
        print(f"  nothing to append (last recorded week: "
              f"{max(have) if have else 'none'})")
        return 0

    fx = (_fetch_usdcny(grid[0], grid[-1])
          if any(t == CNY_TICKER for t in universe) else None)

    new_rows = []
    for f in todo:
        prev = grid[grid.index(f) - 1]
        p0: dict[str, float] = {}
        p1: dict[str, float] = {}
        fx_fallback = False
        for t in universe:
            s = _panel_series(hp, t)
            if s is None:
                continue
            wk = _weekly_closes(s, [prev, f])
            if prev not in wk.index or f not in wk.index:
                continue
            v0, v1 = wk[prev], wk[f]
            if t == CNY_TICKER:
                if fx is not None:
                    fx0 = _weekly_closes(fx, [prev, f])
                    if prev in fx0.index and f in fx0.index:
                        v0, v1 = v0 / fx0[prev], v1 / fx0[f]
                    else:
                        fx_fallback = True
                else:
                    fx_fallback = True
            p0[t], p1[t] = v0, v1
        r_ew, n_ew = ew_week_return(p0, p1, cost_bps)

        r_c = float(c_w[f] / c_w[prev] - 1.0)
        r_blend = float(b_w[f] / b_w[prev] - 1.0)
        st = sleeve_nav_weights(overlay, f.strftime("%Y-%m-%d"))
        w_c = 0.10 * st["equity_scaler"]
        r_wo = without_c_return(r_blend, r_c, w_c)

        new_rows.append({
            "week_end": f.strftime("%Y-%m-%d"),
            "r_rotation": round(r_c, 6),
            "r_ew25": round(r_ew, 6),
            "r_blend": round(r_blend, 6),
            "r_without_c": round(r_wo, 6),
            "w_c": round(w_c, 4),
            "n_ew_names": n_ew,
            "fx_fallback": fx_fallback,
            "computed_at_utc": datetime.now(timezone.utc)
                .strftime("%Y-%m-%d %H:%M UTC"),
        })
        if n_ew < len(universe):
            print(f"  WARN: {f.date()} EW basket covered {n_ew}/"
                  f"{len(universe)} names — panel gaps are disclosed in "
                  f"the row")

    weeks = append_rows(existing, new_rows)
    # Cumulative gaps in percentage points, compounded per leg.
    cum_rot = cum_ew = cum_with = cum_wo = 1.0
    for r in weeks:
        cum_rot *= 1.0 + r["r_rotation"]
        cum_ew *= 1.0 + r["r_ew25"]
        cum_with *= 1.0 + r["r_blend"]
        cum_wo *= 1.0 + r["r_without_c"]
        r["cum_rotation_minus_ew_pp"] = round((cum_rot - cum_ew) * 100, 3)
        r["cum_with_minus_without_pp"] = round((cum_with - cum_wo) * 100, 3)
        r["tripwire"] = bool(r["cum_rotation_minus_ew_pp"] <= TRIPWIRE_PP)

    payload = {
        "registered_by": "KICKOFF_ws7-c-seat.md",
        "anchor_date": ANCHOR_DATE,
        "review_date": "2026-10-02",
        "tripwire_pp": TRIPWIRE_PP,
        "noise_band_pp": NOISE_BAND_PP,
        "universe_n": len(universe),
        "weeks": weeks,
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    last = weeks[-1]
    print(f"  appended {len(new_rows)} week(s); latest {last['week_end']}: "
          f"rotation vs EW {last['cum_rotation_minus_ew_pp']:+.2f}pp, "
          f"seat {last['cum_with_minus_without_pp']:+.2f}pp, "
          f"tripwire={'BREACHED' if last['tripwire'] else 'clear'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
