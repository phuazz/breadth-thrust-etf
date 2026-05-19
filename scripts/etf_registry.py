"""Registry of supported ETFs for the breadth-thrust pipeline.

Each entry defines:
  - symbol          : the ETF ticker (matches yfinance for the parent ETF too)
  - ishares_region  : "us" or "uk" — controls which iShares endpoint we hit
  - product_id      : numeric path component in the iShares product URL
  - url_slug        : human-readable slug in the iShares product URL
  - ajax_id         : the funny number before .ajax in the holdings endpoint
  - filename        : the value of the fileName query param (XYZ_holdings)
  - csv_url_template: full URL template; %s placeholder for asOfDate=YYYYMMDD
  - start_friday    : earliest Friday for which the iShares endpoint returns
                      a populated CSV (validated by probing). Anything before
                      this date returns the empty 'Fund Holdings as of "-"'
                      template.
  - ticker_overrides: dict mapping iShares-CSV ticker to yfinance ticker
                      for share-class quirks (e.g. BRKB -> BRK-B).

Note on iShares US vs UK:
  - The US endpoint pattern is /us/products/<pid>/<slug>/<ajax_id>.ajax
  - The UK endpoint pattern is /uk/individual/en/products/<pid>/<slug>/<ajax_id>.ajax
  - The US endpoint is currently blocked by Akamai bot defence (verified
    2026-05-16). The UK endpoint is NOT blocked and is used for CSP1.
  - Cached SOXX data from before the US block (~2018-2026) remains valid;
    re-running fetch will hit the cache and not need to re-fetch.
"""

from __future__ import annotations

from datetime import date


