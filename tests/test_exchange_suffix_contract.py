"""The iShares Exchange column, pinned spelling by spelling.

``_resolve_yf_symbol`` maps a holding's venue to a yfinance suffix, and its
final branch assumes US when the venue is not in the map. That branch is
silent by construction: a non-US holding with an unrecognised venue keeps
its bare local code, is treated as a US ticker, resolves at no vendor, and
leaves BOTH the numerator and the denominator of breadth. The figure stays
plausible while measuring a smaller universe, so nothing looks wrong.

Two live examples, both found by hand rather than by the pipeline:

  - "Bse Ltd" (the map carried "Bombay Stock Exchange") put two NDIA names
    into the roster as bare numeric scrip codes for their whole tenure.
  - "Gretai Securities Market" — the Taipei Exchange, under its pre-2015
    name — did the same to 7 of ITWN's 78 equity rows, 9.0% of the roster
    by count and 3.05% by weight, on every one of the 451 roster-days from
    2018-01-05 onward.

So this module pins the venue spellings actually observed in the cached
payloads to the suffix each must produce. A vendor renaming a venue, or
introducing a new one, fails here rather than quietly degrading coverage.

The spellings below were swept from all 10,927 non-US roster-days in
data/raw_ishares (2018-01-05 .. 2026-08-13) on 2026-08-15. That cache is
gitignored, so the sweep result is inlined here rather than recomputed —
this is a contract test, and the contract is the point.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from compute_breadth import (  # noqa: E402
    _YF_EXCHANGE_SUFFIXES,
    normalise_for_yfinance,
)
from fetch_constituents import (  # noqa: E402
    _EXCHANGE_ROUTE_UNAVAILABLE,
    _EXCHANGE_TO_YF_SUFFIX,
    UNMAPPED_EXCHANGE_MAX_SHARE,
    UnmappedExchangeError,
    _resolve_yf_symbol,
    report_unmapped_exchanges,
)

# Every Exchange spelling observed in the cache, with the suffix it must
# resolve to and a representative ticker from a real payload row.
# (exchange, raw ticker, expected yfinance symbol)
OBSERVED_VENUES: list[tuple[str, str, str]] = [
    # --- United Kingdom & Ireland -------------------------------------
    ("London Stock Exchange", "HSBA", "HSBA.L"),
    ("Irish Stock Exchange - All Market", "KRZ", "KRZ.IR"),
    # --- Continental Europe -------------------------------------------
    ("Xetra", "SAP", "SAP.DE"),
    ("Deutsche Boerse Xetra", "SAP", "SAP.DE"),
    ("Hanseatische Wertpapierboerse Hamburg", "ABC", "ABC.HM"),
    ("Boerse Duesseldorf", "DGW2", "DGW2.DU"),
    ("Boerse Muenchen", "BAYR", "BAYR.MU"),
    ("Nyse Euronext - Euronext Paris", "BNP", "BNP.PA"),
    ("Borsa Italiana", "ENI", "ENI.MI"),
    ("Euronext Amsterdam", "INGA", "INGA.AS"),
    ("Nyse Euronext - Euronext Brussels", "UCB", "UCB.BR"),
    ("Nyse Euronext - Euronext Lisbon", "EDP", "EDP.LS"),
    ("Bolsa De Madrid", "SAN", "SAN.MC"),
    ("Bme Bolsas Y Mercados Espanoles", "SAN", "SAN.MC"),
    ("SIX Swiss Exchange", "NESN", "NESN.SW"),
    ("Six Swiss Exchange Ag", "NESN", "NESN.SW"),
    ("Nasdaq Omx Helsinki Ltd.", "NOKIA", "NOKIA.HE"),
    ("Omx Nordic Exchange Copenhagen A/S", "NOVO B", "NOVO-B.CO"),
    ("Oslo Bors Asa", "EQNR", "EQNR.OL"),
    ("Wiener Boerse Ag", "OMV", "OMV.VI"),
    ("Warsaw Stock Exchange/Equities/Main Market", "PKO", "PKO.WA"),
    ("Prague Stock Exchange", "CEZ", "CEZ.PR"),
    # --- Asia ----------------------------------------------------------
    ("Tokyo Stock Exchange", "6592", "6592.T"),
    ("Hong Kong Exchanges And Clearing Ltd", "700", "700.HK"),
    ("Shanghai Stock Exchange", "600519", "600519.SS"),
    ("Shenzhen Stock Exchange", "000858", "000858.SZ"),
    ("National Stock Exchange Of India", "INFY", "INFY.NS"),
    ("Taiwan Stock Exchange", "2330", "2330.TW"),
    # The Taipei Exchange under its pre-2015 name. .TWO, never .TW —
    # every currently-listed ITWN name on this venue 404s under .TW.
    ("Gretai Securities Market", "6488", "6488.TWO"),
    # --- Africa ---------------------------------------------------------
    ("Johannesburg Stock Exchange", "NPN", "NPN.JO"),
    # --- US (no suffix) -------------------------------------------------
    ("Nasdaq", "AAPL", "AAPL"),
    ("NASDAQ", "PDD", "PDD"),
    ("New York Stock Exchange Inc.", "JPM", "JPM"),
    ("Non-Nms Quotation Service (Nnqs)", "BCAUY", "BCAUY"),
]


@pytest.mark.parametrize("exchange,raw,expected", OBSERVED_VENUES,
                         ids=[f"{e}|{t}" for e, t, _ in OBSERVED_VENUES])
def test_observed_exchange_spelling_resolves(exchange, raw, expected):
    """Each venue spelling seen in the cache maps to its verified suffix."""
    assert _resolve_yf_symbol(raw, exchange) == expected


@pytest.mark.parametrize("exchange,raw,expected", OBSERVED_VENUES,
                         ids=[f"{e}|{t}" for e, t, _ in OBSERVED_VENUES])
def test_resolved_symbol_survives_normalisation(exchange, raw, expected):
    """The downstream share-class rule must not undo the suffix.

    compute_breadth.normalise_for_yfinance re-splits on the last dot and
    rewrites the tail to a dash unless the suffix is in its own set. That
    set is a second, independent copy of this knowledge: .TWO was absent
    from it, so 6488.TWO would have become 6488-TWO and died exactly where
    the missing map entry had left it.
    """
    assert normalise_for_yfinance(expected) == expected


def test_suffix_sets_agree_across_modules():
    """Every suffix the resolver can emit is one normalise_for_yfinance keeps."""
    emitted = {s.lstrip(".") for s in _EXCHANGE_TO_YF_SUFFIX.values() if s}
    missing = sorted(emitted - _YF_EXCHANGE_SUFFIXES)
    assert not missing, (
        f"_EXCHANGE_TO_YF_SUFFIX can emit {missing}, which "
        f"compute_breadth._YF_EXCHANGE_SUFFIXES does not recognise; those "
        f"symbols would be rewritten to a dash form and resolve at no vendor"
    )


def test_bse_is_recognised_but_deliberately_unrouted():
    """"Bse Ltd" must not silently acquire a .BO route.

    Probed 2026-08-15: Yahoo 404s on 4 of 10 BSE scrip codes tested (TCS,
    HDFC Bank, ICICI Bank, Hindustan Aeronautics), and where it does serve
    the line it ships malformed metadata that makes yfinance raise TypeError
    at every horizon of three months or more for most names. A 200-day
    breadth panel cannot be built on that. Resolution is the issuer's NSE
    line via YF_TICKER_OVERRIDES, so the row must still emerge as the raw
    scrip code for that override to catch it.
    """
    assert "Bse Ltd" in _EXCHANGE_ROUTE_UNAVAILABLE
    assert "Bse Ltd" not in _EXCHANGE_TO_YF_SUFFIX
    assert _resolve_yf_symbol("534091", "Bse Ltd") == "534091"
    # And it must not be reported as an undiscovered venue.
    sink: dict[str, list[str]] = {}
    _resolve_yf_symbol("532483", "Bse Ltd", unmapped=sink)
    assert sink == {}


def test_unknown_exchange_is_recorded_not_swallowed():
    """A venue absent from every table lands in the sink, named."""
    sink: dict[str, list[str]] = {}
    assert _resolve_yf_symbol("1234", "Nagoya Stock Exchange",
                              unmapped=sink) == "1234"
    assert sink == {"Nagoya Stock Exchange": ["1234"]}


def test_unmapped_exchange_raises_above_threshold():
    """Past the bound it is an error, not a log line."""
    sink = {"Gretai Securities Market": ["6488", "5347", "5274"]}
    with pytest.raises(UnmappedExchangeError, match="Gretai"):
        report_unmapped_exchanges(sink, "ITWN", n_equity_rows=78)


def test_unmapped_exchange_tolerates_isolated_artefacts():
    """One stray row in a large roster warns and does not raise.

    The cache carries a handful of one-off corporate-action rows — Bloomberg
    placeholder tickers on venues like 'EUF' and 'QMH' that appear for a
    Friday or two and vanish. Failing the roster build over those would make
    the guard the first thing anyone switched off.
    """
    sink = {"QMH": ["CFRAO"]}
    report_unmapped_exchanges(sink, "EXH7", n_equity_rows=400)


def test_threshold_bounds_are_not_vacuous():
    """A bound of 0 would never fire; a bound of 1 would fire on nothing."""
    assert 0 < UNMAPPED_EXCHANGE_MAX_SHARE < 1
    # The ITWN gap this guard exists for was 7/78 = 9.0%.
    assert UNMAPPED_EXCHANGE_MAX_SHARE < 7 / 78


def test_strict_false_reports_without_raising():
    """Historical re-parses can opt out of the failure, never out of the report."""
    sink = {"Gretai Securities Market": ["6488", "5347", "5274"]}
    report_unmapped_exchanges(sink, "ITWN", n_equity_rows=78, strict=False)
