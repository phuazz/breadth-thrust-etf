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


def load_multi_strategy() -> dict | None:
    """Load data/multi_strategy.json: combinations of Strategy A + Strategy B
    — fixed-weight blends (70/30, 50/50, 30/70) and meta-rotation."""
    path = DATA_DIR / "multi_strategy.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_robustness() -> dict | None:
    """Load data/robustness.json. Trims wf_dates/wf_equity (heavy series
    not currently rendered) to keep the inlined payload compact."""
    path = DATA_DIR / "robustness.json"
    if not path.exists():
        return None
    blob = json.loads(path.read_text(encoding="utf-8"))
    wf = blob.get("test_1_walk_forward_l", {})
    for etf in wf:
        wf[etf].pop("wf_dates", None)
        wf[etf].pop("wf_equity", None)
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

    print("Loading extended history ...", flush=True)
    extended = load_extended_history()
    if extended:
        print(f"  extended history {extended.get('start_date')} → {extended.get('end_date')}")

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

    print("Loading multi-strategy combinations (A+B) ...", flush=True)
    multi = load_multi_strategy()
    if multi:
        strats = multi.get("strategies", {})
        print(f"  multi-strategy: {len(strats)} variants on common window "
              f"{multi.get('common_start')} -> {multi.get('common_end')}")

    data = {
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "ma200": ma200,
        "portfolio": portfolio,
        "robustness": robustness,
        "extended": extended,
        "topk": topk,
        "asset_class": asset_class,
        "multi": multi,
    }

    template_text = TEMPLATE.read_text(encoding="utf-8")
    print(f"\nTemplate size: {len(template_text):,} bytes")
    built = inject(template_text, data)
    DOCS.mkdir(parents=True, exist_ok=True)
    OUT.write_text(built, encoding="utf-8")
    size_kb = len(built) / 1024
    print(f"Wrote {OUT.relative_to(PROJECT_ROOT)}  ({len(built):,} bytes, {size_kb:.1f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
