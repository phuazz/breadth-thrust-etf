"""Scan every tracked file for unresolved git merge conflict markers.

Catches the failure mode where a stash-pop or rebase leaves
``<<<<<<<`` / ``>>>>>>>`` lines in committed files, breaking JS / YAML
/ JSON / Python parsers downstream. The dashboard's 2026-05-30
production outage was caused by exactly this — docs/index.html
contained markers that made the inlined ``<script>`` a syntax error.

Wired in two places:
  * .github/workflows/conflict_check.yml — runs on every push + PR,
    cannot be bypassed.
  * .githooks/pre-commit (installed via scripts/install_git_hooks.sh)
    — local fast-fail before the commit is even created.

Algorithm: only flag markers that appear at the START of a line. The
real conflict-marker convention is a line that begins with seven or
more ``<`` / ``=`` / ``>`` characters. A literal mention of the
marker inside a string or comment (e.g. in this file or in commit
messages) is intentionally ignored.

The script itself is excluded from scanning so it can describe the
markers it looks for without being flagged.

Exit code 0 if clean, 1 if any marker found.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# Lines beginning with these are the unambiguous conflict-marker shape
# git writes. We anchor to start-of-line so quoted string literals
# elsewhere do not false-positive.
MARKER_PREFIXES = ("<<<<<<<", ">>>>>>>")

# Files that legitimately contain the marker as a literal — they
# document the failure mode or test the scanner itself. Paths are
# relative to repo root.
SELF_REFERENCE_EXCEPTIONS = {
    "scripts/check_conflict_markers.py",
    "tests/test_check_conflict_markers.py",
}


def list_tracked_files() -> list[Path]:
    """All files git knows about (respects .gitignore)."""
    out = subprocess.check_output(["git", "ls-files"], text=True,
                                    encoding="utf-8")
    return [Path(p) for p in out.splitlines() if p]


def scan_file(path: Path) -> list[tuple[int, str]]:
    """Return [(lineno, snippet), ...] of conflict-marker lines, or []."""
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []   # binary or unreadable — skip silently
    hits = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for prefix in MARKER_PREFIXES:
            if line.startswith(prefix):
                hits.append((lineno, line[:80]))
                break
    return hits


def main() -> int:
    try:
        files = list_tracked_files()
    except subprocess.CalledProcessError as exc:
        print(f"ERROR: could not list tracked files: {exc}", file=sys.stderr)
        return 2

    bad: list[tuple[Path, int, str]] = []
    n_scanned = 0
    for f in files:
        rel = f.as_posix()
        if rel in SELF_REFERENCE_EXCEPTIONS:
            continue
        if not f.is_file():
            continue
        n_scanned += 1
        for lineno, snippet in scan_file(f):
            bad.append((f, lineno, snippet))

    if bad:
        print(f"FAIL: {len(bad)} conflict marker(s) found in "
              f"{len({f for f, _, _ in bad})} file(s):", file=sys.stderr)
        for f, lineno, snippet in bad:
            print(f"  {f.as_posix()}:{lineno}  {snippet}", file=sys.stderr)
        print("\nResolve the merge conflict in source, then re-run.",
              file=sys.stderr)
        return 1

    print(f"OK: scanned {n_scanned} tracked file(s), no conflict markers.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
