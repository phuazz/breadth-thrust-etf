"""Fill-placement diagnostic (reviews/2026-10-03_fill-placement-diagnostic/):
the engine copy and the adapter, on synthetic data only.

Python months are 1-indexed (January = 1); every date literal below is
1-indexed. No test reads a fetched bar, an engine panel or any actual fill.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import sys
from pathlib import Path
from statistics import NormalDist
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import ws_fill_placement_adapter as ad  # noqa: E402

# WS_FILL_ENGINE points the suite at a mutated copy of the engine (the mutation check of the
# spec-freeze review); by default it is the registered engine
ENGINE = Path(os.environ.get("WS_FILL_ENGINE") or (REPO / "reviews" / "2026-10-03_fill-placement-diagnostic" / "engine" / "fill_timing.py"))
SPEC = json.loads((ENGINE.parent / "prereg_spec.json").read_text(encoding="utf-8"))


def _load_engine(name: str = "ws_fill_engine_under_test"):
    spec = importlib.util.spec_from_file_location(name, ENGINE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


eng = _load_engine()


# ----------------------------------------------------------------------------
# Synthetic bars in the engine's extract shape
# ----------------------------------------------------------------------------
def _weekdays(start: dt.date, n: int) -> list[dt.date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def _line(n: int, start: dt.date, tz: str, open_hm: tuple[int, int], base: float, seed: int, ex_at: int | None = None):
    rng = np.random.default_rng(seed)
    days = _weekdays(start, n)
    c = base * (1 + 0.08 * np.sin(np.arange(n) / 6.0)) + 0.03 * np.arange(n) + rng.normal(0, 0.4, n)
    o = np.concatenate([[c[0]], c[:-1]]) + rng.normal(0, 0.2, n)
    h = np.maximum(o, c) + 0.3 + rng.uniform(0, 0.8, n)
    l = np.minimum(o, c) - 0.3 - rng.uniform(0, 0.8, n)
    f = np.ones(n)
    if ex_at is not None:
        f[:ex_at] = 0.985                       # adjusted close below close before the ex-date
    return days, {"o": o, "h": h, "l": l, "c": c, "f": f, "tz": tz, "open_hm": open_hm}


def _bars(days, arr) -> list[dict]:
    zone = ZoneInfo(arr["tz"])
    hh, mm = arr["open_hm"]
    return [{"d": int(dt.datetime(x.year, x.month, x.day, hh, mm, tzinfo=zone).timestamp()),
             "o": float(arr["o"][i]), "h": float(arr["h"][i]), "l": float(arr["l"][i]),
             "c": float(arr["c"][i]), "ac": float(arr["c"][i] * arr["f"][i])} for i, x in enumerate(days)]


def _expected(arr, i: int, side: int, price: float | None = None, k: int = 3):
    """u, pre and post by hand: the window rebased to bar i's factor."""
    sl = slice(i - k, i + k + 1)
    scale = arr["f"][sl] / arr["f"][i]
    hh, ll, cc = arr["h"][sl] * scale, arr["l"][sl] * scale, arr["c"][sl] * scale
    H, L = hh.max(), ll.min()
    p = arr["c"][i] if price is None else price
    u = (p - L) / (H - L) if side > 0 else (H - p) / (H - L)
    return u, side * (p / cc[0] - 1), -side * (cc[-1] / p - 1)


def _write_inputs(tmp: Path, lines: dict, fills: list[dict]):
    hist = {"_provenance": {"source": "synthetic"}}
    meta = {}
    for key, (days, arr, exchange, ccy) in lines.items():
        hist[key] = {"name": key, "history": _bars(days, arr)}
        meta[key.split("|")[0]] = {"name": key, "yf": key, "exchange": exchange, "ccy": ccy, "type": "ETF"}
    (tmp / "bars.json").write_text(json.dumps(hist), encoding="utf-8")
    (tmp / "fills.json").write_text(json.dumps(fills), encoding="utf-8")
    (tmp / "book.json").write_text(json.dumps({"meta": meta, "_provenance": {"fixture": True, "parity": {"excluded_share": 0.0},
                                                                              "reproduction": {"reconciled": True}}}), encoding="utf-8")
    return SimpleNamespace(history=str(tmp / "bars.json"), fills=str(tmp / "fills.json"), book=str(tmp / "book.json"))


def _fill(key: str, day: dt.date, side: int, price: float, ccy: str, kind: str = "sleeve") -> dict:
    return {"d": day.isoformat(), "a": "B" if side > 0 else "S", "t": key.split("|")[0], "q": 1.0 / price, "p": float(price),
            "ccy": ccy, "yf": key, "th": key.split(":")[0], "fee": None, "ref": None, "kind": kind}


# ----------------------------------------------------------------------------
# Engine: planted fills and guard 2
# ----------------------------------------------------------------------------
def test_planted_fills_reproduce_hand_computed_scores(tmp_path):
    """The engine copy's build and scoring path returns the hand-computed u,
    pre leg and post leg on fills planted at their session closes, one window
    holding an ex-date; the close-priced placebo at offset zero is the actual
    fill; the PCC original's score() agrees."""
    da, aa = _line(200, dt.date(2025, 1, 6), "America/New_York", (9, 30), 100.0, 1, ex_at=100)
    db, ab = _line(200, dt.date(2025, 1, 6), "Europe/Berlin", (9, 0), 40.0, 2)
    lines = {"T:AAA|AAA": (da, aa, "ARCA", "USD"), "T:BBB|BBB.DE": (db, ab, "XETR", "EUR")}
    plant = [("T:AAA|AAA", da, aa, 50, 1), ("T:AAA|AAA", da, aa, 98, -1), ("T:AAA|AAA", da, aa, 150, 1),
             ("T:BBB|BBB.DE", db, ab, 60, -1), ("T:BBB|BBB.DE", db, ab, 120, 1)]
    fills = [_fill(k, days[i], side, arr["c"][i], lines[k][3]) for k, days, arr, i, side in plant]
    args = _write_inputs(tmp_path, lines, fills)
    ctx = eng.build(SPEC, args)
    assert len(ctx["complete"]) == 5
    expected = {(k, days[i].isoformat()): _expected(arr, i, side) for k, days, arr, i, side in plant}
    pcc_path = Path(r"C:/dev/Portfolio-Command-Centre/reviews/2026-10-03_fill-timing/fill_timing.py")
    pcc = None
    if pcc_path.exists():
        spec = importlib.util.spec_from_file_location("ws_fill_engine_pcc_original", pcc_path)
        pcc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pcc)
    k = SPEC["window_sessions_each_side"]
    assert any(f.get("ex_in_window") for f in ctx["complete"]), "the fixture must hold an ex-date inside a window"
    for f in ctx["complete"]:
        s = ctx["series"][f["yf"]]
        i = f["session_index"]
        assert s.dates[i].isoformat() == f["date"]
        o, h, l, c = s.window(i, k)
        sc = eng.score(f["price"], f["side"], o, h, l, c, k)
        want = expected[(f["yf"], f["date"])]
        assert sc["u"] == pytest.approx(want[0], abs=1e-12)
        assert sc["pre"] == pytest.approx(want[1], abs=1e-12)
        assert sc["post"] == pytest.approx(want[2], abs=1e-12)
        vec = eng.score_many(s, np.array([i]), np.array([0.5]), f["side"], k, price_rule="close")
        for key in ("u", "pre", "post"):
            assert float(vec[key][0]) == pytest.approx(sc[key], abs=1e-12)
        if pcc is not None:
            sp = pcc.score(f["price"], f["side"], o, h, l, c, k)
            assert all(sp[key] == sc[key] for key in ("u", "pre", "post"))


