"""Actual rounded D baseline through the shipped book, gate and sender.

Python datetime months are 1-indexed. No vendor fetch or real email is used.
"""
from datetime import datetime, timezone
from pathlib import Path
import inspect
import math
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from test_component_sender import fixture_book, install, NOW
import component_release as cr
import send_component_factsheet as sender
import refresh_all
from component_contract import (expected_budgets,
                                risk_only_hold, unchanged_hold_budget)
from live_targets import _intended_lines


def test_actual_rounded_hold_has_exactly_zero_trades_and_explicit_residual():
    book, basis = fixture_book(rounded_d=True)
    d = [r for r in book["lines"] if r["sleeve"] == "D"]
    assert math.isclose(sum(r["held"] for r in basis["lines"]), 1.000055)
    assert {r["etf"] for r in d} == {"EXV1", "EXV3", "EXH1"}
    assert all(r["target"] == r["held"] and r["delta"] == 0 and not r["risk_adjustment"] for r in d)
    assert book["rounding_residual_nav"] == pytest.approx(.00002, abs=1e-15)
    assert sum(r["target"] for r in book["lines"]) == pytest.approx(1.00002)
    assert risk_only_hold(book, "D")
    assert not cr.validate_book(book, basis, NOW)["d_ready"]


@pytest.mark.parametrize("now", [datetime(2026, 8, 1, 6, tzinfo=timezone.utc),
                                  datetime(2027, 1, 2, 6, tzinfo=timezone.utc)])
def test_rounded_hold_month_and_year_boundaries(now):
    book, basis = fixture_book(now=now, rounded_d=True)
    assert not cr.validate_book(book, basis, now)["d_ready"]


@pytest.mark.parametrize("gate", [False, True])
def test_rounding_is_not_a_blanket_price_exemption(tmp_path, monkeypatch, gate):
    install(tmp_path, monkeypatch, rounded_d=True, gate=gate)
    quotes = cr.read(tmp_path / "data/holdings_prices_1y.json")
    # The real incident: all D funds stop at Thursday, while US quotes are current.
    for key in ("EXV1", "EXV3", "EXH1"):
        quotes["prices"][key]["dates"] = ["2026-09-10"]
    cr.write(tmp_path / "data/holdings_prices_1y.json", quotes)
    if gate:
        with pytest.raises(ValueError, match="missing price observation"):
            cr.preflight(tmp_path, NOW)
        with pytest.raises(ValueError, match="missing price observation"):
            cr.seal(tmp_path, now=NOW)
    else:
        assert not cr.preflight(tmp_path, NOW)["d_ready"]
        sealed = cr.seal(tmp_path, now=NOW)
        assert not set(sealed["quote_evidence"]) & {"EXV1", "EXV3", "EXH1"}
        # A genuine core change still requires its own current price.
        assert "SPY" in sealed["quote_evidence"]


def test_no_send_rounding_rehearsal_reaches_preview_and_remains_deduplicated(tmp_path, monkeypatch):
    install(tmp_path, monkeypatch, rounded_d=True)
    quotes = cr.read(tmp_path / "data/holdings_prices_1y.json")
    for key in ("EXV1", "EXV3", "EXH1"):
        del quotes["prices"][key]
    cr.write(tmp_path / "data/holdings_prices_1y.json", quotes)
    release = cr.seal(tmp_path, now=NOW)
    assert sender.prepare(tmp_path, NOW)["action"] == "preview"
    assert not (tmp_path / sender.LEDGER).exists()
    decision = sender.prepare(tmp_path, NOW, reserve=True)
    html = sender.render(decision, release)
    assert "portfolio-risk adjustment only" not in html
    assert "D holdings are unchanged; small rounding differences in totals are not trades." in html
    sent = []
    sender.send(tmp_path, NOW, transport=lambda *a: sent.append(a), env={})
    assert len(sent) == 1
    assert sender.plan(tmp_path, NOW)[0]["action"] == "wait"


@pytest.mark.parametrize("mutation", ["omitted", "null", "forged", "hidden_resize", "unexplained_target"])
def test_residual_cannot_hide_a_trade_or_nav_error(mutation):
    book, basis = fixture_book(rounded_d=True)
    if mutation == "omitted":
        del book["rounding_residual_nav"]
    elif mutation == "null":
        book["rounding_residual_nav"] = None
    elif mutation == "forged":
        book["rounding_residual_nav"] = .001
    elif mutation == "hidden_resize":
        for r in book["lines"]:
            if r["sleeve"] == "D":
                r["target"] = r["held"] / .20002 * .2
                r["delta"] = r["target"] - r["held"]
        book["rounding_residual_nav"] = 0
    else:
        book["lines"][0]["target"] += .00003
        book["lines"][0]["delta"] += .00003
    with pytest.raises(ValueError):
        cr.validate_book(book, basis, NOW)


def test_only_registered_unchanged_budget_qualifies():
    base = expected_budgets({"gate_on": False, "tilt_on": False})["d"]
    reduced = expected_budgets({"gate_on": True, "tilt_on": False})["d"]
    assert unchanged_hold_budget("D", base + .00002, base)
    assert unchanged_hold_budget("D", reduced - .00002, reduced)
    assert not unchanged_hold_budget("D", base + .00002, reduced)
    assert not unchanged_hold_budget("D", reduced - .00002, base)
    assert not unchanged_hold_budget("D", base + .0002, base)
    assert not unchanged_hold_budget("D", base, base + .000001)
    assert not unchanged_hold_budget("A", base, base)


def test_risk_reduction_and_restoration_remain_real_trades():
    for old_gate, new_gate in ((False, True), (True, False)):
        book, basis = fixture_book(gate=new_gate)
        old_budget = expected_budgets({"gate_on": old_gate, "tilt_on": False})["d"]
        held = {(r["sleeve"], r["etf"]): r["held"] for r in basis["lines"]}
        held[("D", "EXV1")] = old_budget + .00002
        lines = _intended_lines(book["sleeves"], held, book["overlay_decision"]["weights"], adjust_overlays=True)
        d = next(r for r in lines if r["sleeve"] == "D")
        assert d["risk_adjustment"] and d["delta"] != 0
        assert d["target"] == pytest.approx(book["overlay_decision"]["weights"]["d"])


def test_preflight_is_after_live_book_and_before_pages_and_full_tests():
    source = inspect.getsource(refresh_all.main)
    assert 'script == "scripts/live_targets.py"' in source
    assert '"scripts/component_release.py", "preflight"' in source
    assert source.index('"scripts/component_release.py", "preflight"') < source.index('# ----- Step 7')
    # The final seal still follows every guard and the regression suite.
    assert source.index('"pytest (regression suite)"') < source.index('"seal component publication"')
