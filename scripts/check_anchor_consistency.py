"""Does the live track's anchor still match the curve it was taken from?

``mark_to_market_live.py`` copies the deployed curve's last value into
``live_track.json`` as ``anchor_equity`` and extends it with intraday marks.
``run_risk_overlay.py`` owns that curve, in
``risk_overlay.json -> gated_variants[<deployed_key>]``. The two run on
separate schedules, and nothing asserted they agreed.

That gap published a page carrying two vintages of the same number. The
2026-08-10 survivorship restatement (``b841f77``) walked the deployed curve
down at the 2026-08-07 anchor in three steps:

    computed 2026-08-08 23:59   3.0517
    computed 2026-08-09 10:52   3.0356
    computed 2026-08-10 00:53   2.9654
    computed 2026-08-10 12:18   2.9177   <- risk_overlay settled here

``live_track.json`` was not recomputed until 2026-08-10 22:05, so for roughly
thirty-six hours the published page asserted 2.9177 in its strategy block and
3.0517 in its live track -- a 4.4% disagreement about the same curve on the
same date. A downstream consumer reconciled against the stale half and
published performance overstated by that margin.

No existing guard could see it. Freshness checks compare DATES, and both files
carried the same anchor_date throughout; it was the VALUE that moved. Capture
integrity anchors series to the NYSE calendar, which was equally satisfied.
Only a cross-feed comparison catches a restatement that one feed has applied
and the other has not.

This runs in the daily workflow after the mark-to-market and before the
dashboard rebuild, so a split-vintage anchor fails the job rather than being
baked into a published artefact.

Exit codes: 0 consistent, 1 breach.
"""
from __future__ import annotations

import json
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"

# The anchor is a COPY of a curve value, not a recomputation, so the two should
# agree to the precision both files are written at (6dp). This tolerance is
# far tighter than any real restatement -- the smallest step in the incident
# above was 0.0161 -- and loose enough for JSON round-tripping.
TOL = 5e-6


def check_anchor(live_track: dict, risk_overlay: dict) -> tuple[str, str]:
    """Compare live_track's anchor against the deployed curve it came from.

    Pure so it can be unit-tested without touching data/. Returns
    (status, evidence) where status is "ok" or "fail".
    """
    key = live_track.get("deployed_key")
    if not key:
        return "fail", "live_track.json has no deployed_key"

    variants = risk_overlay.get("gated_variants") or {}
    curve = variants.get(key)
    if not curve:
        return "fail", (
            f"risk_overlay.json has no gated_variants['{key}'] -- the deployed "
            f"key changed without the overlay being rebuilt"
        )

    dates, equity = curve.get("dates") or [], curve.get("equity") or []
    if len(dates) != len(equity):
        return "fail", f"curve is malformed: {len(dates)} dates, {len(equity)} equity points"

    anchor_date = live_track.get("anchor_date")
    if anchor_date not in dates:
        return "fail", (
            f"anchor_date {anchor_date} is absent from the deployed curve "
            f"(curve runs {dates[0] if dates else '?'} to {dates[-1] if dates else '?'}) "
            f"-- the anchor points at a session the curve does not contain"
        )

    expected = equity[dates.index(anchor_date)]
    actual = live_track.get("anchor_equity")
    if not isinstance(actual, (int, float)):
        return "fail", "live_track.anchor_equity is missing or not numeric"

    gap = abs(actual - expected)
    stamps = (f"live_track computed {live_track.get('computed_at_utc', '?')}, "
              f"risk_overlay computed {risk_overlay.get('computed_at_utc', '?')}")
    if gap > TOL:
        pct = gap / expected * 100 if expected else float("nan")
        return "fail", (
            f"anchor {anchor_date}: live_track says {actual:.6f}, deployed curve "
            f"says {expected:.6f} -- {gap:.6f} apart ({pct:.2f}%). One feed has "
            f"applied a restatement the other has not. {stamps}"
        )
    return "ok", f"anchor {anchor_date} agrees at {actual:.6f}. {stamps}"


def main() -> int:
    live_track = json.loads((DATA / "live_track.json").read_text(encoding="utf-8"))
    risk_overlay = json.loads((DATA / "risk_overlay.json").read_text(encoding="utf-8"))
    status, evidence = check_anchor(live_track, risk_overlay)
    print(f"{status.upper():4s} anchor consistency: {evidence}")
    return 1 if status == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