def test_guard2_shifted_fill_date_changes_u(tmp_path):
    """Guard 2's fixture. A fill read on its engine date reproduces the
    expected u; the same fill shifted one session later is scored on another
    window and does not, so a test written against the wrong date fails."""
    days, arr = _line(120, dt.date(2025, 3, 3), "America/New_York", (9, 30), 50.0, 7)
    i = 60
    p = float(arr["c"][i])
    # widen the next session's range so the shifted fill is placed there rather than re-dated back
    arr["h"][i + 1] = max(arr["h"][i + 1], p + 1.0)
    arr["l"][i + 1] = min(arr["l"][i + 1], p - 1.0)
    want_u = _expected(arr, i, 1)[0]
    lines = {"T:CCC|CCC": (days, arr, "ARCA", "USD")}
    right = eng.build(SPEC, _write_inputs(tmp_path, lines, [_fill("T:CCC|CCC", days[i], 1, p, "USD")]))
    f = right["complete"][0]
    o, h, l, c = right["series"][f["yf"]].window(f["session_index"], 3)
    assert eng.score(f["price"], 1, o, h, l, c, 3)["u"] == pytest.approx(want_u, abs=1e-12)
    wrong = eng.build(SPEC, _write_inputs(tmp_path, lines, [_fill("T:CCC|CCC", days[i + 1], 1, p, "USD")]))
    g = wrong["complete"][0]
    assert g["session_date"] == days[i + 1].isoformat()
    o, h, l, c = wrong["series"][g["yf"]].window(g["session_index"], 3)
    assert abs(eng.score(g["price"], 1, o, h, l, c, 3)["u"] - want_u) > 1e-6


def test_guard2_non_session_fill_date_is_excluded_and_counted():
    raw = {"SYM": {"bars": [{"date": "2026-09-25", "Close": 10.0}, {"date": "2026-09-29", "Close": 10.5}]}}
    fills = [{"symbol": "SYM", "date": "2026-09-25", "sleeve": "C", "line": "SYM", "side": "B", "kind": "sleeve"},
             {"symbol": "SYM", "date": "2026-09-28", "sleeve": "C", "line": "SYM", "side": "S", "kind": "sleeve"}]
    priced, guard2 = ad.price_fills(fills, raw)
    assert [r["date"] for r in priced] == ["2026-09-25"] and priced[0]["price"] == 10.0
    assert [r["date"] for r in guard2] == ["2026-09-28"] and guard2[0]["reason"].startswith("guard 2")


# ----------------------------------------------------------------------------
# Adapter: the weight-to-fill derivation and the overlay-induced fills
# ----------------------------------------------------------------------------
def test_weight_changes_become_fills_rise_buy_fall_sell_unchanged_none_btc_excluded():
    dates = ["2026-09-08", "2026-09-14", "2026-09-21"]          # a Tuesday roll, then Mondays
    alloc = {"X": [0.5, 0.6, 0.6], "Y": [0.5, 0.4, 0.0], "BTC-USD": [0.0, 0.0, 0.4]}
    fills, excluded = ad.derive_line_fills(dates, alloc, "C")
    got = {(r["line"], r["date"]): (r["side"], r["dw_abs"], r["kind"]) for r in fills}
    assert got == {("X", "2026-09-08"): ("B", 0.5, "sleeve"), ("Y", "2026-09-08"): ("B", 0.5, "sleeve"),
                   ("X", "2026-09-14"): ("B", 0.1, "sleeve"), ("Y", "2026-09-14"): ("S", 0.1, "sleeve"),
                   ("Y", "2026-09-21"): ("S", 0.4, "sleeve")}
    assert ("X", "2026-09-21") not in got                       # an unchanged weight is no fill
    assert [(r["line"], r["date"], r["side"]) for r in excluded] == [("BTC-USD", "2026-09-21", "B")]
    assert excluded[0]["reason"].startswith("BTC-USD excluded")


def test_a_change_below_half_a_published_unit_is_no_fill():
    fills, _ = ad.derive_line_fills(["2026-09-14", "2026-09-21"], {"X": [0.30, 0.30004]}, "A")
    assert [(r["date"], r["side"]) for r in fills] == [("2026-09-14", "B")]


def _toy_pub(gate_events, tilt_events):
    def sleeve(vec):
        return {"headline": {"weekly_allocation_dates": ["2026-03-16"], "weekly_allocation": {k: [v] for k, v in vec.items()}}}
    return {"sleeves": {"A": sleeve({"CSP1": 0.6, "SOXX": 0.4}), "B": sleeve({"SPY": 1.0}),
                        "C": sleeve({"ARKK": 0.8, "BTC-USD": 0.2}), "D": sleeve({"EXH1": 1.0})},
            "overlay": {"events": gate_events, "gate_parameters": {"derisk_fraction": 0.5, "fallback_ticker": "SHY"},
                        "phase22_eem_tilt": {"enabled": True, "events": tilt_events, "signal_stale": False,
                                             "parameters": {"tilt_weight": 0.10, "eem_ticker": "EEM"}}}}


def test_a_gate_flip_derives_overlay_induced_fills_on_every_held_line():
    pub = _toy_pub([{"date": "2026-03-20", "direction": "RISK_OFF"}], [])
    fills, excluded = ad.derive_overlay_induced_fills(pub)
    got = {(r["sleeve"], r["line"]): (r["side"], round(r["dw_abs"], 8), r["kind"]) for r in fills}
    assert got == {("A", "CSP1"): ("S", 0.105, "overlay_induced"), ("A", "SOXX"): ("S", 0.07, "overlay_induced"),
                   ("B", "SPY"): ("S", 0.175, "overlay_induced"), ("C", "ARKK"): ("S", 0.04, "overlay_induced"),
                   ("D", "EXH1"): ("S", 0.1, "overlay_induced")}
    assert [(r["sleeve"], r["line"]) for r in excluded] == [("C", "BTC-USD")]
    legs = ad.derive_overlay_fills(pub["overlay"])
    assert [(r["sleeve"], r["line"], r["side"], r["dw_abs"], r["kind"]) for r in legs] == [("GATE", "SHY", "B", 0.5, "overlay_leg")]


