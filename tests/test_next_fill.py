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


def test_the_fill_day_itself_is_the_next_fill():
    """Asked ON Monday, the next fill is TODAY (2026-08-31, owner instruction).

    This previously asserted the opposite — that Monday's answer is the
    FOLLOWING Monday — on the reasoning that the trade is already being placed
    so showing it as upcoming would mislead. That holds after the trade is
    placed and not before it, and the card is read before: on the morning of
    the 31 August fill it announced a 7/8 September one and said nothing about
    the fill actually due that day. `executed` is False by construction in this
    module, which only ever describes an INTENDED book, so today cannot be
    mistaken for a completed trade.
    """
    now = datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc)   # Monday, a fill day
    assert next_fill_date("NYSE", now) == "2026-08-24"
    assert next_fill_date("XETR", now) == "2026-08-24"


def test_the_day_after_a_fill_moves_on():
    """The corollary, and the half the strict test got right: once the fill
    day has passed the card must advance, not cling to it."""
    now = datetime(2026, 8, 25, 6, 0, tzinfo=timezone.utc)   # Tuesday
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


# ---------------------------------------------------------------------------
# Provisional vs final (2026-08-26)
#
# The card is worth reading only when its weights are the ones that will
# actually trade. The engines rank at rd-1, so that is true exactly when the
# session it was ranked on IS the session before the fill. Mid-week it is not:
# on Wed 2026-08-26 the card showed targets ranked Tue 25 August for a Mon 31
# August fill, with three sessions still to come, every one of which re-ranks
# it. Same card, entirely different standing, and nothing said which.
# ---------------------------------------------------------------------------
from scripts.live_targets import decision_session_for  # noqa: E402


def test_a_monday_fill_is_ranked_on_the_friday():
    assert decision_session_for("NYSE", "2026-08-31") == "2026-08-28"
    assert decision_session_for("XETR", "2026-08-31") == "2026-08-28"


def test_a_holiday_moves_the_decision_session_off_the_previous_weekday():
    """2026-09-07 is Labor Day. The US fill rolls to Tuesday the 8th, but its
    decision session is the FRIDAY the 4th, not the holiday Monday — and Xetra,
    which trades the 7th, ranks on that same Friday."""
    assert decision_session_for("NYSE", "2026-09-08") == "2026-09-04"
    assert decision_session_for("XETR", "2026-09-07") == "2026-09-04"


def test_decision_session_across_the_year_boundary():
    """1 Jan 2027 is a holiday, so the Mon 4 Jan fill ranks on Thu 31 Dec."""
    assert decision_session_for("NYSE", "2027-01-04") == "2026-12-31"


def test_decision_session_across_a_month_boundary():
    assert decision_session_for("NYSE", "2026-09-01") == "2026-08-31"


def test_a_short_lookback_returns_none_rather_than_guessing():
    """Same contract as next_fill_date: no answer beats a wrong one."""
    assert decision_session_for("NYSE", "2026-08-31", lookback_days=0) is None


def test_targets_are_not_final_when_sessions_remain_before_the_fill(targets):
    """The live artefact must state its own standing, not leave it inferred."""
    assert "targets_final" in targets, "the artefact does not declare its standing"
    assert isinstance(targets["targets_final"], bool)
    ds = targets["next_fill"].get("decision_session")
    if ds and targets.get("as_of"):
        # The flag must agree with the dates it is derived from, in both
        # directions — a flag that can disagree with its own evidence is worse
        # than none, because the card is styled off it.
        assert targets["targets_final"] == (targets["as_of"] == ds)


def test_every_sleeve_names_the_close_its_fill_will_use(targets):
    for s in targets["sleeves"]:
        if s.get("fill_date"):
            assert s.get("decision_session_for_fill"), s["sleeve"]
            assert s["decision_session_for_fill"] < s["fill_date"], s["sleeve"]


