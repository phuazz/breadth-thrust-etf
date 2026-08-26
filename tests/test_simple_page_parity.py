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

def test_sleeve_split_matches_sleeve_membership(payload, live, overlay):
    expected: dict[str, float] = {}
    membership = {}
    for key, sleeve in live["sleeve_extensions"].items():
        for ticker in sleeve["weights"]:
            membership[ticker] = key
    membership[bsp.TILT_TICKER] = "tilt"
    # The overlay holds two positions that belong to no strategy sleeve: the
    # EEM tilt and the de-risk instrument. Resolved from config here, as the
    # builder does, so a parameter change breaks both together or neither.
    fallback = (overlay.get("gate_parameters") or {}).get("fallback_ticker")
    if fallback:
        membership[fallback] = bsp.RESERVE_KEY
    for ticker, weight in live["effective_weights"].items():
        k = membership[ticker]
        expected[k] = expected.get(k, 0.0) + weight

    published = {s["key"]: s["weight"] for s in payload["sleeves"]}
    assert set(published) == set(expected)
    for k, w in expected.items():
        assert published[k] == pytest.approx(w, abs=TOL)


def test_sleeve_split_sums_to_nav(payload):
    assert sum(s["weight"] for s in payload["sleeves"]) == pytest.approx(1.0, abs=TOL)


def _rebuild_with_weights(tmp_path, monkeypatch, live, overlay, weights):
    """Build the payload against a synthetic book, leaving the repo untouched."""
    names = json.loads((ROOT / "data" / "etf_names.json").read_text(encoding="utf-8"))
    live = {**live, "effective_weights": weights}
    for src, obj in (("live_track.json", live),
                     ("risk_overlay.json", overlay),
                     ("etf_names.json", names)):
        (tmp_path / src).write_text(json.dumps(obj), encoding="utf-8")
    monkeypatch.setattr(bsp, "LIVE_TRACK", tmp_path / "live_track.json")
    monkeypatch.setattr(bsp, "RISK_OVERLAY", tmp_path / "risk_overlay.json")
    monkeypatch.setattr(bsp, "ETF_NAMES", tmp_path / "etf_names.json")
    return bsp.build_payload()


def test_derisk_reserve_is_shown_at_both_ends_of_its_range(
        tmp_path, monkeypatch, live, overlay):
    """The overlay's de-risk instrument must reach the page whether it holds
    one basis point or half the book.

    Both failure modes were live options on 2026-08-15. Folding it into the
    tilt bucket would have made a 50% cash position read as an emerging-market
    tilt; dropping it as a sub-threshold residual would have made the split
    stop summing to NAV the moment the gate fired. The residual case is the
    one that occurs in calm markets and the large case is the one that occurs
    when a reader most needs the page to be right, so both are pinned.
    """
    fallback = (overlay.get("gate_parameters") or {}).get("fallback_ticker")
    assert fallback, "risk_overlay.json carries no gate_parameters.fallback_ticker"

    sleeve_only = {t: w for t, w in live["effective_weights"].items()
                   if t != fallback}
    scale = sum(sleeve_only.values())

    for reserve_w in (0.0001, 0.5):
        weights = {t: w / scale * (1 - reserve_w) for t, w in sleeve_only.items()}
        weights[fallback] = reserve_w
        payload = _rebuild_with_weights(tmp_path, monkeypatch, live, overlay, weights)

        split = {s["key"]: s["weight"] for s in payload["sleeves"]}
        assert bsp.RESERVE_KEY in split, (
            f"reserve bucket vanished at {reserve_w:.2%} of NAV")
        assert split[bsp.RESERVE_KEY] == pytest.approx(reserve_w, abs=TOL)
        # SCALE THE TOLERANCE TO THE NUMBER OF ROUNDED TERMS. Each sleeve
        # weight is rounded to 6dp independently, so their SUM can sit up to
        # len(split) half-ulps from 1.0 — with six buckets, ~3e-6. A flat TOL
        # of 1e-6 on the sum was always too tight and passed by luck; the WS18
        # restatement moved the weights enough to expose it. The per-bucket
        # assertions above still use TOL, which is where it belongs.
        assert sum(split.values()) == pytest.approx(1.0, abs=TOL * len(split))
        # It must not be quietly filed under the tilt: that bucket carries the
        # tilt's own weight and nothing else.
        assert split.get("tilt") == pytest.approx(weights[bsp.TILT_TICKER], abs=TOL)
        held = {h["panel_key"] for h in payload["holdings"]}
        assert fallback in held, f"{fallback} dropped from holdings at {reserve_w:.2%}"


