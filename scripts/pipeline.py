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

    data = {
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "ma200": ma200,
        "portfolio": portfolio,
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
