"""WS21 — sleeve C ranks Bitcoin on IBIT, staged behind BTE_C_BTC_BASIS.

WHAT THESE PIN. Registration ``KICKOFF_ws21-c-bitcoin-basis.md`` stages one
construction: the incumbent synthetic proxy frozen to 2024-01-11 inclusive,
then ``S_c * IBIT_t / IBIT_c`` with no expense drag, carried under the same
``BTC-USD`` key. Nothing about it may reach the live book before the WS7
verdict, so the first thing pinned is that the flag OFF changes nothing at all.

The rest are the three silent-wrong ways from registration §8, one test group
each:

  §8.1 anchor drift          the frozen artefact's bytes are hashed and the
                             loader refuses a mismatch rather than warning;
  §8.2 basis read as vendor  the cache sidecar declares the column's basis and
                             price_revisions calls the first rebuild a basis
                             change rather than 430 revisions;
  §8.3 strict Norgate        IBIT is a US line the strict path must TAKE while
                             the spot ticker never was, so the unresolved set
                             goes 2 -> 1 and a shortfall raises.

Plus the two the registration names alongside them: the tail heal asks IBIT and
never spot, and the seal refuses to publish while the flag is set.

Every vendor call is stubbed. Python datetime months are 1-indexed
(January = 1); every session in the join arithmetic comes from
venue_calendars.get_calendar("NYSE"), never from a day count.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import btc_basis  # noqa: E402
import price_revisions as pr  # noqa: E402
import price_source as ps  # noqa: E402
import vendor_tail as vt  # noqa: E402
from venue_calendars import get_calendar  # noqa: E402

CUT = pd.Timestamp(btc_basis.CUTOVER)          # 2024-01-11, a Thursday


def nyse(start, end) -> pd.DatetimeIndex:
    """Completed NYSE sessions, inclusive. The only session source here."""
    sched = get_calendar("NYSE").schedule(start_date=str(start), end_date=str(end))
    return pd.DatetimeIndex(pd.to_datetime(sched.index)).normalize()


def frozen_artefact(tmp_path, index=None, s_c=45674.257342138306):
    """A stand-in frozen segment with the real artefact's shape.

    Ends at the cut-over and carries ``S_c`` there, which is the whole contract
    ``load_frozen_segment`` enforces.
    """
    idx = index if index is not None else nyse("2023-11-01", btc_basis.CUTOVER)
    values = np.linspace(s_c * 0.8, s_c, len(idx))
    values[-1] = s_c
    series = pd.Series(values, index=idx, name=btc_basis.SPOT_KEY)
    parquet = tmp_path / "btc_proxy_history_pre_ibit.parquet"
    series.to_frame(btc_basis.SPOT_KEY).to_parquet(parquet)
    sidecar = tmp_path / "btc_proxy_history_pre_ibit.source.json"
    sidecar.write_text(json.dumps({
        "column": btc_basis.SPOT_KEY,
        "sha256": btc_basis.file_sha256(parquet),
        "rows": int(len(series)),
        "first": str(series.index.min().date()),
        "last": str(series.index.max().date()),
        "S_c": float(s_c),
        "cutover": str(btc_basis.CUTOVER),
        "derivation": btc_basis.DERIVATION,
    }, indent=2), encoding="utf-8")
    return parquet, sidecar, series


@pytest.fixture
def frozen(tmp_path, monkeypatch):
    """Point the module's artefact constants at a temporary pair."""
    parquet, sidecar, series = frozen_artefact(tmp_path)
    monkeypatch.setattr(btc_basis, "FROZEN_PARQUET", parquet)
    monkeypatch.setattr(btc_basis, "FROZEN_SIDECAR", sidecar)
    return parquet, sidecar, series


# ---------------------------------------------------------------------------
# The flag
# ---------------------------------------------------------------------------
def test_unset_is_the_incumbent_and_a_typo_is_refused(monkeypatch):
    monkeypatch.delenv(btc_basis.ENV_VAR, raising=False)
    assert btc_basis.requested_basis() == btc_basis.INCUMBENT
    assert btc_basis.is_ibit() is False
    monkeypatch.setenv(btc_basis.ENV_VAR, "IBIT")          # case-insensitive
    assert btc_basis.is_ibit() is True
    monkeypatch.setenv(btc_basis.ENV_VAR, "ibitt")
    with pytest.raises(ValueError, match="is not a basis"):
        btc_basis.requested_basis()


def test_the_default_is_still_incumbent():
    """The one line that flips the book. It flips only after the WS7 verdict
    is filed (registration §3), by a dated commit — not by accident here."""
    assert btc_basis.DEFAULT == btc_basis.INCUMBENT


