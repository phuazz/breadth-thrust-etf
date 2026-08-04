"""Inject scanner_latest.json into scanner_template.html -> docs/scanner.html.

Same shape as pipeline.py: the template is the source file and is what gets
edited; the built artefact in docs/ is generated and never hand-touched. The
template carries a fetch fallback so it also works standalone during
development (`npx serve .` from the project root), which is what keeps the
edit-the-template discipline cheap to follow.

Deliberately separate from pipeline.py rather than a step inside it. The
scanner is a monitoring page on a daily cadence; the dashboard is a weekly
build over the strategy engines. Coupling them would mean a scanner fault
could fail the dashboard build, and the whole premise of the scanner is that
it cannot disturb the book.

Usage:
    python scripts/build_scanner_page.py
    python scripts/build_scanner_page.py --check   # verify, write nothing
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "scanner_template.html"
DATA_PATH = ROOT / "data" / "scanner_latest.json"
OUT_PATH = ROOT / "docs" / "scanner.html"

PLACEHOLDER_START = "// __SCANNER_DATA_START__"
PLACEHOLDER_END = "// __SCANNER_DATA_END__"

# The template is a source file and must stay reviewable; the vault rule is
# 200 KB, well above anything this page should need.
MAX_TEMPLATE_BYTES = 200 * 1024
CONFLICT_MARKERS = ("<<<<<<<", ">>>>>>>")


class ScannerPageError(RuntimeError):
    """Raised when the page cannot be built safely."""


def inject(template_text: str, payload: dict) -> str:
    """Replace the placeholder block with the data as an inline const."""
    start = template_text.find(PLACEHOLDER_START)
    end = template_text.find(PLACEHOLDER_END)
    if start == -1 or end == -1:
        raise ScannerPageError(
            f"placeholder markers missing from {TEMPLATE.name}; expected "
            f"{PLACEHOLDER_START!r} and {PLACEHOLDER_END!r}"
        )
    if end < start:
        raise ScannerPageError("placeholder markers are in the wrong order")

    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    # </script> inside a JSON string would close the host script element early.
    # json.dumps does not escape it, so do it here; the sequence is still
    # valid JSON and parses back to the same string.
    body = body.replace("</", "<\\/")
    replacement = (
        f"{PLACEHOLDER_START}\n"
        f"const SCANNER_DATA_INLINE = {body};\n"
        f"{PLACEHOLDER_END}"
    )
    return (
        template_text[:start] + replacement + template_text[end + len(PLACEHOLDER_END):]
    )


def assert_payload_usable(payload: dict) -> None:
    """Refuse to publish a page the build already knows is wrong.

    run_scanner asserts its own invariants and will not write a bad file, so
    this is a second, independent gate on the artefact as it stands on disk —
    the case where a stale or hand-edited JSON is what reaches the page.
    """
    problems: list[str] = []
    rows = payload.get("rows") or []
    if not rows:
        problems.append("no rows")
    if payload.get("n_rows") != len(rows):
        problems.append(f"n_rows {payload.get('n_rows')} != {len(rows)} rows present")
    if not payload.get("as_of"):
        problems.append("no as_of date")

    ranks = sorted(r["rank"] for r in rows if r.get("rank") is not None)
    if ranks and ranks != list(range(1, len(ranks) + 1)):
        problems.append("ranks are not a permutation of 1..n")

    missing_asof = [r["ticker"] for r in rows if not r.get("as_of")]
    if missing_asof:
        problems.append(f"rows without their own as_of: {missing_asof[:5]}")

    # The unvalidated-parameters statement is a disclosure, not decoration:
    # the page must not be able to ship without it (spec §9.7).
    if payload.get("parameters_validated") is not False:
        problems.append("parameters_validated must be false until validation exists")
    if not payload.get("parameter_note"):
        problems.append("parameter_note is missing")

    if problems:
        raise ScannerPageError(
            "scanner_latest.json is not publishable:\n  - " + "\n  - ".join(problems)
        )


def assert_output_clean(text: str) -> None:
    """Guard against the failure that once hung the main dashboard."""
    for marker in CONFLICT_MARKERS:
        if marker in text:
            raise ScannerPageError(
                f"built page contains a merge conflict marker {marker!r} — "
                f"the fault is in scanner_template.html or the source JSON"
            )
    if PLACEHOLDER_START in text and "SCANNER_DATA_INLINE = null" in text:
        raise ScannerPageError("injection did not take — data is still null")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--check", action="store_true",
                        help="validate inputs and the render, but write nothing")
    args = parser.parse_args(argv)

    if not TEMPLATE.exists():
        raise ScannerPageError(f"missing template: {TEMPLATE}")
    if not DATA_PATH.exists():
        raise ScannerPageError(
            f"missing {DATA_PATH.relative_to(ROOT)} — run "
            f"`python scripts/run_scanner.py` first"
        )

    template_bytes = TEMPLATE.stat().st_size
    if template_bytes > MAX_TEMPLATE_BYTES:
        raise ScannerPageError(
            f"{TEMPLATE.name} is {template_bytes / 1024:.0f} KB, over the "
            f"{MAX_TEMPLATE_BYTES // 1024} KB source-file limit"
        )

    payload = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    assert_payload_usable(payload)

    out = inject(TEMPLATE.read_text(encoding="utf-8"), payload)
    assert_output_clean(out)

    print(f"template {template_bytes / 1024:.0f} KB, "
          f"{payload['n_rows']} rows as of {payload['as_of']}")
    if args.check:
        print(f"check only — would write {len(out) / 1024:.0f} KB")
        return 0

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(out, encoding="utf-8")
    print(f"wrote {OUT_PATH.relative_to(ROOT)} ({OUT_PATH.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ScannerPageError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
