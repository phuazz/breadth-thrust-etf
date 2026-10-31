"""Closing-review P1 (2026-10-30): overlapping sidecar writes made a false READY.

price_source.write_cache_source wrote the published sidecar in place. Two
runs overlapping on it -- B opens and truncates, A completes its genuine
write, B writes its prefix (with B's content_sha256) and fails before
"lines" -- left valid JSON carrying B's digest and A's observation dates. B's
frame matched the digest, so a restart accepted A's Good Friday observation
for B's withdrawn bar: READY, {"SHY": 1.0}, freshness current, where the same
B frame with its own record HOLDs.

Two changes close it. price_source.publish_text writes each sidecar to its own
mkstemp file in the same directory and os.replace swaps it in, so a failed or
overlapped write never leaves a mixture and never disturbs the published file.
native_sessions.record_digest binds the record's fields to one another
(``record_sha256``), so a record not written in one piece is refused by
parse_sidecar, the one reader every path uses, however it arose.

The overlap is driven through the REAL producer and the REAL restart reader.
The only injection is at the file boundary (io.open, which both Path.write_text
and os.fdopen call). Tests marked "fails before the fix" fail on the tree
before 2026-10-30's atomic publication. Offline; nothing leaves tmp_path.
Python datetime months are 1-indexed (January = 1); sessions come from the
venue calendars, never a weekday count.
"""
from __future__ import annotations

import io
import json
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import live_targets as lt  # noqa: E402
import native_sessions as ns  # noqa: E402
import price_source as ps  # noqa: E402
import run_thematic_rotation as th  # noqa: E402
import strategy_freshness as sf  # noqa: E402
from venue_calendars import get_calendar  # noqa: E402

CN = "159801.SZ"
GOOD_FRIDAY = pd.Timestamp("2026-04-03")      # Shenzhen traded; NYSE shut
LAST = "2026-04-17"
NOW = datetime(2026, 4, 18, 3, 0, tzinfo=timezone.utc)


def _sessions(venue, start, end):
    sched = get_calendar(venue).schedule(start_date=start, end_date=end)
    return pd.DatetimeIndex([pd.Timestamp(d).normalize() for d in sched.index])


ROWS = _sessions("NYSE", "2025-03-03", LAST)
XSHG = _sessions("XSHG", "2025-03-03", LAST)
COLS = lt._with_cash(th.TICKERS, th.CASH_PROXY)


def _frames():
    """A: Good Friday observed. B: Good Friday withdrawn, so the Qingming row
    carries Thursday's close. Same rows and shape, so their sidecars have the
    same length up to the native record's "lines" key."""
    a = pd.DataFrame({t: 1.0 for t in COLS}, index=ROWS)
    a[CN] = 0.5 + 0.001 * np.arange(len(ROWS))
    b = a.copy()
    b.at[pd.Timestamp("2026-04-06"), CN] = b.at[pd.Timestamp("2026-04-02"), CN]
    a_rep = {ns.SIDECAR_KEY: ns.sidecar_record({CN: XSHG}, a)}
    b_rep = {ns.SIDECAR_KEY: ns.sidecar_record({CN: XSHG[XSHG != GOOD_FRIDAY]}, b)}
    return (a, a_rep), (b, b_rep)