def test_the_fetch_list_swaps_only_the_bitcoin_line(monkeypatch):
    names = ["ARKK", btc_basis.SPOT_KEY, "SHY"]
    monkeypatch.delenv(btc_basis.ENV_VAR, raising=False)
    assert btc_basis.fetch_list(names) == names
    monkeypatch.setenv(btc_basis.ENV_VAR, "ibit")
    assert btc_basis.fetch_list(names) == ["ARKK", "IBIT", "SHY"]


# ---------------------------------------------------------------------------
# §8.1 — the anchor is the whole risk
# ---------------------------------------------------------------------------
def test_the_hash_guard_refuses_bytes_nobody_signed(frozen, tmp_path):
    parquet, sidecar, series = frozen
    loaded, meta = btc_basis.load_frozen_segment()
    assert len(loaded) == len(series) and float(loaded.loc[CUT]) == float(series.loc[CUT])

    # A revision of pre-2024 history: one cell moves, the hash no longer matches.
    tampered = series.copy()
    tampered.iloc[0] *= 1.0001
    tampered.to_frame(btc_basis.SPOT_KEY).to_parquet(parquet)
    with pytest.raises(btc_basis.BasisError, match="does not match its sidecar"):
        btc_basis.load_frozen_segment()


def test_a_missing_or_unsigned_artefact_refuses(tmp_path, monkeypatch):
    parquet, sidecar, series = frozen_artefact(tmp_path)
    monkeypatch.setattr(btc_basis, "FROZEN_PARQUET", parquet)
    monkeypatch.setattr(btc_basis, "FROZEN_SIDECAR", sidecar)
    sidecar.write_text(json.dumps({"column": btc_basis.SPOT_KEY}), encoding="utf-8")
    with pytest.raises(btc_basis.BasisError, match="records no sha256"):
        btc_basis.load_frozen_segment()
    parquet.unlink()
    with pytest.raises(btc_basis.BasisError, match="is missing"):
        btc_basis.load_frozen_segment()


def test_the_artefact_must_end_at_the_cutover(tmp_path, monkeypatch):
    """A segment ending anywhere else anchors the splice on the wrong value."""
    short = nyse("2023-11-01", "2024-01-09")
    parquet, sidecar, _ = frozen_artefact(tmp_path, index=short)
    monkeypatch.setattr(btc_basis, "FROZEN_PARQUET", parquet)
    monkeypatch.setattr(btc_basis, "FROZEN_SIDECAR", sidecar)
    with pytest.raises(btc_basis.BasisError, match="expected the cut-over"):
        btc_basis.load_frozen_segment()


def test_the_basis_tag_moves_with_the_anchor(frozen):
    _, _, series = frozen
    tag = btc_basis.declared_basis({btc_basis.ENV_VAR: "ibit"})
    assert tag.startswith("ibit-spliced@") and len(tag.split("@")[1]) == 12
    assert btc_basis.declared_basis({}) is None, "incumbent declares nothing"


# ---------------------------------------------------------------------------
# The join — continuity, no drag, and the calendar edges
# ---------------------------------------------------------------------------
def _ibit(index, first=26.63):
    """IBIT total-return closes over ``index``, which must start at the
    cut-over (2024-01-11 IS its first NYSE close, Yahoo 26.63)."""
    steps = np.linspace(0.0, 0.35, len(index))
    return pd.Series(first * np.exp(steps), index=index)


def test_the_cutover_value_is_preserved_exactly(frozen):
    _, _, series = frozen
    live = nyse(btc_basis.CUTOVER, "2024-03-01")
    out = btc_basis.splice(series, _ibit(live))
    assert float(out.loc[CUT]) == float(series.loc[CUT])
    # And nothing before the cut-over moved by so much as a bit.
    before = series.loc[series.index < CUT]
    pd.testing.assert_series_equal(out.loc[out.index < CUT], before,
                                   check_names=False)


def test_the_first_post_cutover_return_is_ibits_own(frozen):
    """The join must not manufacture a day move — the failure WS19b recorded
    for per-cell source merges, which invented several per cent at every
    junction."""
    _, _, series = frozen
    live = nyse(btc_basis.CUTOVER, "2024-03-01")
    ibit = _ibit(live)
    out = btc_basis.splice(series, ibit)
    first_after = live[1]
    spliced_ret = float(out.loc[first_after] / out.loc[CUT] - 1.0)
    ibit_ret = float(ibit.loc[first_after] / ibit.loc[CUT] - 1.0)
    assert spliced_ret == pytest.approx(ibit_ret, rel=0, abs=1e-15)
    # Every later return too, not just the first.
    assert (out.loc[out.index >= CUT].pct_change().dropna()
            - ibit.pct_change().dropna()).abs().max() == pytest.approx(0, abs=1e-12)


