"""Publish gate for the weekly factsheet email (2026-07-25).

Design directive (Zhenghao, 2026-07-25): the weekly factsheet email must
only go out AFTER the breadth panels have been refreshed for the week
just ended, so the emailed rebalance card carries the COMPLETE position
changes — A and D re-anchor only when the local refresh_all.py run
lands, and the old Friday 22:00 UTC cron therefore mailed a factsheet
whose A/D positions and regime flag were a week stale every single
week. weekly_factsheet.yml is now triggered by the refresh commit
(push touching data/breadth_csp1.json) and this script decides whether
that event is publishable.

Modes:
  --mode publish       (push / workflow_dispatch): should this run build
                       and email the factsheet?
  --mode sunday-check  (Sunday schedule): has the week's factsheet gone
                       out? If not, email the operator a warning while
                       there is still time to refresh before Monday's
                       hard guard freezes the builds.

Publish rules:
  anchor    = final NYSE session of the most recent completed trading
              week (nyse_sessions.week_final_anchor — holiday-aware, so
              a Friday-holiday week anchors on Thursday).
  current   = breadth_csp1.json end_date >= anchor
  published = docs/factsheet_published.json marker exists with
              anchor == this anchor. The marker is written by the
              workflow only after a successful non-trial email; the
              factsheet_meta.json as-of cannot serve here because the
              daily mark-to-market runs re-stamp it every weekday.
  released  = docs/factsheet_release.json carries approved_anchor == this
              anchor. Written by scripts/release_factsheet.py once the
              operator has reviewed the week (2026-08-08). Absent or
              unparseable means NOT released.
  publish   = current and (--allow-republish or (released and not published)).
              --allow-republish is passed on workflow_dispatch: dispatching
              the workflow IS the operator acting deliberately, so it does
              not additionally require the marker, and remains the way to
              force a re-send (trial or corrected).

Why the release marker exists: the gate could tell whether the panel was
CURRENT but not whether anyone had CHECKED it, so every refresh landing on
main emailed the distribution list automatically. Holding a send meant
disabling the workflow by hand around the push — a manual step easiest to
forget in exactly the week something is wrong.

Fail-safe: the gate guards an OUTWARD send to the distribution list, so
any internal error fails CLOSED (publish=false) — the opposite polarity
of check_freshness_headroom.py, which fails toward alerting. The
gate_error output lets the workflow email the operator about the error
itself, so a broken gate is never silent either.

Python datetime months are 1-indexed throughout (January = 1).

Outputs (stdout always; appended to $GITHUB_OUTPUT when set):
  publish     'true' | 'false'            (publish mode)
  warn        'true' | 'false'            (sunday-check mode)
  gate_error  'true' | 'false'
  anchor      ISO date of the week-final session
  summary     one line, used as an email subject tail
  detail      multi-line block, used as an email body
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

# Allow importing sibling scripts/ modules.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_freshness_headroom import (  # noqa: E402
    deadline_strings,
    first_failing_run_date,
)
from nyse_sessions import week_final_anchor  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PANEL = ROOT / "data" / "breadth_csp1.json"
DEFAULT_MARKER = ROOT / "docs" / "factsheet_published.json"

# Release marker (2026-08-08). The gate above knows whether the panel is
# CURRENT; it had no way to know whether anyone had CHECKED it. So any
# refresh landing on main emailed the distribution list automatically, and
# holding a send meant disabling the workflow by hand for the duration of
# the push — a manual step that is easy to forget in exactly the week it
# matters. This marker is the operator's countersignature: written by
# scripts/release_factsheet.py once the week's data has been reviewed.
DEFAULT_RELEASE = ROOT / "docs" / "factsheet_release.json"


def read_release_anchor(release_path: Path) -> date | None:
    """Anchor the operator has released for publication, or None.

    Absent or unreadable means NOT released. This fails closed by
    construction: the marker guards an outward send, so anything other than
    an explicit, parseable approval must hold the email.
    """
    try:
        payload = json.loads(release_path.read_text(encoding="utf-8"))
        return date.fromisoformat(str(payload["approved_anchor"]))
    except Exception:
        return None


def read_panel_end(panel_path: Path) -> date:
    blob = json.loads(panel_path.read_text(encoding="utf-8"))
    end_iso = blob.get("end_date")
    if not end_iso:
        raise RuntimeError(f"{panel_path.name} has no end_date field")
    return date.fromisoformat(end_iso)


def read_marker_anchor(marker_path: Path) -> date | None:
    """Anchor of the last published factsheet, or None when the marker
    does not exist yet (first activation) or is unreadable (treated as
    never-published: the worst case is one duplicate email, preferable
    to silently never publishing again)."""
    try:
        blob = json.loads(marker_path.read_text(encoding="utf-8"))
        return date.fromisoformat(blob["anchor"])
    except FileNotFoundError:
        return None
    except Exception:
        return None


def build_gate_report(
    mode: str,
    now_utc: datetime,
    panel_path: Path,
    marker_path: Path,
    allow_republish: bool = False,
    release_path: Path | None = None,
) -> dict:
    """Pure decision core, injectable clock for tests."""
    anchor = week_final_anchor(now_utc)
    panel_end = read_panel_end(panel_path)
    published_anchor = read_marker_anchor(marker_path)
    current = panel_end >= anchor
    published = published_anchor == anchor
    # No release_path means NOT released. This core is meant to be pure —
    # defaulting it to the real repo marker would make its verdict depend on
    # working-tree state and let a test pass or fail on which week it is run.
    # main() passes DEFAULT_RELEASE explicitly.
    released_anchor = read_release_anchor(release_path) if release_path else None
    released = released_anchor == anchor

    base_lines = [
        f"week-final anchor          : {anchor.isoformat()}",
        f"breadth_csp1.json end_date : {panel_end.isoformat()}"
        + (" (current)" if current else " (STALE - behind the anchor)"),
        f"last published anchor      : "
        + (published_anchor.isoformat() if published_anchor else "none (no marker)"),
        f"released for this anchor   : "
        + (released_anchor.isoformat() if released_anchor else "no (not released)"),
    ]

    if mode == "publish":
        # `allow_republish` is set only by workflow_dispatch, which IS the
        # operator acting deliberately — that act is itself the release, so
        # dispatch does not additionally require the marker. An automatic
        # push-triggered run does, which is the whole point: a refresh
        # landing on main must never email on its own.
        publish = current and (allow_republish or (released and not published))
        if publish:
            reason = (
                "panel current to the anchor"
                + ("; sent by operator dispatch" if allow_republish
                   else "; released for this anchor and not yet published")
            )
        elif not current:
            reason = "panel behind the anchor - refresh incomplete, holding the email"
        elif not released:
            reason = (
                "panel current but this anchor is NOT released - holding. "
                "Run scripts/check_publish_readiness.py, then "
                "scripts/release_factsheet.py to release, or dispatch the "
                "workflow manually to send now"
            )
        else:
            reason = "this week's factsheet was already published - not re-sending"
        summary = f"factsheet gate: publish={str(publish).lower()} ({reason})"
        detail = "\n".join(base_lines + [f"decision                   : {reason}"])
        return {
            "publish": publish,
            "warn": False,
            "anchor": anchor,
            "summary": summary,
            "detail": detail,
        }

    if mode == "sunday-check":
        warn = not published
        if not warn:
            summary = f"weekly factsheet for {anchor.isoformat()} already published"
            detail = "\n".join(base_lines)
        elif current:
            summary = (
                f"factsheet for {anchor.isoformat()} NOT published although the "
                f"panel is current - check the push-triggered run, or dispatch "
                f"'Weekly factsheet' manually"
            )
            detail = "\n".join(base_lines + [
                "state                      : refresh landed but no factsheet email went out.",
                "action                     : open the Actions tab, inspect the last 'Weekly factsheet'",
                "                             run, and re-dispatch via 'Run workflow' once fixed.",
            ])
        else:
            # Monday's hard guard freezes builds once the weekday lag
            # exceeds budget — quote that deadline so the warning carries
            # the same clock the operator already knows.
            fail_day = first_failing_run_date(panel_end, now_utc.date())
            utc_s, sgt_s = deadline_strings(fail_day)
            summary = (
                f"weekend refresh has not landed - no factsheet for "
                f"{anchor.isoformat()}; run refresh_all.py before {utc_s} ({sgt_s})"
            )
            detail = "\n".join(base_lines + [
                "state                      : refresh_all.py has not run this weekend; the factsheet",
                "                             email is held until it lands (push-triggered).",
                f"hard guard deadline        : {utc_s} = {sgt_s} - after this, daily builds abort too.",
                "action                     : run `python scripts/refresh_all.py` locally (~4.3 h),",
                "                             commit and push; the push publishes the factsheet.",
            ])
        return {
            "publish": False,
            "warn": warn,
            "anchor": anchor,
            "summary": summary,
            "detail": detail,
        }

    raise ValueError(f"unknown mode: {mode}")


def write_github_output(values: dict[str, str], detail: str) -> None:
    """Append step outputs for the workflow's conditional steps.
    No-op outside GitHub Actions."""
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        return
    with open(out, "a", encoding="utf-8") as fh:
        for key, val in values.items():
            fh.write(f"{key}={val}\n")
        fh.write("detail<<GATE_DETAIL_EOF\n")
        fh.write(detail.rstrip("\n") + "\n")
        fh.write("GATE_DETAIL_EOF\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Decides whether the weekly factsheet email may go "
        "out (publish mode) or whether the operator must be warned that "
        "it has not (sunday-check mode).",
    )
    parser.add_argument("--mode", choices=["publish", "sunday-check"], required=True)
    parser.add_argument("--panel", default=str(DEFAULT_PANEL))
    parser.add_argument("--marker", default=str(DEFAULT_MARKER))
    parser.add_argument("--allow-republish", action="store_true")
    parser.add_argument("--release", default=str(DEFAULT_RELEASE),
                         help="Operator release marker. Absent or not "
                              "matching the anchor holds the email.")
    args = parser.parse_args(argv)
    try:
        report = build_gate_report(
            args.mode,
            datetime.now(timezone.utc),
            Path(args.panel),
            Path(args.marker),
            allow_republish=args.allow_republish,
            release_path=Path(args.release),
        )
    except Exception as exc:  # fail CLOSED, but never silently
        summary = f"factsheet gate could not run: {exc}"
        print(f"GATE ERROR {summary}")
        write_github_output(
            {
                "publish": "false",
                "warn": "true",
                "gate_error": "true",
                "anchor": "",
                "summary": summary,
            },
            summary,
        )
        return 0

    print(report["detail"])
    write_github_output(
        {
            "publish": "true" if report["publish"] else "false",
            "warn": "true" if report["warn"] else "false",
            "gate_error": "false",
            "anchor": report["anchor"].isoformat(),
            "summary": report["summary"],
        },
        report["detail"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
