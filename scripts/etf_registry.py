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