def test_no_expense_drag_after_the_cutover(frozen):
    """IBIT's price is already net of the 25 bp/yr the model charges BEFORE the
    cut-over. Charging it again would compound the fee twice — a ~25 bp/yr
    understatement that no surface would show."""
    _, _, series = frozen
    live = nyse(btc_basis.CUTOVER, "2025-01-10")            # about one year
    ibit = _ibit(live)
    out = btc_basis.splice(series, ibit)
    ratio = (out.loc[live] / (float(series.loc[CUT]) / float(ibit.loc[CUT]) * ibit))
    assert float(ratio.max() - ratio.min()) == pytest.approx(0.0, abs=1e-12)
    assert float(ratio.iloc[-1]) == pytest.approx(1.0, abs=1e-12), (
        "a drag would show as a ratio drifting below 1 over the year")


def test_ibit_bars_at_or_before_the_cutover_never_contribute(frozen):
    """The frozen segment owns that span by registration. A vendor bar there
    would reopen the anchor the freeze exists to close."""
    _, _, series = frozen
    live = nyse("2023-12-01", "2024-03-01")                 # starts BEFORE c
    ibit = _ibit(live)
    out = btc_basis.splice(series, ibit)
    pd.testing.assert_series_equal(out.loc[out.index <= CUT],
                                   series.loc[series.index <= CUT],
                                   check_names=False)


def test_a_missing_cutover_close_refuses_rather_than_guessing(frozen):
    _, _, series = frozen
    live = nyse("2024-01-12", "2024-03-01")                 # no bar at c
    with pytest.raises(btc_basis.BasisError, match="no close at the cut-over"):
        btc_basis.splice(series, _ibit(live))


@pytest.mark.parametrize("cutover, through, label", [
    (date(2024, 1, 31), "2024-02-15", "month boundary: Wed 31 Jan -> Thu 1 Feb"),
    (date(2024, 12, 31), "2025-01-15", "year boundary: Tue 31 Dec -> Thu 2 Jan"),
])
def test_the_join_holds_across_month_and_year_boundaries(cutover, through, label):
    """The two edges any date arithmetic gets wrong. Both sides come from the
    NYSE calendar, so 1 January and a weekend are absent by construction rather
    than by a day count that happens to skip them."""
    before = nyse("2023-10-02", cutover)
    frozen = pd.Series(np.linspace(100.0, 200.0, len(before)), index=before)
    live = nyse(cutover, through)
    ibit = _ibit(live, first=40.0)
    out = btc_basis.splice(frozen, ibit, cutover=cutover)

    cut = pd.Timestamp(cutover)
    assert float(out.loc[cut]) == float(frozen.loc[cut]), label
    # The session AFTER the boundary is the calendar's next one, not "+1 day".
    nxt = live[1]
    assert nxt > cut and nxt.date() != cutover
    assert float(out.loc[nxt] / out.loc[cut]) == pytest.approx(
        float(ibit.loc[nxt] / ibit.loc[cut]), abs=1e-15)
    assert out.index.is_monotonic_increasing and not out.index.has_duplicates
    assert out.index.difference(nyse("2023-10-02", through)).empty


# ---------------------------------------------------------------------------
# §8.2 — a basis change, read as one
# ---------------------------------------------------------------------------
def test_the_sidecar_carries_a_per_column_basis(tmp_path):
    cache = tmp_path / "thematic_prices_cache.parquet"
    ps.write_cache_source(cache, "norgate", {
        "replaced": ["ARKK"], "kept": [], "unresolved": ["159801.SZ"],
        "column_basis": {btc_basis.SPOT_KEY: "ibit-spliced@7ab4a26a7dcf"}})
    blob = json.loads(ps.sidecar_path(cache).read_text(encoding="utf-8"))
    assert blob["column_basis"] == {btc_basis.SPOT_KEY: "ibit-spliced@7ab4a26a7dcf"}
    assert ps.read_cache_column_basis(cache)[btc_basis.SPOT_KEY] == \
        "ibit-spliced@7ab4a26a7dcf"
    # An incumbent cache declares nothing, and so does one with no sidecar.
    ps.write_cache_source(cache, "norgate", {"replaced": ["ARKK"]})
    assert ps.read_cache_column_basis(cache) == {}
    assert ps.read_cache_column_basis(tmp_path / "absent.parquet") == {}


def test_the_declared_basis_wins_over_the_feed_fields():
    """The feed answers "who served this column", which stops being the whole
    answer once a column is CONSTRUCTED rather than downloaded."""
    sidecar = {"source": "norgate", "columns_from_norgate": ["ARKK", "BTC-USD"],
               "column_basis": {"BTC-USD": "ibit-spliced@abcdef012345"}}
    basis = pr.resolve_column_basis(sidecar)
    assert basis["BTC-USD"] == "ibit-spliced@abcdef012345"
    assert basis["ARKK"] == pr.NORGATE


