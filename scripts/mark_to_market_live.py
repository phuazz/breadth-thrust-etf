"""Daily mark-to-market overlay for the deployed blend.

Why this exists
---------------
The full pipeline runs weekly on Saturday (after Friday US close). The
deployed-blend equity series in ``data/risk_overlay.json`` therefore
ends on Friday's close and stays static all week. For a CIO doing live
deployment tracking on Tue / Wed / Thu, this is one or more weekdays
behind.

The strategy itself rebalances Friday close — the holdings selected
at Friday close apply Mon→Fri of the following week, with no intra-
week trading. So computing mid-week NAV does NOT require re-running
signals, walk-forward, bootstrap, or any other expensive logic. It is
a simple weighted-buy-and-hold of the latest holdings against this
week's daily ETF closes.

This script does exactly that and writes ``data/live_track.json`` —
a small JSON the dashboard appends to the deployed blend equity series
before computing the WTD hero card and drawing the Performance chart.

Architecture invariant
----------------------
This script NEVER touches the backtest engine state. The Friday-anchor
equity in ``risk_overlay.json`` is the source of truth; ``live_track``
is a strictly forward-only extension from that anchor. If anything
disagrees, ``risk_overlay.json`` wins.

Run
---
    python scripts/mark_to_market_live.py

Output
------
    data/live_track.json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from overlay_state import tilt_display_state  # noqa: E402

DEPLOYED_KEY = "blend_35_35_10_20_gated_eem_tilted"

# Europe sleeve UCITS on Xetra — need .DE suffix and EUR/USD conversion.
EUROPE_TICKERS = {"EXV1", "EXH1", "EXV3", "EXH3", "EXH9"}

# China A-share tickers — already include the .SZ/.SS suffix in the
# trade_history; needs USDCNY=X conversion.
CN_FX_SUFFIXES = (".SZ", ".SS")


def _load_registry() -> dict:
    """Import the canonical ETF registry as a dict.

    Used to look up the ``yfinance_trading_proxy`` for Strategy A's
    iShares UCITS (e.g. IUES → XLE), so the mark-to-market uses the
    liquid US-listed equivalent rather than the .L UCITS.
    """
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    from etf_registry import ETF_REGISTRY  # type: ignore[import]
    return ETF_REGISTRY


def _resolve_yf_symbol(ticker: str, registry: dict) -> tuple[str, str]:
    """Map a holdings ticker to (yfinance_symbol, fx_handling).

    fx_handling ∈ {'none', 'eur_to_usd', 'cny_to_usd'} tells the caller
    how to post-process the raw yfinance series into USD.

    Strategy A iShares UCITS use the US-listed trading proxy from the
    registry. Europe-sleeve UCITS also take their symbol from the registry
    proxy, and get EUR/USD conversion. China A-shares get CNY/USD.
    Everything else is assumed to be a direct USD-denominated yfinance
    ticker (SPY, EEM, BTC-USD, ...).

    The registry proxy is the ONLY source of a traded symbol. This branch
    used to return ``f"{ticker}.DE"``, which silently assumes the Xetra
    ticker equals the registry key — true for four of the five Europe
    members and false for EXH3, whose panel is Industrial Goods & Services
    (traded as EXH4.DE) while EXH3.DE is a food & beverage fund. The
    concatenation meant the live book priced a different instrument from
    the one the backtest used, on a surface no test compared. Keep the
    suffix only as a fallback for a key with no proxy recorded, and see
    tests/test_europe_symbol_contract.py.
    """
    if ticker in EUROPE_TICKERS:
        proxy = (registry.get(ticker) or {}).get("yfinance_trading_proxy")
        return (proxy or f"{ticker}.DE", "eur_to_usd")
    if ticker.endswith(CN_FX_SUFFIXES):
        return (ticker, "cny_to_usd")
    if ticker in registry and registry[ticker].get("yfinance_trading_proxy"):
        proxy = registry[ticker]["yfinance_trading_proxy"]
        # If the ticker IS the proxy (e.g. SPY -> SPY) just use it.
        return (proxy, "none")
    return (ticker, "none")


def _yf_close_series(symbols: list[str], start: str, end: str) -> pd.DataFrame:
    """Download adj close for ``symbols`` over [start, end] as a single
    DataFrame indexed by date with one column per symbol. Returns an
    empty DataFrame if all symbols fail (graceful for CI without
    network)."""
    if not symbols:
        return pd.DataFrame()
    import yfinance as yf
    raw = yf.download(
        symbols, start=start, end=end,
        auto_adjust=True, progress=False, threads=False,
        group_by="ticker" if len(symbols) > 1 else None,
    )
    if raw is None or raw.empty:
        return pd.DataFrame()
    out = {}
    if isinstance(raw.columns, pd.MultiIndex):
        for sym in symbols:
            if sym in raw.columns.get_level_values(0):
                series = raw[sym]["Close"] if "Close" in raw[sym].columns else None
                if series is not None and not series.empty:
                    out[sym] = series
    else:
        if "Close" in raw.columns:
            out[symbols[0]] = raw["Close"]
    df = pd.DataFrame(out)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df.sort_index()


# Largest deviation from 1.0 a sleeve's own weights may show and still be
# treated as 4dp rounding rather than a defect. Six holdings rounded to 4dp
# can drift 3e-4; 1e-3 leaves headroom for a larger sleeve without admitting
# anything a human would call a wrong weight.
SLEEVE_ROUNDING_TOLERANCE = 1e-3


def _build_effective_weights(
    sleeves: dict, p22_active: bool, regime_state: str
) -> dict[str, float]:
    """Mirror the dashboard's renderPositionsPreview NAV-weight logic so
    the mark-to-market uses the same holdings the live panel shows.

    Returns ``{ticker: nav_weight}`` summing to 1.0 (after the cash
    residual if any). Sleeve weights apply the EEM-tilt adjustment when
    active. The RISK_OFF regime gate is honoured by halving the
    equity-side weights and adding a 50% SHY weight — same as the
    deployed blend equity construction.
    """
    base_wts = {
        "a": 0.35,
        "b": 0.25 if p22_active else 0.35,
        "c": 0.10,
        "d": 0.20,
    }
    # RISK_OFF: half NAV in SHY, half in original blend
    equity_scaler = 0.5 if regime_state == "RISK_OFF" else 1.0
    sleeve_wts = {k: v * equity_scaler for k, v in base_wts.items()}

    weights: dict[str, float] = {}
    for key, sleeve in sleeves.items():
        sw = sleeve_wts[key]
        trades = (sleeve.get("headline", {}) or {}).get("trade_history", [])
        if not trades:
            continue
        held = {h["etf"]: h["weight"]
                for h in trades[-1].get("holdings", [])
                if h.get("etf") and h.get("weight", 0) > 0}
        if not held:
            continue

        # NORMALISE WITHIN THE SLEEVE BEFORE SCALING TO NAV.
        #
        # The engines round their within-sleeve weights to 4dp and do not
        # renormalise, so a sleeve's own weights sum to 0.9999-1.0001
        # depending on the week. Scaled to NAV and summed across sleeves,
        # that leaves the book at 100.0035% of NAV, as it was on
        # 2026-08-22 (strategy A's six holdings summed to 1.0001). The
        # cash residual below cannot absorb it: a NEGATIVE residual fails
        # the `> 1e-6` test and is silently dropped, so an overweight book
        # has no absorber at all while an underweight one does.
        #
        # Normalising HERE, per sleeve, keeps each sleeve at exactly its
        # mandated share — sleeve A's rounding is absorbed by sleeve A and
        # never lands on B, C, D or the tilt, which is what a pro-rata
        # renormalisation of the whole book would do.
        #
        # The treatment is ASYMMETRIC, because the two signs mean different
        # things. A sleeve UNDER 1.0 by more than rounding is a deliberate
        # cash floor — sleeve B holding 30% with 70% in cash is a real
        # state, and its residual belongs in SHY, so leave it untouched.
        # A sleeve OVER 1.0 by more than rounding cannot be a state at all:
        # nothing can be more than fully invested, so that is a defect and
        # must fail rather than be quietly scaled into looking correct.
        held_sum = sum(held.values())
        if held_sum > 1.0 + SLEEVE_ROUNDING_TOLERANCE:
            raise ValueError(
                f"sleeve {key} holdings sum to {held_sum:.6f}, over 1.0 by "
                f"more than the {SLEEVE_ROUNDING_TOLERANCE:g} rounding band — "
                f"a sleeve cannot be more than fully invested, so this is a "
                f"weight-construction defect, not 4dp rounding"
            )
        divisor = (held_sum
                   if abs(held_sum - 1.0) <= SLEEVE_ROUNDING_TOLERANCE
                   else 1.0)
        for etf, wt in held.items():
            weights[etf] = weights.get(etf, 0) + (wt / divisor) * sw

    # EEM tilt — 10% NAV (also scaled by RISK_OFF)
    if p22_active:
        weights["EEM"] = weights.get("EEM", 0) + 0.10 * equity_scaler

    # Cash residual goes into SHY (also when RISK_OFF allocates 50%).
    cash_wt = 1.0 - sum(weights.values())
    if cash_wt > 1e-6:
        weights["SHY"] = weights.get("SHY", 0) + cash_wt

    return weights


def _sleeve_holdings_as_weights(sleeve: dict) -> dict[str, float]:
    """Extract within-sleeve weights from the latest trade_history entry.

    Returned weights sum to <= 1.0 (cash-floor residual is implicit).
    Zero-weight holdings are filtered out.
    """
    trades = (sleeve.get("headline", {}) or {}).get("trade_history", [])
    if not trades:
        return {}
    out: dict[str, float] = {}
    for h in trades[-1].get("holdings", []):
        etf = h.get("etf")
        w = h.get("weight", 0)
        if etf and w > 0:
            out[etf] = out.get(etf, 0) + w
    return out


def _observed_mark(prices: pd.DataFrame, ticker: str, day: pd.Timestamp):
    """Require a positive observed mark; carry only across a proven venue closure.

    Python/Pandas calendar months are 1-indexed. A missing trading-session row
    is not a holiday. Unknown venues and calendar failures cannot authorise carry.
    FX alignment retains its calculation cap, but an unobserved FX observation
    cannot establish a fresh daily NAV.
    """
    from export_holdings_prices import venue_calendar_for
    from native_sessions import native_schedule

    if ticker not in prices:
        return None
    series = prices[ticker]
    observed_day = day
    symbol, _ = _resolve_yf_symbol(ticker, _load_registry())
    venue = venue_calendar_for(symbol)
    value = series.get(day)
    # The caller supplies the NYSE session set. Foreign venues also need
    # their own calendar even when a vendor supplies a non-null holiday row.
    if venue not in (None, "NYSE") or value is None or pd.isna(value):
        if venue is None:
            return None
        try:
            schedule, _ = native_schedule(venue, day - pd.Timedelta(days=31), day)
        except Exception as exc:  # Calendar faults cannot authorise publication.
            print(f"  Calendar unavailable for {ticker}: {exc}")
            return None
        if schedule.empty:
            return None
        observed_day = pd.Timestamp(schedule.index[-1]).tz_localize(None)
        value = series.get(observed_day)
    if observed_day.strftime("%Y-%m-%d") in prices.attrs.get("missing_fx", {}).get(ticker, []):
        return None
    if value is None or not math.isfinite(float(value)) or value <= 0:
        return None
    return value


def _project_daily_equity(
    weights: dict[str, float],
    anchor_equity: float,
    prices: pd.DataFrame,
    anchor_ts: pd.Timestamp,
    session_cap: pd.Timestamp | None = None,
    valid_sessions: set | None = None,
) -> tuple[list[str], list[float]]:
    """Compute weighted buy-and-hold NAV from anchor_ts forward.

    Returns parallel (dates, equity) lists for dates strictly AFTER
    anchor_ts. Every held instrument needs an observed anchor and session
    mark. Stop at the first incomplete session, including an entirely absent
    row. Only a proven venue holiday permits a prior-session mark. The cash
    residual (1 - sum(weights)) remains flat.

    ``session_cap`` (the last completed NYSE session) bounds the output:
    no point is emitted for a price date after it. This keeps the deployed
    series USD-NYSE-anchored — on a US-holiday Friday when only the Europe
    sleeve traded, yfinance supplies that later bar but the product must
    not stamp its as-of on a day with no US close. Any caller that extends
    the deployed series MUST pass it (a normal weekday cap is a no-op).

    ``valid_sessions`` (a set of ``datetime.date`` — the true NYSE
    sessions covering the fetch window) filters INTERIOR non-NYSE dates
    too. The cap alone only trims the tail: a Europe-only holiday bar
    (Juneteenth, Independence Day) fell inside the range on the NEXT
    run and re-entered the splice — the published 2026-06-22 and
    2026-07-06 daily builds each carried such a phantom bar, shifting
    the dashboard's WTD base by -0.25pp on 2026-07-06. Callers that
    extend any published series MUST pass it.
    """
    baselines: dict[str, float] = {}
    weights = {e: w for e, w in weights.items() if w > 0}
    for etf in weights:
        value = _observed_mark(prices, etf, anchor_ts)
        if value is None:
            print(f"  Incomplete anchor {anchor_ts.date()}: {etf}; no extension")
            return [], []
        baselines[etf] = value
    cash_wt = max(0.0, 1.0 - sum(weights.values()))

    post_dates = prices.index[prices.index > anchor_ts]
    if valid_sessions is not None:
        # Enumerate the expected calendar, so an omitted whole row cannot
        # disappear from the completeness check and leave an interior hole.
        post_dates = pd.DatetimeIndex(sorted(
            pd.Timestamp(d) for d in valid_sessions if pd.Timestamp(d) > anchor_ts))
    if session_cap is not None:
        post_dates = post_dates[post_dates <= session_cap]
    dates_out: list[str] = []
    equity_out: list[float] = []
    for d in post_dates:
        marks = {e: _observed_mark(prices, e, d) for e in weights}
        missing = [e for e, value in marks.items() if value is None]
        if missing:
            print(f"  Incomplete session {d.date()}: {', '.join(missing)}; extension stopped")
            break
        factor = cash_wt
        for etf, w in weights.items():
            factor += w * (marks[etf] / baselines[etf])
        dates_out.append(d.strftime("%Y-%m-%d"))
        equity_out.append(anchor_equity * factor)
    return dates_out, equity_out


def _fetch_usd_prices(
    weights: dict[str, float], anchor_date: str, registry: dict
) -> pd.DataFrame:
    """Fetch USD-denominated close series for each held ticker from
    ``anchor_date`` forward. Returns a DataFrame indexed by date with
    one column per ETF ticker (NOT yfinance symbol — we map back).

    Applies EUR/USD or CNY/USD conversion where needed so every series
    is in USD terms, matching the deployed blend equity construction.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Cover the same bounded native-calendar window used for a holiday anchor.
    start = (pd.Timestamp(anchor_date) - pd.Timedelta(days=31)).strftime("%Y-%m-%d")
    end = (pd.Timestamp(today) + pd.Timedelta(days=2)).strftime("%Y-%m-%d")

    # Resolve each ETF -> (yfinance_symbol, fx_handling)
    resolutions = {etf: _resolve_yf_symbol(etf, registry) for etf in weights}
    all_yf_syms = sorted(set(s for s, _ in resolutions.values()))
    needs_eur_fx = any(fx == "eur_to_usd" for _, fx in resolutions.values())
    needs_cny_fx = any(fx == "cny_to_usd" for _, fx in resolutions.values())

    fx_syms = []
    if needs_eur_fx:
        fx_syms.append("EURUSD=X")
    if needs_cny_fx:
        fx_syms.append("USDCNY=X")

    print(f"  Fetching {len(all_yf_syms)} ETF symbol(s) + {len(fx_syms)} FX "
          f"from {start} to {end} ...")
    raw = _yf_close_series(all_yf_syms + fx_syms, start, end)
    if raw.empty:
        return pd.DataFrame()

    # Normalise the index (drop time component). Convert each ETF's native
    # price to USD by forward-filling the FX rate onto its dates with a
    # 10-calendar-day staleness cap (mirrors the Sleeve C / Sleeve D engine
    # FX caps, defect D4): a stalled FX feed degrades to NaN — the USD price
    # drops out for that span — rather than silently freezing the last rate,
    # keeping the live path at parity with the engine. Both the EUR->USD
    # (Sleeve D) and CNY->USD (Sleeve C) legs get the cap; the engine caps
    # its CNY leg already, so capping only EUR here would leave the live path
    # inconsistent with itself.
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    from alignment import align_series_to_index  # noqa: E402
    raw.index = pd.to_datetime(raw.index).normalize()
    eur_usd = raw["EURUSD=X"].dropna() if "EURUSD=X" in raw.columns else None
    usd_cny = raw["USDCNY=X"].dropna() if "USDCNY=X" in raw.columns else None

    out = {}
    missing_fx = {}
    for etf, (sym, fx) in resolutions.items():
        if sym not in raw.columns:
            continue
        series = raw[sym].dropna()
        if series.empty:
            continue
        fx_series = eur_usd if fx == "eur_to_usd" else usd_cny
        if fx != "none":
            # Keep the established capped conversion, but carry observation
            # provenance into the publication guard. Missing FX must never
            # leave an unconverted native price masquerading as USD.
            observed_fx = (fx_series.reindex(series.index) if fx_series is not None
                           else pd.Series(float("nan"), index=series.index))
            missing_fx[etf] = [d.strftime("%Y-%m-%d") for d, value in observed_fx.items()
                               if pd.isna(value) or not math.isfinite(float(value)) or value <= 0]
            if fx_series is None:
                out[etf] = series * float("nan")
                continue
        if fx == "eur_to_usd":
            # EUR price * (USD/EUR) -> USD price
            fx_aligned = align_series_to_index(eur_usd, series.index,
                                               max_stale_days=10)
            series = series * fx_aligned
        elif fx == "cny_to_usd":
            # CNY price * (1 / (CNY/USD)) -> USD price
            fx_aligned = align_series_to_index(usd_cny, series.index,
                                               max_stale_days=10)
            series = series / fx_aligned
        out[etf] = series

    df = pd.DataFrame(out).sort_index()
    df.attrs["missing_fx"] = missing_fx
    return df


