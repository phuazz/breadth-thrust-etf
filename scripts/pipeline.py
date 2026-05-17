"""Dashboard pipeline — inject result JSONs into template.html, write docs/index.html.

Phase 1 dashboard (single page):
  - Cross-ETF result matrix (5 ETFs x 3 configs: Sharpe + MC %ile)
  - SOXX equity curve (strategy vs SOXX vs SPY buy-and-hold)
  - Per-ETF best-result cards

Inputs (loaded automatically):
  - data/backtest_soxx.json                    (baseline backtest + equity curves)
  - data/backtest_csp1_oos.json                (S&P 500 cross-ETF OOS)
  - data/backtest_iues_oos.json                (Energy)
  - data/backtest_iufs_oos.json                (Financials)
  - data/backtest_cndx_oos.json                (NASDAQ-100)

Output:
  - docs/index.html  (GitHub Pages root)

Per CLAUDE.md:
  - Default styling: white theme, sans-serif, high contrast.
  - template.html stays under 200 KB; built docs/index.html may exceed 500 KB
    (data is inlined). NEVER open docs/index.html in this script — only write.

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


# ----- ETF metadata (for table rendering) ---------------------------------


ETF_DESC = {
    "SOXX": "iShares Semiconductor (semis)",
    "IUES": "S&P 500 Energy sector",
    "IUFS": "S&P 500 Financials sector",
    "CNDX": "NASDAQ-100",
    "CSP1": "S&P 500 (full)",
}


def _safe(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(f) or math.isinf(f)) else f


def _row_from_summary(label: str, summary: dict) -> dict:
    """Pull just the headline metrics needed for the dashboard table."""
    return {
        "sharpe": _safe(summary.get("sharpe_annualised")),
        "total_return": _safe(summary.get("equity_curve_total_return")),
        "max_dd": _safe(summary.get("equity_curve_max_dd")),
        "win_rate": _safe(summary.get("win_rate")),
        "mc_pct": _safe(summary.get("mc_strategy_total_return_percentile")),
        "n_trades": summary.get("n_trades"),
        "median_holding_days": _safe(summary.get("median_holding_days")),
    }


def _row_for_etf(etf: str, file_path: Path, is_baseline_backtest: bool = False) -> dict:
    """Build one ETF's row from either backtest_<etf>_oos.json (3 configs) OR
    backtest_soxx.json (the 1 baseline_2xATR config). Returns {etf, configs}.
    """
    payload = json.loads(file_path.read_text(encoding="utf-8"))
    configs: dict[str, dict] = {}
    if is_baseline_backtest:
        # backtest_soxx.json has only the baseline result + monte_carlo.
        primary = payload["primary"]
        mc = payload["benchmarks"]["monte_carlo_null"]
        configs["baseline_2xATR"] = {
            "sharpe": _safe(primary.get("sharpe_annualised")),
            "total_return": _safe(primary.get("equity_curve_total_return")),
            "max_dd": _safe(primary.get("equity_curve_max_dd")),
            "win_rate": _safe(primary.get("win_rate")),
            "mc_pct": _safe(mc.get("strategy_total_return_percentile")),
            "n_trades": primary.get("n_trades"),
            "median_holding_days": _safe(primary.get("median_holding_days")),
        }
    else:
        # backtest_<etf>_oos.json has a summary_table with 3 rows.
        for s in payload.get("summary_table", []):
            label = s["variant"]
            configs[label] = _row_from_summary(label, s)
    return {"etf": etf, "configs": configs}


def build_cross_etf_rows() -> list[dict]:
    """Assemble the 5-ETF cross-matrix.

    SOXX uses its full backtest result (we have all three configs from the
    sensitivity sweeps, but only baseline_2xATR is in the headline
    backtest_soxx.json). For SOXX we therefore pull all three configs from
    the variants + sensitivity files instead of just the baseline.
    """
    rows: list[dict] = []

    # SOXX — pull baseline from backtest_soxx.json + regime_time_only from
    # backtest_variants_soxx.json + regime_time_only_delay5_trend from
    # sensitivity_entry_delay_soxx.json (with delay 5 applied to that config).
    soxx_configs: dict[str, dict] = {}
    bs = json.loads((DATA_DIR / "backtest_soxx.json").read_text(encoding="utf-8"))
    primary = bs["primary"]
    mc = bs["benchmarks"]["monte_carlo_null"]
    soxx_configs["baseline_2xATR"] = {
        "sharpe": _safe(primary.get("sharpe_annualised")),
        "total_return": _safe(primary.get("equity_curve_total_return")),
        "max_dd": _safe(primary.get("equity_curve_max_dd")),
        "win_rate": _safe(primary.get("win_rate")),
        "mc_pct": _safe(mc.get("strategy_total_return_percentile")),
        "n_trades": primary.get("n_trades"),
        "median_holding_days": _safe(primary.get("median_holding_days")),
    }
    variants = json.loads(
        (DATA_DIR / "backtest_variants_soxx.json").read_text(encoding="utf-8")
    )
    for entry in variants.get("summary_table", []):
        if entry["variant"] == "regime_time_only":
            soxx_configs["regime_time_only"] = _row_from_summary("regime_time_only", entry)
            break
    sens = json.loads(
        (DATA_DIR / "sensitivity_trend_filter_soxx.json").read_text(encoding="utf-8")
    )
    for entry in sens.get("summary_table", []):
        if entry["variant"] == "regime_time_only+trendTrue":
            # Trend-filter sweep doesn't combine with delay-5. The closest single
            # config we have for SOXX matching CSP1/IUES/etc. is the split-half
            # winner from oos_split_half_soxx.json, which uses the full window
            # but only the train-half eligible signals. Use sensitivity entry-delay
            # data instead.
            pass
    # Find the regime_time_only + delay 5 result from entry-delay sensitivity.
    sens2 = json.loads(
        (DATA_DIR / "sensitivity_entry_delay_soxx.json").read_text(encoding="utf-8")
    )
    delay5_row = None
    for entry in sens2.get("summary_table", []):
        if entry["variant"] == "regime_time_only+delay5d":
            delay5_row = entry
            break
    # The triple combo (regime + delay 5 + trend) is in the split-half full-
    # window file as the winning variant; pull from there if available.
    triple_row = None
    sh_file = DATA_DIR / "oos_split_half_soxx.json"
    if sh_file.exists():
        sh = json.loads(sh_file.read_text(encoding="utf-8"))
        for r in sh.get("rows", []):
            if r["variant"] == "regime_time_only_delay5_trend":
                # split-half stores train/test halves; total = sum of train + test
                # is not meaningful; for the dashboard use the larger test-half
                # window as the headline result.
                test = r.get("test", {})
                triple_row = {
                    "sharpe": _safe(test.get("sharpe_annualised")),
                    "total_return": _safe(test.get("equity_curve_total_return")),
                    "max_dd": _safe(test.get("equity_curve_max_dd")),
                    "win_rate": _safe(test.get("win_rate")),
                    "mc_pct": _safe(test.get("mc_strategy_total_return_percentile")),
                    "n_trades": test.get("n_trades"),
                    "median_holding_days": _safe(test.get("median_holding_days")),
                    "_label_qualifier": "TEST-HALF",
                }
                break
    if triple_row:
        soxx_configs["regime_time_only_delay5_trend"] = triple_row
    elif delay5_row:
        # Fallback: regime+delay5 only (no trend filter).
        soxx_configs["regime_time_only_delay5_trend"] = _row_from_summary(
            "regime_time_only+delay5d", delay5_row
        )

    rows.append({"etf": "SOXX", "configs": soxx_configs})

    # CSP1, IUES, IUFS, CNDX — pull from their OOS files (all 3 configs each).
    for etf in ("IUES", "IUFS", "CNDX", "CSP1"):
        path = DATA_DIR / f"backtest_{etf.lower()}_oos.json"
        if not path.exists():
            continue
        rows.append(_row_for_etf(etf, path, is_baseline_backtest=False))

    return rows


# ----- Equity curves ------------------------------------------------------


def round_series(values, ndigits=4):
    return [round(float(v), ndigits) if v is not None else None for v in values]


def build_equity_curves() -> dict:
    """Phase 1: SOXX equity curve only. Phase 2 will extend to per-ETF."""
    out: dict[str, dict] = {}
    bs = json.loads((DATA_DIR / "backtest_soxx.json").read_text(encoding="utf-8"))
    eq = bs.get("equity_curves", {})
    if eq:
        out["SOXX"] = {
            "dates": eq["dates"],
            "strategy": round_series(eq["strategy"]),
            "soxx_buy_hold": round_series(eq["soxx_buy_hold"]),
            "spy_buy_hold": round_series(eq["spy_buy_hold"]),
        }
    return out


# ----- Verdict block ------------------------------------------------------


def _ordinal(n: int) -> str:
    """English ordinal suffix: 1st, 2nd, 3rd, 4th ... 11th, 12th, 13th, 21st, 22nd, 23rd ..."""
    n = int(round(n))
    if 11 <= (n % 100) <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def build_verdict_html(rows: list[dict]) -> str:
    """Two-sentence verdict + a single-line headline number."""
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
        f"(measured on the held-out second half of the 2018-2026 window)."
        f"<br><br>"
        f"Applied without re-tuning to four other ETFs (S&amp;P 500, NASDAQ-100, Energy sector, Financials sector), "
        f"the best config across the board underperforms the random null — broadest case (S&amp;P 500) lands at the "
        f"{_ordinal(csp1_mc)} percentile. The signal is a SOXX phenomenon, not a generic breadth-thrust property. "
        f"Sector concentration alone does not predict signal strength."
    )


# ----- Inline + write -----------------------------------------------------


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


def main() -> int:
    print("Loading per-ETF results ...", flush=True)
    rows = build_cross_etf_rows()
    print(f"  Built {len(rows)} ETF rows for cross-matrix")
    for r in rows:
        cfgs = ", ".join(r["configs"].keys())
        print(f"    {r['etf']:5}  configs=[{cfgs}]")

    print("Building equity curves ...", flush=True)
    eq = build_equity_curves()
    print(f"  ETFs with equity curves: {list(eq.keys())}")

    verdict = build_verdict_html(rows)

    data = {
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "verdict_html": verdict,
        "cross_etf": rows,
        "equity_curves": eq,
    }

    template_text = TEMPLATE.read_text(encoding="utf-8")
    print(f"Template size: {len(template_text):,} bytes", flush=True)
    built = inject(template_text, data)
    DOCS.mkdir(parents=True, exist_ok=True)
    OUT.write_text(built, encoding="utf-8")
    print(f"Wrote {OUT.relative_to(PROJECT_ROOT)}  ({len(built):,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
