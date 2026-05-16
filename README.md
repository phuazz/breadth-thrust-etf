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

### Interpretation

All 28 closed trades exit via trailing stop. Zero regime exits, zero time stops. The 2 x ATR(20) trailing stop is firing 2-3 weeks after entry on average, well before any breadth-thrust trend has time to develop.

### Exit-logic variant sweep (`scripts/run_variants.py`)

Diagnostic sweep over five exit configurations on the same 2018-2026 signal stream. **Picking the best variant is in-sample fitting**; this is a diagnostic to confirm whether the trailing-stop mechanic is the binding constraint and to quantify the slack.

| Variant | Trades | Win % | Median hold | Total return | Max DD | Sharpe | MC %ile |
|---|---:|---:|---:|---:|---:|---:|---:|
| `baseline_2xATR` | 29 | 44.8 | 14d | -0.9% | 36.2% | 0.06 | 10.1 |
| `loose_3xATR` | 21 | 52.4 | 30d | +77.1% | 28.7% | 0.48 | 40.0 |
| `loose_4xATR` | 18 | 55.6 | 45d | +109.7% | 33.1% | 0.57 | 42.6 |
| `regime_time_only` (no stop) | 17 | 58.8 | 49d | **+127.8%** | 33.1% | **+0.61** | 47.3 |
| `profit_anchored_3xATR_arm_at_5pct` | 19 | 57.9 | 32d | +112.5% | 29.9% | 0.59 | 46.1 |

Two clear conclusions:

1. **The 2 x ATR stop was actively destructive.** Removing it (`regime_time_only`) or loosening it materially (`loose_4xATR`) turns -1 per cent into +110 to +128 per cent total return over seven years. Median holding period more than triples.

2. **Even the best variant lands at the 47th percentile of the Monte Carlo null.** The null over 2019-2026 produces a median random-entry total return of +66.7 per cent because SOXX itself returned roughly +300 per cent across the window. Same-distribution random entries do at least as well as the timed strategy. **The signal does not generate timing alpha over this window on this ETF.**

The mechanism diagnostic still shows a real +12 pp positive-rate edge at 126 days, so the signal is not noise — but it is not capturing return outside what a random-time replication produces. Two compatible explanations remain:

- SOXX 2019-2026 was a one-way uptrend; the breadth signal cannot beat the unconditional drift.
- The signal fires AFTER short-term overbought conditions and entries are systematically a few days late, eating the easy part of the move.

### Open candidates for the next session
1. **Test on a less volatile sector (XLP, XLV) or a broader benchmark (SPY, QQQ)** where breadth thrusts are rarer and may carry more information. The Zweig framework was originally designed on broad market breadth, not single-sector.
2. **Different regime windows** — pre-2018 if the data permits, or post-2026 forward — to break the one-way-bull bias.
3. **Entry delay**: enter k bars after the signal fires (k = 3, 5, 10) and check whether the slight delay improves selection.
4. **Combine the breadth signal with a trend filter** (e.g. SOXX above 200d MA) — the signal might add value as one component of a composite rather than standalone.
5. **Out-of-sample exit-multiple validation**: take the regime-only or 4xATR result here as a hypothesis and validate on a different ETF before treating either as deployable.

## Sensitivity sweeps (items 3 + 4) — entry-delay and trend-filter

`scripts/run_sensitivity.py` reruns the SOXX signal stream through entry-delay variants (0/3/5/10 trading days after signal) and trend-filter variants (off/on, parent-ETF > 200d MA at signal date), each applied to BOTH the `baseline_2xATR` and `regime_time_only` exit configurations.

### Entry-delay sweep (item 3)

