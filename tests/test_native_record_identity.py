"""Independent-review P1 (2026-10-30): identical prices lent a stale record.

content_sha256 binds a native record to the frame's native values and rows;
record_sha256 binds the record's fields to one another. Neither says WHICH
acquisition observed the dates. When two acquisitions produce the same native
values -- A serves the 2026-04-03 Shenzhen close at Thursday's price, B
withdraws it and the forward fill carries Thursday's price onto the same rows
-- A's whole record passes both checks beside B's frame, and B's withdrawn
Good Friday ranks READY. Codex reproduced it three ways: B's sidecar
publication failing after its parquet (A's sidecar survives), two complete
writers overlapping between parquet and sidecar, and the two sidecar reads of
the cache-hit path seeing different generations.

The fix names the record in the frame: the parquet carries the record's
record_sha256 in its own metadata (native_sessions.FRAME_ATTR, written by
pandas as df.attrs), parse_sidecar refuses a record the frame does not name,
and the cache-hit path reads the record once. A frame without the name
(legacy, or rewritten by a tool that drops it) has no record: refresh,
unknown, HOLD.

Everything runs through the REAL producer (run_thematic_rotation.
download_prices with an offline vendor, constant CNY=X) and the REAL readers
(the cache-hit check, compute_signal, live_targets._rank with the production
top_k_equal_weight(HEADLINE_K) and sleeve_gate_state, native_inputs, and the
freshness reader). Tests whose docstring says "fails before the fix" fail on
the tree before this change. Offline; nothing leaves tmp_path. Python
datetime months are 1-indexed (January = 1); sessions come from the venue
calendars, never a weekday count.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import live_targets as lt  # noqa: E402
import native_sessions as ns  # noqa: E402
import price_source as ps  # noqa: E402
import strategy_freshness as sf  # noqa: E402
import test_native_record_binding as rb  # noqa: E402
import test_native_us_holiday_load_path as lp  # noqa: E402
import vendor_tail as vt  # noqa: E402

CN = "159801.SZ"
US_HOLE = pd.Timestamp("2026-03-10")          # a Tuesday both venues trade


def _vendor(th, withdraw=False, btc_last_missing=False, us_hole=None):
    """rb._vendor with the Good Friday close served at Thursday's price, so
    A (served) and B (withdrawn, then forward-filled) give identical native
    values on every NYSE row. CNY=X is the constant 7.0 of lp._vendor."""
    yf = rb._vendor(th, withdraw_good_friday=withdraw,
                    btc_last_missing=btc_last_missing)
    inner = yf.download

    def download(tickers, *a, **k):
        out = inner(tickers, *a, **k)
        if tickers != "CNY=X":
            if not withdraw:
                out.loc[lp.GOOD_FRIDAY, (CN, "Close")] = \
                    out.loc[lp.THURSDAY, (CN, "Close")]
            if us_hole is not None:
                out.loc[us_hole, (_us(th), "Close")] = np.nan
        return out
    return type("Y", (), {"download": staticmethod(download)})()


def _us(th):
    return next(t for t in th.TICKERS if ps.plain_us_listing(t))


@pytest.fixture(params=["norgate", "yfinance"])
def site(request, monkeypatch, tmp_path):
    import norgate_prices as npx
    import run_thematic_rotation as th
    cache = tmp_path / "thematic_prices_cache.parquet"
    monkeypatch.setattr(th, "PRICE_CACHE", cache)
    monkeypatch.setattr(th, "last_completed_session", lambda now: lp.LAST.date())
    monkeypatch.setattr(th, "cap_to_last_completed_session",
                        lambda df, now_utc=None: df.loc[:lp.LAST])
    monkeypatch.setattr(vt, "single_ticker_closes", lambda t: None)
    monkeypatch.setenv("BTE_PRICE_SOURCE", request.param)
    monkeypatch.delenv("BTE_C_BTC_BASIS", raising=False)
    monkeypatch.setattr(npx, "available", lambda: True)
    monkeypatch.setattr(
        npx, "select_columns",
        lambda df, tickers, s, e, label="": (
            df, {"replaced": [t for t in tickers if ps.plain_us_listing(t)],
                 "kept": [],
                 "unresolved": [t for t in tickers if not ps.plain_us_listing(t)]}))

    def run(**vendor_kw):
        monkeypatch.setattr(th, "yf", _vendor(th, **vendor_kw))
        return th.download_prices()
    return th, cache, run


A_RUN = {"btc_last_missing": True}      # Good Friday served; Bitcoin short
B_RUN = {"withdraw": True}              # Good Friday withdrawn; Bitcoin whole


def _on_disk_freshness(th):
    reach, *_ = sf.native_cache_reach_or_carried(
        th.PRICE_CACHE, list(th.TICKERS), [CN], "C", "x",
        window_rows=th.MA_PERIOD)
    return sf.classify(reach, str(lp.LAST.date()), "NYSE")[0]


def _verdict(th, frame):
    """The production verdict for ``frame``: the sleeve-C rank exactly as
    live_targets calls it, then the freshness reader on the pair on disk."""
    r = lt._rank(th.compute_signal(frame.reindex(
                     columns=lt._with_cash(th.TICKERS, th.CASH_PROXY))),
                 th.top_k_equal_weight(th.HEADLINE_K), "NYSE", lp.NOW, "C",
                 signal_kind="ma_distance", top_k=th.HEADLINE_K,
                 cash_proxy=th.CASH_PROXY, gate_fn=th.sleeve_gate_state,
                 native_inputs=th.native_inputs())
    return r["status"], r["weights"], _on_disk_freshness(th)


def _restart(th, run, capsys, **vendor_kw):
    capsys.readouterr()
    frame = run(**vendor_kw)
    log = capsys.readouterr().out
    return frame, ("Using cached prices" in log), _verdict(th, frame)


def _fsync_fails_once(monkeypatch):
    real = os.fsync
    state = {"fired": False}

    def fsync(fd):
        if not state["fired"]:
            state["fired"] = True
            raise OSError("injected fsync failure")
        return real(fd)
    monkeypatch.setattr(ps.os, "fsync", fsync)
    return state


# ---------------------------------------------------------------------------
# The controls: each frame with its own record
# ---------------------------------------------------------------------------

def test_the_controls(site, capsys):
    """B with B's record HOLDs, behind; A (Bitcoin whole) with A's record is
    READY, current. Both pairs are reused on the next run."""
    th, cache, run = site
    run(**B_RUN)
    b_frame, reused, got = _restart(th, run, capsys, **B_RUN)
    assert reused and got == ("HOLD", {}, sf.BEHIND)
    cache.unlink()                       # else A would reuse B's current pair
    ps.sidecar_path(cache).unlink()
    run()
    _, reused, got = _restart(th, run, capsys)
    assert reused and got[0] == "READY" and got[2] == sf.CURRENT
    assert lp.GOOD_FRIDAY in th.NATIVE_OBSERVATIONS[CN]
    # The premise: the two acquisitions' native values are identical.
    a_frame = pd.read_parquet(cache)
    assert ns.content_digest(a_frame, [CN]) == ns.content_digest(b_frame, [CN])


# ---------------------------------------------------------------------------
# 1. A failed publication: B's parquet beside A's whole sidecar
# ---------------------------------------------------------------------------

def test_a_failed_publication_cannot_lend_the_previous_record(site, monkeypatch,
                                                              capsys):
    """Fails before the fix: restart READY, current, on B's withdrawn bar."""
    th, cache, run = site
    run(**A_RUN)
    a_side = ps.read_cache_sidecar(cache)
    assert "2026-04-03" in a_side[ns.SIDECAR_KEY]["lines"][CN]["dates"]
    state = _fsync_fails_once(monkeypatch)
    with pytest.raises(OSError, match="injected fsync failure"):
        run(**B_RUN)
    assert state["fired"]
    assert ps.read_cache_sidecar(cache) == a_side           # A's, whole
    b_frame = pd.read_parquet(cache)
    assert not np.isnan(b_frame["BTC-USD"].iloc[-1])        # B's parquet
    assert ns.content_digest(b_frame, [CN]) == \
        a_side[ns.SIDECAR_KEY]["content_sha256"]             # both checksums pass
    # Publication reads the pair on disk: A's record is not B's frame's.
    published = _on_disk_freshness(th)
    _, reused, got = _restart(th, run, capsys, **B_RUN)
    print(f"\nfailed publication: on-disk freshness={published} "
          f"restart reused={reused} verdict={got}")
    assert published == sf.UNKNOWN
    assert not reused                                       # refreshed
    assert got == ("HOLD", {}, sf.BEHIND)                   # B's own verdict


