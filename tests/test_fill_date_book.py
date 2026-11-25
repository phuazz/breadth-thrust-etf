"""The fill-date book: the model holds the decided book from the fill (2026-11-25).

WHY. The model assumes every fill is executed in full. The engines record a
rebalance only where the fill session's close is in their price panel, so a
close that arrives late kept the previous book on screen: after the fill of
Mon 23 Nov 2026 sleeve D's tables read "as of the 2026-11-16 rebalance" until
25 Nov, because Monday's Xetra closes were not served at the refresh hour. The
page now shows the verified release's book for a fill whose session has
completed and which the engine has not yet priced, and says the fill close is
not yet priced.

WHAT COULD BE SILENTLY WRONG, and the tests that pin each:
  1. Showing a book the model does not hold: a release that is not final, a
     sleeve on an authorised HOLD, a provisional ranking, or a fill whose
     session has not completed on the sleeve's venue. Each must leave the
     engine's record in place.
  2. Showing the release after the engine has priced the fill. The engine's
     record is the one the return series is built on, so it must win.
  3. A release book printed as if it were priced. Every surface must say the
     fill close is not yet priced, and must not say "book unchanged since".

The sleeve D figures are the 23 Nov 2026 fill as published on origin/main on
24 Nov 2026: the engine record of the 16 Nov fill, the release computed
2026-11-22 10:25 UTC and the live_targets row of 24 Nov. The shipped template
functions run under Node; nothing is transcribed.

Dates are ISO strings compared as the template compares them. Python datetime
months are 1-indexed (January = 1); the weekdays the fixtures rely on are
asserted from the date library below.
"""
from __future__ import annotations

import copy
import datetime
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "template.html"
sys.path.insert(0, str(ROOT / "scripts"))

HARNESS = r"""
const vm = require('vm');
const { src, cases } = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const out = {};
for (const c of cases) {
  const els = {};
  const el = (id) => (els[id] = els[id] || {
    id, innerHTML: '', textContent: '', _q: {},
    querySelector(sel) { return (this._q[sel] = this._q[sel] || { innerHTML: '' }); },
  });
  const RealDate = Date;
  class FixedDate extends RealDate {
    constructor(...a) { if (a.length) super(...a); else super(c.now); }
  }
  const ctx = {
    Date: FixedDate, Math, JSON, Object, Array, Set, Number, String,
    window: { DATA: c.data },
    document: { getElementById: el },
    fmtDateLongSafe: (d) => `<${d}>`,
    _displaySym: (e) => e,
    _etfSwatch: () => '',
    _sleeveExpandAttrs: (e) => ({ attrs: `class="holdings-row" data-etf="${e}"`, caret: '' }),
    _initHoldingsClickExpand: () => {},
    _bcLabel: () => '',
    _bcColour: () => '#999',
    ETF_DESCRIPTIONS: {},
  };
  vm.createContext(ctx);
  vm.runInContext(src, ctx);
  const rec = ctx._heldRebalance(c.sleeve);
  const res = { record: rec ? {
    date: rec.date, decision_date: rec.decision_date, basis: rec.basis || 'engine',
    engine_date: rec.engine_date === undefined ? null : rec.engine_date,
    holdings: rec.holdings,
  } : null };
  if (c.render) {
    ctx[`_render${c.sleeve}Holdings`]();
    const id = c.sleeve.toLowerCase();
    res.asof = el(`${id}-holdings-asof`).textContent;
    res.tbody = el(`${id}-holdings-table`).querySelector('tbody').innerHTML;
    res.note = el(`${id}-holdings-note`).innerHTML;
    res.pending = el(`${id}-pending-selection`).innerHTML;
  }
  out[c.name] = res;
}
process.stdout.write(JSON.stringify(out));
"""

