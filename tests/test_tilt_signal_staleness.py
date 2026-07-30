"""Guard: a stalled EEM/SPY tilt feed must read OFF on every surface.

Regression cover for the 2026-07-29 finding. ``em_regime_context.parquet``
froze on 2026-07-06. ``run_risk_overlay``'s money path behaved correctly —
it forces the tilt flat past ``EEM_MAX_STALE_DAYS`` and reverts to the
baseline 35/35/10/20 blend — and it stamped ``signal_stale`` in the payload
with a code comment telling consumers to treat it like the gate's
stale-panel banner. No consumer did. The live mark, factsheet and weekly
email each read ``current_state`` alone, which holds the LAST VALID reading
for display continuity, so for three weeks reporting published a 10% EEM
leg funded out of sleeve B while the blend itself ran untilted.

These tests pin the agreed resolution (mirror the money path, and badge the
reason) at the shared-convention layer, and assert that no reporting module
reaches for ``current_state`` directly again.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from overlay_state import (  # noqa: E402
    sleeve_nav_weights, tilt_active_on, tilt_display_state, tilt_signal_as_of,
    tilt_signal_stale, tilt_stale_on)

# Tilt turned ON on 2025-04-07 and never turned off — the live event log.
EVENTS = [{"date": "2025-04-07", "direction": "EM_TILT_ON"}]


def _overlay(signal_as_of: str | None, stale: bool) -> dict:
    return {
        "phase22_eem_tilt": {
            "enabled": True,
            "current_state": "EM_TILT_ON",
            "current_state_since": "2025-04-07",
            "signal_as_of": signal_as_of,
            "signal_stale": stale,
            "parameters": {"tilt_weight": 0.10},
            "events": EVENTS,
        },
    }


FRESH = _overlay("2026-07-24", False)
# The exact shape shipped for three weeks: last real bar 2026-07-06, plus
# the 10-day consumption cap, published as signal_as_of.
STALLED = _overlay("2026-07-16", True)


class TestStaleTiltReadsOff:
    def test_fresh_feed_reads_on(self):
        assert tilt_active_on(FRESH, "2026-07-24") is True
        assert tilt_stale_on(FRESH, "2026-07-24") is False

    def test_stalled_feed_reads_off_past_signal_as_of(self):
        assert tilt_active_on(STALLED, "2026-07-29") is False
        assert tilt_stale_on(STALLED, "2026-07-29") is True

    def test_dates_up_to_signal_as_of_still_rest_on_real_data(self):
        """signal_as_of itself is backed by an observation, so it is not
        stale — only the tail past it is. Guards against retroactively
        rewriting settled history when a feed later stalls."""
        assert tilt_active_on(STALLED, "2026-07-16") is True
        assert tilt_stale_on(STALLED, "2026-07-16") is False
        assert tilt_active_on(STALLED, "2025-06-02") is True

    def test_accessors(self):
        assert tilt_signal_as_of(STALLED) == "2026-07-16"
        assert tilt_signal_stale(STALLED) is True
        assert tilt_signal_stale(FRESH) is False
        assert tilt_signal_stale({}) is False
        assert tilt_stale_on({}, "2026-07-29") is False

    def test_missing_signal_fields_do_not_gate(self):
        """Overlays predating the D9 fix carry no signal_stale; they must
        keep their old behaviour rather than silently reading OFF."""
        legacy = {"phase22_eem_tilt": {
            "enabled": True, "current_state": "EM_TILT_ON",
            "events": EVENTS, "parameters": {"tilt_weight": 0.10}}}
        assert tilt_active_on(legacy, "2026-07-29") is True
        assert tilt_stale_on(legacy, "2026-07-29") is False


class TestWeightsMirrorTheMoneyPath:
    def test_stalled_feed_funds_sleeve_b_back_to_baseline(self):
        """The money path reverts to 35/35/10/20 with no EEM leg. Reported
        weights must match, or the factsheet shows a position the blend
        does not hold."""
        w = sleeve_nav_weights(STALLED, "2026-07-29")
        assert w["tilt_on"] is False
        assert w["tilt_nav"] == pytest.approx(0.0)
        assert w["b"] == pytest.approx(0.35)
        assert sum(w[k] for k in "abcd") == pytest.approx(1.0)

    def test_fresh_feed_keeps_the_tilt_funded_out_of_b(self):
        w = sleeve_nav_weights(FRESH, "2026-07-24")
        assert w["tilt_on"] is True
        assert w["tilt_nav"] == pytest.approx(0.10)
        assert w["b"] == pytest.approx(0.25)
        assert sum(w[k] for k in "abcd") + w["tilt_nav"] == pytest.approx(1.0)

    def test_derisk_and_stale_tilt_compose(self):
        """A stalled tilt inside a RISK_OFF week must not resurrect the EEM
        leg through the gate's scaler."""
        ov = dict(STALLED)
        ov["events"] = [{"date": "2026-07-20", "direction": "RISK_OFF"}]
        ov["gate_parameters"] = {"derisk_fraction": 0.50}
        w = sleeve_nav_weights(ov, "2026-07-29")
        assert w["derisk_on"] is True
        assert w["tilt_nav"] == pytest.approx(0.0)
        assert w["b"] == pytest.approx(0.175)
        assert w["shy_overlay"] == pytest.approx(0.50)


