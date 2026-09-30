"""A ticker the batch download drops is asked again alone before it is logged
as unserved.

Every null last_bar in the vendor log up to 2026-09-30 was one ticker missing
from the batch response while its venue peers were served, back on the next
probe. The tripwire reads a null after a recorded bar as a total withdrawal and
mails it, so the probe must not write a null for a line the vendor still
serves. A line that stays empty on the re-fetch is still written as null.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import probe_vendor_availability as probe  # noqa: E402

# Python datetime months are 1-indexed. Wednesday 2026-09-30 03:45 UTC, the
# firing that mailed the SIE.DE retraction.
NOW = datetime(2026, 9, 30, 3, 45, tzinfo=timezone.utc)
DATES = pd.to_datetime(["2026-09-25", "2026-09-28", "2026-09-29"])


def _frame(tickers, empty=()):
    cols = pd.MultiIndex.from_product([["Close"], tickers])
    data = np.array([[np.nan if t in empty else 100.0 for t in tickers]
                     for _ in DATES])
    return pd.DataFrame(data, index=DATES, columns=cols)


class FakeDownload:
    """Batch call drops ``batch_empty``; single calls drop ``always_empty``."""

    def __init__(self, batch_empty=(), always_empty=()):
        self.batch_empty = set(batch_empty)
        self.always_empty = set(always_empty)
        self.single_calls: list[str] = []

    def __call__(self, tickers, **_kw):
        if len(tickers) == 1:
            self.single_calls.append(tickers[0])
            return _frame(tickers, empty=self.always_empty)
        return _frame(tickers, empty=self.batch_empty | self.always_empty)


def _rows(result):
    return {r["ticker"]: r for r in result["rows"]}


def test_a_line_dropped_by_the_batch_is_recovered_and_marked(monkeypatch):
    monkeypatch.setattr(probe, "REFETCH_PAUSE_SECONDS", 0.0)
    dl = FakeDownload(batch_empty={"SIE.DE"})
    rows = _rows(probe.probe(now_utc=NOW, download=dl))
    assert rows["SIE.DE"]["last_bar"] == "2026-09-29"
    assert rows["SIE.DE"]["refetched"] is True
    assert dl.single_calls == ["SIE.DE"]
    # The ordinary path carries no flag, so the log format is unchanged there.
    assert "refetched" not in rows["SAP.DE"]
    assert rows["SAP.DE"]["last_bar"] == "2026-09-29"


def test_a_line_empty_on_every_attempt_is_still_written_as_null(monkeypatch):
    monkeypatch.setattr(probe, "REFETCH_PAUSE_SECONDS", 0.0)
    dl = FakeDownload(always_empty={"SIE.DE"})
    rows = _rows(probe.probe(now_utc=NOW, download=dl))
    assert rows["SIE.DE"]["last_bar"] is None
    assert rows["SIE.DE"]["sessions_behind"] is None
    assert "refetched" not in rows["SIE.DE"]
    assert dl.single_calls == ["SIE.DE"] * probe.REFETCH_ATTEMPTS


def test_a_whole_venue_outage_still_reaches_the_tripwire(monkeypatch):
    monkeypatch.setattr(probe, "REFETCH_PAUSE_SECONDS", 0.0)
    xetr = {t for t, venue, _ in probe.PROBES if venue == "XETR"}
    rows = _rows(probe.probe(now_utc=NOW,
                             download=FakeDownload(always_empty=xetr)))
    assert all(rows[t]["last_bar"] is None for t in xetr)
    assert rows["SPY"]["last_bar"] == "2026-09-29"


def test_a_raising_refetch_counts_as_unserved():
    def boom(*_a, **_kw):
        raise RuntimeError("vendor down")
    assert probe._refetch_single(boom, "SIE.DE", "2026-09-10", "2026-10-02",
                                 attempts=2, pause=0.0,
                                 sleep=lambda _s: None) is None
