"""Pure-logic tests for the cross-feed anchor guard (2026-08-10).

The numbers in test_catches_the_split_vintage are the real ones from the
incident the check was written for: the 2026-08-10 survivorship restatement
moved the deployed curve to 2.917657 at the 2026-08-07 anchor while
live_track.json still carried the pre-restatement 3.051701.

The guard must distinguish two cases that look similar and are not:

  * both feeds restated  -> ok. Restating the record is legitimate and
    routine; the check has no opinion on the level.
  * one feed restated    -> fail. The published page would assert two
    different values for the same curve on the same date.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from check_anchor_consistency import TOL, check_anchor  # noqa: E402

KEY = "blend_35_35_10_20_gated_eem_tilted"


def _overlay(equity_at_anchor: float, computed: str = "2026-08-10T12:18:34+00:00") -> dict:
    return {
        "computed_at_utc": computed,
        "gated_variants": {
            KEY: {
                "dates": ["2026-08-05", "2026-08-06", "2026-08-07"],
                "equity": [2.90, 2.91, equity_at_anchor],
            }
        },
    }


def _track(anchor_equity: float, anchor_date: str = "2026-08-07") -> dict:
    return {
        "computed_at_utc": "2026-08-10 22:05 UTC",
        "deployed_key": KEY,
        "anchor_date": anchor_date,
        "anchor_equity": anchor_equity,
    }


def test_agreeing_anchor_passes():
    status, _ = check_anchor(_track(2.917657), _overlay(2.917657))
    assert status == "ok"


def test_catches_the_split_vintage():
    """The incident: live_track pre-restatement, risk_overlay post."""
    status, evidence = check_anchor(_track(3.051701), _overlay(2.917657))
    assert status == "fail"
    assert "3.051701" in evidence and "2.917657" in evidence
    assert "4.59%" in evidence, "the evidence should quantify the disagreement"


def test_a_restatement_applied_to_both_feeds_passes():
    """Restating the record is legitimate; only disagreement is a breach."""
    for level in (3.051701, 2.965360, 2.917657):
        status, _ = check_anchor(_track(level), _overlay(level))
        assert status == "ok", f"a consistent restatement to {level} should pass"


def test_tolerance_admits_rounding_but_not_a_real_move():
    ok_status, _ = check_anchor(_track(2.917657 + TOL / 2), _overlay(2.917657))
    assert ok_status == "ok"
    # The smallest step in the real restatement sequence was ~0.0161.
    bad_status, _ = check_anchor(_track(2.917657 + 0.0161), _overlay(2.917657))
    assert bad_status == "fail"


def test_anchor_date_absent_from_curve_fails():
    status, evidence = check_anchor(_track(2.917657, anchor_date="2026-08-08"), _overlay(2.917657))
    assert status == "fail"
    assert "absent" in evidence


def test_deployed_key_missing_from_overlay_fails():
    overlay = _overlay(2.917657)
    overlay["gated_variants"] = {"some_other_variant": overlay["gated_variants"][KEY]}
    status, evidence = check_anchor(_track(2.917657), overlay)
    assert status == "fail"
    assert KEY in evidence


@pytest.mark.parametrize("track", [{}, {"deployed_key": KEY}])
def test_malformed_live_track_fails_rather_than_passing_silently(track):
    status, _ = check_anchor(track, _overlay(2.917657))
    assert status == "fail"


def test_malformed_curve_fails():
    overlay = _overlay(2.917657)
    overlay["gated_variants"][KEY]["equity"] = [2.90, 2.91]     # one short
    status, evidence = check_anchor(_track(2.917657), overlay)
    assert status == "fail"
    assert "malformed" in evidence
