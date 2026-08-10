# WS10 — Holiday-Friday rebalance cadence

**Filed:** Monday 2026-08-10 · **Repo:** `breadth-thrust-etf` · **Status:** CLOSED, ADOPTED
**Verdict:** ADOPT `holiday_aware` retrospectively. Performance evidence NULL; adopted on
governance grounds. Published track record restated **downward**.

---

## 1. Question

The weekly engines built their rebalance grid by intersecting calendar Fridays with actual
trading days. A market-holiday Friday was therefore not a rebalance at all: the whole week's
decision was dropped and the book carried the prior week's signal for a fortnight. Should a
shut Friday instead be decided on the last completed session (the Thursday close)?

Raised from a reader-facing symptom — the data-audit table shows `—` against Fridays such as
2026-07-03 and 2026-06-19 — but the display artefact and the strategy behaviour are two
different things, and only the second matters. The panels already resample `W-FRI .last()`
and carry Thursday correctly (`build_panel_series.py:182`); only `build_data_audit.py` samples
literal Fridays, which is why that one table shows a hole.

## 2. Method

`scripts/run_ws10_holiday_cadence.py` runs each deployed sleeve's headline configuration once
per mode over identical panels, then blends at the deployed 35/35/10/20 weights. The mode is
injected by rebinding `engine_rebalance_dates` inside the engine module that calls it; no
engine source is modified and no deployed artefact is written by the harness.

Three modes:

| Mode | Behaviour on a Friday absent from the price index |
|---|---|
| `scheduled` | Skip the week entirely (the pre-2026-08-10 deployed rule) |
| `last_session` | Always fall back to the prior session |
| `holiday_aware` | Fall back **only if the exchange genuinely did not trade**; otherwise skip and report a data gap |

Calendars are per venue: A/B/C are US-listed (NYSE), D is Europe (XETR). `run_portfolio`
backs both A and D, so the calendar is a parameter rather than a constant.

### Three ways this could have been silently wrong

1. **Look-ahead.** Every engine reads its signal at `get_loc(rd) - 1`. A Thursday rebalance
   therefore reads Wednesday's breadth. No look-ahead is introduced.
2. **A mis-targeted patch reporting a convincing null.** `run_europe_rotation` imports the
   `run_portfolio` *function*, so its calendar call resolves in `run_portfolio`'s namespace.
   The harness aborts if the patch target lacks the attribute, if a sleeve with substituted
   weeks yields identical equity, or if a sleeve without vendor gaps differs between
   `last_session` and `holiday_aware`.
3. **Comparing different windows.** Panels are loaded once and shared across modes; the
   eligible start and last close were asserted unchanged before and after.

## 3. Result

Measured from the engines themselves (before = committed, after = rebuilt; windows unchanged,
last close 2026-08-07 in both):

| Sleeve | Blend wt | Before | After | Δ Sharpe |
|---|---|---|---|---|
| A — US relative breadth | 35% | 0.9709 | 0.9461 | −0.0248 |
| B — asset class | 35% | 0.8088 | 0.8030 | −0.0057 |
| C — thematic | 10% | 0.6982 | 0.6370 | −0.0612 |
| D — Europe | 20% | 0.8804 | 0.8919 | **+0.0115** |
| **Blend 35/35/10/20** | — | **1.1867** | **1.1640** | **−0.0227** |

CAGR 15.83% → 15.52%. Max drawdown unchanged at −24.43%. Gated overlay variant 1.2412;
deployed gated + EM tilt 1.2325.

**The performance case is null, not favourable.** Every delta sits far inside the ±0.4 Sharpe
standard error this book already documents for 7.5 years of weekly data. Sleeve C's −0.0612
is **one week**: Thursday 2022-04-14 (before Good Friday) contributes −10.04pp of its −11.36pp
total, and the remaining twelve substituted weeks are net positive. That is n=1.

## 4. Why `holiday_aware` and not `last_session`

`last_session` cannot distinguish a shut market from a missing bar, and the distinction is not
hypothetical. **Friday 2025-10-24 is absent from all five Europe sector ETFs in yfinance, yet
XETR traded that day** — confirmed against `pandas_market_calendars` and, independently,
against `^GDAXI`, `SAP.DE`, `SIE.DE` and `^STOXX50E`, all of which carry the bar. A fresh
direct fetch also lacks it, so it is upstream and not a cache fault.

