"""WS15 step 2 — breadth from an arbitrary cache, validated by reproduction.

Replicates compute_breadth's post-download pipeline against a caller-supplied
price cache, so breadth can be computed on the WS15 residual-filled COPY
without touching the live cache or the committed panel. Two guards make the
replication trustworthy rather than hopeful:

1. REPRODUCE-BEFORE-DIFF. Run first against the live cache with no barriers:
   the output must equal the committed data/breadth_cndx.json EXACTLY —
   every series value at the JSON's own 6dp rounding, every signal. Any
   transcription drift between this driver and compute_breadth.main fails
   loudly there, before a single comparison is drawn on the WS15 leg.
2. ERA BARRIERS. Columns listed in --barriers hold two different securities'
   bars (a reuse-masked fill before the barrier date, the later occupant
   after). Indicators are computed per era, never across the boundary —
   which for FOXA/FOX exactly preserves the committed panel's fresh-listing
   warmup treatment of early Fox Corporation, and for FB/PCLN makes the
   never-roster-read reuse eras structurally unreadable.

Run:
    python scripts/run_ws15_breadth_legs.py --cache data/prices_cache_cndx.parquet \
        --out <workdir>/breadth_repro.json --verify-against data/breadth_cndx.json
    python scripts/run_ws15_breadth_legs.py --cache <workdir>/prices_cache_cndx_ws15.parquet \
        --out <workdir>/breadth_ws15.json --barriers <workdir>/era_barriers.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pandas_market_calendars as mcal

sys.path.insert(0, str(Path(__file__).resolve().parent))
from compute_breadth import (  # noqa: E402
    COMPOSITE_HIGH_PCT, COMPOSITE_LOW_PCT, HIGH_PERIOD, MA_PERIOD,
    MIN_BREADTH_NAMES, PRICE_WARMUP_CALENDAR_DAYS, RSI_OVERBOUGHT, RSI_PERIOD,
    SIGNAL_ELIGIBLE_AFTER, _safe_float, active_roster_at, compute_rsi,
    coverage_verdict, expanding_percentile, expanding_zscore, zweig_trigger,
)
from etf_registry import get_etf  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def barrier_apply(prices: pd.DataFrame, fn, barriers: dict[str, str]) -> pd.DataFrame:
    """per_ticker_apply, with indicator windows cut at each era barrier.

    Non-barrier columns follow compute_breadth.per_ticker_apply exactly
    (same dropna + reindex), so with barriers={} this is bit-identical to
    the committed pipeline — which the reproduction guard asserts.
    """
    out = {}
    for c in prices.columns:
        s = prices[c].dropna()
        if s.empty:
            out[c] = pd.Series(np.nan, index=prices.index)
        elif c in barriers:
            cut = pd.Timestamp(barriers[c])
            parts = [p for p in (s.loc[: cut - pd.Timedelta(days=1)],
                                 s.loc[cut:]) if not p.empty]
            out[c] = pd.concat([fn(p) for p in parts]).reindex(prices.index)
        else:
            out[c] = fn(s).reindex(prices.index)
    return pd.DataFrame(out, index=prices.index)[list(prices.columns)]


def compute(cache_path: Path, barriers: dict[str, str]) -> dict:
    consts = json.loads(
        (DATA / "constituents_cndx.json").read_text(encoding="utf-8"))
    snapshot_dates = sorted(consts["snapshots"].keys())
    snapshot_map = consts["snapshots"]
    universe = sorted({t for snap in snapshot_map.values()
                       for t in snap["tickers"]})

    start_friday = pd.Timestamp(consts["start_friday"])
    end_friday = pd.Timestamp(consts["end_friday"])
    dl_start = start_friday - pd.Timedelta(days=PRICE_WARMUP_CALENDAR_DAYS)
    dl_end = end_friday + pd.Timedelta(days=5)

    cached = pd.read_parquet(cache_path)
    for t in universe:
        if t not in cached.columns:
            cached[t] = np.nan
    prices = cached[list(universe)].loc[dl_start:dl_end]

    rsi = barrier_apply(
        prices, lambda s: compute_rsi(s.to_frame("_c"), RSI_PERIOD)["_c"],
        barriers)
    ma50 = barrier_apply(
        prices, lambda s: s.rolling(MA_PERIOD, min_periods=MA_PERIOD).mean(),
        barriers)
    rolling_high = barrier_apply(
        prices, lambda s: s.rolling(HIGH_PERIOD, min_periods=HIGH_PERIOD).max(),
        barriers)
    above_ma = (prices > ma50) & ma50.notna()
    at_high = (prices >= rolling_high) & rolling_high.notna()
    rsi_overbought = (rsi > RSI_OVERBOUGHT) & rsi.notna()

    cal_name = get_etf(consts["etf"]).get("trading_calendar", "NYSE")
    cal = mcal.get_calendar(cal_name)
    schedule = cal.schedule(start_date=start_friday, end_date=end_friday)
    trading_days = pd.DatetimeIndex(schedule.index.normalize().tz_localize(None))

    rows = []
    for d in trading_days:
        d_str = d.strftime("%Y-%m-%d")
        roster = active_roster_at(snapshot_dates, snapshot_map, d_str)
        if not roster:
            continue
        if d not in prices.index:
            continue
        roster_in_panel = [t for t in roster if t in prices.columns]
        row_price = prices.loc[d, roster_in_panel]
        has_price = row_price.notna()
        n_with_price = int(has_price.sum())

        rsi_at = rsi.loc[d, roster_in_panel]
        rsi_valid = rsi_at.notna() & has_price
        ma_at = ma50.loc[d, roster_in_panel]
        ma_valid = ma_at.notna() & has_price
        high_at = rolling_high.loc[d, roster_in_panel]
        high_valid = high_at.notna() & has_price

        if rsi_valid.sum() >= MIN_BREADTH_NAMES:
            rsi_b = float(rsi_overbought.loc[d, roster_in_panel][rsi_valid].sum()
                          / rsi_valid.sum())
        else:
            rsi_b = np.nan
        if ma_valid.sum() >= MIN_BREADTH_NAMES:
            ma_b = float(above_ma.loc[d, roster_in_panel][ma_valid].sum()
                         / ma_valid.sum())
        else:
            ma_b = np.nan
        if high_valid.sum() >= MIN_BREADTH_NAMES:
            high_b = float(at_high.loc[d, roster_in_panel][high_valid].sum()
                           / high_valid.sum())
        else:
            high_b = np.nan

        rows.append({
            "date": d_str,
            "n_constituents": len(roster),
            "n_with_price": n_with_price,
            "n_with_rsi": int(rsi_valid.sum()),
            "n_with_ma50": int(ma_valid.sum()),
            "n_with_high63": int(high_valid.sum()),
            "rsi_breadth": rsi_b,
            "ma_breadth": ma_b,
            "highs_breadth": high_b,
        })

    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()

    df["rsi_breadth_z"] = expanding_zscore(df["rsi_breadth"])
    df["ma_breadth_z"] = expanding_zscore(df["ma_breadth"])
    df["highs_breadth_z"] = expanding_zscore(df["highs_breadth"])
    df["composite_z"] = df[["rsi_breadth_z", "ma_breadth_z",
                            "highs_breadth_z"]].mean(axis=1, skipna=False)

    df["rsi_p90"] = expanding_percentile(df["rsi_breadth"], COMPOSITE_HIGH_PCT)
    df["rsi_trigger"] = (df["rsi_breadth"] >= df["rsi_p90"]) & df["rsi_p90"].notna()
    df["highs_p90"] = expanding_percentile(df["highs_breadth"], COMPOSITE_HIGH_PCT)
    df["highs_trigger"] = ((df["highs_breadth"] >= df["highs_p90"])
                           & df["highs_p90"].notna())
    df["ma_zweig_trigger"] = zweig_trigger(df["ma_breadth"])

    df["composite_p90"] = expanding_percentile(df["composite_z"], COMPOSITE_HIGH_PCT)
    df["composite_p10"] = expanding_percentile(df["composite_z"], COMPOSITE_LOW_PCT)
    above_p90 = (df["composite_z"] >= df["composite_p90"]) & df["composite_p90"].notna()
    df["composite_above_p90"] = above_p90
    prev_above_p90 = above_p90.shift(1, fill_value=False).astype(bool)
    df["composite_crosses_p90"] = above_p90 & ~prev_above_p90

    df["trigger_count"] = (df["rsi_trigger"].astype(int)
                           + df["highs_trigger"].astype(int)
                           + df["ma_zweig_trigger"].astype(int))
    history_position = np.arange(len(df))
    df["signal_eligible"] = history_position >= SIGNAL_ELIGIBLE_AFTER
    df["signal_fires"] = (df["composite_crosses_p90"]
                          & (df["trigger_count"] >= 2) & df["signal_eligible"])

    signal_rows = df[df["signal_fires"]]
    signals_list = []
    for ts, r in signal_rows.iterrows():
        triggered = []
        if bool(r["rsi_trigger"]):
            triggered.append("rsi")
        if bool(r["ma_zweig_trigger"]):
            triggered.append("ma_zweig")
        if bool(r["highs_trigger"]):
            triggered.append("highs")
        signals_list.append({
            "date": ts.strftime("%Y-%m-%d"),
            "composite_z": _safe_float(r["composite_z"]),
            "composite_p90": _safe_float(r["composite_p90"]),
            "rsi_breadth": _safe_float(r["rsi_breadth"]),
            "ma_breadth": _safe_float(r["ma_breadth"]),
            "highs_breadth": _safe_float(r["highs_breadth"]),
            "triggered_components": triggered,
            "n_constituents": int(r["n_constituents"]),
            "n_with_price": int(r["n_with_price"]),
        })

    import math

    def col_to_jsonlist(s: pd.Series, ndigits: int | None = 6) -> list:
        out = []
        for v in s.tolist():
            if isinstance(v, (bool, np.bool_)):
                out.append(int(v))
            elif v is None or (isinstance(v, float)
                               and (math.isnan(v) or math.isinf(v))):
                out.append(None)
            elif isinstance(v, float) and ndigits is not None:
                out.append(round(v, ndigits))
            else:
                out.append(v)
        return out

    payload = {
        "etf": consts["etf"],
        "computed_at_utc": datetime.now(timezone.utc).isoformat(),
        "cache": str(cache_path),
        "barriers": barriers,
        "start_date": df.index[0].strftime("%Y-%m-%d"),
        "end_date": df.index[-1].strftime("%Y-%m-%d"),
        "n_trading_days": int(len(df)),
        "n_signals": int(df["signal_fires"].sum()),
        "first_eligible_signal_date": (
            df.index[SIGNAL_ELIGIBLE_AFTER].strftime("%Y-%m-%d")
            if len(df) > SIGNAL_ELIGIBLE_AFTER else None),
        "signals": signals_list,
        "series": {
            "dates": [d.strftime("%Y-%m-%d") for d in df.index],
            "n_constituents": df["n_constituents"].astype(int).tolist(),
            "n_with_price": df["n_with_price"].astype(int).tolist(),
            "n_with_rsi": df["n_with_rsi"].astype(int).tolist(),
            "n_with_ma50": df["n_with_ma50"].astype(int).tolist(),
            "n_with_high63": df["n_with_high63"].astype(int).tolist(),
            "rsi_breadth": col_to_jsonlist(df["rsi_breadth"]),
            "ma_breadth": col_to_jsonlist(df["ma_breadth"]),
            "highs_breadth": col_to_jsonlist(df["highs_breadth"]),
            "rsi_breadth_z": col_to_jsonlist(df["rsi_breadth_z"]),
            "ma_breadth_z": col_to_jsonlist(df["ma_breadth_z"]),
            "highs_breadth_z": col_to_jsonlist(df["highs_breadth_z"]),
            "composite_z": col_to_jsonlist(df["composite_z"]),
            "composite_p90": col_to_jsonlist(df["composite_p90"]),
            "composite_p10": col_to_jsonlist(df["composite_p10"]),
            "rsi_p90": col_to_jsonlist(df["rsi_p90"]),
            "highs_p90": col_to_jsonlist(df["highs_p90"]),
            "rsi_trigger": col_to_jsonlist(df["rsi_trigger"]),
            "ma_zweig_trigger": col_to_jsonlist(df["ma_zweig_trigger"]),
            "highs_trigger": col_to_jsonlist(df["highs_trigger"]),
            "trigger_count": df["trigger_count"].astype(int).tolist(),
            "composite_above_p90": col_to_jsonlist(df["composite_above_p90"]),
            "composite_crosses_p90": col_to_jsonlist(df["composite_crosses_p90"]),
            "signal_eligible": col_to_jsonlist(df["signal_eligible"]),
            "signal_fires": col_to_jsonlist(df["signal_fires"]),
        },
    }
    return payload


def verify(payload: dict, committed_path: Path) -> None:
    ref = json.loads(committed_path.read_text(encoding="utf-8"))
    assert payload["series"]["dates"] == ref["series"]["dates"], "date grid differs"
    for k, v in ref["series"].items():
        assert payload["series"][k] == v, f"series[{k}] differs from committed"
    assert payload["signals"] == ref["signals"], "signals differ from committed"
    assert payload["n_signals"] == ref["n_signals"]
    print(f"REPRODUCTION EXACT: {committed_path.name} "
          f"({len(ref['series']['dates'])} days, {ref['n_signals']} signals, "
          f"every series value and signal identical)")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--barriers", default=None,
                    help="JSON file of {column: first-bar date of later era}")
    ap.add_argument("--verify-against", default=None,
                    help="committed breadth JSON that must be reproduced exactly")
    args = ap.parse_args()

    barriers = (json.loads(Path(args.barriers).read_text(encoding="utf-8"))
                if args.barriers else {})
    payload = compute(Path(args.cache), barriers)
    if args.verify_against:
        verify(payload, Path(args.verify_against))
    Path(args.out).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"{Path(args.cache).name}: {payload['n_trading_days']} days, "
          f"{payload['n_signals']} signals -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
