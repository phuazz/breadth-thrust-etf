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


# ONE implementation, imported (2026-09-02). These helpers used to be
# duplicated here, and the build had no way to reach them — which is why the
# fallback literals were maintained by hand and why every refresh that moved a
# figure failed this test and blocked the scheduled push. scripts/figure_bindings
# now owns the spec parsing, resolution and formatting; the build calls its
# sync() and this guard checks the result. A second copy could drift from the
# page's own _figFormat(); there is no longer a second copy.
from scripts.figure_bindings import (  # noqa: E402
    bindings as _bindings,
    fmt as _fmt,
    load_root as _load_root,
    load_spec as _load_spec,
    lookup as _lookup,
    resolve as _resolve,
)


def _unfmt(s: str, fmt: str) -> float:
    """Inverse of _fmt, back to the value's own scale.

    Only used to ask what a reader gets by subtracting two RENDERED figures,
    so it must undo exactly the scaling _fmt applied — percent and pp formats
    multiply by 100 on the way out, so they divide by 100 on the way back.
    """
    num = float(s.replace("%", "").replace("pp", "").replace("+", "").strip())
    if fmt in ("pct1s", "pct1", "pp1", "pct0"):
        return num / 100
    return num


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


def test_visible_rounding_gaps_are_marked_as_approximate():
    """A computed difference printed BESIDE its two components must not look
    like arithmetic the reader can check and fail.

    sub/abs_sub entries are computed on unrounded values, so the printed
    difference can disagree with the difference of the printed components.
    On 2026-08-15 blend3.sharpe crossed 1.085 and began rendering +1.09
    against blend4's +1.16, turning a sentence that had read "+1.08 -> +1.16,
    delta +0.08" into one whose own figures subtract to 0.07. Nothing was
    wrong with the data and no literal could fix it — both roundings are
    honest — but the page asserted a sum that did not add up.

    The invariant is narrow on purpose: it only bites where the gap is
    VISIBLE, meaning both components are rendered near the difference. Where
    they are not (gate.sharpe_add quotes +0.08 with its components in a
    different section) there is nothing for a reader to check and no marker
    is required. Satisfy it with "~" or "before rounding", not by rounding
    the data to agree.
    """
    spec, root = _load_spec(), _load_root()
    if not root:
        pytest.skip("no committed data files to resolve against")

    text = TEMPLATE.read_text(encoding="utf-8")
    by_path = {e["path"]: k for k, e in spec.items() if "path" in e}
    WINDOW = 400
    MARKERS = ("~", "before rounding", "approx")

    unmarked = []
    for key, entry in spec.items():
        op = "sub" if "sub" in entry else ("abs_sub" if "abs_sub" in entry else None)
        if op is None:
            continue
        a, b = (_lookup(root, p) for p in entry[op])
        if a is None or b is None:
            continue
        exact = abs(a - b) if op == "abs_sub" else a - b
        # What a reader gets by subtracting the two RENDERED components.
        ra, rb = _fmt(a, entry["fmt"]), _fmt(b, entry["fmt"])
        naive_num = _unfmt(ra, entry["fmt"]) - _unfmt(rb, entry["fmt"])
        if op == "abs_sub":
            naive_num = abs(naive_num)
        if _fmt(naive_num, entry["fmt"]) == _fmt(exact, entry["fmt"]):
            continue  # the printed sum checks out; nothing to disclose

        comp_keys = [by_path.get(p) for p in entry[op]]
        for m in re.finditer(rf'<span data-fig="{re.escape(key)}">', text):
            lo, hi = max(0, m.start() - WINDOW), m.end() + WINDOW
            window = text[lo:hi]
            both_shown = all(
                ck and f'data-fig="{ck}"' in window for ck in comp_keys)
            if both_shown and not any(mk in window for mk in MARKERS):
                unmarked.append(
                    f"{key} renders {_fmt(exact, entry['fmt'])} beside "
                    f"{ra} and {rb}, which subtract to "
                    f"{_fmt(naive_num, entry['fmt'])}, with no approximation "
                    f"marker within {WINDOW} chars")

    assert not unmarked, (
        "a printed difference disagrees with its own printed components:\n  "
        + "\n  ".join(unmarked))


def test_no_unused_spec_entries():
    """Dead spec entries rot: nothing renders them, so nothing notices when
    their path breaks."""
    spec = _load_spec()
    unused = sorted(set(spec) - {k for k, _ in _bindings()})
    assert not unused, f"FIGURE_SPEC entries bound nowhere in the template: {unused}"
