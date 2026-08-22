"""The reader's guide on the scanner page must agree with the engine.

The guide added to ``scanner_template.html`` states, in plain English, what
every column measures and which thresholds fire an alert. Prose cannot be
computed from the constants, so it is the one part of the page that can go
quietly wrong: someone changes ``ALERT_VOLUME_MULTIPLE`` and the page keeps
telling readers the old number, with nothing failing.

These tests pin each quoted figure to the constant it describes. They are
deliberately literal — the assertion message names the phrase to update — and
they cover only figures, never wording, so the copy stays editable.

The frozen-parameter rule (spec §8) means these constants should rarely move
at all; when one does, the guide moves with it in the same commit.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import run_scanner as rs  # noqa: E402
import scanner_indicators as si  # noqa: E402

TEMPLATE = ROOT / "scanner_template.html"


@pytest.fixture(scope="module")
def page() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def prose(page: str) -> str:
    """The page with runs of whitespace collapsed.

    Guide copy is wrapped for readability in the source file, so a phrase can
    straddle a newline and four spaces of indent. Matching against the raw
    text would fail on re-wrapping rather than on a changed figure, which is
    the opposite of what these tests are for.
    """
    return re.sub(r"\s+", " ", page)


def test_guide_block_present(page: str) -> None:
    """The explainer is not optional furniture — the page ships with it."""
    assert '<details class="guide">' in page
    assert "How to read this page" in page


@pytest.mark.parametrize(
    "phrase, source",
    [
        # Windows and lookbacks
        (f"{si.MA_MID}- and {si.MA_LONG}-day averages", "MA_MID / MA_LONG"),
        (f"within {si.FLAT_MA_TOLERANCE * 100:.0f}% of", "FLAT_MA_TOLERANCE"),
        (f"over the past {si.SLOPE_LOOKBACK} sessions", "SLOPE_LOOKBACK"),
        (f"{si.RV_WINDOW} days of daily returns", "RV_WINDOW"),
        (f"{si.BBW_WINDOW}-day price band", "BBW_WINDOW"),
        (f"over {si.ATR_PERIOD} days as a percentage of price", "ATR_PERIOD"),
        (f"its own {si.VOL_RATIO_WINDOW}-day average", "VOL_RATIO_WINDOW"),
        # Alert thresholds
        (f"above {rs.ALERT_VOLUME_MULTIPLE:.0f}×", "ALERT_VOLUME_MULTIPLE"),
        (f"beyond {rs.ALERT_SIGMA_MOVE:.0f} standard deviations", "ALERT_SIGMA_MOVE"),
        # The sigma yardstick reuses RV_WINDOW and stops at the previous
        # session (run_scanner: daily.iloc[-RV_WINDOW - 1:-1]).
        (f"the {si.RV_WINDOW} daily returns before it", "sigma window"),
        (f"{rs.ALERT_RSI_HIGH:.0f} / {rs.ALERT_RSI_LOW:.0f}", "ALERT_RSI_HIGH / LOW"),
    ],
)
def test_guide_quotes_the_engine_constant(prose: str, phrase: str, source: str) -> None:
    assert phrase in prose, (
        f"the guide no longer states {source} as {phrase!r} — update the prose "
        f"in scanner_template.html and this test together"
    )


def test_rank_horizons_match(prose: str) -> None:
    """The guide names the four momentum windows in months."""
    months = [h // 21 for h in si.RANK_HORIZONS]
    phrase = ", ".join(str(m) for m in months[:-1]) + f" and {months[-1]} months"
    assert phrase in prose, f"guide should describe the horizons as {phrase!r}"


@pytest.mark.parametrize(
    "cls, value, source",
    [
        ("g-pctl", si.PCTL_WINDOW, "PCTL_WINDOW"),
        ("g-pctl-y", si.PCTL_WINDOW // si.TRADING_DAYS_YEAR, "PCTL_WINDOW in years"),
        ("g-bench", rs.BENCHMARK, "BENCHMARK"),
        ("g-stale", rs.STALE_TRADING_DAYS, "STALE_TRADING_DAYS"),
    ],
)
def test_guide_fallback_text_matches(prose: str, cls: str, value, source: str) -> None:
    """The spans are filled from the payload; their fallback text must agree.

    A data-less load (the template served standalone) shows the markup text,
    so a stale fallback is a wrong number on a real page, not dead code.
    """
    assert f'class="{cls}">{value}<' in prose, (
        f"fallback text for .{cls} does not match {source} ({value})"
    )


def test_cell_highlight_thresholds_match_the_guide(prose: str) -> None:
    """The amber-cell rules in JS and the sentence describing them agree."""
    rv = re.search(r"row\.rv_pctl\s*>\s*(\d+)", prose)
    vol = re.search(r"row\.vol_ratio\s*>\s*(\d+)", prose)
    assert rv and vol, "could not find the cellClass highlight thresholds"
    assert f"volatility above p{rv.group(1)}" in prose
    assert int(vol.group(1)) == int(rs.ALERT_VOLUME_MULTIPLE), (
        "the volume highlight and the volume alert must use one threshold"
    )


def test_every_measured_column_carries_a_header_definition(page: str) -> None:
    """A new column cannot ship without its one-line hover definition.

    Identification columns are exempt: the ticker and name cells carry their
    own tooltips (engine tickers, as-of date, full fund name).
    """
    block = re.search(r"const COLS = \[(.*?)\n\];", page, re.S)
    assert block, "COLS array not found"
    entries = re.findall(r"\{ k:'([a-z0-9_]+)'(.*?)\n?\s*\},", block.group(1) + "\n },", re.S)
    assert len(entries) >= 14, f"parsed only {len(entries)} columns"
    undocumented = [
        k for k, body in entries
        if k not in {"ticker", "name"} and "t:" not in body
    ]
    assert not undocumented, f"columns without a header definition: {undocumented}"
