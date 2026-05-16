# breadth-thrust-etf

Sector / thematic ETF breadth-thrust signal — research and backtest.

## What this is

A composite breadth-thrust signal computed from the **point-in-time constituents** of a sector or thematic ETF, plus a backtest of the signal applied to the parent ETF. This session validates the mechanism end-to-end on a single ETF (SOXX, iShares Semiconductor) before any attempt to generalise across sectors.

## Signal definition

Three equal-weighted breadth components, each computed across the ETF's point-in-time constituents:

1. **RSI breadth** — share of constituents with 14-day RSI greater than 70. Trigger: reading in the top decile of the ETF's own history (per-ETF threshold, not universal).
2. **MA breadth** — share of constituents above their 50-day moving average. Trigger: a Zweig-style thrust — crossing from below 50 per cent to above 80 per cent within 20 trading days.
3. **New-highs breadth** — share of constituents at a 63-day closing high. Trigger: top decile reading.

**Composite** = equal-weighted average of the three component z-scores, computed on an expanding window to avoid look-ahead.

**Entry signal**: composite crosses above its rolling 90th percentile AND at least 2 of the 3 components are individually triggered.

## Exit logic (whichever fires first)

- Trailing stop at 2 × ATR(20) below the highest close since entry.
- Regime exit if composite breadth flips to its bottom decile, or if the share of constituents above their 50-day MA falls below 40 per cent.
- Time stop at 252 trading days.

## Data integrity rules

- Constituent lists are **point-in-time** from iShares' historical holdings endpoint. Never substitute current holdings for historical breadth — that would be survivorship + look-ahead bias.
- Snapshots taken weekly (last business day of week); membership held static between snapshots. Documented in `scripts/fetch_constituents.py`.
- All RSI / MA / highs use only price data available at the signal date.
- yfinance is the price source. Coverage of delisted historical names was validated against the 2009 / 2017 / 2024 constituent snapshots before the backtest window was finalised — see "Backtest window" below.

## Backtest window

**Confirmed 2026-05-14**: yfinance coverage of point-in-time SOXX constituents is poor pre-2018 due to a backlog of acquired / delisted semiconductor names that Yahoo has dropped from its historical price feed (XLNX, MXIM, BRCM, ALTR, LLTC, ATML, CY, IDTI, MLNX, CREE, INFN, SNDK, FSL, ARMH, HITT, FEIC, TSRA, VSEA, CYMI, ...). Per snapshot, equity-only coverage:

| Snapshot   | Equities | Covered | Coverage |
|------------|---------:|--------:|---------:|
| 2009-06-30 |       45 |      20 |    44.4% |
| 2012-06-29 |       30 |      21 |    70.0% |
| 2014-06-30 |       30 |      19 |    63.3% |
| 2016-06-30 |       30 |      23 |    76.7% |
| 2018-06-29 |       30 |      24 |    80.0% |
| 2020-06-30 |       30 |      26 |    86.7% |
| 2024-06-28 |       30 |      29 |    96.7% |

Also flagged: iShares' own SOXX history has a year-long gap covering most of 2017 (responses return an empty 'Fund Holdings as of "-"' template between Dec 2016 and Dec 2017). Constituent snapshots will be carried forward through the gap.

Recommended start year **to be agreed with user** before Step 1 — see open question in conversation log dated 2026-05-14.

## Layout

```
breadth-thrust-etf/
├── scripts/
│   ├── fetch_constituents.py   (Step 1) Pull point-in-time SOXX holdings → data/constituents_soxx.json
│   ├── compute_breadth.py      (Step 2) Three components + composite → data/breadth_soxx.json
│   └── backtest.py             (Step 3) Signal + exits → data/backtest_soxx.json
├── data/                       JSON outputs (raw caches gitignored)
├── tests/                      Date edge cases + signal sanity checks
└── requirements.txt
```

## Status

- 2026-05-14: Project initialised. Step 0 smoke test complete (see "Backtest window" above).
- 2026-05-15: Backtest window confirmed as 2018-present. Step 1 (`scripts/fetch_constituents.py`) complete. 436 weekly snapshots written to `data/constituents_soxx.json`. 13 walkbacks for US market holidays (Good Friday, Christmas Eve, July 3/4, New Year, plus one iShares hiccup on 2022-07-08). Zero carry-forwards required. Universe size stable at 30 to 31 across the full window. The mid-2021 SOXX index switch (PHLX SOX to ICE Semiconductor) shows up correctly as a 6-in / 6-out membership churn on 2021-06-18.

## Data sources

- **iShares historical holdings**: `https://www.ishares.com/us/products/239705/ishares-phlx-semiconductor-etf/1467271812596.ajax?fileType=csv&fileName=SOXX_holdings&dataType=fund&asOfDate=YYYYMMDD`. Daily granularity available; earliest confirmed snapshot is 2007-06-29.
- **Prices**: `yfinance` for adjusted close history (constituents + SOXX + SPY).

## Run order

```
python -m pip install -r requirements.txt
python scripts/fetch_constituents.py       # writes data/constituents_soxx.json
python scripts/compute_breadth.py          # writes data/breadth_soxx.json
python scripts/backtest.py                 # writes data/backtest_soxx.json
pytest tests/
```

## Open questions / known limitations

- Membership held static between weekly snapshots — small misalignment around quarterly rebalance dates is accepted and documented in `fetch_constituents.py`.
- yfinance coverage of delisted historical tickers is inconsistent; see "Backtest window" decision above.
- This is a research backtest, not a live trading signal. Transaction costs and slippage assumptions are conservative but stylised.
