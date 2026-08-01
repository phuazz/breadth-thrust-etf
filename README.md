# breadth-thrust-etf

USD-denominated 4-sleeve breadth + momentum ETF rotation strategy with a CSP1 breadth regime overlay and an EEM/SPY relative-strength tilt. **Personal research artefact** — not investment advice, not affiliated with any regulated fund. **Live dashboard**: [phuazz.github.io/breadth-thrust-etf](https://phuazz.github.io/breadth-thrust-etf/)

## Current state (Phase 29 — July 2026)

The deployed strategy is the **35/35/10/20 A:B:C:D blend** with two overlays:

| Sleeve | Mechanism | Universe | Weight |
|---|---|---|---|
| **A** US sectors | Sector-RELATIVE breadth (Phase 20.1) — sector breadth minus cross-sectional mean, rank top K=7 by relative value, weight by positive-relative share | 14 US sector ETFs | 35% |
| **B** Asset-class | ETF-level momentum (% above own 200d MA), top K=7 by signal, weight by signal share, SHY cash floor when fewer than K positive | 12 broad asset-class ETFs (HYG removed Phase 24 — equity-correlated, not defensive; EEM moved to overlay-only Phase 29 — held solely via the Phase 22 tilt) | 35% |
| **C** Thematic | Same as B but on thematic ETFs with +5% signal floor, top K=5 equal-weight (Phase 27); sleeve-breadth gate — all to SHY when <30% of the universe clears the floor (Phase 27) | 25 thematic ETFs (PHO, IHI added Phase 25) | 10% |
| **D** Europe sectors | Same as A but on Stoxx Europe 600 sector UCITS, top K=3, ABSOLUTE breadth (not sector-relative). **EUR prices FX-converted to USD** (Phase 20.2) | 5 Stoxx Europe 600 sector UCITS | 20% |

**Phase 19 risk overlay**: when S&P 500 constituent breadth falls below 20%, shift 50% of NAV to SHY (1-3y Treasury). Re-engage full blend when breadth crosses back above 50%.

**Phase 22 EEM tilt**: when EEM/SPY ratio's 50d MA crosses above its 200d MA (golden cross), tilt 10% of NAV to EEM. Funded from Strategy B (35% → 25% during tilt-ON).

**Backtest stats (gated + EEM-tilted, 2018-Q4 to 2026-Q2, post-Phase 29)**: Sharpe +1.30, CAGR +15.5%, max DD -16.2%. All return figures USD-denominated. Walk-forward Sharpe (annual K refit) for A is +1.00, B is +0.77, C is +0.51 (largest IS-vs-OOS gap), D is ~+0.85.

### Known caveats (acknowledged upfront)

- **Survivorship bias in Strategy C** — 25 thematics all survived to 2026; failed thematics (cannabis, leveraged-thematic, volatility) never in universe. WS3 (2026-07) quantified the bias: BTC-USD alone contributes ~23% of gross sleeve return (top five names ≈ 62%), several post-hoc adds carry history backfilled to before their inclusion, and no point-in-time membership exists. Sleeve C is **KEEP, ON NOTICE** — it loses to its own equal-weight basket at realistic spreads and must justify its seat at the next scheduled review.
- **Phase 19 overlay parameters tuned in-sample** — chosen from 12-variant sweep on same data the gated-vs-ungated comparison reports on.
- **Phase 22 EEM tilt deployed on weak sample** — golden cross has produced few distinct ON-events in 7.5y. WS3 (2026-07): 6 distinct bets ever, bootstrap P(mean > 0) ≈ 0.56, placebo 82nd percentile — retained solely as the designated EM expression, not robustly-evidenced alpha. The tilt feed carries a 10-day staleness cap: if the EEM/SPY cache stalls, the tilt is held flat (baseline blend) rather than freezing the last signal.
- **Strategy D execution-watch** — cost-fragile: the WS3 break-even is ~1.75x of the assumed 15 bps round-trip. Monitor realised Xetra UCITS spreads against the 9 bps one-way assumption; the sleeve's edge is thin if European trading costs run hotter.
- **Strategy D data rebuilt 2026-08-01 (Phase 30)** — the five European breadth series were rebuilt after fixing holiday-NaN window poisoning, unresolved Nordic/CH/AT/PL/IE/CZ exchange strings, and the NYSE-grid sampling. Aggregate ma_breadth coverage (membership-weighted, n>=40) rose from 61.4% to 97.2% and the recurring April-July holes are gone. D-sleeve signal history therefore shifts when engines next re-run; all WS-era D statistics predate the rebuild and should be revalidated at the next scheduled review. Residual survivorship: 66 genuinely delisted constituents (2018-2026 M&A and take-privates) plus 30 old-root alias candidates still have no yfinance history — classification and recovery plan in `reviews/2026-08-01_phase30_residual-constituents.md`.
- **Sharpe sample noise** — 7.5y × weekly gives Sharpe SE ≈ ±0.4; +1.29 sits in ~95% CI [+0.55, +2.03]. Treat as range, not point estimate.
- **Walk-forward scope narrow** — covers within-sleeve K refit only. Weighting scheme, universe additions, cost calibration, overlay parameters all applied across entire backtest as in-sample.
- **No live track record** — every return shown is simulated.
- **Backtest costs conservative-not-pessimistic** — 2-9 bps per unit weight change; no slippage / market-impact model.

### Test coverage

468 pytest tests across 37 files. Key suites:
- `test_backtest_math.py` (17 tests) — structural invariants: long-only, sum-to-100%, no NaN, monotonic dates.
- `test_weight_function_edge_cases.py` (12 tests) — direct unit tests on `top_k_breadth_weight` with synthetic stress inputs (all-positives, all-negatives, mixed, NaN, ties, single-element). Includes regression test that would have caught the Phase 20 long-only bug.
- `test_data_integrity.py`, `test_stale_breadth.py` — freshness guards on iShares constituent caches.
- `test_no_lookahead.py` — verifies signals use `.shift(1)` before weight assignment.

### CI / weekly publish

`.github/workflows/weekly_factsheet.yml` is **event-driven (2026-07-25)**: it fires on the push that lands the local weekend refresh (any commit touching `data/breadth_csp1.json`), gated by `scripts/check_factsheet_gate.py` — the panel must be current to the week-final NYSE session (Thursday in a Friday-holiday week; the cadence rule "a Friday-holiday factsheet dated Thursday is correct, not stale" is preserved through `nyse_sessions.week_final_anchor`) and the week must not already be published (committed `docs/factsheet_published.json` marker, written only after a successful non-trial email). On a publishable event it refreshes strategy engines + blend + overlay, rebuilds dashboard + factsheet PDF, commits refreshed data + docs to main, emails the factsheet via Gmail SMTP, then commits the marker. This replaced the old Friday 22:00 UTC cron, which every normal week mailed a factsheet whose A/D positions and regime flag were a week stale — A and D re-anchor only when the local refresh lands, so the email now waits for it and always carries the complete position changes. A Sunday 09:00 UTC (17:00 SGT) scheduled run is **check-only**: it emails the operator when the week's factsheet has not gone out, distinguishing refresh-missing (run `refresh_all.py`; the push then publishes automatically) from refresh-landed-but-publish-failed (inspect the run, re-dispatch). `workflow_dispatch` remains for trial sends and forced re-issues. `.github/workflows/daily_live_track.yml` (Mon-Fri 21:30 UTC) extends the deployed blend through the latest daily close.

**Ops alerting (2026-07-03; tiered 2026-07-25)**: both workflows email the operator (GMAIL_USER, not the factsheet distribution list) on any failure, and run an early freshness check from weekday-lag 4 on `breadth_csp1.json` via `scripts/check_freshness_headroom.py` — the pipeline's hard guard aborts publishes once that lag exceeds 5, so the alert arrives one to two runs before the dashboard would freeze. Because the weekly local-refresh cadence lands every normal week at lag 4–5 by Thursday/Friday, those routine states are tagged `[REMINDER]` (weekend refresh window still ahead); `[WARN]` is reserved for mid-week staleness, the hard stop, or a checker error. The daily workflow sends the reminder; the weekly workflow's tripwire only emails at the hard stop (running post-refresh, it normally reports ok). Weekend coverage is the Sunday check-only schedule on `weekly_factsheet.yml` described above — it briefly existed as a separate lag-based `sunday_last_call.yml` (2026-07-25, same day), which was superseded within hours because the marker-based check is a strict superset: panel lag can only detect a missing refresh, while the marker also catches a refresh that landed whose publish run then failed.

**Scheduled local refresh (2026-07-25)**: the weekly `refresh_all.py` run is automated on the operator's machine via `scripts/scheduled_refresh.py`, registered as Windows scheduled task `BreadthThrust-WeeklyRefresh` (Saturday 06:00 SGT, catch-up on next power-on if the machine is off, runs when logged on — locked is fine). It operates in a dedicated automation clone (`C:\dev\breadth-thrust-etf-sched`) so it never collides with interactive sessions in the main tree. Guard layers per the vault's unattended-agent rule: clean-tree preflight + `git pull --rebase`; `refresh_all.py` exit 0 (every step green including pytest); the panel must reach `nyse_sessions.week_final_anchor` (catches quietly-stale fetches that exit 0); a local preview of the CI publish-gate decision; and in soak mode it stops before pushing and reports READY for manual review. Arming (`--push` on the task action) is scheduled after two clean soak Saturdays; even then the push only triggers the gated CI flow, which re-verifies everything before any email. Failure alerting: best-effort local email using `GMAIL_USER`/`GMAIL_APP_PASSWORD` environment variables (optional) plus a dated log under `logs/` (gitignored); the guaranteed backstop is the Sunday CI held-factsheet check, which needs nothing from this machine.

**Silent-wrong-data defences (2026-07-03)**: process status alone cannot prove the data is right, so two outcome-level checks exist. In-run, `scripts/check_capture_integrity.py` anchors the series each job just fetched (B, C, live track in the weekly; live track in the daily) to the true NYSE calendar (`scripts/nyse_sessions.py`, holiday- and early-close-aware): 1 session behind warns and still publishes for the live track, while the weekly run treats a B/C 1-behind as a hard fail (`--strict b,c`, added 2026-07-18 after the 2026-07-17 factsheet shipped without the Friday rebalance — the engines' exclusive-end yfinance fetch fencepost that caused it is fixed in the same change); 2+ behind or a corrupt/implausible tail fails the job before anything is built or emailed. Outside-in, `.github/workflows/sentinel.yml` (daily 03:35 UTC = 11:35 SGT — sized to GitHub's cron-delay tail, which this repo has measured at up to ~4h) fetches the LIVE site's `factsheet_meta.json` and emails `[SENTINEL]` if the deployed as-of does not match the calendar — it shares no state with the pipeline, so it catches a green run that published wrong artefacts. Remaining judgement-level checks (Data Health panel consistency, regime/tilt cross-file agreement) live in the `VERIFY_DASHBOARD.md` audit prompt.

The heavy constituent-roster refresh runs LOCALLY (per-ETF parquet caches are gitignored due to size). CI relies on committed breadth JSONs being current; if they go stale beyond 14 days, sleeves degrade gracefully (signal NaN → sleeve goes flat) with a visible stale-data banner.

---

## Phase history

The current architecture evolved over ~30 phases of empirical iteration. Major milestones:
- **Phases 1-3** (original README below): composite breadth signal on SOXX → generalised to MA200-only across 11 US ETFs
- **Phase 4**: introduced Europe sector sleeve (D); moved from 3-way 45/45/10 to 4-way 35/35/10/20
- **Phases 5-17.1**: thematic universe expansion (ITA, BTC-USD, XME, WOOD, REMX, CQQQ, 159801.SZ); Phase 6 equal-weight for C
- **Phase 19**: CSP1 breadth regime overlay deployed
- **Phase 19.1**: IEF→SHY cash floor swap (IEF correlated with equities in 2022 inflation crash)
- **Phase 20 / 20.1**: sector-RELATIVE breadth in A; Phase 20.1 fixed long-only bug under relative signal
- **Phase 20.2**: Strategy D EUR→USD FX conversion (previously contaminated the USD blend)
- **Phase 22**: EEM/SPY relative-strength tilt overlay
- **Phase 24**: removed HYG from B's universe
- **Phase 25**: thematic universe expansion to 25 (PHO water, IHI medical devices)
- **Phase 26.2/26.3**: SEC EDGAR N-PORT fallback for SOXX constituents + per-ETF staleness policy (DATA_INTEGRITY_POLICY.md)
- **Phase 27**: Strategy C moved to K=5 equal-weight + 30% sleeve-breadth gate (exit to SHY)
- **Phase 29** (2026-07-02): EEM moved to **overlay-only** — removed from B's rotation universe after the WS2 universe review found it double-counted against the Phase 22 tilt (see `reviews/2026-07-02_ws2_universe.docx`); EM exposure is expressed solely by the tilt
- **Phase 30** (2026-08-01): European breadth data-integrity fix — indicators computed per traded session (a single home-venue holiday NaN no longer kills a ticker's MA50 for 50 rows), breadth sampled on XETR instead of NYSE for the five D-sleeve funds, and ten previously-unmapped exchange strings (Nasdaq Nordic/Helsinki/Copenhagen, Oslo, Vienna, Warsaw, Prague, SIX case-variants, Dublin, LSE slash notation) now resolve to yfinance symbols. Aggregate D-universe coverage 61.4% → 97.2%; series extend to the present; the recurring April-July, December-February and September-October holes are eliminated. Guard tests in `tests/test_european_breadth_fix.py`

Numbers and ETFs in the historical Phase 3 README below refer to the obsolete single-strategy architecture — see the dashboard for current deployed state.

---

## Historical README — Phase 3 era (obsolete, kept for context)

## Current state (Phase 3 — May 2026)

**Headline finding**: % of constituents above 200d MA, used as a regime indicator with 50% base + 100% leveraged on-signal sizing (50/150), beats buy-and-hold on Sharpe on 8 of 11 ETFs tested.

**Per-ETF MA200 + 50/150 results** (in-sample, 2019-2026 window):

| ETF | Description | Best L | Sharpe | BH Sharpe | Δ |
|---|---|---:|---:|---:|---:|
| SOXX | Semiconductors | 65 | +1.07 | +0.98 | +0.09 |
| CSP1 | S&P 500 | 55 | +1.04 | +0.84 | **+0.20** |
| CNDX | NASDAQ-100 | 55 | +1.05 | +0.92 | +0.13 |
| IUES | Energy | 60 | +0.60 | +0.49 | +0.11 |
| IUFS | Financials | 75 | +0.71 | +0.53 | **+0.18** |
| IUIT | Info Tech | 60 | +1.09 | +0.96 | +0.13 |
| IUHC | Health Care | 80 | +0.55 | +0.60 | -0.05 |
| IUIS | Industrials | 50 | +0.74 | +0.68 | +0.06 |
| IUCS | Cons Staples | 65 | +0.48 | +0.68 | -0.20 |
| IUCD | Cons Discretionary | 50 | +0.68 | +0.59 | +0.09 |
| IUUS | Utilities | 60 | +0.51 | +0.59 | -0.08 |

The three losing ETFs (IUHC, IUCS, IUUS) are the classic defensive sectors. Their constituents tend to rally during risk-off rotation, so a "buy when regime is healthy" filter actively works against them. The signal is therefore best applied to cyclical / growth / broad-market exposure, not defensives.

**Portfolio construction**: rank the 11 ETFs by current ma200_breadth each week; equal-weight (or breadth-weight) the top K; rebalance weekly with 10bps round-trip.

| Variant | Sharpe | Total return | Max DD |
|---|---:|---:|---:|
| **Top 7 leveraged (50/100 overlay on basket)** | **+1.00** | +349% | **25%** |
| Top 7 breadth-weighted leveraged | +1.00 | +375% | 25% |
| Top 5 breadth-weighted unleveraged | +1.00 | +298% | 31% |
| Equal-weight all 11 (benchmark) | +0.81 | +215% | 35% |
| SPY buy-and-hold (benchmark) | +0.77 | +193% | 34% |

The relative-strength tilt (top-K by breadth) materially improves both Sharpe and max DD over equal-weight-all and over SPY. Best variant beats SPY by 0.23 Sharpe with 9 pp lower drawdown.

**Signal**: very simple. Per ETF, compute % of constituents above their 200d MA. Pick a long threshold L (per-ETF, in the 50-75% range — peak Sharpe across the sweep). Allocation = 50% always; step up to 150% when ma200_breadth ≥ L. Rebalance weekly. That's the entire strategy.

## Research journey (historical)

This project went through three explicit phases before landing on the MA200 finding:
1. **Phase 1**: composite signal (RSI breadth + MA breadth + Highs breadth + Zweig thrust + entry delay + trend filter) tuned on SOXX. Sharpe +1.19 in-sample, +1.42 OOS on the held-out test half. Beat BH on SOXX but did not generalise to other ETFs.
2. **Phase 2**: explored size-scaling, leverage, multi-ETF rotation, and master regime filters on the composite signal. Confirmed the composite signal added value only on SOXX.
3. **Phase 3 (current)**: replaced the composite with a single % above 200d MA indicator. Generalises to 8 of 11 ETFs, beats SPY in portfolio form, materially simpler. The dashboard archives this approach.

The composite signal's research is preserved in the git history (see commits before `9e0...` / READMEs below) and in data files prefixed `backtest_`, `improvements_`, `tuning_`, `oos_validation_`, `sensitivity_`. The current dashboard exposes only the MA200 results.

## Original brief (kept for historical context)

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

## Alternative data sources surveyed (2026-05-17)

After iShares US blocked, we systematically probed alternatives. Findings:

### Working today

| Source | Endpoint pattern | History depth | Notes |
|---|---|---|---|
| **iShares UK** | `ishares.com/uk/individual/en/products/<pid>/<slug>/<ajax>.ajax?fileType=csv&...&asOfDate=YYYYMMDD` | Daily, back to ~2014 for major funds | Cloudflare configured differently from US. **CSP1** (S&P 500 UCITS) returns full 503 constituents. **CNDX** (NASDAQ 100 UCITS) returns 101 constituents. Both full-replication. |
| **iShares Switzerland** | `ishares.com/ch/individual/en/products/<pid>/<slug>/<ajax>.ajax?...` | Same as UK | Same fund data via Swiss path. Useful redundancy. |
| **State Street SSGA** | `ssga.com/.../holdings-daily-us-en-spy.xlsx` | Current only | XLSX download for SPY. No `asOfDate` query parameter for history. |
| **SEC EDGAR N-PORT** | `efts.sec.gov/LATEST/search-index?forms=NPORT-P` | Monthly snapshots quarterly-filed, 2019-present | All US-registered funds. XML/JSON. Requires a dedicated extractor (not implemented). The serious fallback for serious work. |

### Tested today and not viable

- **iShares US (`ishares.com/us/...`)** — Akamai bot defence returns 10 MB HTML in place of CSV
- **BlackRock parent (`blackrock.com/us/individual/...`)** — same Akamai block as iShares US
- **iShares Germany (`ishares.com/de/privatanleger/de/...`)** — different URL pattern, 404 on direct adaptation
- **Invesco QQQ direct URL** — 406 on probed endpoint; correct endpoint not found in time-boxed search
- **iShares CSPX (other share class)** — UCITS sample replication, only 30-107 names. Not usable for breadth.

### Paid alternatives (not tested)

Bloomberg Terminal, FactSet, S&P Capital IQ, Refinitiv, Polygon.io, CRSP/Compustat. The user has CFA-side Bloomberg access; not callable from CLI.

### Practical mapping

- **S&P 500 (IVV / SPY / VOO)**: iShares UK **CSP1**. 503 names, daily granularity, 2014-2026. **Used this session for item 1 + 5.**
- **NASDAQ 100 (QQQ)**: iShares UK **CNDX**. 101 names, daily.
- **Russell 2000 (IWM)**: probe iShares UK for an equivalent (none confirmed).
- **Single sectors (XLK, XLV, etc.)**: no direct UCITS equivalents for all 11 SPDR sectors. Falls back to SEC EDGAR N-PORT.
- **SOXX (this project)**: already cached 2018-2026 via US endpoint before block. To extend back to 2007 would need either US unblock OR a UK semiconductor UCITS equivalent (none confirmed).

## Item 1 + 5 (proper) — true cross-ETF OOS via CSP1 (S&P 500)

Confirmed CSP1 endpoint works → built the full pipeline. Refactored `fetch_constituents.py`, `compute_breadth.py`, and `backtest.py` to be ETF-parameterised via `scripts/etf_registry.py`. Pass `--etf SOXX` (default, unchanged behaviour) or `--etf CSP1`.

### CSP1 pipeline output

- **437 weekly snapshots** 2018-01-05 → 2026-05-15
- 0 walkbacks, 10 carry-forwards (5 from iShares data gaps, 5 from late-session DNS hiccups carry-forwarded from 2026-04-10)
- 503 unique tickers per snapshot (full S&P 500)
- yfinance coverage: 96-100% in 2018-2026
- 2,101 trading days of breadth, **113 signal-fire days**, signal-eligible from 2019-01-08 (matches SOXX)
- Mean missing-constituent share 8.0%, max 18.0%

### Cross-ETF OOS backtest (`scripts/run_csp1_oos.py`)

Three configs applied to CSP1 breadth signals, traded on SPY OHLC (S&P 500 in USD). **No re-tuning** — the configs are exactly what won on SOXX.

| Variant | Trades | Win % | Median hold | Total ret | Max DD | Sharpe | MC %ile |
|---|---:|---:|---:|---:|---:|---:|---:|
| baseline_2xATR | 26 | 42.3 | 17d | -11.1% | 17.9% | -0.19 | 5.0 |
| regime_time_only | 14 | 64.3 | 68d | +33.3% | 15.5% | +0.44 | 27.7 |
| regime_time_only + delay 5d + trend | 15 | 53.3 | 49d | **+42.0%** | **14.0%** | **+0.59** | 43.4 |

### Interpretation — the honest read

1. **Parameter ordering generalises across ETFs.** The same ranking holds on both SOXX and S&P 500: `baseline_2xATR` is worst, `regime_time_only` is middle, `regime_time_only_delay5_trend` is best. Adding the entry delay + trend filter helps in both universes.
2. **But the signal magnitude does NOT generalise.** On the SOXX OOS test half the winning config produced Sharpe +1.22 and 82nd-percentile MC; on the broader S&P 500 the same config delivers only Sharpe +0.59 and 43rd-percentile MC — *underperforming a random-entry null*.
3. **The breadth-thrust mechanism is sector-concentrated.** A diverse 500-name universe dilutes the correlated breadth surges that the signal feeds on. Semis (30 names, highly correlated) produces clean breadth-thrust events; the S&P 500 (500 names across 11 sectors) does not.
4. **The 2x ATR stop is structurally bad on both.** -0.9% on SOXX, -11.1% on S&P 500. The destructive-stop finding is robust.

The result is consistent with the CLAUDE.md backtesting principle: "If a backtest Sharpe is low, narrow the universe to where the signal mechanism is structurally strongest." SOXX is structurally strong for this signal; the S&P 500 is not. The strategy is therefore not a generic "breadth-thrust on any ETF" framework — it works on sector-concentrated universes, not broad benchmarks.

### Honest comparison: SOXX (test half) vs CSP1 (full window), same config

| | SOXX OOS test half | CSP1 (S&P 500) full window |
|---|---:|---:|
| Window | 2022-09 → 2026-05 | 2019-01 → 2026-05 |
| Trades | 8 | 15 |
| Win rate | **87.5%** | 53.3% |
| Total return | +123.5% | +42.0% |
| Sharpe | **+1.22** | +0.59 |
| Max DD | 16.4% | 14.0% |
| MC %ile | 82.2 | 43.4 |

The half-window comparison is somewhat unfair (the CSP1 window includes the difficult 2022 bear market while the SOXX test half misses the early 2022 drawdown), but even adjusting for that, the CSP1 result is meaningfully weaker. The S&P 500 is the wrong universe for this signal.

### Next session candidates

1. **Sector-concentrated ETFs**: test on XLF (financials), XLE (energy), XLU (utilities), XBI (biotech). The hypothesis is that breadth thrusts in narrow sectors carry more information than in broad indexes.
2. **NDX-100 (CNDX) test**: closer to S&P 500 in size but more sector-concentrated (mostly tech). Should fall between SOXX and S&P 500 in signal strength.
3. **Extend SOXX back to 2007** if iShares US unblocks. Test signal in the GFC + early-2010s regimes.
4. **Position-sized portfolio**: rather than pick one ETF, combine signals across N sector ETFs with equal sizing. The diversification might smooth the equity curve without diluting the per-signal edge.

## Cross-sector OOS sweep (next-session item 1, executed) — does the parameter choice transfer?

Three additional iShares UK funds added to the registry and run through the same fetch → breadth → OOS backtest pipeline, applying the SOXX-tuned configs without re-tuning:

- **IUES** — S&P 500 Energy Sector UCITS (22-32 constituents) — traded via **XLE** (SPDR Energy Select Sector)
- **IUFS** — S&P 500 Financials Sector UCITS (67-71 constituents) — traded via **XLF** (SPDR Financial Select Sector)
- **CNDX** — iShares NASDAQ 100 UCITS (101-104 constituents) — traded via **QQQ** (Invesco QQQ Trust)

Three configs run on each: `baseline_2xATR`, `regime_time_only` (SOXX exit-logic winner), `regime_time_only_delay5_trend` (SOXX split-half winner).

### Cross-ETF result matrix (Sharpe / MC %ile, 2019-01-08 to 2026-05-15)

| ETF | Universe | baseline_2xATR | regime_time_only | regime+delay5+trend |
|---|---:|---|---|---|
| **SOXX** (semis) | 30 | 0.06 / 10 | 0.61 / 47 | **0.74 / 65** |
| **IUES** (energy) | 22-32 | **0.23 / 46** | 0.11 / 28 | 0.09 / 30 |
| **IUFS** (financials) | 67-71 | **0.09 / 23** | 0.06 / 16 | 0.01 / 15 |
| **CNDX** (NDX-100) | 101 | 0.19 / 22 | 0.29 / 19 | **0.51 / 39** |
| **CSP1** (S&P 500) | 503 | -0.19 / 5 | 0.44 / 28 | **0.59 / 43** |

### Five honest findings

1. **Only SOXX has a config that beats random entry**, and only the regime+delay+trend variant does so (65th percentile of the MC null). On every other ETF, the best config still underperforms the MC null. The split-half SOXX OOS test (82nd percentile on test half) was a window-favoured outcome, not a generic effect.

2. **Parameter choices are sector-dependent, not universal.** On trending tech-heavy universes (SOXX, CNDX, CSP1), the SOXX-tuned `regime+delay+trend` config wins. On mean-reverting cyclicals (Energy, Financials), the original tight 2× ATR stop wins. The "destructive stop" verdict from SOXX is therefore not a universal truth — it depends on whether the underlying tends to trend post-thrust.

3. **Universe size does not predict signal strength cleanly.** IUES (22 names — narrower than SOXX) has weaker results than CNDX (101 names) or CSP1 (503 names). What seems to matter is the underlying's tendency to trend after a breadth surge. Energy is cyclical → breadth thrusts often mark exhaustion. Tech / broad market tend to trend → breadth thrusts mark acceleration.

4. **Financials is the worst sector for this signal** — Sharpe ≤ 0.09 in every config, MC %ile ≤ 23. The breadth-thrust mechanism does not work on regime-driven sectors where the underlying rotates between bull and bear regimes on macro catalysts.

5. **The signal is a SOXX phenomenon more than a sector-concentration phenomenon.** Originally we conjectured "narrow sectors → strong signal" — the data does not support that. SOXX is the outlier; even adjacent narrow tech universes (CNDX) do worse, and similar-size narrow sectors (IUES) do worse still.

### Summary verdict

The signal carries some marginal information (positive-rate edge at 126d on SOXX) but the strategy does NOT generalise as deployable across sectors. SOXX is the home universe; everything else underperforms a random-entry null at the same trade count and holding distribution.

This is the kind of result CLAUDE.md anticipates: "narrow the universe to where the signal mechanism is structurally strongest." After surveying 5 ETFs, that universe is SOXX. Further work would either (a) accept SOXX-only deployment with realistic capacity caveats, or (b) attempt to identify what makes SOXX different (semiconductor capex cycle? earnings clustering? supply-chain correlation?) so the same property can be found in other universes.

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
