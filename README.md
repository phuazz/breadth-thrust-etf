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
- 2026-05-16: Step 2 (`scripts/compute_breadth.py`) complete. `data/breadth_soxx.json` covers 2,096 trading days 2018-01-05 to 2026-05-08. Signal-eligibility begins 2019-01-08 (one year of breadth history accumulated). Universe of 57 unique tickers ever-active; 46 have yfinance coverage, 11 are total losses (XLNX, MXIM, BRCM, ALTR, LLTC, CY, IDTI, MLNX, CREE, INFN, etc.). Mean per-day missing-constituent share 8.2 per cent, max 22.6 per cent in early 2018; drops below 10 per cent from 2021-06-18 onward. 163 raw signal-fire days collapse to **20 distinct signal clusters** across the window, anchored at well-known inflection points (Jan 2019 post-Q4-2018 selloff, Jun 2020 COVID recovery, Aug-Oct 2020 second-leg rally, Aug-Nov 2021, Aug-Nov 2022 bear-market rallies, Jan 2023 AI thrust, several 2023-25 follow-throughs, Apr 2026 recent thrust). Step 3 will dedupe clusters via no-re-entry-while-in-trade.
- 2026-05-16: Step 3 (`scripts/backtest.py`) complete. End-to-end validation finished — see "Results" below.

## Results (single-ETF, SOXX, 2019-01-08 to 2026-05-08)

**Headline: the strategy as specified is essentially flat and underperforms a random-entry null.** But the mechanism diagnostic shows the signal IS picking up something — the 2 x ATR(20) trailing stop is the binding constraint, not the signal.

### Primary (per-trade with exits + costs, 10 bps round-trip)
- 29 trades, 28 trailing-stop exits, 1 still open
- Win rate 44.8 per cent, profit factor 1.11
- Mean trade return +0.30 per cent, median -1.86 per cent
- Mean holding 17 days, median 14 days
- Best +33.9 per cent (2026-04 open trade), worst -9.1 per cent
- Equity curve total return -0.9 per cent over 7+ years, max drawdown 36.2 per cent
- Annualised Sharpe 0.06, Sortino 0.05 — essentially zero

### Mechanism diagnostic (fixed-horizon forward returns, NO exits)
| Horizon | Signal mean | Signal pos rate | SOXX base mean | SOXX base pos rate |
|--------:|------------:|----------------:|---------------:|-------------------:|
|     21d |      +2.57% |          70.5% |         +2.53% |             61.5% |
|     63d |      +6.44% |          73.4% |         +7.04% |             68.8% |
|    126d |     +14.91% |          88.8% |        +14.13% |             76.2% |
|    252d |     +28.97% |          86.0% |        +29.11% |             76.4% |

The signal **shifts the positive-rate distribution materially** (88.8 per cent at 126d versus 76.2 per cent base) but does **not** shift the mean. It narrows the left tail of forward outcomes without lifting average return.

### Monte Carlo null (1,000 random-entry paths, bootstrapped holding distribution)
- Strategy total return: -0.9 per cent
- Null total return p5 / p50 / p95: -17.2 per cent / +66.7 per cent / +226.7 per cent
- Strategy total-return percentile: **10.1** (worse than 90 per cent of random entries)
- Strategy win-rate percentile: 4.8
- Strategy mean-return percentile: 10.3

### Interpretation (for the next session, not this one)

All 28 closed trades exit via trailing stop. Zero regime exits, zero time stops. The 2 x ATR(20) trailing stop is firing 2-3 weeks after entry on average, well before any breadth-thrust trend has time to develop. Asymmetric reward: when the stop survives long enough (Nov 2020 +15.8 per cent, Nov 2023 +8.7 per cent, May 2025 +10.4 per cent, Apr 2026 +33.9 per cent open) the trades work; otherwise they clip out a -5 per cent loss.

Mechanism diagnostic confirms the signal carries information — positive-rate edge of +12 percentage points at 126 days is meaningful. The next session should narrow to where the exit asymmetry hurts least: either looser stops (3-4 x ATR), or different exit logic (e.g. trailing stop on closing high minus N-day low, or no stop and rely on time / regime exits), or a per-ETF tuned stop.

Per session brief, no parameter re-fit is performed here.

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