def test_a_tilt_flip_rescales_sleeve_b_and_a_later_gate_flip_rescales_the_tilt_leg():
    pub = _toy_pub([{"date": "2026-03-25", "direction": "RISK_OFF"}], [{"date": "2026-03-18", "direction": "EM_TILT_ON"}])
    fills, _ = ad.derive_overlay_induced_fills(pub)
    on_tilt = {(r["sleeve"], r["line"]): (r["side"], round(r["dw_abs"], 8)) for r in fills if r["date"] == "2026-03-18"}
    assert on_tilt == {("B", "SPY"): ("S", 0.1)}                 # B funds the tilt: 0.35 -> 0.25
    on_gate = {(r["sleeve"], r["line"]): (r["side"], round(r["dw_abs"], 8)) for r in fills if r["date"] == "2026-03-25"}
    assert on_gate[("TILT", "EEM")] == ("S", 0.05)               # the tilt leg halves with every equity line
    assert on_gate[("B", "SPY")] == ("S", 0.125)


# ----------------------------------------------------------------------------
# Engine: the cluster relation is the rebalance date alone (amendment 2)
# ----------------------------------------------------------------------------
def test_cluster_relation_is_by_rebalance_date_only():
    assert SPEC["placebo"]["block_relations"] == ["same_session_date"]
    fills = [{"yf": "A:X|X", "session_date": "2026-09-14", "session_index": 100},
             {"yf": "A:Y|Y", "session_date": "2026-09-14", "session_index": 100},
             {"yf": "A:X|X", "session_date": "2026-09-21", "session_index": 105}]   # same line, windows overlap
    by_date = eng.make_blocks(fills, 3, tuple(SPEC["placebo"]["block_relations"]))
    assert by_date[0] == by_date[1] and by_date[2] != by_date[0]
    chained = eng.make_blocks(fills, 3, eng.BLOCK_RELATIONS_PCC)    # the disclosure null's relations
    assert chained[0] == chained[1] == chained[2]


def test_confirmatory_set_starts_at_the_blend_inception():
    fills = [{"date": "2018-10-29"}, {"date": "2018-10-31"}, {"date": "2018-11-05"}]
    assert eng.confirmatory_mask(fills, SPEC).tolist() == [False, True, True]
    assert dt.date(2018, 10, 31).strftime("%A") == "Wednesday"


# ----------------------------------------------------------------------------
# Adapter: the parity guard on the within-window ratios (amendment 8)
# ----------------------------------------------------------------------------
def _parity_case(n: int = 40):
    days = _weekdays(dt.date(2026, 6, 1), n)
    rng = np.random.default_rng(3)
    close = 100 * np.cumprod(1 + rng.normal(0, 0.01, n))
    adj = close * 0.98                                           # a dividend basis below the close
    bars = [{"date": d.isoformat(), "Close": float(c), "Adj Close": float(a)} for d, c, a in zip(days, close, adj)]
    raw = {"SYM": {"bars": bars}}
    fills = [{"sleeve": "B", "line": "SYM", "symbol": "SYM", "date": days[i].isoformat(), "side": "B", "kind": "sleeve", "price": float(close[i])}
             for i in (10, 20, 30)]
    return days, adj, raw, fills


def test_a_level_shift_between_vendors_passes_the_ratio_test():
    days, adj, raw, fills = _parity_case()
    panel = {d.isoformat(): float(a * 1.05) for d, a in zip(days, adj)}     # five per cent off in level, everywhere
    kept, excluded, rec = ad.window_parity(fills, raw, lambda s, l, y: panel, basis_mismatch={})
    assert len(kept) == 3 and not excluded and rec["windows_over_tol"] == 0


def test_a_window_failing_the_ratio_test_by_a_planted_one_per_cent_is_excluded_and_counted():
    days, adj, raw, fills = _parity_case()
    panel = {d.isoformat(): float(a) for d, a in zip(days, adj)}
    panel[days[21].isoformat()] *= 1.01                          # inside the second fill's window (t+1)
    kept, excluded, rec = ad.window_parity(fills, raw, lambda s, l, y: panel, basis_mismatch={})
    assert [r["date"] for r in excluded] == [days[20].isoformat()]
    assert excluded[0]["reason"].startswith("parity: a within-window price ratio")
    assert rec["windows_over_tol"] == 1 and rec["fills_excluded"] == 1 and len(kept) == 2
    assert rec["excluded_share"] == pytest.approx(1 / 3, abs=1e-6)
    panel[days[21].isoformat()] /= 1.01
    panel[days[21].isoformat()] *= 1.0009                        # inside the tolerance: kept
    kept, excluded, rec = ad.window_parity(fills, raw, lambda s, l, y: panel, basis_mismatch={})
    assert not excluded and rec["worst_ratio_dev_kept"] == pytest.approx(0.0009, rel=1e-3)


def test_a_missing_panel_bar_in_the_window_excludes_the_fill():
    days, adj, raw, fills = _parity_case()
    panel = {d.isoformat(): float(a) for d, a in zip(days, adj)}
    del panel[days[29].isoformat()]
    kept, excluded, rec = ad.window_parity(fills, raw, lambda s, l, y: panel, basis_mismatch={})
    assert [r["date"] for r in excluded] == [days[30].isoformat()] and rec["windows_missing_a_panel_bar"] == 1


def test_159801_sz_is_excluded_whole_as_a_basis_mismatch():
    days, adj, raw, fills = _parity_case()
    raw = {"159801.SZ": raw["SYM"]}
    fills = [dict(r, sleeve="C", line="159801.SZ", symbol="159801.SZ") for r in fills]

    def panel_for(s, l, y):
        raise AssertionError("the guard must not compare a basis-mismatch line")

    kept, excluded, rec = ad.window_parity(fills, raw, panel_for)
    assert not kept and len(excluded) == 3
    assert all(r["reason"].startswith("basis mismatch") for r in excluded)
    assert rec["basis_mismatch_excluded"] == [{"line": "C:159801.SZ", "fills": 3}]


def test_a_line_without_a_panel_is_declared_unchecked_and_counted_not_passed_silently():
    days, adj, raw, fills = _parity_case()

    def panel_for(s, l, y):
        raise ad.ParityReferenceUnavailable("no panel")

    kept, excluded, rec = ad.window_parity(fills, raw, panel_for, basis_mismatch={})
    assert len(kept) == 3 and not excluded
    assert rec["lines_unchecked"] == 1 and rec["unchecked"][0]["line"] == "B:SYM" and rec["lines_checked"] == 0