# ---------------------------------------------------------------------------
# The collapsed state must not cost the card its safety labels
# ---------------------------------------------------------------------------
def test_collapsing_keeps_the_not_traded_sentence_in_both_states():
    """Both branches of the note carry it. Collapsing hides the TABLE, never
    the reason the card cannot be read as a trade log."""
    t = _template_text()
    assert t.count("Nothing here has been traded") >= 2, (
        "the provisional branch must carry the not-traded sentence too")


def test_the_collapsed_card_still_shows_header_note_and_pill():
    """Only the table, the net line and the hold note collapse."""
    t = _template_text()
    for hidden in ("#preview-nextfill-table", "#preview-nextfill-net", "#nf-hold-note"):
        assert f".prev-card.next-fill.nf-collapsed {hidden}" in t or hidden in t
    assert ".nf-collapsed .nf-note" not in t, "the note must never be collapsed"
    assert ".nf-collapsed .prev-card-h" not in t, "the header must never be collapsed"


def test_the_toggle_is_available_in_both_states():
    """Collapsing hides noise; it must never withhold the detail from a reader
    who wants it."""
    t = _template_text()
    assert 'id="nf-toggle"' in t
    assert "aria-expanded" in t and "aria-controls" in t
    assert "toggle.hidden = false;" in t


def test_the_card_is_expanded_only_when_the_targets_are_final():
    t = _template_text()
    assert "const isFinal = lt.targets_final === true;" in t
    assert "setOpen(isFinal);" in t


# ---------------------------------------------------------------------------
# Both bases, each named beside its number (2026-08-30)
#
# The primary pair is % of TOTAL NAV -- the tradeable size. The grey line
# restates the same move within its sleeve, because 12.5pp of NAV and 62.5%
# of sleeve D are the same order of magnitude and read as two different
# trades. Neither number may appear without its basis: an unlabelled weight
# is exactly how the NAV column got read as a sleeve column in the first
# place.
# ---------------------------------------------------------------------------
def test_the_numbers_carry_both_bases():
    t = _template_text()
    assert "% of total NAV" in t, "the header must name the primary basis"
    assert "nf-within" in t
    # The second basis names itself and its sleeve inline, on every row.
    assert "within ${_escapeHtml(l.sleeve)}" in t


def test_the_within_basis_is_skipped_where_it_is_meaningless():
    """TILT/GATE are single-instrument overlays -- 'within sleeve' is 100%
    by construction there and would print as information."""
    t = _template_text()
    assert "l.sleeve !== 'TILT' && l.sleeve !== 'GATE'" in t


def test_within_weights_and_nav_weights_agree(targets):
    """The renderer derives the held side of the within-sleeve basis as
    held/Σheld over the sleeve's lines. That is an identity, not an estimate,
    exactly while target == within x (sleeve NAV share) and each ranked
    sleeve's `within` weights sum to 1 -- so pin both on the live artefact.
    If a producer change breaks either, this fails before the card divides
    by the wrong denominator."""
    from collections import defaultdict

    tgt_sum = defaultdict(float)
    within_sum = defaultdict(float)
    for ln in targets["lines"]:
        assert "within" in ln, ln["etf"]
        tgt_sum[ln["sleeve"]] += ln["target"]
        within_sum[ln["sleeve"]] += ln["within"]
    for sleeve, s in within_sum.items():
        if sleeve in {"TILT", "GATE"}:
            continue
        assert abs(s - 1.0) < 1e-4 or s == 0.0, (
            f"sleeve {sleeve} within-weights sum to {s}: the sleeve is "
            f"neither fully allocated nor unranked")
    for ln in targets["lines"]:
        nav = tgt_sum[ln["sleeve"]]
        if nav > 0 and ln["sleeve"] not in {"TILT", "GATE"}:
            assert abs(ln["target"] - ln["within"] * nav) < 1e-6, ln