def test_a_rebuild_under_the_flag_is_classified_as_a_basis_change():
    """THE §8.2 FAILURE, exactly. The first rebuild changes every populated
    cell from the cut-over onward. Without the declared basis the guard either
    warns of a vendor retraction or logs it ambiguous and moves on."""
    idx = nyse("2024-01-11", "2024-02-09")
    old = pd.DataFrame({"ARKK": np.linspace(50.0, 55.0, len(idx)),
                        "BTC-USD": np.linspace(45674.0, 48000.0, len(idx))},
                       index=idx)
    new = old.copy()
    new["BTC-USD"] = new["BTC-USD"] * 1.017          # the splice moves the level
    old_sidecar = {"source": "norgate", "columns_from_norgate": ["ARKK"],
                   "columns_kept_on_incumbent": ["BTC-USD"]}
    new_sidecar = {"source": "norgate", "columns_from_norgate": ["ARKK"],
                   "column_basis": {"BTC-USD": "ibit-spliced@7ab4a26a7dcf"}}
    out = pr.diff_frames(old, new, old_sidecar=old_sidecar,
                         new_sidecar=new_sidecar, window_sessions=None)
    assert out["comparable"] is True
    assert out["basis_changes"] == int(len(idx)), out["samples"]
    assert out["revisions"] == 0 and out["ambiguous"] == 0
    assert out["adjustments"] == 0
    sample = out["samples"]["basis_changes"][0]
    assert sample["column"] == "BTC-USD"
    assert sample["old_basis"] == pr.YFINANCE
    assert sample["new_basis"] == "ibit-spliced@7ab4a26a7dcf"

    # The control: the SAME cell moves with no declared basis, and it is a
    # revision or an adjustment — anything but a basis change.
    plain = pr.diff_frames(old, new, old_sidecar=old_sidecar,
                           new_sidecar=old_sidecar, window_sessions=None)
    assert plain["basis_changes"] == 0
    assert plain["revisions"] + plain["adjustments"] + plain["ambiguous"] \
        == int(len(idx))


# ---------------------------------------------------------------------------
# §8.3 — strict-Norgate completeness, inverted
# ---------------------------------------------------------------------------
def test_the_spot_ticker_was_never_expected_from_norgate():
    incumbent = ["ARKK", "159801.SZ", btc_basis.SPOT_KEY, "SHY"]
    report = {"replaced": ["ARKK", "SHY"], "kept": [],
              "unresolved": ["159801.SZ", btc_basis.SPOT_KEY]}
    ps.assert_norgate_complete(report, incumbent, "Strategy C")   # no raise


def test_ibit_is_a_us_line_the_strict_path_must_take():
    """Under the flag the spot ticker leaves the list and IBIT joins it, so the
    'never expected' set shrinks by one and the expected set grows by one. A
    loader that still counted BTC-USD as never-expected would record a yfinance
    frame as Norgate-built."""
    staged = ["ARKK", "159801.SZ", btc_basis.TRADED, "SHY"]
    served = {"replaced": ["ARKK", btc_basis.TRADED, "SHY"], "kept": [],
              "unresolved": ["159801.SZ"]}
    ps.assert_norgate_complete(served, staged, "Strategy C")      # no raise
    assert ps.plain_us_listing(btc_basis.TRADED) is True
    assert ps.plain_us_listing(btc_basis.SPOT_KEY) is False

    withheld = {"replaced": ["ARKK", "SHY"], "kept": [btc_basis.TRADED],
                "unresolved": ["159801.SZ"]}
    with pytest.raises(RuntimeError, match=r"Norgate supplied 2 of 3"):
        ps.assert_norgate_complete(withheld, staged, "Strategy C")


def test_the_unresolved_set_goes_from_two_to_one(monkeypatch):
    """The count stated in registration §8.3, read off the fetch list rather
    than asserted in prose."""
    import run_thematic_rotation as th
    needed = th.TICKERS + [th.CASH_PROXY]
    monkeypatch.delenv(btc_basis.ENV_VAR, raising=False)
    incumbent = {t for t in btc_basis.fetch_list(needed)
                 if not ps.plain_us_listing(t)}
    monkeypatch.setenv(btc_basis.ENV_VAR, "ibit")
    staged = {t for t in btc_basis.fetch_list(needed)
              if not ps.plain_us_listing(t)}
    assert incumbent == {"159801.SZ", btc_basis.SPOT_KEY}
    assert staged == {"159801.SZ"}


