"""Commentary is derived, never written (2026-09-06).

Each sentence the weekly email and the dashboard carry about the planned
moves and the past week comes from a number on disk: the signal a sleeve
ranked on and the same signal at the previous decision, the rank and the
top-K cut, the held sleeve's reason, the blend and SPY over the same window
as the headline tile, weight × return per holding. These tests pin that the
text says what the data says and says nothing when the data is absent.

Python datetime months are 1-indexed (January = 1); the month and year
boundary windows are exercised as CLAUDE.md requires of any date logic.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import build_commentary as bc  # noqa: E402

LABELS = {"IUES": "iShares S&P 500 Energy", "IUMS": "iShares S&P 500 Materials",
          "IUSP": "iShares US Property Yield (REITs)", "DBC": "Broad Commodities",
          "CIBR": "Cybersecurity", "SHY": "1-3y Treasuries"}


def _lt():
    return {
        "as_of": "2026-09-04",
        "one_way_turnover": 0.0454,
        "next_fill": {"by_venue": {"NYSE": "2026-09-08", "XETR": "2026-09-07"}},
        "sleeves": [
            # Sleeve D-style absolute breadth, as fractions (the engines' unit).
            {"sleeve": "A", "status": "READY", "decision_session": "2026-09-04",
             "signal_kind": "breadth", "top_k": 7,
             "signals": {"IUES": 0.912, "IUSP": 0.700, "IUMS": 0.552, "IUFS": 0.906,
                         "IUHC": 0.800, "IUIS": 0.600, "IUCD": 0.580, "IUCS": 0.570, "IUUS": 0.200},
             "signals_prev": {"IUES": 0.889, "IUSP": 0.740, "IUMS": 0.610, "IUFS": 0.906,
                              "IUHC": 0.780, "IUIS": 0.590, "IUCD": 0.560, "IUCS": 0.550, "IUUS": 0.194}},
            {"sleeve": "B", "status": "READY", "decision_session": "2026-09-04",
             "signal_kind": "ma_distance", "top_k": 7,
             "signals": {"DBC": 0.161, "SPY": 0.05}, "signals_prev": {"DBC": 0.144, "SPY": 0.06}},
            {"sleeve": "C", "status": "HOLD", "decision_session": "2026-09-04",
             "reason": "decision row carries 25 of 26 names — below the 100% coverage floor"},
        ],
        "lines": [
            {"sleeve": "A", "etf": "IUES", "traded": "IUES", "held": 0.0977, "target": 0.1113,
             "delta": 0.0135, "status": "READY"},
            {"sleeve": "A", "etf": "IUMS", "traded": "IUMS", "held": 0.0100, "target": 0.0,
             "delta": -0.0100, "status": "READY"},
            {"sleeve": "A", "etf": "IUSP", "traded": "IUSP", "held": 0.0544, "target": 0.0426,
             "delta": -0.0118, "status": "READY"},
            {"sleeve": "B", "etf": "DBC", "traded": "DBC", "held": 0.0541, "target": 0.0623,
             "delta": 0.0082, "status": "READY"},
            {"sleeve": "B", "etf": "SHY", "traded": "SHY", "held": 0.0, "target": 0.0200,
             "delta": 0.0200, "status": "READY"},
            {"sleeve": "A", "etf": "IUFS", "traded": "IUFS", "held": 0.1056, "target": 0.1057,
             "delta": 0.0001, "status": "READY"},   # drift, below the 5bp floor
        ],
    }


# ---------------------------------------------------------------------------
# The planned moves
# ---------------------------------------------------------------------------
def test_moves_are_ordered_by_size_and_named_with_their_driver():
    nf = bc.moves_commentary(_lt(), LABELS)
    texts = [m["text"] for m in nf["moves"]]
    assert [m["etf"] for m in nf["moves"]] == ["SHY", "IUES", "IUSP", "IUMS", "DBC"]
    assert texts[1].startswith("IUES (iShares S&P 500 Energy) 9.8% → 11.1% of NAV (+1.4pp): "
                               "breadth 88.9% → 91.2%")
    assert "rank" in texts[1]


def test_an_exit_names_the_cut_it_fell_through():
    nf = bc.moves_commentary(_lt(), LABELS)
    exit_ = next(m for m in nf["moves"] if m["etf"] == "IUMS")
    assert exit_["action"] == "SELL ALL"
    assert exit_["text"].startswith("IUMS (iShares S&P 500 Materials) exits from 1.0% of NAV")
    assert "breadth 61.0% → 55.2%" in exit_["text"]
    # Previous row: IUFS 90.6, IUES 88.9, IUHC 78.0, IUSP 74.0, IUMS 61.0 -> 5th;
    # now 8th of 9, through the K=7 cut.
    assert "rank 5 → 8 of 9" in exit_["text"]
    assert "out of the top 7" in exit_["text"]


def test_a_price_momentum_sleeve_speaks_in_its_own_units():
    nf = bc.moves_commentary(_lt(), LABELS)
    dbc = next(m for m in nf["moves"] if m["etf"] == "DBC")
    assert "+14.4% → +16.1% against its 200-day average" in dbc["text"]
    assert dbc["signal_kind"] == "ma_distance"


def test_the_cash_proxy_is_explained_not_ranked():
    nf = bc.moves_commentary(_lt(), LABELS)
    shy = next(m for m in nf["moves"] if m["etf"] == "SHY")
    assert shy["action"] == "BUY"
    assert "cash proxy" in shy["text"] and "rank" not in shy["text"]


def test_drift_below_the_floor_is_not_a_sentence():
    nf = bc.moves_commentary(_lt(), LABELS)
    assert "IUFS" not in [m["etf"] for m in nf["moves"]]


def test_a_held_sleeve_gets_its_reason_and_the_summary_states_the_basis():
    nf = bc.moves_commentary(_lt(), LABELS)
    assert nf["holds"] == ["Sleeve C is held and its book is unchanged: decision row "
                           "carries 25 of 26 names — below the 100% coverage floor."]
    assert nf["summary"].startswith("5 moves at the next fill (NYSE Tue 8 Sep 2026, "
                                    "XETR Mon 7 Sep 2026), ranked on the Fri 4 Sep 2026 close")
    assert "one-way turnover 4.54% of NAV" in nf["summary"]
    assert "1 new name" in nf["summary"] and "1 exit" in nf["summary"]
    assert nf["decision_session"] == "2026-09-04"


def test_a_move_without_a_recorded_signal_is_stated_without_a_driver():
    """No number is invented: the move is stated bare and the gap is noted."""
    lt = _lt()
    lt["sleeves"][0]["signals"].pop("IUSP")
    nf = bc.moves_commentary(lt, LABELS)
    iusp = next(m for m in nf["moves"] if m["etf"] == "IUSP")
    assert iusp["text"].endswith("of NAV (-1.2pp).")
    assert any("IUSP" in n for n in nf["notes"])


def test_no_material_move_says_so():
    lt = _lt()
    lt["lines"] = [lt["lines"][-1]]
    nf = bc.moves_commentary(lt, LABELS)
    assert nf["moves"] == []
    assert nf["summary"].startswith("No move above 0.05pp of NAV")


def test_ranks_and_phrases():
    """Every signal arrives as a FRACTION in its sleeve's own unit: absolute
    breadth (D), sector-relative breadth (A, signed), distance from the
    200-day average (B and C). The first live run printed sleeve A's +0.21
    as "0.2%"; these pin the units."""
    r = bc._ranks({"A": 3.0, "B": 9.0, "C": None, "D": 1.0})
    assert r == {"B": 1, "A": 2, "D": 3}
    assert bc._signal_phrase("breadth", 0.552, 0.610, 8, 7, 9, 7) == \
        "breadth 61.0% → 55.2%, rank 7 → 8 of 9, out of the top 7"
    assert bc._signal_phrase("breadth", 0.75, 0.60, 3, 8, 9, 7) == \
        "breadth 60.0% → 75.0%, rank 8 → 3 of 9, into the top 7"
    assert bc._signal_phrase("breadth_relative", 0.214, 0.201, 1, 2, 14, 7) == \
        "breadth +20.1pp → +21.4pp against the sector average, rank 2 → 1 of 14"
    assert bc._signal_phrase("breadth_relative", -0.03, None, 9, None, 14, 7) == \
        "breadth -3.0pp against the sector average, rank 9 of 14"
    assert bc._signal_phrase("ma_distance", 0.05, None, 2, None, 9, None) == \
        "+5.0% against its 200-day average, rank 2 of 9"
    assert bc._signal_phrase("breadth", None, None, None, None, 0, None) == ""


# ---------------------------------------------------------------------------
# The week
# ---------------------------------------------------------------------------
def _hp(dates, **cols):
    return {"prices": {k: {"dates": dates, "prices": v} for k, v in cols.items()}}


WEEK_DATES = ["2026-08-28", "2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]


def test_week_review_states_blend_spy_sleeves_and_holdings():
    hp = _hp(WEEK_DATES,
             SPY=[100, 101, 102, 103, 104, 102],       # +2.0% on the window
             XLE=[50, 50, 51, 52, 53, 55],             # +10% from the Monday fill
             XLB=[20, 20, 19.8, 19.6, 19.4, 19],       # -5% from the Monday fill
             EEM=[40, 40, 41, 41, 41, 41])
    wtd = (0.006, "2026-08-28", "2026-09-04")
    att = {"rows": [{"label": "Sleeve A", "w": 0.35, "ret": 0.01, "contrib": 0.0035},
                    {"label": "Sleeve B", "w": 0.25, "ret": -0.004, "contrib": -0.001}]}
    holdings = [{"etf": "IUES", "sleeve": "A", "effective": 0.098},
                {"etf": "IUMS", "sleeve": "A", "effective": 0.010},
                {"etf": "NOPRICE", "sleeve": "C", "effective": 0.02}]
    overlay = {"current_state": "RISK_ON", "current_state_since": "2026-04-14",
               "current_breadth": 0.477}
    panel = {"series": {"dates": ["2026-08-27", "2026-08-28", "2026-09-04"],
                        "ma_breadth": [0.50, 0.45, 0.477]}}
    w = bc.week_commentary(wtd, att, holdings, hp, {"A": "2026-08-31"}, overlay, panel, LABELS)
    t = w["text"]
    assert t.startswith("Week Fri 28 Aug 2026 close → Fri 4 Sep 2026 close: the blend "
                        "returned +0.60% against SPY +2.00%.")
    assert "By sleeve: Sleeve A +0.35pp (+1.00% at 35% of NAV), Sleeve B -0.10pp" in t
    assert "Largest contributors: XLE (iShares S&P 500 Energy) +0.98pp (+10.0% at 9.8%)" in t
    assert "Largest detractors: XLB (iShares S&P 500 Materials) -0.05pp (-5.0% at 1.0%)" in t
    assert "from the Mon 31 Aug 2026 close to the Fri 4 Sep 2026 close, 2 of 3 holdings priced" in t
    assert "Regime RISK_ON since Tue 14 Apr 2026, S&P 500 breadth 47.7% above the 50-day average (+2.7pp on the week)." in t
    assert w["n_priced"] == 2 and w["n_held"] == 3
    assert w["spy_return"] == pytest.approx(0.02)


def test_week_review_omits_what_it_cannot_derive():
    hp = _hp(WEEK_DATES, XLE=[50, 50, 51, 52, 53, 55])   # no SPY
    w = bc.week_commentary((0.006, "2026-08-28", "2026-09-04"), None,
                           [{"etf": "IUES", "sleeve": "A", "effective": 0.098}],
                           hp, {}, None, None, LABELS)
    assert "against SPY" not in w["text"] and "By sleeve" not in w["text"]
    assert "Regime" not in w["text"]
    assert any("SPY" in n for n in w["notes"])
    assert w["text"].startswith("Week Fri 28 Aug 2026 close → Fri 4 Sep 2026 close: the blend returned +0.60%.")


def test_week_review_without_a_window_is_empty():
    w = bc.week_commentary(None, None, [], {}, {}, None, None, LABELS)
    assert w["text"] == "" and w["notes"]


@pytest.mark.parametrize("start, end, expect", [
    ("2026-08-28", "2026-09-04", "Week Fri 28 Aug 2026 close → Fri 4 Sep 2026 close"),   # month boundary
    ("2026-12-31", "2027-01-08", "Week Thu 31 Dec 2026 close → Fri 8 Jan 2027 close"),   # year boundary
])
def test_week_dates_are_spelled_by_the_date_library(start, end, expect):
    w = bc.week_commentary((0.0, start, end), None, [], {}, {}, None, None, LABELS)
    assert w["text"].startswith(expect)


def test_price_key_prefers_the_traded_symbol_then_the_registry_proxy():
    hp = {"prices": {"XLE": {"dates": WEEK_DATES, "prices": [1] * 6}}}
    assert bc._price_key("IUES", hp) == "XLE"            # UCITS line priced by its US proxy
    hp2 = {"prices": {"EXH4.DE": {"dates": WEEK_DATES, "prices": [1] * 6}}}
    assert bc._price_key("EXH3", hp2) == "EXH4.DE"       # panel id -> traded fund
    assert bc._price_key("IUES", {"prices": {}}) is None
