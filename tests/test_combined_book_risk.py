"""Arithmetic, data-contract and snapshot-validity tests for the reporting-only
combined-book risk view (scripts/combined_book_risk.py). Regression cases for
the Codex reviews are marked with the finding they cover: F1-F5 from
handoffs/2026-10-28_combined-book-risk-view-review.md and R1-R7 from the
seven-findings re-review.

Every mutation test starts from a valid, available baseline built by the
production path, and asserts the specific reason it expects.

Python datetime months are 1-indexed (January = 1); pandas Periods likewise.
"""

import json
import math
import os
import shutil
import signal
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import combined_book_risk as cbr  # noqa: E402
from venue_calendars import get_calendar  # noqa: E402

RNG = np.random.default_rng(20261027)
NOW = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Known covariance fixtures
# --------------------------------------------------------------------------

def test_single_asset_vol_and_full_share():
    r = RNG.normal(0, 0.01, size=(300, 1))
    d = cbr.decompose(np.array([1.0]), cbr.annualised_covariance(r))
    assert d["vol"] == pytest.approx(np.std(r, ddof=1) * np.sqrt(252), rel=1e-12)
    assert d["share"][0] == pytest.approx(1.0)


def test_uncorrelated_assets_share_is_own_variance():
    d = cbr.decompose(np.array([0.5, 0.5]), np.diag([0.04, 0.01]))
    var = 0.25 * 0.04 + 0.25 * 0.01
    assert d["variance"] == pytest.approx(var)
    assert d["share"] == pytest.approx([0.25 * 0.04 / var, 0.25 * 0.01 / var])
    assert sum(d["vol_contrib"]) == pytest.approx(d["vol"])


def test_hedging_asset_has_negative_contribution():
    d = cbr.decompose(np.array([0.8, 0.2]), np.array([[0.04, -0.03], [-0.03, 0.04]]))
    assert d["share"][1] < 0
    assert sum(d["share"]) == pytest.approx(1.0)


def test_zero_variance_shares_are_undefined():
    d = cbr.decompose(np.array([0.3, 0.7]), np.zeros((2, 2)))
    assert d["vol"] == 0.0 and d["share"] == [None, None] and d["vol_contrib"] == [None, None]


def test_variance_equals_fixed_weight_synthetic_series():
    r = RNG.normal(0, 0.01, size=(250, 4)) @ np.array(
        [[1, .3, 0, 0], [0, 1, .2, 0], [0, 0, 1, -.4], [0, 0, 0, 1]])
    w = np.array([0.4, 0.3, 0.2, 0.1])
    d = cbr.decompose(w, cbr.annualised_covariance(r))
    assert d["variance"] == pytest.approx(np.var(r @ w, ddof=1) * 252, rel=1e-12)
    assert sum(d["share"]) == pytest.approx(1.0, abs=1e-12)


def test_duplicating_instrument_across_sleeves_leaves_risk_unchanged():
    cov = cbr.annualised_covariance(RNG.normal(0, 0.01, size=(250, 2)))
    merged = cbr.decompose(np.array([0.6, 0.4]), cov)
    lines = [{"sleeve": "A", "proxy": "X", "held": 0.25},
             {"sleeve": "B", "proxy": "X", "held": 0.35},
             {"sleeve": "B", "proxy": "Y", "held": 0.40}]
    per = cbr.line_contributions(lines, ["X", "Y"], merged["sigma_w"],
                                 merged["variance"], merged["vol"])
    assert per[0]["share"] + per[1]["share"] == pytest.approx(merged["share"][0])
    assert sum(l["share"] for l in per) == pytest.approx(1.0)
    assert per[0]["share"] / per[1]["share"] == pytest.approx(0.25 / 0.35)


def test_correlation_is_undefined_not_nan_divided():
    corr = cbr.correlation(np.array([[0.04, 0.0], [0.0, 0.0]]))
    assert corr[0, 0] == pytest.approx(1.0) and np.isnan(corr[0, 1]) and np.isnan(corr[1, 1])


# --------------------------------------------------------------------------
# Data-contract fixtures
# --------------------------------------------------------------------------

CUTOFF = "2026-10-23"
# Real registry keys so the project's own resolvers run: CSP1 trades as SPY
# (shared proxy with B's SPY), EXV1 is a Xetra EUR line.
BOOK = [("A", "CSP1", 0.30), ("B", "SPY", 0.20), ("B", "DBC", 0.30), ("D", "EXV1", 0.20)]
PROV = {**cbr.ENGINE_FX_PROVENANCE, "aligned_to": "EXV1.DE",
        "exported_at_utc": "2026-10-27T01:00:00Z"}


def sessions(venue, end=CUTOFF, n=252):
    sched = get_calendar(venue).schedule(start_date="2025-06-01", end_date=end)
    return [d.strftime("%Y-%m-%d") for d in sched.index[-n:]]


def walk(dates, seed, start=100.0, constant=False):
    if constant:
        return [start] * len(dates)
    g = np.random.default_rng(seed)
    return [float(x) for x in start * np.cumprod(1 + g.normal(0, 0.01, len(dates)))]


def fx_export(dates, values, prov=PROV):
    """A synthetic operator export in the validated contract."""
    return {"schema": cbr.FX_EXPORT_SCHEMA, "provenance": dict(prov),
            "dates": list(dates), "close": list(values)}


def write_fixture(tmp: Path, book=BOOK, target_override=None, fx_last=CUTOFF,
                  constant=False, fx_values=None):
    tmp.mkdir(parents=True, exist_ok=True)
    lines = [{"sleeve": s, "etf": e, "held": h} for s, e, h in book]
    (tmp / "component_held_basis.json").write_text(json.dumps(
        {"anchor": CUTOFF, "model_as_of": "2026-10-20", "lines": lines,
         "rounding_residual_nav": 0.0}))
    lt_lines = [{"sleeve": s, "etf": e, "held": h,
                 "target": (target_override or {}).get(e, h)} for s, e, h in book]
    (tmp / "live_targets.json").write_text(json.dumps({"as_of": CUTOFF, "lines": lt_lines}))
    eff = {}
    for _, e, h in book:
        eff[e] = eff.get(e, 0) + h
    (tmp / "live_track.json").write_text(json.dumps(
        {"anchor_date": CUTOFF, "effective_weights": eff}))
    nyse, xetr = sessions("NYSE"), sessions("XETR")
    prices = {"SPY": {"dates": nyse, "prices": walk(nyse, 1, constant=constant)},
              "DBC": {"dates": nyse, "prices": walk(nyse, 2, constant=constant)},
              "EXV1.DE": {"dates": xetr, "prices": walk(xetr, 3, constant=constant)}}
    (tmp / "holdings_prices_1y.json").write_text(json.dumps(
        {"computed_at_utc": "2026-10-27T00:00:00+00:00", "prices": prices}))
    fx_dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2025-01-01", fx_last)]
    vals = fx_values if fx_values is not None else (
        [1.1] * len(fx_dates) if constant else walk(fx_dates, 4, start=1.1))
    (tmp / cbr.ENGINE_FX_EXPORT).write_text(json.dumps(fx_export(fx_dates, vals)))
    return tmp


def edit_json(path: Path, fn):
    obj = json.loads(path.read_text())
    fn(obj)
    path.write_text(json.dumps(obj))


def build(tmp, now=NOW):
    return cbr.build(tmp, now)


def snapshot(tmp_path, **kw):
    """A demonstrably valid, available baseline and its saved form."""
    d = write_fixture(tmp_path, **kw)
    saved = json.loads(json.dumps(build(d), allow_nan=False))
    assert saved["risk_status"] == "available", saved["reasons"]
    cbr.validate_output(saved)
    assert cbr.validate_snapshot(saved, d)["state"] == "current"
    return d, saved


def state(saved, d):
    return cbr.validate_snapshot(saved, d)


def test_happy_path_reconciles_and_is_strict_json(tmp_path):
    out = build(write_fixture(tmp_path))
    assert out["risk_status"] == "available", out["reasons"]
    cbr.validate_output(out)
    json.dumps(out, allow_nan=False)
    assert out["capital_total"] == pytest.approx(1.0)
    assert sum(s["share"] for s in out["sleeves"]) == pytest.approx(1.0)
    assert sum(i["share"] for i in out["instruments"]) == pytest.approx(1.0)
    assert out["reconciliation"]["status"] == "pass"
    assert [p["proxy"] for p in out["overlaps"]["proxy"]] == ["SPY"]
    assert out["overlaps"]["exact"] == []
    a = next(s for s in out["sleeves"] if s["sleeve"] == "A")
    spy = next(i for i in out["instruments"] if i["proxy"] == "SPY")
    assert a["share"] == pytest.approx(spy["share"] * 0.30 / 0.50)


def test_targets_cannot_masquerade_as_held(tmp_path):
    base = build(write_fixture(tmp_path / "a"))
    moved = build(write_fixture(tmp_path / "b", target_override={"SPY": 0.9, "DBC": 0.0}))
    assert [s["capital"] for s in moved["sleeves"]] == [s["capital"] for s in base["sleeves"]]
    assert moved["portfolio"]["vol"] == pytest.approx(base["portfolio"]["vol"])


def test_live_targets_held_disagreement_is_flagged(tmp_path):
    d = write_fixture(tmp_path)
    edit_json(d / "live_targets.json", lambda o: o["lines"][0].update(held=0.31))
    out = build(d)
    assert out["risk_status"] == "unavailable"
    assert any("disagree" in r for r in out["reasons"])


def test_incomplete_held_book_is_not_renormalised(tmp_path):
    out = build(write_fixture(tmp_path, book=BOOK[:-1]))  # sums to 0.80
    assert out["capital_status"] == "unavailable" and out["risk_status"] == "unavailable"
    assert out["sleeves"] == []


def test_interior_gap_makes_risk_unavailable(tmp_path):
    d = write_fixture(tmp_path)

    def drop(o):
        for k in ("dates", "prices"):
            del o["prices"]["DBC"][k][100]
    edit_json(d / "holdings_prices_1y.json", drop)
    out = build(d)
    assert out["risk_status"] == "unavailable"
    assert out["portfolio"] is None and out["basis"]["nav_covered"] == 0.0
    assert any(r.startswith("DBC: missing expected sessions") for r in out["reasons"])
    assert out["capital_status"] == "available"
    assert all(s["share"] is None for s in out["sleeves"])


def test_stale_tail_makes_risk_unavailable(tmp_path):
    d = write_fixture(tmp_path)

    def trim(o):
        for k in ("dates", "prices"):
            o["prices"]["EXV1.DE"][k] = o["prices"]["EXV1.DE"][k][:-1]
    edit_json(d / "holdings_prices_1y.json", trim)
    out = build(d)
    assert any(r.startswith("EXV1.DE: last close") for r in out["reasons"])
    assert out["risk_status"] == "unavailable"


def test_missing_engine_fx_names_the_dependency(tmp_path):
    d = write_fixture(tmp_path)
    (d / cbr.ENGINE_FX_EXPORT).unlink()
    out = build(d)
    assert out["risk_status"] == "unavailable" and out["capital_status"] == "available"
    assert any("Missing dependency" in r and "export-engine-fx" in r for r in out["reasons"])


def test_truncated_engine_cache_is_not_forward_filled(tmp_path):
    """The ws1 cache ending months before the cutoff is neither shifted nor
    carried forward, and in any case cannot establish its provenance."""
    d = write_fixture(tmp_path)
    (d / cbr.ENGINE_FX_EXPORT).unlink()
    idx = pd.bdate_range("2025-01-01", "2026-06-19")
    pd.DataFrame({"EURUSD": [1.1] * len(idx)}, index=idx).to_parquet(d / cbr.ENGINE_FX_CACHE)
    out = build(d)
    assert out["risk_status"] == "unavailable"
    tried = out["basis"]["fx"]["tried"]
    assert [t["id"] for t in tried] == ["engine-export", "engine-ws1-cache"]
    assert "2026-06-19" in tried[1]["problem"] and "provenance cannot be established" in tried[1]["problem"]


def test_fresh_legacy_cache_is_still_not_used(tmp_path):
    """R6: a cache that reaches the cutoff is still rejected for provenance."""
    d = write_fixture(tmp_path)
    (d / cbr.ENGINE_FX_EXPORT).unlink()
    idx = pd.bdate_range("2025-01-01", CUTOFF)
    pd.DataFrame({"EURUSD": [1.1] * len(idx)}, index=idx).to_parquet(d / cbr.ENGINE_FX_CACHE)
    out = build(d)
    assert out["risk_status"] == "unavailable"
    assert out["basis"]["fx"]["tried"][1]["problem"].startswith("provenance cannot be established")


def test_future_price_cannot_enter_panel(tmp_path):
    d = write_fixture(tmp_path / "a")
    edit_json(d / "holdings_prices_1y.json", lambda o: (
        o["prices"]["SPY"]["dates"].append("2026-10-26"), o["prices"]["SPY"]["prices"].append(1e6)))
    out = build(d)
    base = build(write_fixture(tmp_path / "b"))
    assert out["basis"]["last_return_date"] == CUTOFF
    assert out["portfolio"]["vol"] == pytest.approx(base["portfolio"]["vol"])
    assert next(i for i in out["instruments"] if i["proxy"] == "SPY")["price"]["after_cutoff"] == 1