# ---------------------------------------------------------------------------
# Through the engine
# ---------------------------------------------------------------------------
def _yf(frame: pd.DataFrame, needed):
    """Shape yf.download returns under group_by='ticker'."""
    yf_frame = pd.concat({t: pd.DataFrame({"Close": frame[t]}) for t in needed},
                         axis=1)
    return type("Y", (), {"download": staticmethod(lambda *a, **k: yf_frame.copy())})()


@pytest.fixture
def engine(monkeypatch, tmp_path):
    """Sleeve C over a window that straddles the cut-over, with the vendor,
    Norgate and the frozen artefact all stubbed."""
    import norgate_prices as npx
    import run_thematic_rotation as th

    idx = nyse("2023-11-01", "2024-03-01")
    last = idx[-1]
    engine_names = th.TICKERS + [th.CASH_PROXY]
    assert btc_basis.SPOT_KEY in engine_names

    def frame_for(names):
        data = {}
        for i, t in enumerate(names):
            data[t] = np.linspace(100.0 + i, 130.0 + i, len(idx))
        f = pd.DataFrame(data, index=idx)
        if btc_basis.TRADED in f.columns:
            f[btc_basis.TRADED] = _ibit(idx, first=26.63).values
            # IBIT does not exist before its first close; blank that span so the
            # stub is not quietly more generous than the vendor.
            f.loc[f.index < CUT, btc_basis.TRADED] = np.nan
        return f

    parquet, sidecar, frozen_series = frozen_artefact(
        tmp_path, index=idx[idx <= CUT])
    monkeypatch.setattr(btc_basis, "FROZEN_PARQUET", parquet)
    monkeypatch.setattr(btc_basis, "FROZEN_SIDECAR", sidecar)

    cache = tmp_path / "thematic_prices_cache.parquet"
    monkeypatch.setattr(th, "PRICE_CACHE", cache)
    monkeypatch.setattr(th, "last_completed_session", lambda now: last.date())
    monkeypatch.setattr(th, "cap_to_last_completed_session",
                        lambda df, now_utc=None: df)
    monkeypatch.setattr(th, "_fx_convert_to_usd", lambda df: df)
    monkeypatch.setattr(npx, "available", lambda: True)
    monkeypatch.setattr(
        npx, "select_columns",
        lambda df, tickers, s, e, label="": (
            df, {"replaced": [t for t in tickers if ps.plain_us_listing(t)],
                 "kept": [],
                 "unresolved": [t for t in tickers if not ps.plain_us_listing(t)]}))

    def install(names):
        f = frame_for(names)
        monkeypatch.setattr(th, "yf", _yf(f, names))
        monkeypatch.setattr(vt, "single_ticker_closes",
                            lambda t, **k: f[t].ffill())
        return f

    return th, cache, install, engine_names, frozen_series, idx


def test_flag_unset_is_bit_identical_to_the_incumbent(engine, monkeypatch):
    """THE STANDARD OF RECORD 2026-08-15-breadth-thrust-etf-1: a staged change
    that is off must change no value anywhere. Cache and signal, on one pinned
    frame, cell for cell — not within a tolerance."""
    th, cache, install, engine_names, _, _ = engine
    monkeypatch.setenv("BTE_PRICE_SOURCE", "norgate")

    monkeypatch.delenv(btc_basis.ENV_VAR, raising=False)
    install(engine_names)
    baseline = th.download_prices()
    baseline_cache = pd.read_parquet(cache)
    baseline_sidecar = json.loads(
        ps.sidecar_path(cache).read_text(encoding="utf-8"))
    baseline_signal = th.compute_signal(baseline)

    monkeypatch.setenv(btc_basis.ENV_VAR, "incumbent")   # explicitly, not just unset
    install(engine_names)
    again = th.download_prices()

    pd.testing.assert_frame_equal(again, baseline)
    pd.testing.assert_frame_equal(pd.read_parquet(cache), baseline_cache)
    pd.testing.assert_frame_equal(th.compute_signal(again), baseline_signal)
    assert btc_basis.TRADED not in again.columns
    blob = json.loads(ps.sidecar_path(cache).read_text(encoding="utf-8"))
    assert "column_basis" not in blob
    assert blob["unresolved"] == baseline_sidecar["unresolved"]
    assert set(blob["unresolved"]) == {"159801.SZ", btc_basis.SPOT_KEY}


