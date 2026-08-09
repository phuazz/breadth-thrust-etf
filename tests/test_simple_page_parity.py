"""The public portfolio page must never disagree with the book it describes.

build/portfolio.html is the page published at phuazz.github.io/portfolio/ —
a second surface over the same positions as docs/index.html. Two surfaces over
one set of numbers is how the factsheet, the digest email and the dashboard
have drifted apart before, and this one is the surface strangers read. So the parity is asserted rather than assumed:
the page's holdings, its sleeve split and its headline statistics must be
derivable from the source JSONs and nothing else.

The build refuses to write on any of these too. These tests are the second,
independent gate — the case where the built artefact on disk has been
hand-edited, or where a source moved after the last build.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build_simple_page as bsp  # noqa: E402
from etf_registry import ETF_REGISTRY  # noqa: E402

TOL = bsp.WEIGHT_TOLERANCE


@pytest.fixture(scope="module")
def live():
    return json.loads((ROOT / "data" / "live_track.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def overlay():
    return json.loads((ROOT / "data" / "risk_overlay.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def payload():
    """The payload as the builder would produce it right now."""
    return bsp.build_payload()


@pytest.fixture(scope="module")
def built_payload():
    """The payload actually embedded in the published page."""
    path = bsp.OUT_PATH
    if not path.exists():
        pytest.skip(f"{path.name} not built yet")
    text = path.read_text(encoding="utf-8")
    start = text.find(bsp.PLACEHOLDER_START)
    end = text.find(bsp.PLACEHOLDER_END)
    assert start != -1 and end != -1, "placeholder markers missing from built page"
    block = text[start + len(bsp.PLACEHOLDER_START):end].strip()
    prefix, suffix = "const PORTFOLIO_DATA_INLINE = ", ";"
    assert block.startswith(prefix) and block.endswith(suffix)
    return json.loads(block[len(prefix):-len(suffix)].replace("<\\/", "</"))


# --------------------------------------------------------------------------
# Holdings parity
# --------------------------------------------------------------------------

def test_holdings_weights_match_effective_weights(payload, live):
    """Every published weight is the book's own effective NAV weight."""
    effective = live["effective_weights"]
    published = {}
    for h in payload["holdings"]:
        published[h["ticker"]] = published.get(h["ticker"], 0.0) + h["weight"]

    # Compare through the same display mapping, so the assertion is about the
    # numbers rather than about the naming convention.
    expected = {}
    for ticker, weight in effective.items():
        key = bsp.display_ticker(ticker)
        expected[key] = expected.get(key, 0.0) + weight

    assert set(published) == set(expected)
    for key, weight in expected.items():
        assert published[key] == pytest.approx(weight, abs=TOL)


def test_holdings_sum_to_nav(payload):
    total = sum(h["weight"] for h in payload["holdings"])
    assert total == pytest.approx(1.0, abs=TOL)


def test_no_position_is_dropped(payload, live):
    assert payload["n_positions"] == len(live["effective_weights"])
    assert len(payload["holdings"]) == len(live["effective_weights"])


def test_every_holding_carries_a_name(payload):
    """A bare ticker is meaningless to the audience this page is for."""
    for h in payload["holdings"]:
        assert h["name"], h["ticker"]
        assert h["name"] != h["ticker"]


# --------------------------------------------------------------------------
# The traded ticker, not the internal panel id
# --------------------------------------------------------------------------

def test_europe_holdings_show_the_traded_ticker(payload):
    """EXH3 is a panel id; the instrument that is bought is EXH4.DE.

    etf_registry states the traded ticker is ALWAYS the proxy field for these
    entries. Printing the key on a page headed "what it holds today" would name
    a fund the book does not own — and in EXH3's case a fund in a different
    sector, which is the error that shipped once already.
    """
    xetr = {k for k, v in ETF_REGISTRY.items() if v.get("trading_calendar") == "XETR"}
    published = {h["ticker"] for h in payload["holdings"]}
    for key in xetr:
        assert key not in published or key == ETF_REGISTRY[key]["yfinance_trading_proxy"].split(".")[0], (
            f"{key} is published as an internal panel id, not as its traded ticker"
        )


