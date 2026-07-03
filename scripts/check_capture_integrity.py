"""Capture-integrity check: did this run actually capture the data it
believes it captured?

Closes the silent-failure class the freshness guard cannot see: a run
that SUCCEEDS while the fetched series quietly stopped at an old
session (partial yfinance capture, cached feed) or contains a corrupt
print. Process status says green; the data is wrong. This check
anchors each series this run just refreshed to the TRUE NYSE calendar
(scripts/nyse_sessions.py) and bounds the newest datapoint, so a bad
capture fails the job BEFORE the dashboard or factsheet is built.

Verdicts per series:
  ok    series ends on the last completed NYSE session
  warn  exactly 1 session behind — upstream may simply be slow to post;
        publish proceeds (cadence rule: always publish the latest
        populated close) but an email goes to the operator
  fail  2+ sessions behind, corrupt tail, or an implausible last return
        -> exit 1, the job fails, the failure alert email fires

Which series are validated depends on which the invoking workflow just
refreshed (--targets): the daily job only re-fetches the live track;
checking Strategy B/C there would false-alarm mid-week by design (they
are weekly-cadence, extended daily via the live splice).

Python datetime months are 1-indexed (January = 1). Printed strings are
plain ASCII (local consoles may not be UTF-8).
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
from nyse_sessions import last_completed_session, sessions_behind  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

# A one-day move beyond this on a STRATEGY-LEVEL equity series is a data
# error, not a market move: these are diversified multi-ETF sleeves whose
# worst true days (March 2020) were far inside this bound. Per-ticker
# moves are deliberately not bounded here — single names can gap.
RETURN_BOUND = 0.15

# (label, file, dates JSON path, equity JSON path)
TARGETS = {
    "b": ("Strategy B (asset-class)", "asset_class_rotation.json",
          ("headline", "headline_equity_dates"), ("headline", "headline_equity")),
    "c": ("Strategy C (thematic)", "thematic_rotation.json",
          ("headline", "headline_equity_dates"), ("headline", "headline_equity")),
    "live": ("Live track", "live_track.json",
             ("live_dates",), ("live_equity",)),
}
TARGET_SETS = {"all": ("b", "c", "live"), "live": ("live",)}


def _dig(blob: dict, path: tuple[str, ...]) -> list:
    node = blob
    for key in path:
        node = node[key]
    return node


def evaluate_target(
    label: str, path: Path, dates_path: tuple[str, ...],
    equity_path: tuple[str, ...], expected: date,
) -> dict:
    """Verdict dict for one series: {label, status, evidence}."""
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
        dates = _dig(blob, dates_path)
        equity = _dig(blob, equity_path)
    except Exception as exc:
        return {"label": label, "status": "fail",
                "evidence": f"unreadable ({path.name}): {exc}"}
    if len(dates) < 2 or len(dates) != len(equity):
        return {"label": label, "status": "fail",
                "evidence": f"malformed series: {len(dates)} dates, "
                            f"{len(equity)} equity points"}
    if dates[-1] <= dates[-2]:
        return {"label": label, "status": "fail",
                "evidence": f"non-increasing tail dates: {dates[-2]} -> {dates[-1]}"}
    try:
        last_ret = equity[-1] / equity[-2] - 1.0
    except (TypeError, ZeroDivisionError) as exc:
        return {"label": label, "status": "fail",
                "evidence": f"corrupt equity tail: {equity[-2:]!r} ({exc})"}
    if abs(last_ret) > RETURN_BOUND:
        return {"label": label, "status": "fail",
                "evidence": f"implausible last daily return {last_ret:+.1%} "
                            f"(bound {RETURN_BOUND:.0%}) on {dates[-1]}"}
    lag = sessions_behind(date.fromisoformat(dates[-1]), expected)
    if lag >= 2:
        status = "fail"
    elif lag == 1:
        status = "warn"
    else:
        status = "ok"
    return {"label": label, "status": status,
            "evidence": f"ends {dates[-1]}, {lag} session(s) behind expected "
                        f"{expected.isoformat()}, last return {last_ret:+.2%}"}


def evaluate_targets(keys: tuple[str, ...], expected: date) -> list[dict]:
    results = []
    for key in keys:
        label, fname, dpath, epath = TARGETS[key]
        results.append(
            evaluate_target(label, DATA_DIR / fname, dpath, epath, expected)
        )
    return results


def write_github_output(values: dict[str, str], detail: str) -> None:
    """Append step outputs for the conditional warn-email step. No-op
    outside GitHub Actions."""
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        return
    with open(out, "a", encoding="utf-8") as fh:
        for key, val in values.items():
            fh.write(f"{key}={val}\n")
        fh.write("detail<<CAPTURE_DETAIL_EOF\n")
        fh.write(detail.rstrip("\n") + "\n")
        fh.write("CAPTURE_DETAIL_EOF\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Anchor freshly-captured series to the NYSE calendar; "
        "fail the job on silent capture corruption.",
    )
    parser.add_argument("--targets", choices=sorted(TARGET_SETS), default="all",
                        help="which series THIS workflow just refreshed")
    args = parser.parse_args(argv)

    expected = last_completed_session(datetime.now(timezone.utc))
    results = evaluate_targets(TARGET_SETS[args.targets], expected)

    worst = ("ok" if all(r["status"] == "ok" for r in results)
             else "fail" if any(r["status"] == "fail" for r in results)
             else "warn")
    lines = [f"expected last completed NYSE session: {expected.isoformat()}"]
    lines += [f"{r['status'].upper():5s} {r['label']}: {r['evidence']}"
              for r in results]
    detail = "\n".join(lines)
    print(detail)

    flagged = [r["label"] for r in results if r["status"] != "ok"]
    summary = (f"capture {worst}: {', '.join(flagged)}" if flagged
               else "capture ok")
    write_github_output(
        {"capture_warn": "true" if worst == "warn" else "false",
         "capture_status": worst, "summary": summary},
        detail,
    )
    # fail -> non-zero exit fails the job BEFORE the dashboard/factsheet
    # build, so a corrupt capture is never published. The failure-alert
    # email step then fires. warn/ok publish normally.
    return 1 if worst == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