Under `last_session` sleeve D would silently rebalance on the Thursday, converting a data
defect into a trade. Under `holiday_aware` the week is skipped exactly as before and the date
is reported by `scheduled_data_gaps` so it can be alarmed. Against this book's history of
stale- and missing-feed incidents, that fail-safety is the reason to prefer it.

It is the **only** such gap on a rebalance Friday across all four sleeves' full history.

## 5. Errors made and corrected during this study

Recorded because both would have silently mis-sized the decision.

1. **Sleeve mislabelling.** The thematic engine was initially measured as "D". Per
   `run_multi_strategy.py:36-37`, **C is thematic and D is Europe**. The real D — 20% of the
   blend, on a different exchange calendar — was not measured at all until corrected.
2. **Wrong signal panel for sleeve A.** The harness passed the raw breadth panel to
   `run_portfolio`, but the deployed engine ranks on the *relative* breadth signal panel
   (`run_topk_robustness._to_signal_panel`). That reproduced A's level to ~0.001 — which is
   why it looked right — while understating A's response to the cadence change by more than
   half (−0.0106 against a true −0.0248). At 35% blend weight it halved the estimated
   restatement: **−0.0123 was presented at sign-off; the true figure is −0.0227.**

The prior project note recording "A +0.026; D +0.043; B/C 0.000" from 2026-07-06 also did not
reproduce and has been superseded.

## 6. Changes shipped

- `rebalance_calendar.py`: `holiday_aware` mode, `scheduled_data_gaps`, and a single
  `DEFAULT_MODE` flip point behind `engine_rebalance_dates`.
- All four engines and the blend routed through that one entry point. The blend previously
  built its own grid and could drift from the books it holds.
- Allocation charts sampled `dayofweek == 4` and would have dropped a Thursday decision; they
  now sample the real rebalance grid.
- `single_name_impl.deployed_sector_layer` derived `rebal_dates` independently of the run that
  produced its weights; the WS6b parity test caught the resulting divergence. Both sites now
  take the grid from the run.
- Dashboard prose figures corrected against source data (see §7).
- 17 calendar tests including holiday-versus-gap discrimination and month/year boundaries;
  1044 tests pass.

## 7. Pre-existing defect found, partially addressed

The methodology and blend-chooser blocks in `template.html` carry **hardcoded** performance
figures rather than templated ones. They had already drifted before this work: they claimed a
+1.15 pre-overlay blend and +1.30/+1.31 deployed against committed data of 1.1867 and 1.2598.
The figures describing the deployed blend were corrected. **A full audit of every hardcoded
number in the 635KB template was NOT done** and remains open.

## 8. Verification

Capture integrity OK (`--strict b,c`); pair integrity 19/19 pass; refresh guard 0 FAIL with
one pre-existing roster-walkback WARN; 1044 tests pass. `check_page.py` 0 fail on both
published pages. Measured on a real emulated viewport at 390 / 844 / 768 / 1280 CSS px:
`clientWidth` confirmed at each width, no horizontal page scroll, 0 uncontained overflow
elements. Smallest rendered font 9px (pre-existing, glyph and table-header chrome).

## 9. Open items

- Full audit of hardcoded figures in `template.html` (§7).
- Friday 2025-10-24 remains missing from the Europe panel; unfixable from the current vendor,
  now handled safely rather than silently.
- `build_data_audit.py` still samples literal Fridays, so its table shows `—` on holiday
  weeks without explaining why — the reader-facing symptom that started this.

---

# Addendum — WS11: survivorship in the constituent price source

**Filed:** Monday 2026-08-10 · **Status:** CLOSED, ADOPTED · **Second restatement same day**

## 1. Question

Raised by ZH after the Data-tab coverage question: CNDX coverage read 81.6% in
2018 and only cleared 99% in 2025. Since Norgate Platinum (subscribed to 2027-01-04)
carries ~21k delisted securities back to 1990, is the missing history recoverable?

## 2. Cause

The roster is point-in-time correct; the PRICE SOURCE is not. yfinance serves history
only under a security's current symbol, so every constituent later acquired, taken
private or renamed has nothing under the ticker it carried while in the index.
compute_breadth drops those from numerator and denominator alike, so early breadth was
computed on survivors.