# --- sleeve D, fill of Mon 23 Nov 2026, as published on 24 Nov 2026 --------
D_SIG = {"EXV1": 0.6389, "EXH9": 0.5143, "EXH1": 0.4545, "EXH3": 0.4406, "EXV3": 0.4151}
D_SIG_PREV = {"EXV1": 0.8333, "EXH9": 0.7143, "EXH1": 0.697, "EXH3": 0.5986, "EXV3": 0.5849}
D_WEIGHTS = {"EXV1": 0.3974, "EXH9": 0.3199, "EXH1": 0.2827}
D_ENGINE = {"date": "2026-11-16", "decision_date": "2026-11-13", "holdings": [
    {"etf": "EXV1", "weight": 0.3713, "breadth_pct": 83.3},
    {"etf": "EXH9", "weight": 0.3182, "breadth_pct": 71.4},
    {"etf": "EXH1", "weight": 0.3105, "breadth_pct": 69.7}]}

B_SIG = {"DBC": -0.0142, "EFA": -0.0639, "EWJ": -0.0923, "GLD": -0.2044,
         "IEF": -0.0045, "IJR": -0.0447, "QQQ": -0.1079, "SPY": -0.0554,
         "TIP": -0.0089, "TLT": -0.0142, "VGK": -0.0522, "VNQ": -0.0133}
B_ENGINE = {"date": "2026-11-16", "decision_date": "2026-11-13", "holdings": [
    {"etf": "DBC", "weight": 0.2083, "signal_pct": 4.0},
    {"etf": "SPY", "weight": 0.1776, "signal_pct": 3.4},
    {"etf": "VNQ", "weight": 0.156, "signal_pct": 3.0},
    {"etf": "SHY", "weight": 0.1429, "signal_pct": 0.4},
    {"etf": "IJR", "weight": 0.1298, "signal_pct": 2.5},
    {"etf": "VGK", "weight": 0.1113, "signal_pct": 2.2},
    {"etf": "EFA", "weight": 0.0741, "signal_pct": 1.4}]}

A_SIG = {"CNDX": -0.0241, "CSP1": 0.041, "IDP6": -0.0903, "IUCD": 0.0339,
         "IUCM": -0.2034, "IUCS": 0.3302, "IUES": -0.0374, "IUFS": 0.081,
         "IUHC": 0.3469, "IUIS": -0.0428, "IUMS": -0.0206, "IUSP": -0.0768,
         "IUUS": 0.0239, "SOXX": -0.3615}
A_WEIGHTS = {"CSP1": 0.0478, "IUCD": 0.039589, "IUCS": 0.385356,
             "IUFS": 0.094498, "IUHC": 0.404807, "IUUS": 0.02795}
A_AVG = 44.48
A_LEVEL = {e: round(A_AVG + 100 * v, 2) for e, v in A_SIG.items()}
A_ENGINE = {"date": "2026-11-16", "decision_date": "2026-11-13", "holdings": [
    {"etf": "IUHC", "weight": 0.6, "breadth_pct": 80.0},
    {"etf": "IUCS", "weight": 0.4, "breadth_pct": 70.0}]}

ROOTS = {"A": "topk", "B": "asset_class", "C": "thematic", "D": "europe"}


def _data(sleeve: str, *, engine: dict, row: dict, live: dict,
          final: bool = True, held: list[str] | None = None,
          release: bool = True, ma200: dict | None = None, K: int = 3) -> dict:
    data = {
        ROOTS[sleeve]: {"headline": {"K": K, "latest_rebalance": copy.deepcopy(engine),
                                     "trade_history": [copy.deepcopy(engine)]}},
        "live_targets": {"computed_at_utc": "2026-11-24T04:23:00.682802+00:00",
                         "as_of": live.get("last_completed_session"),
                         "lines": [], "sleeves": [dict(live, sleeve=sleeve)]},
    }
    if release:
        data["component_release"] = {
            "identity": "cbb8e4b6", "component": "europe", "held_sleeves": held or [],
            "book": {"as_of": "2026-11-20",
                     "computed_at_utc": "2026-11-22T10:25:23.965004+00:00",
                     "targets_final": final, "sleeves": [dict(row, sleeve=sleeve)]}}
    if ma200 is not None:
        data["ma200"] = ma200
    return data