def test_future_dated_holding_rejected(tmp_path):
    d = write_fixture(tmp_path)
    edit_json(d / "component_held_basis.json", lambda o: o.update(model_as_of="2026-10-24"))
    assert build(d)["capital_status"] == "unavailable"


def test_anchor_after_today_rejected(tmp_path):
    assert build(write_fixture(tmp_path), datetime(2026, 10, 22, tzinfo=timezone.utc)
                 )["capital_status"] == "unavailable"


# ---- F2: reconciliation must pass ----------------------------------------

def test_f2_missing_live_track_withholds_risk(tmp_path):
    d = write_fixture(tmp_path)
    (d / "live_track.json").unlink()
    out = build(d)
    assert out["risk_status"] == "unavailable"
    assert out["reconciliation"]["status"] == "not checked"
    assert any("reconciliation not verified" in r for r in out["reasons"])


def test_f2_live_track_anchor_mismatch_withholds_risk(tmp_path):
    d = write_fixture(tmp_path)
    edit_json(d / "live_track.json", lambda o: o.update(anchor_date="2026-10-30"))
    out = build(d)
    assert out["risk_status"] == "unavailable"
    assert "2026-10-30" in out["reconciliation"]["detail"]


def test_f2_reconciliation_failure_withholds_risk(tmp_path):
    d = write_fixture(tmp_path)
    edit_json(d / "live_track.json", lambda o: o["effective_weights"].update(SPY=0.30))
    out = build(d)
    assert out["reconciliation"]["status"] == "fail" and out["risk_status"] == "unavailable"


# ---- F3: input validation and failure handling ----------------------------

@pytest.mark.parametrize("mutate,expect", [
    (lambda v: [-x for x in v], "non-positive"),
    (lambda v: v[:-1] + [0.0], "non-positive"),
])
def test_f3_bad_fx_values_withhold_risk(tmp_path, mutate, expect):
    d = write_fixture(tmp_path)
    edit_json(d / cbr.ENGINE_FX_EXPORT, lambda o: o.update(close=mutate(o["close"])))
    out = build(d)
    assert out["risk_status"] == "unavailable"
    assert any(expect in t["problem"] for t in out["basis"]["fx"]["tried"])


def test_f3_malformed_fx_json_is_unavailable_not_an_exception(tmp_path):
    d = write_fixture(tmp_path)
    (d / cbr.ENGINE_FX_EXPORT).write_text("{not json")
    out = build(d)
    assert out["risk_status"] == "unavailable"
    assert "not valid JSON" in out["basis"]["fx"]["tried"][0]["problem"]


def test_f3_nan_token_in_prices_rejected(tmp_path):
    d = write_fixture(tmp_path)
    text = (d / "holdings_prices_1y.json").read_text()
    (d / "holdings_prices_1y.json").write_text(text.replace('"prices": [', '"prices": [NaN, ', 1))
    out = build(d)
    assert out["risk_status"] == "unavailable"
    assert any("NaN" in r for r in out["reasons"])


@pytest.mark.parametrize("how", ["duplicate", "unordered"])
def test_f3_duplicate_or_unordered_price_dates(tmp_path, how):
    d = write_fixture(tmp_path)

    def mutate(o):
        p = o["prices"]["DBC"]
        if how == "duplicate":
            p["dates"][50] = p["dates"][49]
        else:
            p["dates"][50], p["dates"][51] = p["dates"][51], p["dates"][50]
    edit_json(d / "holdings_prices_1y.json", mutate)
    out = build(d)
    assert out["risk_status"] == "unavailable"
    assert any("duplicated or out of order" in r for r in out["reasons"])


def test_f3_missing_price_file_keeps_capital(tmp_path):
    d = write_fixture(tmp_path)
    (d / "holdings_prices_1y.json").unlink()
    out = build(d)
    assert out["capital_status"] == "available" and out["risk_status"] == "unavailable"


def test_f3_resolver_disagreement_keeps_capital(tmp_path, monkeypatch):
    import export_holdings_prices
    monkeypatch.setattr(export_holdings_prices, "resolve_book_symbol", lambda etf: "ZZZ")
    out = build(write_fixture(tmp_path))
    assert out["capital_status"] == "available" and out["risk_status"] == "unavailable"
    assert any("resolvers disagree" in r for r in out["reasons"])


def test_f3_write_atomic_rejects_nan_and_bad_schema(tmp_path):
    out = build(write_fixture(tmp_path / "d"))
    bad = json.loads(json.dumps(out))
    bad["portfolio"]["vol"] = float("nan")
    with pytest.raises(ValueError, match="portfolio.vol is non-finite"):
        cbr.write_atomic(bad, tmp_path / "x.json")
    bad2 = json.loads(json.dumps(out))
    bad2["reasons"] = ["something"]
    with pytest.raises(ValueError, match="risk available with reasons"):
        cbr.validate_output(bad2)
    assert not (tmp_path / "x.json").exists()


# ---- F4: zero variance ----------------------------------------------------

def test_f4_zero_variance_full_path(tmp_path):
    out = build(write_fixture(tmp_path, constant=True))
    assert out["risk_status"] == "available"
    assert out["portfolio"]["vol"] == 0.0
    assert out["portfolio"]["attribution"] == cbr.UNDEFINED_ATTRIBUTION
    assert all(s["share"] is None and s["vol_contrib"] is None for s in out["sleeves"])
    assert all(i["share"] is None for i in out["instruments"])
    assert out["overlaps"]["correlated"] == []
    text = json.dumps(out, allow_nan=False)  # strict: no NaN or Infinity
    assert "NaN" not in text and "Infinity" not in text
    cbr.validate_output(out)  # valid zero volatility is not confused with defined attribution
    p = tmp_path / "o.json"
    cbr.write_atomic(out, p)
    assert cbr.validate_snapshot(json.loads(p.read_text()), tmp_path)["state"] == "current"


# ---- F5: weekly sensitivity (frozen; verified, not redesigned) ------------

def test_f5_weekly_sensitivity_shares_endpoints_and_book(tmp_path):
    out = build(write_fixture(tmp_path))
    s = out["sensitivity"]
    cc, wk = s["common_close"], s["weekly"]
    assert cc["annualisation"] == 252 and wk["annualisation"] == 52
    assert cc["last_return_date"] == wk["last_return_date"] == s["endpoint_end"] == CUTOFF
    assert cc["first_return_date"] > s["endpoint_start"] and wk["first_return_date"] > s["endpoint_start"]
    assert 45 <= wk["n_returns"] <= 53 and cc["n_returns"] > 4 * wk["n_returns"]
    assert sum(wk["sleeve_share"].values()) == pytest.approx(1.0)
    d = tmp_path
    prices = json.loads((d / "holdings_prices_1y.json").read_text())["prices"]
    fx = json.loads((d / cbr.ENGINE_FX_EXPORT).read_text())
    fxs = pd.Series(fx["close"], index=pd.to_datetime(fx["dates"]))
    ser = {k: pd.Series(v["prices"], index=pd.to_datetime(v["dates"])) for k, v in prices.items()}
    ser["EXV1.DE"] = ser["EXV1.DE"] * fxs.reindex(ser["EXV1.DE"].index, method="ffill")
    panel = pd.DataFrame(ser).dropna()
    weekly = panel.groupby(panel.index.to_period("W-SUN")).tail(1)
    w = pd.Series({"DBC": 0.30, "EXV1.DE": 0.20, "SPY": 0.50})[panel.columns]
    synth = weekly.pct_change().iloc[1:] @ w
    assert wk["vol"] == pytest.approx(math.sqrt(np.var(synth, ddof=1) * 52), rel=1e-10)


def test_f5_sensitivity_unchanged_on_deterministic_fixture(tmp_path):
    """Pins the frozen F5 output on a deterministic fixture: two builds agree
    exactly, and the counts and endpoints match the fixture calendar."""
    a = build(write_fixture(tmp_path / "a"))["sensitivity"]
    b = build(write_fixture(tmp_path / "b"))["sensitivity"]
    assert a == b
    assert (a["endpoint_start"], a["endpoint_end"]) == (a["endpoint_start"], CUTOFF)
    assert a["status"] == "available" and a["min_weekly_returns"] == 26
    assert a["basis"] == "fixed held book, same USD prices and FX, identical endpoints"


def test_week_grouping_across_year_boundary():
    # Monday 2025-12-29 to Sunday 2026-01-04 is one W-SUN week.
    idx = pd.to_datetime(["2025-12-26", "2025-12-29", "2025-12-31", "2026-01-02", "2026-01-05"])
    panel = pd.DataFrame({"X": [1.0, 1.1, 1.2, 1.3, 1.4]}, index=idx)
    last = panel.groupby(panel.index.to_period("W-SUN")).tail(1)
    assert [d.strftime("%Y-%m-%d") for d in last.index] == ["2025-12-26", "2026-01-02", "2026-01-05"]


def test_observation_digests_across_month_and_year_boundaries():
    s = pd.Series([1.0, 2.0, 3.0, 4.0],
                  index=pd.to_datetime(["2025-11-28", "2025-12-01", "2025-12-31", "2026-01-02"]))
    obs = cbr.observation_digests(s)
    assert list(obs) == ["2025-11-28", "2025-12-01", "2025-12-31", "2026-01-02"]
    assert len(set(obs.values())) == 4
    fp = cbr.series_fingerprint(s, 0)
    assert (fp["first_used"], fp["last_used"], fp["n"], fp["after_cutoff"]) == ("2025-11-28", "2026-01-02", 4, 0)


# Date edge cases for the FX staleness cap (10 calendar days, date library).
@pytest.mark.parametrize("cutoff,last,ok", [
    (date(2026, 3, 2), "2026-02-20", True),    # month boundary, 10 days
    (date(2026, 3, 2), "2026-02-19", False),   # month boundary, 11 days
    (date(2026, 1, 2), "2025-12-23", True),    # year boundary, 10 days
    (date(2026, 1, 2), "2025-12-22", False),   # year boundary, 11 days
])
def test_fx_staleness_cap_across_boundaries(tmp_path, cutoff, last, ok):
    f = tmp_path / cbr.ENGINE_FX_EXPORT
    f.write_text(json.dumps(fx_export(["2025-12-01", last], [1.1, 1.1])))
    s, _, reasons = cbr.load_fx(cutoff, [{"id": "engine-export", "path": f}])
    assert (s is not None) is ok and (not reasons) is ok


# ---- F1: snapshot validity --------------------------------------------------

def test_f1_changed_price_before_cutoff_invalidates(tmp_path):
    d, saved = snapshot(tmp_path)
    edit_json(d / "holdings_prices_1y.json",
              lambda o: o["prices"]["SPY"]["prices"].__setitem__(-1, o["prices"]["SPY"]["prices"][-1] * 1.1))
    v = state(saved, d)
    assert v["state"] == "invalid" and any(r.startswith("SPY: 1 used observation(s) restated")
                                           for r in v["reasons"])


def test_f1_same_anchor_held_change_invalidates(tmp_path):
    d, saved = snapshot(tmp_path)
    edit_json(d / "component_held_basis.json", lambda o: (
        o["lines"][0].update(held=0.29), o["lines"][1].update(held=0.21)))
    v = state(saved, d)
    assert v["state"] == "invalid" and any("held amounts changed" in r for r in v["reasons"])


def test_f1_live_effective_weight_change_invalidates(tmp_path):
    d, saved = snapshot(tmp_path)
    edit_json(d / "live_track.json", lambda o: o["effective_weights"].update(SPY=0.30))
    assert state(saved, d)["reasons"] == ["live_track effective weights or anchor changed"]


def test_f1_same_anchor_live_targets_held_change_invalidates(tmp_path):
    d, saved = snapshot(tmp_path)
    edit_json(d / "live_targets.json", lambda o: o["lines"][0].update(held=0.31))
    assert state(saved, d)["reasons"] == ["live_targets held lines changed for the same anchor"]


def test_f1_live_targets_moving_to_next_session_is_harmless(tmp_path):
    d, saved = snapshot(tmp_path)
    edit_json(d / "live_targets.json", lambda o: o.update(as_of="2026-10-27"))
    assert state(saved, d)["state"] == "current"


def roll_prices(o, k=1, restate=None):
    """Fixed-window roll: drop k from the front, append k after the cutoff."""
    for sym, p in o["prices"].items():
        p["dates"] = p["dates"][k:] + ["2026-10-26", "2026-10-27", "2026-10-28"][:k]
        p["prices"] = p["prices"][k:] + [p["prices"][-1] * 1.05] * k
    if restate:
        sym, i, factor = restate
        o["prices"][sym]["prices"][i] *= factor


def test_f1_post_cutoff_append_and_front_roll_are_harmless(tmp_path):
    d, saved = snapshot(tmp_path)
    edit_json(d / "holdings_prices_1y.json", roll_prices)
    edit_json(d / cbr.ENGINE_FX_EXPORT, lambda o: (o["dates"].append("2026-10-26"), o["close"].append(1.2)))
    v = state(saved, d)
    assert v["state"] == "current", v
    assert any("rolled out of the export and cannot be re-verified" in n for n in v["notes"])