class _Partial:
    """A writer's handle that writes its text up to ``stop`` (exclusive),
    flushes, then fails -- the partial write Codex injected."""

    def __init__(self, real, stop='"lines"'):
        self._real, self._stop = real, stop

    def write(self, s):
        self._real.write(s[:s.index(self._stop)])
        self._real.flush()
        raise OSError("injected partial write")

    def __getattr__(self, name):
        return getattr(self._real, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._real.close()
        return False


def _inject(monkeypatch, during_open=None, partial=True):
    """Patch io.open so the NEXT write-mode open runs ``during_open`` (another
    writer, to completion) while this handle is open, and optionally makes
    this handle fail part-way. Returns the record of what fired."""
    real_open = io.open
    state = {"fired": False}

    def faulty(file, mode="r", *args, **kwargs):
        if "w" not in mode or state["fired"]:
            return real_open(file, mode, *args, **kwargs)
        state["fired"] = True
        handle = real_open(file, mode, *args, **kwargs)
        if during_open is not None:
            io.open = real_open
            try:
                during_open()
            finally:
                io.open = faulty
        return _Partial(handle) if partial else handle

    monkeypatch.setattr(io, "open", faulty)
    return state


def _published(cache):
    return json.loads(ps.sidecar_path(cache).read_text(encoding="utf-8"))


def _strays(cache):
    keep = {Path(cache).name, ps.sidecar_path(cache).name}
    return sorted(p.name for p in Path(cache).parent.iterdir() if p.name not in keep)


def _restart(cache, monkeypatch):
    """A fresh run reading the cache back, as production does: the cache-hit
    record check, the sleeve-C rank (live_targets line ~499, production
    top_k_equal_weight and gate) and the freshness reader."""
    monkeypatch.setattr(th, "PRICE_CACHE", cache)
    cached = pd.read_parquet(cache)
    obs = th._cached_native_observations(cached)
    monkeypatch.setattr(th, "NATIVE_OBSERVATIONS", obs)
    r = lt._rank(th.compute_signal(cached.reindex(columns=COLS)),
                 th.top_k_equal_weight(th.HEADLINE_K), "NYSE", NOW, "C",
                 signal_kind="ma_distance", top_k=th.HEADLINE_K,
                 cash_proxy=th.CASH_PROXY, gate_fn=th.sleeve_gate_state,
                 native_inputs=th.native_inputs())
    reach, _, *_ = sf.native_cache_reach_or_carried(
        cache, list(th.TICKERS), [CN], "C", "x", window_rows=th.MA_PERIOD)
    return {"reused": obs is not None, "status": r["status"],
            "weights": r["weights"],
            "freshness": sf.classify(reach, LAST, "NYSE")[0]}


# ---------------------------------------------------------------------------
# The reproduced overlap, through the real producer and restart reader
# ---------------------------------------------------------------------------

def _overlap(tmp_path, monkeypatch, existing=True):
    """B's parquet is published; B's sidecar write overlaps A's complete,
    genuine write_cache_source and fails before "lines"."""
    (a, a_rep), (b, b_rep) = _frames()
    cache = tmp_path / "thematic_prices_cache.parquet"
    if existing:
        a.to_parquet(cache)
        ps.write_cache_source(cache, "yfinance", a_rep)
    b.to_parquet(cache)
    state = _inject(monkeypatch, lambda: ps.write_cache_source(
        cache, "yfinance", a_rep))
    with pytest.raises(OSError, match="injected partial write"):
        ps.write_cache_source(cache, "yfinance", b_rep)
    assert state["fired"]                    # later opens pass straight through
    return cache, a_rep, b_rep


def test_the_overlap_publishes_one_whole_record(tmp_path, monkeypatch):
    """Fails before the fix: the sidecar carried B's digest and A's dates."""
    cache, a_rep, b_rep = _overlap(tmp_path, monkeypatch)
    rec = _published(cache)[ns.SIDECAR_KEY]
    assert rec == a_rep[ns.SIDECAR_KEY]                  # A's, whole
    assert rec["record_sha256"] == ns.record_digest(rec)
    assert _strays(cache) == []                          # B's temp removed


def test_the_overlap_cannot_rank_ready_on_restart(tmp_path, monkeypatch):
    """Fails before the fix: READY, {"SHY": 1.0}, freshness current."""
    cache, _, _ = _overlap(tmp_path, monkeypatch)
    got = _restart(cache, monkeypatch)
    assert got == {"reused": False, "status": "HOLD", "weights": {},
                   "freshness": sf.UNKNOWN}


def test_the_controls_hold_on_b_and_reuse_a_matching_pair(tmp_path, monkeypatch):
    (a, a_rep), (b, b_rep) = _frames()
    cache = tmp_path / "thematic_prices_cache.parquet"
    b.to_parquet(cache)
    ps.write_cache_source(cache, "yfinance", b_rep)
    got = _restart(cache, monkeypatch)
    assert (got["reused"], got["status"], got["weights"], got["freshness"]) == (
        True, "HOLD", {}, sf.BEHIND)
    a.to_parquet(cache)
    ps.write_cache_source(cache, "yfinance", a_rep)
    got = _restart(cache, monkeypatch)
    assert got == {"reused": True, "status": "READY", "weights": {"SHY": 1.0},
                   "freshness": sf.CURRENT}


def test_the_overlap_with_no_prior_sidecar(tmp_path, monkeypatch):
    cache, a_rep, _ = _overlap(tmp_path, monkeypatch, existing=False)
    assert _published(cache)[ns.SIDECAR_KEY] == a_rep[ns.SIDECAR_KEY]
    assert _restart(cache, monkeypatch)["status"] == "HOLD"
    assert _strays(cache) == []


# ---------------------------------------------------------------------------
# Failed writes leave the published sidecar as it was
# ---------------------------------------------------------------------------

def test_a_failed_write_keeps_the_existing_sidecar(tmp_path, monkeypatch):
    (a, a_rep), (b, b_rep) = _frames()
    cache = tmp_path / "thematic_prices_cache.parquet"
    a.to_parquet(cache)
    ps.write_cache_source(cache, "yfinance", a_rep)
    before = ps.sidecar_path(cache).read_bytes()
    b.to_parquet(cache)
    _inject(monkeypatch)
    with pytest.raises(OSError, match="injected partial write"):
        ps.write_cache_source(cache, "yfinance", b_rep)
    assert ps.sidecar_path(cache).read_bytes() == before
    assert _strays(cache) == []
    assert _restart(cache, monkeypatch)["status"] == "HOLD"   # A's record, B's frame


def test_a_failed_write_with_no_sidecar_publishes_nothing(tmp_path, monkeypatch):
    (_, _), (b, b_rep) = _frames()
    cache = tmp_path / "thematic_prices_cache.parquet"
    b.to_parquet(cache)
    _inject(monkeypatch)
    with pytest.raises(OSError, match="injected partial write"):
        ps.write_cache_source(cache, "yfinance", b_rep)
    assert not ps.sidecar_path(cache).exists()
    assert _strays(cache) == []
    got = _restart(cache, monkeypatch)
    assert (got["reused"], got["status"], got["freshness"]) == (
        False, "HOLD", sf.UNKNOWN)


@pytest.mark.parametrize("existing", [True, False])
def test_a_failed_replace_keeps_the_published_sidecar(tmp_path, monkeypatch,
                                                      existing):
    (a, a_rep), (_, b_rep) = _frames()
    cache = tmp_path / "thematic_prices_cache.parquet"
    if existing:
        ps.write_cache_source(cache, "yfinance", a_rep)
    before = ps.sidecar_path(cache).read_bytes() if existing else None

    def refuse(src, dst):
        raise OSError("injected replace failure")
    monkeypatch.setattr(ps.os, "replace", refuse)
    with pytest.raises(OSError, match="injected replace failure"):
        ps.write_cache_source(cache, "yfinance", b_rep)
    if existing:
        assert ps.sidecar_path(cache).read_bytes() == before
    else:
        assert not ps.sidecar_path(cache).exists()
    assert _strays(cache) == []


def test_a_persistent_permission_error_retries_then_fails_closed(tmp_path,
                                                                 monkeypatch):
    (_, a_rep), (_, b_rep) = _frames()
    cache = tmp_path / "thematic_prices_cache.parquet"
    ps.write_cache_source(cache, "yfinance", a_rep)
    before = ps.sidecar_path(cache).read_bytes()
    calls = []

    def locked(src, dst):
        calls.append(src)
        raise PermissionError("held open by a reader")
    monkeypatch.setattr(ps.os, "replace", locked)
    monkeypatch.setattr(ps.time, "sleep", lambda s: None)
    with pytest.raises(PermissionError):
        ps.write_cache_source(cache, "yfinance", b_rep)
    assert len(calls) == ps._REPLACE_ATTEMPTS
    assert ps.sidecar_path(cache).read_bytes() == before
    assert _strays(cache) == []


def test_a_transient_permission_error_is_retried(tmp_path, monkeypatch):
    (_, _), (_, b_rep) = _frames()
    cache = tmp_path / "thematic_prices_cache.parquet"
    real = ps.os.replace
    calls = []

    def flaky(src, dst):
        calls.append(src)
        if len(calls) < 3:
            raise PermissionError("held open by a reader")
        return real(src, dst)
    monkeypatch.setattr(ps.os, "replace", flaky)
    monkeypatch.setattr(ps.time, "sleep", lambda s: None)
    ps.write_cache_source(cache, "yfinance", b_rep)
    assert len(calls) == 3
    assert _published(cache)[ns.SIDECAR_KEY] == b_rep[ns.SIDECAR_KEY]
    assert _strays(cache) == []


# ---------------------------------------------------------------------------
# Overlapping complete publications
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("inner_first", [True, False])
def test_overlapping_complete_publications_leave_one_whole_record(
        tmp_path, monkeypatch, inner_first):
    """One writer completes while the other's handle is open; both complete.
    The last replace wins whole, the temporary names differ, nothing is
    left behind, and the winner is reused only beside its own frame."""
    (a, a_rep), (b, b_rep) = _frames()
    outer, inner = (b_rep, a_rep) if inner_first else (a_rep, b_rep)
    cache = tmp_path / "thematic_prices_cache.parquet"
    names = []
    real_mkstemp = ps.tempfile.mkstemp

    def recording(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        names.append(name)
        return fd, name
    monkeypatch.setattr(ps.tempfile, "mkstemp", recording)
    _inject(monkeypatch, lambda: ps.write_cache_source(cache, "yfinance", inner),
            partial=False)
    ps.write_cache_source(cache, "yfinance", outer)
    assert len(names) == 2 and len(set(names)) == 2
    assert all(Path(n).parent == tmp_path for n in names)
    rec = _published(cache)[ns.SIDECAR_KEY]
    assert rec == outer[ns.SIDECAR_KEY]                  # replaced last, whole
    assert _strays(cache) == []
    winner, loser = (b, a) if inner_first else (a, b)
    loser.to_parquet(cache)
    got = _restart(cache, monkeypatch)
    assert (got["reused"], got["status"], got["freshness"]) == (
        False, "HOLD", sf.UNKNOWN)
    winner.to_parquet(cache)
    assert _restart(cache, monkeypatch)["reused"] is True


def test_concurrent_publications_always_leave_a_whole_record(tmp_path):
    """Real threads, distinct payloads: whatever wins is one payload whole."""
    cache = tmp_path / "thematic_prices_cache.parquet"
    payloads = {f"w{i}": {"tail_heal": {"checked_at_utc": f"w{i}",
                                        "probe": "x" * (50 + 37 * i),
                                        "last_populated": "2026-04-17",
                                        "rows": list(range(i * 40)),
                                        "dropped": []}}
                for i in range(4)}
    errors = []

    def run(key):
        try:
            for _ in range(10):
                ps.write_cache_source(cache, "yfinance", payloads[key])
        except Exception as exc:  # noqa: BLE001 — reported below
            errors.append(repr(exc))
    threads = [threading.Thread(target=run, args=(k,)) for k in payloads]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    heal = _published(cache)["tail_heal"]
    assert heal in [p["tail_heal"] for p in payloads.values()]
    assert _strays(cache) == []


# ---------------------------------------------------------------------------
# No path accepts mixed native provenance
# ---------------------------------------------------------------------------

def _texts():
    (a, a_rep), (b, b_rep) = _frames()
    stamp = {"source": "yfinance", "written_at_utc": "2026-04-18T03:00:00+00:00"}
    return (a, json.dumps({**stamp, **a_rep}, indent=2)), \
           (b, json.dumps({**stamp, **b_rep}, indent=2))


def test_every_torn_record_is_refused_unless_it_is_one_whole_record():
    """Every splice of one writer's prefix onto the other's suffix (the shape
    an in-place overlap leaves) that is still valid JSON: refused beside
    either frame unless it IS one whole record beside its own frame.
    Fails before the fix: B's digest with A's dates was accepted for B."""
    (a, ta), (b, tb) = _texts()
    # Equal up to "lines"; A carries one more date, so the tails differ.
    assert ta.index('"lines"') == tb.index('"lines"')
    whole ={json.loads(ta)[ns.SIDECAR_KEY]["record_sha256"]: "A",
             json.loads(tb)[ns.SIDECAR_KEY]["record_sha256"]: "B"}
    valid = accepted = 0
    for first, second in ((tb, ta), (ta, tb)):
        for cut in range(1, len(first)):
            text = first[:cut] + second[cut:]
            if text in (ta, tb):
                continue
            try:
                blob = json.loads(text)
            except json.JSONDecodeError:
                continue
            valid += 1
            for label, frame in (("A", a), ("B", b)):
                got = ns.parse_sidecar(blob, frame, [CN])
                if got is None:
                    continue
                accepted += 1
                rec = blob[ns.SIDECAR_KEY]
                # Only a splice that changed no record byte, beside its own frame.
                assert whole.get(rec.get("record_sha256")) == label
                assert rec == json.loads(ta if label == "A" else tb)[ns.SIDECAR_KEY]
    assert valid > 0                                     # the shape exists


def test_the_mixed_record_is_refused_on_every_reader(tmp_path, monkeypatch):
    """B's digest with A's dates, written as the overlap left it: the cache-hit
    check refreshes, the rank HOLDs, the freshness row is unknown."""
    (a, ta), (b, tb) = _texts()
    cut = tb.index('"lines"')
    mixed = tb[:cut] + ta[cut:]
    rec = json.loads(mixed)[ns.SIDECAR_KEY]
    assert rec["content_sha256"] == json.loads(tb)[ns.SIDECAR_KEY]["content_sha256"]
    assert "2026-04-03" in rec["lines"][CN]["dates"]
    cache = tmp_path / "thematic_prices_cache.parquet"
    b.to_parquet(cache)
    ps.sidecar_path(cache).write_text(mixed, encoding="utf-8")
    assert _restart(cache, monkeypatch) == {"reused": False, "status": "HOLD",
                                            "weights": {}, "freshness": sf.UNKNOWN}


@pytest.mark.parametrize("field,value", [
    ("through", "2026-04-16"),
    ("window_days", 401),
    ("content_sha256", "0" * 64),
    ("lines", "dates"),
    ("record_sha256", None),
    ("record_sha256", "0" * 64),
    ("record_sha256", 7),
])
def test_record_sha256_binds_the_fields_the_reader_uses(field, value):
    (_, _), (b, b_rep) = _frames()
    side = json.loads(json.dumps(b_rep))
    assert ns.parse_sidecar(side, b, [CN]) is not None
    rec = side[ns.SIDECAR_KEY]
    if field == "lines":
        # A's dates under B's names: content_sha256 still matches B's frame.
        rec["lines"][CN][value] = rec["lines"][CN][value] + ["2026-04-03"]
        assert rec["content_sha256"] == ns.content_digest(b, rec["lines"])
    elif value is None:
        del rec[field]
    else:
        rec[field] = value
    assert ns.parse_sidecar(side, b, [CN]) is None


def test_a_non_finite_value_in_the_record_is_no_record():
    (_, _), (b, b_rep) = _frames()
    side = json.loads(json.dumps(b_rep))
    side[ns.SIDECAR_KEY]["lines"][CN]["last"] = float("nan")
    assert ns.parse_sidecar(side, b, [CN]) is None


def test_a_us_column_repair_keeps_a_matching_record(tmp_path, monkeypatch):
    """Native-column-plus-index binding is kept: repair_price_gaps rewrites
    the parquet without the sidecar, and a US-only change keeps the record."""
    (a, a_rep), _ = _frames()
    cache = tmp_path / "thematic_prices_cache.parquet"
    a.to_parquet(cache)
    ps.write_cache_source(cache, "yfinance", a_rep)
    us = next(t for t in th.TICKERS if t != CN)
    repaired = a.copy()
    repaired.iloc[-5, repaired.columns.get_loc(us)] = 1.01
    repaired.to_parquet(cache)
    got = _restart(cache, monkeypatch)
    assert (got["reused"], got["status"]) == (True, "READY")


def test_the_cache_write_path_still_publishes_through_write_cache_source():
    """The engine's only sidecar write goes through the atomic publisher."""
    src = (Path(th.__file__)).read_text(encoding="utf-8")
    assert src.count("write_cache_source(PRICE_CACHE") == 1
    ps_src = Path(ps.__file__).read_text(encoding="utf-8")
    body = ps_src[ps_src.index("def write_cache_source"):
                  ps_src.index("def publish_text")]
    assert "write_text(" not in body and "publish_text(path" in body
