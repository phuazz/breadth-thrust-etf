"""The partial batch placeholder (2026-09-17) — the production G1 blocker.

``verify_price_tail`` DID run on the hollow tails. It could not remove them.
The ``served or roster_priced > 0`` branch could only end "healed" or
"partial", so a row carrying a handful of residual batch prices was retained
for ever. On 2026-09-15 EXH3 held 1 roster price of 107: the five sampled
names carried no bar for the date, the other 106 were then re-requested,
every one of them ANSWERED, not one carried the date, and the row stayed. G1
refused publication on that evidence — correctly — and went on refusing it on
every firing, because nothing in the loop could ever drop the row. EXV1 and
EXH9 failed identically, and the 14 September fill went unpublished.

The row is dropped on the COMPLETE answer alone — every live name asked, every
one answering, none carrying the date, nothing filled, refused, unanswered or
timed out — and every weaker answer keeps it, because a weaker answer is not
evidence of anything.

WHAT THE CONDITION ESTABLISHES. Full VENDOR NON-SERVICE for that row, and
nothing else. It is not evidence that the exchange was shut: the venue
calendar is the only thing that can answer that and it is not consulted here.
What is dropped is a row that is not usable as a vendor-priced row, carrying
residuals the batch left behind. Nothing in this module may be read as a claim
about whether the market traded.

Nothing here weakens G1, moves a coverage floor, forward-fills or changes
construction: a dropped row is simply not in the frame, which is the clean
vendor-lag shape every reader already handles.

All vendor calls are stubbed; nothing hits the network. Python datetime
months are 1-indexed (January = 1); pandas builds the business-day indexes,
never a hand-computed offset.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import compute_breadth as cb  # noqa: E402
import price_source as ps  # noqa: E402

# Priced floor for 20 names is max(5, int(0.9*20)+1) = 19, so a row one name
# short of the roster is still "not populated" — the production shape.
WIDE = [f"P{i:02d}" for i in range(20)]
END = "2026-09-04"          # a Friday; the row the vendor withheld
LAST_OK = "2026-09-03"


def _residual(n_priced: int = 1, roster=WIDE, n_days: int = 6):
    """A hollow tail row that retains ``n_priced`` residual batch prices.

    Those residuals are exactly what stopped the old code dropping the row.
    """
    idx = pd.bdate_range(end=END, periods=n_days)
    data = {}
    for i, t in enumerate(roster):
        col = pd.Series(100.0 + i + np.arange(n_days, dtype=float), index=idx)
        col.iloc[-1] = np.nan
        data[t] = col
    f = pd.DataFrame(data)
    for t in roster[:n_priced]:
        f.loc[f.index[-1], t] = f[t].iloc[-2]
    return f


def _serving(frame_like: pd.DataFrame, through: str | None = None):
    """Single-ticker stub: the column's own history carried forward, up to
    ``through`` inclusive. None serves every date, i.e. the tail row too."""
    def fetch(t):
        s = frame_like[t].ffill().copy()
        if through is not None:
            s = s.loc[:pd.Timestamp(through)]
        return s
    return fetch


# ---------------------------------------------------------------------------
# The complete answer: dropped
# ---------------------------------------------------------------------------
def test_residual_price_placeholder_is_dropped_when_every_name_answers():
    f = _residual()
    out, v = cb.verify_price_tail(f, WIDE, fetch_single=_serving(f, LAST_OK))
    r = v["rows"][0]
    assert r["verdict"] == "unserved_placeholder", r["verdict"]
    assert r["roster_priced"] == 1, "the fixture is not the production shape"
    assert r["filled"] == 0
    assert r["refused"] == [] and r["no_answer"] == []
    assert r["heal_timed_out"] is False and r["not_attempted"] == []
    assert len(r["unserved"]) == len(r["requested"]) == 19
    # The reason states VENDOR NON-SERVICE, never that the exchange was shut.
    assert "vendor is not serving" in r["reason"]
    assert "not usable as a vendor-priced row" in r["reason"]
    assert "says nothing about whether the exchange traded" in r["reason"]
    assert v["dropped"] == [END]
    assert out.index.max() == pd.Timestamp(LAST_OK), "the placeholder survived"
    assert f.index.max() == pd.Timestamp(END), "the caller's frame was mutated"


def test_the_dropped_placeholder_leaves_a_clean_priced_tail():
    """What G1 then reads: the frame ends on the last session the roster is
    actually priced on. No forward-fill, no synthesised bar, one row fewer."""
    f = _residual()
    out, _ = cb.verify_price_tail(f, WIDE, fetch_single=_serving(f, LAST_OK))
    assert out.index.max() == pd.Timestamp(LAST_OK)
    assert out.loc[pd.Timestamp(LAST_OK), WIDE].notna().all()
    assert len(out) == len(f) - 1


def test_the_production_ratio_is_dropped():
    """EXH3's actual shape: 1 priced of a large roster, the rest answering
    that no such bar exists."""
    roster = [f"Q{i:03d}" for i in range(40)]      # floor 37
    f = _residual(n_priced=1, roster=roster)
    out, v = cb.verify_price_tail(f, roster, fetch_single=_serving(f, LAST_OK))
    assert v["rows"][0]["verdict"] == "unserved_placeholder"
    assert v["rows"][0]["roster_priced"] == 1
    assert out.index.max() == pd.Timestamp(LAST_OK)


# ---------------------------------------------------------------------------
# Every weaker answer: kept
# ---------------------------------------------------------------------------
def test_a_name_that_does_not_answer_keeps_the_row():
    """No answer is no verdict. The row stays and the guard fails closed."""
    f = _residual()
    serving = _serving(f, LAST_OK)

    def fetch(t):
        if t == WIDE[5]:
            raise RuntimeError("vendor down")
        return serving(t)

    out, v = cb.verify_price_tail(f, WIDE, fetch_single=fetch)
    r = v["rows"][0]
    assert r["verdict"] == "partial"
    assert r["no_answer"] == [WIDE[5]]
    assert v["dropped"] == []
    assert out.index.max() == pd.Timestamp(END), "an unanswered row was dropped"


def test_a_timed_out_heal_keeps_the_row():
    """A budget that ran out means the roster was never asked."""
    f = _residual()
    ticks = iter([0.0] + [99.0] * 500)   # started, then every check is over
    out, v = cb.verify_price_tail(
        f, WIDE, fetch_single=_serving(f, LAST_OK),
        heal_budget_s=1.0, clock=lambda: next(ticks))
    r = v["rows"][0]
    assert r["heal_timed_out"] is True
    assert r["verdict"] == "partial"
    assert r["not_attempted"], "the fixture did not actually time out"
    assert v["dropped"] == []
    assert out.index.max() == pd.Timestamp(END), "a timed-out row was dropped"


def test_a_refused_bar_keeps_the_row(monkeypatch):
    """A refusal means a bar EXISTS and the WS15 guard rejected it. That is a
    session the vendor is serving, so the row is not a placeholder."""
    f = _residual()
    serving = _serving(f, LAST_OK)
    monkeypatch.setattr(cb, "_vendor_step_defect",
                        lambda fresh, ticker: "split unapplied (stub)"
                        if ticker == WIDE[7] else None)

    def fetch(t):
        if t == WIDE[7]:
            s = f[t].ffill().copy()
            s.iloc[-1] = s.iloc[-2] * 8.0      # a split-sized step
            return s
        return serving(t)

    out, v = cb.verify_price_tail(f, WIDE, fetch_single=fetch)
    r = v["rows"][0]
    assert r["refused"] == [WIDE[7]], r["refused"]
    assert r["verdict"] == "partial"
    assert v["dropped"] == []
    assert out.index.max() == pd.Timestamp(END), "a refused row was dropped"
    assert pd.isna(out.loc[pd.Timestamp(END), WIDE[7]]), "a refused bar was kept"


def test_a_genuine_partial_fill_keeps_the_row():
    """One real recovered value means the session is real, even though the
    row never reaches the coverage floor."""
    f = _residual()
    short, full = _serving(f, LAST_OK), _serving(f)

    def fetch(t):
        return full(t) if t == WIDE[3] else short(t)

    out, v = cb.verify_price_tail(f, WIDE, fetch_single=fetch)
    r = v["rows"][0]
    assert r["filled"] == 1 and r["verdict"] == "partial"
    assert v["dropped"] == []
    assert out.index.max() == pd.Timestamp(END)
    assert pd.notna(out.loc[pd.Timestamp(END), WIDE[3]])


def test_a_fully_healed_row_is_never_dropped():
    """The drop must not reach a row the heal actually repaired."""
    f = _residual()
    out, v = cb.verify_price_tail(f, WIDE, fetch_single=_serving(f))
    assert v["rows"][0]["verdict"] == "healed"
    assert v["dropped"] == []
    assert out.loc[pd.Timestamp(END), WIDE].notna().all()


# ---------------------------------------------------------------------------
# Cache-hit provenance (2026-09-17)
# ---------------------------------------------------------------------------
def _cached(tmp_path, name="prices_cache_test.parquet"):
    cache = tmp_path / name
    idx = pd.bdate_range(end=END, periods=40)
    pd.DataFrame({t: np.linspace(10.0, 20.0, len(idx)) for t in WIDE},
                 index=idx).to_parquet(cache)
    return cache


def test_a_cache_hit_restores_the_recorded_tail_verification(tmp_path):
    """A cache hit returns before verify_price_tail, so the frame carried no
    tail_verification and the panel JSON written from it had none — erasing
    the record left by the run that DID settle the tail. That is why all
    three blocked panels showed an empty tail_verification while G1 was
    reporting a hollow tail against them: the one artefact an operator is
    told to read had been overwritten by a later cached run."""
    cache = _cached(tmp_path)
    record = {"checked_at_utc": "2026-09-16T01:00:00+00:00", "probe": "stub",
              "last_populated": LAST_OK,
              "rows": [{"date": END, "verdict": "unserved_placeholder"}],
              "dropped": [END]}
    ps.write_cache_source(cache, "yfinance", {"tail_heal": record})

    out = cb.download_prices(WIDE, "2026-08-03", END, cache_path=cache,
                             roster=None, tail_probe=False)
    got = out.attrs.get("tail_verification")
    assert got is not None, "the provenance was erased by a cached run"
    assert got["rows"] == record["rows"]
    assert got["dropped"] == record["dropped"]
    # NOT passed off as a fresh observation: the probe's own timestamp is
    # unchanged and the re-read is stamped separately.
    assert got["checked_at_utc"] == record["checked_at_utc"]
    assert got["from_cache"] is True and "reread_at_utc" in got


def test_a_cache_hit_without_usable_provenance_leaves_the_attribute_absent(
        tmp_path):
    """Missing or malformed provenance behaves exactly as it did before."""
    cache = _cached(tmp_path, "prices_cache_none.parquet")
    ps.write_cache_source(cache, "yfinance", {"tail_heal": "not a dict"})
    out = cb.download_prices(WIDE, "2026-08-03", END, cache_path=cache,
                             roster=None, tail_probe=False)
    assert "tail_verification" not in out.attrs
    assert ps.read_cache_tail_heal(tmp_path / "absent.parquet") is None


def test_read_cache_tail_heal_never_raises(tmp_path):
    cache = _cached(tmp_path, "prices_cache_bad.parquet")
    ps.sidecar_path(cache).write_text("{not json", encoding="utf-8")
    assert ps.read_cache_tail_heal(cache) is None


# ---------------------------------------------------------------------------
# The provenance reader checks the SHAPE, not merely the type (2026-09-17)
#
# It accepted any dict, which tests nothing: the sidecar is a file on disk
# that a half-finished write, a hand edit or a schema change can leave in any
# state, and what it returns is published as provenance in the panel JSON. A
# partial record read as evidence is worse than no record.
# ---------------------------------------------------------------------------
_GOOD = {"checked_at_utc": "2026-09-16T01:00:00+00:00", "probe": "stub",
         "last_populated": LAST_OK, "rows": [], "dropped": []}


def _with(tmp_path, heal, name):
    cache = _cached(tmp_path, name)
    ps.write_cache_source(cache, "yfinance", {"tail_heal": heal})
    return cache


def test_a_complete_record_is_accepted(tmp_path):
    assert ps.read_cache_tail_heal(
        _with(tmp_path, dict(_GOOD), "c_ok.parquet")) == _GOOD


@pytest.mark.parametrize("missing", sorted(_GOOD))
def test_a_record_missing_any_required_field_is_absent(tmp_path, missing):
    heal = {k: v for k, v in _GOOD.items() if k != missing}
    assert ps.read_cache_tail_heal(
        _with(tmp_path, heal, f"c_no_{missing}.parquet")) is None


@pytest.mark.parametrize("field,bad", [
    ("checked_at_utc", 1758000000),     # an epoch instead of a stamp
    ("probe", None),
    ("last_populated", ["2026-09-03"]),
    ("rows", {"date": END}),            # a dict where a list belongs
    ("dropped", END),                   # a bare string, not a list
])
def test_a_record_with_a_wrong_type_is_absent(tmp_path, field, bad):
    heal = dict(_GOOD, **{field: bad})
    assert ps.read_cache_tail_heal(
        _with(tmp_path, heal, f"c_bad_{field}.parquet")) is None


def test_an_empty_record_is_absent(tmp_path):
    assert ps.read_cache_tail_heal(
        _with(tmp_path, {}, "c_empty.parquet")) is None


def test_a_partial_record_never_reaches_the_frame(tmp_path):
    """End to end: a half-written sidecar must leave the attribute absent,
    not publish a fragment as provenance."""
    cache = _with(tmp_path, {"probe": "stub", "rows": []}, "c_part.parquet")
    out = cb.download_prices(WIDE, "2026-08-03", END, cache_path=cache,
                             roster=None, tail_probe=False)
    assert "tail_verification" not in out.attrs


# ---------------------------------------------------------------------------
# Recovery, and per-date evaluation (2026-09-17)
# ---------------------------------------------------------------------------
def test_a_dropped_row_returns_whole_once_the_vendor_serves_it():
    """The round trip the drop depends on. Dropping discards the residual
    batch prices, so the row must come back COMPLETE on a later run once the
    vendor serves the session — restored from the vendor, never rebuilt from
    the residuals and never forward-filled."""
    f = _residual()
    expected = pd.DatetimeIndex([pd.Timestamp(END)])

    # Run one: the vendor serves nothing for END. The row goes.
    dropped_frame, v1 = cb.verify_price_tail(
        f, WIDE, fetch_single=_serving(f, LAST_OK), expected_sessions=expected)
    assert v1["rows"][0]["verdict"] == "unserved_placeholder"
    assert v1["dropped"] == [END]
    assert pd.Timestamp(END) not in dropped_frame.index

    # Run two: the same shortened frame, and now the vendor has the session.
    # expected_sessions is what puts the date back in play — without it the
    # row is simply gone and nothing would ask about it again.
    #
    # The stub serves a close DISTINCT from the previous session (a small
    # step, so the WS15 split guard is not engaged), so a value carried
    # across the gap is distinguishable from one the vendor supplied.
    def restored(t):
        s = f[t].copy()
        s.loc[pd.Timestamp(END)] = s.loc[pd.Timestamp(LAST_OK)] + 1.0
        return s

    healed, v2 = cb.verify_price_tail(
        dropped_frame, WIDE, fetch_single=restored, expected_sessions=expected)
    r = v2["rows"][0]
    assert r["verdict"] == "healed", r["verdict"]
    assert v2["dropped"] == []
    assert healed.loc[pd.Timestamp(END), WIDE].notna().all(), \
        "the row did not come back whole"
    # Every name the vendor supplied carries the vendor's value, one step on
    # from the previous session — not the previous session carried across.
    #
    # ALL TWENTY, including the name that held the residual: the residual was
    # discarded with the dropped row, so nothing of the placeholder survives
    # into the recovered one. That is the round trip the drop depends on.
    filled = r["requested"]
    assert len(filled) == 20 and r["filled"] == 20
    assert (healed.loc[pd.Timestamp(END), filled]
            == healed.loc[pd.Timestamp(LAST_OK), filled] + 1.0).all(), \
        "the recovered row was not taken from the vendor"


def test_a_cached_answer_is_evaluated_separately_for_each_tail_date():
    """Single-ticker answers are cached per NAME across tail rows, so one
    request serves every date in the tail. Each row must still be judged on
    whether THAT date is in the answer: a name carrying the first tail date
    but not the second must fill the first and count unserved on the second,
    not be read as having answered for both."""
    idx = pd.bdate_range(end="2026-09-04", periods=7)   # ... 09-02, 03, 04
    mid, last = idx[-2], idx[-1]
    data = {}
    for i, t in enumerate(WIDE):
        col = pd.Series(100.0 + i + np.arange(len(idx), dtype=float), index=idx)
        col.iloc[-2:] = np.nan                          # a TWO-row hollow tail
        data[t] = col
    f = pd.DataFrame(data)
    f.loc[last, WIDE[0]] = 500.0                        # a residual on row two

    calls: list[str] = []

    def fetch(t):
        calls.append(t)
        s = f[t].ffill().copy()
        return s.loc[:mid]          # carries the FIRST tail date, not the second

    out, v = cb.verify_price_tail(f, WIDE, fetch_single=fetch)
    assert [r["date"] for r in v["rows"]] == [str(mid.date()), str(last.date())]
    first, second = v["rows"]
    # Row one: the answer carries that date, so it heals.
    assert first["verdict"] == "healed" and first["filled"] == 20
    # Row two: the SAME cached answers, judged against a date they do not
    # carry, so every one of them is unserved and the row is dropped.
    assert second["verdict"] == "unserved_placeholder", second["verdict"]
    assert second["filled"] == 0
    assert len(second["unserved"]) == len(second["requested"]) == 19
    assert v["dropped"] == [str(last.date())]
    assert out.index.max() == mid
    assert out.loc[mid, WIDE].notna().all()
    # One request per name for the whole tail: the cache is doing its job.
    assert len(calls) == len(set(calls)), f"names re-requested: {calls}"