# ---------------------------------------------------------------------------
# 2. Two complete writers overlapping between parquet and sidecar
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("outer", ["A", "B"])
@pytest.mark.parametrize("a_btc_short", [True, False],
                         ids=["parquets-differ", "parquets-identical"])
def test_two_complete_overlapping_writers(site, monkeypatch, capsys, outer,
                                          a_btc_short):
    """The outer writer replaces the parquet; the inner writer then runs
    start to finish; the outer writer publishes its sidecar last. Both
    succeed, and the pair on disk is one writer's frame beside the other's
    record. With Bitcoin whole on both, the two parquets were byte-identical
    before the fix. Fails before the fix: frame B beside record A ranked
    READY, current; frame A beside record B was read as B's (behind)."""
    th, cache, run = site
    kw = {"A": dict(A_RUN) if a_btc_short else {}, "B": dict(B_RUN)}
    inner = "B" if outer == "A" else "A"
    real = ps.write_cache_source
    fired = []

    def overlapping(*a, **k):
        if not fired:
            fired.append(inner)
            run(**kw[inner])                 # the other writer, whole
        return real(*a, **k)
    monkeypatch.setattr(ps, "write_cache_source", overlapping)
    run(**kw[outer])
    assert fired == [inner]
    monkeypatch.setattr(ps, "write_cache_source", real)
    side = ps.read_cache_sidecar(cache)[ns.SIDECAR_KEY]
    assert ("2026-04-03" in side["lines"][CN]["dates"]) == (outer == "A")
    on_disk = pd.read_parquet(cache)
    assert ns.content_digest(on_disk, [CN]) == side["content_sha256"]
    accepted = ns.parse_sidecar(ps.read_cache_sidecar(cache), on_disk, [CN])
    published = _on_disk_freshness(th)
    _, reused, got = _restart(th, run, capsys, **B_RUN)
    print(f"\noverlap outer={outer} a_btc_short={a_btc_short}: record "
          f"accepted={accepted is not None} on-disk freshness={published} "
          f"restart reused={reused} verdict={got}")
    assert accepted is None
    assert published == sf.UNKNOWN
    assert not reused
    assert got == ("HOLD", {}, sf.BEHIND)