ETF_REGISTRY: dict[str, dict] = {
    "SOXX": {
        "symbol": "SOXX",
        "ishares_region": "us",
        "product_id": "239705",
        "url_slug": "ishares-phlx-semiconductor-etf",
        "ajax_id": "1467271812596",
        "filename": "SOXX_holdings",
        # Format: ...ajax?fileType=csv&fileName=...&dataType=fund&asOfDate=YYYYMMDD
        "csv_url_template": (
            "https://www.ishares.com/us/products/239705/"
            "ishares-phlx-semiconductor-etf/1467271812596.ajax"
            "?fileType=csv&fileName=SOXX_holdings&dataType=fund"
        ),
        # Earliest date probed to return data (US endpoint cached prior to block).
        "start_friday": date(2018, 1, 5),
        "ticker_overrides": {},
        # Date format in the CSV preamble: "Jun 28, 2024"
        "csv_date_format": "us",
    },
    "CSP1": {
        # iShares Core S&P 500 UCITS ETF (Acc) — Irish-domiciled UCITS that
        # full-replicates the S&P 500. Returns the same ~500 US-listed
        # constituents as IVV / SPY. Tickers in the CSV are stripped of share-
        # class dots (BRK.B -> BRKB, BF.B -> BFB) and need mapping for yfinance.
        "symbol": "CSP1",
        "ishares_region": "uk",
        "product_id": "253743",
        "url_slug": "ishares-vii-plc-ishares-core-sp-500-ucits-etf-acc-fund",
        "ajax_id": "1506575576011",
        "filename": "CSP1_holdings",
        "csv_url_template": (
            "https://www.ishares.com/uk/individual/en/products/253743/"
            "ishares-vii-plc-ishares-core-sp-500-ucits-etf-acc-fund/"
            "1506575576011.ajax"
            "?fileType=csv&fileName=CSP1_holdings&dataType=fund"
        ),
        # Probed: 2014-06-30 OK, 2012-06-29 empty. Bisect not done — set
        # conservative 2014-01 start; fetch will gracefully skip empty dates.
        # Match SOXX's start (2018-01-05) for apples-to-apples OOS comparison.
        # CSP1 data is available back to 2014 if a longer window is wanted.
        "start_friday": date(2018, 1, 5),
        # iShares UK strips share-class dots in modern files (BRKB, BFB) but
        # leaves them in older files (BRK.B, BF.B). Map both forms.
        "ticker_overrides": {
            "BRKB": "BRK-B",
            "BRK.B": "BRK-B",
            "BFB": "BF-B",
            "BF.B": "BF-B",
            "BRKA": "BRK-A",
            "BRK.A": "BRK-A",
        },
        # Date format in CSV preamble: "28/Jun/2024"
        "csv_date_format": "uk",
        # When backtesting, trade the US-listed equivalent in USD for liquidity.
        "yfinance_trading_proxy": "SPY",
    },
    # --- iShares UK S&P 500 sector slice UCITS funds --------------------------
    # Each fund tracks the relevant S&P 500 sector index. Probed 2018-01-05
    # populated, 2017 empty (same gap as CSP1).
    "IUES": {
        "symbol": "IUES",
        "ishares_region": "uk",
        "product_id": "280503",
        "url_slug": "ishares-sp-500-energy-sector-ucits-etf",
        "ajax_id": "1506575576011",
        "filename": "IUES_holdings",
        "csv_url_template": (
            "https://www.ishares.com/uk/individual/en/products/280503/"
            "ishares-sp-500-energy-sector-ucits-etf/1506575576011.ajax"
            "?fileType=csv&fileName=IUES_holdings&dataType=fund"
        ),
        "start_friday": date(2018, 1, 5),
        "ticker_overrides": {
            "BRKB": "BRK-B", "BRK.B": "BRK-B",
            "BFB": "BF-B", "BF.B": "BF-B",
        },
        "csv_date_format": "uk",
        # Trade SPDR XLE (Energy Select Sector SPDR) as the US-listed proxy.
        "yfinance_trading_proxy": "XLE",
    },
    "IUFS": {
        "symbol": "IUFS",
        "ishares_region": "uk",
        "product_id": "280523",
        "url_slug": "ishares-sp-500-financials-sector-ucits-etf",
        "ajax_id": "1506575576011",
        "filename": "IUFS_holdings",
        "csv_url_template": (
            "https://www.ishares.com/uk/individual/en/products/280523/"
            "ishares-sp-500-financials-sector-ucits-etf/1506575576011.ajax"
            "?fileType=csv&fileName=IUFS_holdings&dataType=fund"
        ),
        "start_friday": date(2018, 1, 5),
        "ticker_overrides": {
            "BRKB": "BRK-B", "BRK.B": "BRK-B",
            "BFB": "BF-B", "BF.B": "BF-B",
            "BRKA": "BRK-A", "BRK.A": "BRK-A",
        },
        "csv_date_format": "uk",
        "yfinance_trading_proxy": "XLF",  # SPDR Financial Select Sector
    },
    "CNDX": {
        "symbol": "CNDX",
        "ishares_region": "uk",
        "product_id": "253741",
        "url_slug": "ishares-nasdaq-100-ucits-etf",
        "ajax_id": "1506575576011",
        "filename": "CNDX_holdings",
        "csv_url_template": (
            "https://www.ishares.com/uk/individual/en/products/253741/"
            "ishares-nasdaq-100-ucits-etf/1506575576011.ajax"
            "?fileType=csv&fileName=CNDX_holdings&dataType=fund"
        ),
        "start_friday": date(2018, 1, 5),
        "ticker_overrides": {},
        "csv_date_format": "uk",
        "yfinance_trading_proxy": "QQQ",  # Invesco QQQ Trust
    },
    # --- Six additional S&P 500 sector slice UCITS funds (Phase 3) -----------
    # All track the relevant S&P 500 sector. Trading proxies are the SPDR
    # Select Sector ETFs (US-listed, USD, deep liquidity).
    "IUIT": {  # Information Technology
        "symbol": "IUIT",
        "ishares_region": "uk",
        "product_id": "280510",
        "url_slug": "ishares-sp-500-information-technology-sector-ucits-etf",
        "ajax_id": "1506575576011",
        "filename": "IUIT_holdings",
        "csv_url_template": (
            "https://www.ishares.com/uk/individual/en/products/280510/"
            "ishares-sp-500-information-technology-sector-ucits-etf/"
            "1506575576011.ajax"
            "?fileType=csv&fileName=IUIT_holdings&dataType=fund"
        ),
        "start_friday": date(2018, 1, 5),
        "ticker_overrides": {"BRKB": "BRK-B", "BRK.B": "BRK-B"},
        "csv_date_format": "uk",
        "yfinance_trading_proxy": "XLK",
    },
    "IUHC": {  # Health Care
        "symbol": "IUHC",
        "ishares_region": "uk",
        "product_id": "280507",
        "url_slug": "ishares-sp-500-health-care-sector-ucits-etf",
        "ajax_id": "1506575576011",
        "filename": "IUHC_holdings",
        "csv_url_template": (
            "https://www.ishares.com/uk/individual/en/products/280507/"
            "ishares-sp-500-health-care-sector-ucits-etf/1506575576011.ajax"
            "?fileType=csv&fileName=IUHC_holdings&dataType=fund"
        ),
        "start_friday": date(2018, 1, 5),
        "ticker_overrides": {},
        "csv_date_format": "uk",
        "yfinance_trading_proxy": "XLV",
    },
    "IUIS": {  # Industrials
        "symbol": "IUIS",
        "ishares_region": "uk",
        "product_id": "287109",
        "url_slug": "ishares-s-p-500-industrials-sector-ucits-etf-fund",
        "ajax_id": "1506575576011",
        "filename": "IUIS_holdings",
        "csv_url_template": (
            "https://www.ishares.com/uk/individual/en/products/287109/"
            "ishares-s-p-500-industrials-sector-ucits-etf-fund/"
            "1506575576011.ajax"
            "?fileType=csv&fileName=IUIS_holdings&dataType=fund"
        ),
        "start_friday": date(2018, 1, 5),
        "ticker_overrides": {},
        "csv_date_format": "uk",
        "yfinance_trading_proxy": "XLI",
    },
    "IUCS": {  # Consumer Staples
        "symbol": "IUCS",
        "ishares_region": "uk",
        "product_id": "287102",
        "url_slug": "ishares-s-p-500-consumer-staples-sector-ucits-etf-fund",
        "ajax_id": "1506575576011",
        "filename": "IUCS_holdings",
        "csv_url_template": (
            "https://www.ishares.com/uk/individual/en/products/287102/"
            "ishares-s-p-500-consumer-staples-sector-ucits-etf-fund/"
            "1506575576011.ajax"
            "?fileType=csv&fileName=IUCS_holdings&dataType=fund"
        ),
        "start_friday": date(2018, 1, 5),
        "ticker_overrides": {},
        "csv_date_format": "uk",
        "yfinance_trading_proxy": "XLP",
    },
    "IUCD": {  # Consumer Discretionary
        "symbol": "IUCD",
        "ishares_region": "uk",
        "product_id": "280526",
        "url_slug": "ishares-sp-500-consumer-discretionary-sector-ucits-etf",
        "ajax_id": "1506575576011",
        "filename": "IUCD_holdings",
        "csv_url_template": (
            "https://www.ishares.com/uk/individual/en/products/280526/"
            "ishares-sp-500-consumer-discretionary-sector-ucits-etf/"
            "1506575576011.ajax"
            "?fileType=csv&fileName=IUCD_holdings&dataType=fund"
        ),
        "start_friday": date(2018, 1, 5),
        "ticker_overrides": {},
        "csv_date_format": "uk",
        "yfinance_trading_proxy": "XLY",
    },
    "IUUS": {  # Utilities
        "symbol": "IUUS",
        "ishares_region": "uk",
        "product_id": "287115",
        "url_slug": "ishares-s-p-500-utilities-sector-ucits-etf-fund",
        "ajax_id": "1506575576011",
        "filename": "IUUS_holdings",
        "csv_url_template": (
            "https://www.ishares.com/uk/individual/en/products/287115/"
            "ishares-s-p-500-utilities-sector-ucits-etf-fund/1506575576011.ajax"
            "?fileType=csv&fileName=IUUS_holdings&dataType=fund"
        ),
        "start_friday": date(2018, 1, 5),
        "ticker_overrides": {},
        "csv_date_format": "uk",
        "yfinance_trading_proxy": "XLU",
    },
}


def get_etf(symbol: str) -> dict:
    """Look up an ETF config by symbol, with helpful error if missing."""
    sym = symbol.upper()
    if sym not in ETF_REGISTRY:
        raise KeyError(
            f"ETF {sym!r} not in registry. Known: {sorted(ETF_REGISTRY.keys())}. "
            "Add an entry to scripts/etf_registry.py to extend."
        )
    return ETF_REGISTRY[sym]