def test_f1_restated_fx_invalidates(tmp_path):
    d, saved = snapshot(tmp_path)
    edit_json(d / cbr.ENGINE_FX_EXPORT, lambda o: o["close"].__setitem__(-10, o["close"][-10] * 1.01))
    v = state(saved, d)
    assert v["state"] == "invalid" and any(r.startswith("EUR/USD: 1 used observation(s) restated")
                                           for r in v["reasons"])


def test_f1_calculator_change_invalidates(tmp_path, monkeypatch):
    d, saved = snapshot(tmp_path)
    monkeypatch.setattr(cbr, "CALC_SHA256", "0" * 64)
    assert "the calculator has changed since this snapshot was computed" in state(saved, d)["reasons"]


def test_f1_new_anchor_makes_snapshot_historical(tmp_path):
    d, saved = snapshot(tmp_path)
    edit_json(d / "component_held_basis.json", lambda o: o.update(anchor="2026-10-30"))
    edit_json(d / "live_track.json", lambda o: o.update(anchor_date="2026-10-30"))
    v = state(saved, d)
    assert v["state"] == "historical" and "2026-10-30" in v["notes"][0]


def test_f1_schema_violation_invalidates(tmp_path):
    d, saved = snapshot(tmp_path)
    saved["schema"] = "combined_book_risk/v1"
    assert "rebuild" in state(saved, d)["reasons"][0]


# ---- R1: front rolling must not mask changes to surviving observations ------

def test_r1_reproduction_front_removal_and_restated_successor(tmp_path):
    d, saved = snapshot(tmp_path)

    def mutate(o):
        p = o["prices"]["SPY"]
        del p["dates"][0], p["prices"][0]
        p["prices"][0] *= 2
    edit_json(d / "holdings_prices_1y.json", mutate)
    v = state(saved, d)
    assert v["state"] == "invalid"
    assert any(r.startswith("SPY: 1 used observation(s) restated") for r in v["reasons"])


def test_r1_restatement_in_partially_rolled_month_after_genuine_roll(tmp_path):
    d, saved = snapshot(tmp_path)
    # Roll by one (with one append), then restate the next retained close,
    # which sits in the month whose first observation has rolled out.
    edit_json(d / "holdings_prices_1y.json", lambda o: roll_prices(o, 1, ("DBC", 0, 1.001)))
    v = state(saved, d)
    assert v["state"] == "invalid"
    assert any(r.startswith("DBC: 1 used observation(s) restated") for r in v["reasons"])


def test_r1_front_roll_without_matching_append_invalidates(tmp_path):
    d, saved = snapshot(tmp_path)

    def drop_front(o):
        p = o["prices"]["SPY"]
        del p["dates"][:2], p["prices"][:2]
    edit_json(d / "holdings_prices_1y.json", drop_front)
    v = state(saved, d)
    assert any("not a fixed-window roll" in r for r in v["reasons"])


def test_r1_price_interior_deletion_invalidates(tmp_path):
    d, saved = snapshot(tmp_path)

    def drop(o):
        p = o["prices"]["DBC"]
        del p["dates"][120], p["prices"][120]
    edit_json(d / "holdings_prices_1y.json", drop)
    v = state(saved, d)
    assert any(r.startswith("DBC: 1 used observation(s) deleted") for r in v["reasons"])


def test_r1_fx_interior_deletion_and_retained_restatement(tmp_path):
    d, saved = snapshot(tmp_path)
    first = saved["fingerprint"]["fx"]["first_used"]

    def mutate(o):
        i = o["dates"].index(first)
        del o["dates"][i + 50], o["close"][i + 50]   # interior deletion
        o["close"][i + 1] *= 1.01                     # retained restatement
    edit_json(d / cbr.ENGINE_FX_EXPORT, mutate)
    reasons = state(saved, d)["reasons"]
    assert any(r.startswith("EUR/USD: 1 used observation(s) deleted") for r in reasons)
    assert any(r.startswith("EUR/USD: 1 used observation(s) restated") for r in reasons)


def test_r1_inserted_observation_inside_window_invalidates(tmp_path):
    d, saved = snapshot(tmp_path)

    def insert(o):
        p = o["prices"]["SPY"]
        p["dates"].insert(101, "2026-04-04")  # a Saturday inside the window
        p["prices"].insert(101, p["prices"][100])
        order = sorted(range(len(p["dates"])), key=lambda i: p["dates"][i])
        p["dates"] = [p["dates"][i] for i in order]
        p["prices"] = [p["prices"][i] for i in order]
    edit_json(d / "holdings_prices_1y.json", insert)
    assert any("added inside the used window" in r for r in state(saved, d)["reasons"])


def test_r1_v2_month_fingerprint_fails_closed(tmp_path):
    d, saved = snapshot(tmp_path)
    old = json.loads(json.dumps(saved))
    old["schema"] = old["fingerprint"]["schema"] = "combined_book_risk/v2"
    old["fingerprint"]["prices"] = {k: {"first_used": v["first_used"], "months": {}}
                                    for k, v in old["fingerprint"]["prices"].items()}
    v = state(old, d)
    assert v["state"] == "invalid"
    assert "combined_book_risk/v2" in v["reasons"][0] and "rebuild" in v["reasons"][0]


# ---- R2: nested output contract ----------------------------------------------

def test_r2a_missing_evidence_and_unchecked_reconciliation_rejected(tmp_path):
    d, saved = snapshot(tmp_path)
    saved["fingerprint"].pop("prices")
    saved["fingerprint"].pop("fx")
    saved["reconciliation"]["status"] = "not checked"
    edit_json(d / "holdings_prices_1y.json",
              lambda o: o["prices"]["SPY"]["prices"].__setitem__(-1, 1.0))
    v = state(saved, d)
    assert v["state"] == "invalid"
    assert "without a recorded same-basis reconciliation pass" in v["reasons"][0]
    saved["reconciliation"]["status"] = "pass"
    assert "price evidence missing" in state(saved, d)["reasons"][0]


def test_r2b_zero_variance_with_zero_shares_rejected(tmp_path):
    d, saved = snapshot(tmp_path, constant=True)
    for row in saved["sleeves"] + saved["instruments"]:
        row["share"], row["vol_contrib"] = 0.0, 0.0
    v = state(saved, d)
    assert v["state"] == "invalid" and "must be null (undefined)" in v["reasons"][0]


@pytest.mark.parametrize("mutate,match", [
    (lambda o: o["sensitivity"]["weekly"].update(vol=float("inf")), r"sensitivity\.weekly\.vol is non-finite"),
    (lambda o: o["instruments"].__setitem__(0, "SPY"), "must be objects"),
    (lambda o: o["fingerprint"].update(calc_sha256="0" * 64), "calculator identifiers disagree"),
    (lambda o: o["fingerprint"].update(live_track_sha256=None), "live_track evidence"),
    (lambda o: o["fingerprint"]["fx"].update(provenance_sha256="0" * 64), "FX label or provenance"),
    (lambda o: o["portfolio"].update(attribution=cbr.UNDEFINED_ATTRIBUTION), "attribution label"),
    (lambda o: o["sleeves"][0].update(share=o["sleeves"][0]["share"] + 0.1), "do not add up"),
    (lambda o: o["basis"].update(nav_covered=0.5), "does not cover the whole held book"),
    (lambda o: o["fingerprint"]["prices"]["SPY"]["observations"].popitem(), "internally inconsistent"),
    (lambda o: o.update(run_id=7), "run_id"),
])
def test_r2_contract_violations_named(tmp_path, mutate, match):
    _, saved = snapshot(tmp_path)
    mutate(saved)
    with pytest.raises(ValueError, match=match):
        cbr.validate_output(saved)


def test_r2_unavailable_with_numeric_shares_rejected(tmp_path):
    d = write_fixture(tmp_path)
    (d / cbr.ENGINE_FX_EXPORT).unlink()
    out = build(d)
    cbr.validate_output(out)
    out["sleeves"][0]["share"] = 0.5
    with pytest.raises(ValueError, match="must be null"):
        cbr.validate_output(out)


# ---- R3: failed replacement must not leave an older result eligible ----------

def run_main(d, out_path):
    return cbr.main(["build", "--data-dir", str(d), "--out", str(out_path)])


def test_r3_successful_run_clears_marker(tmp_path):
    d = write_fixture(tmp_path / "d")
    out_path = tmp_path / "cbr.json"
    assert run_main(d, out_path) == 0
    assert not cbr.pending_path(out_path).exists()
    saved = json.loads(out_path.read_text())
    assert cbr.validate_snapshot(saved, d, out_path)["state"] == "current"


def test_r3_replacement_and_failure_record_both_fail(tmp_path, monkeypatch):
    d = write_fixture(tmp_path / "d")
    out_path = tmp_path / "cbr.json"
    run_main(d, out_path)
    old_text = out_path.read_text()
    real_replace = cbr.os.replace

    def deny(src, dst):
        if Path(dst) == out_path:
            raise PermissionError("denied")
        return real_replace(src, dst)
    monkeypatch.setattr(cbr.os, "replace", deny)
    with pytest.raises(PermissionError):
        run_main(d, out_path)
    assert out_path.read_text() == old_text and cbr.pending_path(out_path).exists()
    assert not out_path.with_name(out_path.name + ".tmp").exists()
    v = cbr.validate_snapshot(json.loads(old_text), d, out_path)
    assert v["state"] == "invalid" and "has not completed" in v["reasons"][0]
    # Recovery (N3): the next run refuses while the marker stands; the operator
    # clears it by naming the run, and the next successful run replaces the artefact.
    monkeypatch.setattr(cbr.os, "replace", real_replace)
    with pytest.raises(SystemExit, match="another run"):
        run_main(d, out_path)
    rid = json.loads(cbr.pending_path(out_path).read_text())["run_id"]
    assert cbr.main(["recover", "--out", str(out_path), "--run-id", rid]) == 0
    assert run_main(d, out_path) == 0
    assert not cbr.pending_path(out_path).exists()
    assert cbr.validate_snapshot(json.loads(out_path.read_text()), d, out_path)["state"] == "current"


def test_r3_replacement_fails_once_failure_record_written(tmp_path, monkeypatch):
    d = write_fixture(tmp_path / "d")
    out_path = tmp_path / "cbr.json"
    run_main(d, out_path)
    real_replace, calls = cbr.os.replace, []

    def deny_once(src, dst):
        calls.append(dst)
        if len(calls) == 1:
            raise PermissionError("denied once")
        return real_replace(src, dst)
    monkeypatch.setattr(cbr.os, "replace", deny_once)
    with pytest.raises(PermissionError):
        run_main(d, out_path)
    saved = json.loads(out_path.read_text())
    assert saved["run_status"] == "error" and "PermissionError" in saved["reasons"][0]
    assert not cbr.pending_path(out_path).exists()
    assert cbr.validate_snapshot(saved, d, out_path)["state"] == "invalid"


def test_r3_marker_not_established_aborts_before_calculation(tmp_path, monkeypatch):
    d = write_fixture(tmp_path / "d")
    out_path = tmp_path / "missing-dir" / "cbr.json"
    called = []
    monkeypatch.setattr(cbr, "build", lambda *a, **k: called.append(1))
    with pytest.raises(SystemExit, match="nothing calculated"):
        run_main(d, out_path)
    assert called == []


def test_r3_programming_error_recorded_and_raised(tmp_path, monkeypatch):
    d = write_fixture(tmp_path / "d")
    out_path = tmp_path / "cbr.json"
    run_main(d, out_path)
    monkeypatch.setattr(cbr, "build", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError, match="boom"):
        run_main(d, out_path)
    saved = json.loads(out_path.read_text())
    assert saved["run_status"] == "error" and not cbr.pending_path(out_path).exists()
    assert cbr.validate_snapshot(saved, d, out_path)["reasons"][0].startswith("the last calculator run failed")


# ---- R4: invalid held rows must not disappear --------------------------------

@pytest.mark.parametrize("mutate,expect", [
    (lambda o: o["lines"].insert(0, {"sleeve": "A", "etf": "CSP1", "held": 99, "target": 0}),
     "duplicate held line for A CSP1"),
    (lambda o: o["lines"].append({"sleeve": "B", "etf": "GLD", "held": -0.1, "target": 0}),
     "negative held"),
    (lambda o: o["lines"][0].update(held="0.30"), "is not a number"),
    (lambda o: o["lines"][0].update(held=True), "is not a number"),
    (lambda o: o["lines"].append("A CSP1 0.3"), "is not an object"),
    (lambda o: o["lines"][1].pop("etf"), "etf is missing"),
])
def test_r4_invalid_live_targets_rows_withhold_and_invalidate(tmp_path, mutate, expect):
    d, saved = snapshot(tmp_path)
    edit_json(d / "live_targets.json", lambda o: mutate(o))
    out = build(d)
    assert out["risk_status"] == "unavailable" and out["capital_status"] == "available"
    assert out["reconciliation"]["live_targets_held"] == "not checked"
    assert any(expect in r for r in out["reasons"]), out["reasons"]
    v = state(saved, d)
    assert v["state"] == "invalid" and any(expect in r for r in v["reasons"])


def test_r4_zero_held_row_and_targets_are_harmless(tmp_path):
    d, saved = snapshot(tmp_path)
    edit_json(d / "live_targets.json", lambda o: (
        o["lines"].append({"sleeve": "B", "etf": "GLD", "held": 0.0, "target": 0.25}),
        o["lines"][0].update(target=0.0)))
    assert build(d)["risk_status"] == "available"
    assert state(saved, d)["state"] == "current"


