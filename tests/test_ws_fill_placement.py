"""Fill-placement diagnostic (reviews/2026-10-03_fill-placement-diagnostic/):
the engine copy and the adapter, on synthetic data only.

Python months are 1-indexed (January = 1); every date literal below is
1-indexed. No test reads a fetched bar, an engine panel or any actual fill.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import ws_fill_placement_adapter as ad  # noqa: E402

ENGINE = REPO / "reviews" / "2026-10-03_fill-placement-diagnostic" / "engine" / "fill_timing.py"
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
    (tmp / "book.json").write_text(json.dumps({"meta": meta, "_provenance": {"fixture": True}}), encoding="utf-8")
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
