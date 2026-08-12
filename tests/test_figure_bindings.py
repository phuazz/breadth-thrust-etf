"""Guard for the prose figure bindings in template.html.

Narrative prose used to carry hand-typed performance numbers, and they
drifted: on 2026-08-10 the page claimed a +1.15 pre-overlay blend Sharpe and
+1.30 deployed while the committed data said 1.1867 and 1.2598. The audit
that day corrected 17 such figures, and each is now written as

    <span data-fig="blend4.sharpe">+1.16</span>

where the literal is only a fallback for a JS-less render. That fallback is
exactly what drifted before, so it needs its own guard: this module parses
FIGURE_SPEC out of the template, re-resolves every binding against the
committed JSON, and asserts the committed literal still matches.

The point is that a stale number FAILS THE SUITE rather than shipping. If a
rebuild moves a figure, run the pipeline and commit the template — the
literal must move with it.

The formatters below mirror _figFormat() in the template. They are duplicated
in the sense that two languages need them, but not in the sense that a value
can disagree: test_spec_is_pure_json pins the shared spec, and every fmt name
the spec uses must be implemented here or the test errors out.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = PROJECT_ROOT / "template.html"
DATA_DIR = PROJECT_ROOT / "data"

SPEC_START = "// __FIGURE_SPEC_START__"
SPEC_END = "// __FIGURE_SPEC_END__"

# window.DATA key -> the file pipeline.py loads it from. Only the roots the
# spec actually reaches are listed; a spec entry naming anything else fails
# in test_every_spec_path_resolves rather than silently returning None.
DATA_ROOTS = {
    "multi": "multi_strategy.json",
    "risk_overlay": "risk_overlay.json",
    "bootstrap": "phase7_bootstrap.json",
    "topk": "topk_robustness.json",
}


def _load_spec() -> dict:
    text = TEMPLATE.read_text(encoding="utf-8")
    i, j = text.find(SPEC_START), text.find(SPEC_END)
    assert i != -1 and j != -1, "FIGURE_SPEC markers missing from template.html"
    block = text[i + len(SPEC_START):j]
    block = block.strip()
    assert block.startswith("const FIGURE_SPEC = "), (
        "FIGURE_SPEC block must start with `const FIGURE_SPEC = ` so this "
        f"guard can parse it; got: {block[:60]!r}")
    block = block[len("const FIGURE_SPEC = "):].rstrip().rstrip(";")
    return json.loads(block)


def _load_root() -> dict:
    root = {}
    for key, fname in DATA_ROOTS.items():
        path = DATA_DIR / fname
        if path.exists():
            root[key] = json.loads(path.read_text(encoding="utf-8"))
    return root


def _lookup(root, path: str):
    cur = root
    for part in path.split("."):
        if cur is None:
            return None
        cur = cur.get(part) if isinstance(cur, dict) else None
    return cur


def _fmt(v, fmt: str) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    if fmt == "sharpe":
        return ("+" if v > 0 else "") + f"{v:.2f}"
    if fmt == "sharpe3":
        return ("+" if v > 0 else "") + f"{v:.3f}"
    if fmt == "pct1s":
        return ("+" if v > 0 else "") + f"{v * 100:.1f}%"
    if fmt == "pct1":
        return f"{v * 100:.1f}%"
    if fmt == "pp1":
        return f"{v * 100:.1f}pp"
    if fmt == "pct0":
        return f"{v * 100:.0f}%"
    if fmt == "pctraw":
        # Source value is already a percentage (pct_days_risk_off = 13.04).
        return f"{v:.0f}%"
    raise AssertionError(f"unknown fmt {fmt!r} — mirror it from _figFormat()")


def _resolve(spec_entry: dict, root: dict):
    if "path" in spec_entry:
        return _lookup(root, spec_entry["path"])
    if "sub" in spec_entry:
        a, b = (_lookup(root, p) for p in spec_entry["sub"])
        return None if a is None or b is None else a - b
    if "abs_sub" in spec_entry:
        a, b = (_lookup(root, p) for p in spec_entry["abs_sub"])
        return None if a is None or b is None else abs(a - b)
    raise AssertionError(f"spec entry has no op: {spec_entry!r}")


def _bindings() -> list[tuple[str, str]]:
    """Every (key, literal) pair bound in the template."""
    text = TEMPLATE.read_text(encoding="utf-8")
    return re.findall(r'<span data-fig="([^"]+)">([^<]*)</span>', text)


# ---------------------------------------------------------------------------

def test_spec_is_pure_json():
    """The spec must stay parseable — it is the shared contract between the
    page and this guard. A comment or trailing comma blinds the guard."""
    spec = _load_spec()
    assert spec, "FIGURE_SPEC parsed empty"
    for key, entry in spec.items():
        assert "fmt" in entry, f"{key} has no fmt"
        assert sum(k in entry for k in ("path", "sub", "abs_sub")) == 1, (
            f"{key} must have exactly one of path/sub/abs_sub")


def test_every_binding_has_a_spec_entry():
    spec = _load_spec()
    unknown = sorted({k for k, _ in _bindings()} - set(spec))
    assert not unknown, f"data-fig keys with no FIGURE_SPEC entry: {unknown}"


def test_every_spec_path_resolves():
    """A path that resolves to None would silently leave the stale literal on
    the page — the exact failure this whole mechanism exists to prevent."""
    spec, root = _load_spec(), _load_root()
    if not root:
        pytest.skip("no committed data files to resolve against")
    unresolved = []
    for key, entry in spec.items():
        if _resolve(entry, root) is None:
            unresolved.append(key)
    assert not unresolved, f"FIGURE_SPEC entries resolving to None: {unresolved}"


def test_committed_literals_match_the_data():
    """THE GUARD. Every fallback literal must equal what the page will render.

    When this fails, the template's prose has drifted from data/*.json. Fix by
    updating the literal to the reported value -- do not weaken the test.
    """
    spec, root = _load_spec(), _load_root()
    if not root:
        pytest.skip("no committed data files to resolve against")
    drifted = []
    for key, literal in _bindings():
        expected = _fmt(_resolve(spec[key], root), spec[key]["fmt"])
        if literal != expected:
            drifted.append(f"{key}: template says {literal!r}, data says {expected!r}")
    assert not drifted, "prose figures have drifted from the data:\n  " + "\n  ".join(drifted)


def test_no_unused_spec_entries():
    """Dead spec entries rot: nothing renders them, so nothing notices when
    their path breaks."""
    spec = _load_spec()
    unused = sorted(set(spec) - {k for k, _ in _bindings()})
    assert not unused, f"FIGURE_SPEC entries bound nowhere in the template: {unused}"