def test_engine_inputs_carry_the_kind_and_a_same_side_pair_is_one_unit_with_both_kinds(tmp_path):
    """A sleeve fill and an overlay-induced fill on one line, date and side are
    one unit (rebalance date x line x side) carried with both kinds, so the
    overlay rows can be shown on their own (amendment 6)."""
    days, arr = _line(30, dt.date(2026, 3, 2), "America/New_York", (9, 30), 20.0, 5)
    bars = [{"date": d.isoformat(), "Open": float(arr["o"][i]), "High": float(arr["h"][i]), "Low": float(arr["l"][i]),
             "Close": float(arr["c"][i]), "Adj Close": float(arr["c"][i])} for i, d in enumerate(days)]
    raw = {"SPY": {"bars": bars, "metadata": {"exchangeName": "PCX", "exchangeTimezoneName": "America/New_York",
                                              "currency": "USD", "longName": "toy"}}}
    d = days[15].isoformat()
    base = {"sleeve": "B", "line": "SPY", "symbol": "SPY", "date": d, "side": "S", "price": float(arr["c"][15])}
    priced = [dict(base, kind="sleeve", notional_nav=0.01), dict(base, kind="overlay_induced", notional_nav=0.02),
              dict(base, date=days[20].isoformat(), side="B", price=float(arr["c"][20]), kind="overlay_leg", notional_nav=0.05)]
    rows, meta, extract, record = ad.engine_inputs(priced, raw)
    assert [r["kind"] for r in rows] == ["sleeve", "overlay_induced", "overlay_leg"]
    (tmp_path / "f.json").write_text(json.dumps(rows), encoding="utf-8")
    (tmp_path / "b.json").write_text(json.dumps({"meta": meta}), encoding="utf-8")
    (tmp_path / "h.json").write_text(json.dumps(extract), encoding="utf-8")
    units, excluded = eng.load_fills(SPEC, rows, {"meta": meta}, extract)
    assert not excluded and len(units) == 2
    assert units[0]["kinds"] == ["overlay_induced", "sleeve"] and units[0]["qty"] == pytest.approx(0.03 / base["price"])
    assert units[1]["kinds"] == ["overlay_leg"]


# ----------------------------------------------------------------------------
# The verdict-deciding code (S2-3 of the spec-freeze review); each test below
# fails against a planted mutant of the engine (the mutation check is recorded
# in the PREREG's build record)
# ----------------------------------------------------------------------------
@pytest.mark.parametrize("p, effect, powered, thin, parity, reconciled, want", [
    (0.010, 0.120, True, False, 0.0, True, "GIVE-BACK-AT-SIZE"),
    (0.010, 0.100, True, False, 0.0, True, "GIVE-BACK-AT-SIZE"),        # the floor itself clears
    (0.050, 0.120, True, False, 0.0, True, "GIVE-BACK-AT-SIZE"),        # p at alpha passes ("at or below 0.05")
    (0.010, 0.0999, True, False, 0.0, True, "GIVE-BACK-BELOW-SIZE"),    # just below the floor
    (0.010, 0.050, True, False, 0.0, True, "GIVE-BACK-BELOW-SIZE"),     # significant, below the 10 bp floor
    (0.200, 0.050, True, False, 0.0, True, "NO-GIVE-BACK"),
    (0.010, 0.120, False, False, 0.0, True, "SUGGESTIVE"),             # a demoted pass
    (0.010, 0.050, False, False, 0.0, True, "SUGGESTIVE"),
    (0.200, 0.050, False, False, 0.0, True, "UNRESOLVED"),             # a demoted fail
    (0.010, 0.120, True, True, 0.0, True, "INCONCLUSIVE"),             # thinness on a pass
    (0.010, 0.050, True, True, 0.0, True, "INCONCLUSIVE"),
    (0.200, 0.050, True, True, 0.0, True, "NO-GIVE-BACK"),             # a failing H-D2 carries no thinness suffix
    (0.010, 0.120, True, False, 0.11, True, "INCONCLUSIVE"),           # parity exclusions above a tenth
    (0.200, 0.050, True, False, 0.11, True, "INCONCLUSIVE"),
    (0.010, 0.120, True, True, 0.11, False, "INFEASIBLE"),             # INFEASIBLE takes precedence
])
def test_every_verdict_branch_and_its_precedence(p, effect, powered, thin, parity, reconciled, want):
    st = eng.clause_status(p, effect, 0.10, powered, 0.05)
    assert eng.map_verdict(st, thin, SPEC["thinness"]["scope"], parity, reconciled, SPEC["parity_inconclusive_share"]) == want


def test_a_missing_reconciliation_or_parity_record_stops():
    with pytest.raises(SystemExit):
        eng.map_verdict("PASS", False, "passes_only", None, True, 0.10)
    with pytest.raises(SystemExit):
        eng.map_verdict("PASS", False, "passes_only", 0.0, None, 0.10)


def test_a_disclosure_null_without_usable_spread_is_degenerate_and_not_computed():
    ratio = SPEC["placebo"]["degenerate_below_sd_ratio"]
    assert ratio == 0.01
    assert eng.degenerate(0.0, 3.5e-4, ratio) and eng.degenerate(1.3e-7, 3.5e-4, ratio) and eng.degenerate(float("nan"), 3.5e-4, ratio)
    assert not eng.degenerate(2.6e-4, 3.5e-4, ratio)
    rng = np.random.default_rng(3)
    mask = np.ones(4, dtype=bool)
    flat = {"draws": 50, "direction": "both", "u": np.full((4, 50), 0.5), "post": 1e-9 * rng.random((4, 50))}
    live = {"draws": 50, "direction": "both", "u": rng.random((4, 50)), "post": 1e-3 * rng.random((4, 50))}
    rec_flat = eng.null_sds(flat, mask, 0.05, 0.001, 3.5e-4, ratio)
    rec_live = eng.null_sds(live, mask, 0.05, 0.001, 3.5e-4, ratio)
    assert rec_flat["status"].startswith("degenerate, not computed") and "power_at_delta2" not in rec_flat
    assert "status" not in rec_live and "power_at_delta2" in rec_live


def test_the_registered_parameters_are_the_amended_ones():
    assert SPEC["thinness"]["scope"] == "passes_only"
    assert SPEC["placebo"]["offset_direction"] == "forward"
    assert SPEC["floors"]["H_D2_delta_price"] == 0.0010
    assert (SPEC["placebo"]["offset_min_sessions"], SPEC["placebo"]["offset_max_sessions"]) == (4, 60)
    assert SPEC["confirmatory"]["from_fill_date"] == "2018-10-31"


def test_power_functions():
    assert eng.mde(1.0, 0.05, 0.8) == pytest.approx(1.644854 + 0.841621, abs=1e-5)
    assert eng.power_normal(eng.mde(1.0, 0.05, 0.8), 1.0, 0.05) == pytest.approx(0.80, abs=1e-9)
    assert eng.power_normal(0.0, 1.0, 0.05) == pytest.approx(0.05, abs=1e-9)
    assert eng.point_branch_size(10.0, 2.0, 0.8) == pytest.approx(10.0 + 0.841621 * 2.0, abs=1e-5)
    assert eng.point_branch_size(10.0, 2.0, 0.5) == pytest.approx(10.0, abs=1e-9)


