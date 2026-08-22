"""Strategy D — Europe sector top-K-by-breadth rotation.

Same constituent-breadth engine as Strategy A, but on 5 Stoxx Europe 600
sector UCITS funds (Banks, Oil & Gas, Technology, Industrials, Utilities).

After the 2026-05-24 compute_ma200_breadth fix (allowing 10% sparse
missingness in the rolling window, required because non-US constituents
have ~1-2% missing days that would otherwise nuke the MA200), the Phase 4
experiment showed adding this as a separate 20% sleeve to the 45/45/10
A:B:C baseline improves Sharpe from +1.082 to +1.152 (CAGR +14.9% to
+15.1%) at the cost of ~2.3pp higher max drawdown (-21.5% to -23.8%).
The Sharpe improvement is meaningful at the 20% sleeve weight; the DD
cost is the honest price of adding equity beta.

Standalone Strategy D (common window 2018-11 → 2026-05): Sharpe +0.93,
CAGR +14.9%, DD -32.0% — better Sharpe than SPY's +0.77 on the same
window. Europe's distinct macro cycle (ECB rates, EUR/USD, China
trade exposure) provides genuine orthogonality at the blend level.

Output: data/europe_rotation.json (mirrors topk_robustness.json structure
so the dashboard renderer can reuse Strategy A's code paths).
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
OUT_PATH = DATA_DIR / "europe_rotation.json"

sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

from etf_registry import UNIVERSE_EUROPE_SECTORS  # noqa: E402
from run_portfolio import _build_panels_for, run_portfolio, top_k_breadth_weight  # noqa: E402
from run_improvements import compute_stats  # noqa: E402
from run_ma200_sweep import MA_PERIOD  # noqa: E402
from backtest import download_spy_close  # noqa: E402
from price_panel_guard import (  # noqa: E402
    assert_attribution_sane, assert_panel_usable,
)

# Phase 12 cost calibration: Strategy D trades 5 Stoxx Europe 600 sector
# UCITS on Xetra in EUR (EXV1.DE banks, EXH1.DE oil & gas, EXV3.DE tech,
# EXH4.DE industrial goods & services, EXH9.DE utilities). European UCITS
# bid-ask is
# typically 5-10 bps, plus an extra 2-4 bps FX cost when the investor's
# base currency is USD (which is the most common Navigo client base).
# Realistic blended one-way cost: ~9 bps (was uniform 5 — too tight for
# European sector UCITS including FX).
COST_BPS = 9
COST_FRAC = COST_BPS / 10_000
# Strategy D trades Xetra, NOT the NYSE calendar the other sleeves use.
CALENDAR = "XETR"

K_GRID = [2, 3, 4]
# WS18 (2026-08-22): "Weekly Mon" is the DEPLOYED cell and must be present,
# because main() captures headline_payload by matching HEADLINE_FREQ_NAME
# against this grid. Changing the constant without adding the cell left the
# payload None and every engine died on it — loudly, which was the right
# failure, but the grid is the other half of the same decision.
# "Weekly Fri" is KEPT as a comparison rather than replaced: after a cadence
# move the incumbent is the single most useful row in this table.
REBAL_FREQS = [
    ("Daily",         "D"),
    ("Weekly Mon",    "W-MON"),
    ("Weekly Fri",    "W-FRI"),
    ("Bi-weekly Fri", "2W-FRI"),
    ("Month-end",     "BME"),
]
HEADLINE_K = 3
HEADLINE_FREQ_NAME = "Weekly Mon"
# WS18 (2026-08-22): the whole book moved to a Monday rebalance so
# every sleeve ranks at rd-1. Under the Friday cadence sleeve D could
# only reach rd-2 - the European data is a session late at every hour
# of the decision window - so the live book could not implement what
# this engine backtests, for 20% of NAV, weekly.
HEADLINE_FREQ = "W-MON"


def _safe(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(f) or math.isinf(f)) else f


def round_series(values, ndigits=4):
    out = []
    for v in values:
        try:
            f = float(v)
            out.append(round(f, ndigits) if not (math.isnan(f) or math.isinf(f)) else None)
        except (TypeError, ValueError):
            out.append(None)
    return out


def turnover_stats(weight_panel: pd.DataFrame, eligible_start: pd.Timestamp) -> dict:
    wp = weight_panel.loc[weight_panel.index >= eligible_start].copy()
    diff = wp.diff().abs().sum(axis=1).fillna(0)
    n_years = (wp.index[-1] - wp.index[0]).days / 365.25
    return {
        "annual_turnover": float(diff.sum() / n_years) if n_years > 0 else 0.0,
        "n_flips": int((diff > 1e-6).sum()),
    }


def build_trade_history(weight_panel: pd.DataFrame, breadth_panel: pd.DataFrame,
                          eligible: pd.Timestamp) -> list[dict]:
    """Per-rebalance holdings, records prior-trading-day breadth (decision-time
    value, so share-math reproduces weights exactly)."""
    wp = weight_panel.loc[weight_panel.index >= eligible].copy()
    bp = breadth_panel.reindex(wp.index, method="ffill")
    full_idx = list(wp.index)
    out: list[dict] = []
    prev: pd.Series | None = None
    for i, (dt, row) in enumerate(wp.iterrows()):
        if prev is None or not np.allclose(row.values, prev.values, atol=1e-6):
            non_zero = row[row > 1e-6].sort_values(ascending=False)
            if len(non_zero) == 0:
                prev = row
                continue
            decision_date = full_idx[i - 1] if i > 0 else full_idx[i]
            holdings = []
            for etf, w in non_zero.items():
                b_val = bp.loc[decision_date, etf] if etf in bp.columns else None
                holdings.append({
                    "etf": etf,
                    "weight": round(float(w), 4),
                    "breadth_pct": round(float(b_val) * 100, 1) if b_val == b_val else None,
                })
  # decision_date is the session this rebalance actually RANKED on.
            # All four engines computed it and threw it away, so a
            # rebalance could not say which session decided it. On
            # 2026-08-14 a vendor hole at Thu 13 Aug in the .DE lines
            # moved Strategy D's decision to Wed 12 Aug and flipped
            # EXH3/EXV3 on a 1.3pp margin, invisibly. Recorded now so a
            # stale or divergent decision session is readable, not
            # inferred.
            out.append({"date": dt.strftime("%Y-%m-%d"),
                        "decision_date": decision_date.strftime("%Y-%m-%d"),
                        "holdings": holdings})
            prev = row
    return out


def _fx_convert_eur_to_usd(closes: pd.DataFrame) -> pd.DataFrame:
    """Phase 20.2 (2026-05-28) — convert EUR-denominated Xetra UCITS
    closes to USD. Critical fix: yfinance returns EXV1.DE / EXH1.DE
    etc in EUR. Without this conversion, Strategy D's backtest is in
    EUR and the 4-way blend mixes EUR returns with USD returns from
    A/B/C — meaningless math and an EUR/USD drift of ~12% over the
    2018-2026 window inflates apparent D returns by ~1.5pp annualised.

    Fetches USDEUR=X (yfinance convention: EUR per 1 USD; we want
    USD per 1 EUR, so 1/USDEUR=X), forward-fills onto the closes
    calendar (handles weekends + holidays), and multiplies each EUR
    price by the contemporary USD/EUR rate.
    """
    import yfinance as yf
    print("  Fetching EUR/USD FX series for USD conversion ...", flush=True)
    fx_raw = yf.download("EURUSD=X",
                          start=closes.index.min().strftime("%Y-%m-%d"),
                          end=(closes.index.max() + pd.Timedelta(days=5))
                              .strftime("%Y-%m-%d"),
                          auto_adjust=True, progress=False, threads=False)
    if isinstance(fx_raw.columns, pd.MultiIndex):
        fx_raw.columns = fx_raw.columns.get_level_values(0)
    fx = fx_raw["Close"]
    if isinstance(fx, pd.DataFrame):
        fx = fx.iloc[:, 0]
    fx.index = pd.to_datetime(fx.index).tz_localize(None)
    # WS3 maintenance patch (defect D4): cap the forward-fill at 10 calendar
    # days — the same one-liner Sleeve C uses (run_thematic_rotation.py) — so
    # a stalled EURUSD feed degrades to NaN rather than silently freezing the
    # last rate. Replaces the previous uncapped
    # `.reindex(method="ffill").bfill()`; EURUSD and Xetra both trade
    # weekdays from the same fetch start, so no leading gap to back-fill.
    from alignment import align_series_to_index  # noqa: E402
    fx = align_series_to_index(fx, closes.index, max_stale_days=10)
    print(f"  EUR/USD range over window: {fx.min():.4f} - {fx.max():.4f}  "
          f"(today {fx.iloc[-1]:.4f})")
    # Multiply EUR price by USD/EUR rate (= EURUSD=X) -> USD price
    return closes.multiply(fx, axis=0)


def main() -> int:
    print(f"Building Europe sector breadth panels for {len(UNIVERSE_EUROPE_SECTORS)} ETFs ...",
          flush=True)
    closes_eur, breadths, etfs_used = _build_panels_for(UNIVERSE_EUROPE_SECTORS)
    print(f"  {len(etfs_used)} ETFs used: {etfs_used}")
    if not etfs_used:
        print("ERROR: no usable ETFs in Europe universe")
        return 1

    # CRITICAL: Xetra UCITS prices are EUR-denominated. Convert to USD
    # so D's returns can be honestly blended with USD-native A/B/C.
    closes = _fx_convert_eur_to_usd(closes_eur)

    # Eligible start = latest first-valid date + MA period
    starts = []
    for etf in etfs_used:
        b = breadths[etf].dropna()
        if len(b):
            starts.append(b.index.min())
    eligible = max(starts)
    eligible = pd.Timestamp(eligible.date()) + pd.Timedelta(days=MA_PERIOD)
    eligible = (closes.index[closes.index >= eligible][0]
                if (closes.index >= eligible).any() else closes.index[MA_PERIOD])
    print(f"  Eligible start: {eligible.date()}")

    # Judged on the USD panel, which is what the engine ranks and marks on.
    # Checking the EUR side would pass a member whose FX leg holed the series
    # after conversion. See the 2026-08-15 note in price_panel_guard.py.
    assert_panel_usable(closes, "Strategy D closes (USD)", window_start=eligible)

    # K x cadence grid
    print(f"\n=== K × cadence sensitivity (Strategy D: Europe sectors) ===")
    grid: dict[str, dict[str, dict]] = {}
    headline_payload: dict | None = None
    for K in K_GRID:
        grid[f"K={K}"] = {}
        print(f"\n  --- K = {K} ---")
        for freq_name, freq_code in REBAL_FREQS:
            r = run_portfolio(closes, breadths, top_k_breadth_weight(K),
                              eligible, rebalance_freq=freq_code,
                              cost=COST_FRAC, calendar=CALENDAR)
            eq_window = r["equity"].loc[r["equity"].index >= eligible]
            if len(eq_window) > 0:
                eq_window = eq_window / eq_window.iloc[0]
            st = compute_stats(r["equity"], eligible)
            to = turnover_stats(r["weights"], eligible)
            grid[f"K={K}"][freq_name] = {
                "sharpe": _safe(st["sharpe"]),
                "cagr": _safe(st.get("cagr")),
                "total_return": _safe(st["total_return"]),
                "max_dd": _safe(st["max_dd"]),
                "annual_turnover": _safe(to["annual_turnover"]),
                "n_flips": int(to["n_flips"]),
            }
            print(f"    {freq_name:<14}  Sharpe {st['sharpe']:+.2f}   "
                  f"totRet {st['total_return']*100:+5.0f}%   "
                  f"DD {st['max_dd']*100:>4.1f}%   "
                  f"turnover/yr {to['annual_turnover']:>4.2f}")
            if K == HEADLINE_K and freq_name == HEADLINE_FREQ_NAME:
                trades = build_trade_history(r["weights"], breadths, eligible)

                rets = closes.pct_change().fillna(0).loc[r["weights"].index]
                rets = rets.loc[rets.index >= eligible]
                used_w = r["weights"].loc[rets.index].shift(1).fillna(0)
                daily_contrib = used_w * rets
                total_contrib = daily_contrib.sum()
                total_all = float(total_contrib.sum())
                attribution = {}
                for etf in closes.columns:
                    if etf not in daily_contrib.columns:
                        continue
                    held_mask = used_w[etf] > 1e-6
                    n_held = int(held_mask.sum())
                    total_days = len(used_w)
                    if n_held == 0:
                        ann_ret, avg_w = None, 0.0
                    else:
                        mean_daily = float(rets.loc[held_mask, etf].mean())
                        ann_ret = (1.0 + mean_daily) ** 252 - 1.0
                        avg_w = float(used_w[etf][held_mask].mean())
                    pnl = float(total_contrib.get(etf, 0.0))
                    attribution[etf] = {
                        "days_held": n_held,
                        "pct_of_days": round(n_held / total_days * 100, 1)
                                          if total_days else 0.0,
                        "avg_weight_when_held": round(avg_w, 4),
                        "ann_return_when_held": _safe(ann_ret),
                        "contribution_to_total_return": _safe(pnl),
                        "pct_of_total_contribution": (
                            round(pnl / total_all * 100, 1)
                            if total_all != 0 else 0.0
                        ),
                    }

                # Last gate before anything is written — a large days_held
                # beside an exactly zero return is a price-cache fault, never
                # a market outcome.
                assert_attribution_sane(attribution,
                                        "Strategy D attribution")

                # Sample at the ACTUAL rebalance grid, not every Friday: under
                # a holiday-aware cadence a decision can land on a Thursday,
                # and a dayofweek filter would silently drop it.
                weekly_idx = r["rebalance_dates"]
                weekly_w = r["weights"].loc[weekly_idx]
                weekly_w = weekly_w.loc[(weekly_w.sum(axis=1) > 0.5)]

                headline_payload = {
                    "K": K,
                    "rebal_freq": freq_name,
                    "rebal_freq_code": freq_code,
                    "n_etfs": len(etfs_used),
                    "etfs_used": etfs_used,
                    "eligible_start": eligible.strftime("%Y-%m-%d"),
                    "headline_stats": grid[f"K={K}"][freq_name],
                    "headline_equity_dates": [d.strftime("%Y-%m-%d")
                                                for d in eq_window.index],
                    "headline_equity": round_series(eq_window.values),
                    "n_rebalances": len(trades),
                    "trade_history": trades,
                    "attribution": attribution,
                    "weekly_allocation_dates": [d.strftime("%Y-%m-%d")
                                                  for d in weekly_w.index],
                    "weekly_allocation": {
                        etf: round_series(weekly_w[etf].values)
                        for etf in weekly_w.columns
                    },
                }

    # ---------- Walk-forward K refit (Phase 7) ----------
    # Annual refit picking K from K_GRID on expanding-window train Sharpe,
    # applying that K to the next 12 months of test data. Concatenate the
    # test segments to get the realistic OOS Sharpe.
    print("\n=== Walk-forward K refit (annual, K in {2, 3, 4}) ===")
    wf_K_grid = K_GRID  # [2, 3, 4]
    initial_train_end = closes.index[closes.index >= eligible + pd.Timedelta(days=730)][0]
    last_date = closes.index[-1]
    refit_ends = pd.date_range(initial_train_end, last_date, freq="YE")
    refit_ends = [closes.index[closes.index.searchsorted(r, side="right") - 1]
                   for r in refit_ends]
    refit_ends = [r for r in refit_ends if r >= eligible]

    def _wf_sharpe(eq_series, start, end):
        eq = eq_series.loc[(eq_series.index >= start) & (eq_series.index <= end)]
        if len(eq) < 5:
            return float("nan")
        eq = eq / float(eq.iloc[0])
        daily = eq.pct_change().fillna(0)
        if daily.std() == 0:
            return 0.0
        return float(daily.mean() / daily.std() * math.sqrt(252))

    def _portfolio_equity(K):
        r = run_portfolio(closes, breadths, top_k_breadth_weight(K),
                          eligible, rebalance_freq=HEADLINE_FREQ,
                          cost=COST_FRAC, calendar=CALENDAR)
        return r["equity"]

    wf_segments = []
    wf_test_pieces = []
    K_sequence = []
    for i, train_end in enumerate(refit_ends):
        train_end_idx = closes.index.get_loc(train_end)
        test_end = refit_ends[i + 1] if i + 1 < len(refit_ends) else last_date
        test_start_idx = train_end_idx + 1
        if test_start_idx >= len(closes):
            break
        test_start = closes.index[test_start_idx]
        if test_start > test_end:
            continue
        best_K, best_sh = None, -1e9
        for K in wf_K_grid:
            full_eq = _portfolio_equity(K)
            sh = _wf_sharpe(full_eq, eligible, train_end)
            if not np.isnan(sh) and sh > best_sh:
                best_sh, best_K = sh, K
        if best_K is None:
            continue
        K_sequence.append(best_K)
        full_eq = _portfolio_equity(best_K)
        test_eq = full_eq.loc[test_start:test_end]
        base_val = float(full_eq.iloc[test_start_idx - 1]) if test_start_idx > 0 else 1.0
        test_eq = test_eq / base_val
        test_sh = _wf_sharpe(test_eq, test_start, test_end)
        wf_segments.append({
            "train_end": train_end.strftime("%Y-%m-%d"),
            "test_start": test_start.strftime("%Y-%m-%d"),
            "test_end": test_end.strftime("%Y-%m-%d"),
            "best_K": best_K,
            "train_sharpe": _safe(best_sh),
            "test_sharpe": _safe(test_sh),
            "n_test_days": int(len(test_eq)),
        })
        last_val = wf_test_pieces[-1].iloc[-1] if wf_test_pieces else 1.0
        wf_test_pieces.append(test_eq * last_val / test_eq.iloc[0])

    if wf_test_pieces:
        wf_equity = pd.concat(wf_test_pieces)
        wf_daily = wf_equity.pct_change().fillna(0)
        wf_sh = (wf_daily.mean() / wf_daily.std() * math.sqrt(252)
                  if wf_daily.std() > 0 else 0.0)
        print(f"  Walk-forward Sharpe: {wf_sh:+.3f}  "
              f"(in-sample K={HEADLINE_K}: {headline_payload['headline_stats']['sharpe']:+.3f})")
        print(f"  K sequence:          {K_sequence}")
        print(f"  Segments:            {len(wf_segments)} ({wf_segments[0]['test_start']} -> {wf_segments[-1]['test_end']})")
        walk_forward = {
            "walk_forward_sharpe": _safe(wf_sh),
            "K_grid": wf_K_grid,
            # Persisted 2026-08-03. The per-segment K choices were printed but
            # never written, so when the 2026-08-03 EXH3 correction re-ran the
            # sleeve and every segment chose K=2 against the deployed K=3,
            # there was no way to tell from the artefact whether that
            # preference predated the fix. Storing it makes the next such
            # question answerable from the file instead of from a lost stdout.
            "K_sequence": K_sequence,
            "headline_K": HEADLINE_K,
            "initial_train_end": initial_train_end.strftime("%Y-%m-%d"),
            "segments": wf_segments,
        }
    else:
        print("  Walk-forward: insufficient data, skipped")
        walk_forward = None

    print("\n=== Benchmarks (Europe sleeve vs SPY + VGK) ===")
    spy_close = download_spy_close(closes.index.min().strftime("%Y-%m-%d"),
                                    (closes.index.max() + pd.Timedelta(days=5)).strftime("%Y-%m-%d"))
    spy_close = spy_close.reindex(closes.index).ffill()
    spy_window = spy_close.loc[spy_close.index >= eligible]
    spy_eq = (spy_window / spy_window.iloc[0])
    spy_stats = compute_stats(spy_close, eligible)
    print(f"  SPY                Sharpe {spy_stats['sharpe']:+.2f}   "
          f"totRet {spy_stats['total_return']*100:+.0f}%   DD {spy_stats['max_dd']*100:.1f}%")

    # Phase 27.6 — add VGK (Vanguard FTSE Europe ETF, USD-denominated)
    # as a Europe broad-market benchmark. The Strategy D sleeve trades
    # Stoxx Europe 600 sector slices; VGK is the canonical liquid USD
    # proxy for the underlying Europe-broad universe (FTSE Developed
    # Europe — same ~85% of European market cap as Stoxx 600). Adding
    # it here so the dashboard can show "rotation alpha vs broad
    # Europe passive" alongside the existing SPY (US passive) line.
    import yfinance as yf
    vgk_cache = DATA_DIR / "europe_vgk_cache.parquet"
    try:
        if vgk_cache.exists():
            cached = pd.read_parquet(vgk_cache)
            need_refresh = (cached.index.max() < closes.index.max() - pd.Timedelta(days=3))
        else:
            need_refresh = True
        if need_refresh:
            raw = yf.download("VGK", start=closes.index.min(),
                                end=(closes.index.max() + pd.Timedelta(days=5)),
                                auto_adjust=True, progress=False, threads=False)
            if isinstance(raw.columns, pd.MultiIndex):
                raw.columns = raw.columns.get_level_values(0)
            vgk_close = raw["Close"].copy()
            vgk_close.index = pd.to_datetime(vgk_close.index).tz_localize(None)
            vgk_close.to_frame("Close").to_parquet(vgk_cache)
        else:
            vgk_close = cached["Close"]
        vgk_close = vgk_close.reindex(closes.index).ffill()
        vgk_window = vgk_close.loc[vgk_close.index >= eligible]
        vgk_eq = (vgk_window / vgk_window.iloc[0])
        vgk_stats = compute_stats(vgk_close, eligible)
        print(f"  VGK (Europe broad) Sharpe {vgk_stats['sharpe']:+.2f}   "
              f"totRet {vgk_stats['total_return']*100:+.0f}%   DD {vgk_stats['max_dd']*100:.1f}%")
        vgk_benchmark = {
            "label": "VGK buy-and-hold (Europe broad)",
            "dates": [d.strftime("%Y-%m-%d") for d in vgk_eq.index],
            "equity": round_series(vgk_eq.values),
            "sharpe": _safe(vgk_stats["sharpe"]),
            "total_return": _safe(vgk_stats["total_return"]),
            "max_dd": _safe(vgk_stats["max_dd"]),
            "cagr": _safe(vgk_stats.get("cagr")),
        }
    except Exception as e:
        print(f"  WARN: VGK benchmark fetch failed ({e}); dashboard will only show SPY")
        vgk_benchmark = None

    benchmarks = {
        "spy_buy_hold": {
            "label": "SPY buy-and-hold",
            "dates": [d.strftime("%Y-%m-%d") for d in spy_eq.index],
            "equity": round_series(spy_eq.values),
            "sharpe": _safe(spy_stats["sharpe"]),
            "total_return": _safe(spy_stats["total_return"]),
            "max_dd": _safe(spy_stats["max_dd"]),
            "cagr": _safe(spy_stats.get("cagr")),
        },
    }
    if vgk_benchmark is not None:
        benchmarks["vgk_europe_broad"] = vgk_benchmark

    # Per-ETF colour palette (consistent with Strategy A)
    europe_colours = {
        "EXV1": "#1351b4",  # blue (Banks)
        "EXH1": "#b76e00",  # amber (Oil & Gas)
        "EXV3": "#7c3aed",  # purple (Technology)
        "EXH3": "#a16207",  # bronze (Industrials)
        "EXH9": "#0e7490",  # teal (Utilities)
    }

    # Per-ETF breadth time series — surfaced for the ETF Detail tab so
    # the dashboard can render each Europe sector ETF's % above 200d MA
    # over time (Section 3 of ETF Detail). Same metric as Strategy A's
    # per_etf_detail, just for the Europe universe.
    # Trim to eligible window onwards (signal is 0 before MA200 warm-up).
    per_etf_breadth = {}
    for etf in etfs_used:
        series = breadths[etf].loc[breadths[etf].index >= eligible].dropna()
        if len(series) == 0:
            continue
        per_etf_breadth[etf] = {
            "label": _etf_label(etf),
            "sector": _etf_sector(etf),
            "dates": [d.strftime("%Y-%m-%d") for d in series.index],
            # Convert to percentage (0-100) for display, round to 1 dp
            "breadth_pct": [round(float(v) * 100, 1) for v in series.values],
        }

    payload = {
        "computed_at_utc": datetime.now(timezone.utc).isoformat(),
        "universe": [
            {"etf": t, "label": _etf_label(t), "sector": _etf_sector(t)}
            for t in etfs_used
        ],
        "ma_period": MA_PERIOD,
        "cost_bps": COST_BPS,  # Phase 12 per-strategy cost calibration
        "rebalance_freq_grid": grid,
        "headline": headline_payload,
        "walk_forward": walk_forward,
        "benchmarks": benchmarks,
        "europe_colours": europe_colours,
        "per_etf_breadth": per_etf_breadth,
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nWrote {OUT_PATH.relative_to(PROJECT_ROOT)}")

    print()
    print("=" * 90)
    print(f"STRATEGY D EUROPE HEADLINE — K={HEADLINE_K}, {HEADLINE_FREQ_NAME}")
    print("=" * 90)
    s = headline_payload["headline_stats"]
    print(f"  Sharpe          : {s['sharpe']:+.2f}")
    print(f"  CAGR            : {s['cagr']*100:+.1f}%")
    print(f"  Total return    : {s['total_return']*100:+.1f}%")
    print(f"  Max drawdown    : {s['max_dd']*100:.1f}%")
    print(f"  Annual turnover : {s['annual_turnover']:.2f}")
    return 0


def _etf_label(t):
    return {
        "EXV1": "STOXX Europe 600 Banks",
        "EXH1": "STOXX Europe 600 Oil & Gas",
        "EXV3": "STOXX Europe 600 Technology",
        "EXH3": "STOXX Europe 600 Industrial Goods & Services",
        "EXH9": "STOXX Europe 600 Utilities",
    }.get(t, t)


def _etf_sector(t):
    return {
        "EXV1": "Banks", "EXH1": "Energy", "EXV3": "Technology",
        "EXH3": "Industrials", "EXH9": "Utilities",
    }.get(t, "")


if __name__ == "__main__":
    sys.exit(main())
