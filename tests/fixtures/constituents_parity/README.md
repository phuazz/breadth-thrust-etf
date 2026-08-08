# Constituent transport parity fixtures

Ground-truth pairs used by `tests/test_constituent_api_parity.py` to pin the
Phase 27 transport swap: for each fund, the roster as the **old** iShares CSV
endpoint returned it, and the same roster as the **new** BlackRock
product-data JSON API returns it. The test asserts the two parse to an
identical ticker list.

## Coverage

| Fixture | Fund | As-of | Why this one |
|---|---|---|---|
| `CSP1_20260710` | iShares Core S&P 500 UCITS | 2026-07-10 | Largest roster (504); exercises `ticker_overrides` (BRKB → BRK-B, BFB → BF-B) |
| `SOXX_20260508` | iShares Semiconductor | 2026-05-08 | US region — reached via `targetSite=ishares-us`. Last known-good date before the US `.ajax` route was Akamai-blocked (~2026-05-15) |
| `EXV1_20260710` | STOXX Europe 600 Banks | 2026-07-10 | Multi-venue Europe; includes the ambiguous `Nasdaq Omx Nordic` venue resolved by Location |
| `EXH1_20260710` | STOXX Europe 600 Oil & Gas | 2026-07-10 | LSE slash notation (`BP.`, `SHELL`) |
| `IJPN_20260710` | MSCI Japan | 2026-07-10 | Numeric Tokyo tickers → `.T` |
| `ITWN_20260710` | MSCI Taiwan | 2026-07-10 | Includes the unmapped `Gretai Securities Market` venue |
| `NDIA_20260710` | MSCI India | 2026-07-10 | NSE dot-to-dash roots; `Bse Ltd` venue |
| `IDP6_20260710` | S&P SmallCap 600 | 2026-07-10 | 602 names; mixed US venues plus `NO MARKET (E.G. UNLISTED)` placeholder rows |

The six exchange-suffix funds are all included deliberately: suffix
resolution is where a transport change could mis-route the downstream price
fetch to the wrong listing without raising anything.

## Provenance

- **`*.csv`** — copied from `data/raw_ishares/` (gitignored), captured by the
  legacy `<ajax_id>.ajax?fileType=csv` endpoint before iShares re-platformed.
  That route stopped serving CSV between the 2026-07-10 and 2026-07-17
  refreshes and cannot be re-captured.
- **`*_api.json`** — fetched from `PRODUCT_DATA_API` on 2026-08-07 via
  `fetch_constituents.fetch_product_data`.

## Both are trimmed — read this before trusting a diff

The CSVs keep the preamble, the header row and the holdings block, but only
the columns the parser reads: `Ticker, Name, Sector, Asset Class, Location,
Exchange`. Market value, weight, notional, shares and price are dropped —
`data/raw_ishares/` is gitignored, and there is no reason to commit
position-level fund economics to run a ticker-parity test.

The JSON payloads keep the real nesting
(`componentsByNameMap.holdings.containersByNameMap.all.dataPointsByNameMap`)
and the five datapoints the parser reads (`asOfDate`, `ticker`,
`assetClass`, `exchange`, `countryOfRisk`), with their values verbatim.

The trim was verified not to change the result: the fixture builder asserts
`parse_holdings(trimmed) == parse_holdings(full)` for every fund before
writing. Parity was **also** confirmed against the full, untrimmed cached
CSVs on 2026-08-07 — all eight funds identical, including suffix resolution.

Because both sides are reductions, these fixtures pin *our parsing*, not the
live endpoint. For that, run the opt-in live check:

```bash
BREADTH_LIVE_API_TESTS=1 python -m pytest tests/test_constituent_api_parity.py -k live
```

## Regenerating

Fixtures are only re-cut if the parser's column requirements change. The CSV
side cannot be regenerated from upstream — the endpoint is gone — so copy it
from `data/raw_ishares/` or from this directory's git history. The JSON side:

```bash
python -c "import sys; sys.path.insert(0,'scripts'); import json; from datetime import date; import fetch_constituents as fc; from etf_registry import get_etf; print(json.dumps(fc.fetch_product_data(date(2026,7,10), get_etf('CSP1')))[:200])"
```