def _series(n=300, start=dt.date(2025, 1, 6), prov_at=()):
    days, arr = _line(n, start, "America/New_York", (9, 30), 60.0, 9)
    bars = _bars(days, arr)
    for j in prov_at:
        bars[j]["p"] = True
    return days, eng.Series("T:X|X", {"name": "x", "history": bars}, "America/New_York", SPEC)


def test_forward_offsets_lie_after_the_fill_only():
    days, s = _series()
    fwd = eng.placebo_offsets(s, 100, SPEC, "forward")
    both = eng.placebo_offsets(s, 100, SPEC, "both")
    assert fwd.min() == 4 and fwd.max() == 60 and len(fwd) == 57
    assert set(fwd.tolist()) < set(both.tolist()) and both.min() == -60
    assert len(eng.placebo_offsets(s, 250, SPEC, "forward")) < 57          # near the end the forward pool is short


def test_the_forward_null_scores_sessions_after_the_fill(tmp_path):
    days, arr = _line(260, dt.date(2025, 1, 6), "America/New_York", (9, 30), 50.0, 81)
    i = 100
    c = arr["c"].copy()
    c[i + 4:] = c[i + 3] * np.cumprod(np.full(len(c) - i - 4, 1 - 0.005))      # flat to t+3, then falling 0.5 per cent a session
    arr = dict(arr, c=c, o=np.concatenate([[c[0]], c[:-1]]))
    arr["h"], arr["l"] = np.maximum(arr["o"], c) * 1.002, np.minimum(arr["o"], c) * 0.998
    args = _write_inputs(tmp_path, {"A:AAA|AAA": (days, arr, "ARCA", "USD")}, [_fill("A:AAA|AAA", days[i], 1, c[i], "USD")])
    ctx = eng.build(SPEC, args)
    fwd = eng.simulate_placebo(ctx, np.random.default_rng(3), 200, blocked=True, direction="forward")
    assert float(np.mean(fwd["post"][0])) > 0.01           # a buy placed before the fall: every forward placebo is adverse


def _three_set_run_inputs(tmp_path):
    """Two lines from 2018-06: fills before the blend's inception, inside the
    confirmatory window and too late for a full forward pool."""
    da, aa = _line(260, dt.date(2018, 6, 4), "America/New_York", (9, 30), 80.0, 21)
    db, ab = _line(230, dt.date(2018, 6, 4), "America/New_York", (9, 30), 30.0, 22)    # ends earlier
    lines = {"A:AAA|AAA": (da, aa, "ARCA", "USD"), "B:BBB|BBB": (db, ab, "ARCA", "USD")}
    fills = [_fill("A:AAA|AAA", da[i], s_, aa["c"][i], "USD") for i, s_ in ((60, 1), (120, -1), (150, 1), (180, -1), (220, 1))]
    fills += [_fill("B:BBB|BBB", db[i], s_, ab["c"][i], "USD") for i, s_ in ((120, 1), (180, -1))]   # 180: same date, too late on B
    return _write_inputs(tmp_path, lines, fills), da, db


def test_the_end_rule_and_the_three_sets(tmp_path):
    args, da, db = _three_set_run_inputs(tmp_path)
    ctx = eng.build(SPEC, args)
    sets = {(f["yf"], f["date"]): f["set"] for f in ctx["complete"]}
    assert sets[("A:AAA|AAA", da[60].isoformat())] == "pre_blend"            # 2018-08, before 2018-10-31
    assert sets[("A:AAA|AAA", da[120].isoformat())] == "confirmatory"
    assert sets[("A:AAA|AAA", da[180].isoformat())] == "confirmatory"         # 180 + 63 <= 259
    assert sets[("B:BBB|BBB", db[180].isoformat())] == "post_cutoff"          # 180 + 63 > 229
    assert sets[("A:AAA|AAA", da[220].isoformat())] == "post_cutoff"


def test_a_fill_on_the_blend_inception_day_is_confirmatory(tmp_path):
    days, arr = _line(260, dt.date(2018, 6, 4), "America/New_York", (9, 30), 40.0, 61)
    i = days.index(dt.date(2018, 10, 31))                       # Wednesday 2018-10-31, the blend's first close
    args = _write_inputs(tmp_path, {"A:AAA|AAA": (days, arr, "ARCA", "USD")},
                         [_fill("A:AAA|AAA", days[i - 1], 1, arr["c"][i - 1], "USD"), _fill("A:AAA|AAA", days[i], -1, arr["c"][i], "USD")])
    ctx = eng.build(SPEC, args)
    sets = {f["date"]: f["set"] for f in ctx["complete"]}
    assert sets == {"2018-10-30": "pre_blend", "2018-10-31": "confirmatory"}


def test_the_end_rule_boundary_is_the_last_bar_of_the_last_forward_window(tmp_path):
    days, arr = _line(260, dt.date(2025, 1, 6), "America/New_York", (9, 30), 50.0, 41)
    last = 259 - 60 - 3                                  # offset +60 needs bars to t+63
    lines = {"A:AAA|AAA": (days, arr, "ARCA", "USD")}
    args = _write_inputs(tmp_path, lines, [_fill("A:AAA|AAA", days[last], 1, arr["c"][last], "USD"),
                                           _fill("A:AAA|AAA", days[last + 1], -1, arr["c"][last + 1], "USD")])
    ctx = eng.build(SPEC, args)
    sets = {f["date"]: (f["set"], f["full_forward_pool"]) for f in ctx["complete"]}
    assert sets[days[last].isoformat()] == ("confirmatory", True)
    assert sets[days[last + 1].isoformat()] == ("post_cutoff", False)


def test_the_forward_null_draws_one_offset_per_rebalance_date_cluster(tmp_path):
    days, arr = _line(260, dt.date(2025, 1, 6), "America/New_York", (9, 30), 50.0, 51)
    lines = {"A:AAA|AAA": (days, arr, "ARCA", "USD"), "B:BBB|BBB": (days, arr, "ARCA", "USD")}   # identical bars
    args = _write_inputs(tmp_path, lines, [_fill(key, days[100], 1, arr["c"][100], "USD") for key in lines])
    small = _small_spec()
    ctx = eng.build(small, args)
    fwd, two, ind, ch = eng.simulate_all(ctx, small, uniform_variant=False)
    assert ctx["blocks"][0] == ctx["blocks"][1]
    assert np.array_equal(fwd["post"][0], fwd["post"][1])                  # one offset per draw for the cluster
    assert not np.array_equal(ind["post"][0], ind["post"][1])              # the independent disclosure null differs


def test_a_fill_with_no_placebo_on_either_side_stops_the_accumulated_null(tmp_path):
    days, arr = _line(260, dt.date(2025, 1, 6), "America/New_York", (9, 30), 50.0, 33)
    i = 100
    lines = {"A:AAA|AAA": (days, arr, "ARCA", "USD")}
    args = _write_inputs(tmp_path, lines, [_fill("A:AAA|AAA", days[i], 1, arr["c"][i], "USD")])
    hist = json.loads(Path(args.history).read_text(encoding="utf-8"))
    for j in list(range(i - 63, i - 6, 7)) + list(range(i + 7, i + 64, 7)):   # every window from -60 to +60 is dead
        hist["A:AAA|AAA"]["history"][j]["p"] = True
    Path(args.history).write_text(json.dumps(hist), encoding="utf-8")
    small = _small_spec()
    ctx = eng.build(small, args)
    assert ctx["complete"][0]["set"] == "confirmatory"
    with pytest.raises(SystemExit):
        eng.simulate_all(ctx, small, uniform_variant=False)


