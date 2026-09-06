"""The automatic factsheet release (2026-09-06): one false condition holds.

The release marker was a countersignature written by a person after reading
the week. It stopped two sends in four weeks whose panel was "current to
Friday" but whose content was not — hollow Friday rows on 2026-08-30, and
sleeve C held for a blank member on 2026-09-06. Both are now visible to a
machine, so the verdict is taken by the weekend refresh and every condition
is pinned here: the release fires only when all of them hold, each failure
is named, the hold file vetoes, and the marker carries its evidence.

Every input is a file under a temporary data/docs pair; the readiness
sub-check is stubbed. Python datetime months are 1-indexed (January = 1).
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import auto_release as ar  # noqa: E402

ANCHOR = date(2026, 9, 4)
OK = lambda: (0, "MECHANICALLY READY")  # noqa: E731
BAD = lambda: (1, "NOT READY - fix the FAIL lines above")  # noqa: E731


def _site(tmp_path, *, end="2026-09-04", statuses=("READY",) * 4, final=True,
          decision="2026-09-04", fill_decision="2026-09-04", all_current=True,
          basis=("norgate", "norgate"), published=None, hold=None):
    data, docs = tmp_path / "data", tmp_path / "docs"
    data.mkdir(); docs.mkdir()
    (data / "breadth_csp1.json").write_text(json.dumps({"end_date": end}), encoding="utf-8")
    sleeves = [{"sleeve": s, "status": st, "decision_session": decision,
                "decision_session_for_fill": fill_decision}
               for s, st in zip("ABCD", statuses)]
    (data / "live_targets.json").write_text(json.dumps(
        {"as_of": decision, "targets_final": final, "sleeves": sleeves}), encoding="utf-8")
    (data / "strategy_freshness.json").write_text(json.dumps(
        {"all_current": all_current,
         "strategies": [{"sleeve": s, "status": "current" if all_current else "behind",
                         "data_through": end} for s in "ABCD"]}), encoding="utf-8")
    for name, src in (("asset_class_prices_cache", basis[0]),
                      ("thematic_prices_cache", basis[1])):
        if src is not None:
            (data / f"{name}.source.json").write_text(json.dumps({"source": src}),
                                                        encoding="utf-8")
    if published:
        (docs / "factsheet_published.json").write_text(json.dumps({"anchor": published}),
                                                        encoding="utf-8")
    if hold is not None:
        (docs / "factsheet_hold.json").write_text(hold, encoding="utf-8")
    return data, docs


def _ev(data, docs, **kw):
    kw.setdefault("cadence", "weekend")
    kw.setdefault("price_source", "norgate")
    kw.setdefault("readiness", OK)
    kw.setdefault("env", {})
    return ar.evaluate(ANCHOR, data_dir=data, docs_dir=docs, **kw)


def _failed(v):
    return [c["check"] for c in v["checks"] if not c["ok"]]


def test_every_condition_met_releases(tmp_path):
    v = _ev(*_site(tmp_path))
    assert v["release"] is True and _failed(v) == []
    assert v["anchor"] == "2026-09-04"
    assert len(v["checks"]) == 9


def test_post_fill_cadence_never_releases(tmp_path):
    v = _ev(*_site(tmp_path), cadence="post-fill")
    assert v["release"] is False and _failed(v) == ["weekend cadence"]


def test_a_held_sleeve_holds_the_week(tmp_path):
    """The 2026-09-06 09:47 SGT book: sleeve C HOLD on a blank member."""
    v = _ev(*_site(tmp_path, statuses=("READY", "READY", "HOLD", "READY"), final=False))
    assert v["release"] is False
    assert _failed(v) == ["every sleeve ranked on its fill's close"]
    assert "HOLD: C" in [c for c in v["checks"] if not c["ok"]][0]["detail"]


def test_a_sleeve_not_ranked_on_its_fill_close_holds(tmp_path):
    """targets_final can be true only when every sleeve was ranked on the
    close its fill uses; a provisional book must not release."""
    v = _ev(*_site(tmp_path, final=False, decision="2026-09-02",
                   fill_decision="2026-09-04"))
    assert v["release"] is False
    assert "every sleeve ranked on its fill's close" in _failed(v)


def test_stale_panel_holds(tmp_path):
    v = _ev(*_site(tmp_path, end="2026-08-28"))
    assert "panel reaches the anchor" in _failed(v)


def test_data_behind_holds(tmp_path):
    v = _ev(*_site(tmp_path, all_current=False))
    assert "every sleeve's data reaches its venue's last close" in _failed(v)


def test_a_silent_basis_fallback_holds(tmp_path):
    """Requested Norgate, one cache built from yfinance: a restatement
    nobody chose, held for a person (WS19)."""
    v = _ev(*_site(tmp_path, basis=("norgate", "yfinance")))
    assert "price basis as requested" in _failed(v)


def test_yfinance_requested_accepts_unrecorded_caches(tmp_path):
    """A cache with no sidecar was a yfinance download (pre-2026-09-03)."""
    v = _ev(*_site(tmp_path, basis=(None, None)), price_source="yfinance")
    assert "price basis as requested" not in _failed(v)


def test_auto_source_requires_norgate_actually_taken(tmp_path):
    v = _ev(*_site(tmp_path, basis=("yfinance", "norgate")), price_source="auto")
    assert "price basis as requested" in _failed(v)


def test_staged_roster_promotion_holds(tmp_path):
    v = _ev(*_site(tmp_path), env={"BTE_APPLY_STAGED_ROSTER": "1"})
    assert "no staged roster promotion" in _failed(v)


def test_already_published_does_not_release_again(tmp_path):
    v = _ev(*_site(tmp_path, published="2026-09-04"))
    assert "not already published for this anchor" in _failed(v)


def test_readiness_failure_holds(tmp_path):
    v = _ev(*_site(tmp_path), readiness=BAD)
    assert "publication readiness checks" in _failed(v)


def test_operator_hold_vetoes(tmp_path):
    v = _ev(*_site(tmp_path, hold=json.dumps({"held_at_utc": "2026-09-06T10:00:00Z",
                                                "note": "restating sleeve D"})))
    assert v["release"] is False and _failed(v) == ["no operator hold"]
    assert "restating sleeve D" in [c for c in v["checks"] if not c["ok"]][0]["detail"]


def test_a_malformed_hold_file_still_holds(tmp_path):
    """The veto fails toward not sending."""
    v = _ev(*_site(tmp_path, hold="{not json"))
    assert "no operator hold" in _failed(v)


def test_release_if_ready_writes_the_marker_with_its_evidence(tmp_path):
    data, docs = _site(tmp_path)
    v = ar.release_if_ready(ANCHOR, cadence="weekend", price_source="norgate",
                            data_dir=data, docs_dir=docs, env={}, readiness=OK)
    assert v["release"] is True
    marker = json.loads((docs / "factsheet_release.json").read_text(encoding="utf-8"))
    assert marker["approved_anchor"] == "2026-09-04"
    assert marker["auto"] is True and marker["forced"] is False
    assert [c["check"] for c in marker["conditions"]] == [c["check"] for c in v["checks"]]
    assert all(c["ok"] for c in marker["conditions"])


def test_release_if_ready_writes_nothing_on_hold(tmp_path):
    data, docs = _site(tmp_path, final=False, statuses=("HOLD",) * 4)
    v = ar.release_if_ready(ANCHOR, cadence="weekend", price_source="norgate",
                            data_dir=data, docs_dir=docs, env={}, readiness=OK)
    assert v["release"] is False
    assert not (docs / "factsheet_release.json").exists()


def test_report_names_every_failed_condition(tmp_path):
    v = _ev(*_site(tmp_path, all_current=False, published="2026-09-04"), cadence="post-fill")
    text = ar.format_report(v)
    assert text.startswith("auto-release 2026-09-04: HOLD")
    for name in ("weekend cadence", "every sleeve's data reaches its venue's last close",
                 "not already published for this anchor"):
        assert f"FAIL {name}" in text


def test_missing_inputs_hold_rather_than_release(tmp_path):
    """No live_targets.json at all: the condition reads False, never True."""
    data, docs = _site(tmp_path)
    (data / "live_targets.json").unlink()
    v = _ev(data, docs)
    assert "every sleeve ranked on its fill's close" in _failed(v)
    assert v["release"] is False


@pytest.mark.parametrize("anchor, end", [
    (date(2026, 8, 28), "2026-08-31"),   # month boundary: panel into September
    (date(2026, 12, 31), "2027-01-04"),  # year boundary: panel into 2027
])
def test_panel_condition_across_month_and_year_boundaries(tmp_path, anchor, end):
    data, docs = _site(tmp_path, end=end)
    v = ar.evaluate(anchor, cadence="weekend", price_source="norgate",
                    data_dir=data, docs_dir=docs, env={}, readiness=OK)
    assert "panel reaches the anchor" not in _failed(v)
