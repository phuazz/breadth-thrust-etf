"""The held basis frozen before a component refresh must describe the book
AFTER the last fill, not before it.

2026-11-01: the post-fill runs of 2026-10-27..29 failed on a dirty tree, so
live_targets.json still carried the 2026-10-23 decision, whose "held" is the
book before the 2026-10-26 fill. prepare() froze that as this week's held
book, and every change for the 2026-11-02 fill was measured from it.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from component_basis import prepare  # noqa: E402


def book(as_of, fills, lines):
    return {"as_of": as_of, "next_fill": {"by_venue": fills},
            "sleeves": [{"sleeve": "B", "venue": "NYSE"}, {"sleeve": "D", "venue": "XETR"}],
            "lines": [{"sleeve": s, "etf": e, "held": h, "target": t} for s, e, h, t in lines]}


LINES = [("B", "SHY", 0.0, .05), ("B", "QQQ", .55, .50),
         ("D", "EXV1", .25, .20), ("D", "EXH1", .20, .25)]


def held(result):
    return {(r["sleeve"], r["etf"]): r["held"] for r in result["lines"]}


def run(tmp_path, data, anchor):
    (tmp_path / "live_targets.json").write_text(json.dumps(data), encoding="utf-8")
    return prepare(tmp_path, anchor)


def test_a_book_whose_fill_has_passed_is_held_at_its_targets(tmp_path):
    # Python dates are 1-indexed months; 2026-10-26 is a Monday.
    out = run(tmp_path, book("2026-10-23", {"NYSE": "2026-10-26", "XETR": "2026-10-26"}, LINES),
              "2026-10-30")
    assert held(out) == {("B", "SHY"): .05, ("B", "QQQ"): .50,
                         ("D", "EXV1"): .20, ("D", "EXH1"): .25}
    assert out["model_as_of"] == "2026-10-26"
    assert out["held_after_fill"] == {"book_as_of": "2026-10-23",
                                      "fills": {"NYSE": "2026-10-26", "XETR": "2026-10-26"}}


def test_a_post_fill_book_is_held_as_it_stands(tmp_path):
    # The ordinary week: the post-fill run republished on Tuesday, so the
    # next fill is after the anchor and "held" already includes the last fill.
    out = run(tmp_path, book("2026-10-27", {"NYSE": "2026-11-02", "XETR": "2026-11-02"}, LINES),
              "2026-10-30")
    assert held(out) == {("B", "SHY"): 0.0, ("B", "QQQ"): .55,
                         ("D", "EXV1"): .25, ("D", "EXH1"): .20}
    assert out["model_as_of"] == "2026-10-27" and "held_after_fill" not in out


def test_venues_roll_forward_independently(tmp_path):
    # A venue holiday can move one fill past the anchor while the other passed.
    lines = [("B", "SHY", 0.0, .05), ("B", "QQQ", .55, .50),
             ("D", "EXV1", .25, .25), ("D", "EXH1", .20, .20)]
    out = run(tmp_path, book("2026-10-23", {"NYSE": "2026-10-26", "XETR": "2026-11-02"}, lines),
              "2026-10-30")
    assert held(out)[("B", "SHY")] == .05 and held(out)[("D", "EXV1")] == .25
    assert out["held_after_fill"]["fills"] == {"NYSE": "2026-10-26"}


@pytest.mark.parametrize("as_of,fill,anchor", [
    ("2026-11-27", "2026-11-30", "2026-12-04"),   # month boundary
    ("2026-12-24", "2026-12-28", "2027-01-01"),   # year boundary
])
def test_rollover_across_month_and_year_boundaries(tmp_path, as_of, fill, anchor):
    out = run(tmp_path, book(as_of, {"NYSE": fill, "XETR": fill}, LINES), anchor)
    assert held(out)[("B", "SHY")] == .05 and out["model_as_of"] == fill


def test_a_fill_on_the_anchor_itself_has_happened(tmp_path):
    out = run(tmp_path, book("2026-10-29", {"NYSE": "2026-10-30", "XETR": "2026-10-30"}, LINES),
              "2026-10-30")
    assert held(out)[("B", "SHY")] == .05


def test_an_existing_basis_for_the_anchor_is_kept(tmp_path):
    first = run(tmp_path, book("2026-10-23", {"NYSE": "2026-10-26", "XETR": "2026-10-26"}, LINES),
                "2026-10-30")
    # Sunday's run after Saturday's: live_targets has moved on, the basis does not.
    later = book("2026-10-30", {"NYSE": "2026-11-02", "XETR": "2026-11-02"},
                 [(s, e, t, t) for s, e, _h, t in LINES])
    assert run(tmp_path, later, "2026-10-30") == first