def test_a_post_cutoff_fill_does_not_narrow_its_cluster_in_the_forward_null(tmp_path):
    args, da, db = _three_set_run_inputs(tmp_path)
    ctx = eng.build(SPEC, args)
    F = ctx["complete"]
    conf, preb = eng.set_mask(F, "confirmatory"), eng.set_mask(F, "pre_blend")
    fwd = eng.simulate_placebo(ctx, np.random.default_rng(1), 50, blocked=True, direction="forward", members_mask=conf | preb)
    r_a = next(r for r, f in enumerate(F) if f["yf"] == "A:AAA|AAA" and f["date"] == da[180].isoformat())
    r_b = next(r for r, f in enumerate(F) if f["yf"] == "B:BBB|BBB" and f["date"] == db[180].isoformat())
    assert ctx["blocks"][r_a] == ctx["blocks"][r_b]                         # one rebalance date, one cluster
    assert fwd["offsets_count"][r_a] == 57 and not np.isnan(fwd["post"][r_a]).any()
    assert np.isnan(fwd["post"][r_b]).all()                                 # the post-cutoff fill is not simulated
    picks_after = np.isfinite(fwd["post"][conf | preb]).all()
    assert picks_after


def test_a_missing_forward_placebo_stops_the_coverage(tmp_path):
    days, arr = _line(260, dt.date(2025, 1, 6), "America/New_York", (9, 30), 50.0, 31)
    i = 100
    lines = {"A:AAA|AAA": (days, arr, "ARCA", "USD")}
    args = _write_inputs(tmp_path, lines, [_fill("A:AAA|AAA", days[i], 1, arr["c"][i], "USD")])
    hist = json.loads(Path(args.history).read_text(encoding="utf-8"))
    for j in range(i + 7, i + 64, 7):              # a provisional bar every seventh session kills every forward window
        hist["A:AAA|AAA"]["history"][j]["p"] = True
    Path(args.history).write_text(json.dumps(hist), encoding="utf-8")
    ctx = eng.build(SPEC, args)
    assert ctx["complete"][0]["set"] == "confirmatory"
    small = json.loads(json.dumps(SPEC))
    small["placebo"]["draws_per_fill"] = 20
    small["placebo"]["disclosure_nulls"]["two_sided_blocked"]["draws"] = 20
    small["placebo"]["disclosure_nulls"]["independent_per_fill"]["draws"] = 20
    small["placebo"]["disclosure_nulls"]["chained_same_line"]["draws"] = 20
    ctx["spec"] = small
    fwd, two, ind, ch = eng.simulate_all(ctx, small, uniform_variant=False)
    with pytest.raises(SystemExit):
        eng.coverage(ctx, fwd, two, ind, ch)


def _small_spec():
    small = json.loads(json.dumps(SPEC))
    small["bootstrap_draws"] = 100
    small["placebo"]["draws_per_fill"] = 300
    for key in ("two_sided_blocked", "independent_per_fill", "chained_same_line"):
        small["placebo"]["disclosure_nulls"][key]["draws"] = 100
    return small


def _full_synthetic_run(tmp_path):
    args, da, db = _three_set_run_inputs(tmp_path)
    small = _small_spec()
    ctx = eng.build(small, args)
    fwd, two, ind, ch = eng.simulate_all(ctx, small, uniform_variant=True)
    cov = eng.coverage(ctx, fwd, two, ind, ch)
    res = eng.run(ctx, fwd, cov, args, two, ind, ch)
    return ctx, fwd, two, ind, cov, res


def test_the_verdict_reads_the_forward_null_and_the_coverage_prints_no_centre(tmp_path):
    ctx, fwd, two, ind, cov, res = _full_synthetic_run(tmp_path)
    conf = eng.set_mask(ctx["complete"], "confirmatory")
    nm = eng.masked_means(fwd["post"], conf) * 100
    assert res["H_D2"]["null_mean"] == pytest.approx(round(float(nm.mean()), 4), abs=1e-12)
    assert res["H_D2"]["null_sd"] == pytest.approx(round(float(nm.std(ddof=1)), 4), abs=1e-12)
    mu = eng.masked_means(fwd["u"], conf)                                     # H-D1's headline null is the same forward null
    assert res["H_D1"]["null_mean"] == pytest.approx(round(float(mu.mean()), 4), abs=1e-12)
    assert "mean_post_pct" not in cov["null"]["confirmatory"] and "mean_u" not in cov["null"]["confirmatory"]
    assert res["n_confirmatory"] == int(conf.sum()) and res["n_post_cutoff"] == 2 and res["n_pre_blend"] == 1
    assert res["post_cutoff_disclosure"] is not None and res["pre_blend_disclosure"] is not None
    assert "sleeve_rebalance_only" not in res.get("by_kind", {})
    post = eng.set_mask(ctx["complete"], "post_cutoff")
    assert post.any() and np.isnan(fwd["post"][post]).all()             # post-cutoff fills never enter the forward null
    assert res["H_D2"]["n"] == res["n_confirmatory"]                     # nor do pre-blend fills enter the verdict cell
    _assert_the_verdict_arithmetic(ctx, fwd, cov, res)                   # with pre-blend and post-cutoff fills present


def test_weekly_fills_on_one_line_form_one_cluster_per_date_through_the_spec(tmp_path):
    days, arr = _line(200, dt.date(2025, 1, 6), "America/New_York", (9, 30), 50.0, 71)
    idx = [60, 65, 70, 75]                                               # five sessions apart: their windows overlap
    args = _write_inputs(tmp_path, {"A:AAA|AAA": (days, arr, "ARCA", "USD")},
                         [_fill("A:AAA|AAA", days[i], 1 if j % 2 == 0 else -1, arr["c"][i], "USD") for j, i in enumerate(idx)])
    ctx = eng.build(SPEC, args)
    assert len(set(ctx["blocks"].tolist())) == len(idx)                  # the date relation alone, read from the spec


def test_the_run_stops_when_the_frozen_null_is_not_drawn_again(tmp_path):
    ctx, fwd, two, ind, cov, res = _full_synthetic_run(tmp_path)
    bad = json.loads(json.dumps(cov))
    bad["null"]["confirmatory"]["draws_sha256_mean_post"] = "0" * 64
    with pytest.raises(SystemExit):
        eng.run(ctx, fwd, bad, None, two, ind, ind)