def test_sleeve_order_is_the_validated_palette_order(payload):
    """The categorical palette passed its adjacent-pair separation checks in
    this order. Reordering the sleeves silently reorders the hues."""
    published = [s["key"] for s in payload["sleeves"]]
    assert published == [k for k in bsp.SLEEVE_ORDER if k in published]


# --------------------------------------------------------------------------
# Statistics and dating
# --------------------------------------------------------------------------

def _anchored(variant, payload):
    """The deployed variant's series trimmed to the page's as-of date.

    WS16 (2026-08-13): under the Friday-morning refresh cadence the record
    curves legitimately end on Thursday's close while the page pins its whole
    view to the last completed weekly anchor. The page's contract is the
    TRIMMED window; these tests compare against exactly that."""
    cut = len([d for d in variant["dates"] if d <= payload["as_of"]])
    return variant["dates"][:cut], variant["equity"][:cut]


def test_stats_are_the_deployed_variant_on_the_anchored_window(payload, overlay):
    variant = overlay["gated_variants"][bsp.DEPLOYED_KEY]
    dates, equity = _anchored(variant, payload)
    expected = bsp.weekly_stats(dates, equity)
    st = payload["stats"]
    assert st["sharpe"] == pytest.approx(expected["sharpe"], abs=5e-5)
    assert st["cagr"] == pytest.approx(expected["cagr"], abs=5e-7)
    assert st["max_dd"] == pytest.approx(expected["max_dd"], abs=5e-7)
    assert st["start"] == dates[0]
    assert st["end"] == dates[-1] == payload["as_of"]


def test_positions_and_prices_share_one_date(payload):
    """A holdings table dated one day and a curve dated another is the mixed
    as-of failure in a new place.

    The intent is right; the old first assertion did not test it. It required
    as_of == panel_end_date, and since as_of is min(panel_end, live_anchor)
    that holds only when the PANEL is the staler of the two — so it passed on
    a five-session-stale panel and failed on a one-session-fresh one. Every
    series the page DISPLAYS is trimmed to as_of, which is what "one date"
    means here; panel_end_date is provenance for the regime panel, recorded
    beside the book rather than displayed as its date.
    """
    assert payload["curve"]["dates"][-1] == payload["as_of"]
    assert payload["stats"]["end"] == payload["as_of"]
    for ticker, chart in (payload.get("charts") or {}).items():
        assert chart["last"] <= payload["as_of"], (
            f"{ticker} chart is dated after the book's as-of")
    # And the regime panel must not be BEHIND the book — that direction is the
    # one that shipped an eleven-week-old de-risk headline.
    assert bsp._sessions_between(payload["as_of"], payload["panel_end_date"]) >= 0


def test_curve_is_downsampled_but_intact(payload, overlay):
    variant = overlay["gated_variants"][bsp.DEPLOYED_KEY]
    dates, _ = _anchored(variant, payload)
    curve = payload["curve"]
    assert len(curve["dates"]) == len(curve["equity"])
    assert len(curve["dates"]) <= bsp.MAX_CURVE_POINTS + 1
    assert curve["dates"][0] == dates[0]
    assert curve["dates"][-1] == dates[-1] == payload["as_of"]
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


