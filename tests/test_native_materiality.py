"""A filled native line holds sleeve C only when it could change the book
(owner, 2026-11-03).

On the 2 November 2026 rank sleeve C held on one missing Shenzhen close while
159801.SZ stood 26.7% below its 200-day average: no price that line could have
printed moves it to the +5% floor. These pin the extent of a fill, the signal
range under the 10% per-session bound, the corner test on C's own weight and
gate functions, the READY/HOLD outcome in live_targets._rank, and the same
test in the freshness report the release gate reads.

Offline: no network, nothing outside tmp_path. Python datetime months are
1-indexed (January = 1). Sessions come from the venue calendars.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import live_targets as lt  # noqa: E402
import native_sessions as ns  # noqa: E402
import run_thematic_rotation as th  # noqa: E402
import strategy_freshness as sf  # noqa: E402
from venue_calendars import get_calendar  # noqa: E402

CN = "159801.SZ"
W = 200
L = 0.10
HIST = "2025-09-01"


def _sessions(venue, start, end):
    sched = get_calendar(venue).schedule(start_date=start, end_date=end)
    return pd.DatetimeIndex([pd.Timestamp(d).normalize() for d in sched.index])


OCT_ROWS = _sessions("NYSE", HIST, "2026-10-28")
OCT_NOW = datetime(2026, 10, 29, 3, 0, tzinfo=timezone.utc)   # NYSE last: 10-28


def _row(cn: float, n_above: int, others: int = 24) -> pd.Series:
    """A sleeve C signal row: ``n_above`` names at +10%, the rest of the
    ``others`` at -5%, the native line at ``cn``, cash at 0."""
    vals = {f"T{i:02d}": (0.10 + 0.001 * i if i < n_above else -0.05)
            for i in range(others)}
    vals[CN] = cn
    vals[th.CASH_PROXY] = 0.0
    return pd.Series(vals)


def _panel(rows, row: pd.Series) -> pd.DataFrame:
    return pd.DataFrame([row.to_dict()] * len(rows), index=rows)


C_WEIGHTS = th.top_k_equal_weight(th.HEADLINE_K)


# ---------------------------------------------------------------------------
# The extent of a fill
# ---------------------------------------------------------------------------

def test_one_withdrawn_close_at_the_tail():
    observed = _sessions("XSHG", HIST, "2026-10-27")
    p = ns.provenance(OCT_ROWS, observed, "XSHG", window_rows=W)
    assert ns.stale_extent(p, pd.Timestamp("2026-10-28")) == {
        "depth": 1, "max_gap": 1, "missing": 1, "filled_rows": 1}


def test_an_interior_gap_moves_only_the_average():
    full = _sessions("XSHG", HIST, "2026-10-28")
    observed = full[full != pd.Timestamp("2026-10-27")]
    p = ns.provenance(OCT_ROWS, observed, "XSHG", window_rows=W)
    assert ns.stale_extent(p, pd.Timestamp("2026-10-28")) == {
        "depth": 0, "max_gap": 1, "missing": 1, "filled_rows": 1}


def test_month_boundary_two_closes_withdrawn():
    """30 October and 2 November, across the month end: depth 2."""
    rows = _sessions("NYSE", HIST, "2026-11-02")
    observed = _sessions("XSHG", HIST, "2026-10-29")
    p = ns.provenance(rows, observed, "XSHG", window_rows=W)
    assert ns.stale_extent(p, pd.Timestamp("2026-11-02")) == {
        "depth": 2, "max_gap": 2, "missing": 2, "filled_rows": 2}


def test_a_shenzhen_session_on_a_us_holiday_still_counts():
    """Thanksgiving 26 November 2026: Shenzhen trades, NYSE shuts, so no NYSE
    row requires that session. With closes observed only through the 25th,
    the 27 November price is two Shenzhen sessions stale, not one."""
    rows = _sessions("NYSE", HIST, "2026-11-27")
    observed = _sessions("XSHG", HIST, "2026-11-25")
    p = ns.provenance(rows, observed, "XSHG", window_rows=W)
    assert p.at[pd.Timestamp("2026-11-27"), "missing_native_sessions"] == [
        pd.Timestamp("2026-11-27")]
    assert ns.stale_extent(p, pd.Timestamp("2026-11-27")) == {
        "depth": 2, "max_gap": 2, "missing": 1, "filled_rows": 1}


def test_year_boundary_counts_unannounced_sessions_as_missing():
    """Across New Year, past the Shenzhen holiday data (2027 is not yet
    announced): 1, 4 and 5 January all count as sessions the price may have
    moved over. If any is a closure the true extent is smaller, so counting it
    only widens the range (the safe direction)."""
    rows = _sessions("NYSE", HIST, "2027-01-05")
    observed = _sessions("XSHG", HIST, "2026-12-31")
    p = ns.provenance(rows, observed, "XSHG", window_rows=W)
    assert ns.stale_extent(p, pd.Timestamp("2027-01-05")) == {
        "depth": 3, "max_gap": 3, "missing": 2, "filled_rows": 2}


def test_no_record_is_unmeasurable():
    p = ns.provenance(OCT_ROWS, None, "XSHG", window_rows=W)
    assert ns.stale_extent(p, pd.Timestamp("2026-10-28")) is None


# ---------------------------------------------------------------------------
# The signal range
# ---------------------------------------------------------------------------

def test_range_for_one_missing_close():
    lo, hi = ns.signal_range(-0.267, {"depth": 1, "max_gap": 1, "missing": 1, "filled_rows": 1},
                             W, L)
    d = 1 * 0.10 / W
    assert lo == pytest.approx(0.733 * 0.9 / (1 + d) - 1)
    assert hi == pytest.approx(0.733 * 1.1 / (1 - d) - 1)


def test_two_missing_closes_widen_the_range():
    one = ns.signal_range(0.0, {"depth": 1, "max_gap": 1, "missing": 1, "filled_rows": 1}, W, L)
    two = ns.signal_range(0.0, {"depth": 2, "max_gap": 2, "missing": 2, "filled_rows": 2}, W, L)
    assert two[0] < one[0] and two[1] > one[1]


@pytest.mark.parametrize("extent, bound, signal", [
    (None, L, 0.0),
    ({"depth": 1, "max_gap": 1, "missing": 1, "filled_rows": 1}, None, 0.0),
    ({"depth": 1, "max_gap": 1, "missing": 1, "filled_rows": 1}, L, float("nan")),
    ({"depth": 1, "max_gap": 60, "missing": 60, "filled_rows": W}, L, 0.0),   # average unbounded
])
def test_unknown_inputs_give_no_range(extent, bound, signal):
    assert ns.signal_range(signal, extent, W, bound) is None


# ---------------------------------------------------------------------------
# The corner test on C's own functions
# ---------------------------------------------------------------------------

def test_far_below_the_floor_with_the_gate_fired_is_immaterial():
    """The 2 November shape: 6 of 25 clear +5%, the gate parks C in SHY, and
    159801.SZ sits 26.7% below its average."""
    row = _row(-0.267, n_above=6)
    rng = ns.signal_range(-0.267, {"depth": 1, "max_gap": 1, "missing": 1, "filled_rows": 1}, W, L)
    assert th.sleeve_gate_state(row)["fired"]
    assert ns.decision_unchanged(row, {CN: rng}, C_WEIGHTS, th.sleeve_gate_state)


def test_near_the_floor_is_material():
    row = _row(0.03, n_above=9)
    rng = ns.signal_range(0.03, {"depth": 1, "max_gap": 1, "missing": 1, "filled_rows": 1}, W, L)
    assert rng[1] > th.SIGNAL_FLOOR
    assert not ns.decision_unchanged(row, {CN: rng}, C_WEIGHTS, th.sleeve_gate_state)


def test_a_line_that_could_flip_the_gate_is_material():
    """8 of 25 above (32%, gate off) with 159801.SZ one of them at +6%: a 10%
    lower print takes it under the floor, 7 of 25, and the gate fires."""
    row = _row(0.06, n_above=7)
    assert th.sleeve_gate_state(row)["n_above"] == 8
    assert not th.sleeve_gate_state(row)["fired"]
    rng = ns.signal_range(0.06, {"depth": 1, "max_gap": 1, "missing": 1, "filled_rows": 1}, W, L)
    assert not ns.decision_unchanged(row, {CN: rng}, C_WEIGHTS, th.sleeve_gate_state)


def test_a_held_line_that_cannot_leave_the_book_is_immaterial():
    """Equal weights: a line far above the floor and inside the top five keeps
    its one fifth at both ends, so even a held line can be immaterial."""
    row = _row(0.50, n_above=9)
    rng = ns.signal_range(0.50, {"depth": 1, "max_gap": 1, "missing": 1, "filled_rows": 1}, W, L)
    assert rng[0] > th.SIGNAL_FLOOR      # cannot fall under the floor ...
    # ... and stays in the top five at both ends, so the book is unchanged.
    assert ns.decision_unchanged(row, {CN: rng}, C_WEIGHTS, th.sleeve_gate_state)


# ---------------------------------------------------------------------------
# live_targets._rank
# ---------------------------------------------------------------------------

def _rank(row, observed, bound=L):
    return lt._rank(_panel(OCT_ROWS, row), C_WEIGHTS, "NYSE", OCT_NOW, "C",
                    signal_kind="ma_distance", top_k=th.HEADLINE_K,
                    cash_proxy=th.CASH_PROXY, gate_fn=th.sleeve_gate_state,
                    native_inputs={CN: {"venue": "XSHG", "observed": observed,
                                        "window_rows": W, "move_bound": bound}})


WITHDRAWN = _sessions("XSHG", HIST, "2026-10-27")


def test_rank_is_ready_when_the_filled_line_is_immaterial():
    r = _rank(_row(-0.267, n_above=9), WITHDRAWN)
    assert r["status"] == "READY" and r["reason"] is None
    assert CN not in r["weights"] and len(r["weights"]) == th.HEADLINE_K
    u = r["native_unconfirmed"][CN]
    assert u["signal_filled"] == -0.267 and u["move_bound"] == L
    assert u["stale_extent"] == {"depth": 1, "max_gap": 1, "missing": 1, "filled_rows": 1}
    assert u["required_native_session"] == "2026-10-28"
    assert u["native_close_carried"] == "2026-10-27"
    assert r["native_provenance"][CN]["current"] is False
    json.dumps(r)


def test_rank_is_ready_and_parked_when_the_gate_fires_either_way():
    r = _rank(_row(-0.267, n_above=6), WITHDRAWN)
    assert r["status"] == "READY"
    assert r["weights"] == {th.CASH_PROXY: 1.0}
    assert r["gate"]["fired"] and r["gate"]["n_above"] == 6


def test_rank_holds_when_the_filled_line_is_material():
    r = _rank(_row(0.03, n_above=9), WITHDRAWN)
    assert r["status"] == "HOLD" and r["weights"] == {}
    assert r["missing_signal_inputs"] == [CN]
    assert "could change the sleeve's weights or gate count" in r["reason"]
    assert "native_unconfirmed" not in r


def test_rank_holds_without_a_bound():
    r = _rank(_row(-0.267, n_above=9), WITHDRAWN, bound=None)
    assert r["status"] == "HOLD" and r["weights"] == {}


def test_rank_holds_without_a_record():
    r = _rank(_row(-0.267, n_above=9), None)
    assert r["status"] == "HOLD" and r["weights"] == {}


def test_a_current_line_carries_no_unconfirmed_record():
    r = _rank(_row(-0.267, n_above=9), _sessions("XSHG", HIST, "2026-10-28"))
    assert r["status"] == "READY" and "native_unconfirmed" not in r


def test_the_engine_passes_its_bound():
    assert th.NATIVE_DAILY_MOVE_BOUND == 0.10
    assert all(spec["move_bound"] == th.NATIVE_DAILY_MOVE_BOUND
               for spec in th.native_inputs().values())


# ---------------------------------------------------------------------------
# The freshness report
# ---------------------------------------------------------------------------

def _cache(tmp_path):
    p = tmp_path / "th.parquet"
    pd.DataFrame({"X": 1.0, CN: 1.0}, index=OCT_ROWS).to_parquet(p)
    import price_source
    frame = pd.read_parquet(p)
    rec = ns.sidecar_record({CN: WITHDRAWN}, frame)
    frame.to_parquet(p)
    price_source.write_cache_source(p, "yfinance", {ns.SIDECAR_KEY: rec})
    return p


def _measure(cache, materiality):
    return sf.native_cache_reach_or_carried(
        cache, ["X", CN], [CN], "C", "price cache (thematic ETFs)",
        window_rows=W, materiality=materiality)


def test_freshness_records_the_extent(tmp_path):
    reach, lag, _, _, native = _measure(_cache(tmp_path), None)
    assert reach == "2026-10-27" and lag == [CN]
    assert native[CN]["stale_extent"] == {"depth": 1, "max_gap": 1, "missing": 1, "filled_rows": 1}


def test_freshness_does_not_cap_an_immaterial_line(tmp_path):
    info = {"signal_range": [-0.34, -0.19]}
    reach, lag, _, _, native = _measure(_cache(tmp_path), lambda f, n: {CN: info})
    assert reach == "2026-10-28" and lag == []
    assert native[CN]["immaterial"] == info
    assert native[CN]["last_valid"] == "2026-10-27"      # the evidence is kept


def test_freshness_caps_a_material_line(tmp_path):
    reach, lag, *_ = _measure(_cache(tmp_path), lambda f, n: {})
    assert reach == "2026-10-27" and lag == [CN]


def test_a_materiality_fault_never_relaxes_the_cap(tmp_path):
    def boom(f, n):
        raise RuntimeError("signal unavailable")
    reach, lag, _, _, native = _measure(_cache(tmp_path), boom)
    assert reach == "2026-10-27" and lag == [CN]
    assert "immaterial" not in native[CN]


def _thematic(monkeypatch, cn, n_above, extent):
    row = _row(cn, n_above=n_above, others=len(th.TICKERS) - 1)
    names = [t for t in th.TICKERS if t != CN]
    row = row.rename(dict(zip([f"T{i:02d}" for i in range(len(names))], names)))
    frame = pd.DataFrame(1.0, index=OCT_ROWS, columns=list(row.index))
    sig = pd.DataFrame([row.to_dict()] * len(OCT_ROWS), index=OCT_ROWS)
    monkeypatch.setattr(th, "compute_signal", lambda closes: sig[closes.columns])
    native = {CN: {"last_valid": "2026-10-27", "stale_extent": extent}}
    return sf._thematic_native_materiality(frame, native)


ONE = {"depth": 1, "max_gap": 1, "missing": 1, "filled_rows": 1}


def test_thematic_materiality_relaxes_the_2_november_shape(monkeypatch):
    out = _thematic(monkeypatch, -0.267, 6, ONE)
    assert set(out) == {CN}
    assert out[CN]["move_bound"] == th.NATIVE_DAILY_MOVE_BOUND
    assert out[CN]["signal_range"][1] < th.SIGNAL_FLOOR


def test_thematic_materiality_keeps_a_line_near_the_floor(monkeypatch):
    assert _thematic(monkeypatch, 0.03, 9, ONE) == {}


def test_thematic_materiality_keeps_an_unmeasurable_line(monkeypatch):
    assert _thematic(monkeypatch, -0.267, 6, None) == {}
