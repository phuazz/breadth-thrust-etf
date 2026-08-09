"""Phase 19 + 22 — Overlay layer on top of the deployed blend.

This is the dedicated home for "overlays that modulate the deployed blend
based on a market signal".

Phase 19 (Idea 1): aggregate market-breadth regime gate. When CSP1
breadth collapses below the off-threshold, de-risk 50% of the blend
into SHY. Re-engage when breadth recovers above the on-threshold.

Phase 22 (2026-05-28): EEM/SPY relative-strength tilt. When the
EEM/SPY ratio's 50d MA crosses above its 200d MA (golden cross —
EM is in a sustained relative-strength uptrend vs US), tilt 10% of
the blend into EEM. Funded from Strategy B (asset-class momentum
sleeve, 35% baseline → 25% during tilt-ON) because:
  (a) Empirically: from_B gave +0.005 Full Sharpe + 3.7pp 22-on Total
      vs proportional (-0.014 / +1.6pp) or from_A (-0.018 / +0.8pp).
  (b) Mechanistically: B already includes EEM in its rotation
      universe but momentum lags relative-strength by ~4mo. The
      overlay is a fast-EEM supplement to B's slow-EEM momentum.
  (c) B has been the weakest sleeve in 2022-onwards (CAGR ~9% vs
      A 17% / C 21% / D 14%), so shrinking it has the lowest
      opportunity cost.

Both overlays apply independently and compose: the EEM-tilted
ungated blend is gated by the same Phase 19 breadth signal.

Architecture:
  * Reads ``data/multi_strategy.json`` to get the underlying blend's
    daily equity curve.
  * Reads ``data/breadth_csp1.json`` for the S&P 500 constituent-breadth
    time series (CSP1 holdings, computed by scripts/compute_breadth.py).
  * Reads ``data/asset_class_prices_cache.parquet`` to source the IEF
    fallback (7-10y US Treasury — what the live strategy actually
    rotates into when the gate fires).
  * Emits ``data/risk_overlay.json`` with the gated equity curve,
    diagnostics (current state, last switch date, current breadth, etc),
    and full event log.

Output schema:
  {
    "computed_at_utc": "...",
    "underlying_blend_key": "blend_35_35_10_20",
    "gate_parameters": { off_threshold, on_threshold, derisk_fraction,
                          switch_cost_bps, fallback_ticker },
    "current_state": "RISK_ON" | "RISK_OFF",
    "current_state_since": "YYYY-MM-DD",
    "current_breadth": 0.4502,
    "n_switches": 16,
    "days_risk_off": 212,
    "pct_days_risk_off": 11.41,
    "events": [ {date, direction, breadth}, ... ],
    "ungated_reference": { sharpe, cagr, max_dd, total_return },
    "gated_variants": {
      "blend_35_35_10_20_gated": {
        label, dates, equity, sharpe, cagr, max_dd, total_return
      }
    }
  }

The dashboard's pipeline.py merges ``gated_variants`` into
``multi.strategies`` at injection time, and ``gate_parameters`` +
``current_state`` etc into ``multi.regime_gate`` for backward
compatibility with the existing dashboard render code. So the
template.html does not need to change — only the data flow upstream.

Usage:
    # After multi_strategy.json has been built
    python scripts/run_risk_overlay.py

Returns nonzero exit if upstream data is missing.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

# Allow importing sibling scripts/ modules when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from regime_publish import (  # noqa: E402
    assert_state_since_matches_events,
    detect_historical_revision,
)
from alignment import align_series_to_index  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUT_PATH = DATA_DIR / "risk_overlay.json"

# ----------------------------------------------------------------------
# Phase 19 — breadth regime gate parameters
# Chosen via the 12-variant sweep in scripts/run_regime_gate.py
# (Phase 19, variant #4). Pareto-improving on the ungated 4-way blend.
# ----------------------------------------------------------------------
UNDERLYING_BLEND_KEY = "blend_35_35_10_20"
OFF_THRESHOLD = 0.20    # de-risk when S&P 500 breadth falls below 20%
ON_THRESHOLD = 0.50     # re-engage when breadth crosses back above 50%
DERISK_FRACTION = 0.50  # 50% partial de-risk (not full move to cash)
SWITCH_COST_BPS = 5     # bps charged per regime flip
FALLBACK_TICKER = "SHY" # 1-3y Treasury — cleaner cash-equivalent than IEF.
                         # Phase 19.1: switched from IEF to SHY after empirical
                         # test in scripts/compare_fallback_ticker.py.
                         # Rationale: the overlay's mission is defensive against
                         # equity broad-weakness, not to express a duration view.
                         # IEF (~7y duration) sells off in inflation-driven stress
                         # (2022 episode: IEF -8.4% during the inflation crash
                         # window). SHY (~1.8y duration) is duration-neutral and
                         # always defensive. Full backtest: SHY gives Sharpe
                         # +1.29 vs IEF +1.27, Max DD -16.5% vs -16.9%.

# Phase 19.2 (WS3 maintenance patch, defect D3) — staleness cap on the
# gate breadth feed. An uncapped forward-fill would freeze the gate on a
# stalled CSP1 panel by carrying a stale breadth NUMBER forward; capping
# at 10 calendar days (mirroring the Sleeve C FX cap) makes it NaN past
# the window so _compute_states holds the last regime state instead —
# the WS3-verified "gate holds state on NaN" degradation.
GATE_MAX_STALE_DAYS = 10

# Stage 2 — Norgate feed migration (reviews/2026-07-17_norgate-feed-
# migration.md §4). publish_norgate_breadth.py --commit-path writes
# vendor-DERIVED gate states (0/1 only; licence: never the series values)
# to this file each trading morning. When the file is present and fresh
# under the same 10-day cap, its states are consumed directly and
# _compute_states on the scrape series is bypassed; otherwise the scrape
# path below runs verbatim. Rollback = delete or stop refreshing the file.
NORGATE_STATES_PATH = DATA_DIR / "gate_states_norgate.json"

# ----------------------------------------------------------------------
# Phase 22 — EEM/SPY relative-strength tilt parameters
# Test sweep in scripts/test_phase22_eem_overlay.py + funding-source
# comparison in scripts/test_phase22_funding_source.py.
# ----------------------------------------------------------------------
EEM_TILT_ENABLED = True
EEM_TICKER = "EEM"
EEM_REFERENCE_TICKER = "SPY"      # ratio baseline (EEM / SPY)
EEM_TILT_FAST_MA = 50             # short MA of EEM/SPY ratio
EEM_TILT_SLOW_MA = 200            # long MA — golden cross when fast > slow
EEM_TILT_WEIGHT = 0.10            # tilt 10% of blend into EEM on signal-ON
EEM_FUND_FROM_SLEEVE = "strategy_b"  # take the 10pp out of Strategy B
EEM_RATIO_CACHE = "em_regime_context.parquet"
# WS3 maintenance patch (memo proposal #1): staleness cap on the EEM/SPY
# tilt feed. A stopped em_regime_context cache forward-filled without a cap
# would freeze the tilt state and mark the 10pp EEM sleeve at a fake 0%
# daily return while ON. Cap at 10 calendar days (mirroring the Sleeve C FX
# cap); past the window the tilt is HELD FLAT (baseline 35/35/10/20 blend).
EEM_MAX_STALE_DAYS = 10
# Refresh trigger for the cache itself (2026-07-29). EEM_MAX_STALE_DAYS above
# only decides how a stale feed is CONSUMED; nothing re-fetched the cache, so
# `_load_eem_data` used any file that merely had both columns. em_regime_context
# duly froze at 2026-07-06 and the tilt ran 17 weekdays behind for three weeks
# while every consumer published EM_TILT_ON. export_holdings_prices.py had
# already hit this and re-fetches EEM on the same 7-calendar-day rule; mirror
# that here so the tilt leg cannot silently outlive its data.
EEM_MAX_CACHE_AGE_DAYS = 7


def _round(x, n=4):
    if x is None or (isinstance(x, float) and (np.isnan(x) or np.isinf(x))):
        return None
    return round(float(x), n)


def _round_series(arr, n=6):
    return [_round(v, n) for v in arr]


def _stats(daily_ret: pd.Series, eq: pd.Series) -> dict:
    """Match run_multi_strategy.compute_stats exactly so the displayed
    Sharpe / CAGR / DD on the gated variant uses the same methodology
    as every other entry in multi_strategy.json. Otherwise the gated
    variant would show a different Sharpe (mean(daily)*252 vs
    (1+mean)^252-1 etc) and reviewers would be unable to compare them
    apples-to-apples."""
    import math
    if len(eq) < 2:
        return {"sharpe": None, "cagr": None,
                 "total_return": None, "max_dd": None}
    eq = eq / eq.iloc[0]
    daily = eq.pct_change().fillna(0)
    n_years = (eq.index[-1] - eq.index[0]).days / 365.25
    total_ret = float(eq.iloc[-1] - 1.0)
    cagr = (float(eq.iloc[-1]) ** (1.0 / n_years) - 1.0) if n_years > 0 else 0.0
    sharpe = (daily.mean() / daily.std() * math.sqrt(252)
              if daily.std() > 0 else 0.0)
    rolling_max = eq.cummax()
    dd = (eq - rolling_max) / rolling_max
    return {"sharpe": _round(sharpe), "cagr": _round(cagr),
             "total_return": _round(total_ret), "max_dd": _round(float(dd.min()))}


def _compute_states(breadth: pd.Series, off: float, on: float) -> pd.Series:
    """Walk-forward regime detection with hysteresis."""
    states = []
    state = 1.0  # start RISK_ON
    for v in breadth.values:
        if pd.isna(v):
            states.append(state); continue
        if state == 1.0 and v < off:
            state = 0.0
        elif state == 0.0 and v > on:
            state = 1.0
        states.append(state)
    return pd.Series(states, index=breadth.index, dtype=float)


def _load_norgate_states(
    common: pd.DatetimeIndex,
) -> tuple[pd.Series | None, str | None]:
    """Load vendor-derived gate states aligned to ``common`` (Stage 2).

    Returns ``(states, last_bar)`` when NORGATE_STATES_PATH exists, parses
    cleanly, carries strictly binary states, and its last bar is within
    GATE_MAX_STALE_DAYS calendar days of the end of ``common`` — the same
    10-day convention as the D3 scrape cap, measured against the target
    index (deterministic), never wall clock. Returns ``(None, None)`` on
    absence, staleness, or any parse defect so the caller falls through to
    the scrape path verbatim: fail-open to the incumbent feed, never
    fail-silent on a malformed file.
    """
    if not NORGATE_STATES_PATH.exists():
        return None, None
    try:
        doc = json.loads(NORGATE_STATES_PATH.read_text(encoding="utf-8"))
        ser = pd.Series(
            [float(s) for s in doc["series"]["state"]],
            index=pd.to_datetime(doc["series"]["dates"]),
            dtype=float,
        ).sort_index()
        last_bar = pd.Timestamp(doc["last_bar"])
    except Exception as exc:
        print(f"  WARN: {NORGATE_STATES_PATH.name} unreadable "
              f"({type(exc).__name__}: {exc}) — scrape feed governs")
        return None, None
    if len(ser) == 0 or not set(pd.unique(ser.values)).issubset({0.0, 1.0}):
        print(f"  WARN: {NORGATE_STATES_PATH.name} empty or non-binary "
              f"states — scrape feed governs")
        return None, None
    gap_days = int((common[-1] - last_bar).days)
    if gap_days > GATE_MAX_STALE_DAYS:
        print(f"  {NORGATE_STATES_PATH.name} stale ({gap_days} calendar "
              f"days behind blend calendar end > {GATE_MAX_STALE_DAYS}-day "
              f"cap) — scrape feed governs")
        return None, None
    # ffill = hold state on days past the file's last bar — the same
    # degradation the scrape path produces (NaN past the cap holds state
    # inside _compute_states), bounded above by the freshness gate. Days
    # before the file's first bar (1957) cannot occur on the blend
    # calendar; fillna(1.0) mirrors _compute_states' RISK_ON start.
    aligned = ser.reindex(common, method="ffill").fillna(1.0)
    return aligned, doc.get("last_bar")


def _load_eem_data() -> tuple[pd.Series, pd.Series] | tuple[None, None]:
    """Load (EEM_close, EEM_SPY_ratio). Tries em_regime_context.parquet
    first, then falls back to yfinance. Returns (None, None) on failure
    so Phase 22 is gracefully skipped without breaking Phase 19.

    A cache that exists but has STOPPED is re-fetched (EEM_MAX_CACHE_AGE_DAYS,
    calendar days so a weekend + holiday cluster never trips it). If that
    re-fetch fails we keep the stale frame rather than disabling Phase 22 —
    the EEM_MAX_STALE_DAYS cap downstream then holds the tilt flat and stamps
    signal_stale, which is a better outcome than losing the diagnostics.
    """
    cache = DATA_DIR / EEM_RATIO_CACHE
    df = None
    if cache.exists():
        try:
            df = pd.read_parquet(cache)
        except Exception:
            df = None
    usable = (df is not None and EEM_TICKER in df.columns
              and EEM_REFERENCE_TICKER in df.columns)
    stale = False
    if usable and len(df.index):
        last = pd.to_datetime(df.index.max())
        cutoff = pd.Timestamp.now("UTC").tz_localize(None) - pd.Timedelta(
            days=EEM_MAX_CACHE_AGE_DAYS)
        stale = bool(last < cutoff)
        if stale:
            print(f"  {EEM_RATIO_CACHE} stale (last {last.date()}, beyond "
                  f"{EEM_MAX_CACHE_AGE_DAYS}d) — re-fetching", flush=True)
    if not usable or stale:
        reason = "stale" if stale else "not in cache"
        print(f"  Fetching {EEM_TICKER} + {EEM_REFERENCE_TICKER} from "
              f"yfinance ({reason})...", flush=True)
        try:
            import yfinance as yf
            raw = yf.download([EEM_TICKER, EEM_REFERENCE_TICKER],
                               start="2003-01-01", auto_adjust=True,
                               progress=False, threads=True,
                               group_by="ticker")
            closes = {t: raw[(t, "Close")] for t in (EEM_TICKER, EEM_REFERENCE_TICKER)
                      if (t, "Close") in raw.columns}
            fetched = pd.DataFrame(closes)
            fetched.index = pd.to_datetime(fetched.index).tz_localize(None)
            # Only accept a fetch that actually carries both legs and some
            # rows; a partial download must not overwrite a good cache.
            if (EEM_TICKER not in fetched.columns
                    or EEM_REFERENCE_TICKER not in fetched.columns
                    or fetched.dropna().empty):
                raise ValueError(
                    f"incomplete download (cols={list(fetched.columns)}, "
                    f"rows={len(fetched)})")
            df = fetched
            df.to_parquet(cache)
            print(f"  {EEM_RATIO_CACHE} refreshed to {df.index.max().date()} "
                  f"({len(df)} rows)", flush=True)
        except Exception as exc:
            if usable:
                # Keep the stale frame: EEM_MAX_STALE_DAYS holds the tilt flat
                # and stamps signal_stale, which beats losing Phase 22 whole.
                print(f"  WARN: cannot refresh EEM/SPY ({exc}) — continuing on "
                      f"the stale cache; tilt will be held flat and flagged",
                      file=sys.stderr)
            else:
                print(f"  WARN: Phase 22 disabled (cannot fetch EEM/SPY): "
                      f"{exc}", file=sys.stderr)
                return None, None
    eem = df[EEM_TICKER].dropna()
    ratio = (df[EEM_TICKER] / df[EEM_REFERENCE_TICKER]).dropna()
    return eem, ratio


def _compute_eem_tilt_signal(ratio: pd.Series) -> pd.Series:
    """V2 golden-cross: 1 when EEM/SPY 50d MA > 200d MA, else 0.

    Selected over V1 (price-above-MA) because golden cross requires the
    short-term trend to itself be above the long-term trend — both have
    to be improving together, not just a daily blip. Only 11 switches in
    7 years (~1.6/year) vs V1's 65 — much cleaner regime indicator with
    similar empirical lift.
    """
    fast = ratio.rolling(EEM_TILT_FAST_MA, min_periods=EEM_TILT_FAST_MA).mean()
    slow = ratio.rolling(EEM_TILT_SLOW_MA, min_periods=EEM_TILT_SLOW_MA).mean()
    return (fast > slow).astype(float)


def _align_tilt_signal_for_diagnostics(
    eem_signal: pd.Series, common: pd.DatetimeIndex,
) -> tuple[pd.Series, pd.Timestamp | None, bool]:
    """Freshness-capped alignment for the PUBLISHED tilt diagnostics (D9).

    The money path (``_build_eem_tilted_blend``) caps its forward-fill at
    ``EEM_MAX_STALE_DAYS`` and degrades to the baseline blend when the
    EEM/SPY feed stalls; the diagnostics used a raw uncapped ffill, so a
    frozen feed kept publishing a confident ``current_state`` forever.

    Returns ``(sig_aligned, signal_last_valid, signal_stale)``:
      - ``sig_aligned`` holds the LAST VALID signal through any stale tail
        (display continuity; NaN-free, so the daily_series ``int()``
        serialisation cannot crash) and is value-identical to the old raw
        ffill whenever the feed is fresh;
      - ``signal_last_valid`` is the last bar with a within-cap
        observation (None when the feed never overlaps the window);
      - ``signal_stale`` is True when that bar is before the window end —
        the caller must surface it (WARN + payload fields), never hide it.
    Dates before the first observation fill as 0 (no signal -> tilt OFF),
    matching the previous behaviour.
    """
    sig_capped = align_series_to_index(eem_signal, common,
                                       max_stale_days=EEM_MAX_STALE_DAYS)
    signal_last_valid = sig_capped.last_valid_index()
    signal_stale = bool(signal_last_valid is not None
                        and signal_last_valid < common[-1])
    return sig_capped.ffill().fillna(0), signal_last_valid, signal_stale


def _build_eem_tilted_blend(
    multi: dict,
    eem_prices: pd.Series,
    eem_signal: pd.Series,
    common: pd.DatetimeIndex,
    fund_from_sleeve: str = EEM_FUND_FROM_SLEEVE,
    tilt_weight: float = EEM_TILT_WEIGHT,
    switch_cost_bps: float = SWITCH_COST_BPS,
) -> pd.Series | None:
    """Compute the EEM-tilted UNGATED 4-way blend equity curve.

    During tilt-OFF days: normal 35/35/10/20 A:B:C:D blend.
    During tilt-ON days:  the EEM_FUND_FROM_SLEEVE allocation is reduced
                            by tilt_weight, and EEM is added at tilt_weight.
                            (e.g. funding from B: A=35, B=25, C=10, D=20,
                            EEM=10 sums to 100%.)
    """
    sleeves = {}
    for key in ("strategy_a", "strategy_b", "strategy_c", "strategy_d"):
        s = multi.get("strategies", {}).get(key)
        if not s or "dates" not in s or "equity" not in s:
            print(f"  WARN: Phase 22 skipped — sleeve {key} missing",
                  file=sys.stderr)
            return None
        sleeves[key] = pd.Series(s["equity"], index=pd.to_datetime(s["dates"]))

    sleeve_weights = {
        "strategy_a": 0.35, "strategy_b": 0.35,
        "strategy_c": 0.10, "strategy_d": 0.20,
    }
    if fund_from_sleeve not in sleeve_weights:
        print(f"  ERROR: invalid fund_from_sleeve={fund_from_sleeve}",
              file=sys.stderr)
        return None
    base_w = sleeve_weights[fund_from_sleeve]
    if base_w < tilt_weight:
        print(f"  ERROR: tilt_weight {tilt_weight} > base weight of "
              f"{fund_from_sleeve} ({base_w})", file=sys.stderr)
        return None

    rets = {k: s.reindex(common).pct_change().fillna(0)
            for k, s in sleeves.items()}
    # WS3 maintenance patch (memo proposal #1). The EEM price and tilt
    # signal share the em_regime_context cache; an uncapped
    # .reindex(method="ffill") would freeze a stopped cache indefinitely —
    # marking the 10pp EEM sleeve at a fake 0% daily return while the tilt
    # reads ON. Cap the forward-fill at EEM_MAX_STALE_DAYS (mirroring the
    # Sleeve C FX cap) and DEGRADE by holding the tilt flat: on any day the
    # feed is stale, force the tilt OFF so the blend reverts to the baseline
    # 35/35/10/20 rather than trusting a frozen signal/return.
    eem_aligned = align_series_to_index(eem_prices, common,
                                        max_stale_days=EEM_MAX_STALE_DAYS)
    sig_capped = align_series_to_index(eem_signal, common,
                                       max_stale_days=EEM_MAX_STALE_DAYS)
    tilt_stale = eem_aligned.isna() | sig_capped.isna()
    if bool(tilt_stale.iloc[-1]):
        print(f"  WARN: Phase 22 EEM/SPY feed stale > {EEM_MAX_STALE_DAYS} "
              f"days at as-of {common[-1].date()} — tilt held flat "
              f"(baseline blend, no EEM tilt).", file=sys.stderr)
    eem_ret = eem_aligned.pct_change().fillna(0)
    # Lag the signal one day (decision uses the prior close), then force the
    # tilt OFF wherever the feed is stale — the hold-flat degradation path.
    sig = sig_capped.fillna(0).shift(1).fillna(0)
    sig = sig.where(~tilt_stale, 0.0)
    # Tilt-OFF daily return: baseline 35/35/10/20
    tilt_off_ret = sum(sleeve_weights[k] * rets[k] for k in sleeve_weights)
    # Tilt-ON daily return: fund_from_sleeve reduced by tilt_weight; EEM added
    tilt_on_w = {k: sleeve_weights[k] for k in sleeve_weights}
    tilt_on_w[fund_from_sleeve] = base_w - tilt_weight
    tilt_on_ret = sum(tilt_on_w[k] * rets[k] for k in tilt_on_w) + tilt_weight * eem_ret
    # Switch cost on tilt-state transitions
    sw = sig.diff().fillna(0).abs() * (switch_cost_bps / 10_000.0)
    blended_ret = sig * tilt_on_ret + (1.0 - sig) * tilt_off_ret - sw
    return (1.0 + blended_ret).cumprod()


def main() -> int:
    # ----- Load upstream data -----
    multi_path = DATA_DIR / "multi_strategy.json"
    csp1_path = DATA_DIR / "breadth_csp1.json"
    ac_cache_path = DATA_DIR / "asset_class_prices_cache.parquet"
    for required in (multi_path, csp1_path):
        if not required.exists():
            print(f"ERROR: required upstream missing: "
                  f"{required.relative_to(ROOT)}", file=sys.stderr)
            print(f"  Run the upstream pipeline first "
                  f"(run_multi_strategy.py / compute_breadth.py).",
                  file=sys.stderr)
            return 1

    multi = json.loads(multi_path.read_text(encoding="utf-8"))
    blend = multi.get("strategies", {}).get(UNDERLYING_BLEND_KEY)
    if not blend or "dates" not in blend or "equity" not in blend:
        print(f"ERROR: {UNDERLYING_BLEND_KEY} not present in "
              f"multi_strategy.json — run_multi_strategy.py needs to "
              f"emit it first.", file=sys.stderr)
        return 1
    blend_eq = pd.Series(blend["equity"],
                          index=pd.to_datetime(blend["dates"]),
                          name="blend")

    csp1 = json.loads(csp1_path.read_text(encoding="utf-8"))
    breadth = pd.Series(csp1["series"]["ma_breadth"],
                         index=pd.to_datetime(csp1["series"]["dates"]),
                         name="breadth").dropna()
    # Phase 28.5 — capture the panel's TRUE end_date before the downstream
    # `.reindex(common, method='ffill')` extends the index onto the blend's
    # calendar (which can run days past the panel's last real value when
    # the live-track mark-to-market has spliced through). Reporting the
    # ffilled tail as panel_end_date would lie — that's the exact silent-
    # staleness shape Phase 28.5 exists to prevent.
    panel_end_date_str = (csp1.get("end_date")
                            or breadth.index[-1].strftime("%Y-%m-%d"))

    # Try asset_class cache first (in case FALLBACK_TICKER is one of
    # Strategy B's holdings). Otherwise fetch fresh + cache locally.
    fallback = None
    if ac_cache_path.exists():
        try:
            df = pd.read_parquet(ac_cache_path)
            if FALLBACK_TICKER in df.columns:
                fallback = df[FALLBACK_TICKER].dropna()
        except Exception:
            pass
    if fallback is None:
        local_cache = DATA_DIR / f"risk_overlay_{FALLBACK_TICKER.lower()}_cache.parquet"
        if local_cache.exists():
            try:
                ser = pd.read_parquet(local_cache)
                if FALLBACK_TICKER in ser.columns:
                    fallback = ser[FALLBACK_TICKER].dropna()
            except Exception:
                pass
    if fallback is None:
        print(f"  Fetching {FALLBACK_TICKER} from yfinance (not in any "
              f"existing cache)...", flush=True)
        try:
            import yfinance as yf
            raw = yf.download(FALLBACK_TICKER, start="2007-01-01",
                               progress=False, auto_adjust=True)
            close = raw["Close"]
            if isinstance(close, pd.DataFrame):
                close = close.iloc[:, 0]
            fallback = close.dropna()
            fallback.name = FALLBACK_TICKER
            # Cache locally for next run — keeps the script idempotent
            # and offline-capable.
            local_cache = DATA_DIR / f"risk_overlay_{FALLBACK_TICKER.lower()}_cache.parquet"
            fallback.to_frame().to_parquet(local_cache)
        except Exception as exc:
            print(f"ERROR: cannot fetch {FALLBACK_TICKER} fallback "
                  f"prices: {exc}", file=sys.stderr)
            return 1

    # ----- Align on the blend's calendar -----
    common = blend_eq.index
    # WS3 maintenance patch (defect D3): cap the gate-breadth forward-fill
    # at GATE_MAX_STALE_DAYS. Past the cap the value is NaN, which
    # _compute_states holds as the last regime state ("gate holds state on
    # NaN") — NOT a frozen breadth number that a stalled CSP1 feed could
    # otherwise carry forward and act on. (fallback SHY below stays a plain
    # ffill: a 1-3y Treasury proxy, not a signal feed, and out of scope.)
    breadth = align_series_to_index(breadth, common,
                                    max_stale_days=GATE_MAX_STALE_DAYS)
    fallback_aligned = fallback.reindex(common, method="ffill")
    blend_ret = blend_eq.pct_change().fillna(0)
    fallback_ret = fallback_aligned.pct_change().fillna(0)

    # ----- Compute gated equity -----
    # Stage 2 feed preference (review §4): vendor-derived states when the
    # published file is fresh, scrape-computed states otherwise —
    # behaviour identical to the pre-Stage-2 pipeline whenever the file is
    # absent or stale past the cap.
    norgate_states, norgate_last_bar = _load_norgate_states(common)
    if norgate_states is not None:
        states = norgate_states
        gate_feed = "norgate-local"
    else:
        states = _compute_states(breadth, OFF_THRESHOLD, ON_THRESHOLD)
        gate_feed = "csp1-scrape"
    states_lagged = states.shift(1).fillna(1.0)
    state_changes = states_lagged.diff().fillna(0).abs()
    switch_cost = state_changes * (SWITCH_COST_BPS / 10_000.0)
    blend_w = states_lagged + (1.0 - states_lagged) * (1.0 - DERISK_FRACTION)
    fallback_w = (1.0 - states_lagged) * DERISK_FRACTION
    gated_ret = blend_w * blend_ret + fallback_w * fallback_ret - switch_cost
    gated_eq = (1.0 + gated_ret).cumprod()

    # ----- Diagnostics -----
    n_switches = int(state_changes.sum())
    days_off = int((states_lagged == 0).sum())
    pct_off = days_off / len(states_lagged) * 100
    transitions = states.diff().fillna(0)
    last_change_date = (states.index[transitions != 0][-1]
                         if (transitions != 0).any() else states.index[0])
    current_state = "RISK_ON" if states.iloc[-1] == 1.0 else "RISK_OFF"
    # NOTE (Stage 2): event `breadth` annotations always read the SCRAPE
    # series — the derived-states file carries no levels (licence). On the
    # norgate feed a flip date may therefore annotate a scrape level that
    # sits on the other side of the threshold; the annotation is
    # diagnostic only, never an input.
    events = [
        {"date": d.strftime("%Y-%m-%d"),
         "direction": "RISK_OFF" if states.loc[d] == 0.0 else "RISK_ON",
         "breadth": _round(breadth.loc[d])}
        for d in states.index[transitions != 0]
    ]

    ungated_stats = _stats(blend_ret, blend_eq)
    gated_stats = _stats(gated_ret, gated_eq)

    # ----- Phase 22: EEM/SPY relative-strength tilt -----
    # Build a second gated variant where the EEM-tilted ungated blend is
    # subject to the SAME Phase 19 breadth gate. Both overlays compose.
    phase22_payload = None
    tilted_gated_stats = None
    tilted_gated_eq = None
    tilted_gated_key = f"{UNDERLYING_BLEND_KEY}_gated_eem_tilted"
    if EEM_TILT_ENABLED:
        eem_prices, eem_ratio = _load_eem_data()
        if eem_prices is not None and eem_ratio is not None:
            eem_signal = _compute_eem_tilt_signal(eem_ratio)
            tilted_ungated_eq = _build_eem_tilted_blend(
                multi, eem_prices, eem_signal, common,
                fund_from_sleeve=EEM_FUND_FROM_SLEEVE,
                tilt_weight=EEM_TILT_WEIGHT,
                switch_cost_bps=SWITCH_COST_BPS,
            )
            if tilted_ungated_eq is not None:
                # Apply the Phase 19 breadth gate to the tilted ungated.
                tilted_ungated_ret = tilted_ungated_eq.pct_change().fillna(0)
                tilted_gated_ret = (blend_w * tilted_ungated_ret
                                     + fallback_w * fallback_ret
                                     - switch_cost)
                tilted_gated_eq = (1.0 + tilted_gated_ret).cumprod()
                tilted_gated_stats = _stats(tilted_gated_ret, tilted_gated_eq)
                # Phase 22 diagnostics — freshness-capped (D9, 2026-07-18):
                # aligned like the money path so a stalled feed cannot keep
                # publishing a confident live state. The displayed series
                # holds the last valid reading; the payload carries
                # signal_as_of / signal_stale so consumers can badge it.
                (sig_aligned, signal_last_valid,
                 signal_stale) = _align_tilt_signal_for_diagnostics(
                    eem_signal, common)
                if signal_stale:
                    print(f"  WARN: Phase 22 tilt DIAGNOSTIC frozen past "
                          f"{signal_last_valid.date()} (EEM/SPY feed stale "
                          f"> {EEM_MAX_STALE_DAYS}d) — published state is "
                          f"the last valid reading, not a live signal")
                tilt_state = "EM_TILT_ON" if sig_aligned.iloc[-1] == 1.0 else "EM_TILT_OFF"
                tilt_transitions = sig_aligned.diff().fillna(0)
                last_tilt_change = (sig_aligned.index[tilt_transitions != 0][-1]
                                     if (tilt_transitions != 0).any()
                                     else sig_aligned.index[0])
                tilt_n_switches = int(tilt_transitions.abs().sum())
                tilt_days_on = int((sig_aligned == 1.0).sum())
                tilt_events = [
                    {"date": d.strftime("%Y-%m-%d"),
                     "direction": ("EM_TILT_ON" if sig_aligned.loc[d] == 1.0
                                    else "EM_TILT_OFF"),
                     "ratio": _round(eem_ratio.reindex([d], method="ffill").iloc[0]),
                     "fast_ma": _round(eem_ratio.rolling(EEM_TILT_FAST_MA)
                                       .mean().reindex([d], method="ffill").iloc[0]),
                     "slow_ma": _round(eem_ratio.rolling(EEM_TILT_SLOW_MA)
                                       .mean().reindex([d], method="ffill").iloc[0])}
                    for d in sig_aligned.index[tilt_transitions != 0]
                ]
                # Emit the daily time series so the EEM Tilt tab can
                # render a chart of the underlying signal (the raw
                # numbers — current ratio 0.09xx — are not meaningful
                # to a reader without the trend context).
                fast_ma_series = (eem_ratio
                                   .rolling(EEM_TILT_FAST_MA).mean())
                slow_ma_series = (eem_ratio
                                   .rolling(EEM_TILT_SLOW_MA).mean())
                # Restrict to the common backtest window for the chart
                # so it aligns with the blend equity panels.
                chart_idx = sig_aligned.index
                ratio_chart = eem_ratio.reindex(chart_idx, method="ffill")
                fast_chart = fast_ma_series.reindex(chart_idx, method="ffill")
                slow_chart = slow_ma_series.reindex(chart_idx, method="ffill")
                daily_series = {
                    "dates": [d.strftime("%Y-%m-%d") for d in chart_idx],
                    "ratio": [_round(v, 4) for v in ratio_chart.values],
                    "fast_ma": [_round(v, 4) for v in fast_chart.values],
                    "slow_ma": [_round(v, 4) for v in slow_chart.values],
                    "tilt_state": [int(v) for v in sig_aligned.values],
                }

                phase22_payload = {
                    "enabled": True,
                    "parameters": {
                        "eem_ticker": EEM_TICKER,
                        "reference_ticker": EEM_REFERENCE_TICKER,
                        "fast_ma": EEM_TILT_FAST_MA,
                        "slow_ma": EEM_TILT_SLOW_MA,
                        "tilt_weight": EEM_TILT_WEIGHT,
                        "fund_from_sleeve": EEM_FUND_FROM_SLEEVE,
                    },
                    "current_state": tilt_state,
                    "current_state_since": last_tilt_change.strftime("%Y-%m-%d"),
                    # D9 — freshness of the signal behind current_state. A
                    # consumer must treat signal_stale=True like the gate's
                    # stale-panel banner: last valid reading, not live.
                    "signal_as_of": (signal_last_valid.strftime("%Y-%m-%d")
                                      if signal_last_valid is not None
                                      else None),
                    "signal_stale": signal_stale,
                    "current_ratio": _round(eem_ratio.iloc[-1]),
                    "current_fast_ma": _round(
                        eem_ratio.rolling(EEM_TILT_FAST_MA).mean().iloc[-1]),
                    "current_slow_ma": _round(
                        eem_ratio.rolling(EEM_TILT_SLOW_MA).mean().iloc[-1]),
                    "n_switches": tilt_n_switches,
                    "days_tilt_on": tilt_days_on,
                    "pct_days_tilt_on": _round(
                        tilt_days_on / len(sig_aligned) * 100, 2),
                    "events": tilt_events,
                    "daily_series": daily_series,
                }
        else:
            print(f"  Phase 22 skipped (EEM/SPY data unavailable)")

    label = (f"DEPLOYED · {UNDERLYING_BLEND_KEY} with CSP1 breadth gate "
              f"(Phase 19: off={int(OFF_THRESHOLD*100)}%, "
              f"on={int(ON_THRESHOLD*100)}%, "
              f"derisk={int(DERISK_FRACTION*100)}%)")
    gated_key = f"{UNDERLYING_BLEND_KEY}_gated"

    payload = {
        "computed_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "underlying_blend_key": UNDERLYING_BLEND_KEY,
        "gate_parameters": {
            "off_threshold": OFF_THRESHOLD,
            "on_threshold": ON_THRESHOLD,
            "derisk_fraction": DERISK_FRACTION,
            "switch_cost_bps": SWITCH_COST_BPS,
            "fallback_ticker": FALLBACK_TICKER,
        },
        "current_state": current_state,
        "current_state_since": last_change_date.strftime("%Y-%m-%d"),
        "current_breadth": _round(breadth.iloc[-1]),
        "n_switches": n_switches,
        "days_risk_off": days_off,
        "pct_days_risk_off": _round(pct_off, 2),
        "events": events,
        "ungated_reference": {
            "key": UNDERLYING_BLEND_KEY, **ungated_stats,
        },
        "gated_variants": {
            gated_key: {
                "label": label,
                "dates": [d.strftime("%Y-%m-%d") for d in gated_eq.index],
                "equity": _round_series(gated_eq.values),
                **gated_stats,
            },
        },
    }
    # Append Phase 22 (EEM-tilted gated variant) when available
    if tilted_gated_eq is not None and tilted_gated_stats is not None:
        tilted_label = (
            f"DEPLOYED · {UNDERLYING_BLEND_KEY} with breadth gate + EEM tilt "
            f"(Phase 19 + 22: golden cross V2, {int(EEM_TILT_WEIGHT*100)}% "
            f"funded from {EEM_FUND_FROM_SLEEVE})"
        )
        # Promote the EEM-tilted variant to DEPLOYED by tagging the original
        # as REFERENCE. The dashboard's getDeployedBlendKey() will resolve to
        # the eem_tilted key when present.
        payload["gated_variants"][gated_key]["label"] = (
            f"REFERENCE · {UNDERLYING_BLEND_KEY} with breadth gate only "
            f"(Phase 19, no EEM tilt)"
        )
        payload["gated_variants"][tilted_gated_key] = {
            "label": tilted_label,
            "dates": [d.strftime("%Y-%m-%d") for d in tilted_gated_eq.index],
            "equity": _round_series(tilted_gated_eq.values),
            **tilted_gated_stats,
        }
        payload["phase22_eem_tilt"] = phase22_payload
    else:
        payload["phase22_eem_tilt"] = {"enabled": False}
    # ----- Phase 28.5 guards (FM-1 panel_end_date, FM-2 reconciliation,
    # historical-revision detection) ---------------------------------------
    # FM-1 surfacing: emit the panel's end_date alongside the regime
    # headline so renderers can compute their own freshness verdict
    # without having to re-open breadth_csp1.json. This is the explicit
    # provenance field the 2026-06-13 publish was missing. Use the
    # CSP1-original end_date (captured pre-ffill), not breadth.index[-1]
    # which has been extended onto the blend calendar by ffill.
    payload["panel_end_date"] = panel_end_date_str
    # Stage 2 feed provenance: which feed produced the STATES this run,
    # and that feed's own freshness anchor. csp1-scrape keeps the CSP1
    # panel end as its anchor (behaviour unchanged from pre-Stage-2).
    payload["gate_feed"] = gate_feed
    payload["gate_feed_last_bar"] = (
        norgate_last_bar if gate_feed == "norgate-local"
        else panel_end_date_str
    )

    # FM-2 reconciliation: current_state_since must equal the most recent
    # event date matching current_state. Hard-fail at write time so the
    # publish path never emits a JSON whose headline date silently disagrees
    # with its own events list (which would have caught the
    # 2025-05-02-vs-2026-04-09 mismatch that lingered through the
    # 2026-06-13 publish if today's events recomputation had been right).
    series_start = breadth.index[0].strftime("%Y-%m-%d")
    assert_state_since_matches_events(
        current_state=current_state,
        current_state_since=payload["current_state_since"],
        events=events,
        series_start_date=series_start,
    )

    # Historical-revision detection: compare today's events list to the
    # previously-committed file. If past-date entries have appeared,
    # disappeared, or flipped direction, surface the change in the payload
    # so the dashboard / factsheet can call it out rather than silently
    # publishing the new history.
    historical_revision: list[dict] = []
    if OUT_PATH.exists():
        try:
            prior = json.loads(OUT_PATH.read_text(encoding="utf-8"))
            historical_revision = detect_historical_revision(
                prior.get("events", []) or [], events,
            )
        except Exception as e:
            print(f"  WARN: could not read prior overlay for revision check: {e}")
    payload["historical_revision"] = historical_revision
    if historical_revision:
        print(f"  HISTORICAL REGIME REVISION: {len(historical_revision)} "
               f"past-date event(s) changed by this run:")
        for r in historical_revision:
            print(f"    {r['date']}  {r['change']}  "
                   f"{r.get('from','-')} -> {r.get('to','-')}")

    OUT_PATH.write_text(json.dumps(payload, separators=(",", ":")),
                         encoding="utf-8")

    # Console summary
    delta_sh = (gated_stats["sharpe"] or 0) - (ungated_stats["sharpe"] or 0)
    delta_dd = (gated_stats["max_dd"] or 0) - (ungated_stats["max_dd"] or 0)
    print(f"Phase 19 risk overlay — built {OUT_PATH.relative_to(ROOT)}")
    print(f"  Underlying blend ({UNDERLYING_BLEND_KEY}):")
    print(f"    Sharpe {ungated_stats['sharpe']:+.4f}  "
          f"CAGR {ungated_stats['cagr']*100:+.1f}%  "
          f"DD {ungated_stats['max_dd']*100:+.2f}%")
    print(f"  Gated variant ({gated_key}):")
    print(f"    Sharpe {gated_stats['sharpe']:+.4f}  "
          f"CAGR {gated_stats['cagr']*100:+.1f}%  "
          f"DD {gated_stats['max_dd']*100:+.2f}%")
    print(f"    Delta vs ungated: Sharpe {delta_sh:+.4f}  "
          f"DD {delta_dd*100:+.2f}pp")
    print(f"  Current state: {current_state} since "
          f"{last_change_date.strftime('%Y-%m-%d')}  "
          f"(S&P 500 breadth {breadth.iloc[-1]*100:.1f}%)")
    print(f"  Gate feed: {gate_feed} "
          f"(states last bar {payload['gate_feed_last_bar']})")
    print(f"  History: {n_switches} switches, "
          f"{pct_off:.1f}% of days RISK_OFF")
    if tilted_gated_eq is not None and tilted_gated_stats is not None:
        delta_tilt_sh = ((tilted_gated_stats["sharpe"] or 0)
                          - (gated_stats["sharpe"] or 0))
        delta_tilt_dd = ((tilted_gated_stats["max_dd"] or 0)
                          - (gated_stats["max_dd"] or 0))
        print(f"  Phase 22 EEM-tilted gated variant ({tilted_gated_key}):")
        print(f"    Sharpe {tilted_gated_stats['sharpe']:+.4f}  "
              f"CAGR {tilted_gated_stats['cagr']*100:+.1f}%  "
              f"DD {tilted_gated_stats['max_dd']*100:+.2f}%")
        print(f"    Delta vs gated (no tilt): Sharpe {delta_tilt_sh:+.4f}  "
              f"DD {delta_tilt_dd*100:+.2f}pp")
        if phase22_payload:
            print(f"  Phase 22 tilt: {phase22_payload['current_state']} since "
                  f"{phase22_payload['current_state_since']}  "
                  f"(EEM/SPY ratio {phase22_payload['current_ratio']:.4f}, "
                  f"fast {phase22_payload['current_fast_ma']:.4f}, "
                  f"slow {phase22_payload['current_slow_ma']:.4f})")
            print(f"  Phase 22 history: {phase22_payload['n_switches']} switches, "
                  f"{phase22_payload['pct_days_tilt_on']:.1f}% of days tilt-ON")
    return 0


if __name__ == "__main__":
    sys.exit(main())