def test_r4_other_as_of_is_not_cross_checked(tmp_path):
    d, saved = snapshot(tmp_path)
    edit_json(d / "live_targets.json", lambda o: (
        o.update(as_of="2026-10-27"), o["lines"].append({"sleeve": "A", "etf": "CSP1", "held": 99})))
    out = build(d)
    assert out["reconciliation"]["live_targets_held"] == "not applicable (other as-of)"
    assert out["risk_status"] == "available" and state(saved, d)["state"] == "current"


# ---- R5: malformed dependencies keep capital and never crash -----------------

MALFORMED = [
    ("live_track.json", lambda o: [], "live_track.json: top level is a list"),
    ("live_track.json", lambda o: {**o, "effective_weights": []}, "effective_weights is missing or not an object"),
    ("live_track.json", lambda o: {**o, "effective_weights": {"SPY": "0.5"}}, "live_track effective weight SPY is not a number"),
    ("live_track.json", lambda o: {**o, "anchor_date": 20261023}, "anchor_date is missing or not a string"),
    ("live_targets.json", lambda o: [], "live_targets.json: top level is a list"),
    ("live_targets.json", lambda o: {**o, "lines": {"a": 1}}, "lines is not a list"),
    ("holdings_prices_1y.json", lambda o: [], "holdings_prices_1y.json: top level is a list"),
    ("holdings_prices_1y.json", lambda o: {**o, "prices": []}, "prices is missing or not an object"),
    ("holdings_prices_1y.json", lambda o: {**o, "prices": {**o["prices"], "SPY": []}}, "SPY: no price series"),
    ("holdings_prices_1y.json", lambda o: {**o, "computed_at_utc": 5}, "computed_at_utc is not a string"),
]


@pytest.mark.parametrize("fname,mutate,expect", MALFORMED)
def test_r5_malformed_dependency_keeps_capital(tmp_path, fname, mutate, expect):
    d, saved = snapshot(tmp_path)
    obj = json.loads((d / fname).read_text())
    (d / fname).write_text(json.dumps(mutate(obj)))
    out = build(d)
    assert out["capital_status"] == "available" and out["risk_status"] == "unavailable"
    assert [s["capital"] for s in out["sleeves"]] == [s["capital"] for s in saved["sleeves"]]
    assert out["overlaps"]["proxy"] == saved["overlaps"]["proxy"]
    assert any(expect in r for r in out["reasons"]), out["reasons"]
    cbr.validate_output(out)
    v = state(saved, d)
    assert v["state"] == "invalid"


@pytest.mark.parametrize("mutate", [lambda o: [], lambda o: {**o, "lines": [1]},
                                    lambda o: {**o, "lines": [{"sleeve": "A", "etf": "X", "held": "1"}]}])
def test_r5_malformed_held_basis_is_reported_not_raised(tmp_path, mutate):
    d, saved = snapshot(tmp_path)
    obj = json.loads((d / "component_held_basis.json").read_text())
    (d / "component_held_basis.json").write_text(json.dumps(mutate(obj)))
    out = build(d)
    assert out["capital_status"] == "unavailable" and out["reasons"]
    assert state(saved, d)["state"] == "invalid"


def test_r5_cli_and_loader_survive_live_track_list(tmp_path, monkeypatch):
    import pipeline
    d = write_fixture(tmp_path / "d")
    out_path = d / "combined_book_risk.json"
    run_main(d, out_path)
    (d / "live_track.json").write_text("[]")
    monkeypatch.setattr(pipeline, "DATA_DIR", d)
    loaded = pipeline.load_combined_book_risk()
    assert loaded["display_state"] == "invalid"
    assert any("live_track" in r for r in loaded["display_reasons"])
    assert run_main(d, out_path) == 0
    saved = json.loads(out_path.read_text())
    assert saved["run_status"] == "ok" and saved["capital_status"] == "available"
    assert saved["risk_status"] == "unavailable" and saved["sleeves"]


# ---- R6: FX provenance validated, not assigned ---------------------------------

@pytest.mark.parametrize("mutate,expect", [
    (lambda o: o.update(source="UNRELATED FX SOURCE"), "unexpected field(s) source"),
    (lambda o: o.pop("provenance"), "provenance is missing"),
    (lambda o: o["provenance"].update(converter="some_other.convert"), "provenance converter"),
    (lambda o: o["provenance"].update(ticker="EURGBP=X"), "provenance ticker"),
    (lambda o: o["provenance"].update(aligned_to="SPY"), "is not a Xetra line"),
    (lambda o: o["provenance"].update(exported_at_utc="yesterday"), "is not a timestamp"),
    (lambda o: o["provenance"].update(note="x"), "unexpected fields"),
    (lambda o: o.update(schema="other/v1"), "schema 'other/v1'"),
])
def test_r6_fx_metadata_must_validate(tmp_path, mutate, expect):
    d, saved = snapshot(tmp_path)
    edit_json(d / cbr.ENGINE_FX_EXPORT, mutate)
    out = build(d)
    assert out["risk_status"] == "unavailable" and out["capital_status"] == "available"
    assert expect in out["basis"]["fx"]["tried"][0]["problem"]
    v = state(saved, d)
    assert v["state"] == "invalid" and any("EUR/USD" in r for r in v["reasons"])


def test_r6_label_is_derived_from_validated_metadata(tmp_path):
    d = write_fixture(tmp_path)
    edit_json(d / cbr.ENGINE_FX_EXPORT, lambda o: o["provenance"].update(
        aligned_to="EXH1.DE", exported_at_utc="2026-10-27T02:00:00Z"))
    fx = build(d)["basis"]["fx"]
    assert fx["label"] == cbr.fx_label(fx["provenance"])
    assert "aligned to EXH1.DE" in fx["label"] and "2026-10-27T02:00:00Z" in fx["label"]


def test_r6_changed_provenance_invalidates_but_reexport_time_does_not(tmp_path):
    d, saved = snapshot(tmp_path)
    edit_json(d / cbr.ENGINE_FX_EXPORT, lambda o: o["provenance"].update(exported_at_utc="2026-10-28T09:00:00Z"))
    assert state(saved, d)["state"] == "current"
    edit_json(d / cbr.ENGINE_FX_EXPORT, lambda o: o["provenance"].update(aligned_to="EXH1.DE"))
    v = state(saved, d)
    assert v["reasons"] == ["EUR/USD provenance changed or cannot be established"]


# ---- R7: parquet errors caught narrowly ---------------------------------------

def test_r7_unreadable_parquet_is_a_data_problem(tmp_path):
    d = write_fixture(tmp_path)
    (d / cbr.ENGINE_FX_EXPORT).unlink()
    (d / cbr.ENGINE_FX_CACHE).write_bytes(b"not a parquet file")
    out = build(d)
    assert out["run_status"] == "ok" and out["capital_status"] == "available"
    assert "is unreadable" in out["basis"]["fx"]["tried"][1]["problem"]


def test_r7_programming_error_in_parquet_reader_propagates(tmp_path, monkeypatch):
    d = write_fixture(tmp_path / "d")
    (d / cbr.ENGINE_FX_EXPORT).unlink()
    pd.DataFrame({"EURUSD": [1.1, 1.1]}, index=pd.bdate_range("2026-01-01", periods=2)).to_parquet(
        d / cbr.ENGINE_FX_CACHE)

    def boom(*a, **k):
        raise RuntimeError("injected")
    monkeypatch.setattr(cbr.pd, "read_parquet", boom)
    with pytest.raises(RuntimeError, match="injected"):
        build(d)
    out_path = tmp_path / "cbr.json"
    with pytest.raises(RuntimeError, match="injected"):
        run_main(d, out_path)
    saved = json.loads(out_path.read_text())
    assert saved["run_status"] == "error" and "RuntimeError: injected" in saved["reasons"][0]


# ---- Pipeline loader ------------------------------------------------------------

def test_pipeline_loader_reports_state(tmp_path, monkeypatch):
    import pipeline
    d, saved = snapshot(tmp_path)
    cbr.write_atomic(saved, d / "combined_book_risk.json")
    monkeypatch.setattr(pipeline, "DATA_DIR", d)
    loaded = pipeline.load_combined_book_risk()
    assert loaded["display_state"] == "current" and "fingerprint" not in loaded
    edit_json(d / "component_held_basis.json", lambda o: o["lines"][0].update(held=0.29))
    assert pipeline.load_combined_book_risk()["display_state"] == "invalid"
    (d / "combined_book_risk.json").write_text("{broken")
    out = pipeline.load_combined_book_risk()
    assert out["display_state"] == "invalid" and out["display_reasons"]
    (d / "combined_book_risk.json").write_text("[]")
    out = pipeline.load_combined_book_risk()
    assert out == {"display_state": "invalid", "display_reasons": out["display_reasons"],
                   "display_notes": []} and "not an object" in out["display_reasons"][0]


# --------------------------------------------------------------------------
# Executable renderer checks (the shipped template function, run in Node)
# --------------------------------------------------------------------------

def _node():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the renderer tests")
    return node


def _extract(src: str, name: str) -> str:
    start = src.index(f"\nfunction {name}(") + 1
    return src[start:src.index("\n}\n", start) + 2]


HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const states = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const root = { innerHTML: '' };
global.window = {};
global.document = { getElementById: id => id === 'cbr-root' ? root : null };
(0, eval)(src);
const out = {};
for (const [k, v] of Object.entries(states)) {
  window.DATA = { combined_book_risk: v };
  renderCombinedBookRisk();
  out[k] = root.innerHTML;
}
process.stdout.write(JSON.stringify(out));
"""


def render(tmp_path, states: dict) -> dict:
    node = _node()
    tpl = (ROOT / "template.html").read_text(encoding="utf-8")
    src = "\n".join(_extract(tpl, n) for n in ("_escapeHtml", "_displaySym", "renderCombinedBookRisk"))
    (tmp_path / "src.js").write_text(src, encoding="utf-8")
    (tmp_path / "states.json").write_text(json.dumps(states, allow_nan=False), encoding="utf-8")
    (tmp_path / "h.cjs").write_text(HARNESS, encoding="utf-8")
    r = subprocess.run([node, str(tmp_path / "h.cjs"), str(tmp_path / "src.js"),
                        str(tmp_path / "states.json")], capture_output=True, text=True,
                       encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def shown(blob, st="current", reasons=(), notes=()):
    return {**blob, "display_state": st, "display_reasons": list(reasons), "display_notes": list(notes)}


def risk_cells(html):
    """Text of the 'Est. risk contribution' column in the sleeve table."""
    body = html.split("<caption>", 1)[1].split("</table>", 1)[0]
    return [row.split('<td class="num">')[2].split("<")[0]
            for row in body.split("<tr>")[2:]
            if row.count('<td class="num">') >= 3 and not row.startswith("<td>Unallocated")]


def test_render_states_executable(tmp_path):
    _, avail = snapshot(tmp_path / "a")
    _, zero = snapshot(tmp_path / "z", constant=True)
    d = write_fixture(tmp_path / "u")
    (d / cbr.ENGINE_FX_EXPORT).unlink()
    unavail = build(d)
    malformed_zero = json.loads(json.dumps(zero))
    for row in malformed_zero["sleeves"] + malformed_zero["instruments"]:
        row["share"], row["vol_contrib"] = 0.0, 0.0
    note = "SPY: 1 earliest used observation(s) (2025-10-24 to 2025-10-24) have rolled out"
    html = render(tmp_path, {
        "available": shown(avail),
        "available_notes": shown(avail, notes=[note]),
        "historical": shown(avail, "historical", notes=["the model-held book has moved"]),
        "invalid": shown(avail, "invalid", reasons=["SPY: 1 used observation(s) restated"]),
        "unavailable": shown(unavail),
        "zero": shown(zero),
        "zero_malformed": shown(malformed_zero),
    })
    assert all(c.endswith("%") for c in risk_cells(html["available"]))
    assert "cbr-fill risk" in html["available"] and "cbr-note" not in html["available"]
    assert note in html["available_notes"] and "Re-verified against the current files" in html["available_notes"]
    assert "Historical snapshot" in html["historical"] and "historical, as at" in html["historical"]
    assert "Snapshot invalid; risk withheld" in html["invalid"]
    assert set(risk_cells(html["invalid"])) == {"unavailable"}
    assert set(risk_cells(html["unavailable"])) == {"unavailable"}
    assert "Missing dependency" in html["unavailable"]
    for k in ("zero", "zero_malformed"):
        assert set(risk_cells(html[k])) == {"undefined"}, k
        assert "cbr-fill risk" not in html[k] and "cbr-fill neg" not in html[k]
        assert "0.0%" not in html[k].split("Sampling sensitivity")[0].split("Capital weight and estimated")[1]
        assert "undefined" in html[k] and "not shown as 0%" in html[k]


FOUR_SLEEVES = [("A", "CSP1", 0.30), ("B", "SPY", 0.20), ("C", "DBC", 0.30), ("D", "EXV1", 0.20)]


def sensitivity_table(html):
    return html.split("Sampling sensitivity", 1)[1].split("<table", 1)[1].split("</table>", 1)[0]


def test_d1_sensitivity_table_width_does_not_grow_with_sleeves(tmp_path):
    """Render readiness D1: with four sleeves the convention-by-sleeve table
    clipped at 390 px. Sleeves are rows, so the column count stays fixed."""
    _, four = snapshot(tmp_path / "f", book=FOUR_SLEEVES)
    assert {s["sleeve"] for s in four["sleeves"]} == {"A", "B", "C", "D"}
    table = sensitivity_table(render(tmp_path, {"four": shown(four)})["four"])
    head = table.split("</thead>", 1)[0]
    assert head.count("</th>") <= 3, head
    body = table.split("<tbody>", 1)[1]
    for k in "ABCD":
        assert f"<tr><td>{k}</td>" in body, k
    cc, wk = four["sensitivity"]["common_close"], four["sensitivity"]["weekly"]
    assert f"<td class=\"num\">{cc['n_returns']}</td><td class=\"num\">{wk['n_returns']}</td>" in body


def test_d2_reasons_only_invalid_lead_does_not_mention_tables(tmp_path):
    """Render readiness D2: a reasons-only invalid object draws no tables, so
    its lead must not describe capital or overlap tables."""
    html = render(tmp_path, {"pending": {
        "display_state": "invalid", "display_notes": [],
        "display_reasons": ["a snapshot write is in progress"]}})["pending"]
    assert "<table" not in html
    assert "Snapshot invalid; risk withheld" in html and "a snapshot write is in progress" in html
    assert D2_LEAD in html, html
    assert "show the book as computed" not in html


D2_LEAD = "Risk figures are withheld; no capital or overlap tables are shown."


def test_d2_invalid_without_sleeves_lead_does_not_deny_basis_capital(tmp_path):
    """D2 review P3: with capital available but no sleeve rows the renderer
    returns before any table, while the basis still prints the capital total,
    so the lead must not say that no figures are shown."""
    _, four = snapshot(tmp_path / "f", book=FOUR_SLEEVES)
    blob = {**shown(four, "invalid", reasons=["sleeve rows missing"]), "sleeves": []}
    assert blob["capital_status"] == "available" and blob["capital_total"] == pytest.approx(1.0)
    html = render(tmp_path, {"no_sleeves": blob})["no_sleeves"]
    assert "<table" not in html
    assert "Capital total 100.000%" in html
    assert D2_LEAD in html, html
    assert "no figures are shown" not in html


def test_d2_control_invalid_with_capital_keeps_tables_and_original_lead(tmp_path):
    """Control, expected to pass before and after the wording change."""
    _, four = snapshot(tmp_path / "f", book=FOUR_SLEEVES)
    html = render(tmp_path, {"inv": shown(four, "invalid", reasons=["SPY: 1 used observation(s) restated"])})["inv"]
    assert "<table" in html and "Capital weight and estimated risk contribution by sleeve" in html
    assert ("Risk figures are withheld. The capital and overlap tables show the book as computed "
            "and are not verified against the current files.") in html
    assert D2_LEAD not in html


# --------------------------------------------------------------------------
# Invariance and the real book
# --------------------------------------------------------------------------

def test_build_does_not_touch_engine_files():
    data = ROOT / "data"
    before = {f: cbr.sha256(data / f) for f in cbr.ENGINE_FILES}
    cbr.build(data, NOW)
    assert {f: cbr.sha256(data / f) for f in cbr.ENGINE_FILES} == before
    assert cbr.OUT_PATH.name not in cbr.ENGINE_FILES


def test_real_book_contract():
    """On the repository data the capital view reconciles; the risk estimate
    is withheld only for the named engine-FX dependency when that is missing."""
    out = cbr.build(ROOT / "data", NOW)
    cbr.validate_output(out)
    assert out["capital_status"] == "available"
    rec = out["reconciliation"]
    assert rec["status"] == "pass" and rec["max_abs_diff"] <= rec["tolerance"] == 1e-4
    if out["risk_status"] != "available":
        assert len(out["reasons"]) == 1 and "Missing dependency" in out["reasons"][0]
        cache = out["basis"]["fx"]["tried"][1]
        assert cache["last"] == "2026-06-19" and "provenance cannot be established" in cache["problem"]
        return
    prices = json.loads((ROOT / "data" / "holdings_prices_1y.json").read_text())["prices"]
    cutoff = date.fromisoformat(out["basis"]["price_cutoff"])
    syms = [i["proxy"] for i in out["instruments"]]
    native, _, _ = cbr.load_prices(prices, syms, cutoff)
    fx, _, _ = cbr.load_fx(cutoff, cbr.fx_sources(ROOT / "data"))
    panel, _ = cbr.usd_panel(native, {i["proxy"]: i["fx"] for i in out["instruments"]}, fx)
    rets = panel[syms].pct_change().iloc[1:]
    w = np.array([i["weight"] for i in out["instruments"]])
    assert out["portfolio"]["variance"] == pytest.approx(np.var(rets.to_numpy() @ w, ddof=1) * 252, rel=1e-10)


# ==========================================================================
# v4 follow-up: N1-N7 and the frozen F5 output
# ==========================================================================

# ---- F5: frozen output, captured on the accepted v3 calculator before edits --

def test_f5_frozen_output_on_accepted_fixture(tmp_path):
    """Not a determinism check: explicit values captured from the accepted
    implementation on this fixture (and matched by the independent review)."""
    s = build(write_fixture(tmp_path))["sensitivity"]
    assert (s["endpoint_start"], s["endpoint_end"]) == ("2025-10-31", "2026-10-23")
    assert s["status"] == "available" and s["min_weekly_returns"] == 26
    cc, wk = s["common_close"], s["weekly"]
    assert (cc["annualisation"], cc["n_returns"], cc["first_return_date"], cc["last_return_date"]) == \
        (252, 240, "2025-11-03", "2026-10-23")
    assert (wk["annualisation"], wk["n_returns"], wk["first_return_date"], wk["last_return_date"]) == \
        (52, 51, "2025-11-07", "2026-10-23")
    assert cc["vol"] == pytest.approx(0.10373671233917266, rel=1e-12)
    assert wk["vol"] == pytest.approx(0.09587637548562739, rel=1e-12)
    assert cc["sleeve_share"] == pytest.approx(
        {"A": 0.33530704673088546, "B": 0.4304118900949053, "D": 0.2342810631742092}, rel=1e-12)
    assert wk["sleeve_share"] == pytest.approx(
        {"A": 0.28326024944892686, "B": 0.4278287078916532, "D": 0.2889110426594201}, rel=1e-12)


# ---- N1: evidence coverage must match the recorded estimation window -----------

def truncate_fp(fs, keep=2):
    """A self-consistent fingerprint reduced to its last ``keep`` digests."""
    obs = dict(list(fs["observations"].items())[-keep:])
    fs.update(observations=obs, n=len(obs), first_used=next(iter(obs)),
              last_used=list(obs)[-1], sha256=cbr.canonical_sha(obs))


def test_n1_truncated_price_fingerprint_cannot_support_estimate(tmp_path):
    d, saved = snapshot(tmp_path)
    assert saved["basis"]["n_returns"] >= 200
    truncate_fp(saved["fingerprint"]["prices"]["SPY"])
    edit_json(d / "holdings_prices_1y.json",
              lambda o: o["prices"]["SPY"]["prices"].__setitem__(100, o["prices"]["SPY"]["prices"][100] * 1.5))
    v = state(saved, d)
    assert v["state"] == "invalid", v
    assert any("price evidence does not cover the recorded" in r for r in v["reasons"]), v


def test_n1_truncated_fx_fingerprint_cannot_support_estimate(tmp_path):
    d, saved = snapshot(tmp_path)
    truncate_fp(saved["fingerprint"]["fx"])
    first = saved["fingerprint"]["prices"]["EXV1.DE"]["first_used"]
    edit_json(d / cbr.ENGINE_FX_EXPORT,
              lambda o: o["close"].__setitem__(o["dates"].index(first), o["close"][o["dates"].index(first)] * 1.2))
    v = state(saved, d)
    assert v["state"] == "invalid", v
    assert any("FX evidence does not cover" in r for r in v["reasons"]), v


def test_n1_fingerprint_disagreeing_with_price_record_rejected(tmp_path):
    _, saved = snapshot(tmp_path)
    saved["instruments"][0]["price"]["closes"] -= 1
    with pytest.raises(ValueError, match="price evidence does not cover the recorded"):
        cbr.validate_output(saved)


# ---- N2: exact dates and numbers, no coercion, no crash ------------------------

@pytest.mark.parametrize("mutate,expect", [
    (lambda o: o["prices"]["SPY"]["prices"].__setitem__(5, True), "SPY: non-numeric values"),
    (lambda o: o["prices"]["SPY"]["prices"].__setitem__(5, "101.5"), "SPY: non-numeric values"),
    (lambda o: o["prices"]["DBC"]["dates"].__setitem__(5, o["prices"]["DBC"]["dates"][5] + "garbage"),
     "DBC: unparseable date"),
    (lambda o: o["prices"]["SPY"].update(prices=[[v] for v in o["prices"]["SPY"]["prices"]]),
     "SPY: non-numeric values"),
])
def test_n2_malformed_series_withheld_with_capital(tmp_path, mutate, expect):
    d, saved = snapshot(tmp_path)
    edit_json(d / "holdings_prices_1y.json", mutate)
    out = build(d)
    assert out["risk_status"] == "unavailable" and out["capital_status"] == "available"
    assert [s["capital"] for s in out["sleeves"]] == [s["capital"] for s in saved["sleeves"]]
    assert out["overlaps"]["exact"] == saved["overlaps"]["exact"]
    assert out["overlaps"]["proxy"] == saved["overlaps"]["proxy"]
    assert any(expect in r for r in out["reasons"]), out["reasons"]
    v = state(saved, d)
    assert v["state"] == "invalid" and any(expect in r for r in v["reasons"]), v


def test_n2_nested_price_arrays_cli_and_loader_keep_capital(tmp_path, monkeypatch):
    import pipeline
    d = write_fixture(tmp_path / "d")
    edit_json(d / "holdings_prices_1y.json",
              lambda o: o["prices"]["SPY"].update(prices=[[v] for v in o["prices"]["SPY"]["prices"]]))
    out_path = d / "combined_book_risk.json"
    assert run_main(d, out_path) == 0
    saved = json.loads(out_path.read_text())
    assert saved["run_status"] == "ok" and saved["capital_status"] == "available"
    assert saved["risk_status"] == "unavailable" and saved["sleeves"]
    monkeypatch.setattr(pipeline, "DATA_DIR", d)
    loaded = pipeline.load_combined_book_risk()
    assert loaded["risk_status"] == "unavailable" and loaded["capital_status"] == "available"


def test_n2_numeric_controls_still_accepted(tmp_path):
    d, saved = snapshot(tmp_path)
    # An integer close is a JSON number; a numpy-backed float cache is numeric.
    edit_json(d / "holdings_prices_1y.json", lambda o: o["prices"]["DBC"]["prices"].__setitem__(-1, 100))
    assert build(d)["risk_status"] == "available"


def test_n2_non_numeric_legacy_parquet_is_named_input_error(tmp_path):
    d = write_fixture(tmp_path)
    (d / cbr.ENGINE_FX_EXPORT).unlink()
    pd.DataFrame({"EURUSD": ["a", "b"]}, index=pd.bdate_range("2026-10-01", periods=2)).to_parquet(
        d / cbr.ENGINE_FX_CACHE)
    out = build(d)
    assert out["run_status"] == "ok" and out["capital_status"] == "available"
    assert "non-numeric" in out["basis"]["fx"]["tried"][1]["problem"]


@pytest.mark.parametrize("value", [-0.00005, 1.5])
def test_n2_live_track_weight_outside_long_only_range_withheld(tmp_path, value):
    d, saved = snapshot(tmp_path)
    edit_json(d / "live_track.json", lambda o: o["effective_weights"].update(GLD=value))
    out = build(d)
    assert out["risk_status"] == "unavailable" and out["capital_status"] == "available"
    assert any("live_track effective weight GLD" in r and "outside [0, 1]" in r for r in out["reasons"]), out["reasons"]
    v = state(saved, d)
    assert v["state"] == "invalid" and any("outside [0, 1]" in r for r in v["reasons"])


def test_n2_zero_live_track_weight_is_harmless(tmp_path):
    d = write_fixture(tmp_path)
    edit_json(d / "live_track.json", lambda o: o["effective_weights"].update(GLD=0.0))
    out = build(d)
    assert out["risk_status"] == "available" and out["reconciliation"]["status"] == "pass"


# ---- N3: one run per artefact; ownership-safe cleanup; explicit recovery -------

def test_n3_overlapping_run_cannot_erase_active_guard(tmp_path, monkeypatch):
    d = write_fixture(tmp_path / "d")
    out_path = tmp_path / "cbr.json"
    real_build, real_replace = cbr.build, cbr.os.replace
    seen = {}

    def build_a(*a, **k):
        # Deterministic interleaving: run B starts and finishes inside run A.
        monkeypatch.setattr(cbr, "build", real_build)
        try:
            seen["b"] = run_main(d, out_path)
        except SystemExit as exc:
            seen["b"] = exc
        # Run A then fails, and its failure record cannot be written.
        monkeypatch.setattr(cbr.os, "replace",
                            lambda s, t: (_ for _ in ()).throw(PermissionError("denied")))
        raise RuntimeError("run A failed")
    monkeypatch.setattr(cbr, "build", build_a)
    with pytest.raises(RuntimeError, match="run A failed"):
        run_main(d, out_path)
    monkeypatch.setattr(cbr.os, "replace", real_replace)
    assert isinstance(seen["b"], SystemExit) and "another run" in str(seen["b"]), seen
    assert cbr.pending_path(out_path).exists()
    if out_path.exists():
        v = cbr.validate_snapshot(json.loads(out_path.read_text()), d, out_path)
        assert v["state"] == "invalid" and "has not completed" in v["reasons"][0]


def test_n3_end_run_never_clears_another_runs_marker(tmp_path):
    out_path = tmp_path / "cbr.json"
    run = cbr.begin_run(out_path, "a" * 32, NOW)
    try:
        with pytest.raises(RuntimeError, match="belongs to another run"):
            cbr.end_run(run, "b" * 32)
        assert cbr.pending_path(out_path).exists()
    finally:
        run.release()


def test_n3_interrupted_run_requires_explicit_recovery(tmp_path):
    d = write_fixture(tmp_path / "d")
    out_path = tmp_path / "cbr.json"
    assert run_main(d, out_path) == 0
    # A run that was interrupted: its marker stays, its lock went with the process.
    cbr.begin_run(out_path, "c" * 32, NOW).release()
    with pytest.raises(SystemExit, match="recover --run-id " + "c" * 32):
        run_main(d, out_path)
    assert cbr.pending_path(out_path).exists()
    with pytest.raises(SystemExit, match="does not match"):
        cbr.main(["recover", "--out", str(out_path), "--run-id", "d" * 32])
    assert cbr.pending_path(out_path).exists()
    assert cbr.main(["recover", "--out", str(out_path), "--run-id", "c" * 32]) == 0
    assert not cbr.pending_path(out_path).exists()
    assert cbr.validate_snapshot(json.loads(out_path.read_text()), d, out_path)["state"] == "invalid"
    assert run_main(d, out_path) == 0
    assert cbr.validate_snapshot(json.loads(out_path.read_text()), d, out_path)["state"] == "current"


# ---- N4: post-cutoff closes present at baseline do not authorise front loss ----

def add_post_cutoff(o, sym="SPY", day="2026-10-26"):
    o["prices"][sym]["dates"].append(day)
    o["prices"][sym]["prices"].append(o["prices"][sym]["prices"][-1])


def test_n4_existing_post_cutoff_close_does_not_authorise_front_deletion(tmp_path):
    d = write_fixture(tmp_path)
    edit_json(d / "holdings_prices_1y.json", add_post_cutoff)
    saved = json.loads(json.dumps(build(d)))
    assert saved["risk_status"] == "available" and state(saved, d)["state"] == "current"

    def drop_first(o):
        del o["prices"]["SPY"]["dates"][0], o["prices"]["SPY"]["prices"][0]
    edit_json(d / "holdings_prices_1y.json", drop_first)
    v = state(saved, d)
    assert v["state"] == "invalid", v
    assert any("SPY" in r and "not a fixed-window roll" in r for r in v["reasons"]), v


def test_n4_controls_after_baseline_with_post_cutoff_close(tmp_path):
    def baseline(sub):
        d = write_fixture(tmp_path / sub)
        edit_json(d / "holdings_prices_1y.json", add_post_cutoff)
        saved = json.loads(json.dumps(build(d)))
        assert state(saved, d)["state"] == "current"
        return d, saved
    # Genuine fixed-window roll: one off the front, one appended.
    d, saved = baseline("roll")
    edit_json(d / "holdings_prices_1y.json", lambda o: (
        o["prices"]["SPY"]["dates"].pop(0), o["prices"]["SPY"]["prices"].pop(0),
        add_post_cutoff(o, day="2026-10-27")))
    v = state(saved, d)
    assert v["state"] == "current" and any("rolled out" in n for n in v["notes"]), v
    # Harmless append only.
    d, saved = baseline("append")
    edit_json(d / "holdings_prices_1y.json", lambda o: add_post_cutoff(o, day="2026-10-27"))
    assert state(saved, d) == {"state": "current", "reasons": [], "notes": []}
    # Retained restatement and interior deletion still reject.
    d, saved = baseline("restate")
    edit_json(d / "holdings_prices_1y.json", lambda o: o["prices"]["SPY"]["prices"].__setitem__(50, 1.0))
    assert any("restated" in r for r in state(saved, d)["reasons"])
    d, saved = baseline("interior")
    edit_json(d / "holdings_prices_1y.json", lambda o: (o["prices"]["SPY"]["dates"].pop(60),
                                                      o["prices"]["SPY"]["prices"].pop(60)))
    assert any("deleted" in r for r in state(saved, d)["reasons"])


def fx_starting_at_used_window(d):
    """Trim the FX export so its first observation is the first one used."""
    prices = json.loads((d / "holdings_prices_1y.json").read_text())["prices"]
    first = min(p["dates"][0] for p in prices.values())
    start = (pd.Timestamp(first) - pd.Timedelta(days=cbr.FX_MAX_STALE_DAYS)).strftime("%Y-%m-%d")

    def trim(o):
        keep = [i for i, x in enumerate(o["dates"]) if x >= start]
        o["dates"] = [o["dates"][i] for i in keep]
        o["close"] = [o["close"][i] for i in keep]
    edit_json(d / cbr.ENGINE_FX_EXPORT, trim)


def test_n4_fx_existing_post_cutoff_close_does_not_authorise_front_deletion(tmp_path):
    d = write_fixture(tmp_path)
    fx_starting_at_used_window(d)
    edit_json(d / cbr.ENGINE_FX_EXPORT, lambda o: (o["dates"].append("2026-10-26"), o["close"].append(1.15)))
    saved = json.loads(json.dumps(build(d)))
    assert saved["risk_status"] == "available", saved["reasons"]
    assert saved["fingerprint"]["fx"]["first_used"] == json.loads(
        (d / cbr.ENGINE_FX_EXPORT).read_text())["dates"][0]
    assert state(saved, d)["state"] == "current"
    edit_json(d / cbr.ENGINE_FX_EXPORT, lambda o: (o["dates"].pop(0), o["close"].pop(0)))
    v = state(saved, d)
    assert v["state"] == "invalid" and any("EUR/USD" in r and "not a fixed-window roll" in r
                                           for r in v["reasons"]), v
    # Control: a genuine FX roll (one appended as well) is accepted with a note.
    edit_json(d / cbr.ENGINE_FX_EXPORT, lambda o: (o["dates"].append("2026-10-27"), o["close"].append(1.15)))
    v = state(saved, d)
    assert v["state"] == "current" and any("EUR/USD" in n and "rolled out" in n for n in v["notes"]), v


# ---- N5: malformed nested artefacts are contract failures, not crashes ---------

NESTED = [
    (lambda o: o["fingerprint"].update(prices=[1]), "fingerprint prices"),
    (lambda o: o["overlaps"].update(correlated=[None]), "overlaps correlated"),
    (lambda o: o["overlaps"].update(exact=[None]), "overlaps exact"),
    (lambda o: o["basis"]["fx"]["provenance"].pop("vendor"), "provenance"),
    (lambda o: rekey_first(o["fingerprint"]["prices"]["SPY"], "2025-02-30"), "SPY"),
]


def rekey_first(fs, day):
    """Replace the first digest's date, keeping the fingerprint self-consistent."""
    obs = dict(fs["observations"])
    first = next(iter(obs))
    obs = {day: obs.pop(first), **obs}
    fs.update(observations=obs, first_used=day, sha256=cbr.canonical_sha(obs))