def deployed_series_end(anchor_date: str, live_dates: list[str]) -> str:
    """Latest session the deployed series reaches: the last live point, or the
    anchor itself when the splice is empty.

    An empty splice is a legitimate state, not a missing field. A local
    refresh that re-anchors on the latest completed session leaves nothing to
    extend, and check_capture_integrity judges that case on exactly this
    effective end. Data Health reads the field written here so the same state
    reports the anchor's date rather than "field renamed or no longer
    written", which is what the published page said after every local refresh
    from 2026-09-26 to 2026-10-01 (badge STALE, row BROKEN, data current).
    ISO dates sort chronologically, so max() is the later date.
    """
    return max([anchor_date, *live_dates])


def _publication_retreat(previous: dict, candidate: dict) -> str | None:
    """Refuse a shorter observed series without copying old marks into new NAV.

    A later anchor may absorb the previous live extension. Each sleeve is
    checked independently so a current deployed blend cannot hide its retreat.
    """
    pairs = [("deployed", previous, candidate, "live_dates")]
    for key, old in previous.get("sleeve_extensions", {}).items():
        pairs.append((key, old, candidate.get("sleeve_extensions", {}).get(key, {}), "dates"))
    for label, old, new, dates_key in pairs:
        old_anchor = old.get("anchor_date", "")
        new_anchor = new.get("anchor_date", "")
        old_end = deployed_series_end(old_anchor, old.get(dates_key, []))
        new_end = deployed_series_end(new_anchor, new.get(dates_key, []))
        if new_anchor < old_anchor or new_end < old_end:
            return f"{label} would retreat from {old_anchor}/{old_end} to {new_anchor}/{new_end}"
    return None


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default=str(DATA_DIR / "live_track.json"))
    args = p.parse_args()

    print("=== Daily mark-to-market overlay ===")
    print(f"Now (UTC): {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

    # Load all sleeve JSONs + risk overlay
    sleeves = {
        "a": json.loads((DATA_DIR / "topk_robustness.json").read_text(encoding="utf-8")),
        "b": json.loads((DATA_DIR / "asset_class_rotation.json").read_text(encoding="utf-8")),
        "c": json.loads((DATA_DIR / "thematic_rotation.json").read_text(encoding="utf-8")),
        "d": json.loads((DATA_DIR / "europe_rotation.json").read_text(encoding="utf-8")),
    }
    overlay = json.loads((DATA_DIR / "risk_overlay.json").read_text(encoding="utf-8"))
    blend = overlay["gated_variants"][DEPLOYED_KEY]
    anchor_date = blend["dates"][-1]
    anchor_equity = blend["equity"][-1]
    print(f"Deployed blend anchor: {anchor_date}  equity = {anchor_equity:.6f}")

    # Point-in-time AND freshness-aware (2026-07-29). Reading current_state
    # here published a 10% EEM leg for three weeks off a feed frozen at
    # 2026-07-06 while the blend itself ran untilted; overlay_state mirrors
    # the money path, so a stalled feed marks the book untilted and says so.
    tilt = tilt_display_state(overlay, anchor_date)
    p22_active = tilt["active"]
    regime_state = overlay.get("current_state", "RISK_ON")
    print(f"Regime: {regime_state} | EEM tilt: {tilt['label']}")
    if tilt["stale"]:
        print(f"  WARN: EEM/SPY tilt feed stale since "
              f"{tilt['signal_as_of']} — marking the book UNTILTED "
              f"(baseline sleeve B at 0.35), matching the blend.",
              file=sys.stderr)

    registry = _load_registry()

    # --- Deployed-blend effective weights (with EEM tilt + regime gate)
    deployed_weights = _build_effective_weights(sleeves, p22_active, regime_state)
    wt_sum = sum(deployed_weights.values())
    print(f"\nDeployed-blend effective NAV weights "
          f"({len(deployed_weights)} positions, sum={wt_sum:.4f}):")
    for etf, w in sorted(deployed_weights.items(), key=lambda x: -x[1]):
        print(f"  {etf:<12} {w * 100:6.2f}%")
    if abs(wt_sum - 1.0) > 0.01:
        print(f"  WARNING: weights do not sum to 1.0 (sum={wt_sum:.4f})")

    # --- Per-sleeve within-sleeve weights (no NAV scaling)
    # We extend each sleeve's own equity curve as a separate mark-to-
    # market so the Performance chart's per-sleeve lines (A/B/C/D)
    # extend through the live week alongside the deployed-blend line.
    multi = json.loads((DATA_DIR / "multi_strategy.json").read_text(encoding="utf-8"))
    sleeve_anchors: dict[str, dict] = {}
    for k, sleeve in sleeves.items():
        ms_key = f"strategy_{k}"
        ms_entry = multi.get("strategies", {}).get(ms_key)
        if not ms_entry or not ms_entry.get("dates") or not ms_entry.get("equity"):
            print(f"  WARN: multi.strategies.{ms_key} missing — sleeve "
                  f"{k.upper()} will not extend on the chart")
            continue
        sleeve_anchors[k] = {
            "ms_key": ms_key,
            "anchor_date": ms_entry["dates"][-1],
            "anchor_equity": ms_entry["equity"][-1],
            "weights": _sleeve_holdings_as_weights(sleeve),
        }
        print(f"  {k.upper()} anchor: {sleeve_anchors[k]['anchor_date']}  "
              f"equity={sleeve_anchors[k]['anchor_equity']:.4f}  "
              f"holdings={list(sleeve_anchors[k]['weights'].keys())}")

    # --- Single yfinance fetch covering the union of all tickers we need
    # (deployed + every sleeve). Saves us five round-trips.
    union_weights: dict[str, float] = dict(deployed_weights)
    for sa in sleeve_anchors.values():
        for etf in sa["weights"]:
            union_weights.setdefault(etf, 0.0)
    fetch_anchor = min([anchor_date, *(sa["anchor_date"] for sa in sleeve_anchors.values())])
    prices = _fetch_usd_prices(union_weights, fetch_anchor, registry)
    if prices.empty:
        print("\nERROR: no price data downloaded. Cannot compute mark-to-market.",
              file=sys.stderr)
        return 1

    # --- NYSE anchor cap for the deployed series' as-of.
    # This is a USD-NYSE-anchored product: the deployed NAV must not carry
    # a date past the last completed NYSE session. On a US-holiday Friday
    # only the Europe sleeve trades, so yfinance supplies e.g. a 2026-07-03
    # bar while the last US close was 07-02; without this cap the factsheet
    # dates to 07-03 (against the cadence rule) and the deployed-site
    # sentinel false-alarms. On a normal weekday the last session IS today's
    # close, so the cap drops nothing.
    from nyse_sessions import (  # scripts/ is on sys.path
        last_completed_session, sessions_between)
    session_cap = pd.Timestamp(last_completed_session(datetime.now(timezone.utc)))
    late = [d.strftime("%Y-%m-%d") for d in prices.index[prices.index > session_cap]]
    if late:
        print(f"\nNYSE anchor cap: last completed session {session_cap.date()}; "
              f"dropping {len(late)} later price row(s) {late} from the deployed "
              f"extension (e.g. Europe-only holiday bar).")
    # True NYSE sessions across the whole fetch window. The cap above only
    # trims the TAIL — an interior Europe-only bar (US-holiday Friday or
    # Monday, Xetra open) passed the cap on the next run and re-entered the
    # splice (published 2026-06-22 and 2026-07-06 builds). Every projection
    # filters through this set so the extension can never carry a date the
    # deployed calendar rejects.
    valid_sessions = sessions_between(prices.index[0].date(),
                                       session_cap.date())

    # --- Deployed-blend projection
    anchor_ts = pd.Timestamp(anchor_date)
    daily_dates, daily_equity = _project_daily_equity(
        deployed_weights, anchor_equity, prices, anchor_ts,
        session_cap=session_cap, valid_sessions=valid_sessions)
    if daily_dates:
        print(f"\nDeployed-blend extension ({len(daily_dates)} point(s)):")
        for d, e in zip(daily_dates, daily_equity):
            pct = (e / anchor_equity - 1) * 100
            print(f"  {d}: equity {e:.6f} ({pct:+.3f}% vs anchor)")
    else:
        print("\nNo new deployed-blend points beyond anchor.")

    # --- Per-sleeve projections (each anchored at its own multi.strategies
    # last-date, so the Performance chart lines line up)
    sleeve_extensions: dict[str, dict] = {}
    for k, sa in sleeve_anchors.items():
        s_anchor_ts = pd.Timestamp(sa["anchor_date"])
        s_dates, s_equity = _project_daily_equity(
            sa["weights"], sa["anchor_equity"], prices, s_anchor_ts,
            session_cap=session_cap, valid_sessions=valid_sessions)
        sleeve_extensions[sa["ms_key"]] = {
            "anchor_date": sa["anchor_date"],
            "anchor_equity": round(sa["anchor_equity"], 6),
            "weights": {e: round(w, 6) for e, w in sa["weights"].items()},
            "dates": s_dates,
            "equity": [round(v, 6) for v in s_equity],
        }
        if s_dates:
            last_e = s_equity[-1]
            pct = (last_e / sa["anchor_equity"] - 1) * 100
            print(f"  {k.upper()}: +{len(s_dates)} pts, "
                  f"last {s_dates[-1]} = {last_e:.4f} ({pct:+.3f}% vs anchor)")

    payload = {
        "computed_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "deployed_key": DEPLOYED_KEY,
        "anchor_date": anchor_date,
        "anchor_equity": anchor_equity,
        "regime_state": regime_state,
        "eem_tilt_active": p22_active,
        # Carried so every downstream surface (dashboard, digest, factsheet)
        # can badge a stalled tilt feed instead of inferring it is live.
        "eem_tilt_signal_as_of": tilt["signal_as_of"],
        "eem_tilt_signal_stale": tilt["stale"],
        "effective_weights": {k: round(v, 6) for k, v in
                                sorted(deployed_weights.items(),
                                        key=lambda x: -x[1])},
        # Backwards-compatible flat fields used by older pipeline.py:
        "live_dates": daily_dates,
        "live_equity": [round(v, 6) for v in daily_equity],
        # The latest session the deployed series reaches (the anchor when the
        # splice is empty). Data Health's observation selector for this file.
        "deployed_series_end": deployed_series_end(anchor_date, daily_dates),
        # New per-sleeve block for the Performance chart's sleeve lines:
        "sleeve_extensions": sleeve_extensions,
        "notes": (
            "Daily mark-to-market overlay. Deployed-blend section uses "
            "the same effective NAV weights as renderPositionsPreview "
            "(EEM tilt + breadth-gate aware). Per-sleeve extensions "
            "(strategy_a/b/c/d) use within-sleeve weights anchored at "
            "each sleeve's multi.strategies last-date, so the "
            "Performance chart lines all advance through the live week."
        ),
    }

    out_path = Path(args.out)
    if out_path.exists():
        previous = json.loads(out_path.read_text(encoding="utf-8"))
        retreat = _publication_retreat(previous, payload)
        if retreat:
            print(f"\nERROR: {retreat}; existing live track retained.", file=sys.stderr)
            return 1
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nWrote {out_path.relative_to(PROJECT_ROOT)} "
          f"({out_path.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