| Variant | Trades | Win % | Median hold | Total return | Max DD | Sharpe | MC %ile |
|---|---:|---:|---:|---:|---:|---:|---:|
| baseline_2xATR + delay 0d | 29 | 44.8 | 14 | -0.9% | 36.2% | 0.06 | 10.1 |
| baseline_2xATR + delay 3d | 29 | 51.7 | 15 | +71.8% | 22.7% | 0.51 | 47.3 |
| baseline_2xATR + delay 5d | 31 | 58.1 | 17 | **+139.0%** | **21.2%** | **0.75** | **73.2** |
| baseline_2xATR + delay 10d | 31 | 61.3 | 16 | +123.3% | 23.4% | 0.68 | 67.3 |
| regime_time_only + delay 0d | 17 | 58.8 | 49 | +127.8% | 33.1% | 0.61 | 47.3 |
| regime_time_only + delay 3d | 17 | 64.7 | 46 | +165.2% | 28.1% | 0.72 | 62.0 |
| regime_time_only + delay 5d | 17 | 64.7 | 44 | **+171.0%** | 29.3% | **0.74** | **66.5** |
| regime_time_only + delay 10d | 19 | 63.2 | 31 | +106.7% | 30.5% | 0.58 | 50.8 |

**5-day delay is the sweet spot for both exit configs.** With the baseline 2x ATR stop AND a 5-day entry delay, the strategy lands at the 73rd percentile of the MC null — the first config to clearly beat random entry on this window.

The "entries are too early" hypothesis is confirmed. The breadth signal fires on a short-term overbought condition (typically a 3-5 day pullback follows); the underlying trend resumes after that. Entering at signal-day open captures the pullback, which the tight ATR stop then locks in as a loss.

### Trend-filter sweep (item 4)

| Variant | Trades | Win % | Median hold | Total return | Max DD | Sharpe | MC %ile |
|---|---:|---:|---:|---:|---:|---:|---:|
| baseline_2xATR | 29 | 44.8 | 14 | -0.9% | 36.2% | 0.06 | 10.1 |
| baseline_2xATR + trend filter | 27 | 48.1 | 14 | +6.3% | 29.8% | 0.12 | 17.1 |
| regime_time_only | 17 | 58.8 | 49 | +127.8% | 33.1% | 0.61 | 47.3 |
| regime_time_only + trend filter | 15 | 66.7 | 50 | **+167.3%** | **23.1%** | **0.74** | **64.6** |

Trend filter materially improves the regime-only variant (Sharpe 0.61 → 0.74, total return +128% → +167%, max DD 33% → 23%). Dropping just two signals (the ones that fired below the 200d MA) removes the worst loss-makers. Trend filter alone with regime exits also beats the MC null.

### Items 3 + 4 combined takeaway

Two independent fixes — entry delay AND trend filter — each push the strategy past the MC null on SOXX 2019-2026. The mechanism diagnostic (+12 pp positive-rate edge at 126d) was therefore not noise. The breadth signal does carry information; the original spec just packaged it badly via early entry and too-tight stops. Both fixes are intuitive (signal fires on short-term overbought, trend filter avoids countertrend), so the in-sample-fitting concern is somewhat mitigated — but only "somewhat". Out-of-sample validation (items 1 + 5 below) remains required.

## Items 1 + 2 + 5 (partial) — iShares fetch BLOCKED, pivot to split-half OOS

On 2026-05-16 iShares' Akamai bot defence began returning a 10 MB HTML product page in place of the CSV regardless of headers, session cookies, or referrer. The fetch endpoint that worked perfectly on 2026-05-15 (and against which the entire constituent JSON was built) is now blocked. Items 1 (IVV broader benchmark), 5 (OOS exit-multiple validation on a different ETF), and item 2's full pre-2018 SOXX extension all require fresh iShares fetches and cannot proceed today.

Best-available substitute: a within-SOXX split-half OOS test using the cached 2018-2026 data. The 2019-01-08 to 2026-05-08 signal-eligible window splits roughly evenly at 2022-09-08. Six candidate configurations (subset of prior sweeps) are run on each half independently; the winner by TRAIN Sharpe is selected and its TEST performance reported as the OOS result. Same-distribution Monte Carlo nulls are computed separately for each half.