def test_n5_malformed_fx_fingerprint_on_unavailable_artefact(tmp_path):
    d = write_fixture(tmp_path)
    edit_json(d / "live_track.json", lambda o: o.update(anchor_date="2026-10-22"))
    saved = json.loads(json.dumps(build(d)))
    assert saved["risk_status"] == "unavailable" and isinstance(saved["fingerprint"].get("fx"), dict)
    cbr.validate_output(saved)
    saved["fingerprint"]["fx"] = "x"
    with pytest.raises(ValueError, match="fingerprint fx"):
        cbr.validate_output(saved)
    v = state(saved, d)
    assert v["state"] == "invalid" and v["reasons"][0].startswith("artefact fails its contract")


@pytest.mark.parametrize("mutate,match", NESTED)
def test_n5_malformed_nested_artefact_is_named_contract_failure(tmp_path, mutate, match):
    d, saved = snapshot(tmp_path)
    mutate(saved)
    with pytest.raises(ValueError, match=match):
        cbr.validate_output(saved)
    v = state(saved, d)
    assert v["state"] == "invalid" and v["reasons"][0].startswith("artefact fails its contract")


@pytest.mark.parametrize("mutate,match", NESTED[:2])
def test_n5_loader_reports_malformed_nested_artefact_invalid(tmp_path, monkeypatch, mutate, match):
    import pipeline
    d, saved = snapshot(tmp_path)
    mutate(saved)
    (d / "combined_book_risk.json").write_text(json.dumps(saved))
    monkeypatch.setattr(pipeline, "DATA_DIR", d)
    out = pipeline.load_combined_book_risk()
    assert out["display_state"] == "invalid" and match in out["display_reasons"][0]
    assert set(out) == {"display_state", "display_reasons", "display_notes"}


# ---- N6: duplicate JSON keys rejected at the reader ----------------------------

def test_n6_duplicate_held_key_withholds_and_invalidates(tmp_path):
    d, saved = snapshot(tmp_path)
    p = d / "live_targets.json"
    text = p.read_text()
    assert text.startswith('{"as_of": "2026-10-23", "lines": [{"sleeve": "A", "etf": "CSP1", "held": 0.3,')
    p.write_text(text.replace('"held": 0.3,', '"held": 99, "held": 0.3,', 1))
    out = build(d)
    assert out["risk_status"] == "unavailable" and out["capital_status"] == "available"
    assert any("live_targets.json" in r and "duplicate key 'held'" in r for r in out["reasons"]), out["reasons"]
    v = state(saved, d)
    assert v["state"] == "invalid" and any("duplicate key 'held'" in r for r in v["reasons"])


def test_n6_nested_duplicate_key_rejected_and_valid_json_unchanged(tmp_path):
    p = tmp_path / "x.json"
    p.write_text('{"a": {"b": [1, {"c": 1, "c": 2}]}}')
    with pytest.raises(cbr.InputError, match="x.json: duplicate key 'c'"):
        cbr.read_json(p)
    p.write_text('{"a": {"b": [1, {"c": 1, "d": 2}]}, "e": null}')
    assert cbr.read_json(p) == {"a": {"b": [1, {"c": 1, "d": 2}]}, "e": None}


# ---- N7: only identifiable file/format failures are data problems --------------

def test_n7_injected_value_error_in_parquet_reader_propagates(tmp_path, monkeypatch):
    d = write_fixture(tmp_path / "d")
    (d / cbr.ENGINE_FX_EXPORT).unlink()
    pd.DataFrame({"EURUSD": [1.1, 1.1]}, index=pd.bdate_range("2026-01-01", periods=2)).to_parquet(
        d / cbr.ENGINE_FX_CACHE)

    def boom(*a, **k):
        raise ValueError("injected programming defect")
    monkeypatch.setattr(cbr.pd, "read_parquet", boom)
    with pytest.raises(ValueError, match="injected programming defect"):
        build(d)
    out_path = tmp_path / "cbr.json"
    with pytest.raises(ValueError, match="injected programming defect"):
        run_main(d, out_path)
    saved = json.loads(out_path.read_text())
    assert saved["run_status"] == "error" and "injected programming defect" in saved["reasons"][0]
    assert not cbr.pending_path(out_path).exists()


def test_n7_non_date_parquet_index_is_named_input_error(tmp_path):
    d = write_fixture(tmp_path)
    (d / cbr.ENGINE_FX_EXPORT).unlink()
    pd.DataFrame({"EURUSD": [1.1, 1.2]}, index=["x", "y"]).to_parquet(d / cbr.ENGINE_FX_CACHE)
    out = build(d)
    assert out["run_status"] == "ok" and "index is not dates" in out["basis"]["fx"]["tried"][1]["problem"]


# ==========================================================================
# v5 follow-up: the four remaining findings (M1-M4)
# ==========================================================================

# ---- M1: evidence bound to the held-line mapping ------------------------------

def restate_price(d, sym, i=100, factor=1.5):
    edit_json(d / "holdings_prices_1y.json",
              lambda o: o["prices"][sym]["prices"].__setitem__(i, o["prices"][sym]["prices"][i] * factor))


def inst(saved, proxy):
    return next(i for i in saved["instruments"] if i["proxy"] == proxy)


def test_m1_reproduction_a_proxy_relabel_hides_spy_restatement(tmp_path):
    d, saved = snapshot(tmp_path)
    inst(saved, "SPY")["proxy"] = "DBC"
    del saved["fingerprint"]["prices"]["SPY"]
    restate_price(d, "SPY")
    v = state(saved, d)
    assert v["state"] == "invalid", v