def test_the_staged_run_builds_the_composite_and_hides_ibit(engine, monkeypatch):
    th, cache, install, engine_names, frozen_series, idx = engine
    monkeypatch.setenv("BTE_PRICE_SOURCE", "norgate")
    monkeypatch.setenv(btc_basis.ENV_VAR, "ibit")
    fetched = install(btc_basis.fetch_list(engine_names))
    out = th.download_prices()

    assert btc_basis.TRADED not in out.columns
    assert list(out.columns) == list(pd.read_parquet(cache).columns)
    assert float(out.loc[CUT, btc_basis.SPOT_KEY]) == \
        float(frozen_series.loc[CUT])
    live = idx[idx > CUT]
    ibit = fetched[btc_basis.TRADED]
    ratio = out.loc[live, btc_basis.SPOT_KEY] / ibit.loc[live]
    assert float(ratio.max() - ratio.min()) == pytest.approx(0.0, abs=1e-9)

    blob = json.loads(ps.sidecar_path(cache).read_text(encoding="utf-8"))
    assert blob["column_basis"][btc_basis.SPOT_KEY].startswith("ibit-spliced@")
    assert blob["unresolved"] == ["159801.SZ"]
    assert btc_basis.TRADED not in blob["columns_from_norgate"], (
        "half the column is a frozen Yahoo segment; 'taken from Norgate' "
        "would be a half-truth about the other half")


def test_the_staged_cache_is_not_reused_by_an_incumbent_run(engine, monkeypatch,
                                                            capsys):
    """WS19's vacuous switch, in a third costume: a cache current through the
    right session but built on the OTHER construction."""
    th, cache, install, engine_names, _, _ = engine
    monkeypatch.setenv("BTE_PRICE_SOURCE", "norgate")
    monkeypatch.setenv(btc_basis.ENV_VAR, "ibit")
    install(btc_basis.fetch_list(engine_names))
    staged = th.download_prices()

    install(btc_basis.fetch_list(engine_names))
    th.download_prices()
    assert "Using cached prices" in capsys.readouterr().out, \
        "a matching-basis cache must still be reused"

    monkeypatch.delenv(btc_basis.ENV_VAR, raising=False)
    install(engine_names)
    incumbent = th.download_prices()
    printed = capsys.readouterr().out
    assert "is on basis ibit-spliced@" in printed
    assert "Using cached prices" not in printed
    assert not np.allclose(incumbent[btc_basis.SPOT_KEY].dropna().values,
                           staged[btc_basis.SPOT_KEY].dropna().values)


def test_the_tail_heal_asks_ibit_and_never_spot(engine, monkeypatch):
    """Registration §8, the fourth guard: the column now follows IBIT, so a
    probe for spot would fill a hole in the staged series with the very series
    the staging replaces."""
    th, cache, install, engine_names, _, idx = engine
    monkeypatch.setenv("BTE_PRICE_SOURCE", "yfinance")
    monkeypatch.setenv(btc_basis.ENV_VAR, "ibit")
    names = btc_basis.fetch_list(engine_names)
    fetched = install(names)
    fetched.loc[idx[-1], btc_basis.TRADED] = np.nan      # the blank tail cell

    asked = []

    def fetch(t, **k):
        asked.append(t)
        return fetched[t].ffill()

    monkeypatch.setattr(th, "yf", _yf(fetched, names))
    monkeypatch.setattr(vt, "single_ticker_closes", fetch)
    out = th.download_prices()

    assert asked == [btc_basis.TRADED]
    assert btc_basis.SPOT_KEY not in asked
    assert pd.notna(out.loc[idx[-1], btc_basis.SPOT_KEY])
    blob = json.loads(ps.sidecar_path(cache).read_text(encoding="utf-8"))
    assert blob["tail_heal"]["rows"][0]["filled"] == [btc_basis.TRADED]


def test_the_staged_series_changes_only_after_the_cutover(engine, monkeypatch,
                                                          tmp_path):
    """The registration's claim, end to end: before ``c`` the staged series IS
    the incumbent, cell for cell, and after it the series moves.

    The frozen artefact is rebuilt HERE from the incumbent run's own output,
    which is what ``freeze_btc_proxy_history.py`` does against the real cache.
    Freezing anything else would make the first half of this assertion vacuous.
    """
    th, cache, install, engine_names, _, idx = engine
    monkeypatch.setenv("BTE_PRICE_SOURCE", "norgate")

    monkeypatch.delenv(btc_basis.ENV_VAR, raising=False)
    install(engine_names)
    incumbent = th.download_prices()[btc_basis.SPOT_KEY]

    seg = incumbent.loc[incumbent.index <= CUT].dropna()
    seg.to_frame(btc_basis.SPOT_KEY).to_parquet(btc_basis.FROZEN_PARQUET)
    meta = json.loads(btc_basis.FROZEN_SIDECAR.read_text(encoding="utf-8"))
    meta["sha256"] = btc_basis.file_sha256(btc_basis.FROZEN_PARQUET)
    meta["S_c"] = float(seg.loc[CUT])
    btc_basis.FROZEN_SIDECAR.write_text(json.dumps(meta, indent=2),
                                        encoding="utf-8")

    monkeypatch.setenv(btc_basis.ENV_VAR, "ibit")
    install(btc_basis.fetch_list(engine_names))
    staged = th.download_prices()[btc_basis.SPOT_KEY]

    before = idx[idx <= CUT]
    assert (incumbent.loc[before] - staged.loc[before]).abs().max() == 0.0, \
        "the frozen segment IS the incumbent series before the cut-over"
    after = idx[idx > CUT]
    assert (incumbent.loc[after] - staged.loc[after]).abs().max() > 0.0
    # The level is retained for cache continuity, so the two stay the same
    # order of magnitude — the splice changes the series' shape, not its units.
    assert 0.5 < float(staged.loc[after].mean() / incumbent.loc[after].mean()) < 2.0


