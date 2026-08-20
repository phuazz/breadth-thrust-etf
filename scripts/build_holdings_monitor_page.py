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
import re
import shutil
import subprocess
import sys
import tempfile
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


def check_inline_script(html: str) -> str:
    """Parse the page's own inline script with node, or explain why not.

    THIS EXISTS BECAUSE OF A REAL FAILURE. On 2026-08-19 an edit left a bare
    apostrophe inside a single-quoted JS string. A syntax error aborts the
    WHOLE script before any global is defined, so the page still returned
    HTTP 200, still had the right title, still passed the static mobile
    check — and rendered an empty table. Nothing in the build noticed, and
    nothing downstream could: every check was on the file, not on whether
    the file runs.

    A parse check is cheap and catches the entire class. It is skipped, with
    a printed note, when node is unavailable — a missing tool must not block
    a build, but its absence must not be silent either.
    """
    node = shutil.which("node")
    if not node:
        return "SKIPPED (node not on PATH — the page is unparsed)"
    # The inline script is the one without a src attribute.
    blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
                        html, re.DOTALL)
    if not blocks:
        raise MonitorPageError("no inline script found in the built page")
    body = "\n".join(blocks)
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "page.js"
        f.write_text(body, encoding="utf-8")
        p = subprocess.run([node, "--check", str(f)],
                           capture_output=True, text=True)
    if p.returncode != 0:
        detail = (p.stderr or p.stdout or "").strip().splitlines()
        raise MonitorPageError(
            "the page's inline script does not parse — it would render blank:\n  "
            + "\n  ".join(detail[:6]))
    return f"OK ({len(body)/1024:.0f}KB parsed)"


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
    if "dates" not in series or "series" not in series:
        raise MonitorPageError(
            "series file is not the shared-axis shape {dates, series}; the "
            "page would read every chart as empty")
    n_dates = len(series["dates"])
    # Every array must match the shared axis. A short array would silently
    # misalign a name's whole price history against the date axis — the
    # chart would render, look plausible, and be wrong by however many
    # sessions it was short.
    ragged = {tk: len(v) for tk, v in series["series"].items()
              if len(v) != n_dates}
    if ragged:
        raise MonitorPageError(
            f"{len(ragged)} series do not match the {n_dates}-session shared "
            f"axis: {dict(list(ragged.items())[:5])}")
    wanted = {r["t"] for f in funds.values() for r in f["rows"]}
    missing = wanted - set(series["series"])
    if missing:
        cover = 1 - len(missing) / max(1, len(wanted))
        print(f"  NOTE: {len(missing)} of {len(wanted)} names have no chart series "
              f"({cover:.1%} covered): {sorted(missing)[:8]}")

    out = inject(tpl, payload)
    # Parse the built artefact, not the template: the injected payload sits
    # inside the same script, so a payload that broke it would be invisible
    # to any check on the source.
    print(f"  script parse: {check_inline_script(out)}")
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