def merge_instrument(saved, src="SPY", dst="DBC"):
    """A self-consistent forgery: the src instrument's lines re-pointed at
    dst, with the weights, shares, overlaps and (when present) the saved
    mapping rewritten to agree."""
    a, b = inst(saved, src), inst(saved, dst)
    b["lines"] = sorted(b["lines"] + a["lines"], key=lambda l: (l["sleeve"], l["etf"]))
    for k in ("weight", "share", "vol_contrib"):
        b[k] = b[k] + a[k]
    saved["instruments"].remove(a)
    del saved["fingerprint"]["prices"][src]
    for m in saved["fingerprint"].get("mapping", {}).values():
        if m["proxy"] == src:
            m["proxy"] = dst
    saved["overlaps"]["proxy"] = [{"proxy": dst, "lines": b["lines"],
                                   "combined": sum(l["held"] for l in b["lines"])}]
    saved["overlaps"]["correlated"] = [c for c in saved["overlaps"]["correlated"]
                                       if src not in (c["a"], c["b"])]


def test_m1_consistent_proxy_forgery_rejected(tmp_path):
    d, saved = snapshot(tmp_path)
    merge_instrument(saved)
    restate_price(d, "SPY")
    v = state(saved, d)
    assert v["state"] == "invalid", v
    assert any("mapping" in r for r in v["reasons"]), v


def drop_eur_conversion(saved, d):
    """Reproduction B: the EUR line re-labelled as USD, its FX evidence removed
    and the export deleted."""
    inst(saved, "EXV1.DE")["fx"] = "none"
    saved["fingerprint"].pop("fx")
    saved["basis"]["fx"] = None
    (d / cbr.ENGINE_FX_EXPORT).unlink()


def test_m1_reproduction_b_fx_bypass(tmp_path):
    d, saved = snapshot(tmp_path)
    drop_eur_conversion(saved, d)
    v = state(saved, d)
    assert v["state"] == "invalid", v


def test_m1_consistent_fx_bypass_forgery_rejected(tmp_path):
    d, saved = snapshot(tmp_path)
    drop_eur_conversion(saved, d)
    if "mapping" in saved["fingerprint"]:
        saved["fingerprint"]["mapping"]["EXV1"]["fx"] = "none"
    v = state(saved, d)
    assert v["state"] == "invalid", v
    assert any("EXV1" in r and "mapping" in r for r in v["reasons"]), v


def test_m1_control_shared_proxy_lines_are_legitimate(tmp_path):
    """Control (passes on both versions): two legal instruments on one proxy."""
    _, saved = snapshot(tmp_path)
    spy = inst(saved, "SPY")
    assert sorted((l["sleeve"], l["etf"]) for l in spy["lines"]) == [("A", "CSP1"), ("B", "SPY")]
    assert spy["weight"] == pytest.approx(0.5)
    assert [r["proxy"] for r in saved["overlaps"]["proxy"]] == ["SPY"]


def test_m1_control_historical_snapshot_not_compared_with_newer_book(tmp_path):
    """Control (passes on both versions): a newer, different book makes the
    snapshot historical, not invalid."""
    d, saved = snapshot(tmp_path)
    edit_json(d / "component_held_basis.json", lambda o: o.update(
        anchor="2026-10-26", lines=[{"sleeve": "B", "etf": "SPY", "held": 0.5},
                                    {"sleeve": "B", "etf": "DBC", "held": 0.5}]))
    v = state(saved, d)
    assert v["state"] == "historical", v


# ---- M2: recovery cannot override an active run, nor requalify old output ------

def marker_rid(out_path):
    return json.loads(cbr.pending_path(out_path).read_text())["run_id"]


def on_disk_state(d, out_path):
    return cbr.validate_snapshot(json.loads(out_path.read_text()), d, out_path)


def test_m2_recover_refused_while_owner_active_in_process(tmp_path, monkeypatch):
    d = write_fixture(tmp_path / "d")
    out_path = tmp_path / "cbr.json"
    assert run_main(d, out_path) == 0
    real_build = cbr.build
    seen = {}

    def build_a(*a, **k):
        try:
            seen["recover"] = cbr.recover(out_path, marker_rid(out_path))
        except SystemExit as exc:
            seen["recover"] = exc
        # Immediately after the recovery attempt, before anything is rebuilt.
        seen["marker"] = cbr.pending_path(out_path).exists()
        seen["state"] = on_disk_state(d, out_path)["state"]
        monkeypatch.setattr(cbr, "build", real_build)
        try:
            seen["b"] = run_main(d, out_path)
        except SystemExit as exc:
            seen["b"] = exc
        return real_build(*a, **k)
    monkeypatch.setattr(cbr, "build", build_a)
    try:
        rc = run_main(d, out_path)
    except Exception as exc:  # recorded, asserted after the ownership checks
        rc = exc
    assert isinstance(seen["recover"], SystemExit) and "still active" in str(seen["recover"]), seen
    assert seen["marker"] and seen["state"] == "invalid", seen
    assert isinstance(seen["b"], SystemExit) and "another run" in str(seen["b"]), seen
    assert rc == 0 and not cbr.pending_path(out_path).exists()
    assert on_disk_state(d, out_path)["state"] == "current"


CHILD = r"""
import os, sys
sys.path.insert(0, sys.argv[1])
import combined_book_risk as cbr
real = cbr.build
def hold(*a, **k):
    # The interpreter's own pid: on Windows a venv python.exe is a launcher
    # whose child is the real interpreter, so killing the launcher is not enough.
    print(f"ready {os.getpid()}", flush=True)
    sys.stdin.readline()   # blocks until the parent kills this process
    return real(*a, **k)
cbr.build = hold
cbr.main(["build", "--data-dir", sys.argv[2], "--out", sys.argv[3]])
"""