def _d_row(**over) -> dict:
    row = {"venue": "XETR", "status": "READY", "decision_session": "2026-11-20",
           "decision_session_for_fill": "2026-11-20", "fill_date": "2026-11-23",
           "signals": dict(D_SIG), "weights": dict(D_WEIGHTS), "top_k": 3, "gate": None}
    row.update(over)
    return row


def _d_live(**over) -> dict:
    live = {"venue": "XETR", "status": "HOLD", "decision_session": "2026-11-20",
            "decision_session_prev": "2026-11-13", "decision_session_for_fill": "2026-11-27",
            "fill_date": "2026-11-30", "last_completed_session": "2026-11-23",
            "signals": dict(D_SIG), "signals_prev": dict(D_SIG_PREV),
            "weights": dict(D_WEIGHTS), "top_k": 3, "gate": None}
    live.update(over)
    return live


def _span() -> str:
    t = TEMPLATE.read_text(encoding="utf-8")
    m = re.search(r"\nfunction _renderAHoldings\(.*?(?=\n// -{5,} Portfolio tab)", t, re.S)
    assert m, "sleeve renderers not found ahead of the Portfolio tab marker"
    return m.group(0)


def _run(cases: list[dict]) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    for c in cases:
        c.setdefault("now", "2026-11-24T05:00:00Z")
        c.setdefault("render", False)
    r = subprocess.run([node, "-e", HARNESS], input=json.dumps({"src": _span(), "cases": cases}),
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert r.returncode == 0, r.stderr[:3000]
    return json.loads(r.stdout)


def test_the_fixture_dates_are_the_weekdays_the_incident_had():
    # weekday(): Monday = 0. Read from the date library, not from memory.
    assert datetime.date(2026, 11, 13).weekday() == 4      # Friday, decision of the 16 Nov fill
    assert datetime.date(2026, 11, 16).weekday() == 0      # Monday, the fill the engine still showed
    assert datetime.date(2026, 11, 20).weekday() == 4      # Friday, decision of the 23 Nov fill
    assert datetime.date(2026, 11, 23).weekday() == 0      # Monday, the fill the model had made
    assert datetime.date(2026, 11, 30).weekday() == 0      # Monday, month-boundary fill
    assert datetime.date(2026, 12, 31).weekday() == 3      # Thursday, year-boundary decision
    assert datetime.date(2027, 1, 4).weekday() == 0        # Monday, year-boundary fill


# ---------------------------------------------------------------------------
# The resolver
# ---------------------------------------------------------------------------
def test_sleeve_d_shows_the_release_book_once_the_fill_session_has_completed():
    """The 24 Nov 2026 instance: the engine still records the 16 Nov fill."""
    got = _run([{"name": "d", "sleeve": "D",
                 "data": _data("D", engine=D_ENGINE, row=_d_row(), live=_d_live())}])["d"]["record"]
    assert got["basis"] == "release"
    assert (got["date"], got["decision_date"], got["engine_date"]) == (
        "2026-11-23", "2026-11-20", "2026-11-16")
    assert [(h["etf"], h["weight"], h["breadth_pct"]) for h in got["holdings"]] == [
        ("EXV1", 0.3974, 63.9), ("EXH9", 0.3199, 51.4), ("EXH1", 0.2827, 45.5)]


@pytest.mark.parametrize("name, kwargs", [
    # The engine has priced the fill: its record wins.
    ("engine_current", {"engine": dict(D_ENGINE, date="2026-11-23", decision_date="2026-11-20")}),
    # The fill session has not completed on the sleeve's venue.
    ("session_open", {"live": _d_live(last_completed_session="2026-11-20")}),
    ("no_reach", {"live": _d_live(last_completed_session=None)}),
    # The release is not the model's instruction for this sleeve.
    ("not_final", {"final": False}),
    ("authorised_hold", {"held": ["D"]}),
    ("sleeve_held_in_release", {"row": _d_row(status="HOLD")}),
    ("provisional_ranking", {"row": _d_row(decision_session_for_fill="2026-11-27")}),
    ("no_fill_date", {"row": _d_row(fill_date=None)}),
    ("empty_book", {"row": _d_row(weights={})}),
    ("no_release", {"release": False}),
])
def test_the_engine_record_stands_whenever_the_release_is_not_the_held_book(name, kwargs):
    args = {"engine": D_ENGINE, "row": _d_row(), "live": _d_live()}
    args.update(kwargs)
    got = _run([{"name": name, "sleeve": "D", "data": _data("D", **args)}])[name]["record"]
    assert got["basis"] == "engine"
    assert got["date"] == args["engine"]["date"]
    assert got["holdings"] == args["engine"]["holdings"]


@pytest.mark.parametrize("fill, decision, reached, engine_date, expect", [
    # Month boundary: fill Mon 30 Nov, read on a payload that reaches 1 Dec.
    ("2026-11-30", "2026-11-27", "2026-12-01", "2026-11-23", "release"),
    ("2026-11-30", "2026-11-27", "2026-11-27", "2026-11-23", "engine"),
    # Year boundary: decision Thu 31 Dec 2026, fill Mon 4 Jan 2027.
    ("2027-01-04", "2026-12-31", "2027-01-04", "2026-12-28", "release"),
    ("2027-01-04", "2026-12-31", "2026-12-31", "2026-12-28", "engine"),
    # The engine catches up across the year end.
    ("2027-01-04", "2026-12-31", "2027-01-05", "2027-01-04", "engine"),
])
def test_the_rule_holds_across_a_month_and_a_year_boundary(fill, decision, reached,
                                                          engine_date, expect):
    data = _data("D", engine=dict(D_ENGINE, date=engine_date),
                 row=_d_row(fill_date=fill, decision_session=decision,
                            decision_session_for_fill=decision),
                 live=_d_live(last_completed_session=reached))
    got = _run([{"name": "x", "sleeve": "D", "data": data}])["x"]["record"]
    assert got["basis"] == expect
    assert got["date"] == (fill if expect == "release" else engine_date)


def test_an_all_cash_sleeve_b_is_a_release_book_with_an_unranked_cash_line():
    """Sleeve B on the 20 Nov 2026 close: nothing above its 200-day average."""
    row = {"venue": "NYSE", "status": "READY", "decision_session": "2026-11-20",
           "decision_session_for_fill": "2026-11-20", "fill_date": "2026-11-23",
           "signals": dict(B_SIG), "weights": {"SHY": 1.0}, "top_k": 7, "gate": None}
    live = {"venue": "NYSE", "status": "READY", "decision_session": "2026-11-23",
            "decision_session_prev": "2026-11-20", "decision_session_for_fill": "2026-11-27",
            "fill_date": "2026-11-30", "last_completed_session": "2026-11-23",
            "signals": dict(B_SIG), "signals_prev": dict(B_SIG), "weights": {"SHY": 1.0},
            "top_k": 7, "gate": None}
    got = _run([{"name": "b", "sleeve": "B", "render": True,
                 "data": _data("B", engine=B_ENGINE, row=row, live=live, K=7)}])["b"]
    assert got["record"]["basis"] == "release"
    assert got["record"]["holdings"] == [{"etf": "SHY", "weight": 1.0, "signal_pct": None}]
    assert "SHY (cash)" in got["tbody"] and "7 of 7 slots unfilled" in got["tbody"]
    assert "100.0%" in got["tbody"]
    assert "fill close not yet priced" in got["asof"]


def _a_case(with_levels: bool) -> dict:
    row = {"venue": "NYSE", "status": "READY", "decision_session": "2026-11-20",
           "decision_session_for_fill": "2026-11-20", "fill_date": "2026-11-23",
           "signals": dict(A_SIG), "weights": dict(A_WEIGHTS), "top_k": 7, "gate": None}
    live = {"venue": "NYSE", "status": "READY", "decision_session": "2026-11-23",
            "decision_session_prev": "2026-11-20", "decision_session_for_fill": "2026-11-27",
            "fill_date": "2026-11-30", "last_completed_session": "2026-11-23",
            "signals": dict(A_SIG), "signals_prev": dict(A_SIG), "weights": dict(A_WEIGHTS),
            "top_k": 7, "gate": None}
    ma200 = {"per_etf_detail": {e: {"breadth_dates": ["2026-11-13", "2026-11-20"],
                                    "breadth_pct": [50.0, lvl]} for e, lvl in A_LEVEL.items()}}
    return _data("A", engine=A_ENGINE, row=row, live=live, K=7,
                 ma200=ma200 if with_levels else None)


def test_sleeve_a_flips_only_when_the_weekly_series_reproduces_the_release():
    """Every consumer of a sleeve A record prints the breadth LEVEL, and the
    release carries breadth minus the average. Without a level that
    reproduces the signals the engine's record stays."""
    got = _run([{"name": "with", "sleeve": "A", "data": _a_case(True)},
                {"name": "without", "sleeve": "A", "data": _a_case(False)}])
    rec = got["with"]["record"]
    assert rec["basis"] == "release" and rec["date"] == "2026-11-23"
    top = rec["holdings"][0]
    assert (top["etf"], top["weight"]) == ("IUHC", 0.404807)
    assert top["breadth_pct"] == round(A_LEVEL["IUHC"], 1) == 79.2
    assert len(rec["holdings"]) == 6
    assert got["without"]["record"]["basis"] == "engine"
    assert got["without"]["record"]["date"] == "2026-11-16"


# ---------------------------------------------------------------------------
# What the reader sees
# ---------------------------------------------------------------------------
def test_the_sleeve_d_tab_shows_the_filled_book_and_says_it_is_not_yet_priced():
    got = _run([{"name": "d", "sleeve": "D", "render": True,
                 "data": _data("D", engine=D_ENGINE, row=_d_row(), live=_d_live())}])["d"]
    assert "as of the 2026-11-23 rebalance" in got["asof"]
    assert "fill close not yet priced" in got["asof"]
    assert "unchanged since" not in got["asof"]
    body = re.sub(r"<[^>]+>", " ", got["tbody"])
    for etf, weight, breadth in (("EXV1", "39.7%", "63.9%"), ("EXH9", "32.0%", "51.4%"),
                                 ("EXH1", "28.3%", "45.5%")):
        assert etf in body and weight in body and breadth in body, (etf, body[:400])
    # The whole ranked row is listed: the two sectors outside the top three.
    assert got["tbody"].count('class="holdings-row"') == 5
    note = got["note"]
    assert "verified release for this fill" in note
    assert "computed <2026-11-22> 10:25 UTC, before the fill" in note
    assert "assumes the fill was executed in full on <2026-11-23>" in note
    assert "still shows the <2026-11-16> fill" in note
    assert "frozen at the release and is not re-read" in note
    assert "re-read from current data" not in note
    # The release book follows from the rule: no reconciliation warning.
    assert "Check:" not in note


def test_once_the_engine_prices_the_fill_the_page_reads_as_before():
    engine = {"date": "2026-11-23", "decision_date": "2026-11-20", "holdings": [
        {"etf": "EXV1", "weight": 0.3974, "breadth_pct": 63.9},
        {"etf": "EXH9", "weight": 0.3199, "breadth_pct": 51.4},
        {"etf": "EXH1", "weight": 0.2827, "breadth_pct": 45.5}]}
    got = _run([{"name": "d", "sleeve": "D", "render": True,
                 "data": _data("D", engine=engine, row=_d_row(), live=_d_live())}])["d"]
    assert got["record"]["basis"] == "engine"
    assert "not yet priced" not in got["asof"] and "not yet priced" not in got["note"]
    assert "weights from the engine's 2026-11-23 rebalance record" in got["note"]
    assert "re-read from current data" in got["note"]


# ---------------------------------------------------------------------------
# Every held-book surface reads the one resolver
# ---------------------------------------------------------------------------
def test_no_surface_reads_the_engine_record_past_the_resolver():
    t = TEMPLATE.read_text(encoding="utf-8")
    # The Monitor tab's position panels and its preview card took the record directly.
    for raw in ("topk.headline.latest_rebalance", "ac.headline.latest_rebalance",
                "tc.headline.latest_rebalance", "eu.headline.latest_rebalance",
                "sleeve?.headline?.latest_rebalance"):
        assert raw not in t, f"{raw} is read directly, past _heldRebalance"
    for letter in "ABCD":
        assert f"_heldRebalance('{letter}')" in t
        assert f"_releaseFillRecord('{letter}', h.latest_rebalance || last)" in t
    m = re.search(r"\nfunction _momLatestRebal\(root\) \{(.*?)\n\}", t, re.S)
    assert m and "_heldRebalance(sleeve)" in m.group(1)
    assert "const _lastRebalOf = (k) => _heldRebalance(k.toUpperCase());" in t
    # A cash line of a release book carries no signal; it must not print "null%".
    assert "${sgn}${h.signal_pct}% above 200d MA</span></span>" not in t
    assert t.count("h.signal_pct == null ? 'cash line, not ranked'") == 2


def test_the_monitor_tab_reads_the_sleeve_a_mean_at_the_decision_session():
    """Found beside the fill-date book on 25 Nov 2026: the box printed the
    mean of the series' LAST row (49.6%, sum 60.5pp) beside weights decided on
    the 20 Nov close (44.48%, 85.69pp), so no row reproduced its weight. The
    last row may only be the fallback."""
    t = TEMPLATE.read_text(encoding="utf-8")
    assert "const aLv = aXs ? _sleeveALevels(aLatest.decision_date || null, aXs.sig) : null;" in t
    assert "const aMean = aLv && aLv.ok ? aLv.avg : aMeanLatest;" in t
    assert "const aMean = aAllBreadths.length" not in t


# ---------------------------------------------------------------------------
# The pipeline passes the release through, and only the fields the page reads
# ---------------------------------------------------------------------------
def _release_file() -> dict:
    return {
        "schema": 1, "identity": "cbb8e4b6", "component": "europe", "held_sleeves": [],
        "quote_evidence": {"EXV1": {"secret": "not for the page"}},
        "book": {"as_of": "2026-11-20", "computed_at_utc": "2026-11-22T10:25:23.965004+00:00",
                 "targets_final": True, "next_fill": {"date": "2026-11-23"},
                 "lines": [{"etf": "EXV1", "sleeve": "D", "held": 0.07426, "target": 0.0794776}],
                 "sleeves": [dict(_d_row(), sleeve="D", signals_prev=dict(D_SIG_PREV),
                                  reason=None, last_completed_session="2026-11-20")]},
    }


def test_the_pipeline_passes_through_only_what_the_page_reads(tmp_path, monkeypatch):
    import pipeline

    monkeypatch.setattr(pipeline, "DATA_DIR", tmp_path)
    (tmp_path / "component_release.json").write_text(json.dumps(_release_file()), encoding="utf-8")
    got = pipeline.load_component_release()
    assert set(got) == {"identity", "component", "held_sleeves", "book"}
    assert set(got["book"]) == {"as_of", "computed_at_utc", "targets_final", "next_fill", "sleeves"}
    row = got["book"]["sleeves"][0]
    assert row["sleeve"] == "D" and row["fill_date"] == "2026-11-23"
    assert row["signals"] == D_SIG and row["weights"] == D_WEIGHTS
    assert "signals_prev" not in row and "last_completed_session" not in row
    assert "quote_evidence" not in json.dumps(got)


@pytest.mark.parametrize("content", [None, "{not json", json.dumps({"book": {}}),
                                     json.dumps({"book": {"sleeves": "x"}}), json.dumps([1, 2])])
def test_an_absent_or_unusable_release_leaves_the_page_on_the_engine_records(
        tmp_path, monkeypatch, content):
    import pipeline

    monkeypatch.setattr(pipeline, "DATA_DIR", tmp_path)
    if content is not None:
        (tmp_path / "component_release.json").write_text(content, encoding="utf-8")
    assert pipeline.load_component_release() is None
