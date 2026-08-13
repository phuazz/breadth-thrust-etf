// WS15 technical findings record — content spec + build.
// Run: node scripts/build_ws15_record.js
// Every number traces to reviews/ws15/*.json and RESEARCH_MEMO.md (WS15
// section); no figure is invented here.
const { buildReport } = require('C:/Users/phuaz/.claude/skills/research-review/assets/report_builder.js');

const spec = {
  meta: {
    title: 'WS15 — Survivorship on the published CNDX record',
    subtitle: 'What the survivor-price defect was worth to the published cross-ETF OOS backtest, the residual WS11 missed, and the repair of a latent Monte Carlo defect',
    dateISO: '2026-08-13',
    weekday: 'Thursday',
    headerLeft: 'breadth-thrust-etf — WS15',
    headerRight: 'Personal / internal',
    assetsDir: 'C:/dev/breadth-thrust-etf/reviews/charts',
  },
  metaTable: [
    ['Project / context', 'breadth-thrust-etf (Personal). Follow-on to WS10 (cadence restatement), WS11 (constituent-price survivorship, blend restated 1.1640 to 1.1481) and WS12/WS13 (execution timing).'],
    ['Review scope', 'The US CNDX constituent panel only. Published surfaces: data/backtest_cndx_oos.json (2026-05-17 vintage, inlined nowhere on the live dashboard but committed and public) and the README cross-ETF OOS table. Other panels inventoried, not re-measured.'],
    ['Evaluation window', 'Breadth 2018-01-05 to 2026-08-07 (2,158 NYSE sessions); the published leg ends 2026-05-15. OOS trading eligibility from session 253 onward, as deployed.'],
    ['Data basis', 'yfinance auto_adjust=True cache (total-return) + Norgate Platinum TOTALRETURN fills; NDU updated 2026-08-13, delisted archive 21,099 symbols back to 1990. Coverage gate PASS before any pull.'],
    ['Method basis', 'Five legs, one change at a time, all on one freshly-pulled QQQ/SPY basis. Reproduce-before-diff guards: the breadth driver reproduced the committed corrected series exactly; the T1 leg reproduced the published OOS file (trades exact, stats to ~3e-4 relative, seeded MC within ±2pp).'],
    ['Repository commits', '9bf63e7 (published OOS + its breadth basis, 2026-05-17); 1ada87b (last survivor panel, 2026-08-08); b841f77 (WS11 restatement, 2026-08-10); working tree at 6abe564.'],
    ['Running memo', 'RESEARCH_MEMO.md — "WS15" section (2026-08-13).'],
    ['Outcome', 'Published CNDX OOS row +44.5% / Sharpe 0.51 / MC 39.6 restates to +20.0% / 0.27 / MC 18.3; three-quarters of the fall is data-vintage artefact, not survivorship. RESTATEMENT PACKAGE PENDING SIGN-OFF; nothing published was touched.'],
  ],
  sections: [
    { type: 'h1', text: '1. Executive summary' },
    { type: 'numbers', items: [
      [{ text: 'The published CNDX OOS backtest is stale on every axis, not merely survivor-biased. ', bold: true }, { text: 'Its 87 signal-fire days were computed on the May-2026 roster file and price vintage; only 35 of them survive the August roster rebuild at all. Re-priced on today\u2019s data the headline variant falls from +44.5% total return / 0.51 Sharpe / MC percentile 39.6 to +20.0% / 0.27 / 18.3.' }],
      [{ text: 'Survivorship itself pushed the OTHER way on this surface. ', bold: true }, { text: 'On the clean pair (identical window, roster, config and code; only the Norgate fill differs) correction ADDS +4.5pp total return and +0.04 Sharpe: the survivor panel had suppressed 8 genuine fire days and fabricated 1. The direction is opposite to the sleeve-A blend effect WS11 measured, which is why direction is measured, never assumed.' }],
      [{ text: 'WS11\u2019s correction was itself incomplete. ', bold: true }, { text: 'Its backfill only treated all-NaN columns, so reuse-masked names were skipped: Facebook missing 1,115 roster-days (2018 to mid-2022), 21st Century Fox 295/296, Priceline 38, plus EA\u2019s final 11 sessions and MNST\u2019s 14-session split hole. The WS15 fill takes 2018 median coverage from 97.1% to 100.0% and residual gaps from 1,805 roster-days to 35, all unfillable by design.' }],
      [{ text: 'Every variant of the published table sits below its random-entry null median after correction ', bold: true }, { text: '(MC percentiles 1.9 to 18.3). The CNDX thrust edge, as published, does not survive its own data corrections.' }],
      [{ text: 'A latent Monte Carlo defect in backtest.py was found and fixed: ', bold: true }, { text: 'the Phase 10.2 non-overlap sampler returned zero valid null paths on this input shape (all 1,000 discarded, every MC field None). No committed artefact carries the damage; the fix is pinned by a regression suite (35 backtest tests pass).' }],
      [{ text: 'The class is far larger outside CNDX: ', bold: true }, { text: '36,862 residual roster-days across 199 names in the other 13 US panels, including the 2023 bank failures (SIVB, FRC, SBNY) absent from financials breadth during the crisis they defined, and Brown-Forman (BFB) unpriced for the entire history via a ticker-normalisation gap. Inventoried only; queued as a follow-on decision.' }],
    ]},

    { type: 'h1', text: '2. The five legs', pageBreakBefore: true },
    { type: 'p', text: 'Each leg changes exactly one thing. T1 must reproduce the published artefact before any comparison is drawn \u2014 it does: trade counts, win rates and holding periods are exact; returns and Sharpe agree to ~3e-4 relative (residual = vendor bar revisions since May); the seeded Monte Carlo reproduces within \u00b12 percentile points. All legs trade one freshly-pulled QQQ/SPY basis, so a dividend that went ex after a window rescales that whole window by one constant and cancels in returns, stops and Sharpe.' },
    { type: 'chart', file: 'ws15_decomposition.png', caption: 'The published result, re-priced one change at a time (headline variant). Bars are total return over the eligible window with the signal count inside each bar; diamonds are annualised Sharpe. The fall from the second to the third bar is the August data refresh (roster rebuild + vendor re-basing + three more months), not survivorship; the survivorship step (third to fourth) is positive.' },
    { type: 'table',
      headers: ['Leg', 'Signals', 'n', 'Win %', 'Total ret %', 'Sharpe', 'MC pctile'],
      widths: [3200, 1000, 700, 1000, 1200, 1000, 926],
      numericFrom: 1,
      rows: [
        ['T1 published repro (May code, May panel)', '87', '13', '61.5', '+44.5', '+0.51', '39.6'],
        ['T2 today\u2019s code, May panel', '87', '13', '61.5', '+44.5', '+0.51', '37.5'],
        ['T3 Aug panel, survivor prices', '36', '13', '53.8', '+18.3', '+0.25', '14.4'],
        ['T4 Aug panel, WS11 corrected', '43', '15', '53.3', '+22.8', '+0.29', '19.5'],
        ['T5 Aug panel, WS15 residual-fixed', '42', '15', '46.7', '+20.0', '+0.27', '18.3'],
      ]},
    { type: 'p', text: 'Variant shown: regime_time_only_delay5_trend, the README table\u2019s bolded column. Attribution of the published-to-truth fall of 24.5pp total return: code evolution \u22480; data refresh \u221226.2pp (Sharpe \u22120.26); survivorship +4.5pp (+0.04); WS15 residual \u22122.8pp (\u22120.02). MC percentiles are comparable within T2\u2013T5 (one null definition, one seed); T1\u2019s MC is the published pre-non-overlap definition.' },
    { type: 'h2', text: 'All three variants, published versus current truth' },
    { type: 'table',
      headers: ['Variant', 'Published (T1)', 'Corrected (T4)', 'Residual-fixed (T5)'],
      widths: [3326, 1900, 1900, 1900],
      numericFrom: 1,
      rows: [
        ['baseline_2xATR', '+10.3% / +0.19 / 22.6', '\u221211.0% / \u22120.16 / 6.0', '\u221218.6% / \u22120.32 / 1.9'],
        ['regime_time_only', '+23.4% / +0.29 / 18.9', '+14.2% / +0.20 / 9.0', '+4.4% / +0.10 / 5.7'],
        ['regime_time_only_delay5_trend', '+44.5% / +0.51 / 39.6', '+22.8% / +0.29 / 19.5', '+20.0% / +0.27 / 18.3'],
      ]},
    { type: 'p', text: 'Cells are total return / Sharpe / MC percentile. The README cross-ETF OOS table\u2019s CNDX row (0.19/22 \u00b7 0.29/19 \u00b7 0.51/39) is the T1 column to the digit \u2014 the published basis is pinned, and every variant now sits well below its null median.' },

    { type: 'h1', text: '3. The residual WS11 missed', pageBreakBefore: true },
    { type: 'p', text: 'backfill_delisted_prices treats a column as unpriced only when it is absent or all-NaN. A column holding unrelated ticker-reuse bars therefore counted as priced and was skipped, even though it was empty across the roster\u2019s held window. The sweep below is held-window-aware: it counts roster-days on which a held name has no bar.' },
    { type: 'table',
      headers: ['Name', 'Roster-days missing', 'Span', 'Source of fill', 'Mode'],
      widths: [1200, 1700, 2400, 2100, 1626],
      numericFrom: 1,
      rows: [
        ['FB', '1,115', '2018-01-05 to 2022-06-09', 'META (Meta Platforms)', 'raw, era barrier'],
        ['FOX', '296', '2018-01-05 to 2019-03-12', 'TFCF-201903 (21CF B)', 'raw, era barrier'],
        ['FOXA', '295', '2018-01-05 to 2019-03-11', 'TFCFA-201903 (21CF A)', 'raw, era barrier'],
        ['PCLN', '38', '2018-01-05 to 2018-03-01', 'BKNG (Booking Holdings)', 'raw, era barrier'],
        ['EA', '11', '2026-07-20 to 2026-08-03', 'EA-202608', 'rescaled \u00d71.000000'],
        ['MNST', '15', '2026-07-20 to 2026-08-07', 'MNST (live)', 'rescaled \u00d72.000000'],
      ]},
    { type: 'p', text: 'Every mapping was verified against Norgate security_name before use (the WS11 lesson: fourteen \u201cobvious successor\u201d guesses were wrong). Same-security splices are rescaled onto the column\u2019s own basis via the median overlap ratio with a stability assertion \u2014 MNST\u2019s ratio of exactly 2.000000 is the 2-for-1 split factor, and gluing without it would have fabricated a \u221250% day. Cross-security columns get an era barrier: no indicator window ever spans two securities, which exactly preserves the committed panel\u2019s fresh-listing warmup treatment of early Fox Corporation. The 24 WS11 fill columns were also extended back through the 2017-07 warmup they lacked (3,000 bars), so early-2018 moving averages exist from the first breadth date. After the fill, 35 roster-day gaps remain \u2014 all stale-roster tails (the security was already dead while listed) or by-design exclusions (TMUSR rights line, VSNTV UW composite).' },
    { type: 'chart', file: 'ws15_coverage.png', caption: 'Median share of the CNDX roster carrying a usable price, by year. The survivor panel (red) climbs toward the survivors; WS11 (navy) recovered most of it; WS15 (teal) closes the reuse-masked residual to 100.0% in every year.' },
    { type: 'p', text: 'Signal-set effect of the residual fix, corrected versus WS15: 40 of 43 fire days common; lost 2019-03-15, 2019-11-08 and 2026-05-06; gained 2023-12-01 and 2025-06-30. A current-year published signal (2026-05-06) moves under a data fix whose newest bar is 2022 \u2014 expanding thresholds propagate history changes forward, which is the mechanism by which early-year coverage defects contaminate recent signals.' },

    { type: 'h1', text: '4. Classifications, from evidence', pageBreakBefore: true },
    { type: 'table',
      headers: ['Name', 'Queue-entry claim', 'Evidence', 'Classification'],
      widths: [1000, 2100, 3600, 2326],
      rows: [
        ['MNST', '\u2014', 'Norgate LIVE, quoting through 2026-08-12; yfinance serves pre-split bars unhalved beside post-split bars and drops Monday 2026-08-10', 'Live; yfinance MIS-ADJUSTED around the 2026-08-11 2-for-1 split. URGENT ingestion hazard'],
        ['EA', '\u2014', 'EA-202608, last quoted 2026-08-04 (Electronic Arts); yfinance stopped serving three weeks earlier', 'Genuine delisting; final 11 sessions recovered from Norgate'],
        ['SPCX', '\u2014', 'Space Exploration Technologies Class A, live, young listing', 'No defect: inside 50-session warmup'],
        ['HONA', '\u2014', 'Honeywell Aerospace Inc, live, young listing', 'No defect: inside 50-session warmup'],
        ['HOLX', '\u201clive listing, empty \u2014 fetch defect\u201d', 'HOLX-202604, last quoted 2026-04-06 (Hologic)', 'Genuine delisting, correctly filled by WS11; claim stale when written'],
        ['WBA', 'same', 'WBA-202508, last quoted 2025-08-27 (Walgreens Boots Alliance)', 'Genuine delisting, correctly filled by WS11'],
        ['ANSS', 'same', 'ANSS-202507, last quoted 2025-07-16 (ANSYS)', 'Genuine delisting, correctly filled by WS11'],
      ]},
    { type: 'callout', text: 'MNST is the urgent item: the next refresh would replace the column wholesale with a mixed-basis series carrying a spurious \u221250% step on 2026-08-11, poisoning ~50 sessions of MA breadth and ~200 sessions of sleeve A\u2019s 200-day panel for that name. No existing guard checks per-name basis continuity \u2014 the coverage floors count names, not levels. Options before Friday 2026-08-15: exclude/refetch MNST once the vendor repairs the factor, source the name from Norgate, or adopt a basis-continuity guard in download_prices (patch to be presented, not applied).' },

    { type: 'h1', text: '5. The Monte Carlo defect and its repair' },
    { type: 'p', text: 'The Phase 10.2 sampler (2026-05-25) placed each random entry uniformly over all remaining feasible positions while reserving room for later trades at only the minimum holding (one session here). Early entries scattered deep into the window and stranded the rest: on this input (13 trades, 596 holding sessions, ~1,750-session window) every one of 1,000 restart-bounded paths came back partial, monte_carlo_null discarded them all, and every MC field was None. Nothing was regenerated between that commit and today, so no committed artefact carries the damage \u2014 but any regeneration would have shipped an empty null. The replacement bootstraps the holding lengths, then places all entries at once through a gap transform (sorted iid draws on the exact feasible box, shifted by occupied space): a feasible configuration is never dead-ended, infeasibility returns an empty path only after holding redraws, and a gapped eligibility window is refused loudly. Pinned by tests/test_mc_nonoverlap_sampler.py: the exact failing shape must fit in 200 of 200 paths, tight-window and infeasible cases behave, and an end-to-end null on synthetic data yields \u2265199/200 valid paths. The full backtest suite (35 tests) passes.' },

    { type: 'h1', text: '6. The class outside CNDX (inventory only)' },
    { type: 'table',
      headers: ['Panel', 'Largest gaps', 'Residual roster-days', 'Names'],
      widths: [1200, 4926, 1900, 1000],
      numericFrom: 2,
      rows: [
        ['CSP1', 'SIVB 1,269 \u00b7 FB 1,115 \u00b7 FRC 1,091 \u00b7 INFO 1,047 \u00b7 COG 946 \u00b7 LB 902', '9,789', '76'],
        ['IUSP', 'NSA 2,146 \u00b7 MPW 2,032 \u00b7 KW 1,944 \u2014 structural, needs own diagnosis', '14,387', '41'],
        ['IUFS', 'SIVB 1,269 \u00b7 FRC 1,091 \u00b7 STI 488 \u00b7 SBNY 322 \u2014 the 2023 bank failures', '3,185', '8'],
        ['IUCS', 'BFB 2,158 (entire history; iShares \u201cBFB\u201d vs yfinance \u201cBF-B\u201d) \u00b7 MNST 15', '2,175', '4'],
        ['IUES', 'COG 941 \u00b7 APC 400 \u00b7 NFX 279', '1,691', '10'],
        ['Other 8 panels', 'IUCD LB 902 \u00b7 IUCM FB 936 \u00b7 SOXX WOLF 741 \u00b7 IUIS INFO 1,047/ARNC 562 \u00b7 IUMS DWDP 356 \u00b7 IUIT FB 179', '5,635', '60'],
        ['Total (13 panels)', 'CNDX after WS15: 35 roster-days, the unfillable floor', '36,862', '199'],
      ]},
    { type: 'p', text: 'Direction unknown per panel and never assumed \u2014 on this study\u2019s own evidence the same defect flattered the blend (WS11) and penalised the CNDX OOS surface (finding 2). Financials breadth excluding SIVB/FRC/SBNY during March 2023 is the highest-risk item qualitatively: excluding crashing names flatters breadth exactly when the sleeve is deciding. Any fix moves sleeve A\u2019s restated 0.9132 again and stacks with the queued sleeve-D (Europe) decision.' },

    { type: 'h1', text: '7. Decisions', pageBreakBefore: true },
    { type: 'table',
      headers: ['Component', 'Decision', 'Basis'],
      widths: [2826, 1600, 4600],
      rows: [
        ['data/backtest_cndx_oos.json', 'RESTATE (pending sign-off)', 'Finding 2: stale on code, roster, prices and window; regenerate on T4 or T5 panel with repaired MC. Choice of panel follows the residual-fill adoption decision.'],
        ['README cross-ETF OOS table (CNDX row)', 'RESTATE (pending sign-off)', 'Carries T1 to the digit; correct row + dated note; CSP1/IUES/IUFS rows carry the same defect classes, unmeasured here.'],
        ['README early-phase per-ETF table', 'ANNOTATE (pending sign-off)', 'Same vintage class, different engine; re-running that phase is a separate decision.'],
        ['WS15 residual fill into the live cache', 'OWNER CALL', 'Closes 1,770 fillable roster-days; nudges sleeve A a third time; requires held-window-aware backfill patch.'],
        ['MNST ingestion', 'URGENT before 2026-08-15 refresh', 'Finding 4 callout: mixed-basis vendor series; no guard would catch it.'],
        ['scripts/backtest.py MC sampler', 'FIXED + TESTED (committed with study)', 'Finding 5: latent, blocking any honest regeneration; no published figure depended on it.'],
        ['Cross-panel residual class', 'QUEUED', 'Finding 6 inventory; measurement not commissioned under this scope.'],
        ['Deployed blend / factsheet / docs', 'UNTOUCHED', 'Restatement gate: nothing published changes before sign-off.'],
      ]},

    { type: 'h1', text: '8. Trial register' },
    { type: 'p', text: 'No parameters were tuned and nothing was selected: five legs \u00d7 three pre-existing fixed variants re-priced; zero new configurations searched. No multiple-testing haircut is owed by this study; it consumes the haircuts owed by the original SOXX-tuned configurations, which are unchanged.' },

    { type: 'h1', text: '9. Artefact register' },
    { type: 'bullets', items: [
      'Gate: scripts/run_ws15_gate.py \u2192 reviews/ws15_gate.json (PASS, 2026-08-13; NDU same-day, 21,099 delisted symbols to 1990, 27/27 symbols verified)',
      'Residual fill: scripts/run_ws15_residual_fill.py (working copy only; live cache untouched) \u2192 reviews/ws15/ws15_fill_report.json',
      'Breadth driver: scripts/run_ws15_breadth_legs.py (exact reproduction of the committed corrected series: 2,158 days, 43 signals, every 6dp value identical)',
      'OOS legs: scripts/run_ws15_oos_legs.py \u2192 reviews/ws15/ws15_oos_legs.json (T1 reproduction check embedded)',
      'Breadth comparison: scripts/build_ws15_breadth_compare.py \u2192 reviews/ws15/ws15_breadth_compare.json',
      'Cross-panel inventory: reviews/ws15/ws15_cross_panel_gaps.json',
      'Charts: scripts/plot_ws15_summary.py \u2192 reviews/charts/ws15_decomposition.png, ws15_coverage.png',
      'MC repair: scripts/backtest.py (_sample_non_overlapping_random_trades) + tests/test_mc_nonoverlap_sampler.py (35 backtest tests pass)',
      'Running memo: RESEARCH_MEMO.md \u00a7 WS15; ledger: C:/dev/STUDIES_LEDGER.md row 2026-08-13',
    ]},

    { type: 'h1', text: '10. Next phase' },
    { type: 'bullets', items: [
      'Owner sign-off on the restatement package (section 7, rows 1\u20134); only then do data/, README or docs change.',
      'MNST decision before the Friday 2026-08-15 refresh (urgent-operational).',
      'Cross-panel residual follow-on: scope CSP1 + IUFS (the 2023 banks) and diagnose IUSP; BFB parser mapping is a one-line ticker_overrides fix wherever adopted.',
      'Held-window-aware _unpriced in backfill_delisted_prices \u2014 patch to be presented with the adoption decision, not applied unilaterally.',
    ]},
  ],
  signoff: [
    ['Prepared by', 'Claude (Fable 5), operator session 2026-08-13'],
    ['Reviewed and approved by', 'PENDING \u2014 Zhenghao (restatement package requires explicit sign-off)'],
    ['Date', '2026-08-13'],
    ['Next review', 'On sign-off decision; cross-panel follow-on when commissioned'],
  ],
  disclaimer: 'Internal research record for a personal project. Not investment advice, not a client communication, and not for distribution. All figures trace to the committed evidence JSONs named in the artefact register; the published surfaces named in section 7 remain unrestated until sign-off.',
};

buildReport(spec, 'C:/dev/breadth-thrust-etf/reviews/2026-08-13_ws15_cndx-survivorship-restatement.docx')
  .then(r => console.log('wrote', r.outPath, r.bytes, 'bytes'))
  .catch(e => { console.error(e.message); process.exit(1); });
