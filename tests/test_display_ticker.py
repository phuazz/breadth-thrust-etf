"""Every reader-facing surface must print the traded ticker, not the panel key.

The registry key is not always a tradeable symbol. ``EXH3`` is the id of a
constituent and breadth panel on disk; the fund those constituents belong to
trades as ``EXH4.DE``, and ``EXH3.DE`` is a different fund in a different
sector. Printing the key names something the book does not own.

``check_pair_integrity.py`` guards the pricing half of this defect — that the
series a sleeve trades moves with the basket it signals on. These tests guard
the reader-facing half, which is a separate failure: prices can be perfectly
correct while the label above them is wrong.

Four surfaces print holdings, and the defect class is one surface deriving the
answer itself rather than reading the registry. So the rule lives in
``etf_registry.display_ticker`` and every surface is asserted to route through
it: the dashboard (via the injected map), the factsheet PDF, the weekly email,
and the published portfolio page.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build_simple_page as bsp  # noqa: E402
from etf_registry import (  # noqa: E402
    ETF_REGISTRY,
    display_ticker,
    display_ticker_map,
)

TEMPLATE = ROOT / "template.html"
SIMPLE_TEMPLATE = ROOT / "simple_template.html"


# --------------------------------------------------------------------------
# The rule itself
# --------------------------------------------------------------------------

def test_exh3_prints_as_exh4():
    """The specific mislabel this repo has form on."""
    assert display_ticker("EXH3") == "EXH4"


def test_every_xetr_member_prints_its_traded_ticker():
    for key, cfg in ETF_REGISTRY.items():
        if cfg.get("trading_calendar") != "XETR":
            continue
        proxy = cfg.get("yfinance_trading_proxy")
        assert proxy, f"{key} is a XETR member with no trading proxy"
        assert display_ticker(key) == proxy.split(".")[0]


def test_us_listed_proxies_are_not_mistaken_for_holdings():
    """The opposite error, and just as wrong.

    Sleeve A HOLDS the S&P 500 sector UCITS and merely PRICES them off the
    US-listed SPDRs. A resolver that returned the proxy unconditionally would
    relabel every sleeve A row with a fund the book does not own.
    """
    for key, proxy in [("IUFS", "XLF"), ("IUES", "XLE"), ("IUHC", "XLV"),
                       ("IUIS", "XLI"), ("IUMS", "XLB"), ("IUSP", "XLRE")]:
        assert ETF_REGISTRY[key]["yfinance_trading_proxy"] == proxy
        assert display_ticker(key) == key


def test_unknown_symbols_pass_through():
    """Most held tickers (SPY, QQQ, thematics) are not registry members."""
    for key in ("SPY", "QQQ", "EEM", "SHY", "ARKG", "BTC-USD"):
        assert display_ticker(key) == key


def test_no_xetr_member_would_resolve_by_suffix_appending():
    """Appending '.DE' to the key is the shortcut that caused the defect.

    If it ever happens to agree with the registry for every member, someone
    will reintroduce it as a simplification. EXH3 is what makes it disagree,
    so this asserts the two are genuinely different rules.
    """
    disagreements = [
        key for key, cfg in ETF_REGISTRY.items()
        if cfg.get("trading_calendar") == "XETR"
        and cfg["yfinance_trading_proxy"] != f"{key}.DE"
    ]
    assert "EXH3" in disagreements


def test_map_covers_the_whole_registry():
    m = display_ticker_map()
    assert set(m) == set(ETF_REGISTRY)
    assert m["EXH3"] == "EXH4"


# --------------------------------------------------------------------------
# Surface 1 — the dashboard
# --------------------------------------------------------------------------

def test_pipeline_emits_the_display_map():
    """The dashboard cannot resolve this itself; it reads the injected map."""
    src = (ROOT / "scripts" / "pipeline.py").read_text(encoding="utf-8")
    assert "display_ticker_map()" in src
    assert '"display_tickers"' in src


ELEMENT_TEXT = re.compile(r">([^<>]*)<")
TITLE_ATTR = re.compile(r'title="([^"]*)"')
# A BARE interpolation of the panel key: `${h.etf}` rendered straight into the
# page. Deliberately not `\w+\.etf` anywhere in the segment — most such
# references are lookups that must keep the key (ETF_DESCRIPTIONS[h.etf],
# lbl(a.etf), colourFor(e.etf), _proxy(e.etf) for the trade-as column). It is
# printing the key as the label that is the defect.
BARE_KEY = re.compile(r"\$\{\s*\w+\.etf\s*\}")


def test_dashboard_renders_no_panel_key_as_visible_text():
    """Every ticker a reader SEES or hovers goes through _displaySym.

    Scanning element text and title attributes rather than a list of known
    cells: the first version of this test enumerated the holdings and activity
    cells and passed while the sleeve-composition bars and the universe chips
    were still printing EXH3. Anything rendered between ``>`` and ``<``, or
    into a tooltip, is read by a human and must be resolved.

    Lookup keys are deliberately untouched — ``data-etf``, ``colourFor`` and the
    name map are attributes and function arguments, not element text, so they
    keep the panel key and do not appear here.
    """
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "function _displaySym(" in text, "resolver missing from the template"

    offenders = []
    for i, line in enumerate(text.splitlines(), 1):
        spans = [m.span(1) for pattern in (ELEMENT_TEXT, TITLE_ATTR)
                 for m in pattern.finditer(line)]
        for m in BARE_KEY.finditer(line):
            if any(s <= m.start() and m.end() <= e for s, e in spans):
                offenders.append((i, m.group(0), line.strip()[:90]))
    assert not offenders, (
        "panel keys rendered as visible text instead of _displaySym():\n"
        + "\n".join(f"  line {n}: {tok} in {frag}" for n, tok, frag in offenders)
    )


BARE_VAR = re.compile(r"\$\{\s*etf\s*\}")

# The panel key is the RIGHT answer in these two messages: they are about the
# file on disk and the script that rebuilds it, keyed by panel id, exactly as
# the Data Health rows are.
PANEL_ID_MESSAGES = ("No series file for", "No ETF-level series for")


def test_no_bare_etf_variable_is_rendered_as_a_label():
    """The chip and row-label renderers hold the key in a bare ``etf`` local.

    They are invisible to the ``${x.etf}`` sweep, and all seven were still
    printing EXH3 after that sweep passed — three chip groups, four sleeve
    breakdown tables.
    """
    text = TEMPLATE.read_text(encoding="utf-8")
    offenders = []
    for i, line in enumerate(text.splitlines(), 1):
        if any(msg in line for msg in PANEL_ID_MESSAGES):
            continue
        spans = [m.span(1) for m in ELEMENT_TEXT.finditer(line)]
        for m in BARE_VAR.finditer(line):
            if any(s <= m.start() and m.end() <= e for s, e in spans):
                offenders.append((i, line.strip()[:80]))
    assert not offenders, (
        "bare etf variable rendered as a label:\n"
        + "\n".join(f"  line {n}: {frag}" for n, frag in offenders)
    )


def test_chart_series_and_hover_labels_resolve():
    """A Plotly legend entry and a hover box are read by a human too."""
    text = TEMPLATE.read_text(encoding="utf-8")
    # Resolved once inside the shared helper rather than at each call site.
    assert "const sym = _displaySym(etf);" in text
    assert "return d ? `${sym} — ${d}` : sym;" in text
    # Trace names carry their own label and are resolved individually.
    assert "name: `${etf}" not in text, "a Plotly trace still names the panel key"


# "`${etf} — " is the house idiom for "ticker, separator, description". It is
# how a legend entry, a trace name and a precomputed `label` local are all
# built, which makes it the one pattern worth banning outright.
TICKER_DASH_LABEL = re.compile(r"`\$\{\s*etf\s*\}\s*(?:—|&mdash;|-)")


def test_no_label_literal_starts_from_the_bare_panel_key():
    """The three stacked-area charts precompute `label` before naming a trace.

    They survived both sweeps above — the key is not ``x.etf``, and the literal
    is assigned to a variable rather than rendered between ``>`` and ``<`` — and
    the Europe chart legend still read "EXH3 — Industrials" on the built page
    while the whole suite was green. Banning the idiom is what actually closes
    it.
    """
    text = TEMPLATE.read_text(encoding="utf-8")
    offenders = [
        (i, line.strip()[:90])
        for i, line in enumerate(text.splitlines(), 1)
        if TICKER_DASH_LABEL.search(line)
    ]
    assert not offenders, (
        "label literals built from the panel key instead of _displaySym():\n"
        + "\n".join(f"  line {n}: {frag}" for n, frag in offenders)
    )


def test_label_fallbacks_resolve_too():
    """``meta.label ? ... : etf`` — the no-description branch is still a label."""
    text = TEMPLATE.read_text(encoding="utf-8")
    for i, line in enumerate(text.splitlines(), 1):
        if "const label = " in line and "etf" in line:
            assert "_displaySym(etf)" in line, f"line {i}: {line.strip()[:90]}"


def test_the_seg_bars_resolve_in_every_sleeve_tab():
    """Four identical copies, one per sleeve tab — the pattern-consistency case.

    The ternary form ``${pct >= 7 ? h.etf : ''}`` is not a bare interpolation,
    so the sweep above cannot see it. Sleeve D's bar is the one that renders
    the Europe members, and it was still printing EXH3 after the first fix.
    """
    text = TEMPLATE.read_text(encoding="utf-8")
    assert text.count("${pct >= 7 ? _displaySym(h.etf) : ''}") == 4
    assert "${pct >= 7 ? h.etf : ''}" not in text


def test_universe_and_holding_chips_resolve():
    """The chip helpers take a bare ticker, so the sweep above cannot see them."""
    text = TEMPLATE.read_text(encoding="utf-8")
    for chip in ('<span class="uni-chip${cashCls}">${_displaySym(t)}</span>',
                 '<span class="uni-chip">${_displaySym(t)}</span>'):
        assert chip in text, f"chip renderer not resolved: {chip}"


def test_dashboard_keeps_the_panel_key_for_lookups():
    """The fix must not have rewritten the row identity or chart lookup.

    data-etf keys the expand/chart behaviour and the colour and name maps; if
    it became EXH4, the row would lose its panel and silently stop expanding.
    """
    text = TEMPLATE.read_text(encoding="utf-8")
    assert 'data-etf="${e.etf}"' in text
    assert "colourFor(e.etf)" in text
    assert "EXH3: 'iShares Stoxx Europe 600 Industrial Goods & Services'" in text


# --------------------------------------------------------------------------
# Surfaces 2 and 3 — the factsheet PDF and the weekly email
# --------------------------------------------------------------------------

@pytest.mark.parametrize("script,sites", [
    ("build_factsheet.py", ['col_p(display_ticker(h["etf"])',
                            "col_p(display_ticker(etf)",
                            "display_ticker(r['etf'])"]),
    ("build_email_body.py", ["sym = display_ticker(etf)"]),
])
def test_document_surfaces_resolve_before_printing(script, sites):
    src = (ROOT / "scripts" / script).read_text(encoding="utf-8")
    assert "from etf_registry import display_ticker" in src, script
    for site in sites:
        assert site in src, f"{script}: {site!r} not found — a print site regressed"


def test_email_names_stay_keyed_by_the_panel_key():
    """The label map is keyed by panel key; resolving before the lookup would
    silently blank the fund name for the Europe rows."""
    src = (ROOT / "scripts" / "build_email_body.py").read_text(encoding="utf-8")
    assert "nm = labels.get(etf, \"\")" in src


# --------------------------------------------------------------------------
# Surface 4 — the public portfolio page
# --------------------------------------------------------------------------

IMPORTS_RESOLVER = re.compile(
    r"^from etf_registry import .*\bdisplay_ticker\b", re.M)


def test_simple_page_uses_the_shared_rule():
    """It had its own copy first; one rule, one place.

    Matched as an import of the name rather than one exact line — the first
    version of this pinned the whole statement and broke the moment the builder
    imported a second symbol alongside it, which says nothing about the rule.
    """
    src = (ROOT / "scripts" / "build_simple_page.py").read_text(encoding="utf-8")
    assert IMPORTS_RESOLVER.search(src), "display_ticker is not imported from the registry"
    assert "def display_ticker(" not in src, "local reimplementation is back"


def test_built_surfaces_agree_on_the_europe_label():
    """End-to-end: whatever is published must not carry the panel id.

    Reads the built artefacts rather than the sources, so a stale build that
    still prints EXH3 fails here even though every source is correct.
    """
    if not bsp.OUT_PATH.exists():
        pytest.skip(f"{bsp.OUT_PATH.name} not built yet")
    payload = json.loads((ROOT / "data" / "portfolio_simple.json")
                          .read_text(encoding="utf-8"))
    tickers = {h["ticker"] for h in payload["holdings"]}
    assert "EXH3" not in tickers
    # KEY ON THE PANEL ID, NOT ON A PREFIX. The previous form asserted that if
    # any strategy_d holding started with "EXH" then EXH4 must be displayed —
    # which treats EXH1 (Oil & Gas, whose panel id and traded ticker are both
    # EXH1) as evidence that the industrials fund is held. It passed only while
    # the basket happened to contain EXH3, and failed the moment sleeve D held
    # EXH1 without it. The rule being guarded is narrow and exact: the fund
    # whose PANEL ID is EXH3 trades as EXH4 and must display that way.
    for h in payload["holdings"]:
        if h.get("panel_key") == "EXH3":
            assert h["ticker"] == "EXH4", (
                f"industrials panel EXH3 displayed as {h['ticker']!r}")