# ---------------------------------------------------------------------------
# 3. The cache-hit path's record reads change generation
# ---------------------------------------------------------------------------

def test_the_cache_hit_reads_one_record(site, monkeypatch, capsys):
    """B's genuine pair is on disk. Between the cache-hit path's record check
    and its adoption of the record, another writer publishes A's whole
    sidecar. Fails before the fix: the check passed on B's record, the
    adoption took A's, READY, and the freshness reader called it current."""
    th, cache, run = site
    run(**A_RUN)
    a_bytes = ps.sidecar_path(cache).read_bytes()
    run(**B_RUN)                                     # refreshes: A was short
    real = ps.read_cache_sidecar
    reads = []

    def swapping(path):
        out = real(path)
        if sys._getframe(1).f_code.co_name == "_cached_native_observations":
            reads.append(out)
            if len(reads) == 1:
                ps.sidecar_path(cache).write_bytes(a_bytes)
        return out
    monkeypatch.setattr(ps, "read_cache_sidecar", swapping)
    _, reused, got = _restart(th, run, capsys, **B_RUN)
    adopted_a = lp.GOOD_FRIDAY in th.NATIVE_OBSERVATIONS[CN]
    print(f"\ncache-hit generation change: reads={len(reads)} "
          f"adopted A's dates={adopted_a} reused={reused} verdict={got}")
    assert reused
    assert not adopted_a                                       # B's record
    assert got[:2] == ("HOLD", {})
    assert got[2] == sf.UNKNOWN          # A's sidecar is now beside B's frame


