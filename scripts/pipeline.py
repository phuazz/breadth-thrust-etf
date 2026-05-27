"""Dashboard pipeline — inject result JSONs into template.html, write docs/index.html.

Phase 3 dashboard (5 tabs):
  - Monitor   : per-ETF live state cards (today's allocation under MA200 + 50/150)
  - Strategy  : MA200 verdict, per-ETF comparison table, threshold robustness chart
  - ETF Detail: drill-in — breadth time series, equity curve, long episodes table
  - Portfolio : relative-strength portfolio construction (top-K by breadth)
  - Method    : signal definition, data sources, OOS validation, caveats

Inputs (loaded automatically):
  - data/ma200_sweep.json           (per-ETF sweep + monitor + detail blocks)
  - data/portfolio_construction.json (top-K portfolio variants vs benchmarks)

Output:
  - docs/index.html  (GitHub Pages root)

Per CLAUDE.md:
  - White theme, sans-serif, high contrast.
  - template.html stays under 200 KB. Built artefact will be ~1-3 MB
    due to inlined per-ETF equity / breadth series — NEVER re-read it
    into Claude context.

Run:
    python scripts/pipeline.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
TEMPLATE = PROJECT_ROOT / "template.html"
DOCS = PROJECT_ROOT / "docs"
OUT = DOCS / "index.html"


PLACEHOLDER_START = "// __DASHBOARD_DATA_START__"
PLACEHOLDER_END = "// __DASHBOARD_DATA_END__"


def load_ma200() -> dict | None:
    """Load data/ma200_sweep.json. Trims winner_equity_curves to only the
    series the dashboard actually renders (family_b, family_d, buy_and_hold)
    — family_a and family_c equity curves stay in the source JSON for
    reproducibility but are dropped from the inlined dashboard payload to
    keep docs/index.html under ~3 MB.
    """
    path = DATA_DIR / "ma200_sweep.json"
    if not path.exists():
        return None
    blob = json.loads(path.read_text(encoding="utf-8"))
    if "winner_equity_curves" in blob:
        trimmed = {}
        for etf, ws in blob["winner_equity_curves"].items():
            trimmed[etf] = {
                k: v for k, v in ws.items()
                if k in ("family_b", "family_d", "buy_and_hold")
            }
        blob["winner_equity_curves"] = trimmed
    return blob


def load_portfolio() -> dict | None:
    path = DATA_DIR / "portfolio_construction.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_extended_history() -> dict | None:
    path = DATA_DIR / "extended_history.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_topk_robustness() -> dict | None:
    """Load data/topk_robustness.json: rebalance-frequency grid + trade history
    for the top-K rotation headline variant (K=7 weekly Friday)."""
    path = DATA_DIR / "topk_robustness.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_asset_class() -> dict | None:
    """Load data/asset_class_rotation.json: Strategy B (Phase 2) — asset-class
    momentum rotation across 14 broad ETFs (US equity / Intl / EM / RE /
    commodities / bonds)."""
    path = DATA_DIR / "asset_class_rotation.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_thematic() -> dict | None:
    """Load data/thematic_rotation.json: Strategy C (Phase 3) — thematic
    momentum rotation across 16 thematic ETFs (AI / cyber / clean energy /
    biotech / commodity-equity / etc) with signal floor + per-ETF cap."""
    path = DATA_DIR / "thematic_rotation.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_europe() -> dict | None:
    """Load data/europe_rotation.json: Strategy D (Phase 4) — Europe sector
    breadth top-K rotation across 5 Stoxx Europe 600 sector UCITS ETFs
    (EXV1 banks / EXH1 energy / EXV3 tech / EXH3 healthcare / EXH9 utilities)."""
    path = DATA_DIR / "europe_rotation.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_multi_strategy() -> dict | None:
    """Load data/multi_strategy.json: combinations of Strategy A + Strategy B
    — fixed-weight blends (70/30, 50/50, 30/70) and meta-rotation."""
    path = DATA_DIR / "multi_strategy.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_bootstrap() -> dict | None:
    """Load data/phase7_bootstrap.json: block-bootstrap CIs on per-strategy
    Sharpe and on key paired differentials (deployed vs baseline, etc).
    Block size 60 trading days, 2000 samples."""
    path = DATA_DIR / "phase7_bootstrap.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_right_tail() -> dict | None:
    """Load data/phase8_right_tail.json: Sortino, skewness, rolling 12m
    extremes, regime decomposition, % months as top sleeve. Surfaces the
    optionality-side metrics that Sharpe ratios alone underrate."""
    path = DATA_DIR / "phase8_right_tail.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_risk_overlay() -> dict | None:
    """Load data/risk_overlay.json — Phase 19 breadth regime gate
    diagnostics + gated equity variants. Built by
    scripts/run_risk_overlay.py after multi_strategy.json is built.

    The pipeline merges its ``gated_variants`` into
    window.DATA.multi.strategies (so the existing chart code finds the
    gated key) and its top-level diagnostics into
    window.DATA.multi.regime_gate (for the live regime badge). This
    preserves the user-facing dashboard shape while the architecture
    keeps the overlay as a separate concern from blend construction.
    """
    path = DATA_DIR / "risk_overlay.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_holdings_prices() -> dict | None:
    """Load data/holdings_prices_1y.json — per-ETF 1Y daily close prices
    used by the Monitor tab's holdings click-to-expand mini-chart.
    Built by scripts/export_holdings_prices.py from existing price caches.
    """
    path = DATA_DIR / "holdings_prices_1y.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_robustness() -> dict | None:
    """Load data/robustness.json into a slim payload for the Risk & Validation
    tab.

    Phase 9 cleanup (2026-05-24): keeps only the fields actually rendered by
    the dashboard:
      - test_10_wf_topk_portfolio  (paradigm comparison + WF K refit segments)
      - test_11 lives in topk_robustness.json, not here
      - test_12 lives in ma200_sweep.json's per-ETF breadth, not here

    Legacy fields kept ONLY because renderRobustnessParadigm reads them to
    compute paradigm-1 and paradigm-2 comparison rows (the strategic case
    for top-K rotation over per-ETF L tuning):
      - test_1_walk_forward_l    (paradigm 1: per-ETF tuned L)
      - test_8_fixed_L            (paradigm 2: fixed L = 60)
      - The wf_dates / wf_equity per-ETF sub-arrays inside test_1 are
        stripped — they were never rendered.

    All other test_N_* legacy fields are dropped from the inlined payload
    (still in the source robustness.json on disk for archival
    reproducibility, but not shipped to the browser).
    """
    path = DATA_DIR / "robustness.json"
    if not path.exists():
        return None
    blob = json.loads(path.read_text(encoding="utf-8"))
    # Strip heavy sub-arrays from test_1 (the only legacy field we keep)
    wf = blob.get("test_1_walk_forward_l", {})
    for etf in wf:
        wf[etf].pop("wf_dates", None)
        wf[etf].pop("wf_equity", None)
    # Drop the non-rendered legacy fields entirely
    legacy_drop = [
        "test_2_borrow_cost",
        "test_3_sub_periods",
        "test_4_bootstrap",
        "test_5_ma_period_csp1",
        "test_5b_ma_period_soxx",
        "test_6_rebalance_freq",
        "test_7_wf_x_cadence",
    ]
    for k in legacy_drop:
        blob.pop(k, None)
    return blob


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
    print("Loading MA200 sweep ...", flush=True)
    ma200 = load_ma200()
    if ma200:
        n_etfs = len(ma200.get("monitor", {}))
        print(f"  {n_etfs} ETFs in monitor block")
        in_long = sum(1 for c in ma200.get("monitor", {}).values() if c.get("in_long_state"))
        print(f"  {in_long}/{n_etfs} ETFs currently in LONG-leveraged state")
    else:
        print("  WARNING: no ma200_sweep.json found")

    print("Loading portfolio construction ...", flush=True)
    portfolio = load_portfolio()
    if portfolio:
        print(f"  {len(portfolio.get('results', {}))} portfolio variants")
    else:
        print("  WARNING: no portfolio_construction.json found")

    print("Loading robustness ...", flush=True)
    robustness = load_robustness()
    if robustness:
        wf = robustness.get("test_1_walk_forward_l", {})
        print(f"  walk-forward L for {len(wf)} ETFs")

    # Phase 9 cleanup: extended_history.json was the input to Robustness
    # Test 9 (per-ETF L-threshold 2000-2026 backtest), which is legacy.
    # The renderer was removed in Phase 9 so we no longer inline the data.
    # The file remains on disk for archival reproducibility.
    extended = None

    print("Loading top-K rotation robustness ...", flush=True)
    topk = load_topk_robustness()
    if topk:
        h = topk.get("headline", {})
        print(f"  top-K rotation: K={h.get('K')} {h.get('rebal_freq')}, "
              f"{h.get('n_rebalances')} rebalances, "
              f"Sharpe {h.get('headline_stats', {}).get('sharpe', 0):+.2f}")

    print("Loading asset-class rotation (Strategy B) ...", flush=True)
    asset_class = load_asset_class()
    if asset_class:
        h = asset_class.get("headline", {})
        print(f"  asset-class: K={h.get('K')} {h.get('rebal_freq')}, "
              f"{h.get('n_rebalances')} rebalances, "
              f"Sharpe {h.get('headline_stats', {}).get('sharpe', 0):+.2f}")

    print("Loading thematic rotation (Strategy C) ...", flush=True)
    thematic = load_thematic()
    if thematic:
        h = thematic.get("headline", {})
        print(f"  thematic: K={h.get('K')} {h.get('rebal_freq')}, "
              f"{h.get('n_rebalances')} rebalances, "
              f"Sharpe {h.get('headline_stats', {}).get('sharpe', 0):+.2f}")

    print("Loading Europe sector rotation (Strategy D) ...", flush=True)
    europe = load_europe()
    if europe:
        h = europe.get("headline", {})
        print(f"  europe: K={h.get('K')} {h.get('rebal_freq')}, "
              f"{h.get('n_rebalances')} rebalances, "
              f"Sharpe {h.get('headline_stats', {}).get('sharpe', 0):+.2f}")

    print("Loading multi-strategy combinations (A+B[+C][+D]) ...", flush=True)
    multi = load_multi_strategy()
    if multi:
        strats = multi.get("strategies", {})
        print(f"  multi-strategy: {len(strats)} variants on common window "
              f"{multi.get('common_start')} -> {multi.get('common_end')}")

    print("Loading bootstrap CIs (Phase 7) ...", flush=True)
    bootstrap = load_bootstrap()
    if bootstrap:
        ps = bootstrap.get("per_strategy", {})
        ds = bootstrap.get("differentials", {})
        print(f"  bootstrap: {len(ps)} per-strategy + {len(ds)} paired diffs "
              f"({bootstrap.get('n_bootstrap_samples', 0)} samples, "
              f"block size {bootstrap.get('block_size_days', 0)}d)")

    print("Loading right-tail / regime metrics (Phase 8) ...", flush=True)
    right_tail = load_right_tail()
    if right_tail:
        ps = right_tail.get("per_strategy", {})
        rd = right_tail.get("regime_decomposition", {})
        ts = right_tail.get("top_sleeve_by_month", {})
        print(f"  right-tail: {len(ps)} per-strategy, {len(rd)} regimes, "
              f"{len(ts)} top-sleeve buckets")

    print("Loading risk overlay (Phase 19 regime gate) ...", flush=True)
    risk_overlay = load_risk_overlay()
    if risk_overlay:
        # Merge the gated variant(s) into the multi-strategy strategies
        # dict so the existing chart-rendering code finds them. Merge
        # the top-level diagnostics into multi.regime_gate for the
        # live regime badge in the hero strip.
        if multi and "strategies" in multi:
            for k, v in (risk_overlay.get("gated_variants") or {}).items():
                multi["strategies"][k] = v
            multi["regime_gate"] = {
                **(risk_overlay.get("gate_parameters") or {}),
                "current_state": risk_overlay.get("current_state"),
                "current_state_since": risk_overlay.get("current_state_since"),
                "current_breadth": risk_overlay.get("current_breadth"),
                "n_switches": risk_overlay.get("n_switches"),
                "days_risk_off": risk_overlay.get("days_risk_off"),
                "pct_days_risk_off": risk_overlay.get("pct_days_risk_off"),
            }
        print(f"  risk_overlay: {len(risk_overlay.get('gated_variants', {}))} "
              f"gated variant(s), current={risk_overlay.get('current_state')} "
              f"since {risk_overlay.get('current_state_since')}")
    else:
        print("  WARNING: data/risk_overlay.json missing — run "
              "scripts/run_risk_overlay.py after run_multi_strategy.py to "
              "enable the Phase 19 breadth regime gate")

    print("Loading holdings 1Y prices ...", flush=True)
    holdings_prices = load_holdings_prices()
    if holdings_prices:
        n = len(holdings_prices.get("prices", {}))
        print(f"  holdings_prices: {n} tickers (built by "
              f"scripts/export_holdings_prices.py)")
    else:
        print("  WARNING: data/holdings_prices_1y.json missing — "
              "run scripts/export_holdings_prices.py to enable the "
              "holdings click-to-expand mini-chart")

    data = {
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "ma200": ma200,
        "portfolio": portfolio,
        "robustness": robustness,
        "extended": extended,
        "topk": topk,
        "asset_class": asset_class,
        "thematic": thematic,
        "europe": europe,
        "multi": multi,
        "bootstrap": bootstrap,
        "right_tail": right_tail,
        "holdings_prices": holdings_prices,
        "risk_overlay": risk_overlay,
    }

    template_text = TEMPLATE.read_text(encoding="utf-8")
    print(f"\nTemplate size: {len(template_text):,} bytes")
    built = inject(template_text, data)
    DOCS.mkdir(parents=True, exist_ok=True)
    OUT.write_text(built, encoding="utf-8")
    size_kb = len(built) / 1024
    print(f"Wrote {OUT.relative_to(PROJECT_ROOT)}  ({len(built):,} bytes, {size_kb:.1f} KB)")

    # B28 — Auto-generate the monthly factsheet PDF from the same data
    # the dashboard uses. Soft-fail if matplotlib is unavailable so a
    # broken factsheet build does not break the dashboard pipeline.
    print(f"\nBuilding factsheet PDF ...")
    try:
        import build_factsheet
        build_factsheet.build(DOCS / "factsheet_latest.pdf")
    except Exception as exc:
        print(f"  WARN: factsheet build failed (non-fatal): {exc}",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
