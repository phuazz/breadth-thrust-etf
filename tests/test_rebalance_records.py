"""The last rebalance is not the last trade, and the surfaces must not confuse them.

REPRODUCES 2026-09-03. Sleeve C is equal-weighted, so a rebalance that holds
the same names writes no trade_history entry, and every surface that labelled
``trade_history[-1]`` "the rebalance" printed a week-old date with week-old
signals beside it. ``latest_rebalance_record`` is the record those surfaces
now read; these tests hold it to the trade record's conventions and pin the
surfaces to it.

Python datetime months are 1-indexed (January = 1).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from rebalance_records import latest_rebalance_record  # noqa: E402
from run_thematic_rotation import build_trade_history  # noqa: E402


def _panel():
    """Daily weights over three Monday rebalances; the third holds the second's
    book unchanged. Equal-weight, as sleeve C is."""
    idx = pd.bdate_range("2026-08-03", "2026-09-01")      # Mon 3 Aug .. Tue 1 Sep
    cols = ["AAA", "BBB", "CCC", "SHY"]
    w = pd.DataFrame(0.0, index=idx, columns=cols)
    rebals = [pd.Timestamp("2026-08-17"), pd.Timestamp("2026-08-24"),
              pd.Timestamp("2026-08-31")]
    books = {rebals[0]: {"AAA": 0.5, "BBB": 0.5},
             rebals[1]: {"AAA": 0.5, "CCC": 0.5},
             rebals[2]: {"AAA": 0.5, "CCC": 0.5}}           # held, no trade
    current = {}
    for d in idx:
        if d in books:
            current = books[d]
        for etf, x in current.items():
            w.loc[d, etf] = x
    sig = pd.DataFrame(np.linspace(0.05, 0.30, len(idx))[:, None] * np.arange(1, 5),
                       index=idx, columns=cols)
    return w, sig, rebals


def test_last_rebalance_is_reported_even_when_it_did_not_trade():
    w, sig, rebals = _panel()
    trades = build_trade_history(w, sig, rebals[0])
    latest = latest_rebalance_record(w, sig, rebals, "signal_pct", rebals[0])
    assert trades[-1]["date"] == "2026-08-24", "the trade record changed shape"
    assert latest["date"] == "2026-08-31"
    assert latest["decision_date"] == "2026-08-28", \
        "decision session must be the index entry before the rebalance date"
    assert [h["etf"] for h in latest["holdings"]] == ["AAA", "CCC"]


def test_record_matches_the_trade_record_on_a_week_that_traded():
    """Same conventions: on a traded week the two records are identical."""
    w, sig, rebals = _panel()
    trades = build_trade_history(w, sig, rebals[0])
    latest = latest_rebalance_record(w, sig, rebals[:2], "signal_pct", rebals[0])
    assert latest == trades[-1]


def test_signal_is_the_decision_session_value_not_the_rebalance_day():
    w, sig, rebals = _panel()
    latest = latest_rebalance_record(w, sig, rebals, "signal_pct", rebals[0])
    decision = pd.Timestamp("2026-08-28")
    for h in latest["holdings"]:
        assert h["signal_pct"] == round(float(sig.loc[decision, h["etf"]]) * 100, 1)


def test_all_cash_rebalance_is_a_record_with_no_holdings():
    w, sig, rebals = _panel()
    w.loc["2026-08-31":, :] = 0.0
    latest = latest_rebalance_record(w, sig, rebals, "signal_pct", rebals[0])
    assert latest["date"] == "2026-08-31"
    assert latest["holdings"] == []


def test_no_rebalance_in_window_is_none():
    w, sig, rebals = _panel()
    assert latest_rebalance_record(w, sig, rebals, "signal_pct",
                                   pd.Timestamp("2026-12-01")) is None
    assert latest_rebalance_record(w, sig, [], "signal_pct") is None


# ---------------------------------------------------------------------------
# The surfaces. Pinned as prose, the way the WS18 stale surfaces were: the
# defect was a label, and a label regresses silently.
# ---------------------------------------------------------------------------
TEMPLATE = (ROOT / "template.html").read_text(encoding="utf-8")


def _fn(name: str) -> str:
    m = re.search(rf"function {name}\(\) \{{.*?\n\}}\n", TEMPLATE, re.S)
    assert m, f"{name} not found in template.html"
    return m.group(0)


def test_every_current_selection_block_reads_the_latest_rebalance():
    for fn in ("_renderAHoldings", "_renderBHoldings", "_renderCHoldings",
               "_renderDHoldings"):
        body = _fn(fn)
        assert "latest_rebalance" in body, f"{fn} still labels the last trade as the rebalance"
        assert "unchanged since" in body, f"{fn} does not disclose a held week"


def test_hero_line_reads_the_latest_rebalance():
    body = _fn("renderLivePositioning") if "function renderLivePositioning()" in TEMPLATE \
        else TEMPLATE[TEMPLATE.index("// ===== Latest rebalances ====="):][:3000]
    assert "latest_rebalance" in body


def test_trade_tables_call_their_rows_trades_not_rebalances():
    """The per-sleeve history tables list trade_history rows."""
    assert "${history.length} rebalances" not in TEMPLATE