# ---------------------------------------------------------------------------
# The staged basis cannot reach the published book
# ---------------------------------------------------------------------------
def test_the_seal_refuses_while_the_flag_is_set(monkeypatch):
    """Driven at the guard's own position in ``seal``: the book, the held basis
    and the pre-trade report are stubbed PASSING, so the only thing that can
    raise is the flag."""
    import check_pretrade_ready as cpr
    import component_release as cr
    monkeypatch.setattr(cr, "read", lambda path: {})
    monkeypatch.setattr(cr, "validate_book", lambda *a, **k: {"d_ready": False})
    monkeypatch.setattr(cpr, "build_book_report",
                        lambda *a, **k: {"status": "ready", "detail": ""})
    monkeypatch.delenv("BTE_APPLY_STAGED_ROSTER", raising=False)

    for value in ("ibit", "IBIT ", "spliced"):
        monkeypatch.setenv(btc_basis.ENV_VAR, value)
        with pytest.raises(ValueError, match="staged sleeve C Bitcoin basis"):
            cr.seal()

    # The incumbent passes this guard and fails later, on the refresh receipt —
    # which is the proof that the flag, and not the stubbing, is what refused.
    for value in ("incumbent", ""):
        monkeypatch.setenv(btc_basis.ENV_VAR, value)
        with pytest.raises(Exception) as caught:
            cr.seal()
        assert "staged sleeve C Bitcoin basis" not in str(caught.value)


def test_the_seal_mirrors_the_staged_roster_rule():
    """Same shape, same site, so the two cannot drift apart."""
    src = (ROOT / "scripts" / "component_release.py").read_text(encoding="utf-8")
    seal = src[src.index("def seal("):src.index("def verify(")]
    assert "BTE_APPLY_STAGED_ROSTER" in seal and btc_basis.ENV_VAR in seal
    assert seal.index("BTE_APPLY_STAGED_ROSTER") < seal.index(btc_basis.ENV_VAR)


def test_no_scheduled_path_sets_the_flag():
    """Registration §6: scheduled runs never set it.

    Asserted over the surfaces that can actually set an environment variable
    for an unattended run — the workflows and the two refresh drivers — rather
    than over every mention of the name, which would only prove that the
    comments explaining the flag exist.
    """
    paths = [*(ROOT / ".github" / "workflows").glob("*.yml"),
             *(ROOT / ".github" / "workflows").glob("*.yaml")]
    for name in ("scheduled_refresh.py", "refresh_all.py", "auto_release.py",
                 "run_weekly_factsheet.py", "pipeline.py"):
        candidate = ROOT / "scripts" / name
        if candidate.exists():
            paths.append(candidate)
    hits = [p.name for p in paths
            if btc_basis.ENV_VAR in p.read_text(encoding="utf-8", errors="ignore")]
    assert not hits, f"{btc_basis.ENV_VAR} appears in an unattended path: {hits}"


# ---------------------------------------------------------------------------
# The reader-facing half (independent of the flag)
# ---------------------------------------------------------------------------
def test_the_registry_names_the_fund_the_book_holds():
    from etf_registry import display_ticker, get_etf
    assert display_ticker(btc_basis.SPOT_KEY) == btc_basis.TRADED
    assert get_etf(btc_basis.SPOT_KEY)["yfinance_trading_proxy"] == btc_basis.TRADED
    assert get_etf(btc_basis.SPOT_KEY)["name"] == "iShares Bitcoin Trust ETF"


