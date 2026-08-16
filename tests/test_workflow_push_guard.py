"""Workflow publish steps must not swallow git failures, or drop what they built.

2026-08-13, external code review, finding F1 (CONFIRMED): three workflows
pushed with a soft fallback of the form ``git push origin main || echo``.
A push with nothing to send succeeds anyway, so the fallback branch fired
ONLY on genuine failures — mislabelling them, leaving the job green, and
keeping the ``if: failure()`` alert emails silent while a published
surface quietly froze. The daily live track had no outside-in catch at
all (the sentinel watches factsheet_meta.json only).

The fix ports the scanner-daily / universe_monitor pattern to every
workflow push: three attempts, ``git pull --rebase --autostash`` between
them, loud ``exit 1`` on exhaustion. These tests pin the convention:

  1. no ``||`` fallback of any kind on a ``git push`` line — hard-fail
     is expressed through the retry loop, never through ``||``;
  2. no ``||`` fallback on a ``git add`` line — a soft-failed add feeds
     an empty staged-diff check and skips the publish silently.

``git diff --cached --quiet || git commit`` remains legitimate — there
the ``||`` belongs to the emptiness check, not to a swallowed failure —
which is why the ban is scoped to push/add lines rather than to ``||``
generally. Comment lines are skipped so prose may mention the idiom.

  3. a workflow that RUNS a build script must STAGE what that script
     writes (2026-08-16).

The third is the same failure wearing different clothes: the daily and
weekly workflows both rebuilt build/portfolio.html and then staged only
``data/`` and ``docs/``, so the reduced public page — which
phuazz/portfolio fetches from raw.githubusercontent.com and republishes —
reached main only when a human committed it. Nothing in either job could
notice, because both rebuild BEFORE they run pytest: the runner's copy is
always fresh, and the drift exists only in committed state. It surfaced
on 759eb6c, four commits after the weekly refresh that opened it, through
the Tests workflow's parity check rather than through either publisher.

The artefact paths are read off the builder module rather than written
out here, so renaming an output moves the guard with it instead of
quietly emptying it.
"""

from __future__ import annotations

import sys
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_DIR = ROOT / ".github" / "workflows"
sys.path.insert(0, str(ROOT / "scripts"))

import build_simple_page as bsp  # noqa: E402

# script filename -> the tracked artefacts running it writes.
BUILD_ARTEFACTS = {
    "build_simple_page.py": (
        bsp.OUT_PATH.relative_to(ROOT).as_posix(),
        bsp.PAYLOAD_PATH.relative_to(ROOT).as_posix(),
    ),
}


def _offenders(fragment: str) -> list[str]:
    hits = []
    for wf in sorted(WORKFLOW_DIR.glob("*.yml")):
        for lineno, line in enumerate(
            wf.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if line.lstrip().startswith("#"):
                continue
            if fragment in line and "||" in line:
                hits.append(f"{wf.name}:{lineno}: {line.strip()}")
    return hits


def test_workflow_directory_found():
    """If the glob breaks, the two scans below would pass vacuously."""
    assert WORKFLOW_DIR.is_dir(), f"missing workflow dir: {WORKFLOW_DIR}"
    assert list(WORKFLOW_DIR.glob("*.yml")), "no workflow files found"


def test_no_swallowed_git_push():
    offenders = _offenders("git push")
    assert not offenders, (
        "git push in a workflow must hard-fail (retry loop + exit 1), never "
        "fall through '||' — a green job on a rejected push freezes the "
        "published surface silently:\n" + "\n".join(offenders)
    )


def test_no_swallowed_git_add():
    offenders = _offenders("git add")
    assert not offenders, (
        "git add in a workflow must not carry a '||' fallback — a failed add "
        "feeds an empty staged-diff check and skips the publish silently:\n"
        + "\n".join(offenders)
    )


# --------------------------------------------------------------------------
# A workflow that builds an artefact must commit it
# --------------------------------------------------------------------------

def _live_lines(wf: Path) -> list[str]:
    """Non-comment lines, with backslash continuations joined.

    Continuations matter: scanner-daily already splits one add across two
    lines, so a token-wise scan that did not join them would read the tail
    of the path list as absent and pass the wrong way.
    """
    text = wf.read_text(encoding="utf-8").replace("\\\n", " ")
    return [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]


def _stages(artefact: str, lines: list[str]) -> bool:
    """Is `artefact` staged by some `git add` line — directly or by prefix?"""
    covering = {artefact, ".", "-A", "--all"}
    parent = PurePosixPath(artefact).parent
    while str(parent) != ".":
        covering |= {str(parent), f"{parent}/"}
        parent = parent.parent
    return any(
        set(line.split()) & covering
        for line in lines
        if "git add" in line
    )


def _builders() -> list[tuple[Path, str, tuple[str, ...]]]:
    found = []
    for wf in sorted(WORKFLOW_DIR.glob("*.yml")):
        lines = _live_lines(wf)
        for script, artefacts in BUILD_ARTEFACTS.items():
            if any(script in line for line in lines):
                found.append((wf, script, artefacts))
    return found


def test_build_scripts_are_actually_run_somewhere():
    """Without this, the staging assertion below would pass vacuously.

    A renamed script, a changed workflow glob or a builder invoked through
    a wrapper all empty the scan; each must fail loudly here rather than
    read as a clean sweep.
    """
    assert _builders(), (
        "no workflow runs any script in BUILD_ARTEFACTS — the mapping is "
        f"stale: {sorted(BUILD_ARTEFACTS)}"
    )


def test_built_artefacts_are_staged_by_the_workflow_that_builds_them():
    offenders = []
    for wf, script, artefacts in _builders():
        lines = _live_lines(wf)
        if not any("git add" in line for line in lines):
            continue  # a workflow that builds but never commits, e.g. Tests
        for artefact in artefacts:
            if not _stages(artefact, lines):
                offenders.append(f"{wf.name}: runs {script}, never stages {artefact}")

    assert not offenders, (
        "a workflow that rebuilds a published artefact must stage it, or the "
        "rebuild dies on the runner and the committed surface drifts behind "
        "its own sources — invisible to that job, because it rebuilds before "
        "it tests:\n" + "\n".join(offenders)
    )
