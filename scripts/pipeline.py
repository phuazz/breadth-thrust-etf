"""Dashboard pipeline — inject result JSONs into template.html, write docs/index.html.

Phase 2 dashboard (tabbed):
  - Overview      : verdict + cross-ETF matrix + per-ETF best-result cards
  - Equity Curves : per-ETF equity for the 3 standard configs vs ETF & SPY buy-and-hold
  - Breadth       : per-ETF composite z-score + breadth components + signal-fire markers
  - Trade Detail  : per-trade sortable table + exit-reason pie per ETF×config
  - Sensitivity   : SOXX exit-logic, entry-delay, trend-filter variant tables, plus split-half

Inputs (loaded automatically):
  - data/backtest_soxx.json                    (SOXX baseline + equity curves)
  - data/backtest_<etf>_oos.json  for each of  CSP1, IUES, IUFS, CNDX
  - data/breadth_<etf>.json       for each of  SOXX, CSP1, IUES, IUFS, CNDX
  - data/backtest_variants_soxx.json
  - data/sensitivity_entry_delay_soxx.json
  - data/sensitivity_trend_filter_soxx.json
  - data/oos_split_half_soxx.json

Output:
  - docs/index.html  (GitHub Pages root)

Per CLAUDE.md:
  - White theme, sans-serif, high contrast.
  - template.html stays under 200 KB. The built docs/index.html may exceed
    500 KB once all the time-series data are inlined; that is the expected
    pattern (NEVER re-read it into Claude context — only write).

Run:
    python scripts/pipeline.py
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
TEMPLATE = PROJECT_ROOT / "template.html"
DOCS = PROJECT_ROOT / "docs"
OUT = DOCS / "index.html"

ETFS = ["SOXX", "CSP1", "CNDX", "IUES", "IUFS"]

ETF_DESC = {
    "SOXX": "iShares Semiconductor (semis)",
    "IUES": "S&P 500 Energy sector",
    "IUFS": "S&P 500 Financials sector",
    "CNDX": "NASDAQ-100",
    "CSP1": "S&P 500 (full)",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(f) or math.isinf(f)) else f


def round_series(values, ndigits=4):
    out = []
    for v in values:
        if v is None:
            out.append(None); continue
        try:
            f = float(v)
            out.append(round(f, ndigits) if not (math.isnan(f) or math.isinf(f)) else None)
        except (TypeError, ValueError):
            out.append(None)
    return out


def _row_from_summary(label: str, summary: dict) -> dict:
    return {
        "sharpe": _safe(summary.get("sharpe_annualised")),
        "total_return": _safe(summary.get("equity_curve_total_return")),
        "max_dd": _safe(summary.get("equity_curve_max_dd")),
        "win_rate": _safe(summary.get("win_rate")),
        "mc_pct": _safe(summary.get("mc_strategy_total_return_percentile")),
        "n_trades": summary.get("n_trades"),
        "median_holding_days": _safe(summary.get("median_holding_days")),
    }


# ---------------------------------------------------------------------------
# Cross-ETF result matrix (Overview)
# ---------------------------------------------------------------------------


def build_cross_etf_rows() -> list[dict]:
    """Assemble the 5-ETF cross-matrix.

    All five ETFs are read from their backtest_<etf>_oos.json file so the
    window, MC null, and reporting basis are apples-to-apples. SOXX is
    still 'in-sample' in the sense that the triple-combo config was tuned
    on it — that nuance is conveyed by the split-half OOS table in the
    Sensitivity tab.
    """
    rows: list[dict] = []
    for etf in ("SOXX", "IUES", "IUFS", "CNDX", "CSP1"):
        path = DATA_DIR / f"backtest_{etf.lower()}_oos.json"
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        configs: dict[str, dict] = {}
        for s in payload.get("summary_table", []):
            configs[s["variant"]] = _row_from_summary(s["variant"], s)
        rows.append({"etf": etf, "configs": configs})
    return rows


# ---------------------------------------------------------------------------
# Equity curves (per ETF × config)
# ---------------------------------------------------------------------------


def build_equity_curves() -> dict:
    """Per-ETF equity curves on the signal-eligible window.

    All five ETFs come from their backtest_<etf>_oos.json variants block,
    which since the Phase 2 rerun stores an equity_curve per variant.

    Structure:
      { ETF: { dates: [...], configs: { cfg: {strategy, traded_etf_buy_hold, spy_buy_hold} } } }
    """
    out: dict[str, dict] = {}
    for etf in ("SOXX", "IUES", "IUFS", "CNDX", "CSP1"):
        path = DATA_DIR / f"backtest_{etf.lower()}_oos.json"
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        configs_out: dict[str, dict] = {}
        dates_out = None
        for cfg_name, cfg_data in payload.get("variants", {}).items():
            eq = cfg_data.get("equity_curve")
            if not eq:
                continue
            if dates_out is None:
                dates_out = eq["dates"]
            configs_out[cfg_name] = {
                "strategy": round_series(eq["strategy"], 4),
                "traded_etf_buy_hold": round_series(eq["traded_etf_buy_hold"], 4),
                "spy_buy_hold": round_series(eq["spy_buy_hold"], 4),
            }
        if dates_out:
            out[etf] = {"dates": dates_out, "configs": configs_out}
    return out


# ---------------------------------------------------------------------------
# Breadth time series (per ETF) — for the Breadth Signals tab
# ---------------------------------------------------------------------------


def build_breadth_series() -> dict:
    """Extract minimal breadth fields per ETF.

    Trims to the signal-eligible window (from index `signal_eligible_after`)
    to keep size down. Pulls dates + composite_z + the three component %s
    + the dates of every signal_fires == 1 row.
    """
    out: dict[str, dict] = {}
    for etf in ETFS:
        path = DATA_DIR / f"breadth_{etf.lower()}.json"
        if not path.exists():
            continue
        blob = json.loads(path.read_text(encoding="utf-8"))
        ser = blob["series"]
        start_idx = blob["config"].get("signal_eligible_after", 252)
        dates = ser["dates"][start_idx:]
        comp_z = ser["composite_z"][start_idx:]
        ma_b = ser["ma_breadth"][start_idx:]
        rsi_b = ser["rsi_breadth"][start_idx:]
        hi_b = ser["highs_breadth"][start_idx:]
        sig = ser["signal_fires"][start_idx:]
        signal_dates = [d for d, s in zip(dates, sig) if s]
        out[etf] = {
            "dates": dates,
            "composite_z": round_series(comp_z, 3),
            "ma_breadth": round_series(ma_b, 4),
            "rsi_breadth": round_series(rsi_b, 4),
            "highs_breadth": round_series(hi_b, 4),
            "signal_dates": signal_dates,
        }
    return out


# ---------------------------------------------------------------------------
# Trades per ETF × config
# ---------------------------------------------------------------------------


def _trim_trade(t: dict) -> dict:
    """Keep only what the dashboard trade table needs."""
    return {
        "signal_date": t.get("signal_date"),
        "entry_date": t.get("entry_date"),
        "exit_date": t.get("exit_date"),
        "holding_days": t.get("holding_days"),
        "trade_return": _safe(t.get("trade_return")),
        "max_drawdown": _safe(t.get("max_drawdown")),
        "exit_reason": t.get("exit_reason"),
    }


def build_trades_by_etf() -> dict:
    """For each ETF × config, the list of trades.

    All five ETFs read from backtest_<etf>_oos.json variants block — same
    file family the cross-ETF matrix uses, so trade lists, equity curves,
    and headline cells all stay in sync.
    """
    out: dict[str, dict] = {}
    for etf in ("SOXX", "IUES", "IUFS", "CNDX", "CSP1"):
        path = DATA_DIR / f"backtest_{etf.lower()}_oos.json"
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        out[etf] = {}
        for cfg_name, cfg_data in payload.get("variants", {}).items():
            out[etf][cfg_name] = [_trim_trade(t) for t in cfg_data.get("trades", [])]
    return out


# ---------------------------------------------------------------------------
# Sensitivity tables — for the Sensitivity tab
# ---------------------------------------------------------------------------


def _summarise_for_table(s: dict) -> dict:
    return {
        "variant": s.get("variant"),
        "n_trades": s.get("n_trades"),
        "win_rate": _safe(s.get("win_rate")),
        "median_holding_days": _safe(s.get("median_holding_days")),
        "equity_curve_total_return": _safe(s.get("equity_curve_total_return")),
        "equity_curve_max_dd": _safe(s.get("equity_curve_max_dd")),
        "sharpe_annualised": _safe(s.get("sharpe_annualised")),
        "mc_strategy_total_return_percentile": _safe(s.get("mc_strategy_total_return_percentile")),
    }


def build_variants() -> dict:
    """SOXX exit-logic, entry-delay, and trend-filter sweeps + split-half."""
    out: dict[str, list[dict]] = {}
    vfile = DATA_DIR / "backtest_variants_soxx.json"
    if vfile.exists():
        v = json.loads(vfile.read_text(encoding="utf-8"))
        out["exit_logic"] = [_summarise_for_table(r) for r in v.get("summary_table", [])]
    dfile = DATA_DIR / "sensitivity_entry_delay_soxx.json"
    if dfile.exists():
        d = json.loads(dfile.read_text(encoding="utf-8"))
        out["entry_delay"] = [_summarise_for_table(r) for r in d.get("summary_table", [])]
    tfile = DATA_DIR / "sensitivity_trend_filter_soxx.json"
    if tfile.exists():
        t = json.loads(tfile.read_text(encoding="utf-8"))
        out["trend_filter"] = [_summarise_for_table(r) for r in t.get("summary_table", [])]
    return out


def build_splithalf() -> list[dict]:
    sh_file = DATA_DIR / "oos_split_half_soxx.json"
    if not sh_file.exists():
        return []
    sh = json.loads(sh_file.read_text(encoding="utf-8"))
    out = []
    for r in sh.get("rows", []):
        out.append({
            "variant": r.get("variant"),
            "train": {
                "n_trades": r.get("train", {}).get("n_trades"),
                "sharpe_annualised": _safe(r.get("train", {}).get("sharpe_annualised")),
                "equity_curve_total_return": _safe(r.get("train", {}).get("equity_curve_total_return")),
                "mc_strategy_total_return_percentile": _safe(r.get("train", {}).get("mc_strategy_total_return_percentile")),
            },
            "test": {
                "n_trades": r.get("test", {}).get("n_trades"),
                "sharpe_annualised": _safe(r.get("test", {}).get("sharpe_annualised")),
                "equity_curve_total_return": _safe(r.get("test", {}).get("equity_curve_total_return")),
                "mc_strategy_total_return_percentile": _safe(r.get("test", {}).get("mc_strategy_total_return_percentile")),
            },
        })
    return out


# ---------------------------------------------------------------------------
# Verdict block + ordinal
# ---------------------------------------------------------------------------


def _ordinal(n) -> str:
    n = int(round(n))
    if 11 <= (n % 100) <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def build_verdict_html(rows: list[dict]) -> str:
    soxx = next((r for r in rows if r["etf"] == "SOXX"), None)
    csp1 = next((r for r in rows if r["etf"] == "CSP1"), None)
    soxx_triple = soxx["configs"].get("regime_time_only_delay5_trend") if soxx else None
    csp1_triple = csp1["configs"].get("regime_time_only_delay5_trend") if csp1 else None
    soxx_sh = soxx_triple["sharpe"] if soxx_triple else None
    soxx_mc = soxx_triple["mc_pct"] if soxx_triple else None
    csp1_mc = csp1_triple["mc_pct"] if csp1_triple else None
    return (
        f"The breadth-thrust signal carries marginal information, but it generalises only on SOXX. "
        f"With the tuned config (regime exits + 5-day entry delay + 200-day trend filter), "
        f"SOXX delivers Sharpe <strong>{soxx_sh:+.2f}</strong> at the "
        f"<strong>{_ordinal(soxx_mc)} percentile</strong> of a same-distribution random-entry null "
        f"over the 2019-2026 signal-eligible window."
        f"<br><br>"
        f"Applied without re-tuning to four other ETFs (S&amp;P 500, NASDAQ-100, Energy sector, Financials sector), "
        f"the best config across the board underperforms the random null — broadest case (S&amp;P 500) lands at the "
        f"{_ordinal(csp1_mc)} percentile. The signal is a SOXX phenomenon, not a generic breadth-thrust property. "
        f"Sector concentration alone does not predict signal strength."
    )


# ---------------------------------------------------------------------------
# Inline + write
# ---------------------------------------------------------------------------


PLACEHOLDER_START = "// __DASHBOARD_DATA_START__"
PLACEHOLDER_END = "// __DASHBOARD_DATA_END__"


def inject(template_text: str, data: dict) -> str:
    payload_json = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    replacement = (
        f"{PLACEHOLDER_START}\n"
        f"const DASHBOARD_DATA_INLINE = {payload_json};\n"
        f"{PLACEHOLDER_END}"
    )
    start_idx = template_text.find(PLACEHOLDER_START)
    end_idx = template_text.find(PLACEHOLDER_END)
    if start_idx == -1 or end_idx == -1:
        raise RuntimeError(
            f"Could not find placeholder markers in {TEMPLATE}. "
            f"Expected '{PLACEHOLDER_START}' and '{PLACEHOLDER_END}'."
        )
    return (
        template_text[:start_idx]
        + replacement
        + template_text[end_idx + len(PLACEHOLDER_END) :]
    )


def build_improvements() -> dict | None:
    """Load data/improvements.json if it exists; passed through to the
    Improvements tab in the dashboard."""
    path = DATA_DIR / "improvements.json"
    if not path.exists():
        return None
    blob = json.loads(path.read_text(encoding="utf-8"))
    return {
        "test_1_regime_overlay": blob.get("test_1_regime_overlay", {}),
        "test_2_size_scaled": blob.get("test_2_size_scaled", {}),
        "test_3_multi_etf_rotation": blob.get("test_3_multi_etf_rotation", {}),
        "baselines": blob.get("baselines", {}),
        "computed_at_utc": blob.get("computed_at_utc"),
    }


def build_live_signal() -> dict | None:
    """Load data/live_signal.json (today's recommended allocation)."""
    path = DATA_DIR / "live_signal.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def build_oos_validation() -> dict | None:
    """Load data/oos_validation.json (train/test split on SOXX)."""
    path = DATA_DIR / "oos_validation.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def build_tuning() -> dict | None:
    """Load data/tuning.json (Phase 2 sensitivity work) into the dashboard."""
    path = DATA_DIR / "tuning.json"
    if not path.exists():
        return None
    blob = json.loads(path.read_text(encoding="utf-8"))
    # Strip equity-curve series from the continuous + master tables — they
    # are not plotted in the current Tuning tab. Keeps the inlined payload
    # small. Equity arrays remain in tuning.json for future use.
    cont_trimmed = {}
    for etf, r in blob.get("continuous_signal_base50", {}).items():
        cont_trimmed[etf] = {k: v for k, v in r.items() if k not in ("dates", "equity")}
    master_trimmed = {}
    for etf, r in blob.get("master_csp1_overlay_on_50_100", {}).items():
        master_trimmed[etf] = {
            "with_master_filter": r.get("with_master_filter"),
            "without_master_filter": r.get("without_master_filter"),
        }
    return {
        "base_thrust_grid": blob.get("base_thrust_grid", {}),
        "continuous_signal_base50": cont_trimmed,
        "master_csp1_overlay_on_50_100": master_trimmed,
        "computed_at_utc": blob.get("computed_at_utc"),
    }


def main() -> int:
    print("Loading per-ETF results ...", flush=True)
    rows = build_cross_etf_rows()
    print(f"  Cross-ETF matrix rows: {len(rows)}")

    print("Loading equity curves ...", flush=True)
    eq = build_equity_curves()
    for etf, blob in eq.items():
        print(f"  {etf}: {len(blob['dates'])} dates, configs={list(blob['configs'].keys())}")

    print("Loading breadth series ...", flush=True)
    breadth = build_breadth_series()
    for etf, blob in breadth.items():
        print(f"  {etf}: {len(blob['dates'])} dates, "
              f"{len(blob['signal_dates'])} signal-fire days")

    print("Loading trades ...", flush=True)
    trades = build_trades_by_etf()
    total_trades = sum(len(v) for e in trades.values() for v in e.values())
    print(f"  Total trade records across ETFs×configs: {total_trades}")

    print("Loading sensitivity sweeps ...", flush=True)
    variants = build_variants()
    splithalf = build_splithalf()

    print("Loading improvements ...", flush=True)
    improvements = build_improvements()
    if improvements:
        print(f"  test_1: {len(improvements['test_1_regime_overlay'])} ETFs")
        print(f"  test_2: {len(improvements['test_2_size_scaled'])} ETFs")
        print(f"  test_3: portfolio block present")

    print("Loading tuning ...", flush=True)
    tuning = build_tuning()
    if tuning:
        print(f"  grid    : {len(tuning['base_thrust_grid'])} ETFs")
        print(f"  cont    : {len(tuning['continuous_signal_base50'])} ETFs")
        print(f"  master  : {len(tuning['master_csp1_overlay_on_50_100'])} ETFs")

    print("Loading live signal + OOS validation ...", flush=True)
    live_signal = build_live_signal()
    oos_validation = build_oos_validation()
    if live_signal:
        print(f"  live    : as of {live_signal.get('latest_data_date')} -- "
              f"alloc {live_signal.get('current_allocation_pct')}%")
    if oos_validation:
        tw = oos_validation.get("train_winner", {})
        ts = oos_validation.get("test_winner_stats", {})
        print(f"  oos     : train winner b={tw.get('base_pct')}/t={tw.get('thrust_pct')}, "
              f"test Sharpe {ts.get('sharpe'):+.2f}")

    verdict = build_verdict_html(rows)

    data = {
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "verdict_html": verdict,
        "cross_etf": rows,
        "equity_curves": eq,
        "breadth": breadth,
        "trades": trades,
        "variants": variants,
        "splithalf": splithalf,
        "improvements": improvements,
        "tuning": tuning,
        "live_signal": live_signal,
        "oos_validation": oos_validation,
    }

    template_text = TEMPLATE.read_text(encoding="utf-8")
    print(f"\nTemplate size: {len(template_text):,} bytes")
    built = inject(template_text, data)
    DOCS.mkdir(parents=True, exist_ok=True)
    OUT.write_text(built, encoding="utf-8")
    size_kb = len(built) / 1024
    print(f"Wrote {OUT.relative_to(PROJECT_ROOT)}  ({len(built):,} bytes, {size_kb:.1f} KB)")
    if size_kb > 1500:
        print(f"  WARNING: built file is large ({size_kb:.1f} KB).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