def test_the_gap_repair_refuses_a_declared_basis_and_still_fills_an_incumbent_one(
        tmp_path, monkeypatch):
    """A PROMOTION PREREQUISITE, pinned rather than left to review.

    repair_price_gaps splices the RETURN of a source onto the cached level,
    which is safe only because each ticker's sources are declared against the
    construction its column carries: BTC-USD's cached series is the UTC-day
    spot close and Binance BTCUSDT is quoted on the same clock. Under the
    staged basis the column follows IBIT at the 16:00 ET close, so BOTH the
    primary (yfinance BTC-USD) and the secondary (Binance) would splice a
    return measured on another clock — a move IBIT did not make, missing its
    premium/discount entirely, on a sleeve whose floor is +5%.

    The answer is a refusal, not a different source: choosing one is a
    construction decision, and WS21 fixed one construction with no menu.
    """
    import numpy as np
    import repair_price_gaps as rp

    sessions = nyse("2026-06-01", "2026-08-31")
    frame = pd.DataFrame(
        {btc_basis.SPOT_KEY: np.linspace(40000.0, 44000.0, len(sessions)),
         "ARKK": np.linspace(50.0, 55.0, len(sessions))}, index=sessions)
    gap, prev = sessions[-3], sessions[-4]
    frame.loc[gap, btc_basis.SPOT_KEY] = np.nan
    cache = tmp_path / "unit.parquet"
    frame.to_parquet(cache)
    monkeypatch.setattr(rp, "DATA_DIR", tmp_path)
    monkeypatch.setitem(rp.CACHES, "unit", ("unit.parquet", "n/a"))
    spot = pd.Series({prev: 100.0, gap: 104.0})
    monkeypatch.setattr(rp, "fetch_primary", lambda t, s, e: spot)
    monkeypatch.setattr(rp, "fetch_secondary",
                        lambda t, s, e: (spot, "binance:BTCUSDT"))

    # No declared basis — every cache today. Behaviour is unchanged.
    ps.write_cache_source(cache, "norgate", {"replaced": ["ARKK"]})
    (before,) = rp.repair_cache("unit", only_ticker=btc_basis.SPOT_KEY,
                                apply=False, sessions=sessions)
    assert before["source"] == "primary:yfinance"
    assert before["value"] == pytest.approx(
        float(frame.loc[prev, btc_basis.SPOT_KEY]) * 1.04)
    assert "declared_basis" not in before

    # Declared ibit-spliced — reported, not filled, and the reason names why.
    ps.write_cache_source(cache, "norgate", {
        "replaced": ["ARKK"],
        "column_basis": {btc_basis.SPOT_KEY: "ibit-spliced@7ab4a26a7dcf"}})
    (after,) = rp.repair_cache("unit", only_ticker=btc_basis.SPOT_KEY,
                               apply=False, sessions=sessions)
    assert after.get("value") is None, "a refused column must print no number"
    assert after["declared_basis"] == "ibit-spliced@7ab4a26a7dcf"
    assert "another clock" in after["refused"]
    assert after["date"] == str(gap.date())

    # And --apply writes nothing for a refused column.
    digest_before = cache.read_bytes()
    rp.repair_cache("unit", only_ticker=btc_basis.SPOT_KEY, apply=True,
                    sessions=sessions)
    assert cache.read_bytes() == digest_before


def test_the_published_label_never_describes_a_construction_not_in_use(monkeypatch):
    """The prose half of what the registry entry fixes on the ticker half."""
    import run_thematic_rotation as th
    monkeypatch.delenv(btc_basis.ENV_VAR, raising=False)
    assert th._universe_label(btc_basis.SPOT_KEY) == \
        th.UNIVERSE[btc_basis.SPOT_KEY]["label"]
    assert "CoinDesk spot" in th._universe_label(btc_basis.SPOT_KEY)
    monkeypatch.setenv(btc_basis.ENV_VAR, "ibit")
    assert th._universe_label(btc_basis.SPOT_KEY) == btc_basis.LABEL
    assert "IBIT from 2024-01-11" in btc_basis.LABEL
    # Every other member has one label under either basis.
    for t in ("ARKK", "XBI", "159801.SZ"):
        assert th._universe_label(t) == th.UNIVERSE[t]["label"]


def test_the_universe_entry_keeps_the_fields_the_incumbent_basis_needs():
    """Removing them is a PROMOTION step, not a tidy-up. While the default is
    the incumbent, the drag and the calendar alignment are load-bearing: the
    series would silently lose its modelled fee and its NYSE reindex."""
    import run_thematic_rotation as th
    entry = th.UNIVERSE[btc_basis.SPOT_KEY]
    assert entry["expense_ratio_bps"] == 25
    assert entry["trading_calendar"] == "crypto_24x7"
    assert btc_basis.DEFAULT == btc_basis.INCUMBENT


def test_the_price_exporter_fetches_ibit_for_the_bitcoin_line():
    """component_release.price_evidence looks the changed position up by its
    registry trading proxy, so the exporter has to publish that key or the seal
    falls back to the spot series for its evidence."""
    import export_holdings_prices as ehp
    assert ehp.resolve_book_symbol(btc_basis.SPOT_KEY) == btc_basis.TRADED
    assert ehp.resolve_book_symbol("EXH3") == "EXH4.DE"        # unchanged
    assert ehp.resolve_book_symbol("ARKK") == "ARKK"           # unchanged