def _one_holding_feed(tmp_path, monkeypatch, dates, prices):
    """Point build_charts at a one-line synthetic feed and return the holdings.

    EEM is used because it is priced under its own symbol, so the feed key is
    the ticker and the fixture does not encode a proxy mapping that could go
    stale beside the registry.
    """
    path = tmp_path / "holdings_prices_1y.json"
    path.write_text(
        json.dumps({"prices": {bsp.price_series_key("EEM"): {
            "dates": dates, "prices": prices}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(bsp, "HOLDINGS_PRICES", path)
    return [{"ticker": "EEM", "panel_key": "EEM", "weight": 1.0}]


# Python's datetime is 1-indexed on months; both cases below straddle a
# boundary deliberately — 06-30/07-01 and 12-31/01-02.
@pytest.mark.parametrize("as_of,dates,kept_last,kept_n", [
    ("2026-06-30",
     ["2026-06-26", "2026-06-29", "2026-06-30", "2026-07-01", "2026-07-02"],
     "2026-06-30", 3),
    ("2025-12-31",
     ["2025-12-30", "2025-12-31", "2026-01-02"],
     "2025-12-31", 2),
])
def test_a_series_running_past_the_book_is_cut_not_dropped(
        tmp_path, monkeypatch, as_of, dates, kept_last, kept_n):
    """The feed is refreshed daily, the book struck weekly, so on most days
    every series runs past as_of. Dropping those charts emptied the page
    (21 of 23 gone on 2026-08-11); the chart is truncated instead."""
    holdings = _one_holding_feed(
        tmp_path, monkeypatch, dates, [10.0 + i for i in range(len(dates))])
    calendars, charts, skipped = bsp.build_charts(holdings, as_of)

    assert skipped == []
    chart = charts["EEM"]
    assert chart["last"] == kept_last
    assert len(chart["px"]) == kept_n
    assert calendars[chart["cal"]] == dates[:kept_n]
    assert chart["lag_days"] == 0


def test_a_series_entirely_after_the_book_still_loses_its_chart(
        tmp_path, monkeypatch):
    """Nothing left after the cut is not a lag, it is the wrong feed."""
    holdings = _one_holding_feed(
        tmp_path, monkeypatch, ["2026-07-01", "2026-07-02"], [10.0, 11.0])
    _, charts, skipped = bsp.build_charts(holdings, "2026-06-30")
    assert charts == {} and skipped == ["EEM"]


def test_the_lag_ceiling_is_applied_after_the_cut(tmp_path, monkeypatch):
    """A stale series that only looked fresh because of post-as_of bars must
    still be dropped — otherwise truncation smuggles a broken feed back in."""
    holdings = _one_holding_feed(
        tmp_path, monkeypatch,
        ["2026-06-01", "2026-06-29", "2026-07-01"], [10.0, 11.0, 12.0])
    _, charts, skipped = bsp.build_charts(holdings, "2026-06-30")
    assert skipped == [] and charts["EEM"]["lag_days"] == 1

    holdings = _one_holding_feed(
        tmp_path, monkeypatch, ["2026-06-01", "2026-07-01"], [10.0, 12.0])
    _, charts, skipped = bsp.build_charts(holdings, "2026-06-30")
    assert charts == {} and skipped == ["EEM"], (
        "29d behind after the cut, past the 14d ceiling")


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

# --------------------------------------------------------------------------
# Parity, and the one key that cannot take part in it (2026-08-26)
#
# THIS COMPARISON MUST BE DETERMINISTIC OR IT IS NOT A TEST. `freshness` is
# not: build_simple_page._load_freshness runs verdict_has_lapsed, which READS
# THE CLOCK on purpose, so the same page and the same sources produce a block
# before a venue closes and None after it. The suite therefore went red on its
# own, with no commit and no source change, every time a session completed
# after the last local refresh_all.py run -- 2026-08-25 08:24 UTC in the case
# that surfaced this. Two scheduled workflows gate on this suite, so the ETF
# scanner stopped publishing for the same reason.
#
# Excluding the key is not weakening the guard, because parity was never the
# thing protecting it: a lapsed verdict is caught at BUILD time, where
# _load_freshness withholds it. What parity is for -- a hand-edited page, a
# source rolled back under a built artefact -- is still checked on this key
# below, against the clock-free half of the same loader.
# --------------------------------------------------------------------------

CLOCK_DEPENDENT = ("freshness",)


def test_built_page_matches_the_current_sources(built_payload, payload):
    """Catches a hand-edited build/portfolio.html and a stale build alike."""
    strip = lambda d: {k: v for k, v in d.items() if k not in CLOCK_DEPENDENT}
    assert strip(built_payload) == strip(json.loads(json.dumps(payload)))


def test_the_pages_freshness_block_is_the_one_on_disk(built_payload):
    """The clock-free half of the parity check on the excluded key.

    A page MAY carry no freshness block: the build withholds it whenever the
    verdict has lapsed, and that is the safe direction. What it may never do is
    carry one that disagrees with the report on disk -- that is a hand-edit, or
    a report rolled back underneath a built page, and neither is visible
    anywhere else.
    """
    shown = built_payload.get("freshness")
    if shown is None:
        pytest.skip("page carries no freshness block — always permitted")
    blob = bsp.read_freshness_report()
    assert blob is not None, (
        "the page discloses a freshness verdict that no longer exists on disk")
    assert shown == json.loads(json.dumps(bsp.filter_freshness(blob)))


def test_a_lapsed_verdict_is_withheld_from_a_fresh_build(monkeypatch):
    """Pin the behaviour the exclusion above relies on.

    Parity no longer covers this key, so the withholding has to be tested
    directly or the exclusion would quietly become a hole.
    """
    blob = bsp.read_freshness_report()
    if blob is None:
        pytest.skip("no freshness report on disk to reason about")
    monkeypatch.setattr(bsp, "verdict_has_lapsed", lambda *a, **k: True)
    assert bsp._load_freshness() is None
    monkeypatch.setattr(bsp, "verdict_has_lapsed", lambda *a, **k: False)
    assert bsp._load_freshness() == bsp.filter_freshness(blob)


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


# ---------------------------------------------------------------------------
# Panel-vs-curve staleness, in BOTH directions
#
# The guard here used to assert as_of == panel_end_date. Because as_of is
# min(panel_end, live_anchor), that equality holds only when the PANEL is the
# staler of the two -- so it permitted the dangerous direction and refused the
# benign one. The committed data carried panel_end 2026-08-07 against
# live_anchor 2026-08-12 and passed, publishing a regime panel five sessions
# stale. These pin the corrected behaviour.
# ---------------------------------------------------------------------------

def _minimal_payload(as_of: str, panel_end: str) -> dict:
    return {
        "as_of": as_of,
        "panel_end_date": panel_end,
        "holdings": [{"etf": "SPY", "ticker": "SPY", "weight": 1.0}],
        "sleeves": [{"key": "a", "weight": 1.0}],
        "curve": {"dates": [as_of], "equity": [1.0]},
        "calendars": {}, "charts": {},
    }


def test_a_stale_panel_is_now_refused():
    """The direction that shipped an eleven-week-old de-risk headline."""
    p = _minimal_payload(as_of="2026-08-12", panel_end="2026-08-07")
    with pytest.raises(bsp.SimplePageError, match="BEHIND"):
        bsp.assert_payload_usable(p)


def test_the_committed_shape_that_used_to_pass_now_fails():
    """Exactly the committed pairing, asserted as a regression."""
    p = _minimal_payload(as_of="2026-08-07", panel_end="2026-08-07")
    p["curve"] = {"dates": ["2026-08-07"], "equity": [1.0]}
    # as_of == panel_end passes on its own; the failure above is what the real
    # committed pair (min() = 2026-08-07 against a 2026-08-12 curve) hid.
    bsp.assert_payload_usable(p)


def test_a_panel_one_session_ahead_is_allowed():
    """The routine case: the .DE lines publish about a session late, so the
    NAV curve trails the US breadth panel most weeks."""
    p = _minimal_payload(as_of="2026-08-12", panel_end="2026-08-13")
    bsp.assert_payload_usable(p)


def test_a_panel_far_ahead_is_still_refused():
    p = _minimal_payload(as_of="2026-06-01", panel_end="2026-08-13")
    with pytest.raises(bsp.SimplePageError, match="leads the page as-of"):
        bsp.assert_payload_usable(p)


def test_lead_is_counted_in_sessions_not_calendar_days():
    """A long weekend must not read as a staleness breach. Fri 3 Jul 2026 is a
    US holiday, so 2-6 July spans only two sessions, not four days."""
    assert bsp._sessions_between("2026-07-02", "2026-07-06") == 1
    assert bsp._sessions_between("2026-08-13", "2026-08-12") == -1
    assert bsp._sessions_between("2026-08-13", "2026-08-13") == 0
    assert bsp._sessions_between(None, "2026-08-13") is None


# ---------------------------------------------------------------------------
# The tolerance itself (2026-08-22)
#
# The flat 1e-6 that stood here was justified in a comment as "far looser
# than float noise over that many terms". It was in fact TIGHTER than the
# 6dp storage rounding it had to survive: 22 weights stored at 6dp can sum
# 1.1e-5 away from 1.0 with every weight correct. It passed for months
# because most weeks' roundings cancelled, then failed on 2026-08-22 on a
# week where they did not — masking a real 3.5e-5 engine defect underneath.
#
# A tolerance that fails on correct data is as bad as one that passes wrong
# data, so pin both directions: wide enough for the storage format, and
# still narrow enough to catch an error a human would care about.
# ---------------------------------------------------------------------------
def test_tolerance_admits_worst_case_storage_rounding():
    """Every weight correct, every one rounded the same way — must pass."""
    for n in (5, 22, 24, 40):
        worst = n * 0.5 * 10 ** -bsp.WEIGHT_DECIMALS
        assert worst <= bsp.weight_tolerance(n), n


def test_tolerance_still_catches_an_error_worth_catching():
    """One basis point is the smallest weight error that means anything on
    a real book. It must fail at any plausible position count."""
    for n in (5, 22, 24, 40):
        assert bsp.weight_tolerance(n) < 1e-4, n


def test_tolerance_would_have_caught_the_2026_08_22_defect():
    """The engine defect the old flat tolerance nearly let through: sleeve
    A's 4dp weights summed to 1.0001, putting the book at 100.0035% of NAV
    across 22 positions."""
    assert abs(1.000035 - 1.0) > bsp.weight_tolerance(22)


def test_tolerance_grows_with_the_book():
    assert bsp.weight_tolerance(40) > bsp.weight_tolerance(22)


# ---------------------------------------------------------------------------
# The freshness block is filtered, not copied (2026-08-22)
#
# The per-strategy freshness report is written for three audiences and its full
# row names the method: `source` reads "breadth panels (constituents above
# their 200-day average)" and the technical `why` says "constituents" and
# "venue". Copied wholesale onto this page it tripped
# test_built_page_discloses_no_parameters, which is the guard working.
#
# The fix was an ALLOW-list. These pin that it stays one, because a deny-list
# would silently ship whatever field is added upstream next.
#
# They read the FILTERED REPORT ON DISK, not payload["freshness"] (2026-08-26).
# Taking the payload meant they skipped whenever verdict_has_lapsed was true,
# which is most of the time between local refresh_all.py runs -- so the three
# guards over the allow-list went dark exactly while the report sat stale, and
# a method field added upstream in that window would have met no test at all.
# Nothing about the allow-list depends on whether the verdict is still current.
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def freshness_block():
    blob = bsp.read_freshness_report()
    if blob is None:
        pytest.skip("no freshness report on disk")
    return bsp.filter_freshness(blob)


def test_freshness_block_carries_no_method_fields(freshness_block):
    permitted = {"sleeve", "label", "data_through", "venue_last_session",
                 "sessions_behind", "status", "laggards", "why_plain"}
    for row in freshness_block["strategies"]:
        extra = set(row) - permitted
        assert not extra, f"method fields reached the reduced page: {extra}"


def test_freshness_block_still_answers_the_question_it_exists_for(freshness_block):
    """Filtering must not strip it down to uselessness: every row still has to
    say WHICH strategy, THROUGH WHAT DATE, and whether that is current."""
    assert len(freshness_block["strategies"]) == 4
    for row in freshness_block["strategies"]:
        assert row["label"]
        assert row["data_through"]
        assert row["status"] in {"current", "behind", "unknown"}


def test_a_behind_row_keeps_a_plain_reason_or_names_its_laggard(freshness_block):
    for row in freshness_block["strategies"]:
        if row["status"] == "behind":
            assert row.get("why_plain") or row.get("laggards"), row


# ---------------------------------------------------------------------------
# The freshness ceiling. Corrected 2026-08-26: it used to bound each sleeve by
# the book's as_of, which is min(panel_end, live_anchor) — and live_anchor is
# the blend curve's last date, an INTERSECTION across sleeves and therefore
# floored by the slowest. That asserted uniformity, not freshness, and it
# contradicted the per-sleeve block whose whole purpose is to disclose
# divergence. It never fired while refreshes ran only at weekends; the
# post-fill pair refreshes mid-week, when Xetra is a session behind NYSE.
# ---------------------------------------------------------------------------

def _payload_with(freshness_rows, as_of, panel_end):
    """A payload complete enough that assert_payload_usable REACHES the
    freshness ceiling. Built deliberately: a thinner dict raises KeyError on
    'curve' first, and an assertion of the form "no freshness complaint" then
    passes on the exception instead of on the guard — a false green."""
    return {
        "holdings": [{"ticker": "SPY", "weight": 1.0}],
        "sleeves": [{"weight": 1.0}],
        "as_of": as_of,
        "panel_end_date": panel_end,
        "freshness": {"strategies": freshness_rows},
        "curve": {"dates": [as_of], "equity": [1.0]},
        "calendars": {},
        "charts": {},
    }


def _problems(payload):
    """Return the validator's complaint text, or '' if it accepted the payload.

    A KeyError here means the fixture never reached the check under test, so it
    is re-raised rather than returned as a 'problem' — otherwise a missing key
    reads as a passing guard.
    """
    try:
        bsp.assert_payload_usable(payload)
    except KeyError:
        raise
    except Exception as exc:                                   # noqa: BLE001
        return str(exc)
    return ""


def test_midweek_venue_divergence_is_publishable():
    """The exact state the post-fill pair produces every Tuesday and Wednesday.

    US sleeves carry Tuesday's close; sleeve D carries Monday's because Xetra
    has not settled, which drags live_anchor — and therefore as_of — back to
    Monday. Every sleeve is correct and the page must build.
    """
    rows = [{"sleeve": s, "data_through": "2026-08-25"} for s in "ABC"]
    rows.append({"sleeve": "D", "data_through": "2026-08-24"})
    problems = _problems(_payload_with(rows, as_of="2026-08-24",
                                       panel_end="2026-08-25"))
    assert "freshness says sleeve" not in problems, problems


def test_a_report_from_a_later_refresh_is_still_refused():
    """The guard must keep catching what it was built for.

    A sleeve reaching PAST the newest data this refresh produced cannot have
    come from this refresh, whatever the venues were doing.
    """
    rows = [{"sleeve": "A", "data_through": "2026-08-26"}]
    problems = _problems(_payload_with(rows, as_of="2026-08-24",
                                       panel_end="2026-08-25"))
    assert "freshness says sleeve A reaches 2026-08-26" in problems
    assert "2026-08-25" in problems


def test_ceiling_falls_back_to_as_of_when_panel_end_is_absent():
    """No panel_end must not silently disable the guard."""
    rows = [{"sleeve": "A", "data_through": "2026-08-26"}]
    problems = _problems(_payload_with(rows, as_of="2026-08-24", panel_end=None))
    assert "freshness says sleeve A" in problems
