"""The next-fill panel must announce that it has not happened.

This is the ONLY forward-looking block on the dashboard. Every other card is a
record of what happened; this one is an intention, and the single way it can
mislead is by being read as a trade log. So the tests here are less about the
numbers than about the labelling and the date it claims to be for.

The fill date is derived from the SAME function the engines use, per venue,
because "the next Monday" is not the same thing: under holiday_aware_next a
holiday Monday rolls FORWARD, and the venues diverge when it does — 2026-09-07
is a NYSE holiday that pushes the US sleeves to the 8th while Xetra trades the
7th. Python months are 1-indexed and no weekday is computed by hand.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.live_targets import next_fill_date

TEMPLATE = Path("template.html")
BUILT = Path("docs/index.html")


# ---------------------------------------------------------------------------
# The fill date
# ---------------------------------------------------------------------------
def test_next_fill_is_the_coming_monday_from_a_sunday():
    now = datetime(2026, 8, 23, 6, 0, tzinfo=timezone.utc)   # Sunday
    assert next_fill_date("NYSE", now) == "2026-08-24"
    assert next_fill_date("XETR", now) == "2026-08-24"


def test_next_fill_is_strictly_in_the_future_on_the_fill_day_itself():
    """Asked ON Monday, the next fill is the FOLLOWING week, not today.
    Otherwise the card would show a trade already being placed as upcoming."""
    now = datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc)   # Monday
    assert next_fill_date("NYSE", now) == "2026-08-31"


def test_the_venues_diverge_over_a_one_sided_holiday():
    """7 Sep 2026 is a NYSE holiday and a normal Xetra session. A single
    'next Monday' would be wrong for one of the two sleeves."""
    now = datetime(2026, 9, 2, 6, 0, tzinfo=timezone.utc)    # Wednesday
    nyse, xetr = next_fill_date("NYSE", now), next_fill_date("XETR", now)
    assert xetr == "2026-09-07"
    assert nyse == "2026-09-08", "a NYSE holiday must roll the US sleeves on"
    assert nyse != xetr


def test_a_short_horizon_returns_none_rather_than_guessing():
    now = datetime(2026, 8, 25, 6, 0, tzinfo=timezone.utc)
    assert next_fill_date("NYSE", now, horizon_days=2) is None


# ---------------------------------------------------------------------------
# The artefact says it is unexecuted
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def targets():
    p = Path("data/live_targets.json")
    if not p.exists():
        pytest.skip("live_targets.json not built")
    return json.loads(p.read_text(encoding="utf-8"))


def test_the_artefact_declares_itself_unexecuted(targets):
    """`executed` is False by construction — this module only ever describes
    an INTENDED book. A consumer must be able to tell without inference."""
    assert targets.get("executed") is False


def test_the_artefact_names_the_fill_it_is_for(targets):
    nf = targets.get("next_fill") or {}
    assert nf.get("by_venue"), "must state the fill date per venue"
    for venue, d in nf["by_venue"].items():
        assert d is None or len(d) == 10, (venue, d)


def test_the_fill_is_after_the_close_it_was_ranked_on(targets):
    """Rank on the last close, fill on the next scheduled session. If the fill
    date were on or before the ranking close, the card would be describing a
    trade that could not have used the signal it claims."""
    nf = (targets.get("next_fill") or {}).get("by_venue") or {}
    for venue, fill in nf.items():
        if fill:
            assert fill > targets["as_of"], (venue, fill, targets["as_of"])


# ---------------------------------------------------------------------------
# The template cannot quietly stop saying so
# ---------------------------------------------------------------------------
def _template_text():
    if not TEMPLATE.exists():
        pytest.skip("template.html not present")
    return TEMPLATE.read_text(encoding="utf-8")


def test_the_card_carries_the_not_traded_sentence():
    """The load-bearing label. If this string goes, the card becomes
    indistinguishable from the rebalance-history card beside it."""
    assert "Nothing here has been traded" in _template_text()


def test_the_card_carries_a_planned_marker():
    t = _template_text()
    assert 'id="nf-pill"' in t
    assert "next-fill" in t


def test_the_renderer_hides_the_card_when_there_are_no_targets():
    """Absent data must render NOTHING. A card defaulting to the current book
    would assert that no trade is coming, which is a stronger and different
    claim than 'not computed'."""
    t = _template_text()
    assert 'id="nextfill-card" style="display:none"' in t
    assert "if (!lt || !Array.isArray(lt.lines) || !lt.lines.length) return;" in t


def test_the_renderer_calls_out_hold_sleeves_separately():
    """A HOLD sleeve's lines must not sit silently beside the actionable ones
    — that is how a sleeve that cannot be ranked gets traded anyway."""
    t = _template_text()
    assert "Do not trade" in t
    assert "nf-hold" in t


def test_omitted_drift_lines_are_counted_not_silently_dropped():
    t = _template_text()
    assert "NF_MIN_MOVE" in t
    assert "drift, not shown" in t


# ---------------------------------------------------------------------------
# The built page actually carries it
# ---------------------------------------------------------------------------
def test_the_built_page_carries_the_label_and_the_data():
    """Verify the RENDERED artefact, not the source: a build step that drops
    the block would leave the template correct and the page wrong."""
    if not BUILT.exists():
        pytest.skip("docs/index.html not built")
    # Never read the whole 7MB file into memory at once.
    found = {"sentence": False, "card": False, "data": False}
    with BUILT.open(encoding="utf-8", errors="replace") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), ""):
            if "Nothing here has been traded" in chunk:
                found["sentence"] = True
            if 'id="nextfill-card"' in chunk:
                found["card"] = True
            if '"live_targets":' in chunk:
                found["data"] = True
            if all(found.values()):
                break
    assert found["sentence"], "the not-traded label did not reach the page"
    assert found["card"], "the card markup did not reach the page"
    assert found["data"], "live_targets data did not reach the page"