class TestDisplayBadging:
    def test_stale_label_names_the_date(self):
        d = tilt_display_state(STALLED, "2026-07-29")
        assert d["active"] is False and d["stale"] is True
        assert d["signal_as_of"] == "2026-07-16"
        assert "2026-07-16" in d["label"]
        assert "EM_TILT_ON" not in d["label"]

    def test_fresh_label_is_plain(self):
        assert tilt_display_state(FRESH, "2026-07-24")["label"] == "EM_TILT_ON"


class TestNoConsumerReadsCurrentStateDirectly:
    """The defect was four modules independently reading current_state.
    Keep them on the shared helper so one fix covers every surface."""

    CONSUMERS = ["mark_to_market_live.py", "build_factsheet.py",
                 "build_email_body.py"]
    PATTERN = re.compile(
        r"""current_state["']\s*\)?\s*==\s*["']EM_TILT_ON""")

    @pytest.mark.parametrize("name", CONSUMERS)
    def test_no_direct_em_tilt_on_comparison(self, name):
        src = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        hits = [ln for i, ln in enumerate(src.splitlines(), 1)
                if self.PATTERN.search(ln)]
        assert not hits, (
            f"{name} compares current_state to EM_TILT_ON directly: {hits}. "
            f"Use overlay_state.tilt_active_on / tilt_display_state so a "
            f"stalled feed reads OFF on every surface at once.")

    @pytest.mark.parametrize("name", CONSUMERS)
    def test_consumer_imports_the_shared_helper(self, name):
        src = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert "overlay_state import" in src, (
            f"{name} does not import overlay_state")
        assert ("tilt_display_state" in src or "tilt_stale_on" in src), (
            f"{name} imports overlay_state but not a freshness-aware tilt "
            f"helper — it cannot badge a stalled feed")


class TestEmailTiltCardFailsSafe:
    def test_stalled_feed_reads_off_and_drops_the_frozen_ratio(self):
        import build_email_body as be
        state, since, ratio = be._eem_tilt_state(STALLED, "2026-07-29")
        assert state == "EM_TILT_OFF"
        assert "2026-07-16" in since
        assert ratio is None, "current_ratio is frozen on a stale feed"

    def test_missing_asof_does_not_default_to_on(self):
        """No as-of date means freshness cannot be proven; a flagged overlay
        must read OFF rather than falling through to current_state."""
        import build_email_body as be
        assert be._eem_tilt_state(STALLED, None)[0] == "EM_TILT_OFF"
        assert be._eem_tilt_state(STALLED, "")[0] == "EM_TILT_OFF"

    def test_fresh_feed_unaffected(self):
        import build_email_body as be
        state, since, ratio = be._eem_tilt_state(FRESH, "2026-07-24")
        assert state == "EM_TILT_ON" and since == "2025-04-07"


class TestOverlayLoaderRefetchesStaleCache:
    """The upstream half of the fix: nothing re-fetched the cache, so the
    tilt outlived its data for three weeks."""

    def test_cache_age_cap_is_defined_and_tighter_than_consumption_cap(self):
        import run_risk_overlay as rro
        assert rro.EEM_MAX_CACHE_AGE_DAYS <= rro.EEM_MAX_STALE_DAYS, (
            "the refresh trigger must fire before the consumption cap, "
            "otherwise the tilt is held flat before anything re-fetches")
