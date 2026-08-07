# ETF cross-sectional technical scanner — implementation spec (for Claude Code)

## 0. Background and goal

Add a daily-updated, cross-sectional ETF technical scanner page to the existing breadth-thrust-etf repository. This is a monitoring panel, not a trading strategy: do not modify any existing strategy scripts or JSON outputs. Output is a static HTML page (docs/scanner.html) built daily by GitHub Actions and published via GitHub Pages.

The design is finalized on the owner's side: main view = alert strip (events) on top + ranked main table (state), with columns grouped into four zones (Directional signals / Risk signals / State & confirmation / ETF layer). This spec describes implementation only — do not redesign.

## 1. Integration

Three new components, reusing existing infrastructure:

1. `scripts/run_scanner.py` — fetch/incrementally update price data, compute all indicators, write `data/scanner_latest.json`
2. `.github/workflows/scanner-daily.yml` — cron `30 22 * * 1-5` (UTC; ≈06:30 SGT the next morning; the same calendar day's US, Xetra, and China sessions have all closed by then). Steps: checkout → install deps → run_scanner.py → build scanner.html → commit & push docs/
3. `docs/scanner.html` — static page with data injected as embedded JSON (same template-injection pattern as pipeline.py); add a nav link on index.html

Reuse: the existing yfinance download/cache logic; the existing USDCNY=X FX-conversion pipeline for 159801.SZ; the existing SPX constituent-breadth JSON (for the overlay status chips, §5).

## 2. Universe (54 tickers)

**Source of truth is `scripts/etf_registry.py`.** The table below was extracted from the dashboard page; cross-check against the registry at build time and report any differences explicitly — never silently adopt either side.

| Sleeve | Tickers | Notes |
|---|---|---|
| A (14, trade proxies) | SPY, QQQ, IJR, SOXX, XLE, XLF, XLV, XLI, XLP, XLY, XLU, XLB, XLC, XLRE | Scan the trade proxies, not the UCITS originals (official mapping CSP1→SPY, CNDX→QQQ, IDP6→IJR, sector UCITS→SPDR XLs, IUSP→XLRE); better data quality and these are the actually-traded instruments |
| B (12) | SPY, IJR, QQQ, EFA, VGK, EWJ, VNQ, GLD, DBC, TLT, IEF, TIP | Phase 29 definition: HYG removed, EEM moved to overlay |
| C (25) | ARKK, CIBR, SKYY, BOTZ, BLOK, ICLN, TAN, LIT, URA, XBI, ARKG, JETS, GDX, COPX, MOO, PAVE, ITA, IBIT, XME, WOOD, REMX, CQQQ, 159801.SZ, PHO, IHI | BTC exposure via IBIT (short history — §7 degradation rule); 159801.SZ converted to USD via the existing FX pipeline |
| D (5) | EXV1.DE, EXH1.DE, EXV3.DE, **EXH4.DE**, EXH9.DE | Xetra, EUR-denominated; convert to USD series (§7). Registry key `EXH3` is the Industrial Goods & Services panel and its fund trades as **EXH4.DE** — corrected 2026-08-03; `EXH3.DE` is the Food & Beverage fund. Resolve through `etf_registry.yfinance_trading_proxy`, never by appending `.DE` to the panel key |
| Overlay | EEM | EM-tilt instrument |

Deduplicated: 54 rows. SHY excluded (cash proxy; technical signals on it are meaningless) — can be added later. ETFs belonging to multiple sleeves carry multiple tags (e.g., SPY: A, B).

**ETF names**: display the fund's long name next to the ticker. Source order: registry name field if present; otherwise yfinance `Ticker.info["longName"]`, fetched once and committed as a static names dict (`data/etf_names.json`). Never fetch names on the daily run — `info` calls are slow and flaky, and names rarely change. Display: truncate at ~28 characters with an ellipsis; full name in the tooltip.

## 3. Column definitions (16 columns, four zones)

All return/momentum computations use yfinance `auto_adjust=True` adjusted series; ATR uses the same adjusted OHLC (one consistent source throughout). "Percentile" = current value vs the same indicator's own trailing 504 trading days.

Identification
1. **Ticker**
2. **Name** — per §2

Directional signals
3. **Trend state** (discrete badge): Strong up = C > MA50 > MA200 and both MAs' 20-day slopes > 0; Up = C > MA200 but alignment/slopes incomplete; Range = C between MA50 and MA200, or |MA50/MA200 − 1| < 1%; Down / Strong down symmetric. MAs are SMAs.
4. **Rank** (global cross-section): compute 1M/3M/6M/12M total returns per ETF → percentile-rank each horizon within the 54 → equal-weight average → rank (1 = strongest). Also compute within-sleeve rank for the filter view.
5. **ΔR 20D**: Rank(t−20 trading days) − Rank(t); positive = improving; shown as ↑n / ↓n / —.
6. **12-1%**: P(t−21) / P(t−252) − 1.
7. **vs 52W high**: C(t) / max(C over past 252 trading days) − 1, close series.

Risk signals
8. **RV pctl**: RV = std(daily log returns, 20D) × √252; 504-day percentile.
9. **BBW pctl**: BBW = 4σ_P / MA20, where σ_P = 20-day standard deviation of closing price levels; 504-day percentile.
10. **ATR%**: TR = max(H−L, |H−C_prev|, |L−C_prev|); ATR = 14-day Wilder smoothing; ATR% = ATR / C, shown as a raw %.

State & confirmation
11. **1D%**: C(t)/C(t−1) − 1.
12. **Vol ×20D**: V(t) / SMA(V, 20D).
13. **RS 1M**: RS = ln(P_etf,t / P_etf,t−21) − ln(P_spy,t / P_spy,t−21). **Benchmark = SPY for every row** (owner decision 2026-08-03; supersedes the earlier hierarchical mapping — no additional benchmark tickers are needed). SPY's own row shows "—". Shown in percentage points on one global heat scale. Recorded design note: with a single benchmark, RS ordering is equivalent to 1M-return ordering; the column is retained as a "vs US equities" magnitude read. For bonds and commodities, interpret it as inverse-equity-beta, not intra-group leadership.
14. **Dev 200D**: C/MA200 − 1.

ETF layer
15. **P/D%**: (C − NAV)/NAV; NAV per the §6 snapshot mechanism.
16. **5D flow%**: (SO_t − SO_{t−5}) / SO_{t−5}; SO = shares outstanding, per §6.

Sleeve tags (A/B/C/D/OV) are a filter attribute rendered as chips above the table; a visible column is optional.

## 4. Alert rules (all thresholds are placeholder defaults)

Check all 54 daily; each trigger produces a chip (ticker + event + value):
1. 52-week high / 52-week low (close basis)
2. Cross above / below MA200 (yesterday on the other side, today's close through)
3. Daily |return| > 2σ (σ = 20-day daily-return std)
4. Volume > 3 × 20-day average volume
5. Squeeze: RV pctl < 25 AND BBW pctl < 10 (both-low requirement); squeeze release = in squeeze state and today's |return| > 1.5σ
6. RSI14 ≥ 75 or ≤ 25 (Wilder smoothing)
7. ETF layer (enable once data is available): |P/D − 1Y mean| > 2 × 1Y std; daily |ΔSO/SO| > 1%

Chips are color-coded by category; when more than 12 fire, truncate by priority (ETF layer > squeeze > cross/52W events > statistical > RSI > volume) and show "+n more".

## 5. Overlay status chips (read existing data — do not recompute)

One row of two status chips above the table:
- **Breadth gate**: read the existing SPX constituent-breadth JSON produced by the pipeline; show the current value and RISK_ON / RISK_OFF state (thresholds off=20% / on=50% come from the existing config — do not define new ones)
- **EM tilt**: EEM/SPY price-ratio 50D/200D MA state (golden-cross test); ON/OFF + distance to the cross in %

## 6. NAV / shares-outstanding snapshot accumulation

yfinance provides only same-day snapshots (`Ticker.info` navPrice / sharesOutstanding), no history. Implementation:
- On each daily run, fetch snapshots for all 54 and append to `data/scanner_snapshots.csv` (date, ticker, nav, so, close), committed with the repo — history accrues naturally
- **P/D value column**: shown from day 1 for tickers where navPrice exists; the σ-normalized alert enables after ≥120 trading days of snapshots; in the interim use absolute alert thresholds (US broad/sector |P/D| > 0.3%; cross-border (EEM/CQQQ/IBIT etc.) |P/D| > 1.0%)
- **5D flow**: enables automatically once 5 trading days of snapshots exist; show "—" before that
- NAV for 159801.SZ and the five Xetra funds is generally unavailable in yfinance: show "—"; never fill with proxy values

## 7. Engineering and data rules

- **Multi-market calendars**: compute each ticker's indicators on its own trading calendar; do not force-align to NYSE. Rank uses each ticker's latest EOD value; the footer shows per-market data dates (US / DE / CN)
- **Xetra and 159801.SZ**: convert to USD series first (Xetra via EURUSD=X; 159801.SZ reuses the existing USDCNY pipeline); rank and all indicators are computed on USD series for cross-sectional comparability, consistent with the main site's USD basis
- **IBIT short history**: percentile window = min(504, available history); cells using a shortened window carry a "^" marker; indicators requiring more than the available history (below 252 days) show "—"
- **Data health**: any ticker whose latest data is stale by > 3 trading days → gray out the row and list it in a Data-Health-style footnote; retry fetches 3×; a single ticker failure must not block the page build, but ≥5 failures abort the build (consistent with the repo's data-integrity policy)
- **Page**: static HTML + embedded JSON + vanilla-JS sort/filter; no backend. Default sort by Rank; click column headers to sort; sleeve filter chips; Name column truncation + tooltip; horizontal scroll on mobile; visual style consistent with the main site — no new frameworks
- **Performance**: incremental updates for 54 tickers × ~10y of daily bars, reusing the existing cache; expected Actions runtime 3–6 minutes

## 8. Parameter freeze (hard constraint on the implementer)

All parameters are industry defaults: RSI 14, MAs 20/50/200, ATR 14, BBW 20/2σ, percentile window 504D, equal-weight four-horizon momentum composite, and every threshold in §4. **None of these has been validated on this universe.** No "incidental tuning" during implementation — including nudging thresholds to make any particular day's sample output look better. Changing any parameter requires the multi-sample out-of-sample validation process, consistent with the principles recorded on the repo's Robustness page (robust over optimal; external priors first; a single sample validates mechanism, never selects parameters).

## 9. Acceptance checklist

1. Pick three tickers spanning markets — SOXX, EXV1.DE, 159801.SZ — and verify MA200, RSI14, ATR%, 12-1, and vs-52W-high against a manual/TradingView calculation (differences within data-source tolerance, < 0.5%)
2. Ranks total exactly 54 with no duplicates; ΔR shows "—" for the first 20 trading days
3. Name column populated for all 54 (spot-check 3, including one Xetra fund); truncation and tooltip behave as specified
4. RS 1M: SPY's own row shows "—"; verify sign and magnitude for one bond row (e.g., TLT) against a manual calculation
5. Snapshot file appends correctly with no duplicate rows; re-running the same day is idempotent
6. Actions green for 5 consecutive trading days; scanner.html horizontally scrollable on mobile
7. Page footer includes: per-market data dates, "percentile window 504D", and a one-line statement that parameters are unvalidated defaults