def test_exh3_resolves_to_exh4():
    """Pinned explicitly: this is the specific mislabel the book has form on."""
    assert bsp.display_ticker("EXH3") == "EXH4"


def test_non_europe_tickers_are_not_rewritten():
    """Sleeve A holds the UCITS lines; its proxy is a price source, not what is
    bought. The mapping must not silently swap IUFS for XLF."""
    assert bsp.display_ticker("IUFS") == "IUFS"
    assert bsp.display_ticker("IUES") == "IUES"
    assert bsp.display_ticker("SPY") == "SPY"  # not in the registry at all


# --------------------------------------------------------------------------
# Sleeve split
# --------------------------------------------------------------------------

def test_sleeve_split_matches_sleeve_membership(payload, live):
    expected: dict[str, float] = {}
    membership = {}
    for key, sleeve in live["sleeve_extensions"].items():
        for ticker in sleeve["weights"]:
            membership[ticker] = key
    membership[bsp.TILT_TICKER] = "tilt"
    for ticker, weight in live["effective_weights"].items():
        k = membership[ticker]
        expected[k] = expected.get(k, 0.0) + weight

    published = {s["key"]: s["weight"] for s in payload["sleeves"]}
    assert set(published) == set(expected)
    for k, w in expected.items():
        assert published[k] == pytest.approx(w, abs=TOL)


def test_sleeve_split_sums_to_nav(payload):
    assert sum(s["weight"] for s in payload["sleeves"]) == pytest.approx(1.0, abs=TOL)


def test_sleeve_order_is_the_validated_palette_order(payload):
    """The categorical palette passed its adjacent-pair separation checks in
    this order. Reordering the sleeves silently reorders the hues."""
    published = [s["key"] for s in payload["sleeves"]]
    assert published == [k for k in bsp.SLEEVE_ORDER if k in published]


# --------------------------------------------------------------------------
# Statistics and dating
# --------------------------------------------------------------------------

def test_stats_are_the_deployed_variant(payload, overlay):
    variant = overlay["gated_variants"][bsp.DEPLOYED_KEY]
    st = payload["stats"]
    assert st["sharpe"] == pytest.approx(variant["sharpe"], abs=5e-5)
    assert st["cagr"] == pytest.approx(variant["cagr"], abs=5e-7)
    assert st["max_dd"] == pytest.approx(variant["max_dd"], abs=5e-7)
    assert st["start"] == variant["dates"][0]
    assert st["end"] == variant["dates"][-1]


def test_positions_and_prices_share_one_date(payload):
    """A holdings table dated one day and a curve dated another is the mixed
    as-of failure in a new place."""
    assert payload["as_of"] == payload["panel_end_date"]
    assert payload["curve"]["dates"][-1] == payload["as_of"]


def test_curve_is_downsampled_but_intact(payload, overlay):
    variant = overlay["gated_variants"][bsp.DEPLOYED_KEY]
    curve = payload["curve"]
    assert len(curve["dates"]) == len(curve["equity"])
    assert len(curve["dates"]) <= bsp.MAX_CURVE_POINTS + 1
    assert curve["dates"][0] == variant["dates"][0]
    assert curve["dates"][-1] == variant["dates"][-1]
    assert curve["dates"] == sorted(curve["dates"])


# --------------------------------------------------------------------------
# Per-holding price charts
# --------------------------------------------------------------------------

def test_every_chart_belongs_to_a_holding(payload):
    shown = {h["ticker"] for h in payload["holdings"]}
    assert set(payload["charts"]) <= shown


def test_charts_and_calendars_line_up(payload):
    """A ragged series draws a chart that is silently wrong, not one that fails."""
    calendars = payload["calendars"]
    for ticker, chart in payload["charts"].items():
        axis = calendars.get(chart["cal"])
        assert axis is not None, f"{ticker} references calendar {chart['cal']!r}"
        assert len(chart["px"]) == len(axis), ticker
        assert chart["last"] == axis[-1], ticker
        assert axis == sorted(axis), f"{ticker}: calendar out of order"