def test_m2_cross_process_active_refused_then_dead_owner_recovered(tmp_path):
    d = write_fixture(tmp_path / "d")
    out_path = tmp_path / "cbr.json"
    assert run_main(d, out_path) == 0
    (tmp_path / "child.py").write_text(CHILD, encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(tmp_path / "child.py"), str(ROOT / "scripts"),
                             str(d), str(out_path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    pid = None
    try:
        line = proc.stdout.readline()   # synchronisation: the child holds the run
        assert line.startswith("ready "), proc.stderr.read()
        pid = int(line.split()[1])
        rid = marker_rid(out_path)
        with pytest.raises(SystemExit, match="still active"):
            cbr.recover(out_path, rid)
        assert cbr.pending_path(out_path).exists()
        assert on_disk_state(d, out_path)["state"] == "invalid"
        with pytest.raises(SystemExit, match="another run"):
            run_main(d, out_path)
    finally:
        if pid is not None:
            os.kill(pid, signal.SIGTERM)   # TerminateProcess on Windows: no cleanup runs
        else:
            proc.kill()
        # A venv launcher exits only after its child interpreter has, so this
        # wait (not a sleep) is the synchronisation point for the lock release.
        proc.wait(timeout=60)
    # The owner is gone: recovery succeeds, and immediately afterwards (before
    # any rebuild) the artefact on disk is still not current.
    cbr.recover(out_path, rid)
    assert not cbr.pending_path(out_path).exists()
    v = on_disk_state(d, out_path)
    assert v["state"] == "invalid" and "interrupted" in v["reasons"][0], v
    assert run_main(d, out_path) == 0
    assert on_disk_state(d, out_path)["state"] == "current"


def test_m2_build_cannot_start_while_recovery_runs(tmp_path, monkeypatch):
    d = write_fixture(tmp_path / "d")
    out_path = tmp_path / "cbr.json"
    assert run_main(d, out_path) == 0
    rid = "e" * 32   # the marker a terminated process leaves behind (no lock held)
    cbr.pending_path(out_path).write_text(json.dumps({"run_id": rid, "started_utc": "2026-10-27T12:00:00Z"}))
    real_write, seen = cbr.write_atomic, {}

    def write_during_recovery(obj, path):
        if "b" not in seen:
            seen["b"] = "running"
            try:
                seen["b"] = run_main(d, out_path)
            except SystemExit as exc:
                seen["b"] = exc
        return real_write(obj, path)
    monkeypatch.setattr(cbr, "write_atomic", write_during_recovery)
    cbr.recover(out_path, rid)
    monkeypatch.setattr(cbr, "write_atomic", real_write)
    assert isinstance(seen.get("b"), SystemExit) and "another run" in str(seen["b"]), seen
    assert on_disk_state(d, out_path)["state"] == "invalid"
    assert run_main(d, out_path) == 0
    assert on_disk_state(d, out_path)["state"] == "current"


# ---- M3: child holding records validated before display ------------------------

CHILD_RECORDS = [
    (lambda o: o["instruments"][0].update(lines=[None]), "line records malformed"),
    (lambda o: o["instruments"][0]["lines"][0].update(held="0.3"), "line records malformed"),
    (lambda o: o["instruments"][0]["lines"][0].pop("etf"), "line records malformed"),
    (lambda o: o["instruments"][0]["lines"][0].update(held=0.6), "do not match the held book"),
    (lambda o: o["instruments"][0]["lines"][0].update(sleeve="C"), "do not match the held book"),
    (lambda o: o["overlaps"]["proxy"][0].update(lines=[None]), "overlaps proxy"),
    (lambda o: o["overlaps"]["proxy"][0].update(combined="0.5"), "overlaps proxy"),
    (lambda o: o["overlaps"]["exact"].append({"instrument": "SPY", "lines": [], "combined": 0.1}),
     "overlaps exact"),
    (lambda o: o["overlaps"]["correlated"].append({**o["overlaps"]["correlated"][0], "a_weight": 0.9})
     if o["overlaps"]["correlated"] else o["overlaps"]["correlated"].append({"corr": 0.9}),
     "overlaps correlated"),
]


@pytest.mark.parametrize("mutate,match", CHILD_RECORDS)
def test_m3_malformed_child_record_is_named_contract_failure(tmp_path, mutate, match):
    d, saved = snapshot(tmp_path)
    mutate(saved)
    with pytest.raises(ValueError, match=match):
        cbr.validate_output(saved)
    v = state(saved, d)
    assert v["state"] == "invalid" and v["reasons"][0].startswith("artefact fails its contract"), v


def test_m3_null_line_rejected_by_loader_before_rendering(tmp_path, monkeypatch):
    import pipeline
    d, saved = snapshot(tmp_path / "s")
    saved["instruments"][0]["lines"] = [None]
    (d / "combined_book_risk.json").write_text(json.dumps(saved))
    monkeypatch.setattr(pipeline, "DATA_DIR", d)
    loaded = pipeline.load_combined_book_risk()
    html = render(tmp_path, {"null_line": loaded})["null_line"]
    assert loaded["display_state"] == "invalid"
    assert set(loaded) == {"display_state", "display_reasons", "display_notes"}
    assert "Snapshot invalid; risk withheld" in html and "line records malformed" in html


# ---- M4: only file/format failures of the parquet reader are data problems -----

def legacy_cache_only(d):
    (d / cbr.ENGINE_FX_EXPORT).unlink()
    pd.DataFrame({"EURUSD": [1.1] * 50}, index=pd.bdate_range("2026-01-01", periods=50)).to_parquet(
        d / cbr.ENGINE_FX_CACHE)


def test_m4_arrow_not_implemented_propagates_as_failed_run(tmp_path, monkeypatch):
    import pyarrow
    d = write_fixture(tmp_path / "d")
    legacy_cache_only(d)

    def unsupported(*a, **k):
        raise pyarrow.ArrowNotImplementedError("unsupported reader operation")
    monkeypatch.setattr(cbr.pd, "read_parquet", unsupported)
    with pytest.raises(pyarrow.ArrowNotImplementedError):
        build(d)
    out_path = tmp_path / "cbr.json"
    with pytest.raises(pyarrow.ArrowNotImplementedError):
        run_main(d, out_path)
    saved = json.loads(out_path.read_text())
    assert saved["run_status"] == "error" and "unsupported reader operation" in saved["reasons"][0]


def test_m4_control_truncated_parquet_is_a_data_problem_keeping_capital(tmp_path):
    """Control (passes on both versions): a genuinely corrupt file is an input
    problem, the capital view is kept and the legacy cache is never used."""
    d = write_fixture(tmp_path)
    legacy_cache_only(d)
    p = d / cbr.ENGINE_FX_CACHE
    p.write_bytes(p.read_bytes()[: p.stat().st_size // 2])
    out = build(d)
    assert out["run_status"] == "ok" and out["capital_status"] == "available"
    assert out["risk_status"] == "unavailable"
    assert "is unreadable (ArrowInvalid" in out["basis"]["fx"]["tried"][1]["problem"]


# ==========================================================================
# v6 follow-up: the two remaining findings (M2 reader race, M3 correlated pairs)
# ==========================================================================

# ---- M2 (finding 1): reading and validating the artefact is one operation -----

def interrupted(out_path, rid="f" * 32):
    """The marker a terminated process leaves behind; no lock is held."""
    cbr.pending_path(out_path).write_text(json.dumps(
        {"run_id": rid, "started_utc": "2026-10-27T12:00:00Z"}))
    return rid


def no_estimate(loaded):
    """Nothing displayable as a risk estimate: the renderer shows figures only
    for a current or historical snapshot, so the state must be invalid."""
    return loaded["display_state"] == "invalid"


def loader_with_hook(monkeypatch, d, hook):
    """The actual pipeline loader, with ``hook`` run each time the object it
    read is about to be validated: the window between the read and the check."""
    import pipeline
    real = cbr.validate_snapshot

    def validate(saved, *a, **k):
        hook()
        return real(saved, *a, **k)
    monkeypatch.setattr(pipeline, "DATA_DIR", d)
    monkeypatch.setattr(cbr, "validate_snapshot", validate)
    try:
        return pipeline.load_combined_book_risk()
    finally:
        monkeypatch.setattr(cbr, "validate_snapshot", real)


def test_v6_m2_reproduction_recovery_between_read_and_validation(tmp_path, monkeypatch):
    """Finding 1 as reported: a valid artefact, an interrupted run's marker
    with its lock released, the loader reads the old object, recovery
    completes, and only then does the loader validate what it read."""
    import pipeline
    d = write_fixture(tmp_path / "d")
    out_path = d / "combined_book_risk.json"
    assert run_main(d, out_path) == 0
    rid = interrupted(out_path)
    seen = {}

    def recover_once():
        if "recover" not in seen:
            try:
                seen["recover"] = cbr.recover(out_path, rid)
            except SystemExit as exc:
                seen["recover"] = exc
    loaded = loader_with_hook(monkeypatch, d, recover_once)
    assert no_estimate(loaded), (loaded.get("display_state"), loaded.get("risk_status"))
    assert on_disk_state(d, out_path)["state"] == "invalid"
    # Recovery (now, if the reader prevented it above) leaves the view invalid.
    if not isinstance(seen.get("recover"), str):
        cbr.recover(out_path, rid)
    assert not cbr.pending_path(out_path).exists()
    assert no_estimate(pipeline.load_combined_book_risk())


def test_v6_m2_failed_build_between_read_and_validation_not_published(tmp_path, monkeypatch):
    """A run that starts after the read, fails, records the failure and clears
    its marker before the loader validates must not leave the old estimate
    displayed as current."""
    d = write_fixture(tmp_path / "d")
    out_path = d / "combined_book_risk.json"
    assert run_main(d, out_path) == 0
    real_build, seen = cbr.build, {}

    def failing_build_once():
        if "b" in seen:
            return

        def boom(*a, **k):
            raise RuntimeError("injected build failure")
        monkeypatch.setattr(cbr, "build", boom)
        try:
            run_main(d, out_path)
        except RuntimeError as exc:
            seen["b"] = exc
        finally:
            monkeypatch.setattr(cbr, "build", real_build)
    loaded = loader_with_hook(monkeypatch, d, failing_build_once)
    assert isinstance(seen.get("b"), RuntimeError) and not cbr.pending_path(out_path).exists()
    assert on_disk_state(d, out_path)["state"] == "invalid"
    assert no_estimate(loaded), (loaded.get("display_state"), loaded.get("risk_status"))


def test_v6_m2_cli_check_recovery_between_read_and_validation(tmp_path, monkeypatch):
    """The same interleaving through the `check` command."""
    d = write_fixture(tmp_path / "d")
    out_path = d / "combined_book_risk.json"
    assert run_main(d, out_path) == 0
    rid = interrupted(out_path)
    real, seen = cbr.validate_snapshot, {}

    def validate(saved, *a, **k):
        if "recover" not in seen:
            try:
                seen["recover"] = cbr.recover(out_path, rid)
            except SystemExit as exc:
                seen["recover"] = exc
        return real(saved, *a, **k)
    monkeypatch.setattr(cbr, "validate_snapshot", validate)
    assert cbr.main(["check", "--data-dir", str(d), "--out", str(out_path)]) == 1


def test_v6_m2_contract_artefact_that_keeps_changing_is_withheld(tmp_path, monkeypatch):
    """New contract (not a defect reproduction): when every read is replaced
    before its validation completes, the loader gives up after a bounded
    number of attempts and shows reasons only."""
    d = write_fixture(tmp_path / "d")
    out_path = d / "combined_book_risk.json"
    assert run_main(d, out_path) == 0
    calls = []

    def rebuild():
        calls.append(1)
        assert run_main(d, out_path) == 0
    loaded = loader_with_hook(monkeypatch, d, rebuild)
    assert set(loaded) == {"display_state", "display_reasons", "display_notes"}, loaded.get("display_state")
    assert loaded["display_state"] == "invalid" and "changed while" in loaded["display_reasons"][0]
    assert len(calls) == cbr.READ_ATTEMPTS


@pytest.mark.parametrize("owner", ["build", "recovery"])
def test_v6_m2_control_read_while_owner_active_is_withheld(tmp_path, monkeypatch, owner):
    """Control (passes on both versions): a read while a run or a recovery is
    executing shows no estimate."""
    import pipeline
    d = write_fixture(tmp_path / "d")
    out_path = d / "combined_book_risk.json"
    assert run_main(d, out_path) == 0
    monkeypatch.setattr(pipeline, "DATA_DIR", d)
    seen = {}
    if owner == "build":
        real_build = cbr.build

        def build_reads(*a, **k):
            seen["loaded"] = pipeline.load_combined_book_risk()
            return real_build(*a, **k)
        monkeypatch.setattr(cbr, "build", build_reads)
        assert run_main(d, out_path) == 0
    else:
        rid = interrupted(out_path)
        real_write = cbr.write_atomic

        def write_reads(obj, path):
            seen.setdefault("loaded", pipeline.load_combined_book_risk())
            return real_write(obj, path)
        monkeypatch.setattr(cbr, "write_atomic", write_reads)
        cbr.recover(out_path, rid)
    assert no_estimate(seen["loaded"]), seen["loaded"].get("display_state")


def test_v6_m2_control_uncontended_read_is_current(tmp_path, monkeypatch):
    """Control: an ordinary read still returns the current estimate, and the
    reader holds nothing afterwards."""
    import pipeline
    d = write_fixture(tmp_path / "d")
    out_path = d / "combined_book_risk.json"
    assert run_main(d, out_path) == 0
    monkeypatch.setattr(pipeline, "DATA_DIR", d)
    loaded = pipeline.load_combined_book_risk()
    assert loaded["display_state"] == "current" and loaded["risk_status"] == "available"
    assert loaded["display_reasons"] == [] and "fingerprint" not in loaded
    lock = cbr.RunLock(out_path)
    assert lock.acquire()
    lock.release()


def test_v6_m2_control_failed_recovery_write_stays_invalid(tmp_path, monkeypatch):
    """Control: a recovery whose failure record cannot be written keeps the
    marker, releases its lock, and the view stays invalid."""
    import pipeline
    d = write_fixture(tmp_path / "d")
    out_path = d / "combined_book_risk.json"
    assert run_main(d, out_path) == 0
    rid = interrupted(out_path)
    real_write = cbr.write_atomic

    def fail(obj, path):
        raise OSError("injected write failure")
    monkeypatch.setattr(cbr, "write_atomic", fail)
    with pytest.raises(OSError, match="injected write failure"):
        cbr.recover(out_path, rid)
    monkeypatch.setattr(cbr, "write_atomic", real_write)
    assert cbr.pending_path(out_path).exists()
    monkeypatch.setattr(pipeline, "DATA_DIR", d)
    assert no_estimate(pipeline.load_combined_book_risk())
    lock = cbr.RunLock(out_path)
    assert lock.acquire()
    lock.release()


# ---- M3 (finding 2): correlated-overlap records follow overlaps()' rules -------

def corr_pair(saved, a, b, corr):
    """A correlated record naming real instruments with their true sleeves
    and weights, as overlaps() writes it."""
    ins = {i["proxy"]: i for i in saved["instruments"]}

    def sleeves(p):
        return sorted({ln["sleeve"] for ln in ins[p]["lines"]})
    return {"a": a, "a_sleeves": sleeves(a), "a_weight": ins[a]["weight"],
            "b": b, "b_sleeves": sleeves(b), "b_weight": ins[b]["weight"], "corr": corr}


def contract_invalid(saved, d, match):
    with pytest.raises(ValueError, match=match):
        cbr.validate_output(saved)
    v = state(saved, d)
    assert v["state"] == "invalid" and v["reasons"][0].startswith("artefact fails its contract"), v


def test_v6_m3_reproduction_a_pair_below_threshold(tmp_path):
    d, saved = snapshot(tmp_path)
    saved["overlaps"]["correlated"] = [corr_pair(saved, "DBC", "EXV1.DE", 0.5)]
    contract_invalid(saved, d, "below the display threshold")


@pytest.mark.parametrize("bad", [[], {}])
def test_v6_m3_reproduction_b_unhashable_identifier(tmp_path, bad):
    d, saved = snapshot(tmp_path)
    saved["overlaps"]["correlated"] = [{**corr_pair(saved, "DBC", "EXV1.DE", 0.9), "a": bad}]
    contract_invalid(saved, d, "overlaps correlated record malformed")


def test_v6_m3_malformed_identifier_rejected_by_loader(tmp_path, monkeypatch):
    import pipeline
    d, saved = snapshot(tmp_path)
    saved["overlaps"]["correlated"] = [{**corr_pair(saved, "DBC", "EXV1.DE", 0.9), "a": []}]
    (d / "combined_book_risk.json").write_text(json.dumps(saved))
    monkeypatch.setattr(pipeline, "DATA_DIR", d)
    loaded = pipeline.load_combined_book_risk()
    assert set(loaded) == {"display_state", "display_reasons", "display_notes"}
    assert loaded["display_state"] == "invalid"
    assert "overlaps correlated record malformed" in loaded["display_reasons"][0]


PAIR_RULES = [
    (lambda s: [corr_pair(s, "DBC", "EXV1.DE", cbr.CORR_DISPLAY_THRESHOLD - 1e-9)],
     "below the display threshold"),
    (lambda s: [corr_pair(s, "EXV1.DE", "DBC", 0.9)], "instrument order"),
    (lambda s: [corr_pair(s, "DBC", "EXV1.DE", 0.9), corr_pair(s, "DBC", "EXV1.DE", 0.9)], "repeats"),
    (lambda s: [corr_pair(s, "DBC", "EXV1.DE", 0.85), corr_pair(s, "DBC", "SPY", 0.95)],
     "descending correlation order"),
    (lambda s: [{**corr_pair(s, "DBC", "EXV1.DE", 0.9), "note": "extra"}],
     "overlaps correlated record malformed"),
]


@pytest.mark.parametrize("pairs,match", PAIR_RULES)
def test_v6_m3_pair_selection_rules_enforced(tmp_path, pairs, match):
    d, saved = snapshot(tmp_path)
    saved["overlaps"]["correlated"] = pairs(saved)
    contract_invalid(saved, d, match)


# DBC and EXV1 both held only in sleeve B: overlaps() never lists that pair.
SAME_SLEEVE_BOOK = [("A", "CSP1", 0.30), ("B", "SPY", 0.20), ("B", "DBC", 0.30), ("B", "EXV1", 0.20)]


def test_v6_m3_same_sleeve_pair_rejected(tmp_path):
    d, saved = snapshot(tmp_path, book=SAME_SLEEVE_BOOK)
    saved["overlaps"]["correlated"] = [corr_pair(saved, "DBC", "EXV1.DE", 0.9)]
    contract_invalid(saved, d, "same-sleeve")


def test_v6_m3_recorded_threshold_must_be_the_configured_one(tmp_path):
    d, saved = snapshot(tmp_path)
    saved["overlaps"]["corr_threshold"] = 0.5
    saved["overlaps"]["correlated"] = [corr_pair(saved, "DBC", "EXV1.DE", 0.6)]
    contract_invalid(saved, d, "corr_threshold")


@pytest.mark.parametrize("pairs", [
    lambda s: [corr_pair(s, "DBC", "SPY", 0.95),
               corr_pair(s, "DBC", "EXV1.DE", cbr.CORR_DISPLAY_THRESHOLD)],
    lambda s: [corr_pair(s, "DBC", "EXV1.DE", 0.9), corr_pair(s, "DBC", "SPY", 0.9)],
])
def test_v6_m3_control_qualifying_pairs_accepted(tmp_path, pairs):
    """Control: cross-sleeve pairs at or above the threshold, in overlaps()'
    order (descending correlation, ties in instrument order), validate."""
    d, saved = snapshot(tmp_path)
    saved["overlaps"]["correlated"] = pairs(saved)
    cbr.validate_output(saved)
    assert state(saved, d)["state"] == "current"


def test_v6_m3_control_negative_contribution_unaffected(tmp_path):
    """Control: a hedging line's negative risk contribution is legitimate and
    is not confused with the positive-correlation display threshold."""
    d = write_fixture(tmp_path)

    def hedge(o):
        spy = np.array(o["prices"]["SPY"]["prices"])
        r = spy[1:] / spy[:-1] - 1
        o["prices"]["DBC"]["prices"] = [float(x) for x in 100 * np.cumprod(np.r_[1.0, 1 - 0.5 * r])]
    edit_json(d / "holdings_prices_1y.json", hedge)
    saved = json.loads(json.dumps(build(d), allow_nan=False))
    assert saved["risk_status"] == "available", saved["reasons"]
    assert min(i["share"] for i in saved["instruments"]) < 0
    cbr.validate_output(saved)
    assert state(saved, d)["state"] == "current"
