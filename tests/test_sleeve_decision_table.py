"""The sleeve decision tables: signal, cut and weight on one row (2026-11-24).

WHY. The four current-selection tables listed the held names and one input
each. Sleeve A printed breadth beside weights that follow from breadth MINUS
the sector average and never printed the average, so on Tue 24 Nov 2026 the
owner read six held sectors as "most are below their 200-day average", a level
test the sleeve does not run. Sleeves B and C drew the engine's cash proxy as a
ranked holding, so a gated sleeve C read "the lowest-ranked holding is SHY ...
well above the +5% hard floor ... no cash is held". Each table is now the whole
ranked cross-section at the decision close, grouped by the cut the rule draws.

WHAT COULD BE SILENTLY WRONG, and the tests that pin each:
  1. A table that drops a ranked name (the row count must equal the payload's).
  2. A level column or average from a series the engine did not rank on (it
     is withheld unless level minus average reproduces every signal).
  3. A grouping that contradicts the book (the rule's selection and weights
     must reproduce the book, or the note says they do not).

WHAT THESE TESTS DO NOT COVER. For a held table the payload row, the engine's
record and the weekly series are recomputed together at every refresh, so the
check in (3) passes after a vendor restatement even though the decision reads
differently from the ranking made before the fill. The page says so in its
source line; nothing here can detect it.

The sleeve A, B, C and D figures are the 20 Nov 2026 decision as published in
live_targets.json and ma200_sweep.json on origin/main, so the expected strings
are production values. The shipped template functions run under Node; nothing
is transcribed.
"""
from __future__ import annotations

import datetime
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "template.html"

HARNESS = r"""
const vm = require('vm');
// Source and cases arrive together on stdin: the cases carry whole
// cross-sections and overrun the Windows command-line limit as an argument.
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
  ctx[`_render${c.sleeve}Holdings`]();
  const id = c.sleeve.toLowerCase();
  out[c.name] = {
    thead: el(`${id}-holdings-table`).querySelector('thead').innerHTML,
    tbody: el(`${id}-holdings-table`).querySelector('tbody').innerHTML,
    note: el(`${id}-holdings-note`).innerHTML,
    pending: el(`${id}-pending-selection`).innerHTML,
  };
}
process.stdout.write(JSON.stringify(out));
"""

# --- the 20 Nov 2026 decision, as published -------------------------------
A_SIG = {"CNDX": -0.0241, "CSP1": 0.041, "IDP6": -0.0903, "IUCD": 0.0339,
         "IUCM": -0.2034, "IUCS": 0.3302, "IUES": -0.0374, "IUFS": 0.081,
         "IUHC": 0.3469, "IUIS": -0.0428, "IUMS": -0.0206, "IUSP": -0.0768,
         "IUUS": 0.0239, "SOXX": -0.3615}
A_WEIGHTS = {"CSP1": 0.0478, "IUCD": 0.039589, "IUCS": 0.385356,
             "IUFS": 0.094498, "IUHC": 0.404807, "IUUS": 0.02795}
# Weekly sector breadth at the 20 Nov close, to the series' own 0.01pp: each
# is the published signal plus the 44.48% equal-weighted average.
A_AVG = 44.48
A_LEVEL = {e: round(A_AVG + 100 * v, 2) for e, v in A_SIG.items()}

B_SIG = {"DBC": -0.0142, "EFA": -0.0639, "EWJ": -0.0923, "GLD": -0.2044,
         "IEF": -0.0045, "IJR": -0.0447, "QQQ": -0.1079, "SPY": -0.0554,
         "TIP": -0.0089, "TLT": -0.0142, "VGK": -0.0522, "VNQ": -0.0133}
B_PREV = {"DBC": 0.0403, "EFA": 0.0143, "EWJ": -0.0013, "GLD": -0.1297,
          "IEF": -0.0226, "IJR": 0.0251, "QQQ": -0.0019, "SPY": 0.0344,
          "TIP": -0.0199, "TLT": -0.0403, "VGK": 0.0215, "VNQ": 0.0302}
B_HELD = [("DBC", 0.2083, 4.0), ("SPY", 0.1776, 3.4), ("VNQ", 0.156, 3.0),
          ("SHY", 0.1429, 0.4), ("IJR", 0.1298, 2.5), ("VGK", 0.1113, 2.2),
          ("EFA", 0.0741, 1.4)]