Three distinct mechanisms, not one: genuine delistings (ATVI, CELG, XLNX, SHPG); ticker
REUSE, where the old symbol now belongs to someone else; and malformed identifiers in
the iShares ticker field (`VSNTV UW`, `OXY WS`, `1812473D`).

## 3. The trap, and why verification was not optional

Resolving by ticker alone attaches whoever holds it now. `FB` today is the ProShares
S&P 500 Dynamic Buffer ETF, first quoted 2025-06-26 — and the committed cache **already
held 281 of its bars** under Facebook's old ticker. `CA` is CA Inc in 2018 and an
Xtrackers muni ETF in 2025. `SPLK` is Spanlink in 1998 and Splunk in 2018.

Resolution is therefore by (ticker, as-of date) against each candidate's quotation
window. For renames, where no dated candidate exists, an explicit map is required — and
proposing the obvious successor ticker produced **fourteen wrong answers**:

| proposed | Norgate says |
|---|---|
| CBS / VIAC → PARA | Banzai International |
| DISCA → WBD | Wimm-Bill-Dann Foods |
| ABC → COR | Crystal Oil |
| CDAY → DAY | Dayton Mining |
| SGH → PENG | Prima Energy |

Every mapping in the final table was checked against Norgate's `security_name`. The
correct symbol is frequently not the plain successor: CBS is `PARAA-202508`, Marsh &
McLennan is `MRSH`, BNY Mellon is `BNY`, Fiserv is `FISV`.

## 4. Result

285 of 327 unpriced US constituents restored. Median 2018 coverage: CNDX 81.6% → 97.1%,
CSP1 82.6% → 97.6%, SOXX 80.0% → 100.0%, IUCM 53.8% → 88.5%, IUSP 65.8% → 91.7%.
Never-resolved 327 → 42, of which 15 are warrants, rights, when-issued lines and
Bloomberg composites that are not ordinary listings and have nothing to attach.

**Survivorship was flattering the record:**

| Series | Survivors only | Corrected | Δ |
|---|---|---|---|
| Strategy A | 0.9501 | 0.9132 | −0.0369 |
| Strategy B | 1.0254 | 1.0254 | 0.0000 |
| Strategy C | 0.6447 | 0.6447 | 0.0000 |
| Strategy D | 0.9440 | 0.9440 | 0.0000 |
| **Blend 35/35/10/20** | **1.1640** | **1.1481** | **−0.0158** |

CAGR 15.52% → 15.20%. Deployed gated+EEM variant 1.2325 → 1.2224. B, C and D at exactly
zero is the control: they rank on ETF-level momentum, so only A reads constituent
breadth, and the blend moves through A's 35% weight.

## 5. Two defects found on the way

1. **compute_breadth wiped the backfill.** It rebuilt each cache purely from the
   yfinance download, so any column yfinance could not serve returned all-NaN and
   overwrote it. The first run filled 25 of CNDX's 27 and the next recompute erased
   every one — caught only because the coverage table reported +0.0pp across every year.
2. **The price basis was backwards, and it was mine.** `auto_adjust=True` means the
   cached closes are total-return; the backfill was asking Norgate for capital-only,
   which would have put a price series beside dividend-reinvested ones in one panel.

## 6. Limits

Corrects **US** constituent breadth only. Norgate here is US Equities, so sleeve D's
Europe panels and the Asia panels (ICHN, IJPN, ITWN, NDIA, IDP6) remain survivor-biased;
the blend figure is partially corrected, not complete. If sleeve D's bias runs the same
direction the true number is lower still. The script refuses those panels outright
rather than half-filling them.

42 names remain unpriced. Beyond the 15 non-listings, the rest are genuine: Chesapeake's
pre-bankruptcy history does not live under Expand Energy, and First Republic's
2010–2023 incarnation is not the 2007 one.

## 7. Verification

Capture integrity OK (`--strict b,c`); pair integrity 19/19; refresh guard 0 FAIL with
the pre-existing roster-walkback WARN; `check_page` 0 fail; 1084 tests pass. Measured at
390 and 1280 CSS px with the resize event dispatched: no horizontal scroll, 0 uncontained
overflow, 49 bound figures all resolving.

The figure-binding guard added earlier the same day passed its first real test: it failed
with all twenty drifted literals named, and exposed nine figures the earlier audit had
corrected but never bound, which had drifted straight back.
