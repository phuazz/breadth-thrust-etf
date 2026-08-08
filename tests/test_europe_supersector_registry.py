"""Registry integrity for the STOXX Europe 600 supersector expansion.

Fourteen supersectors were added in Phase 27 (2026-08-07) to capture their
point-in-time breadth history. They are DATA ONLY: no deployed universe
includes them, and this module asserts that stays true.

The other half of this file guards the naming trap. The registry key
``EXH3`` holds Industrial Goods & Services (product 251948, traded as
EXH4.DE) for historical reasons documented in the registry. The fund whose
Xetra ticker actually is EXH3 — Food & Beverage, product 251944 — is keyed
``EXFB``. Adding it under its natural ticker would have silently overwritten
the industrials panel, which is the same class of bug as the 2026-08-03
EXH3/EXH4 correction that ``test_europe_symbol_contract.py`` guards.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import fetch_constituents as fc  # noqa: E402
from etf_registry import (  # noqa: E402
    ETF_REGISTRY,
    EUROPE_SUPERSECTORS_CANDIDATE,
    UNIVERSE_EUROPE_SECTORS,
    UNIVERSE_GLOBAL,
)

# portfolioId -> (registry key, Xetra ticker) as read from the product-data
# API's fundHeader component on 2026-08-07. These are transcribed from the
# upstream response, not inferred from the ticker.
EXPECTED_PRODUCT_IDS = {
    "EXV5": "251932", "EXV6": "251936", "EXV7": "251938", "EXV8": "251940",
    "EXH2": "251942", "EXFB": "251944", "EXV4": "251946", "EXH5": "251950",
    "EXH6": "251952", "EXH7": "251956", "EXI5": "251958", "EXH8": "251959",
    "EXV2": "251963", "EXV9": "251965",
}


def test_all_fourteen_registered():
    assert len(EUROPE_SUPERSECTORS_CANDIDATE) == 14
    assert set(EUROPE_SUPERSECTORS_CANDIDATE) <= set(ETF_REGISTRY)


@pytest.mark.parametrize("symbol", EUROPE_SUPERSECTORS_CANDIDATE)
def test_product_id_matches_the_swept_value(symbol):
    """A wrong portfolioId silently fetches a DIFFERENT FUND's holdings.

    That is not hypothetical: it is exactly how sleeve D came to signal on
    industrials breadth while holding a food & beverage ETF.
    """
    assert ETF_REGISTRY[symbol]["product_id"] == EXPECTED_PRODUCT_IDS[symbol]


def test_product_ids_are_unique_across_the_whole_registry():
    """Two keys pointing at one fund would make their panels identical."""
    seen: dict[str, str] = {}
    for symbol, cfg in ETF_REGISTRY.items():
        pid = cfg["product_id"]
        assert pid not in seen, (
            f"{symbol} and {seen[pid]} share product_id {pid}"
        )
        seen[pid] = symbol


def test_exh3_key_is_industrials_and_food_beverage_is_exfb():
    """The naming trap, asserted in both directions."""
    # EXH3 the KEY is the industrials panel, priced as EXH4.DE.
    assert ETF_REGISTRY["EXH3"]["product_id"] == "251948"
    assert ETF_REGISTRY["EXH3"]["yfinance_trading_proxy"] == "EXH4.DE"
    # Food & Beverage is keyed EXFB but genuinely trades as EXH3.DE.
    assert ETF_REGISTRY["EXFB"]["product_id"] == "251944"
    assert ETF_REGISTRY["EXFB"]["yfinance_trading_proxy"] == "EXH3.DE"
    # The two must never collapse onto one panel.
    assert (ETF_REGISTRY["EXH3"]["product_id"]
            != ETF_REGISTRY["EXFB"]["product_id"])


@pytest.mark.parametrize("symbol", EUROPE_SUPERSECTORS_CANDIDATE)
def test_europe_entries_carry_the_europe_conventions(symbol):
    cfg = ETF_REGISTRY[symbol]
    assert cfg["symbol"] == symbol
    assert cfg["ishares_region"] == "uk"
    assert cfg["apply_exchange_suffix"] is True, (
        "European constituents need Exchange-based yfinance suffixes; "
        "without this they resolve as bare US tickers"
    )
    assert cfg["trading_calendar"] == "XETR"
    assert cfg["start_friday"] == date(2018, 1, 5)
    assert cfg["yfinance_trading_proxy"].endswith(".DE")


@pytest.mark.parametrize("symbol", EUROPE_SUPERSECTORS_CANDIDATE)
def test_candidates_are_not_in_any_deployed_universe(symbol):
    """Capturing data must not change what the strategies trade."""
    assert symbol not in UNIVERSE_EUROPE_SECTORS
    assert symbol not in UNIVERSE_GLOBAL


def test_deployed_europe_sleeve_is_unchanged():
    """The expansion must not have disturbed sleeve D's membership."""
    assert UNIVERSE_EUROPE_SECTORS == ["EXV1", "EXH1", "EXV3", "EXH3", "EXH9"]


@pytest.mark.parametrize("symbol", EUROPE_SUPERSECTORS_CANDIDATE)
def test_request_params_build_without_legacy_csv_fields(symbol):
    """These entries deliberately omit url_slug / ajax_id / filename /
    csv_url_template — the retired CSV route. Fetching must not need them."""
    cfg = ETF_REGISTRY[symbol]
    assert "csv_url_template" not in cfg
    params = fc.product_data_params(date(2018, 1, 5), cfg)
    assert params["portfolioId"] == EXPECTED_PRODUCT_IDS[symbol]
    assert params["targetSite"] == "ishares-uk"
    assert params["asOfDate"] == "20180105"


@pytest.mark.parametrize("symbol", EUROPE_SUPERSECTORS_CANDIDATE)
def test_legacy_csv_fetch_fails_loudly_for_new_funds(symbol):
    """They have no cached CSV history, so the legacy path must raise a
    clear error rather than a KeyError deep in a URL format string."""
    with pytest.raises(fc.EndpointUnavailable, match="no csv_url_template"):
        fc.fetch_with_retry(date(2018, 1, 5), ETF_REGISTRY[symbol])