def test_no_chart_is_dated_after_the_book(payload):
    for ticker, chart in payload["charts"].items():
        assert chart["last"] <= payload["as_of"], (
            f"{ticker} chart is dated after the holdings table")


def test_stale_series_lose_their_chart_rather_than_lying(payload):
    """A per-ticker lag is disclosed on the chart; a big one is a broken feed.

    EEM sits behind the rest of the book most weeks because its series settles
    later, which is exactly why the page dates each chart individually instead
    of inheriting the page's as-of — the mixed as-of failure in a new place.
    """
    for ticker, chart in payload["charts"].items():
        assert 0 <= chart["lag_days"] <= bsp.MAX_CHART_LAG_DAYS, (
            f"{ticker} is {chart['lag_days']}d behind and should have been dropped")


def test_currency_is_the_instruments_own(payload):
    """The feed publishes native-currency closes; Xetra series are in euros.

    Labelling a euro price line "USD" would be a straightforward misstatement
    on a page other people read.
    """
    by_ticker = {h["ticker"]: h["panel_key"] for h in payload["holdings"]}
    for ticker, chart in payload["charts"].items():
        proxy = bsp.price_series_key(by_ticker[ticker])
        expected = "EUR" if proxy.endswith(".DE") else "USD"
        assert chart["currency"] == expected, ticker


def test_proxy_pricing_is_disclosed_exactly_when_it_applies(payload):
    """Set for sleeve A (holds IUFS, priced off XLF); absent for Xetra, where
    the proxy is the same fund on its home exchange."""
    charts = payload["charts"]
    if "IUFS" in charts:
        assert charts["IUFS"]["priced_via"] == "XLF"
    if "EXH4" in charts:
        assert charts["EXH4"]["priced_via"] is None
    for ticker, chart in charts.items():
        via = chart["priced_via"]
        assert via is None or via != ticker


def test_unavailable_charts_are_named_not_silent(payload):
    """A row that lost its chart must be distinguishable from one that never
    had one — otherwise a feed outage looks like a design decision."""
    for ticker in payload["charts_unavailable"]:
        assert ticker not in payload["charts"]
    shown = {h["ticker"] for h in payload["holdings"]}
    assert set(payload["charts_unavailable"]) <= shown
    assert set(payload["charts"]) | set(payload["charts_unavailable"]) <= shown


# --------------------------------------------------------------------------
# The published artefact
# --------------------------------------------------------------------------

def test_built_page_matches_the_current_sources(built_payload, payload):
    """Catches a hand-edited build/portfolio.html and a stale build alike."""
    assert built_payload == json.loads(json.dumps(payload))


def _flat(path: Path) -> str:
    """Page text with runs of whitespace collapsed.

    The disclosures are prose and wrap across lines in the source; matching the
    raw text would assert on the line breaks rather than on the words.
    """
    return " ".join(path.read_text(encoding="utf-8").split()).lower()


def test_built_page_carries_its_disclosures():
    """The simulated-record statement is a disclosure, not decoration: the page
    must not be able to ship without it."""
    path = bsp.OUT_PATH
    text = _flat(path)
    for required in ("simulated", "no live track record", "not investment advice"):
        assert required in text, f"missing disclosure: {required}"
    assert 'name="viewport"' in path.read_text(encoding="utf-8")


def test_built_page_discloses_no_parameters():
    """The whole point of the reduced surface. If a threshold, a lookback or a
    top-K value appears here, the page has stopped being the simple one."""
    text = _flat(bsp.OUT_PATH)
    for leak in ("200d", "200-day", "50d ", "golden cross", "top k", "top-k",
                 "breadth", "z-score", "lookback", "sharpe ratio of the sleeve"):
        assert leak not in text, f"method detail leaked onto the simple page: {leak!r}"


def test_page_stays_small_enough_to_open():
    """A page a reader loads on a phone, and a source file a reviewer opens."""
    assert (ROOT / "simple_template.html").stat().st_size <= bsp.MAX_TEMPLATE_BYTES
    assert bsp.OUT_PATH.stat().st_size <= 500 * 1024
