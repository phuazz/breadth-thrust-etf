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
    # --- Phase 1 expansion (2026-05-22): 3 missing S&P 500 sectors +
    #     S&P SmallCap 600. Brings universe from 11 → 15 ETFs.
    # -------------------------------------------------------------------------
    "IUMS": {  # S&P 500 Materials
        "symbol": "IUMS",
        "ishares_region": "uk",
        "product_id": "287104",
        "url_slug": "ishares-s-p-500-materials-sector-ucits-etf-fund",
        "ajax_id": "1506575576011",
        "filename": "IUMS_holdings",
        "csv_url_template": (
            "https://www.ishares.com/uk/individual/en/products/287104/"
            "ishares-s-p-500-materials-sector-ucits-etf-fund/1506575576011.ajax"
            "?fileType=csv&fileName=IUMS_holdings&dataType=fund"
        ),
        "start_friday": date(2018, 1, 5),
        "ticker_overrides": {},
        "csv_date_format": "uk",
        "yfinance_trading_proxy": "XLB",  # SPDR Materials Select Sector
    },
    "IUCM": {  # S&P 500 Communication Services
        # Note: launched 2018 with the GICS sector reclassification; iShares
        # UK fund inception around 2018-12. start_friday backdated to
        # 2018-01-05 for symmetry; warm-up months will return empty CSVs
        # and the fetch script handles that via carry-forward.
        "symbol": "IUCM",
        "ishares_region": "uk",
        "product_id": "304659",
        "url_slug": "ishares-s-p-500-communication-sector-ucits-etf-usd-acc-fund",
        "ajax_id": "1506575576011",
        "filename": "IUCM_holdings",
        "csv_url_template": (
            "https://www.ishares.com/uk/individual/en/products/304659/"
            "ishares-s-p-500-communication-sector-ucits-etf-usd-acc-fund/"
            "1506575576011.ajax"
            "?fileType=csv&fileName=IUCM_holdings&dataType=fund"
        ),
        "start_friday": date(2018, 1, 5),
        "ticker_overrides": {},
        "csv_date_format": "uk",
        "yfinance_trading_proxy": "XLC",  # SPDR Communication Select Sector
    },
    "IUSP": {  # US Property Yield (REIT proxy for Real Estate sector)
        # CAVEAT: tracks FTSE EPRA NAREIT US Dividend+ index (~38 US REITs),
        # NOT S&P 500 Real Estate. Broader REIT universe with a dividend
        # tilt — different index methodology than the other sectors. The
        # breadth signal still works because it is constituent-relative
        # (% above 200d MA), but the constituent set will not exactly
        # match the S&P 500 Real Estate sub-sector. No S&P 500 Real Estate
        # UCITS fund exists on iShares UK as of 2026-05.
        "symbol": "IUSP",
        "ishares_region": "uk",
        "product_id": "251803",
        "url_slug": "ishares-us-property-yield-ucits-etf",
        "ajax_id": "1506575576011",
        "filename": "IUSP_holdings",
        "csv_url_template": (
            "https://www.ishares.com/uk/individual/en/products/251803/"
            "ishares-us-property-yield-ucits-etf/1506575576011.ajax"
            "?fileType=csv&fileName=IUSP_holdings&dataType=fund"
        ),
        "start_friday": date(2018, 1, 5),
        "ticker_overrides": {},
        "csv_date_format": "uk",
        "yfinance_trading_proxy": "XLRE",  # SPDR Real Estate Select Sector
    },
    # =====================================================================
    # PHASE 4 (2026-05-23): non-US universes for constituent-breadth signal
    # =====================================================================
    # 5 Stoxx Europe 600 sector UCITS + 4 country UCITS. All iShares UK,
    # CSV format same as US sector funds, BUT constituents trade on European
    # / Asian exchanges so apply_exchange_suffix=True triggers the per-row
    # Exchange-to-yfinance-suffix resolver in fetch_constituents.
    # Trading proxies for deployment: the ETF itself on Xetra/LSE (EUR-priced)
    # or a US-listed equivalent where one exists.
    # ---------------------------------------------------------------------
    "EXV1": {  # Stoxx Europe 600 Banks (EUR, Xetra-listed)
        "symbol": "EXV1",
        "ishares_region": "uk",
        "product_id": "251934",
        "url_slug": "ishares-stoxx-europe-600-banks-ucits-etf-de-fund",
        "ajax_id": "1506575576011",
        "filename": "EXV1_holdings",
        "csv_url_template": (
            "https://www.ishares.com/uk/individual/en/products/251934/"
            "ishares-stoxx-europe-600-banks-ucits-etf-de-fund/1506575576011.ajax"
            "?fileType=csv&fileName=EXV1_holdings&dataType=fund"
        ),
        "start_friday": date(2018, 1, 5),
        "ticker_overrides": {},
        "csv_date_format": "uk",
        "apply_exchange_suffix": True,
        "yfinance_trading_proxy": "EXV1.DE",  # trade the ETF on Xetra in EUR
    },
    "EXH1": {  # Stoxx Europe 600 Oil & Gas
        "symbol": "EXH1",
        "ishares_region": "uk",
        "product_id": "251954",
        "url_slug": "ishares-stoxx-europe-600-oil-gas-ucits-etf-de-fund",
        "ajax_id": "1506575576011",
        "filename": "EXH1_holdings",
        "csv_url_template": (
            "https://www.ishares.com/uk/individual/en/products/251954/"
            "ishares-stoxx-europe-600-oil-gas-ucits-etf-de-fund/1506575576011.ajax"
            "?fileType=csv&fileName=EXH1_holdings&dataType=fund"
        ),
        "start_friday": date(2018, 1, 5),
        "ticker_overrides": {},
        "csv_date_format": "uk",
        "apply_exchange_suffix": True,
        "yfinance_trading_proxy": "EXH1.DE",
    },
    "EXV3": {  # Stoxx Europe 600 Technology
        "symbol": "EXV3",
        "ishares_region": "uk",
        "product_id": "251961",
        "url_slug": "ishares-stoxx-europe-600-technology-ucits-etf-de-fund",
        "ajax_id": "1506575576011",
        "filename": "EXV3_holdings",
        "csv_url_template": (
            "https://www.ishares.com/uk/individual/en/products/251961/"
            "ishares-stoxx-europe-600-technology-ucits-etf-de-fund/1506575576011.ajax"
            "?fileType=csv&fileName=EXV3_holdings&dataType=fund"
        ),
        "start_friday": date(2018, 1, 5),
        "ticker_overrides": {},
        "csv_date_format": "uk",
        "apply_exchange_suffix": True,
        "yfinance_trading_proxy": "EXV3.DE",
    },
    "EXH3": {  # Stoxx Europe 600 Industrial Goods & Services
        "symbol": "EXH3",
        "ishares_region": "uk",
        "product_id": "251948",
        "url_slug": "ishares-stoxx-europe-600-industrial-goods-services-ucits-etf-de-fund",
        "ajax_id": "1506575576011",
        "filename": "EXH3_holdings",
        "csv_url_template": (
            "https://www.ishares.com/uk/individual/en/products/251948/"
            "ishares-stoxx-europe-600-industrial-goods-services-ucits-etf-de-fund/"
            "1506575576011.ajax"
            "?fileType=csv&fileName=EXH3_holdings&dataType=fund"
        ),
        "start_friday": date(2018, 1, 5),
        "ticker_overrides": {},
        "csv_date_format": "uk",
        "apply_exchange_suffix": True,
        "yfinance_trading_proxy": "EXH3.DE",
    },
    "EXH9": {  # Stoxx Europe 600 Utilities (verified product_id 251967)
        "symbol": "EXH9",
        "ishares_region": "uk",
        "product_id": "251967",
        "url_slug": "ishares-stoxx-europe-600-utilities-ucits-etf-de-fund",
        "ajax_id": "1506575576011",
        "filename": "EXH9_holdings",
        "csv_url_template": (
            "https://www.ishares.com/uk/individual/en/products/251967/"
            "ishares-stoxx-europe-600-utilities-ucits-etf-de-fund/1506575576011.ajax"
            "?fileType=csv&fileName=EXH9_holdings&dataType=fund"
        ),
        "start_friday": date(2018, 1, 5),
        "ticker_overrides": {},
        "csv_date_format": "uk",
        "apply_exchange_suffix": True,
        "yfinance_trading_proxy": "EXH9.DE",
    },
    # --- Single-country UCITS ETFs ---
    "IJPN": {  # MSCI Japan (USD distributing, listed London/Xetra)
        "symbol": "IJPN",
        "ishares_region": "uk",
        "product_id": "251866",
        "url_slug": "ishares-msci-japan-ucits-etf-inc-fund",
        "ajax_id": "1506575576011",
        "filename": "IJPN_holdings",
        "csv_url_template": (
            "https://www.ishares.com/uk/individual/en/products/251866/"
            "ishares-msci-japan-ucits-etf-inc-fund/1506575576011.ajax"
            "?fileType=csv&fileName=IJPN_holdings&dataType=fund"
        ),
        "start_friday": date(2018, 1, 5),
        "ticker_overrides": {},
        "csv_date_format": "uk",
        "apply_exchange_suffix": True,
        "yfinance_trading_proxy": "EWJ",  # US-listed iShares MSCI Japan
    },
    "NDIA": {  # MSCI India (USD)
        "symbol": "NDIA",
        "ishares_region": "uk",
        "product_id": "297617",
        "url_slug": "ishares-msci-india-ucits-etf-usd-acc-fund",
        "ajax_id": "1506575576011",
        "filename": "NDIA_holdings",
        "csv_url_template": (
            "https://www.ishares.com/uk/individual/en/products/297617/"
            "ishares-msci-india-ucits-etf-usd-acc-fund/1506575576011.ajax"
            "?fileType=csv&fileName=NDIA_holdings&dataType=fund"
        ),
        "start_friday": date(2018, 1, 5),
        "ticker_overrides": {},
        "csv_date_format": "uk",
        "apply_exchange_suffix": True,
        "yfinance_trading_proxy": "INDA",  # US-listed iShares MSCI India
    },
    "ICHN": {  # MSCI China (USD)
        "symbol": "ICHN",
        "ishares_region": "uk",
        "product_id": "308751",
        "url_slug": "ishares-msci-china-ucits-etf-fund",
        "ajax_id": "1506575576011",
        "filename": "ICHN_holdings",
        "csv_url_template": (
            "https://www.ishares.com/uk/individual/en/products/308751/"
            "ishares-msci-china-ucits-etf-fund/1506575576011.ajax"
            "?fileType=csv&fileName=ICHN_holdings&dataType=fund"
        ),
        "start_friday": date(2018, 1, 5),
        "ticker_overrides": {},
        "csv_date_format": "uk",
        "apply_exchange_suffix": True,
        "yfinance_trading_proxy": "MCHI",  # US-listed iShares MSCI China
    },
    "ITWN": {  # MSCI Taiwan
        "symbol": "ITWN",
        "ishares_region": "uk",
        "product_id": "251878",
        "url_slug": "ishares-msci-taiwan-ucits-etf",
        "ajax_id": "1506575576011",
        "filename": "ITWN_holdings",
        "csv_url_template": (
            "https://www.ishares.com/uk/individual/en/products/251878/"
            "ishares-msci-taiwan-ucits-etf/1506575576011.ajax"
            "?fileType=csv&fileName=ITWN_holdings&dataType=fund"
        ),
        "start_friday": date(2018, 1, 5),
        "ticker_overrides": {},
        "csv_date_format": "uk",
        "apply_exchange_suffix": True,
        "yfinance_trading_proxy": "EWT",  # US-listed iShares MSCI Taiwan
    },
    "IDP6": {  # S&P SmallCap 600 (US small-cap, categorically a market-cap
              # slice rather than a sector — but useful breadth dimension)
        "symbol": "IDP6",
        "ishares_region": "uk",
        "product_id": "251920",
        "url_slug": "ishares-sp-smallcap-600-ucits-etf",
        "ajax_id": "1506575576011",
        "filename": "ISP6_holdings",  # download filename uses ISP6 not IDP6
        "csv_url_template": (
            "https://www.ishares.com/uk/individual/en/products/251920/"
            "ishares-sp-smallcap-600-ucits-etf/1506575576011.ajax"
            "?fileType=csv&fileName=ISP6_holdings&dataType=fund"
        ),
        "start_friday": date(2018, 1, 5),
        "ticker_overrides": {
            "BRKB": "BRK-B", "BRK.B": "BRK-B",
            "BFB": "BF-B", "BF.B": "BF-B",
        },
        "csv_date_format": "uk",
        "yfinance_trading_proxy": "IJR",  # iShares Core S&P Small-Cap (US-listed)
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


# =========================================================================
# Active backtest universe — single source of truth.
# Downstream scripts import this constant rather than redeclaring the list.
# Edit here to add / remove ETFs from the backtest universe.
# =========================================================================
UNIVERSE_ETFS: list[str] = [
    # Broad-market / concentrated
    "SOXX",   # iShares Semiconductor (semis)
    "CSP1",   # S&P 500 (full)
    "CNDX",   # NASDAQ-100
    # Complete S&P 500 sector slices (iShares UK UCITS, SPDR proxies)
    "IUES",   # Energy            → XLE
    "IUFS",   # Financials        → XLF
    # IUIT (S&P 500 Info Tech) PRUNED 2026-05-23 — correlation with CNDX
    # is 0.97 (Test 12 diagnostic), and CNDX/QQQ is the more-traded variant
    # of the same large-cap tech exposure. Keeping both was double-counting.
    # The registry entry remains for reference; just removed from active list.
    "IUHC",   # Health Care       → XLV
    "IUIS",   # Industrials       → XLI
    "IUCS",   # Consumer Staples  → XLP
    "IUCD",   # Consumer Disc     → XLY
    "IUUS",   # Utilities         → XLU
    "IUMS",   # Materials         → XLB         (added 2026-05-22)
    "IUCM",   # Comm Services     → XLC         (added 2026-05-22)
    "IUSP",   # US REITs          → XLRE        (added 2026-05-22, REIT proxy)
    # Market-cap dimension (not a sector — different universe slice)
    "IDP6",   # S&P SmallCap 600  → IJR         (added 2026-05-22)
]


# =========================================================================
# Phase 4 (2026-05-23) — non-US universes for testing merge-vs-separate
# Both options will be benchmarked before deciding the deployed architecture.
# =========================================================================

# Europe sector breadth — 5 Stoxx Europe 600 sector UCITS
UNIVERSE_EUROPE_SECTORS: list[str] = [
    "EXV1",   # Banks
    "EXH1",   # Oil & Gas
    "EXV3",   # Technology
    "EXH3",   # Industrial Goods & Services
    "EXH9",   # Utilities
]

# Single-country breadth — 4 country UCITS
UNIVERSE_COUNTRIES: list[str] = [
    "IJPN",   # Japan
    "NDIA",   # India
    "ICHN",   # China
    "ITWN",   # Taiwan
]

# Merged variant: UNIVERSE_ETFS + Europe sectors + Countries = 23 ETFs
UNIVERSE_GLOBAL: list[str] = UNIVERSE_ETFS + UNIVERSE_EUROPE_SECTORS + UNIVERSE_COUNTRIES
