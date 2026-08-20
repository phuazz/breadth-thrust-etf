"""Inject holdings_monitor_latest.json into the template -> docs/holdings-monitor.html.

Same shape as build_scanner_page.py: the template is the source file and is
what gets edited; the artefact in docs/ is generated and never hand-touched.
The template carries a fetch fallback so it also works standalone during
development (`npx serve .` from the project root), which keeps the
edit-the-template discipline cheap to follow.

Deliberately separate from pipeline.py. The monitor runs daily against
third-party CDNs and the dashboard is a weekly build over the strategy
engines; coupling them would let a CDN hiccup fail the dashboard build, and
the premise of the monitor is that it cannot disturb the book.

Usage:
    python scripts/build_holdings_monitor_page.py
    python scripts/build_holdings_monitor_page.py --check   # verify, write nothing
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "holdings_monitor_template.html"
DATA_PATH = ROOT / "data" / "holdings_monitor_latest.json"
OUT_PATH = ROOT / "docs" / "holdings-monitor.html"
# Fetched lazily by the page when a row is expanded, so it is not injected —
# but it must exist and cover the rows, or clicking a row yields nothing.
SERIES_PATH = ROOT / "docs" / "holdings-monitor-series.json"

PLACEHOLDER_START = "// __MONITOR_DATA_START__"
PLACEHOLDER_END = "// __MONITOR_DATA_END__"

# The template is a source file and must stay reviewable; the vault rule is
# 200 KB, well above anything this page should need.
MAX_TEMPLATE_BYTES = 200 * 1024
CONFLICT_MARKERS = ("<<<<<<<", ">>>>>>>")


class MonitorPageError(RuntimeError):
    """Raised when the page cannot be built safely."""


def inject(template_text: str, payload: dict) -> str:
    start = template_text.find(PLACEHOLDER_START)
    end = template_text.find(PLACEHOLDER_END)
    if start == -1 or end == -1:
        raise MonitorPageError(
            f"placeholder markers missing from {TEMPLATE.name}; expected "
            f"{PLACEHOLDER_START!r} and {PLACEHOLDER_END!r}")
    if end < start:
        raise MonitorPageError("placeholder markers are in the wrong order")
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return (template_text[:start] + PLACEHOLDER_START + "\nvar MONITOR = " +
            body + ";\n" + template_text[end:])


def build(check_only: bool = False) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    if not TEMPLATE.exists():
        raise MonitorPageError(f"{TEMPLATE} not found")
    if not DATA_PATH.exists():
        raise MonitorPageError(
            f"{DATA_PATH.name} not found — run run_holdings_monitor.py first")

    tpl = TEMPLATE.read_text(encoding="utf-8")
    size = len(tpl.encode("utf-8"))
    if size > MAX_TEMPLATE_BYTES:
        raise MonitorPageError(
            f"{TEMPLATE.name} is {size/1024:.0f}KB, over the "
            f"{MAX_TEMPLATE_BYTES/1024:.0f}KB source-file limit")
    for marker in CONFLICT_MARKERS:
        if marker in tpl:
            raise MonitorPageError(
                f"{TEMPLATE.name} contains an unresolved merge marker {marker!r}")

    payload = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    funds = payload.get("funds") or {}
    if not funds:
        raise MonitorPageError("payload carries no funds")
    for etf, f in funds.items():
        if not f.get("rows"):
            raise MonitorPageError(f"{etf} has no rows; refusing to build an empty page")

    # The series file is fetched by the page, not injected, so a missing or
    # under-covering one produces a page whose charts silently do nothing.
    if not SERIES_PATH.exists():
        raise MonitorPageError(f"{SERIES_PATH.name} missing; charts would be dead")
    series = json.loads(SERIES_PATH.read_text(encoding="utf-8"))
    wanted = {r["t"] for f in funds.values() for r in f["rows"]}
    missing = wanted - set(series)
    if missing:
        cover = 1 - len(missing) / max(1, len(wanted))
        print(f"  NOTE: {len(missing)} of {len(wanted)} names have no chart series "
              f"({cover:.1%} covered): {sorted(missing)[:8]}")

    out = inject(tpl, payload)
    if check_only:
        print(f"OK  template {size/1024:.0f}KB, payload "
              f"{DATA_PATH.stat().st_size/1024:.0f}KB, would write "
              f"{len(out.encode('utf-8'))/1024:.0f}KB")
        return 0
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(out, encoding="utf-8")
    print(f"Wrote {OUT_PATH.relative_to(ROOT)} "
          f"({len(out.encode('utf-8'))/1024:.0f}KB) from "
          f"{TEMPLATE.name} ({size/1024:.0f}KB)")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--check", action="store_true", help="verify, write nothing")
    a = p.parse_args(argv)
    try:
        return build(a.check)
    except MonitorPageError as exc:
        print(f"FAIL: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
