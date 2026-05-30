"""Regression tests for scripts/check_conflict_markers.py.

Three guarantees the scanner must hold:
  1. Detects a real merge conflict marker (line beginning with
     ``<<<<<<<`` or ``>>>>>>>``).
  2. Does NOT false-positive on a literal mention of the marker
     inside a docstring or string literal (must be at start of line).
  3. Skips its self-reference exception list so the scanner and its
     own tests do not flag themselves.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# Import the scanner functions directly so we exercise the algorithm
# without spawning a subprocess for each test.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from check_conflict_markers import (  # noqa: E402
    MARKER_PREFIXES,
    SELF_REFERENCE_EXCEPTIONS,
    scan_file,
)


# ---------------------------------------------------------------------------
# Real-marker detection
# ---------------------------------------------------------------------------

# A real git conflict block uses 7 of <<<, ===, >>>. The scanner anchors
# on `startswith` so we only need lines that begin with the marker.
REAL_CONFLICT_BLOCK = """\
<<<<<<< HEAD
const x = 1;
=======
const x = 2;
>>>>>>> branch
"""


def test_scanner_detects_real_conflict_block(tmp_path: Path):
    f = tmp_path / "broken.js"
    f.write_text(REAL_CONFLICT_BLOCK, encoding="utf-8")
    hits = scan_file(f)
    # Expect two hits — the <<<<<<< line and the >>>>>>> line.
    # (We do not check ======= because it appears in too many
    # legitimate contexts: doc separators, table dividers, etc.)
    assert len(hits) == 2
    linenos = sorted(lineno for lineno, _ in hits)
    assert linenos == [1, 5]


def test_scanner_detects_marker_at_first_line(tmp_path: Path):
    f = tmp_path / "first_line.txt"
    f.write_text("<<<<<<< HEAD\nsome content\n", encoding="utf-8")
    hits = scan_file(f)
    assert len(hits) == 1
    assert hits[0][0] == 1


def test_scanner_detects_marker_with_trailing_branch_name(tmp_path: Path):
    f = tmp_path / "with_branch.html"
    f.write_text(">>>>>>> feature/my-branch-name\n", encoding="utf-8")
    hits = scan_file(f)
    assert len(hits) == 1


# ---------------------------------------------------------------------------
# False-positive resistance — literal mentions inside string / comments
# ---------------------------------------------------------------------------

def test_scanner_ignores_marker_inside_string_literal(tmp_path: Path):
    """A JS string containing the marker as content (not at start of
    line) must NOT be flagged. The marker has to anchor at column 0."""
    f = tmp_path / "code.js"
    f.write_text(
        'const msg = "watch out for <<<<<<< in your files";\n'
        'console.log("the marker >>>>>>> looks like this");\n',
        encoding="utf-8",
    )
    assert scan_file(f) == []


def test_scanner_ignores_marker_inside_python_docstring(tmp_path: Path):
    f = tmp_path / "module.py"
    f.write_text(
        '"""Helper module.\n\n'
        'Looks for <<<<<<< at the start of a line.\n'
        'Also handles >>>>>>> patterns.\n'
        '"""\n'
        'def foo(): pass\n',
        encoding="utf-8",
    )
    assert scan_file(f) == []


def test_scanner_ignores_marker_indented(tmp_path: Path):
    """Even a literal marker, if it appears INDENTED (not column 0),
    is not a real conflict marker and should be ignored."""
    f = tmp_path / "indented.md"
    f.write_text(
        "Some prose.\n"
        "    <<<<<<< this is just an indented quotation\n"
        "    >>>>>>> still indented\n",
        encoding="utf-8",
    )
    assert scan_file(f) == []


# ---------------------------------------------------------------------------
# File-handling safety
# ---------------------------------------------------------------------------

def test_scanner_handles_binary_file_gracefully(tmp_path: Path):
    f = tmp_path / "icon.bin"
    f.write_bytes(b"\x00\x01\x02\xff\xfe\x80<<<<<<<\xff")
    # Should return [] (silently skip) rather than raising
    assert scan_file(f) == []


def test_scanner_handles_empty_file(tmp_path: Path):
    f = tmp_path / "empty.txt"
    f.write_text("", encoding="utf-8")
    assert scan_file(f) == []


def test_scanner_handles_unicode_content(tmp_path: Path):
    f = tmp_path / "unicode.md"
    f.write_text("Résumé · 中文 · emoji 🚀\nno markers here\n",
                  encoding="utf-8")
    assert scan_file(f) == []


# ---------------------------------------------------------------------------
# Self-reference exception list
# ---------------------------------------------------------------------------

def test_self_reference_exceptions_listed_correctly():
    """The scanner and its own tests must be in the exception list so
    they can mention the markers as string literals without flagging
    themselves when run on the real repo."""
    assert "scripts/check_conflict_markers.py" in SELF_REFERENCE_EXCEPTIONS
    assert "tests/test_check_conflict_markers.py" in SELF_REFERENCE_EXCEPTIONS


# ---------------------------------------------------------------------------
# End-to-end: run the actual script against the live repo
# ---------------------------------------------------------------------------

def test_repo_currently_has_no_conflict_markers():
    """The repo as committed must currently be clean. This is the
    test that fires if anyone commits a file with conflict markers in
    the future — the test fails BEFORE CI catches it, which means
    pytest -q catches it locally too."""
    repo_root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, "scripts/check_conflict_markers.py"],
        cwd=repo_root, capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"check_conflict_markers.py failed:\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


def test_marker_prefixes_constant_unchanged():
    """If anyone changes MARKER_PREFIXES to include ``=======``, this
    test fails — because that prefix appears in too many legitimate
    contexts (RST separators, ASCII tables, doc dividers) and would
    cause runaway false positives."""
    assert MARKER_PREFIXES == ("<<<<<<<", ">>>>>>>"), (
        "Do not add '=======' to MARKER_PREFIXES — it false-positives "
        "on RST headings, ASCII tables, and other doc conventions."
    )