C_SIG = {"159801.SZ": -0.2015, "ARKG": -0.0086, "ARKK": -0.1776, "BLOK": -0.284,
         "BOTZ": -0.2423, "BTC-USD": -0.1845, "CIBR": 0.0756, "COPX": -0.3131,
         "CQQQ": -0.2935, "GDX": -0.232, "ICLN": -0.2549, "IHI": 0.0403,
         "ITA": -0.1249, "JETS": -0.0441, "LIT": -0.2928, "MOO": -0.0487,
         "PAVE": -0.105, "PHO": 0.0598, "REMX": -0.4773, "SKYY": -0.0155,
         "TAN": -0.3604, "URA": -0.4088, "WOOD": -0.1179, "XBI": -0.0224,
         "XME": -0.2744}
C_GATE = {"breadth": 0.08, "enabled": True, "fired": True, "floor": 0.05,
          "n_above": 2, "n_universe": 25, "threshold": 0.3}
C_CARRIED = {"159801.SZ": {"signal_filled": -0.2015, "signal_range": [-0.2817, -0.1212],
                           "native_close_carried": "2026-11-19"}}

D_SIG = {"EXV1": 0.6389, "EXH1": 0.4545, "EXV3": 0.4151, "EXH3": 0.4406, "EXH9": 0.5143}
D_WEIGHTS = {"EXV1": 0.397388, "EXH9": 0.319885, "EXH1": 0.282727}

NOW = "2026-11-25T03:00:00Z"   # after the post-fill refresh has re-anchored


def _engine(date, decision, holdings, K, own):
    hold = [{"etf": e, "weight": w, own: v} for e, w, v in holdings]
    rec = {"date": date, "decision_date": decision, "holdings": hold}
    return {"headline": {"K": K, "trade_history": [rec], "latest_rebalance": rec}}


def _sleeve(sleeve, sig, weights, *, decision, fill, prev=None, prev_date=None,
            top_k, gate=None, status="READY", **extra):
    return {"sleeve": sleeve, "status": status, "fill_date": fill,
            "decision_session": decision, "decision_session_for_fill": decision,
            "top_k": top_k, "weights": weights, "signals": sig,
            "signals_prev": prev or {}, "decision_session_prev": prev_date,
            "gate": gate, **extra}


def _lt(*sleeves, lines=()):
    return {"computed_at_utc": "2026-11-22T10:25:23+00:00", "targets_final": True,
            "sleeves": list(sleeves), "lines": list(lines)}


def _series(level, dates):
    """A weekly breadth series whose LAST label carries `level`."""
    return {"per_etf_detail": {e: {"breadth_dates": list(dates),
                                   "breadth_pct": [50.0] * (len(dates) - 1) + [v]}
                               for e, v in level.items()}}


def _a(*, sig=None, book=None, decision="2026-11-20", fill="2026-11-23", top_k=7,
       level=None, labels=("2026-11-13", "2026-11-20"), engine_decision=None,
       prev=None, prev_date=None):
    """Sleeve A with the held table on the `fill` book decided at `decision`."""
    sig = A_SIG if sig is None else sig
    book = A_WEIGHTS if book is None else book
    held = [(e, round(w, 4), 0.0) for e, w in book.items()]
    sl = _sleeve("A", sig, book, decision=decision, fill=fill, top_k=top_k,
                 prev=prev, prev_date=prev_date)
    return {"topk": _engine(fill, engine_decision or decision, held, top_k, "breadth_pct"),
            "live_targets": _lt(sl),
            "ma200": _series(A_LEVEL if level is None else level, labels)}


