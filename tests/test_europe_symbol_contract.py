"""The Europe sleeve's traded symbol, asserted across every surface.

Sleeve D's traded ticker is restated in four places that were held in sync
only by convention: the registry's ``yfinance_trading_proxy``, the daily
mark-to-market resolver, the holdings-price exporter's resolver, and the
exporter's static OHLC ticker list. Three of the four derived the symbol by
appending ".DE" to the registry key, which assumes the Xetra ticker equals
the key.

That assumption held for four of the five members and failed for the fifth.
Registry key EXH3 names the Industrial Goods & Services panel (iShares
product 251948), and that fund trades as EXH4.DE — EXH3.DE is the Food &
Beverage fund. So sleeve D signalled on industrials breadth, and the
backtest, the live book and the price export each independently priced a
food & beverage ETF. No test compared the surfaces, so nothing failed.

This module is the guard. It is the same shape as
``test_weights_contract.py``: every surface is asserted against ONE source
of truth — the registry — so a future member whose Xetra ticker differs
from its key fails here instead of shipping.

It is pure and offline. The behavioural counterpart, which checks that the
priced series actually moves with its own constituents, is
``scripts/check_pair_integrity.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from etf_registry import (  # noqa: E402
    ETF_REGISTRY,
    UNIVERSE_ETFS,
    UNIVERSE_EUROPE_SECTORS,
)
from export_holdings_prices import (  # noqa: E402
    INDIVIDUAL_OHLC_TICKERS,
    resolve_book_symbol,
)
from mark_to_market_live import _resolve_yf_symbol  # noqa: E402


def _proxy(key: str) -> str | None:
    return (ETF_REGISTRY.get(key) or {}).get("yfinance_trading_proxy")


# =========================================================================
# The registry is the single source of truth
# =========================================================================
@pytest.mark.parametrize("key", UNIVERSE_EUROPE_SECTORS)
def test_every_europe_member_records_a_trading_proxy(key):
    """Without a proxy the resolvers fall back to key + ".DE", which is the
    convention that failed. Every member must state its ticker explicitly.
    """
    assert _proxy(key), f"{key} has no yfinance_trading_proxy recorded"


@pytest.mark.parametrize("key", UNIVERSE_EUROPE_SECTORS)
def test_mark_to_market_resolves_to_the_registry_proxy(key):
    symbol, fx = _resolve_yf_symbol(key, ETF_REGISTRY)
    assert symbol == _proxy(key)
    assert fx == "eur_to_usd", f"{key} must be flagged for EUR conversion"


@pytest.mark.parametrize("key", UNIVERSE_EUROPE_SECTORS)
def test_holdings_exporter_resolves_to_the_registry_proxy(key):
    assert resolve_book_symbol(key) == _proxy(key)


@pytest.mark.parametrize("key", UNIVERSE_EUROPE_SECTORS)
def test_the_two_resolvers_agree(key):
    """The live book and the price export must never price different
    instruments for the same holding."""
    assert _resolve_yf_symbol(key, ETF_REGISTRY)[0] == resolve_book_symbol(key)


@pytest.mark.parametrize("key", UNIVERSE_EUROPE_SECTORS)
def test_the_static_ohlc_list_covers_every_traded_europe_symbol(key):
    """A fresh CI runner has no gitignored caches, so the static list is the
    floor. A traded symbol missing from it is a hole in the book's prices.
    """
    assert _proxy(key) in INDIVIDUAL_OHLC_TICKERS


# =========================================================================
# Regression pins that name the defect
# =========================================================================
def test_exh3_panel_prices_as_exh4():
    """Registry key EXH3 is the Industrial Goods & Services panel, and that
    fund's Xetra ticker is EXH4.DE.

    Evidence recorded in the registry entry: over 2024-08-01..2026-08-01,
    EXH4.DE correlates 0.973 with this panel's own constituents and EXH3.DE
    correlates 0.244, while EXH3.DE correlates 0.933 with food & beverage
    majors.
    """
    assert _proxy("EXH3") == "EXH4.DE"
    assert _resolve_yf_symbol("EXH3", ETF_REGISTRY)[0] == "EXH4.DE"
    assert resolve_book_symbol("EXH3") == "EXH4.DE"


def test_exh3_de_appears_nowhere_as_a_traded_symbol():
    """EXH3.DE is a food & beverage fund and is not in any sleeve."""
    assert "EXH3.DE" not in INDIVIDUAL_OHLC_TICKERS
    traded = {_proxy(k) for k in UNIVERSE_EUROPE_SECTORS}
    assert "EXH3.DE" not in traded


def test_the_constituent_source_still_points_at_the_industrials_product():
    """The fetch URL must not be 'corrected' alongside the ticker.

    The constituents were always right — product 251948 is Industrial
    Goods & Services — and the endpoint accepts fileName=EXH3_holdings for
    it. Only the traded ticker was wrong. Changing the URL would break a
    working fetch and swap which sector the sleeve tracks.
    """
    entry = ETF_REGISTRY["EXH3"]
    assert "industrial-goods-services" in entry["url_slug"]
    assert entry["product_id"] == "251948"
    assert entry["filename"] == "EXH3_holdings"


# =========================================================================
# The same contract for sleeve A, which shares the resolvers
# =========================================================================
@pytest.mark.parametrize("key", UNIVERSE_ETFS)
def test_sleeve_a_resolvers_agree_with_the_registry(key):
    proxy = _proxy(key) or key
    assert _resolve_yf_symbol(key, ETF_REGISTRY)[0] == proxy
    assert resolve_book_symbol(key) == proxy


def test_no_two_europe_members_share_a_traded_symbol():
    """A duplicated proxy would double-count one sector and drop another —
    the failure mode a copy-paste of these entries produces."""
    traded = [_proxy(k) for k in UNIVERSE_EUROPE_SECTORS]
    assert len(traded) == len(set(traded)), f"duplicate Europe proxies: {traded}"


# =========================================================================
# Surface 5: the dashboard payload (2026-08-08)
# =========================================================================
# The Monitor tab resolved a row's traded ticker from ma200.monitor, which
# carries only the 14 Strategy A members, and fell back to the PANEL KEY for
# every other sleeve. So the Europe rows asked for prices under "EXV1" when
# the series is keyed "EXV1.DE" — hasChart was false and they rendered with
# no expander, which is how this was noticed. For EXH3 the fallback was not
# merely suffix-less but wrong: it trades as EXH4.DE.
#
# pipeline.py now emits data["trading_proxies"] straight from the registry.
# These pin that map so a sixth surface cannot drift the same way.

def _payload_proxies() -> dict:
    """The map pipeline.py emits, built the same way it builds it."""
    return {etf: (cfg.get("yfinance_trading_proxy") or etf)
            for etf, cfg in ETF_REGISTRY.items()}


@pytest.mark.parametrize("key", UNIVERSE_EUROPE_SECTORS)
def test_dashboard_payload_carries_the_registry_proxy(key):
    assert _payload_proxies()[key] == _proxy(key)


@pytest.mark.parametrize("key", UNIVERSE_EUROPE_SECTORS)
def test_dashboard_payload_never_falls_back_to_the_panel_key(key):
    """The bare key is not a tradable symbol for any Europe member. If this
    fails, the registry lost a proxy and the payload silently reverted to
    the convention that shipped EXH3/EXH4."""
    assert _payload_proxies()[key] != key


def test_dashboard_payload_prices_exh3_as_exh4():
    """The specific trap, asserted on this surface too."""
    assert _payload_proxies()["EXH3"] == "EXH4.DE"


def test_dashboard_payload_covers_every_registered_panel():
    """Every panel, not just the traded ones. A candidate supersector opened
    from the Data tab needs a resolvable symbol as much as a deployed one."""
    proxies = _payload_proxies()
    missing = [e for e in ETF_REGISTRY if e not in proxies]
    assert not missing, f"panels absent from the payload map: {missing}"
    assert all(proxies.values()), "a panel resolved to an empty symbol"


def test_pipeline_emits_the_proxy_map():
    """Guard the wiring, not just the shape: if the key is renamed or the
    emit is dropped, the dashboard silently reverts to the panel key."""
    src = (ROOT / "scripts" / "pipeline.py").read_text(encoding="utf-8")
    assert '"trading_proxies"' in src, (
        "pipeline.py no longer emits trading_proxies; the dashboard would "
        "fall back to the panel key for every non-Strategy-A sleeve"
    )