# ---------------------------------------------------------------------------
# A row that reaches the last close but arrives hollow (2026-08-30)
#
# _rank() verified the decision row REACHES the venue's last completed
# session, never that it is POPULATED. On 2026-08-30 sleeve A's 2026-08-28
# row carried 5 of its 14 names (the 27th had carried all 14) and still
# ranked READY: top-K of whatever published put 35% of NAV into IDP6 alone
# at 50.06% one-way turnover, and sleeve D's 3-of-5 row silently ejected
# EXV3. The run was caught by hand and the artefact discarded. The known
# cause sits upstream — the vendor withholds the newest non-US session —
# but the card cannot wait on vendor fixes: a partial row is a different
# signal, not a smaller one.
#
# The dates below are library-verified: 2026-08-28 is a Friday and the last
# completed NYSE session at Sat 2026-08-29 12:00 UTC, and 17–28 Aug 2026
# holds no NYSE holiday, so business days and sessions coincide. No date
# arithmetic here — the guard is coverage arithmetic; the month- and
# year-boundary cases for this file's date logic sit above.
# ---------------------------------------------------------------------------
import pandas as pd  # noqa: E402

from scripts.live_targets import ROW_COVERAGE_FLOOR, _rank  # noqa: E402


def _panel(populated_last: int, total: int = 14) -> pd.DataFrame:
    """Ten sessions of breadth ending Fri 2026-08-28, fully populated except
    the final row, which carries only the first ``populated_last`` names."""
    idx = pd.bdate_range("2026-08-17", "2026-08-28")
    df = pd.DataFrame({f"N{i:02d}": 0.9 - 0.05 * i for i in range(total)},
                      index=idx)
    df.iloc[-1, populated_last:] = float("nan")
    return df


def _must_not_rank(row):
    raise AssertionError("the guard must refuse before any weight is computed")


def _top3(row):
    top = row.sort_values(ascending=False).head(3)
    return top / top.sum()


def test_a_partial_decision_row_reports_hold_and_names_the_coverage():
    """The failure mode is silent: a 5-of-14 row ranks cleanly, top-K simply
    selects from the five names that published. So the guard must refuse
    BEFORE the weight function — a book ranked and then discarded is exactly
    the artefact that gets trusted — and the reason must name the coverage,
    because 'HOLD' alone does not tell the operator how hollow the row is."""
    now = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)   # Saturday
    r = _rank(_panel(5), _must_not_rank, "NYSE", now, "A")
    assert r["status"] == "HOLD"
    assert "carries 5 of 14 names" in r["reason"]
    assert r["weights"] == {}
    assert r["decision_session"] == "2026-08-28", (
        "the HOLD must still name the row that failed, or the operator "
        "cannot check it against the vendor")


def test_a_full_decision_row_still_ranks():
    """The guard must not become a standing hold: a fully populated row at
    the venue's last close ranks exactly as it did before the guard."""
    now = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)   # Saturday
    r = _rank(_panel(14), _top3, "NYSE", now, "A")
    assert r["status"] == "READY"
    assert r["reason"] is None
    assert r["decision_session"] == "2026-08-28"
    assert len(r["weights"]) == 3
    # Published weights are rounded to 6 dp in the artefact, so a three-way
    # split sums to 1 only within that rounding.
    assert abs(sum(r["weights"].values()) - 1.0) < 1e-5


def test_the_default_floor_is_the_full_row():
    """One missing name already re-ranks the sleeve on a different universe,
    so the default floor is 1.0, not a ratio: 13 of 14 still holds. The
    repository's other coverage floor (0.85) was calibrated while the ITWN
    bug was depressing the measurement and proved too loose once the bug was
    fixed (2026-08-16) — a floor tuned around a defect is no precedent."""
    assert ROW_COVERAGE_FLOOR == 1.0
    now = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)   # Saturday
    r = _rank(_panel(13), _must_not_rank, "NYSE", now, "A")
    assert r["status"] == "HOLD"
    assert "carries 13 of 14 names" in r["reason"]
