"""Resolve the template's prose figures from data, and rewrite the fallbacks.

WHY THIS EXISTS (2026-09-02). `template.html` carries hard-coded fallback
literals inside `<span data-fig="...">` so the page reads correctly with no
JavaScript. Those literals were maintained BY HAND, and
tests/test_figure_bindings.py failed whenever the refreshed numbers moved away
from them.

That made every unattended refresh unable to complete. The scheduled run
rebuilt the panels, re-ran the engines, passed every integrity guard — and
then failed pytest on a dozen drifted literals, so `scheduled_refresh` refused
to commit and nothing reached origin. It is not a rare collision: ANY refresh
that moves a bound figure trips it, which is most of them. The fleet-watch row
shows the weekend pair last pushed on 2026-08-01, and this is one of the two
reasons why (the other, a `build/portfolio.html` staging omission, was fixed
on 2026-09-02).

So the build now rewrites the fallbacks from the same data the page renders,
and the test guards the RESULT rather than demanding a human keep two copies
of a number in step.

WHAT THIS DOES NOT DO, and the reason it prints every change. Rewriting a
literal from data means a WRONG datum now propagates into the prose silently,
where previously a human had to type it and might have noticed. The sync is
therefore loud by construction: every change is printed with its before and
after, and the refresh summary carries them. Automation removes the typing,
not the reading.

The spec, the formats and the data roots live in `template.html` itself,
between the FIGURE_SPEC markers. This module and the test both read them from
there — there is one spec, and neither side may drift from it.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = PROJECT_ROOT / "template.html"
DATA_DIR = PROJECT_ROOT / "data"

SPEC_START = "// __FIGURE_SPEC_START__"
SPEC_END = "// __FIGURE_SPEC_END__"

# window.DATA key -> the file pipeline.py loads it from. Only the roots the
# spec actually reaches are listed; a spec entry naming anything else resolves
# to None and is reported rather than silently written as an em dash.
DATA_ROOTS = {
    "multi": "multi_strategy.json",
    "risk_overlay": "risk_overlay.json",
    "bootstrap": "phase7_bootstrap.json",
    "topk": "topk_robustness.json",
}

BINDING_RE = re.compile(r'(<span data-fig="([^"]+)">)([^<]*)(</span>)')


class SpecError(RuntimeError):
    """The spec block is missing or unparseable — refuse rather than guess."""


def load_spec(template: Path = TEMPLATE) -> dict:
    text = template.read_text(encoding="utf-8")
    i, j = text.find(SPEC_START), text.find(SPEC_END)
    if i == -1 or j == -1:
        raise SpecError("FIGURE_SPEC markers missing from template.html")
    block = text[i + len(SPEC_START):j].strip()
    if not block.startswith("const FIGURE_SPEC = "):
        raise SpecError(
            "FIGURE_SPEC block must start with `const FIGURE_SPEC = `; "
            f"got {block[:60]!r}")
    return json.loads(block[len("const FIGURE_SPEC = "):].rstrip().rstrip(";"))


def load_root(data_dir: Path = DATA_DIR) -> dict:
    root = {}
    for key, fname in DATA_ROOTS.items():
        path = data_dir / fname
        if path.exists():
            root[key] = json.loads(path.read_text(encoding="utf-8"))
    return root


def lookup(root, path: str):
    cur = root
    for part in path.split("."):
        if cur is None:
            return None
        cur = cur.get(part) if isinstance(cur, dict) else None
    return cur


def fmt(v, spec_fmt: str) -> str:
    """Mirror of the page's _figFormat(). Any format the spec uses must exist
    here, or the sync raises rather than writing something the page would
    render differently."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    if spec_fmt == "sharpe":
        return ("+" if v > 0 else "") + f"{v:.2f}"
    if spec_fmt == "sharpe3":
        return ("+" if v > 0 else "") + f"{v:.3f}"
    if spec_fmt == "pct1s":
        return ("+" if v > 0 else "") + f"{v * 100:.1f}%"
    if spec_fmt == "pct1":
        return f"{v * 100:.1f}%"
    if spec_fmt == "pp1":
        return f"{v * 100:.1f}pp"
    if spec_fmt == "pct0":
        return f"{v * 100:.0f}%"
    if spec_fmt == "pctraw":
        # Source value is already a percentage (pct_days_risk_off = 13.04).
        return f"{v:.0f}%"
    raise SpecError(f"unknown fmt {spec_fmt!r} — mirror it from _figFormat()")


def resolve(spec_entry: dict, root: dict):
    if "path" in spec_entry:
        return lookup(root, spec_entry["path"])
    if "sub" in spec_entry:
        a, b = (lookup(root, p) for p in spec_entry["sub"])
        return None if a is None or b is None else a - b
    if "abs_sub" in spec_entry:
        a, b = (lookup(root, p) for p in spec_entry["abs_sub"])
        return None if a is None or b is None else abs(a - b)
    raise SpecError(f"spec entry has no op: {spec_entry!r}")


def bindings(template: Path = TEMPLATE) -> list[tuple[str, str]]:
    """Every (key, literal) pair bound in the template."""
    text = template.read_text(encoding="utf-8")
    return [(k, lit) for _, k, lit, _ in BINDING_RE.findall(text)]


def expected(template: Path = TEMPLATE, data_dir: Path = DATA_DIR) -> dict:
    """key -> the literal the data says it should carry."""
    spec, root = load_spec(template), load_root(data_dir)
    out = {}
    for key, entry in spec.items():
        out[key] = fmt(resolve(entry, root), entry["fmt"])
    return out


def sync(template: Path = TEMPLATE, data_dir: Path = DATA_DIR,
         dry_run: bool = False, verbose: bool = True) -> list[tuple[str, str, str]]:
    """Rewrite drifted fallback literals in place. Returns (key, was, now).

    A key bound in the template but absent from the spec is LEFT ALONE and
    reported: the spec is the contract, and silently blanking a figure the
    spec does not describe would be worse than leaving a stale one visible.
    """
    spec = load_spec(template)
    want = expected(template, data_dir)
    text = template.read_text(encoding="utf-8")
    changes: list[tuple[str, str, str]] = []
    unspecced: set[str] = set()

    def _sub(m: re.Match) -> str:
        open_tag, key, literal, close_tag = m.groups()
        if key not in spec:
            unspecced.add(key)
            return m.group(0)
        new = want[key]
        if new != literal:
            changes.append((key, literal, new))
        return f"{open_tag}{new}{close_tag}"

    new_text = BINDING_RE.sub(_sub, text)

    if verbose:
        if changes:
            print(f"  figure bindings: rewriting {len(changes)} literal(s) "
                  f"from the data", flush=True)
            for key, was, now in changes:
                print(f"    {key}: {was} -> {now}", flush=True)
        else:
            print("  figure bindings: all literals already match the data",
                  flush=True)
        for key in sorted(unspecced):
            print(f"  figure bindings: {key} is bound in the template but not "
                  f"in FIGURE_SPEC — left as it is", flush=True)

    if changes and not dry_run:
        template.write_text(new_text, encoding="utf-8")
    return changes


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="report drift and exit 1 if any; write nothing")
    args = ap.parse_args()
    changes = sync(dry_run=args.check)
    return 1 if (args.check and changes) else 0


if __name__ == "__main__":
    raise SystemExit(main())