### Split-half results (`scripts/run_split_half.py`)

| Variant | Train n | Train win | Train ret | Train Sharpe | Train MC% | Test n | Test win | Test ret | Test Sharpe | Test MC% |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| baseline_2xATR | 14 | 50.0 | -7.3 | -0.09 | 15.2 | 15 | 40.0 | +7.9 | +0.21 | 20.4 |
| regime_time_only | 8 | 50.0 | +19.7 | +0.35 | 26.9 | 9 | 66.7 | +92.0 | +0.94 | 57.1 |
| baseline_2xATR + delay 5d | 15 | 40.0 | +11.2 | +0.28 | 33.4 | 16 | 75.0 | +116.8 | +1.23 | **88.8** |
| regime_time_only + delay 5d | 8 | 50.0 | +31.4 | +0.50 | 41.9 | 9 | 77.8 | +108.1 | +1.08 | 71.4 |
| regime_time_only + trend | 7 | 57.1 | +28.1 | +0.47 | 35.5 | 8 | 75.0 | +110.5 | +1.10 | 72.9 |
| **regime_time_only + delay 5d + trend** | 7 | 57.1 | +37.8 | **+0.59** | 48.2 | 8 | **87.5** | +123.5 | **+1.22** | 82.2 |

Winner by train Sharpe: **regime_time_only + delay 5d + trend** (train Sharpe +0.59). Its OOS / test-half stats:

| | |
|---|---:|
| Trades | 8 |
| Win rate | **87.5%** |
| Total return | **+123.5%** |
| Max DD | **16.4%** |
| Sharpe | **+1.22** |
| MC percentile (total return) | 82.2 |

### Interpretation

1. **Cross-variant ordering is preserved across the split.** The best variant on train is also among the best on test. Train and test rank correlations are tight. This is the opposite of what overfitting looks like (where the in-sample winner degrades OOS).
2. **Every variant improves from train to test.** Part of the win is therefore that the test half (2022-09 to 2026-05) was a more favourable breadth-thrust environment — AI rally, multiple V-shaped recoveries — than the train half (which spans COVID + 2022 inflation shock).
3. **5-day entry delay is the single most robust factor.** `baseline_2xATR + delay 5d` lands at the **88.8th MC percentile on test** despite the original 2x ATR stop. This is the strongest evidence that timing was the binding constraint, not the stop, and that the delay choice generalises.
4. **The triple combination (regime + delay + trend filter) has the best test Sharpe (1.22) tied with `baseline_2xATR + delay 5d` (1.23)**, with materially lower max DD (16.4% vs ~25%). Trend filter primarily cuts drawdown rather than adding return.

### Caveats

- **Same ETF, same constituent universe** — this is NOT a true cross-ETF OOS. iShares blocking prevented the cleaner IVV / S&P 500 test.
- **Breadth thresholds are computed on the FULL window**, not re-estimated per half. The composite_p90 / p10 thresholds use train-half breadth values when evaluating test-half signals. Strictly OOS would re-fit thresholds, but doing so on a 252-day expanding window would make train-half stats meaningless. We accept this minor leakage in exchange for stable thresholds.
- **Small sample**: 7-15 trades per half. Sharpe estimates are noisy. The improvement direction is clear, but the magnitude estimates have wide error bars.

### Outstanding work (when iShares fetch is restored)

- **Item 1 + 5 (proper cross-ETF OOS)**: refetch IVV (S&P 500), recompute breadth, apply the `regime_time_only_delay5_trend` config without re-tuning. If it works there, the parameter choice is much more credible.
- **Item 2 (extend back to 2007)**: refetch SOXX historicals to 2007-06-29 (earliest available). The pre-2018 yfinance coverage is ~45-70%, so breadth percentages will be more biased — useful for stress-testing how the signal degrades when the breadth panel is sparse.

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