# ---------------------------------------------------------------------------
# Missing or mismatched identity fails closed; matching pairs are reused
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", [None, "0" * 64, 7])
def test_a_frame_without_its_records_name_has_no_record(site, capsys, name):
    """A legacy parquet (no name), or one naming another record: the freshness
    reader is unknown and the next run refreshes. Fails before the fix: the
    record was accepted on content alone."""
    th, cache, run = site
    run()
    frame = pd.read_parquet(cache)
    if name is None:
        frame.attrs.clear()
    else:
        frame.attrs[ns.FRAME_ATTR] = name
    frame.to_parquet(cache)
    assert _on_disk_freshness(th) == sf.UNKNOWN
    _, reused, got = _restart(th, run, capsys)
    assert not reused
    assert got[0] == "READY" and got[2] == sf.CURRENT        # rebuilt, genuine


def test_the_parquet_names_the_record_beside_it(site):
    th, cache, run = site
    run(**B_RUN)
    side = ps.read_cache_sidecar(cache)[ns.SIDECAR_KEY]
    frame = pd.read_parquet(cache)
    assert frame.attrs[ns.FRAME_ATTR] == side["record_sha256"]
    assert ns.parse_sidecar(ps.read_cache_sidecar(cache), frame, [CN]) is not None


# ---------------------------------------------------------------------------
# Repairs: US-only keeps the record; native values and rows still void it
# ---------------------------------------------------------------------------

def test_a_us_only_repair_keeps_the_native_record(site, monkeypatch, capsys):
    """repair_price_gaps --apply rewrites the parquet without the sidecar. A
    US-column fill keeps the frame's name and native content: reused, READY,
    current, with the repaired value in place."""
    import repair_price_gaps as rp
    th, cache, run = site
    run(us_hole=US_HOLE)
    us = _us(th)
    before = pd.read_parquet(cache)
    assert np.isnan(before.at[US_HOLE, us])
    prev = before.index[before.index.get_loc(US_HOLE) - 1]
    monkeypatch.setattr(rp, "DATA_DIR", cache.parent)
    monkeypatch.setattr(rp, "LEDGER", cache.parent / "price_gap_repairs.jsonl")
    monkeypatch.setattr(rp, "fetch_primary", lambda *a, **k: pd.Series(
        {prev: 50.0, US_HOLE: 51.0}))
    monkeypatch.setattr(rp, "fetch_secondary",
                        lambda *a, **k: (pd.Series(dtype=float), ""))
    reps = rp.repair_cache("thematic", only_ticker=us, apply=True)
    assert [r["date"] for r in reps if r.get("value") is not None] == ["2026-03-10"]
    after = pd.read_parquet(cache)
    assert after.at[US_HOLE, us] == pytest.approx(before.at[prev, us] * 51.0 / 50.0)
    assert after.attrs.get(ns.FRAME_ATTR) == before.attrs.get(ns.FRAME_ATTR)
    assert _on_disk_freshness(th) == sf.CURRENT
    _, reused, got = _restart(th, run, capsys, us_hole=US_HOLE)
    assert reused and got[0] == "READY" and got[2] == sf.CURRENT


@pytest.mark.parametrize("change", ["native-value", "row-dropped"])
def test_a_native_change_on_disk_still_voids_the_record(site, capsys, change):
    """The name is kept by a rewrite, but content_sha256 still binds the
    native values and rows: another frame, no record, refreshed."""
    th, cache, run = site
    run()
    frame = pd.read_parquet(cache)
    name = frame.attrs[ns.FRAME_ATTR]
    if change == "native-value":
        frame.iloc[-30, frame.columns.get_loc(CN)] *= 1.001
    else:
        frame = frame.drop(frame.index[-30])
    assert frame.attrs[ns.FRAME_ATTR] == name
    frame.to_parquet(cache)
    assert _on_disk_freshness(th) == sf.UNKNOWN
    _, reused, _ = _restart(th, run, capsys)
    assert not reused
