"""AUDIT PROBE (implementation audit, 2026-07-04) — read-only demonstration.

Suspected defect (S2): the daily capture-integrity gate fails the whole
daily_live_track job on the FIRST trading day of each week, because
`mark_to_market_live._project_daily_equity` emits only dates strictly
AFTER the Friday anchor, so on Monday `live_track.json` carries a single
post-anchor point, and `check_capture_integrity.evaluate_target` treats
any series with `len(dates) < 2` as a malformed 'fail'.

What would FALSIFY the defect:
  (a) evaluate_target() on a single-point live series returns 'ok'/'warn'
      (not 'fail'), OR
  (b) _project_daily_equity() with a Friday anchor + a single following
      Monday close returns >= 2 dates (i.e. it includes the anchor).
Either result means Monday would clear the gate and the defect is unreal.

This probe demonstrates MECHANICS ONLY — no performance numbers. It writes
data/audit_capture_probe.json and prints a PASS/FALSIFIED verdict.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from check_capture_integrity import evaluate_target  # noqa: E402
from mark_to_market_live import _project_daily_equity  # noqa: E402


def probe_evaluate_single_point() -> dict:
    """Feed evaluate_target a synthetic one-point live series (a Monday)."""
    tmp = ROOT / "data" / "_audit_tmp_live_track.json"
    # A perfectly healthy single post-anchor point: sane date, sane 0.3% move.
    tmp.write_text(json.dumps({
        "live_dates": ["2026-07-06"],          # Monday, one session after Fri 3 Jul
        "live_equity": [1.0032],
    }), encoding="utf-8")
    try:
        verdict = evaluate_target(
            "Live track (synthetic Monday, 1 point)", tmp,
            ("live_dates",), ("live_equity",),
            expected=date(2026, 7, 6),
        )
    finally:
        tmp.unlink(missing_ok=True)
    return verdict


def probe_project_monday_point_count() -> dict:
    """Confirm a Friday anchor + a Monday close yields exactly ONE dated
    point (anchor excluded)."""
    idx = pd.to_datetime(["2026-07-03", "2026-07-06"])  # Fri anchor, Mon close
    prices = pd.DataFrame({"SPY": [500.0, 501.5]}, index=idx)
    weights = {"SPY": 1.0}
    dates_out, equity_out = _project_daily_equity(
        weights, anchor_equity=1.0, prices=prices,
        anchor_ts=pd.Timestamp("2026-07-03"),
    )
    return {"n_points_after_friday_anchor": len(dates_out), "dates": dates_out}


def main() -> int:
    single = probe_evaluate_single_point()
    proj = probe_project_monday_point_count()

    defect_confirmed = (
        single["status"] == "fail"
        and proj["n_points_after_friday_anchor"] == 1
    )

    out = {
        "probe": "capture-integrity Monday single-point",
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "evaluate_target_on_single_point": single,
        "project_daily_equity_monday": proj,
        "defect_confirmed": defect_confirmed,
        "reading": (
            "CONFIRMED: a healthy Monday live_track (1 post-anchor point) is "
            "graded 'fail' by evaluate_target, and _project_daily_equity does "
            "emit exactly 1 point on Monday — so the daily job's non-"
            "continue-on-error capture step returns 1 and the job dies before "
            "pipeline.py, firing a spurious [FAIL] email and skipping the "
            "Monday publish."
            if defect_confirmed else
            "FALSIFIED: Monday would clear the capture gate; defect unreal."
        ),
    }
    out_path = ROOT / "data" / "audit_capture_probe.json"
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps(out, indent=2))
    print(f"\nWrote {out_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