@pytest.mark.parametrize("path, value", [
    (("provenance", "engine_sha256"), "0" * 64), (("provenance", "spec_sha256"), "0" * 64),
    (("provenance", "history_json_sha256"), "0" * 64), (("provenance", "trades_json_sha256"), "0" * 64),
    (("provenance", "book_json_sha256"), "0" * 64), (("provenance", "fx_json_sha256"), "x"),
    (("counts", "complete"), -1), (("counts", "confirmatory"), -1), (("power", "H_D2", "delta"), 0.002),
    (("null", "confirmatory", "draws_sha256_mean_u"), "0" * 64),
])
def test_the_run_stops_on_any_difference_from_the_frozen_record(tmp_path, path, value):
    ctx, fwd, two, ind, cov, res = _full_synthetic_run(tmp_path)
    bad = json.loads(json.dumps(cov))
    node = bad
    for k in path[:-1]:
        node = node[k]
    node[path[-1]] = node[path[-1]] + 1 if value == -1 else value
    with pytest.raises(SystemExit):
        eng.run(ctx, fwd, bad, None, two, ind, ind)


def test_the_demotion_binds_on_the_p_test_power(tmp_path):
    ctx, fwd, two, ind, cov, res = _full_synthetic_run(tmp_path)
    for power, powered, states in ((0.79, False, ("SUGGESTIVE", "SUGGESTIVE-BELOW-FLOOR", "UNRESOLVED")),
                                   (0.80, True, ("PASS", "DETECTED-BELOW-FLOOR", "FAIL")),    # at the target counts as powered
                                   (0.81, True, ("PASS", "DETECTED-BELOW-FLOOR", "FAIL"))):
        c = json.loads(json.dumps(cov))
        c["power"]["H_D2"]["power_at_delta"] = power
        r = eng.run(ctx, fwd, c, None, two, ind, ind)
        assert r["verdict_inputs"]["powered_p_test"] is powered
        assert r["verdict_inputs"]["H_D2_status"] in states


def test_the_charts_pass_their_render_check_on_synthetic_results(tmp_path):
    ctx, fwd, two, ind, cov, res = _full_synthetic_run(tmp_path)
    rdir = tmp_path / "results"
    rdir.mkdir()
    (rdir / "results.json").write_text(json.dumps(res, default=float), encoding="utf-8")
    (rdir / "coverage.json").write_text(json.dumps(cov), encoding="utf-8")
    spec_c = importlib.util.spec_from_file_location("ws_fill_charts_under_test", ENGINE.parent / "charts.py")
    charts = importlib.util.module_from_spec(spec_c)
    spec_c.loader.exec_module(charts)
    charts.RESULTS_DIR, charts.CHART_DIR = rdir, tmp_path / "charts"
    charts.main()                                  # raises SystemExit if the render check fails
    report = json.loads((tmp_path / "charts" / "render_check.json").read_text(encoding="utf-8"))
    assert all(r["texts_outside_frame"] == 0 and r["tight_bbox_inside_frame"] for r in report.values())
    assert report["fig2_u_by_sleeve.png"]["marks"] == res["n_confirmatory"]


# ----------------------------------------------------------------------------
# Adapter: the gate's initial state (amendment 11)
# ----------------------------------------------------------------------------
def test_the_gate_starts_risk_off_at_the_blend_inception():
    pub = _toy_pub([{"date": "2026-03-25", "direction": "RISK_ON"}], [])
    pub["overlay"]["gated_variants"] = {ad.DEPLOYED_BLEND_KEY: {"dates": ["2026-03-20", "2026-03-23", "2026-03-24", "2026-03-25", "2026-03-26"],
                                                                "equity": [1.0, 1.0, 1.0, 1.0, 1.0]}}
    assert ad.gate_counts(pub["overlay"]) == (3, 2)          # three sessions RISK_OFF; the inception and the 25 March switches
    legs = ad.derive_overlay_fills(pub["overlay"])
    assert [(r["date"], r["side"], r["dw_abs"]) for r in legs] == [("2026-03-20", "B", 0.5), ("2026-03-25", "S", 0.5)]
    induced, _ = ad.derive_overlay_induced_fills(pub)
    assert {r["date"] for r in induced} == {"2026-03-25"}    # no induced fill on the inception day
    assert all(r["side"] == "B" for r in induced)


def test_derive_stops_unless_the_gate_model_reproduces_the_published_counts():
    pub = _toy_pub([{"date": "2026-03-25", "direction": "RISK_ON"}], [])
    pub["overlay"]["gated_variants"] = {ad.DEPLOYED_BLEND_KEY: {"dates": ["2026-03-20", "2026-03-23", "2026-03-24", "2026-03-25", "2026-03-26"],
                                                                "equity": [1.0, 1.0, 1.0, 1.0, 1.0]}}
    for payload in pub["sleeves"].values():
        payload["headline"]["headline_equity_dates"], payload["headline"]["headline_equity"] = ["2026-03-16"], [1.0]
    pub["overlay"]["days_risk_off"], pub["overlay"]["n_switches"] = 3, 2
    out = ad.derive(pub)
    assert (out["counts"]["gate_model"]["days_risk_off"], out["counts"]["gate_model"]["switches"]) == (3, 2)
    pub["overlay"]["n_switches"] = 1                          # the count of the superseded reading (inactive until the first event)
    with pytest.raises(SystemExit):
        ad.derive(pub)


# ----------------------------------------------------------------------------
# The verdict arithmetic end to end, on planted books (S2-A of the second
# spec-freeze review): build, the nulls, coverage and run, with the cells
# recomputed by hand from the forward null
# ----------------------------------------------------------------------------
def _planted_book(tmp_path, give_back, wick=None, drift=None, n_lines=8, n=480, spacing=10, first=70, seed=5, sd=0.001):
    """Low-volatility synthetic lines from Monday 2019-01-07 (weekdays, bars at
    the New York open), one fill every `spacing` sessions on each line from bar
    `first`, side at random, sizes of 1, 2 or 3 NAV units so that notional
    weights vary. give_back(line, date) moves the t+3 close against the fill by
    that fraction of price. wick(line), if given, plants a wick on the bar
    before each fill, (near, far) of the close on the fill's worse and better
    side, which fixes the window's range and so the fill's adverse rank u.
    drift(line), if given, adds that daily drift to the line, and every fill on
    it then takes the drift's side, as a momentum book would, which moves the
    forward null's centre well away from zero."""
    rng = np.random.default_rng(seed)
    days = _weekdays(dt.date(2019, 1, 7), n)
    last = n - 1 - 63                                        # every fill keeps its full forward pool
    lines, fills = {}, []
    for L in range(n_lines):
        key = f"S:L{L}|L{L}"
        mu = 0.0 if drift is None else drift(L)
        c = 50 * np.cumprod(1 + mu + rng.normal(0, sd, n))
        idx = list(range(first, last + 1, spacing))
        sides = rng.choice([-1, 1], size=len(idx)) if drift is None else np.full(len(idx), 1 if mu > 0 else -1)
        up, down = np.full(n, 0.002), np.full(n, 0.002)
        for i, s in zip(idx, sides):
            c[i + 3] *= 1 - s * give_back(L, days[i])
            if wick is not None:
                near, far = wick(L)
                up[i - 1], down[i - 1] = (near, far) if s > 0 else (far, near)
        o = np.concatenate([[c[0]], c[:-1]])
        arr = {"o": o, "h": np.maximum(o, c) * (1 + up), "l": np.minimum(o, c) * (1 - down), "c": c, "f": np.ones(n),
               "tz": "America/New_York", "open_hm": (9, 30)}
        lines[key] = (days, arr, "ARCA", "USD")
        for k_, (i, s) in enumerate(zip(idx, sides)):
            f = _fill(key, days[i], int(s), c[i], "USD")
            f["q"] = (1 + k_ % 3) / c[i]
            fills.append(f)
    return _write_inputs(tmp_path, lines, fills)