def _cases() -> list[dict]:
    cases = []

    def add(name, sleeve, data, now=NOW):
        cases.append({"name": name, "sleeve": sleeve, "data": data, "now": now})

    add("A_held", "A", _a())
    # Mid-week, after the post-fill refresh: the 20 Nov row is signals_prev.
    d = _a()
    s = d["live_targets"]["sleeves"][0]
    s.update(signals={e: v * 0.5 for e, v in A_SIG.items()}, signals_prev=A_SIG,
             decision_session="2026-11-24", decision_session_prev="2026-11-20",
             decision_session_for_fill="2026-11-27", fill_date="2026-11-30")
    add("A_held_midweek", "A", d)

    # --- the level guard -----------------------------------------------------
    add("A_level_off_0p10", "A", _a(level=dict(A_LEVEL, IUFS=A_LEVEL["IUFS"] + 0.10)))
    add("A_level_off_0p02", "A", _a(level=dict(A_LEVEL, IUFS=A_LEVEL["IUFS"] + 0.02)))
    add("A_level_no_reading", "A", _a(labels=("2026-11-06", "2026-11-13")))
    # A Wednesday row finds the coming Friday's label, whose readings are
    # another close's, and fails the reconciliation.
    other = {e: round(A_AVG + 90 * v, 2) for e, v in A_SIG.items()}
    add("A_midweek_row", "A", _a(decision="2026-11-18", fill="2026-11-19", level=other))
    # Holiday-shifted weeks: the decision is the Thursday, the weekly label
    # the Friday holiday. These are also the month- and year-boundary cases.
    add("A_month_boundary", "A", _a(decision="2026-10-30", fill="2026-11-02",
                                    labels=("2026-10-23", "2026-10-30")))
    add("A_christmas", "A", _a(decision="2026-12-24", fill="2026-12-28",
                               labels=("2026-12-18", "2026-12-25")), now="2026-12-30T03:00:00Z")
    add("A_year_boundary", "A", _a(decision="2026-12-31", fill="2027-01-04",
                                   labels=("2026-12-25", "2027-01-01")), now="2027-01-06T03:00:00Z")

    # --- no row for the held book's decision ---------------------------------
    add("A_no_cross_section", "A", _a(engine_decision="2026-11-06"))
    # A previous row IS in the payload, but for another close.
    add("A_prev_row_other_close", "A", _a(engine_decision="2026-11-06",
                                          prev=A_SIG, prev_date="2026-11-13"))

    # --- the reconciliation ---------------------------------------------------
    add("A_book_outside_rule", "A", _a(book=dict(A_WEIGHTS, IUES=0.05)))
    add("A_weights_off_2pp", "A", _a(book=dict(A_WEIGHTS, IUHC=0.3848, IUCS=0.4054)))
    add("A_rule_name_not_held", "A", _a(book={e: w for e, w in A_WEIGHTS.items() if e != "IUUS"}))
    add("A_stray_line", "A", _a(book=dict(A_WEIGHTS, ZZZZ=0.02)))
    add("A_null_signal", "A", _a(sig=dict(A_SIG, SOXX=None)))
    # Published precision. A tie at four decimals across the cut, listed
    # alphabetically while the engine held the later name; and a name printed
    # exactly on the cut, held and not held.
    tie = {"AA": 0.10, "BB": 0.05, "CC": 0.05, "DD": -0.20}
    add("A_tie_at_cut", "A", _a(sig=tie, top_k=2, book={"AA": 0.6667, "CC": 0.3333}))
    on = {"AA": 0.10, "BB": 0.0, "CC": -0.10}
    add("A_on_cut_held", "A", _a(sig=on, book={"AA": 0.9999, "BB": 0.0001}))
    add("A_on_cut_not_held", "A", _a(sig=on, book={"AA": 1.0}))

    # --- sleeve B -------------------------------------------------------------
    b_lines = [{"sleeve": "B", "etf": e, "held": w * 0.35, "target": 0.0}
               for e, w, _ in B_HELD if e != "SHY"]
    b_lines.append({"sleeve": "B", "etf": "SHY", "held": 0.05, "target": 0.35})

    def b_data(held=B_HELD, sig=B_SIG, weights=None, lines=b_lines):
        return {"asset_class": _engine("2026-11-16", "2026-11-13", held, 7, "signal_pct"),
                "live_targets": _lt(_sleeve("B", sig, weights or {"SHY": 1.0},
                                            decision="2026-11-20", fill="2026-11-23", top_k=7,
                                            prev=B_PREV, prev_date="2026-11-13"), lines=lines)}

    add("B_held_and_pending", "B", b_data(), now="2026-11-24T02:00:00Z")
    add("B_cash_line_off", "B", b_data(held=[(e, 0.30 if e == "SHY" else w, v) for e, w, v in B_HELD]))
    # IEF is a ranked sleeve B name, not the cash proxy: as the one name above
    # its average it is selected, and it ENTERS the book.
    ief_lines = [l for l in b_lines if l["etf"] != "SHY"] + [
        {"sleeve": "B", "etf": "IEF", "held": 0.0, "target": 0.05},
        {"sleeve": "B", "etf": "SHY", "held": 0.05, "target": 0.30}]
    add("B_ief_is_ranked", "B", b_data(sig=dict(B_SIG, IEF=0.01),
                                       weights={"IEF": 0.142857, "SHY": 0.857143},
                                       lines=ief_lines), now="2026-11-24T02:00:00Z")

    # --- sleeve C -------------------------------------------------------------
    add("C_gate_on", "C", {
        "thematic": _engine("2026-11-23", "2026-11-20", [("SHY", 1.0, 0.4)], 5, "signal_pct"),
        "live_targets": _lt(_sleeve("C", C_SIG, {"SHY": 1.0}, decision="2026-11-20",
                                    fill="2026-11-23", top_k=5, gate=C_GATE,
                                    native_unconfirmed=C_CARRIED))})
    # The held book was decided on the PREVIOUS row: its gate count is derived
    # from that row, not read from the payload's record of the current one.
    c_prev = dict(C_SIG, ARKG=0.1348, SKYY=0.1057, PHO=0.1014, CIBR=0.1269,
                  IHI=0.0888, XBI=0.0535, JETS=0.0657, MOO=0.0529)
    top5 = [("ARKG", 0.2, 13.5), ("CIBR", 0.2, 12.7), ("SKYY", 0.2, 10.6),
            ("PHO", 0.2, 10.1), ("IHI", 0.2, 8.9)]
    add("C_gate_off_prev", "C", {
        "thematic": _engine("2026-11-16", "2026-11-13", top5, 5, "signal_pct"),
        "live_targets": _lt(_sleeve("C", C_SIG, {"SHY": 1.0}, decision="2026-11-17",
                                    fill="2026-11-23", top_k=5, gate=C_GATE,
                                    prev=c_prev, prev_date="2026-11-13",
                                    native_unconfirmed=C_CARRIED))})
    # A name printed at exactly +5.00%: the engine counted it above (eight,
    # gate off); the rows show seven strictly above.
    on_floor = dict(c_prev, MOO=0.05)
    add("C_name_on_the_floor", "C", {
        "thematic": _engine("2026-11-23", "2026-11-20", top5, 5, "signal_pct"),
        "live_targets": _lt(_sleeve("C", on_floor, {e: 0.2 for e, _, _ in top5},
                                    decision="2026-11-20", fill="2026-11-23", top_k=5,
                                    gate=dict(C_GATE, n_above=8, breadth=0.32, fired=False)))})

    # Exactly at the threshold the gate is OFF: it fires on breadth BELOW 30%
    # (sleeve_gate_state). Three of ten above the floor, on a previous row so
    # the count is the page's own.
    ten = {"T1": 0.20, "T2": 0.15, "T3": 0.10, "T4": 0.04, "T5": 0.03,
           "T6": -0.01, "T7": -0.02, "T8": -0.03, "T9": -0.04, "T0": -0.05}
    add("C_exactly_at_threshold", "C", {
        "thematic": _engine("2026-11-16", "2026-11-13",
                            [("T1", 0.2, 20.0), ("T2", 0.2, 15.0), ("T3", 0.2, 10.0),
                             ("SHY", 0.4, 0.4)], 5, "signal_pct"),
        "live_targets": _lt(_sleeve("C", {e: v - 0.3 for e, v in ten.items()}, {"SHY": 1.0},
                                    decision="2026-11-17", fill="2026-11-23", top_k=5,
                                    gate=C_GATE, prev=ten, prev_date="2026-11-13"))})

    # --- sleeve D -------------------------------------------------------------
    def d_data(sig, weights):
        return {"europe": _engine("2026-11-23", "2026-11-20",
                                  [(e, round(w, 4), 100 * sig[e]) for e, w in weights.items()],
                                  3, "breadth_pct"),
                "live_targets": _lt(_sleeve("D", sig, weights, decision="2026-11-20",
                                            fill="2026-11-23", top_k=3))}

    add("D_held", "D", d_data(D_SIG, D_WEIGHTS))
    # The weight function drops a reading of zero from the top three.
    add("D_zero_readings", "D", d_data({"EXV1": 0.5, "EXH1": 0.3, "EXV3": 0.0,
                                        "EXH3": 0.0, "EXH9": 0.0},
                                       {"EXV1": 0.625, "EXH1": 0.375}))
    return cases


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to execute the template functions")
    return node


