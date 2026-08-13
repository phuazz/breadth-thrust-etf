"""Workflow publish steps must not swallow git failures.

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
"""

from __future__ import annotations

from pathlib import Path

WORKFLOW_DIR = Path(__file__).resolve().parent.parent / ".github" / "workflows"


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