def _planted_run(tmp_path, give_back, wick=None, drift=None, seed=5):
    args = _planted_book(tmp_path, give_back, wick, drift, seed=seed)
    small = _small_spec()
    ctx = eng.build(small, args)
    fwd, two, ind, ch = eng.simulate_all(ctx, small, uniform_variant=True)
    cov = eng.coverage(ctx, fwd, two, ind, ch)
    res = eng.run(ctx, fwd, cov, args, two, ind, ch)
    return ctx, fwd, cov, res


def _assert_the_verdict_arithmetic(ctx, fwd, cov, res):
    """Every expected value below is computed with plain numpy from the forward
    null's matrix, not with the engine's helpers; rounded figures are compared to
    within one unit of their last decimal, since the engine's matrix-vector mean
    and numpy's mean may differ in the last bit."""
    F = ctx["complete"]
    conf = eng.set_mask(F, "confirmatory")
    apost = np.array([f["post"] for f in F])
    a = float(apost[conf].mean()) * 100
    per_draw = fwd["post"][conf].mean(axis=0)                                           # the null's mean post leg, per draw
    nm = per_draw * 100
    H, vi = res["H_D2"], res["verdict_inputs"]
    assert H["n"] == int(conf.sum())
    assert H["actual"] == pytest.approx(round(a, 4), abs=1.01e-4)
    assert H["null_mean"] == pytest.approx(round(float(nm.mean()), 4), abs=1.01e-4)
    assert H["effect"] == pytest.approx(round(a - float(nm.mean()), 4), abs=1.01e-4)     # actual less the forward null's centre
    assert H["p_one_sided_worse"] == pytest.approx(round(float((np.sum(nm >= a) + 1) / (len(nm) + 1)), 4), abs=1.01e-4)   # worse tail, +1
    assert vi["floor_delta_pct"] == 0.1                                                   # 10 bp, in the effect's per-cent units
    sd = float(per_draw.std(ddof=1))
    nd = NormalDist()
    assert cov["power"]["H_D2"]["sd_mean_post"] == pytest.approx(sd, abs=1.01e-8)         # the equal-weighted forward spread
    assert cov["power"]["H_D2"]["power_at_delta"] == pytest.approx(nd.cdf(0.001 / sd - nd.inv_cdf(0.95)), abs=1.01e-4)
    null_by_fill = fwd["post"].mean(axis=1)                                               # every draw, per fill
    for key, groups in (("line", np.array([f["yf"] for f in F])), ("year", np.array([f["session_date"][:4] for f in F]))):
        g_conf, detail = groups[conf], {d["dropped"]: d["effect"] for d in res["thinness"]["H_D2"][key]["detail"]}
        for g in sorted(set(g_conf.tolist())):
            keep = g_conf != g
            if keep.any():
                assert detail[g] == pytest.approx(round(float(apost[conf][keep].mean() - null_by_fill[conf][keep].mean()) * 100, 4), abs=1.01e-4)
    th = res["thinness"]["H_D2"]
    assert vi["thinness_fires"] == bool(th["line"]["sign_flips"] or th["year"]["sign_flips"])


PLANTED = {   # give_back, wick, drift, seed, expected H-D2 status, expected verdict
    "a give-back of 50 bp on every fill": (lambda L, d: 0.005, None, None, 5, "PASS", "GIVE-BACK-AT-SIZE"),
    "a give-back of 5 bp on every fill": (lambda L, d: 0.0005, None, None, 5, "DETECTED-BELOW-FLOOR", "GIVE-BACK-BELOW-SIZE"),
    "one line carries the give-back": (lambda L, d: 0.02 if L == 0 else -0.001, None, None, 5, "PASS", "INCONCLUSIVE"),
    "one calendar year carries the give-back": (lambda L, d: 0.006 if d.year == 2019 else -0.002, None, None, 5, "PASS", "INCONCLUSIVE"),
    "H-D1 thin, H-D2 not": (lambda L, d: 0.005, lambda L: (0.02, 0.5) if L == 0 else (0.6, 0.5), None, 5, "PASS", "GIVE-BACK-AT-SIZE"),
    # no give-back at all; the seed puts p mid-range (0.56), where a wrong alpha would pass it
    "no give-back": (lambda L, d: 0.0, None, None, 9, "FAIL", "NO-GIVE-BACK"),
    # a momentum book: the forward null's centre sits near -30 bp, so the raw mean (about -15 bp) and the effect
    # (about +15 bp) lie on opposite sides of the 10 bp floor
    "the side follows the drift": (lambda L, d: 0.0015, None, lambda L: 0.001 if L % 2 == 0 else -0.001, 5, "PASS", "GIVE-BACK-AT-SIZE"),
}


@pytest.mark.parametrize("case", list(PLANTED))
def test_planted_books_reach_the_registered_verdict_by_the_registered_arithmetic(tmp_path, case):
    give_back, wick, drift, seed, status, verdict = PLANTED[case]
    ctx, fwd, cov, res = _planted_run(tmp_path, give_back, wick, drift, seed)
    _assert_the_verdict_arithmetic(ctx, fwd, cov, res)
    vi, th, H = res["verdict_inputs"], res["thinness"], res["H_D2"]
    assert vi["powered_p_test"] is True and vi["H_D2_status"] == status and res["verdict"] == verdict
    if case == "no give-back":
        assert 0.2 < H["p_one_sided_worse"] < 0.7
    if case == "the side follows the drift":
        assert H["null_mean"] < -0.2 and H["actual"] < 0.1 < H["effect"]
    if case == "one line carries the give-back":
        assert th["H_D2"]["line"]["sign_flips"] and not th["H_D2"]["year"]["sign_flips"]
    if case == "one calendar year carries the give-back":
        assert th["H_D2"]["year"]["sign_flips"] and not th["H_D2"]["line"]["sign_flips"]
    if case == "H-D1 thin, H-D2 not":                                                     # thinness reads H-D2 alone
        assert th["H_D1"]["line"]["sign_flips"] and not (th["H_D2"]["line"]["sign_flips"] or th["H_D2"]["year"]["sign_flips"])
