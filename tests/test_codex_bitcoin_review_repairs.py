"""Repairs from the Codex review of the 2026-09-19 Bitcoin incident.

Six findings, each pinned at the level it failed rather than at the level it
was reported. Every vendor call is stubbed; nothing here sends, publishes or
recomputes a filed measurement.

Python datetime months are 1-indexed (January = 1).
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import btc_basis  # noqa: E402
import component_release as cr  # noqa: E402
import send_component_factsheet as sender  # noqa: E402
from component_contract import (  # noqa: E402
    hold_rounding_residual, registered_budgets, unchanged_hold_budget,
)
from component_publication import email_wording  # noqa: E402
from test_component_sender import NOW, fixture_book, install  # noqa: E402


# ---------------------------------------------------------------------------
# 1. Sender readiness wording
#
# A verified core can now contain a held C, and the cover text said "A-C ready"
# regardless. The held sleeve is ALSO invisible in the changes table - its
# positions did not move, so every line sits under the 1e-8 threshold - which
# means prose is the only place the reader can learn about it.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("held,d_hold,expect", [
    ((), True, "A-C ready"),
    ((), False, "all strategies ready"),
    (("C",), True, "A and B ready; C on HOLD"),
    (("C",), False, "A and B ready; C on HOLD"),
    (("B", "C"), True, "A ready; B and C on HOLD"),
    (("A", "B", "C"), True, "A-C on HOLD"),
])
def test_the_cover_text_states_which_core_sleeves_actually_ranked(held, d_hold, expect):
    w = email_wording({"action": "regular", "d_hold": d_hold, "core_held": held})
    assert expect in w["subject"], w["subject"]
    assert expect in w["heading"] or "all strategies" in w["heading"]
    if held:
        assert "on HOLD for selection" in w["core_status"]
        # The all-held sentence uses the range form ("Strategies A-C"), so
        # assert the phrase rather than each letter.
        assert expect.split(";")[-1].strip() in w["subject"]
        assert "Keep the existing selection for" in w["summary"]
    else:
        assert "A-C and the portfolio overlays are verified" in w["core_status"]


def test_a_held_core_sleeve_is_never_described_as_ready():
    for action in ("preview", "regular", "d_update"):
        w = email_wording({"action": action, "d_hold": True, "core_held": ("C",)})
        joined = " ".join(w.values())
        assert "A-C ready" not in joined
        assert "Strategies A-C and the portfolio overlays are verified" not in joined


def _held_c_site(tmp_path, monkeypatch, *, d_ready):
    """A sealed release whose sleeve C is on an authorised HOLD."""
    install(tmp_path, monkeypatch, ready=d_ready)
    book = cr.read(tmp_path / "data/live_targets.json")
    basis = cr.read(tmp_path / "data/component_held_basis.json")
    hold = ("C",) if d_ready else ("C",)
    rebuilt, _ = fixture_book(d_ready=d_ready, hold=hold)
    cr.write(tmp_path / "data/live_targets.json", rebuilt)
    prices = {r["etf"]: {"dates": [rebuilt["as_of"]], "prices": [100]}
              for r in rebuilt["lines"]}
    cr.write(tmp_path / "data/holdings_prices_1y.json", {"prices": prices})
    return cr.seal(tmp_path, now=NOW)


@pytest.mark.parametrize("d_ready", [True, False])
def test_the_held_c_reaches_html_text_and_pdf(tmp_path, monkeypatch, d_ready):
    """THROUGH THE ACTUAL SENDER, not the wording function alone."""
    from component_factsheet_view import render_pdf, render_text
    release = _held_c_site(tmp_path, monkeypatch, d_ready=d_ready)
    assert release["held_sleeves"] == ["C"] if d_ready else \
        release["held_sleeves"] == ["C", "D"]

    decision = sender.prepare(tmp_path, NOW, reserve=True)
    assert "C" in decision["core_held"], decision
    html = sender.render(decision, release)
    text = render_text(decision, release)
    pdf = render_pdf(decision, release)

    for surface, body in (("html", html), ("text", text)):
        assert "C on HOLD" in body or "C is on HOLD" in body, surface
        assert "A-C ready" not in body, surface
    assert b"%PDF" == pdf[:4]
    assert len(pdf) > 1000
    # The held sleeve has no row in the changes table, which is exactly why
    # the sentence has to exist.
    changed = [r for r in release["book"]["lines"]
               if r["sleeve"] == "C" and abs(r["delta"]) > 1e-8]
    assert not changed, "a held C should propose no trade"


def test_delivery_deduplication_and_changed_core_review_survive(tmp_path, monkeypatch):
    """The wording changed; the routing, ledger keys and dedup must not."""
    release = _held_c_site(tmp_path, monkeypatch, d_ready=True)
    sent = []
    decision = sender.prepare(tmp_path, NOW, reserve=True)
    assert decision["action"] == "regular"
    sender.send(tmp_path, NOW, transport=lambda *a: sent.append(a), env={})
    assert len(sent) == 1
    # Deduplicated: a second plan proposes nothing.
    assert sender.plan(tmp_path, NOW)[0]["action"] == "wait"
    # A changed core after distribution still demands operator review. Driven
    # at email_decision, because plan() short-circuits on the ledger anchor
    # before it reaches this branch.
    from component_publication import Snapshot, email_decision
    anchor = release["anchor"]
    sent_ledger = {"anchor": anchor, "core": release["core_identity"],
                   "europe": release["europe_identity"], "regular": "all_ready"}
    again = email_decision(
        NOW, anchor=anchor,
        core=Snapshot("a-different-core-identity", True, anchor, anchor),
        europe=Snapshot(release["europe_identity"], release["d_ready"],
                        anchor, anchor),
        sent=sent_ledger, core_held=("C",))
    assert again["action"] == "alert" and again["audience"] == "operator"
    assert "review required" in again["reason"]
    # And the held sleeve rides along without disturbing the routing.
    assert again["core_held"] == ("C",)


# ---------------------------------------------------------------------------
# 2. Persisted WS21 publication containment
#
# seal checked BTE_C_BTC_BASIS in its OWN environment. The engine can be run
# under the flag in one shell and sealed from another: the staged construction
# persists in thematic_rotation.json while the sealing environment is clean.
# ---------------------------------------------------------------------------
def test_a_staged_artefact_refuses_to_seal_with_the_flag_unset(tmp_path, monkeypatch):
    install(tmp_path, monkeypatch)
    monkeypatch.delenv(btc_basis.ENV_VAR, raising=False)
    cr.seal(tmp_path, now=NOW)                      # clean artefact seals

    artefact = tmp_path / "data/thematic_rotation.json"
    cr.write(artefact, {**cr.read(artefact), "c_btc_basis": "ibit"})
    with pytest.raises(ValueError, match="staged sleeve C Bitcoin basis"):
        cr.seal(tmp_path, now=NOW)


def test_verification_also_checks_the_construction(tmp_path, monkeypatch):
    """DEFENCE IN DEPTH, and honest about which gate binds.

    The seal-time check is the binding one: it reads the working tree, where
    a staged artefact actually appears. `verify(committed=True)` reads the
    COMMIT, so a staged working tree does not reach it, and a staged artefact
    that WAS committed changes the sealed source digest and is refused one
    check earlier. This gate exists for a seal produced by an older build,
    and is asserted directly plus wired into verify.
    """
    staged = {"c_btc_basis": "ibit"}
    with pytest.raises(ValueError, match="staged sleeve C Bitcoin basis"):
        cr.assert_incumbent_construction(tmp_path, reader=lambda _p: staged)
    cr.assert_incumbent_construction(tmp_path,
                                     reader=lambda _p: {"c_btc_basis": "incumbent"})
    src = (ROOT / "scripts" / "component_release.py").read_text(encoding="utf-8")
    verify_body = src[src.index("def verify("):]
    assert "assert_incumbent_construction(root, reader)" in verify_body
    seal_body = src[src.index("def seal("):src.index("def verify(")]
    assert "assert_incumbent_construction(root)" in seal_body


def test_a_legacy_incumbent_artefact_is_still_accepted(tmp_path):
    """No declaration at all is the incumbent: every artefact before today."""
    (tmp_path / "data").mkdir()
    cr.write(tmp_path / "data/thematic_rotation.json", {"universe": []})
    cr.assert_incumbent_construction(tmp_path)
    cr.write(tmp_path / "data/thematic_rotation.json", {"c_btc_basis": "incumbent"})
    cr.assert_incumbent_construction(tmp_path)


# ---------------------------------------------------------------------------
# 3. Spot prices masquerading as IBIT evidence
#
# price_evidence read `prices.get(proxy) or prices.get(key)` and labelled the
# result price_key=proxy. Codex reproduced a 60000 spot quote accepted as
# evidence for IBIT, a fund trading near 35.
# ---------------------------------------------------------------------------
def _book_with_btc(tmp_path, monkeypatch, prices):
    install(tmp_path, monkeypatch)
    book = cr.read(tmp_path / "data/live_targets.json")
    basis = cr.read(tmp_path / "data/component_held_basis.json")
    line = dict(book["lines"][0])
    line.update(sleeve="C", etf="BTC-USD", traded="IBIT",
                held=0.0, target=0.0, delta=0.0)
    book["lines"].append(line)
    cr.write(tmp_path / "data/holdings_prices_1y.json", {"prices": prices})
    return book, basis


def test_a_spot_quote_is_refused_as_evidence_for_ibit(tmp_path, monkeypatch):
    base = {"SPY": {"dates": ["2026-09-11"], "prices": [500]}}
    book, _ = _book_with_btc(tmp_path, monkeypatch, {
        **base, "BTC-USD": {"dates": ["2026-09-11"], "prices": [60000]}})
    book["lines"][-1].update(target=0.02, delta=0.02)
    with pytest.raises(ValueError, match="different instrument"):
        cr.price_evidence(tmp_path, book)


def test_a_genuine_ibit_quote_is_accepted_and_labelled(tmp_path, monkeypatch):
    base = {"SPY": {"dates": ["2026-09-11"], "prices": [500]}}
    book, _ = _book_with_btc(tmp_path, monkeypatch, {
        **base, "IBIT": {"dates": ["2026-09-11"], "prices": [35.4]}})
    book["lines"][-1].update(target=0.02, delta=0.02)
    evidence = cr.price_evidence(tmp_path, book)
    assert evidence["BTC-USD"] == {"price_key": "IBIT", "session": "2026-09-11",
                                   "price": 35.4}


def test_a_venue_suffixed_alias_is_still_accepted(tmp_path, monkeypatch):
    """EXV1 -> EXV1.DE is the SAME fund quoted on its exchange, which is how
    resolve_book_symbol builds it. EXH3 -> EXH4.DE is not, and neither is
    BTC-USD -> IBIT."""
    from etf_registry import ETF_REGISTRY
    assert ETF_REGISTRY["EXV1"]["yfinance_trading_proxy"] == "EXV1.DE"
    assert ETF_REGISTRY["EXH3"]["yfinance_trading_proxy"] == "EXH4.DE"
    assert "EXV1.DE".split(".")[0] == "EXV1"
    assert "EXH4.DE".split(".")[0] != "EXH3"
    assert "IBIT".split(".")[0] != "BTC-USD"


def test_the_holding_return_view_uses_the_same_rule():
    src = (ROOT / "scripts" / "component_factsheet_view.py").read_text(encoding="utf-8")
    block = src[src.index("    holding_returns = []"):src.index("    # The equity path")]
    assert 'prices.get(proxy) or prices.get(line["etf"], {})' not in block
    assert 'proxy.split(".")[0] == line["etf"]' in block


# ---------------------------------------------------------------------------
# 4. Frozen-anchor protection
#
# freeze() overwrote the parquet AND its sidecar together, so a second run
# replaced the anchor and the hash certifying it, and the loader accepted the
# replacement. Every value after the cut-over is a ratio off S_c.
# ---------------------------------------------------------------------------
def test_the_freezer_refuses_to_overwrite_an_existing_artefact(tmp_path):
    import freeze_btc_proxy_history as fz
    parquet = tmp_path / "frozen.parquet"
    parquet.write_bytes(b"not really a parquet")
    with pytest.raises(fz.FreezeRefused, match="already exists"):
        fz.freeze(parquet=parquet, sidecar=tmp_path / "frozen.json")
    assert parquet.read_bytes() == b"not really a parquet", "left untouched"


def test_check_mode_still_works_over_an_existing_artefact(tmp_path):
    """--check must stay usable: it is how you compare without overwriting.

    THE SOURCE IS SYNTHETIC, AND HAS TO BE (2026-09-20). This called freeze()
    without a cache, so it read the default — data/thematic_prices_cache.parquet,
    the gitignored sleeve C cache. That file exists on the main clone and can
    never exist on a runner, so the test passed locally and failed CI on every
    push, whatever the push contained. The sibling above passes in CI only
    because its refusal fires before check_source() is reached. A test may read
    a committed artefact; it may not read an ignored one.
    """
    import numpy as np
    import pandas as pd
    import freeze_btc_proxy_history as fz
    cut = pd.Timestamp(btc_basis.CUTOVER)
    # Built from the same calendar build_segment() reindexes to, so the
    # segment is the whole series and the cut-over row is its last.
    idx = fz._nyse_sessions(cut - pd.Timedelta(days=30), cut)
    source = tmp_path / "thematic_prices_cache.parquet"
    pd.DataFrame({btc_basis.SPOT_KEY: np.linspace(40000.0, 45000.0, len(idx))},
                 index=idx).to_parquet(source)
    parquet = tmp_path / "frozen.parquet"
    parquet.write_bytes(b"x")
    report = fz.freeze(cache=source, parquet=parquet,
                       sidecar=tmp_path / "frozen.json", write=False)
    assert report["rows"] == len(idx) and report["sha256"] is None
    assert report["last"] == str(cut.date())
    assert report["S_c"] == 45000.0
    assert parquet.read_bytes() == b"x", "--check wrote to the artefact"


def test_the_production_anchor_is_pinned_independently_of_its_sidecar():
    """THE SECOND WITNESS. The sidecar sits beside the parquet and is rewritten
    with it, so agreement between them cannot tell the registered anchor from a
    replacement. The digest in source can only move through a commit."""
    assert btc_basis.REGISTERED_SHA256 == btc_basis.file_sha256(
        btc_basis.FROZEN_PARQUET)
    sidecar = json.loads(btc_basis.FROZEN_SIDECAR.read_text(encoding="utf-8"))
    assert sidecar["sha256"] == btc_basis.REGISTERED_SHA256
    assert sidecar["S_c"] == 45674.257342138306
    assert sidecar["rows"] == 1517


def test_a_replacement_anchor_is_refused_even_with_a_matching_sidecar(tmp_path,
                                                                     monkeypatch):
    """The exact defect: re-freeze rewrites both, and the pair agrees."""
    import numpy as np
    import pandas as pd
    idx = pd.DatetimeIndex([pd.Timestamp("2024-01-10"), pd.Timestamp(btc_basis.CUTOVER)])
    series = pd.Series([1.0, 2.0], index=idx, name=btc_basis.SPOT_KEY)
    parquet = tmp_path / "replacement.parquet"
    sidecar = tmp_path / "replacement.json"
    series.to_frame(btc_basis.SPOT_KEY).to_parquet(parquet)
    sidecar.write_text(json.dumps({"sha256": btc_basis.file_sha256(parquet)}),
                       encoding="utf-8")
    # Internally consistent, and NOT the production artefact: allowed. This
    # is the state the old guard could not distinguish from the real one.
    monkeypatch.setattr(btc_basis, "FROZEN_PARQUET", parquet)
    monkeypatch.setattr(btc_basis, "FROZEN_SIDECAR", sidecar)
    loaded, _ = btc_basis.load_frozen_segment()
    assert float(loaded.loc[pd.Timestamp(btc_basis.CUTOVER)]) == 2.0
    # Now let it stand where production stands: the registered digest, which
    # no rewrite of the pair can touch, refuses it.
    monkeypatch.setattr(btc_basis, "PRODUCTION_PARQUET", parquet)
    with pytest.raises(btc_basis.BasisError, match="REGISTERED_SHA256"):
        btc_basis.load_frozen_segment()
    _ = np


# ---------------------------------------------------------------------------
# 5. The rounding contract, generalised as one piece
#
# Generalising unchanged_hold_budget alone would preserve a held C or B basket
# while the residual still counted D, and the target book would fail to
# conserve NAV - a refusal to publish, for the wrong reason, at the worst time.
# ---------------------------------------------------------------------------
def test_d_behaviour_is_exactly_what_it_was():
    assert registered_budgets("D") == [0.10, 0.20]
    assert unchanged_hold_budget("D", 0.20002, 0.20)
    assert unchanged_hold_budget("D", 0.09998, 0.10)
    assert not unchanged_hold_budget("D", 0.20002, 0.10)
    assert not unchanged_hold_budget("D", 0.2002, 0.20), "beyond the tolerance"


def test_b_carries_its_tilt_dependent_budgets():
    """B is the sleeve the EM tilt comes out of, so it has four."""
    budgets = registered_budgets("B")
    assert len(budgets) == 4
    assert budgets[-1] == pytest.approx(0.35)
    for b in budgets:
        assert unchanged_hold_budget("B", b + 0.00002, b)
    # The smallest gap is 500x the tolerance, so no real move reads as rounding.
    gaps = [b - a for a, b in zip(budgets, budgets[1:])]
    assert min(gaps) > 100 * 1e-4


@pytest.mark.parametrize("sleeve", ["A", "B", "C", "D"])
def test_a_real_budget_transition_is_always_a_trade(sleeve):
    budgets = registered_budgets(sleeve)
    for held_at in budgets:
        for target in budgets:
            if target != held_at:
                assert not unchanged_hold_budget(sleeve, held_at, target), (
                    f"{sleeve} {held_at} -> {target} must remain a trade")


def test_an_unregistered_budget_never_qualifies():
    assert not unchanged_hold_budget("C", 0.12, 0.12)
    assert not unchanged_hold_budget("D", 0.15, 0.15)


def test_the_residual_sums_over_every_held_sleeve():
    budgets = {"a": 0.35, "b": 0.35, "c": 0.10, "d": 0.20}
    sleeves = [{"sleeve": "C", "status": "HOLD"}, {"sleeve": "D", "status": "HOLD"},
               {"sleeve": "A", "status": "READY"}]
    rows = [{"sleeve": "C", "held": 0.10002}, {"sleeve": "D", "held": 0.20003},
            {"sleeve": "A", "held": 0.35}]
    assert hold_rounding_residual(sleeves, rows, budgets) == pytest.approx(5e-5)
    # A READY sleeve contributes nothing however its holdings round.
    ready_only = [{"sleeve": "A", "status": "READY"}]
    assert hold_rounding_residual(ready_only, rows, budgets) == 0.0


def test_a_held_sleeve_off_its_registered_budget_contributes_no_residual():
    """It is being resized, so its lines are a trade and there is no residual."""
    budgets = {"a": 0.35, "b": 0.35, "c": 0.10, "d": 0.20}
    sleeves = [{"sleeve": "D", "status": "HOLD"}]
    rows = [{"sleeve": "D", "held": 0.1}]      # held at 10%, budget now 20%
    assert hold_rounding_residual(sleeves, rows, budgets) == 0.0


def test_a_rounded_held_c_basket_conserves_nav_end_to_end():
    """The two halves together: preserved basket AND its residual accounted."""
    from live_targets import _intended_lines
    book, basis = fixture_book(d_ready=True, hold=("C",))
    budgets = book["overlay_decision"]["weights"]
    held = {(r["sleeve"], r["etf"]): float(r["held"]) for r in basis["lines"]}
    held[("C", "SMH")] = budgets["c"] + 0.00002        # rounded model weight
    lines = _intended_lines(book["sleeves"], held, budgets, adjust_overlays=True)
    c = [r for r in lines if r["sleeve"] == "C"]
    assert all(r["target"] == r["held"] and r["delta"] == 0 for r in c), \
        "an unchanged registered budget preserves the basket exactly"
    residual = hold_rounding_residual(book["sleeves"], lines, budgets)
    assert residual == pytest.approx(0.00002, abs=1e-12)
    assert sum(r["target"] for r in lines) == pytest.approx(1.0 + residual,
                                                            abs=1e-9)


def test_todays_actual_c_basket_still_produces_exactly_zero_deltas():
    """Codex verified this; it is pinned so a future change cannot quietly
    introduce a rounding residual on the live book."""
    book = json.loads((ROOT / "data" / "live_targets.json").read_text(encoding="utf-8"))
    status = {s["sleeve"]: s["status"] for s in book["sleeves"]}
    if status.get("C") != "HOLD":
        pytest.skip(f"sleeve C is {status.get('C')} in the current book; the\n"
                    f"claim is about a HELD basket, and a ranked one trades")
    c = [r for r in book["lines"] if r["sleeve"] == "C"]
    assert c and all(abs(float(r["delta"])) <= 1e-8 for r in c)


# ---------------------------------------------------------------------------
# 6. M1 disclosure
# ---------------------------------------------------------------------------
def test_the_m1_note_names_all_three_terms():
    import ws21_measure as w
    note = w._m1_note()
    for term in ("USDT/USD basis", "expense ratio", "premium/discount"):
        assert term in note, term
    assert "premium/discount and nothing else" not in note
    assert "no decomposition was performed" in note


def test_the_filed_disclosure_carries_the_caveat_in_markdown():
    md = (ROOT / "reviews" / "2026-09-19_ws21_measurements.md").read_text(encoding="utf-8")
    assert "USDT/USD basis" in md
    assert "cumulative ratio" in md.lower()
    assert "not attributable" in md.lower()


def test_the_registered_calculation_and_stop_band_are_unchanged():
    import ws21_measure as w
    payload = json.loads((ROOT / "reviews" / "2026-09-19_ws21_measurements.json")
                         .read_text(encoding="utf-8"))
    assert w.M1_STOP_BAND == 0.010 and w.M4_STOP == 1e-4
    assert payload["M1"]["stop_band"] == 0.010
    assert payload["M1"]["stop_condition"] == "PASS"
    assert payload["M1"]["p5"] == pytest.approx(-0.00203, abs=1e-4)
    assert payload["M1"]["p95"] == pytest.approx(0.00208, abs=1e-4)


# ---------------------------------------------------------------------------
# Policy boundaries the repairs must NOT have moved
# ---------------------------------------------------------------------------
def test_ws21_construction_and_default_are_untouched():
    assert btc_basis.DEFAULT == btc_basis.INCUMBENT
    assert btc_basis.CUTOVER == date(2024, 1, 11)
    assert btc_basis.TRADED == "IBIT" and btc_basis.SPOT_KEY == "BTC-USD"


def test_the_coverage_floor_is_untouched():
    src = (ROOT / "scripts" / "live_targets.py").read_text(encoding="utf-8")
    assert "coverage_floor" in src
    assert "have < total * coverage_floor" in src


def test_the_validator_accepts_all_four_sleeves_on_hold():
    """STATED, NOT SILENTLY DECIDED. The generic contract admits it: each HOLD
    is separately authorised, carries a reason and is risk-only, and the book
    then proposes no trade at all. That is coherent - there is nothing to
    review and nothing to fill - but it has never occurred and is not an
    endorsed publication policy. Flagged for the owner in the handoff.
    """
    book, basis = fixture_book(hold=("A", "B", "C"))       # D holds by default
    verdict = cr.validate_book(book, basis, NOW)
    assert verdict["held_sleeves"] == ["A", "B", "C", "D"]
    assert book["targets_final"] is False
    assert all(abs(r["delta"]) <= 1e-8 for r in book["lines"]
               if r["sleeve"] in "ABCD")