def _functions() -> str:
    t = TEMPLATE.read_text(encoding="utf-8")
    m = re.search(r"\nfunction _renderAHoldings\(.*?(?=\n// -{5,} Portfolio tab)", t, re.S)
    assert m, "sleeve selection functions not found in template.html"
    return m.group(0)


@pytest.fixture(scope="module")
def rendered() -> dict:
    r = subprocess.run([_node(), "-e", HARNESS],
                       input=json.dumps({"src": _functions(), "cases": _cases()}),
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    return json.loads(r.stdout)


def _rows(tbody: str) -> list[str]:
    return re.findall(r'<tr class="holdings-row" data-etf="([^"]+)"', tbody)


def _groups(tbody: str) -> list[str]:
    return re.findall(r'<tr class="sleeve-group-row"><td colspan="\d+">(.*?)</td></tr>', tbody)


def _cells(tbody: str, etf: str) -> list[str]:
    m = re.search(rf'<tr class="holdings-row" data-etf="{re.escape(etf)}">(.*?)</tr>', tbody, re.S)
    assert m, f"{etf} has no row"
    return [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", m.group(1), re.S)]


# --- sleeve A ---------------------------------------------------------------

@pytest.mark.parametrize("case", ["A_held", "A_held_midweek"])
def test_a_lists_every_ranked_sector_with_level_excess_and_weight(rendered, case):
    out = rendered[case]
    rows = _rows(out["tbody"])
    assert len(rows) == len(A_SIG) == 14 and set(rows) == set(A_SIG)
    # Rank order: held first, by excess; then the eight below the average.
    assert rows == ["IUHC", "IUCS", "IUFS", "CSP1", "IUCD", "IUUS",
                    "IUMS", "CNDX", "IUES", "IUIS", "IUSP", "IDP6", "IUCM", "SOXX"]
    assert _groups(out["tbody"]) == ["Held: above the sector average (6)",
                                     "Not held: below the sector average (8)"]
    # The average sits in the heading of the column it explains.
    assert "Minus sector average (44.48%)" in out["thead"]
    assert "Breadth at decision" in out["thead"]
    assert _cells(out["tbody"], "IUHC") == ["1", "IUHC", "", "79.17%", "+34.69pp", "40.5%"]
    assert _cells(out["tbody"], "IUCD") == ["5", "IUCD", "", "47.87%", "+3.39pp", "4.0%"]
    assert _cells(out["tbody"], "SOXX") == ["14", "SOXX", "", "8.33%", "-36.15pp", "—"]
    note = out["note"]
    assert ("Average of the 14 sector breadth readings at the <2026-11-20> close, "
            "each fund counting equally: <strong>44.48%</strong>.") in note
    assert "<strong>6 of 14</strong> sectors were above the average" in note
    assert "the sleeve holds 6, against a maximum of 7" in note
    assert "divided by the sum of the held excesses, 85.69pp" in note
    assert "Sleeve A holds no cash" in note
    assert "Check:" not in note
    assert "clustered near the average" not in note


def test_a_level_average_and_excess_subtract_on_the_face_of_the_table(rendered):
    """Every printed row: level minus the printed average is the printed
    excess, and the printed excesses give the printed weights."""
    out = rendered["A_held"]
    avg = float(re.search(r"Minus sector average \(([\d.]+)%\)", out["thead"]).group(1))
    excess, weight = {}, {}
    for e in A_SIG:
        c = _cells(out["tbody"], e)
        level, ex = float(c[3].rstrip("%")), float(c[4].replace("pp", ""))
        assert abs((level - avg) - ex) < 0.011, e
        excess[e] = ex
        if c[5] != "—":
            weight[e] = float(c[5].rstrip("%"))
    assert len(weight) == 6
    total = sum(excess[e] for e in weight)
    assert abs(total - 85.69) < 0.011
    for e, w in weight.items():
        assert abs(100 * excess[e] / total - w) < 0.06, e
    assert abs(sum(weight.values()) - 100.0) < 0.11


@pytest.mark.parametrize("case,why", [
    ("A_level_off_0p10", "does not reproduce these signals for this close"),
    ("A_midweek_row", "does not reproduce these signals for this close"),
    ("A_level_no_reading", "has no reading for this close"),
])
def test_a_level_is_withheld_unless_it_reproduces_the_payload(rendered, case, why):
    out = rendered[case]
    assert "Breadth at decision" not in out["thead"]
    assert "44.48%" not in out["thead"] and "Average of the 14" not in out["note"]
    assert "Breadth minus sector average" in out["thead"]
    assert f"The breadth level and the average are not printed: the weekly sector breadth series {why}." in out["note"]
    # The rows and the weights do not depend on the level series.
    assert len(_rows(out["tbody"])) == 14
    assert _cells(out["tbody"], "IUHC") == ["1", "IUHC", "", "+34.69pp", "40.5%"]


def test_a_level_guard_allows_rounding_and_nothing_wider(rendered):
    # 0.02pp off is the two files' own rounding; it prints.
    assert "Breadth at decision" in rendered["A_level_off_0p02"]["thead"]
    # 0.10pp off is not; A_level_off_0p10 above is withheld.
    assert "Breadth at decision" not in rendered["A_level_off_0p10"]["thead"]


@pytest.mark.parametrize("case,decision,label,fill", [
    ("A_month_boundary", "2026-10-30", "2026-10-30", "2026-11-02"),
    ("A_christmas", "2026-12-24", "2026-12-25", "2026-12-28"),
    ("A_year_boundary", "2026-12-31", "2027-01-01", "2027-01-04"),
])
def test_level_is_found_under_the_friday_label_across_month_and_year_ends(
        rendered, case, decision, label, fill):
    # Python datetime months are 1-indexed; weekday() is Monday=0 .. Sunday=6.
    # The weekly series is labelled W-FRI (run_ma200_sweep), the fill is a
    # Monday, and a decision that is not the label is the Thursday before a
    # Friday holiday. Asserted from the calendar, not assumed.
    day = datetime.date.fromisoformat
    assert day(label).weekday() == 4 and day(fill).weekday() == 0
    assert day(decision).weekday() == (4 if decision == label else 3)
    assert (day(label) - day(decision)).days == (0 if decision == label else 1)
    out = rendered[case]
    assert len(_rows(out["tbody"])) == 14
    assert f"sector breadth readings at the <{decision}> close" in out["note"]
    assert "<strong>44.48%</strong>" in out["note"] and "Breadth at decision" in out["thead"]
    assert out["pending"] == ""   # the held table has caught up with the fill


@pytest.mark.parametrize("case", ["A_no_cross_section", "A_prev_row_other_close"])
def test_a_without_a_row_for_that_close_falls_back_to_held_names(rendered, case):
    out = rendered[case]
    assert len(_rows(out["tbody"])) == 6 and not _groups(out["tbody"])
    assert "Breadth at rebalance" in out["thead"]
    assert "Held names only" in out["note"] and "<2026-11-06> decision close" in out["note"]
    assert "ranks on the level minus the sector average" in out["note"]


def test_a_book_the_rule_does_not_give_is_flagged_not_relabelled(rendered):
    out = rendered["A_book_outside_rule"]
    # IUES is held, so it is listed as held; its signal still reads red, and
    # the group no longer claims the rule as the reason for all seven.
    assert _groups(out["tbody"])[0] == "Held: not all inside the rule, see the check below (7)"
    assert _cells(out["tbody"], "IUES")[-1] == "5.0%"
    assert re.search(r'data-etf="IUES">.*?<td class="num bad"><strong>-3\.74pp', out["tbody"], re.S)
    assert "<strong>Check:</strong> this cross-section does not reproduce the book." in out["note"]
    assert "Held outside the rule: IUES -3.74pp." in out["note"]


def test_a_each_leg_of_the_reconciliation_fires_alone(rendered):
    off = rendered["A_weights_off_2pp"]["note"]
    assert "The rule's weights differ from the book's by more than 0.5pp." in off
    assert "outside the rule" not in off and "not held:" not in off
    missed = rendered["A_rule_name_not_held"]["note"]
    assert "Inside the rule but not held: IUUS +2.39pp." in missed
    assert "outside the rule" not in missed
    stray = rendered["A_stray_line"]
    assert "In the book but not in the ranked row: ZZZZ." in stray["note"]
    assert "Sleeve A holds no cash" not in stray["note"]
    assert _cells(stray["tbody"], "ZZZZ") == ["—", "ZZZZ", "In the book, not in the ranked row", "—", "—", "2.0%"]
    assert "(cash)" not in stray["tbody"]


def test_a_null_signal_is_listed_not_dropped(rendered):
    out = rendered["A_null_signal"]
    assert len(_rows(out["tbody"])) == 14
    assert _groups(out["tbody"])[-1] == "Not ranked: no signal at the decision close (1)"
    assert _cells(out["tbody"], "SOXX")[0] == "—"


def test_published_precision_is_settled_by_the_book(rendered):
    tie = rendered["A_tie_at_cut"]
    assert _rows(tie["tbody"]) == ["AA", "CC", "BB", "DD"]   # the held tie sorts first
    assert _groups(tie["tbody"]) == ["Held: above the sector average (2)",
                                     "Not held: above the sector average, outside the top 2 (1)",
                                     "Not held: below the sector average (1)"]
    assert "Check:" not in tie["note"]
    held = rendered["A_on_cut_held"]
    assert _groups(held["tbody"])[0] == "Held: above the sector average (2)"
    assert "Check:" not in held["note"]
    # Not held, a signal of exactly zero is not above the average: strict.
    out = rendered["A_on_cut_not_held"]
    assert _groups(out["tbody"]) == ["Held: above the sector average (1)",
                                     "Not held: below the sector average (2)"]
    assert "<strong>1 of 3</strong> sectors was above the average" in out["note"]
    assert "Check:" not in out["note"]


# --- sleeve B ---------------------------------------------------------------

def test_b_held_book_books_shy_as_cash_and_names_the_invested_share(rendered):
    out = rendered["B_held_and_pending"]
    rows = _rows(out["tbody"])
    assert len(rows) == 12 and "SHY" not in rows
    assert rows[:6] == ["DBC", "SPY", "VNQ", "IJR", "VGK", "EFA"]
    assert _groups(out["tbody"]) == ["Held: above the 200-day average (6)",
                                     "Not held: below the 200-day average (6)"]
    assert _cells(out["tbody"], "DBC") == ["1", "DBC", "", "+4.03%", "20.8%"]
    assert '<tr class="sleeve-cash-row">' in out["tbody"]
    assert "SHY (cash)" in out["tbody"] and "1 of 7 slots unfilled" in out["tbody"]
    assert "14.3%" in out["tbody"]
    note = out["note"]
    assert "<strong>6 of 12</strong> asset classes were above their 200-day average at the <2026-11-13> close." in note
    assert "6 of 7 slots are taken, so 6/7 of the sleeve is invested and 1/7 is in SHY." in note
    assert "sum of the held signals, 16.58%" in note
    assert "Check:" not in note and "no cash is held" not in note


def test_b_cash_line_is_part_of_the_reconciliation(rendered):
    note = rendered["B_cash_line_off"]["note"]
    assert "<strong>Check:</strong>" in note
    assert "The rule's weights differ from the book's by more than 0.5pp." in note


def test_b_all_cash_selection_renders_a_pending_table(rendered):
    p = rendered["B_held_and_pending"]["pending"]
    assert 'id="b-pending-table"' in p
    assert "Latest selection — the <2026-11-23> rebalance, filled on <2026-11-23>" in p
    assert len(_rows(p)) == 12
    assert _groups(p) == ["Not selected: below the 200-day average (12)"]
    assert "SHY (cash)" in p and "7 of 7 slots unfilled" in p and "100.0%" in p
    assert "<strong>None of the 12</strong> asset classes was above its 200-day average at the <2026-11-20> close" in p
    assert "Left the book at the fill: VNQ -1.33%, DBC -1.42%" in p
    assert "Source: live_targets.json, published <2026-11-22> 10:25 UTC" in p
    assert "Check:" not in p


def test_b_ief_is_a_ranked_name_in_the_line_and_in_the_table(rendered):
    out = rendered["B_ief_is_ranked"]
    p = out["pending"]
    assert _groups(p)[0] == "Selected: above the 200-day average (1)"
    assert _cells(p, "IEF")[-2:] == ["+1.00%", "14.3%"]
    assert "<strong>1 of 12</strong> asset classes was above its 200-day average at the <2026-11-20> close." in p
    assert "1 of 7 slots is taken, so 1/7 of the sleeve is invested and 6/7 is in SHY." in p
    assert "Check:" not in p
    # The pending-fill line above it reads the same payload the same way.
    assert "IEF enters at 5.0% of NAV" in out["note"]
    assert "IEF takes" not in out["note"]


# --- sleeve C ---------------------------------------------------------------

def test_c_gate_on_lists_all_25_and_never_ranks_the_cash_proxy(rendered):
    out = rendered["C_gate_on"]
    rows = _rows(out["tbody"])
    assert len(rows) == 25 and "SHY" not in rows
    assert rows[:2] == ["CIBR", "PHO"]
    assert _groups(out["tbody"]) == ["Not held: above the +5% floor, sleeve gate on (2)",
                                     "Not held: below the +5% floor (23)"]
    assert "Sleeve gate on: 2 of 25 above the +5% floor, 8 needed" in out["tbody"]
    assert _cells(out["tbody"], "CIBR") == ["1", "CIBR", "", "+7.56%", "—"]
    note = out["note"]
    assert "<strong>2 of 25</strong> themes were above the +5% floor at the <2026-11-20> close." in note
    assert "The sleeve gate needs 8 (30% of 25)." in note
    assert "Gate on: the whole sleeve is in SHY." in note
    assert "lowest-ranked holding" not in note and "no cash is held" not in note
    assert "engine's count on unrounded signals" not in note
    assert "Check:" not in note


def test_c_carried_line_is_marked_on_the_current_row_only(rendered):
    cur = rendered["C_gate_on"]
    assert _cells(cur["tbody"], "159801.SZ")[3] == "-20.15% †"
    assert ("† 159801.SZ is ranked on a close carried from <2026-11-19>; its signal could lie "
            "between -28.17% and -12.12%, and no value in that range changes the book.") in cur["note"]
    # The held book below was decided on signals_prev; the payload's record
    # of a carried line describes its current row and is not applied to it.
    prev = rendered["C_gate_off_prev"]
    assert "†" not in prev["tbody"] and "†" not in prev["note"]


def test_c_gate_count_for_a_previous_row_is_derived_from_that_row(rendered):
    out = rendered["C_gate_off_prev"]
    # The payload's own gate record (2 of 25, fired) describes its CURRENT
    # row; the held book was decided on the previous one, 8 of 25 above.
    assert "<strong>8 of 25</strong> themes were above the +5% floor at the <2026-11-13> close." in out["note"]
    assert "Gate off: the top 5 above the floor take 1/5 of the sleeve each." in out["note"]
    assert _groups(out["tbody"]) == ["Held: above the +5% floor (5)",
                                     "Not held: above the +5% floor, outside the top 5 (3)",
                                     "Not held: below the +5% floor (17)"]
    assert "Check:" not in out["note"]


def test_c_gate_is_off_at_exactly_the_threshold(rendered):
    out = rendered["C_exactly_at_threshold"]
    assert "<strong>3 of 10</strong> themes were above the +5% floor at the <2026-11-13> close." in out["note"]
    assert "The sleeve gate needs 3 (30% of 10)." in out["note"]
    assert ("Gate off: the top 5 above the floor take 1/5 of the sleeve each; "
            "2 unfilled slots are in SHY.") in out["note"]
    assert "2 of 5 slots unfilled" in out["tbody"] and "40.0%" in out["tbody"]
    assert _groups(out["tbody"]) == ["Held: above the +5% floor (3)",
                                     "Not held: below the +5% floor (7)"]
    assert "Check:" not in out["note"]


def test_c_name_printed_on_the_floor_is_below_it_and_the_counts_are_reconciled(rendered):
    out = rendered["C_name_on_the_floor"]
    groups = _groups(out["tbody"])
    assert groups == ["Held: above the +5% floor (5)",
                      "Not held: above the +5% floor, outside the top 5 (2)",
                      "Not held: below the +5% floor (18)"]
    assert _cells(out["tbody"], "MOO")[3] == "+5.00%"
    assert re.search(r'data-etf="MOO">.*?<td class="num bad"><strong>\+5\.00%', out["tbody"], re.S)
    note = out["note"]
    assert "<strong>8 of 25</strong> themes were above the +5% floor" in note
    assert ("That is the engine's count on unrounded signals; 7 of the rows above print "
            "strictly over +5.00%") in note
    assert "Check:" not in note


# --- sleeve D ---------------------------------------------------------------

def test_d_ranks_five_holds_three_and_states_the_divisor(rendered):
    out = rendered["D_held"]
    assert _rows(out["tbody"]) == ["EXV1", "EXH9", "EXH1", "EXH3", "EXV3"]
    assert _groups(out["tbody"]) == ["Held: top 3 by breadth (3)",
                                     "Not held: outside the top 3 (2)"]
    assert _cells(out["tbody"], "EXV1") == ["1", "EXV1", "", "63.9%", "39.7%"]
    assert _cells(out["tbody"], "EXH3") == ["4", "EXH3", "", "44.1%", "—"]
    assert ("The top 3 of 5 sectors by breadth at the <2026-11-20> close are held, provided "
            "the reading is above zero; sleeve D holds no cash.") in out["note"]
    assert "sum of the held readings, 160.8%." in out["note"]
    assert "has no floor" not in out["note"]
    assert "Check:" not in out["note"]


def test_d_drops_a_reading_of_zero_from_the_top_three(rendered):
    out = rendered["D_zero_readings"]
    assert _groups(out["tbody"]) == ["Held: top 3 by breadth (2)",
                                     "Not held: no constituent above its 200-day average (3)"]
    assert _cells(out["tbody"], "EXV1")[-1] == "62.5%"
    assert "Check:" not in out["note"]


# --- all four ----------------------------------------------------------------

@pytest.mark.parametrize("case", ["A_held", "B_held_and_pending", "C_gate_on", "D_held"])
def test_every_held_table_names_its_sources_and_says_they_are_a_re_read(rendered, case):
    note = rendered[case]["note"]
    assert "Sources: signals from live_targets.json, published <2026-11-22> 10:25 UTC" in note
    assert "weights from the engine's 2026-11-" in note
    assert ("Both are re-read from current data at each refresh, so after a data restatement "
            "this shows the decision as it now reads, which can differ from the ranking made "
            "before the fill.") in note
