"""Emit a CSV of the most-relevant iShares ETFs (UK UCITS + US-domiciled)
with a deployed=Yes/No tag mapped to our current four strategies.

Output: ``data/ishares_catalogue.csv``.

WHY NOT A LIVE SCRAPE? The iShares product-screener endpoint is
Cloudflare-protected and rejects bare HTTP requests with 403. A
production scraper would need ``cloudscraper`` (full browser emulation)
plus periodic re-authentication, which is heavy for a research artifact
that does not need to be live. Instead this script ships a hand-curated
reference list of the ~120 most-relevant iShares ETFs (sourced from the
public UK + US product lines) covering every major asset class, sector,
region, and theme that BlackRock issues at meaningful AUM.

To refresh: open https://www.ishares.com/uk/individual/en/products and
https://www.ishares.com/us/products in a browser, sort by AUM, and
update REFERENCE_CATALOGUE below. Roughly once a year is enough — the
iShares product set is stable, with maybe 5-10 new launches per year
that pass the "meaningful AUM" filter.

Each entry is tagged with the strategy code (A/B/C/D) if deployed,
or empty if not. The output CSV is sorted with deployed rows first
(grouped at the top), then by category and AUM tier.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUT_CSV = DATA_DIR / "ishares_catalogue.csv"


# ----------------------------------------------------------------------
# Deployed-ticker map — keyed by the form used in our strategy scripts
# (yfinance form for non-US, bare for US-listed).
# ----------------------------------------------------------------------
DEPLOYED: dict[str, str] = {
    # Strategy A — US sectors + breadth
    "SOXX": "A", "CSP1.L": "A", "CNDX.L": "A",
    "IUES.L": "A", "IUFS.L": "A", "IUHC.L": "A", "IUIS.L": "A",
    "IUCS.L": "A", "IUCD.L": "A", "IUUS.L": "A", "IUMS.L": "A",
    "IUCM.L": "A", "IUSP.L": "A", "IDP6.L": "A",
    # Strategy B — asset class (only the iShares-issued ones are tagged here)
    "IEF": "B", "TLT": "B", "TIP": "B", "HYG": "B", "EFA": "B",
    "EEM": "B", "EWJ": "B", "IJR": "B",
    # Strategy C — thematic (iShares-issued only)
    "ICLN": "C", "ITA": "C",
    # Strategy D — Europe sector UCITS (all iShares Xetra)
    "EXV1.DE": "D", "EXH1.DE": "D", "EXV3.DE": "D",
    "EXH3.DE": "D", "EXH9.DE": "D",
}


# ----------------------------------------------------------------------
# Hand-curated reference list. Sourced from iShares UK + US public
# product pages. Each row: (ticker_yf, name, asset_class, sub_class,
# domicile, exchange, currency, aum_tier_usd, ter_bps_approx).
#
# AUM tiers (rough, USD billions):
#   "XXL"  >= 50
#   "XL"   20-50
#   "L"    5-20
#   "M"    1-5
#   "S"    0.1-1
#   "XS"   < 0.1
# TER is approximate (basis points/yr) — refresh against ishares.com
# for exact current ER.
# ----------------------------------------------------------------------
REFERENCE_CATALOGUE: list[tuple[str, str, str, str, str, str, str, str, float | None]] = [
    # ==================================================================
    # === STRATEGY A — US sectors + broad (already deployed) ===========
    # ==================================================================
    ("SOXX",    "iShares Semiconductor",                    "Equity", "US Sector",       "US", "NASDAQ", "USD", "L",   35.0),
    ("CSP1.L",  "iShares Core S&P 500 (UCITS, USD-acc)",    "Equity", "US Broad",        "UK", "LSE",    "USD", "XXL", 7.0),
    ("CNDX.L",  "iShares NASDAQ 100 (UCITS, USD-acc)",      "Equity", "US Broad",        "UK", "LSE",    "USD", "L",   33.0),
    ("IUES.L",  "iShares S&P 500 Energy Sector (UCITS)",    "Equity", "US Sector",       "UK", "LSE",    "USD", "M",   15.0),
    ("IUFS.L",  "iShares S&P 500 Financials Sector (UCITS)","Equity", "US Sector",       "UK", "LSE",    "USD", "L",   15.0),
    ("IUHC.L",  "iShares S&P 500 Health Care Sector (UCITS)","Equity","US Sector",       "UK", "LSE",    "USD", "L",   15.0),
    ("IUIS.L",  "iShares S&P 500 Industrials Sector (UCITS)","Equity","US Sector",       "UK", "LSE",    "USD", "M",   15.0),
    ("IUCS.L",  "iShares S&P 500 Consumer Staples (UCITS)", "Equity", "US Sector",       "UK", "LSE",    "USD", "M",   15.0),
    ("IUCD.L",  "iShares S&P 500 Consumer Discretionary",   "Equity", "US Sector",       "UK", "LSE",    "USD", "M",   15.0),
    ("IUUS.L",  "iShares S&P 500 Utilities Sector (UCITS)", "Equity", "US Sector",       "UK", "LSE",    "USD", "M",   15.0),
    ("IUMS.L",  "iShares S&P 500 Materials Sector (UCITS)", "Equity", "US Sector",       "UK", "LSE",    "USD", "S",   15.0),
    ("IUCM.L",  "iShares S&P 500 Comm Services (UCITS)",    "Equity", "US Sector",       "UK", "LSE",    "USD", "S",   15.0),
    ("IUSP.L",  "iShares FTSE EPRA NAREIT US Property",     "Equity", "US REITs",        "UK", "LSE",    "USD", "S",   40.0),
    ("IDP6.L",  "iShares S&P SmallCap 600 (UCITS)",         "Equity", "US Small-Cap",    "UK", "LSE",    "USD", "M",   30.0),
    # ==================================================================
    # === STRATEGY A — US sectors NOT deployed =========================
    # ==================================================================
    ("IUIT.L",  "iShares S&P 500 Info Tech Sector (UCITS)", "Equity", "US Sector",       "UK", "LSE",    "USD", "L",   15.0),  # pruned May 2026 — 0.97 corr with CNDX
    # ==================================================================
    # === STRATEGY D — Europe sectors (5 deployed) =====================
    # ==================================================================
    ("EXV1.DE", "iShares Stoxx Europe 600 Banks (UCITS)",   "Equity", "Europe Sector",   "DE", "Xetra",  "EUR", "L",   46.0),
    ("EXH1.DE", "iShares Stoxx Europe 600 Oil & Gas (UCITS)","Equity","Europe Sector",   "DE", "Xetra",  "EUR", "M",   46.0),
    ("EXV3.DE", "iShares Stoxx Europe 600 Technology (UCITS)","Equity","Europe Sector",  "DE", "Xetra",  "EUR", "M",   46.0),
    ("EXH3.DE", "iShares Stoxx Europe 600 Industrials (UCITS)","Equity","Europe Sector", "DE", "Xetra",  "EUR", "M",   46.0),
    ("EXH9.DE", "iShares Stoxx Europe 600 Utilities (UCITS)","Equity","Europe Sector",   "DE", "Xetra",  "EUR", "M",   46.0),
    # ==================================================================
    # === STRATEGY D — Europe sectors NOT deployed (14 more) ===========
    # ==================================================================
    ("EXV4.DE", "iShares Stoxx Europe 600 Financial Svcs", "Equity", "Europe Sector",   "DE", "Xetra",  "EUR", "S",   46.0),
    ("EXV6.DE", "iShares Stoxx Europe 600 Insurance",      "Equity", "Europe Sector",   "DE", "Xetra",  "EUR", "S",   46.0),
    ("EXV7.DE", "iShares Stoxx Europe 600 Telecoms",       "Equity", "Europe Sector",   "DE", "Xetra",  "EUR", "S",   46.0),
    ("EXV8.DE", "iShares Stoxx Europe 600 Personal Goods", "Equity", "Europe Sector",   "DE", "Xetra",  "EUR", "S",   46.0),
    ("EXV9.DE", "iShares Stoxx Europe 600 Travel & Leisure","Equity","Europe Sector",   "DE", "Xetra",  "EUR", "S",   46.0),
    ("EXSA.DE", "iShares Stoxx Europe 600 (broad)",        "Equity", "Europe Broad",    "DE", "Xetra",  "EUR", "L",   20.0),
    ("EXH4.DE", "iShares Stoxx Europe 600 Health Care",    "Equity", "Europe Sector",   "DE", "Xetra",  "EUR", "M",   46.0),
    ("EXH5.DE", "iShares Stoxx Europe 600 Cons Staples",   "Equity", "Europe Sector",   "DE", "Xetra",  "EUR", "S",   46.0),
    ("EXH6.DE", "iShares Stoxx Europe 600 Media",          "Equity", "Europe Sector",   "DE", "Xetra",  "EUR", "XS",  46.0),
    ("EXH7.DE", "iShares Stoxx Europe 600 Retail",         "Equity", "Europe Sector",   "DE", "Xetra",  "EUR", "XS",  46.0),
    ("EXI3.DE", "iShares Stoxx Europe 600 Automobiles",    "Equity", "Europe Sector",   "DE", "Xetra",  "EUR", "S",   46.0),
    ("EXI4.DE", "iShares Stoxx Europe 600 Basic Resources","Equity", "Europe Sector",   "DE", "Xetra",  "EUR", "S",   46.0),
    ("EXI5.DE", "iShares Stoxx Europe 600 Chemicals",      "Equity", "Europe Sector",   "DE", "Xetra",  "EUR", "S",   46.0),
    ("EXI6.DE", "iShares Stoxx Europe 600 Construction",   "Equity", "Europe Sector",   "DE", "Xetra",  "EUR", "S",   46.0),
    ("EXI7.DE", "iShares Stoxx Europe 600 Food & Beverage","Equity", "Europe Sector",   "DE", "Xetra",  "EUR", "S",   46.0),
    # ==================================================================
    # === STRATEGY B — asset class (deployed iShares only) =============
    # ==================================================================
    ("IEF",     "iShares 7-10y US Treasury",               "Fixed Income", "US Govt",   "US", "NASDAQ", "USD", "XL",  15.0),
    ("TLT",     "iShares 20+y US Treasury",                "Fixed Income", "US Govt",   "US", "NASDAQ", "USD", "XL",  15.0),
    ("TIP",     "iShares TIPS Bond",                       "Fixed Income", "US TIPS",   "US", "NYSE",   "USD", "L",   19.0),
    ("HYG",     "iShares iBoxx US High Yield Corp Bond",   "Fixed Income", "US HY",     "US", "NYSE",   "USD", "L",   49.0),
    ("EFA",     "iShares MSCI EAFE",                       "Equity", "Intl Developed",  "US", "NYSE",   "USD", "XXL", 32.0),
    ("EEM",     "iShares MSCI Emerging Markets",           "Equity", "EM Broad",        "US", "NYSE",   "USD", "XXL", 70.0),
    ("EWJ",     "iShares MSCI Japan",                      "Equity", "Single Country",  "US", "NYSE",   "USD", "L",   50.0),
    ("IJR",     "iShares Core S&P Small-Cap",              "Equity", "US Small-Cap",    "US", "NYSE",   "USD", "XXL", 6.0),
    # ==================================================================
    # === STRATEGY B — FIXED INCOME NOT DEPLOYED (big gap) =============
    # ==================================================================
    ("IGLO.L",  "iShares Global Govt Bond (UCITS)",        "Fixed Income", "Global Govt","UK", "LSE",   "USD", "M",   20.0),
    ("IEAC.L",  "iShares Core EUR Corp Bond (UCITS)",      "Fixed Income", "Euro IG",   "UK", "LSE",    "EUR", "L",   20.0),
    ("IEAA.L",  "iShares EUR Corp 1-5y (UCITS)",           "Fixed Income", "Euro IG Short","UK","LSE",  "EUR", "M",   20.0),
    ("SEMB.L",  "iShares JPM USD EM Bond (UCITS)",         "Fixed Income", "EM USD Govt","UK","LSE",    "USD", "L",   45.0),
    ("EMHY.L",  "iShares EM USD High Yield Bond (UCITS)",  "Fixed Income", "EM HY",     "UK", "LSE",    "USD", "S",   50.0),
    ("LQDE.L",  "iShares USD Corp Bond IG (UCITS)",        "Fixed Income", "US IG",     "UK", "LSE",    "USD", "L",   20.0),
    ("LQD",     "iShares iBoxx $ Investment Grade Corp",   "Fixed Income", "US IG",     "US", "NYSE",   "USD", "XXL", 14.0),
    ("AGG",     "iShares Core US Aggregate Bond",          "Fixed Income", "US Agg",    "US", "NYSE",   "USD", "XXL", 3.0),
    ("MUB",     "iShares National Muni Bond",              "Fixed Income", "US Muni",   "US", "NYSE",   "USD", "XL",  5.0),
    ("SHV",     "iShares Short Treasury Bond",             "Fixed Income", "US T-Bill", "US", "NYSE",   "USD", "XXL", 15.0),
    ("IGSB",    "iShares 1-5y Investment Grade Corp",      "Fixed Income", "US IG Short","US","NYSE",   "USD", "XL",  6.0),
    ("PFF",     "iShares Preferred & Income Securities",   "Fixed Income", "US Preferreds","US","NASDAQ","USD","L",   46.0),
    # ==================================================================
    # === STRATEGY B — COMMODITIES NOT DEPLOYED ========================
    # ==================================================================
    ("SLV",     "iShares Silver Trust",                    "Commodity", "Silver Spot",  "US", "NYSE",   "USD", "L",   50.0),  # Phase 16 tested+reverted
    ("IAU",     "iShares Gold Trust",                      "Commodity", "Gold Spot",    "US", "NYSE",   "USD", "XXL", 25.0),  # cheaper alt to GLD
    ("SGOL",    "(Not iShares — Aberdeen)",                "Commodity", "Gold Spot",    "—",  "—",     "—",   "L",   17.0),  # reference only
    # ==================================================================
    # === SINGLE-COUNTRY UCITS (Phase 4 tested 4, rejected) ============
    # ==================================================================
    ("IJPN.L",  "iShares MSCI Japan (UCITS)",              "Equity", "Single Country",  "UK", "LSE",    "USD", "L",   59.0),  # Phase 4 tested
    ("NDIA.L",  "iShares MSCI India (UCITS)",              "Equity", "Single Country",  "UK", "LSE",    "USD", "M",   65.0),  # Phase 4 tested
    ("ICHN.L",  "iShares MSCI China A (UCITS)",            "Equity", "Single Country",  "UK", "LSE",    "USD", "M",   40.0),  # Phase 4 tested
    ("ITWN.L",  "iShares MSCI Taiwan (UCITS)",             "Equity", "Single Country",  "UK", "LSE",    "USD", "S",   59.0),  # Phase 4 tested
    ("ISF.L",   "iShares Core FTSE 100 (UCITS)",           "Equity", "Single Country",  "UK", "LSE",    "GBP", "L",   7.0),
    ("IUKD.L",  "iShares UK Dividend (UCITS)",             "Equity", "Single Country",  "UK", "LSE",    "GBP", "M",   40.0),
    ("EWZ",     "iShares MSCI Brazil",                     "Equity", "Single Country",  "US", "NYSE",   "USD", "L",   59.0),
    ("EWY",     "iShares MSCI South Korea",                "Equity", "Single Country",  "US", "NYSE",   "USD", "L",   59.0),
    ("EWG",     "iShares MSCI Germany",                    "Equity", "Single Country",  "US", "NYSE",   "USD", "M",   50.0),
    ("EWU",     "iShares MSCI United Kingdom",             "Equity", "Single Country",  "US", "NYSE",   "USD", "M",   50.0),
    ("INDA",    "iShares MSCI India",                      "Equity", "Single Country",  "US", "NYSE",   "USD", "XL",  64.0),
    ("MCHI",    "iShares MSCI China",                      "Equity", "Single Country",  "US", "NASDAQ", "USD", "L",   59.0),
    ("EWT",     "iShares MSCI Taiwan",                     "Equity", "Single Country",  "US", "NYSE",   "USD", "L",   59.0),
    ("EWQ",     "iShares MSCI France",                     "Equity", "Single Country",  "US", "NYSE",   "USD", "S",   50.0),
    ("KSA",     "iShares MSCI Saudi Arabia",               "Equity", "Single Country",  "US", "NYSE",   "USD", "S",   74.0),
    # ==================================================================
    # === FACTOR / SMART BETA — NOT DEPLOYED (entirely missing) ========
    # ==================================================================
    ("MTUM",    "iShares MSCI USA Momentum Factor",        "Equity", "Factor",          "US", "NYSE",   "USD", "L",   15.0),
    ("QUAL",    "iShares MSCI USA Quality Factor",         "Equity", "Factor",          "US", "NYSE",   "USD", "L",   15.0),
    ("VLUE",    "iShares MSCI USA Value Factor",           "Equity", "Factor",          "US", "NYSE",   "USD", "L",   15.0),
    ("USMV",    "iShares MSCI USA Min Vol Factor",         "Equity", "Factor",          "US", "NYSE",   "USD", "XL",  15.0),
    ("SIZE",    "iShares MSCI USA Size Factor",            "Equity", "Factor",          "US", "NYSE",   "USD", "S",   15.0),
    ("IWMO.L",  "iShares Edge MSCI World Momentum (UCITS)","Equity", "Factor",          "UK", "LSE",    "USD", "M",   30.0),
    ("IWQU.L",  "iShares Edge MSCI World Quality (UCITS)", "Equity", "Factor",          "UK", "LSE",    "USD", "M",   30.0),
    ("IWVL.L",  "iShares Edge MSCI World Value (UCITS)",   "Equity", "Factor",          "UK", "LSE",    "USD", "M",   30.0),
    ("MVOL.L",  "iShares Edge MSCI World Min Vol (UCITS)", "Equity", "Factor",          "UK", "LSE",    "USD", "M",   30.0),
    ("IFSW.L",  "iShares Edge MSCI World Multifactor",     "Equity", "Factor",          "UK", "LSE",    "USD", "S",   50.0),
    # ==================================================================
    # === iShares THEMATICS — DEPLOYED in Strategy C ===================
    # ==================================================================
    ("ICLN",    "iShares Global Clean Energy",             "Equity", "Thematic",        "US", "NASDAQ", "USD", "L",   41.0),
    ("ITA",     "iShares US Aerospace & Defense",          "Equity", "Thematic",        "US", "CBOE",   "USD", "XL",  40.0),
    ("XBI",     "(Not iShares — SPDR. Reference only)",    "Equity", "Thematic",        "—",  "—",     "—",   "L",   35.0),
    # ==================================================================
    # === iShares THEMATICS NOT DEPLOYED (Strategy C alternatives) =====
    # ==================================================================
    ("INRG.L",  "iShares Global Clean Energy (UCITS)",     "Equity", "Thematic",        "UK", "LSE",    "USD", "L",   65.0),  # = ICLN (already in C)
    ("WAT.L",   "iShares Global Water (UCITS)",            "Equity", "Thematic",        "UK", "LSE",    "USD", "L",   65.0),
    ("WCBR.L",  "iShares Global Cybersecurity (UCITS)",    "Equity", "Thematic",        "UK", "LSE",    "USD", "S",   50.0),  # alt to CIBR
    ("DRIV.L",  "iShares Global Auto & Self Drive (UCITS)","Equity", "Thematic",        "UK", "LSE",    "USD", "S",   40.0),
    ("AIAI.L",  "iShares AI Infrastructure (UCITS)",       "Equity", "Thematic",        "UK", "LSE",    "USD", "XS",  50.0),  # newer launch
    ("HEAL.L",  "iShares Global Healthcare Innovation",    "Equity", "Thematic",        "UK", "LSE",    "USD", "S",   50.0),
    ("AGED.L",  "iShares Ageing Population (UCITS)",       "Equity", "Thematic",        "UK", "LSE",    "USD", "S",   40.0),
    ("DGTL.L",  "iShares Digitalisation (UCITS)",          "Equity", "Thematic",        "UK", "LSE",    "USD", "M",   40.0),
    ("RBOT.L",  "iShares Automation & Robotics (UCITS)",   "Equity", "Thematic",        "UK", "LSE",    "USD", "M",   40.0),  # alt to BOTZ
    ("HMWO.L",  "iShares Core MSCI World",                 "Equity", "World Broad",     "UK", "LSE",    "USD", "XXL", 20.0),
    ("HMEM.L",  "iShares Core MSCI EM (UCITS)",            "Equity", "EM Broad",        "UK", "LSE",    "USD", "XXL", 18.0),
    # ==================================================================
    # === iShares CRYPTO ETPs (NOT DEPLOYED — IBIT covered via BTC-USD)
    # ==================================================================
    ("IBIT",    "iShares Bitcoin Trust",                   "Crypto", "BTC",             "US", "NASDAQ", "USD", "XXL", 25.0),  # deployed via BTC-USD proxy
    ("ETHA",    "iShares Ethereum Trust",                  "Crypto", "ETH",             "US", "NASDAQ", "USD", "L",   25.0),  # too new (<5y) for WF
    ("IB1B.L",  "iShares Bitcoin (UCITS, UK)",             "Crypto", "BTC",             "UK", "LSE",    "USD", "S",   15.0),
    ("IETH.L",  "iShares Ethereum (UCITS, UK)",            "Crypto", "ETH",             "UK", "LSE",    "USD", "S",   15.0),
    # ==================================================================
    # === iShares CORE EQUITY BUILDING BLOCKS (broad, low-cost) ========
    # ==================================================================
    ("ITOT",    "iShares Core S&P Total US Stock Market",  "Equity", "US Total",        "US", "NYSE",   "USD", "XXL", 3.0),
    ("IVV",     "iShares Core S&P 500",                    "Equity", "US Broad",        "US", "NYSE",   "USD", "XXL", 3.0),
    ("IJH",     "iShares Core S&P Mid-Cap",                "Equity", "US Mid-Cap",      "US", "NYSE",   "USD", "XXL", 5.0),
    ("IWM",     "iShares Russell 2000",                    "Equity", "US Small-Cap",    "US", "NYSE",   "USD", "XXL", 19.0),
    ("IWB",     "iShares Russell 1000",                    "Equity", "US Large-Cap",    "US", "NYSE",   "USD", "XXL", 15.0),
    ("IDEV",    "iShares Core MSCI International Dev",     "Equity", "Intl Developed",  "US", "NYSE",   "USD", "XL",  4.0),
    ("IXUS",    "iShares Core MSCI Total Intl Stock",      "Equity", "Intl ex-US",      "US", "NYSE",   "USD", "XL",  7.0),
    ("EMXC",    "iShares MSCI EM ex-China",                "Equity", "EM ex-China",     "US", "NASDAQ", "USD", "L",   18.0),
    ("IEMG",    "iShares Core MSCI EM",                    "Equity", "EM Broad",        "US", "NYSE",   "USD", "XXL", 9.0),
    ("ACWI",    "iShares MSCI ACWI",                       "Equity", "World Broad",     "US", "NASDAQ", "USD", "XL",  32.0),
    # ==================================================================
    # === iShares REGIONAL THEMATIC / FRONTIER MARKETS =================
    # ==================================================================
    ("VNM",     "VanEck Vietnam (REF only — not iShares)", "Equity", "Single Country",  "—",  "—",     "—",   "S",   60.0),
    ("FM",      "iShares MSCI Frontier 100",               "Equity", "Frontier",        "US", "NYSE",   "USD", "S",   80.0),
    ("EDEN",    "iShares MSCI Denmark",                    "Equity", "Single Country",  "US", "NYSE",   "USD", "S",   50.0),
    ("EWA",     "iShares MSCI Australia",                  "Equity", "Single Country",  "US", "NYSE",   "USD", "M",   50.0),
    ("EWC",     "iShares MSCI Canada",                     "Equity", "Single Country",  "US", "NYSE",   "USD", "M",   50.0),
    ("EWH",     "iShares MSCI Hong Kong",                  "Equity", "Single Country",  "US", "NYSE",   "USD", "S",   50.0),
    ("EWM",     "iShares MSCI Malaysia",                   "Equity", "Single Country",  "US", "NYSE",   "USD", "XS",  50.0),
    ("THD",     "iShares MSCI Thailand",                   "Equity", "Single Country",  "US", "NASDAQ", "USD", "S",   59.0),
    ("EIDO",    "iShares MSCI Indonesia",                  "Equity", "Single Country",  "US", "NYSE",   "USD", "S",   59.0),
    ("EPOL",    "iShares MSCI Poland",                     "Equity", "Single Country",  "US", "NYSE",   "USD", "S",   59.0),
    ("EZA",     "iShares MSCI South Africa",               "Equity", "Single Country",  "US", "NYSE",   "USD", "S",   59.0),
]


def main() -> int:
    print(f"Building iShares catalogue reference list at "
          f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} ...")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    rows = []
    for (ticker, name, asset, sub, dom, exch, ccy, tier, ter) in REFERENCE_CATALOGUE:
        rows.append({
            "ticker": ticker,
            "name": name,
            "asset_class": asset,
            "sub_class": sub,
            "domicile": dom,
            "exchange": exch,
            "currency": ccy,
            "aum_tier": tier,
            "ter_bps_approx": ter,
            "deployed_in_strategy": DEPLOYED.get(ticker, ""),
        })
    # Sort: deployed first, then by asset_class, sub_class, aum_tier desc
    aum_order = {"XXL": 6, "XL": 5, "L": 4, "M": 3, "S": 2, "XS": 1}
    rows.sort(key=lambda r: (
        0 if r["deployed_in_strategy"] else 1,
        r["asset_class"],
        r["sub_class"],
        -aum_order.get(r["aum_tier"], 0),
        r["ticker"],
    ))
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    deployed_n = sum(1 for r in rows if r["deployed_in_strategy"])
    print(f"  Wrote {OUT_CSV.relative_to(ROOT)}")
    print(f"  {len(rows)} ETFs total, {deployed_n} deployed, "
          f"{len(rows) - deployed_n} not deployed")
    # Summary by asset class
    print("\n  By asset class:")
    counts: dict[str, tuple[int, int]] = {}
    for r in rows:
        d, t = counts.get(r["asset_class"], (0, 0))
        counts[r["asset_class"]] = (
            d + (1 if r["deployed_in_strategy"] else 0),
            t + 1,
        )
    for ac, (d, t) in sorted(counts.items()):
        print(f"    {ac:15s} {d:2d} deployed / {t:2d} total "
              f"({(t-d):2d} not deployed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
