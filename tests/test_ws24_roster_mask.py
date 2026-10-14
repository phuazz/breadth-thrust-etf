"""Selftests for the WS24 roster-mask library (scripts/ws24_roster_mask.py),
committed BEFORE any WS24 figure is computed (§5 of
KICKOFF_ws24-pit-roster-mask.md, frozen at 04607653).

Coverage maps to §5's further guards:

  G0 parity with the deployed function:
      test_m0_is_the_deployed_function_exactly,
      test_m2_without_barriers_or_mask_is_m0
  a joiner first counted on its first resolved snapshot, MA from pre-entry
  prices:   test_joiner_first_counted_after_its_entry_friday
  a leaver dropped after its last resolved snapshot:
            test_leaver_dropped_after_the_first_snapshot_without_it
  a Friday signal bar resolving to the previous Friday:
            test_friday_signal_bar_reads_previous_friday,
            test_holiday_shifted_bar_resolves_before_the_session
  an empty column beside a priced alias:
            test_empty_column_beside_priced_alias_counts_through_the_map,
            test_uncovered_alias_fails_g1
  a reused ticker inside its predecessor's membership window:
            test_identity_test_fails_a_stand_in_security,
            test_g4_cells_excluded_from_every_arm_but_m0
  a barrier date inside a mask window (FOXA, 2019-03-12):
            test_barrier_inside_mask_window_never_spans_two_securities
  Date-boundary rule (vault CLAUDE.md, one month and one year boundary):
            test_month_boundary_resolution, test_year_boundary_resolution,
            test_refresh_window_boundaries
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import ws24_roster_mask as R  # noqa: E402
from run_ma200_sweep import compute_ma200_breadth  # noqa: E402 (deployed leg)


def _sessions(start: str, end: str) -> pd.DatetimeIndex:
    # start/end bounds, never `periods` (local pandas 3.0.0 bdate_range bug).
    return pd.bdate_range(start, end)


def _fridays(start: str, end: str) -> list[str]:
    return [d.strftime("%Y-%m-%d")
            for d in pd.date_range(start, end, freq="W-FRI")]


def _trend(index, start=100.0, drift=0.001, seed=0):
    rng = np.random.default_rng(seed)
    r = drift + 0.01 * rng.standard_normal(len(index))
    return pd.Series(start * np.exp(np.cumsum(r)), index=index)


def _snapmap(keys, members_by_key):
    return {k: {"tickers": list(members_by_key(k))} for k in keys}


# ---------------------------------------------------------------------------
# G0
# ---------------------------------------------------------------------------

def test_m0_is_the_deployed_function_exactly():
    idx = _sessions("2018-01-01", "2020-06-30")
    rng = np.random.default_rng(7)
    px = pd.DataFrame({f"T{i}": _trend(idx, seed=i) for i in range(12)})
    # sparse missingness, a late listing and a delisting
    px = px.mask(rng.random(px.shape) < 0.02)
    px.loc[: "2018-09-30", "T3"] = np.nan
    px.loc["2019-11-01":, "T5"] = np.nan
    deployed = compute_ma200_breadth(px, 200)
    pd.testing.assert_series_equal(R.masked_breadth(px), deployed,
                                   check_exact=True, check_names=False)
    pd.testing.assert_series_equal(R.arm_breadth("M0", px, [], {}, {}),
                                   deployed, check_exact=True,
                                   check_names=False)


def test_m2_without_barriers_or_mask_is_m0():
    idx = _sessions("2018-01-01", "2019-12-31")
    px = pd.DataFrame({f"T{i}": _trend(idx, seed=i) for i in range(5)})
    split, applied = R.era_split(px, {})
    assert applied == {} and split is px
    pd.testing.assert_series_equal(R.masked_breadth(split),
                                   compute_ma200_breadth(px, 200),
                                   check_exact=True, check_names=False)


def test_unregistered_arm_raises():
    with pytest.raises(ValueError):
        R.arm_breadth("M3", pd.DataFrame(), [], {}, {})


# ---------------------------------------------------------------------------
# Roster timing (O7)
# ---------------------------------------------------------------------------

def test_friday_signal_bar_reads_previous_friday():
    keys = _fridays("2026-09-04", "2026-10-09")
    fri = pd.Timestamp("2026-10-09")
    assert fri.weekday() == 4
    assert R.resolve_key(keys, fri, "strict") == "2026-10-02"
    assert R.resolve_key(keys, fri, "le") == "2026-10-09"
    mon = pd.Timestamp("2026-10-12")
    assert mon.weekday() == 0
    assert R.resolve_key(keys, mon, "strict") == "2026-10-09"


def test_holiday_shifted_bar_resolves_before_the_session():
    # Good Friday 2026-04-03: the decision falls back to Thursday 2026-04-02,
    # which must still read the 2026-03-27 roster, never the 04-03 key.
    keys = _fridays("2026-03-06", "2026-04-10")
    thu = pd.Timestamp("2026-04-02")
    assert thu.weekday() == 3
    assert R.resolve_key(keys, thu, "strict") == "2026-03-27"
    all_keys = _fridays("2018-01-05", "2026-10-09")
    for d in pd.bdate_range("2018-01-08", "2026-10-09"):
        key = R.resolve_key(all_keys, d, "strict")
        assert key is None or key < d.strftime("%Y-%m-%d")


def test_month_boundary_resolution():
    keys = _fridays("2019-05-03", "2019-06-14")
    assert pd.Timestamp("2019-05-31").weekday() == 4      # last Friday of May
    assert R.resolve_key(keys, pd.Timestamp("2019-05-31"), "strict") == "2019-05-24"
    assert R.resolve_key(keys, pd.Timestamp("2019-06-03"), "strict") == "2019-05-31"
    assert R.resolve_key(keys, pd.Timestamp("2019-06-07"), "le") == "2019-06-07"


def test_year_boundary_resolution():
    keys = _fridays("2018-12-07", "2019-01-11")
    assert pd.Timestamp("2018-12-31").weekday() == 0
    assert pd.Timestamp("2019-01-02").weekday() == 2
    assert R.resolve_key(keys, pd.Timestamp("2018-12-31"), "strict") == "2018-12-28"
    assert R.resolve_key(keys, pd.Timestamp("2019-01-02"), "strict") == "2018-12-28"
    assert R.resolve_key(keys, pd.Timestamp("2019-01-04"), "strict") == "2018-12-28"
    assert R.resolve_key(keys, pd.Timestamp("2019-01-04"), "le") == "2019-01-04"
    assert R.resolve_key(keys, pd.Timestamp("2018-12-07"), "strict") is None


# ---------------------------------------------------------------------------
# Joiner, leaver
# ---------------------------------------------------------------------------

def _two_name_panel():
    idx = _sessions("2018-01-01", "2019-12-31")
    px = pd.DataFrame({"A": _trend(idx, drift=-0.002, seed=1),   # below MA
                       "J": _trend(idx, drift=0.004, seed=2)})   # above MA
    return idx, px


def test_joiner_first_counted_after_its_entry_friday():
    idx, px = _two_name_panel()
    keys = _fridays("2018-01-05", "2019-12-27")
    entry = "2019-03-01"                                     # J joins here
    snaps = _snapmap(keys, lambda k: ["A"] + (["J"] if k >= entry else []))
    m1 = R.arm_breadth("M1", px, keys, snaps, {})
    m0 = R.masked_breadth(px)
    # On the entry Friday itself J is not yet known (strict rule) ...
    assert m1.loc["2019-03-01"] == pytest.approx(
        R.masked_breadth(px[["A"]]).loc["2019-03-01"])
    # ... and on the next session it counts, with a valid MA built on
    # pre-entry prices (no warm-up restart at entry).
    first = pd.Timestamp("2019-03-04")
    assert m1.loc[first] == pytest.approx(m0.loc[first])
    ma = px["J"].rolling(200, min_periods=180).mean()
    assert pd.notna(ma.loc[first])


def test_leaver_dropped_after_the_first_snapshot_without_it():
    idx, px = _two_name_panel()
    keys = _fridays("2018-01-05", "2019-12-27")
    last_in = "2019-06-07"
    snaps = _snapmap(keys, lambda k: ["A"] + (["J"] if k <= last_in else []))
    member = R.membership_matrix(px.index, px.columns, keys, snaps, "strict")
    # Friday 06-14 resolves to the 06-07 roster, so J still counts that day;
    # the Monday after reads 06-14, which no longer carries J.
    assert bool(member.loc["2019-06-14", "J"])
    assert not bool(member.loc["2019-06-17", "J"])
    assert not member.loc["2019-06-17":, "J"].any()


# ---------------------------------------------------------------------------
# Alias map and G1
# ---------------------------------------------------------------------------

def _berkshire_panel():
    idx = _sessions("2018-01-01", "2019-06-30")
    px = pd.DataFrame({"A": _trend(idx, seed=1),
                       "BRK-B": _trend(idx, seed=3),
                       "BRK B": np.nan}, index=idx)
    keys = _fridays("2018-01-05", "2019-06-28")
    snaps = _snapmap(keys, lambda k: ["A", "BRK B"])
    return px, keys, snaps


def _n_with_price_50d(px, keys, snaps):
    # The 50-day panel's own count: roster (key on or before d) intersected
    # with the cache columns, priced on d, NO alias map.
    out = {}
    for d in px.index:
        key = R.resolve_key(keys, d, "le")
        roster = [t for t in R.roster_symbols(snaps, key) if t in px.columns]
        out[d] = int(px.loc[d, roster].notna().sum())
    return pd.Series(out)


def test_empty_column_beside_priced_alias_counts_through_the_map():
    px, keys, snaps = _berkshire_panel()
    member = R.membership_matrix(px.index, px.columns, keys, snaps, "strict")
    assert member.loc["2019-03-04", "BRK-B"]
    assert not member.loc["2019-03-04", "BRK B"]
    g1 = R.g1_reconcile(px, keys, snaps, _n_with_price_50d(px, keys, snaps))
    assert g1["pass"], g1


def test_uncovered_alias_fails_g1():
    px, keys, snaps = _berkshire_panel()
    g1 = R.g1_reconcile(px, keys, snaps, _n_with_price_50d(px, keys, snaps),
                        aliases={})
    assert not g1["pass"]
    assert g1["uncovered_aliases"] == {"BRK B": ["BRK-B"]}


def test_g1_fails_when_the_mask_prices_fewer_members():
    px, keys, snaps = _berkshire_panel()
    n50 = _n_with_price_50d(px, keys, snaps) + 2      # a later vintage
    g1 = R.g1_reconcile(px, keys, snaps, n50)
    assert not g1["pass"] and g1["shortfall_days"] > 0


def test_alias_candidates():
    assert R.alias_candidates("BF B") == ["BF-B", "BF.B"]
    assert R.alias_candidates("RTX US") == ["RTX"]
    assert R.alias_candidates("AAPL") == []


# ---------------------------------------------------------------------------
# G4 identity and symmetric exclusion
# ---------------------------------------------------------------------------

def test_identity_test_fails_a_stand_in_security():
    idx = _sessions("2018-01-01", "2019-12-31")
    true = _trend(idx, seed=11)
    stand_in = _trend(idx, seed=12)                      # another security
    same_basis_shift = true * 0.6                        # same security, rebased
    assert R.identity_test(stand_in, true)["status"] == "fail"
    assert R.identity_test(same_basis_shift, true)["status"] == "pass"
    assert R.identity_test(true.iloc[:10], true)["status"] == "unverified"


def test_g4_cells_excluded_from_every_arm_but_m0():
    idx, px = _two_name_panel()
    keys = _fridays("2018-01-05", "2019-12-27")
    snaps = _snapmap(keys, lambda k: ["A", "J"])
    cells = [{"column": "J", "start": "2019-01-01", "end": "2019-03-29"}]
    d = pd.Timestamp("2019-02-01")
    only_a = R.masked_breadth(px[["A"]]).loc[d]
    assert R.arm_breadth("M0", px, keys, snaps, {}, cells).loc[d] != only_a
    for arm in ("M0s", "M1", "M2"):
        assert R.arm_breadth(arm, px, keys, snaps, {}, cells).loc[d] == \
            pytest.approx(only_a), arm
    after = pd.Timestamp("2019-04-01")
    assert R.arm_breadth("M1", px, keys, snaps, {}, cells).loc[after] == \
        pytest.approx(R.masked_breadth(px).loc[after])


def test_membership_runs():
    keys = _fridays("2019-01-04", "2019-03-01")
    snaps = _snapmap(keys, lambda k: ["X"] if k < "2019-01-25"
                     or k > "2019-02-08" else [])
    assert R.membership_runs(keys, snaps, "X") == [
        ("2019-01-04", "2019-01-18"), ("2019-02-15", "2019-03-01")]


# ---------------------------------------------------------------------------
# Era barriers (M2)
# ---------------------------------------------------------------------------

def test_barrier_inside_mask_window_never_spans_two_securities():
    idx = _sessions("2018-01-01", "2019-12-31")
    barrier = "2019-03-12"            # FOXA: 21st Century Fox -> Fox Corp
    old = _trend(idx, start=40.0, drift=0.002, seed=21)
    new = _trend(idx, start=20.0, drift=0.002, seed=22)
    foxa = old.where(idx < barrier).combine_first(new.where(idx >= barrier))
    px = pd.DataFrame({"A": _trend(idx, seed=1), "FOXA": foxa})
    keys = _fridays("2018-01-05", "2019-12-27")
    snaps = _snapmap(keys, lambda k: ["A", "FOXA"])
    barriers = {"FOXA": barrier}
    split, applied = R.era_split(px, barriers)
    assert applied == barriers
    assert split["FOXA"].loc[barrier:].isna().all()
    assert split[R.era_column("FOXA", barrier)].loc[:"2019-03-11"].isna().all()
    m1 = R.arm_breadth("M1", px, keys, snaps, barriers)
    m2 = R.arm_breadth("M2", px, keys, snaps, barriers)
    soon = pd.Timestamp("2019-04-15")       # inside the later era's warm-up
    # M1 still counts FOXA on an average spanning both securities; M2 counts
    # only A until the later era has 180 bars of its own.
    assert m2.loc[soon] == pytest.approx(R.masked_breadth(px[["A"]]).loc[soon])
    assert m1.loc[soon] == pytest.approx(R.masked_breadth(px).loc[soon])
    late = pd.Timestamp("2019-12-31")
    ma_new = split[R.era_column("FOXA", barrier)].rolling(
        200, min_periods=180).mean()
    assert pd.notna(ma_new.loc[late])
    member = R.membership_matrix(split.index, split.columns, keys, snaps,
                                 "strict", applied)
    assert member.loc[late, R.era_column("FOXA", barrier)]
    assert not member.loc[late, "FOXA"]


# ---------------------------------------------------------------------------
# D1 and D3
# ---------------------------------------------------------------------------

def test_contamination_classes_partition_non_member_cells():
    idx = _sessions("2019-01-01", "2019-03-29")
    m = pd.DataFrame(False, index=idx, columns=["P", "Q", "N"])
    m.loc["2019-02-01":"2019-02-15", "P"] = True
    m.loc["2019-01-01":"2019-01-31", "Q"] = True
    m.loc["2019-03-01":, "Q"] = True
    cls = R.contamination_classes(m)
    total = cls["pre_entry"] | cls["post_exit"] | cls["between_windows"]
    assert (total == ~m).all().all()
    assert cls["pre_entry"]["N"].all()
    assert cls["between_windows"].loc["2019-02-11", "Q"]
    assert cls["post_exit"].loc["2019-03-01", "P"]


def test_duplicate_lineage_flags_a_renamed_copy():
    idx = _sessions("2019-01-01", "2019-12-31")
    meta = _trend(idx, seed=5)
    px = pd.DataFrame({"FB": meta, "META": meta * 1.7,
                       "X": _trend(idx, seed=6)})
    member = pd.DataFrame(False, index=idx, columns=px.columns)
    member["FB"] = True
    member["X"] = True
    flag = R.duplicate_lineage(px, member)
    assert flag["META"].all()
    assert not flag["X"].any() and not flag["FB"].any()


def test_selection_agreement():
    d = pd.DatetimeIndex(["2019-01-07", "2019-01-14"])
    a = pd.DataFrame({"X": [0.5, 0.5], "Y": [0.5, 0.5]}, index=d)
    b = pd.DataFrame({"X": [0.5, 1.0], "Y": [0.5, 0.0]}, index=d)
    out = R.selection_agreement(a, b, d)
    assert out["identical_share"] == 0.5
    assert out["mean_abs_weight_diff"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Machine-load guard
# ---------------------------------------------------------------------------

def test_refresh_window_boundaries():
    cases = {
        datetime(2026, 10, 14, 14, 59): True,    # Wednesday, inside
        datetime(2026, 10, 14, 15, 0): False,    # Wednesday, window closed
        datetime(2026, 10, 15, 10, 0): False,    # Thursday
        datetime(2026, 10, 31, 9, 0): True,      # Saturday, month boundary
        datetime(2026, 11, 2, 9, 0): False,      # Monday
        datetime(2026, 12, 31, 10, 0): False,    # Thursday, year boundary
        datetime(2027, 1, 2, 9, 30): True,       # Saturday, new year
        datetime(2027, 1, 3, 8, 59): False,      # Sunday, before 09:00
    }
    weekdays = {datetime(2026, 10, 14): 2, datetime(2026, 10, 31): 5,
                datetime(2026, 12, 31): 3, datetime(2027, 1, 2): 5,
                datetime(2027, 1, 3): 6}
    for d, wd in weekdays.items():
        assert d.weekday() == wd, d
    for now, expected in cases.items():
        assert R.refresh_window_open(now) is expected, now


# ---------------------------------------------------------------------------
# Runner guards (§5.3): checksums and the network refusal
# ---------------------------------------------------------------------------

def test_checksums_refuse_a_tampered_or_unlisted_file(tmp_path):
    import run_ws24_roster_mask as M
    (tmp_path / "a.json").write_text("{}", encoding="utf-8")
    (tmp_path / "SHA256SUMS").write_text(
        f"{M.sha256(tmp_path / 'a.json')}  a.json\n", encoding="utf-8")
    assert M.verify_checksums(tmp_path) == 1
    (tmp_path / "b.json").write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit):
        M.verify_checksums(tmp_path)
    (tmp_path / "b.json").unlink()
    (tmp_path / "a.json").write_text("{ }", encoding="utf-8")
    with pytest.raises(SystemExit):
        M.verify_checksums(tmp_path)


def test_network_guard_serves_only_the_pinned_fx(monkeypatch):
    import yfinance
    import backtest
    import run_asset_class_rotation as B
    import run_thematic_rotation as C
    import run_ws24_roster_mask as M
    for mod, attr in ((yfinance, "download"), (backtest, "download_soxx_ohlc"),
                      (B, "download_prices"), (C, "download_prices")):
        monkeypatch.setattr(mod, attr, getattr(mod, attr))
    fx = pd.DataFrame({"Close": [1.1]}, index=pd.to_datetime(["2020-01-02"]))
    M.install_network_guard(fx)
    assert yfinance.download("EURUSD=X").equals(fx)
    for call in (lambda: yfinance.download("SPY"),
                 lambda: backtest.download_soxx_ohlc("2020-01-01", "2020-02-01"),
                 lambda: B.download_prices(), lambda: C.download_prices()):
        with pytest.raises(RuntimeError):
            call()


def test_staging_flags_refused(monkeypatch):
    import run_ws24_roster_mask as M
    monkeypatch.setenv("BTE_APPLY_STAGED_ROSTER", "1")
    with pytest.raises(SystemExit):
        M.assert_staging_flags_unset()
    monkeypatch.delenv("BTE_APPLY_STAGED_ROSTER")
    monkeypatch.delenv("BTE_C_BTC_BASIS", raising=False)
    assert M.assert_staging_flags_unset() == {f: "unset" for f in M.STAGING_FLAGS}
