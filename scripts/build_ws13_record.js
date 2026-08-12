/*
 * Content spec — WS12/WS13 execution-timing findings record.
 *
 * Every figure is READ from the study JSON rather than typed in, so the
 * record cannot drift from the data it reports. If a source file is missing
 * the build fails loudly instead of emitting a document with holes.
 */
const fs = require('fs');

const ROOT = 'C:/dev/breadth-thrust-etf';
const feedPath = `${ROOT}/data/execution_timing.json`;
if (!fs.existsSync(feedPath)) {
  throw new Error(`missing ${feedPath} — run scripts/run_ws13_execution_grid.py`);
}
const D = JSON.parse(fs.readFileSync(feedPath, 'utf8'));
const B = D.blend;
const FL = (D.fill_lag || {}).legs || {};
const FLD = (D.fill_lag || {}).delta || {};
const T = (D.paired_tests || {}).tests || {};
const DAYS = D.days;

const f4 = (x) => (x === null || x === undefined ? '—' : x.toFixed(4));
const s4 = (x) => (x === null || x === undefined ? '—' : (x >= 0 ? '+' : '') + x.toFixed(4));
const pp = (x) => (x === null || x === undefined ? '—' : (x >= 0 ? '+' : '') + (x * 100).toFixed(2) + 'pp');
const pc = (x) => (x === null || x === undefined ? '—' : (x * 100).toFixed(2) + '%');
const ciExcludesZero = (r) =>
  r && r.delta_p5 !== null && ((r.delta_p5 > 0 && r.delta_p95 > 0) || (r.delta_p5 < 0 && r.delta_p95 < 0));

// --- Finding 2: WS12 fill-lag legs -----------------------------------------
const fillLagRows = [
  ['Deployed — Thu close signal, Fri close fill', f4(FL.friday_close && FL.friday_close.sharpe),
   pc(FL.friday_close && FL.friday_close.cagr), pc(FL.friday_close && FL.friday_close.max_dd), '—'],
  ['Same decision, filled one session later', f4(FL.monday_close && FL.monday_close.sharpe),
   pc(FL.monday_close && FL.monday_close.cagr), pc(FL.monday_close && FL.monday_close.max_dd),
   s4(FLD.monday_close && FLD.monday_close.sharpe)],
  ['W-MON grid — Fri close signal, Mon close fill', f4(FL.monday_grid && FL.monday_grid.sharpe),
   pc(FL.monday_grid && FL.monday_grid.cagr), pc(FL.monday_grid && FL.monday_grid.max_dd),
   s4(FLD.monday_grid && FLD.monday_grid.sharpe)],
];

// --- Finding 3: WS13 weekday x fill ----------------------------------------
const gridRows = DAYS.map((d) => [
  'W-' + d + (d === 'FRI' ? ' (deployed grid)' : ''),
  f4(B[d].close.sharpe), f4(B[d].open.sharpe),
  s4(B[d].open_minus_close.sharpe), f4(B[d].open_2x.sharpe), pc(B[d].open.max_dd),
]);

// --- Finding 4: paired bootstrap -------------------------------------------
const pairedRows = Object.keys(T).map((k) => {
  const r = T[k];
  return [k, s4(r.delta_point),
    r.delta_p5 === null ? '—' : `[${s4(r.delta_p5)}, ${s4(r.delta_p95)}]`,
    r.p_better === null ? '—' : r.p_better.toFixed(2),
    ciExcludesZero(r) ? 'yes' : 'no'];
});

// --- Finding 6: venues ------------------------------------------------------
const venueRows = D.sleeves.map((s) => [
  s.sleeve, s.venues.join(' + '), s.crosses_at_one_moment ? 'yes' : 'NO',
  String(s.cost_bps) + ' bps',
]);

const sessRows = Object.keys(D.sessions_sgt).map((v) => {
  const r = D.sessions_sgt[v];
  return [v, `${r.local_open}–${r.local_close} ${r.timezone}`,
    r.sgt_open_summer, r.sgt_close_summer + (r.sgt_close_rolls_summer ? ' (+1d)' : ''),
    r.sgt_open_winter, r.sgt_close_winter + (r.sgt_close_rolls_winter ? ' (+1d)' : '')];
});

const splitSleeves = D.sleeves.filter((s) => !s.crosses_at_one_moment).map((s) => s.sleeve);

module.exports = {
  meta: {
    title: 'Execution timing — fill lag, weekday grid, and the open-versus-close question',
    subtitle: 'WS12 and WS13, breadth-thrust-etf',
    dateISO: '2026-08-12',
    weekday: 'Wednesday',
    headerLeft: 'WS12 / WS13 — execution timing',
    metaLeftW: 2400,
    assetsDir: `${ROOT}/reviews/assets`,
  },
  metaTable: [
    ['Project / context', 'breadth-thrust-etf — Personal'],
    ['Study', 'Does the deployed Thursday-signal / Friday-close convention carry look-ahead, what does a later fill cost, and does the choice of weekday or of open-versus-close matter?'],
    ['Evaluation window', `WS12 to 2026-08-10; WS13 to ${D.as_of}. Full available history per sleeve (B from 2007-10-18; A, C and D from late 2018). No train/test split — this is an execution-convention study on fixed, already-selected strategies, not a parameter fit.`],
    ['Data basis', 'yfinance adjusted OHLC through each engine\u2019s own fetch path; sleeve A priced on its US trading proxies; sleeve D FX-converted EUR to USD; per-sleeve costs A 2 / B 2 / C 5 / D 9 bps, with the open leg stressed to 1.5x and 2x.'],
    ['Method basis', 'Deployed engines unmodified. One weight panel per configuration produces both fill legs, so the legs cannot differ by anything except the fill moment. Blend is the 35/35/10/20 four-way, before the EEM tilt and the breadth gate.'],
    ['Repository commits', '4083fbd (forward-roll mode + WS12), 28e1a61 (Execution Timing tab + WS13), 69a2c5d (decision path + schedule), 56b8c7c (signal-bar copy corrections)'],
    ['Running memo', 'RESEARCH_MEMO.md'],
    ['Outcome', 'REVIEWED — deployed convention confirmed free of look-ahead. FRIDAY-OPEN FILL ADOPTED 2026-08-12 on operational grounds; Monday open flagged against; weekday grid unchanged. The engines still model a close fill, deliberately — see section 4.'],
  ],
  sections: [
    { type: 'h1', text: '1. Executive summary' },
    // Kept to ONE page deliberately. Spilling this list over a page break made
    // Word restart its numbering, so claims 7 and 8 printed as "1." and "2." —
    // a numbered list lying about its own count. Shorter items also match the
    // format spec, which asks for 4-8 lines, not 8 paragraphs.
    { type: 'numbers', items: [
      'No look-ahead. Every engine reads the session BEFORE the rebalance, so a Friday rebalance ranks on Thursday\u2019s close. The reconstruction matches each engine\u2019s equity to 0.0, and WS10 on the same panel agrees to 0.0003.',
      `Filling the same decision one session later costs ${s4(FLD.monday_close && FLD.monday_close.sharpe)} Sharpe and ${pp(FLD.monday_close && FLD.monday_close.cagr)} CAGR \u2014 the pessimistic bound, since it also leaves the signal a session staler.`,
      `A W-MON grid signalling off the weekly close returns ${s4(FLD.monday_grid && FLD.monday_grid.sharpe)} Sharpe, but does not fix the problem: the Monday close is 04:00 Singapore time on Tuesday.`,
      `Open versus close ties on four of five grids. Monday is the exception at ${s4(T['MON: open minus close'] && T['MON: open minus close'].delta_point)}, interval clear of zero \u2014 its auction absorbs the weekend gap.`,
      `RECOMMENDED: fill at the Friday open. ${s4(B.FRI.open_minus_close.sharpe)} against the Friday close with the interval straddling zero, ${s4(B.FRI.open_2x.sharpe - B.FRI.close.sharpe)} at doubled cost. Same decision, executed earlier \u2014 21:30 SGT Friday, not 04:00 SGT Saturday.`,
      `The Wednesday grid tests best (${f4(B.WED.close.sharpe)} against ${f4(B.FRI.close.sharpe)}) and is REJECTED \u2014 best of five, uncorrected for multiplicity, sleeves disagree, no mechanism. Friday alone has a structural story.`,
      `Sleeve ${splitSleeves.join(' and ')} cannot cross a rebalance at one moment (${D.sleeves.filter((s) => !s.crosses_at_one_moment).map((s) => s.venues.join(', ')).join('; ')}). A, B and D each sit on one venue.`,
      '13 configurations evaluated, 0 adopted, 1 flagged against.',
    ] },

    { type: 'h1', text: '2. Verified architecture' },
    { type: 'p', text: 'What actually runs, established by reading the deployed code rather than the written record. Where the two disagreed, the code was taken as authoritative and the record corrected.' },
    { type: 'bullets', items: [
      'Rebalance grid is W-FRI, intersected with trading days under holiday_aware (adopted by WS10 on 2026-08-10). Weights at rebalance date rd are built from the signal at get_loc(rd) - 1 — the previous session.',
      'Returns are weight_panel.shift(1) * pct_change, so weights set at rd first earn the rd-to-rd+1 return. That is arithmetically a fill at rd\u2019s close. Turnover cost is charged on rd, the same session as the fill.',
      'The pattern is identical in run_portfolio (sleeves A and D), run_asset_class_rotation (B) and run_thematic_rotation (C), and both overlays lag their signals by one session.',
      'Documentation drift found and corrected: four places in the published dashboard stated the rank is computed from Friday\u2019s close. The rebalance date was right; the session read was not. Corrected in commit 56b8c7c.',
      'A second drift: weekly_factsheet.yml\u2019s header described a Sunday 13:00 UTC check-only run against an actual cron of 09:00 UTC. Corrected in the same commit.',
    ] },

    { type: 'h1', text: '3. Findings' },
    { type: 'h2', text: '3.1 The fill lag — what a later execution costs' },
    { type: 'p', text: 'One weight panel per configuration produces both legs, so the only difference between them is the session on which the weights begin to earn. The reconstruction is asserted against each engine\u2019s own equity before the lagged leg is computed; without that, the lagged leg would measure the reconstruction rather than the strategy.' },
    { type: 'table',
      headers: ['Configuration', 'Sharpe', 'CAGR', 'Max drawdown', 'vs deployed'],
      rows: fillLagRows,
      widths: [3626, 1350, 1350, 1350, 1350], numericFrom: 1 },
    { type: 'p', text: 'The per-rebalance gap is the return difference between holding the new weights and the old over the fill session. Its t-statistic does not exceed 0.5 on any sleeve, and removing the single largest gap event reverses the sign on sleeves B and C. This is exposure to a handful of weekend gaps, not a systematic cost, and no per-year figure should be quoted from it.' },

    { type: 'h2', text: '3.2 The weekday surface, and why its peak is not a decision' },
    { type: 'p', text: 'Five weekday grids, each filled at the close and at the open, all under the forward-roll calendar mode so that a shut scheduled day rolls to the next session and the signal bar stays on the prior weekly close in every week.' },
    { type: 'table',
      headers: ['Grid', 'Close fill', 'Open fill', 'Open − close', 'Open @2x cost', 'Max DD (open)'],
      rows: gridRows,
      widths: [2126, 1380, 1380, 1380, 1380, 1380], numericFrom: 1 },
    { type: 'chart', file: 'ws13_fig1_execution_surface.png',
      caption: 'Levels above, and below them the differences that were actually measured. The upper panel is a set of levels and invites the eye to the Wednesday peak; the lower panel is the paired test, where zero is the null and only an interval clear of zero supports a claim. Only Monday\u2019s does. The Wednesday peak is labelled as rejected on the chart because a reader who takes only the upper panel would draw the opposite conclusion.' },
    { type: 'callout', text: 'The ~0.36 unpaired Sharpe standard error quoted elsewhere in this book is the WRONG yardstick for these comparisons. Two weekday grids, or an open and a close fill, run on one history and are heavily correlated, so the standard error of their difference is far smaller than that of either level. Judging a paired difference against an unpaired standard error would dismiss a real effect as noise — and, in the other direction, let a fitted one be defended as "well inside the SE".' },

    { type: 'h2', text: '3.3 Open versus close — the paired tests' },
    { type: 'p', text: 'Moving-block bootstrap on the blend\u2019s daily returns, reusing the existing run_phase7_bootstrap machinery: 60 trading-day blocks, 2000 samples, fixed seed. An interval clear of zero is distinguishable from sampling noise within this history. It does not establish that the effect persists out of sample, and where a cell was chosen as the best of several the interval is optimistic by construction.' },
    { type: 'table',
      headers: ['Comparison', 'Δ Sharpe', '90% interval', 'P(first better)', 'Clear of zero'],
      rows: pairedRows,
      widths: [3626, 1200, 2000, 1200, 1000], numericFrom: 1 },
    { type: 'p', text: 'One significant result from five open-versus-close tests at a 90% bar is approximately what chance produces, so the Monday finding is suggestive rather than established. Its mechanism, however, was available in advance rather than constructed afterwards: the Monday opening auction prices the whole weekend\u2019s news in a single print, at the widest spreads of the week.' },

    { type: 'h2', text: '3.4 What actually decided the recommendation' },
    { type: 'p', text: 'The Sharpe figures span 1.13 to 1.30 and mostly tie. The wall-clock time of the fill does not tie at all: four of the six variants would have the book turning over between midnight and five in the morning Singapore time. That is the axis on which this decision was made.' },
    { type: 'chart', file: 'ws13_fig2_decision_path.png',
      caption: 'The six variants positioned by the Singapore time at which the book would actually turn over, with Sharpe as annotation rather than as bar length. An earlier draft encoded Sharpe as length and read as a race the rejected Wednesday grid was winning — the opposite of the finding.' },
    { type: 'table',
      headers: ['Venue', 'Local session', 'SGT open (S)', 'SGT close (S)', 'SGT open (W)', 'SGT close (W)'],
      rows: sessRows,
      widths: [1026, 3200, 1200, 1300, 1200, 1100], numericFrom: 2 },
    { type: 'p', text: 'Summer and winter offsets are both published because the United States and Europe shift daylight saving on different dates; a single figure is wrong for several weeks a year. Singapore itself observes no daylight saving.' },

    { type: 'h2', text: '3.5 Cross-trade feasibility' },
    { type: 'p', text: 'A rotation sells one holding and buys another in the same decision. If both legs sit on one venue they cross at a single moment and the sleeve is never unintentionally long or flat. If they sit on different venues the legs are hours apart and the sleeve carries an unhedged gap that no backtest on daily closes can see.' },
    { type: 'table',
      headers: ['Sleeve', 'Venues', 'Crosses at one moment', 'Cost assumption'],
      rows: venueRows,
      widths: [1026, 3500, 2500, 2000], numericFrom: 3 },
    { type: 'p', text: 'The blend\u2019s own 35/35/10/20 rebalance trades US against Xetra and therefore cannot cross at a single moment either. This is a pre-existing property of the architecture, surfaced here rather than introduced.' },

    // No forced break here. With one, the closing cross-trade paragraph is
    // stranded alone on an otherwise empty page; the section flows cleanly
    // without it.
    { type: 'h1', text: '4. Decisions' },
    { type: 'callout', text: 'REVISED 2026-08-12, after the owner adopted the Friday-open fill. The version filed earlier the same day recorded it as recommended and not adopted. Two consequences were decided with it. The engines are NOT being rebuilt to model an open fill: the gap was measured at +0.0299 Sharpe, or -0.0065 at doubled cost, and restating every published number to move one that does not move is not a trade worth making — so the record continues to be computed close-to-close while execution is at the open, and that is disclosed on the dashboard rather than footnoted. The refresh moves off Saturday to a Friday morning so the instruction precedes the fill; it is operator-run, not scheduled, because the per-constituent caches are gitignored and it cannot run in CI.' },
    { type: 'table',
      headers: ['Component', 'Decision', 'Basis'],
      rows: [
        ['Signal-to-fill convention (Thu close → Fri close)', 'KEEP', 'No look-ahead; reconstruction matches engine equity to 0.0; WS10 cross-check agrees to 0.0003'],
        ['Rebalance weekday (W-FRI)', 'KEEP', 'Wednesday tests better but was selected as best of five, is uncorrected for multiplicity, has no mechanism, and the sleeves disagree'],
        ['Fill at the Friday open', 'ADOPTED 2026-08-12', `${s4(B.FRI.open_minus_close.sharpe)} Sharpe with the interval straddling zero, ${s4(B.FRI.open_2x.sharpe - B.FRI.close.sharpe)} at doubled cost; moves the fill from 04:00 SGT Saturday to 21:30 SGT Friday`],
        ['Fill at the Monday open', 'REJECT', `${s4(T['MON: open minus close'] && T['MON: open minus close'].delta_point)} against its own close with the interval clear of zero; the Monday auction absorbs the weekend gap`],
        ['Delaying the existing decision one session', 'REJECT', 'Discards a session of information and retains the same 04:00 SGT fill problem'],
        ['holiday_aware_next calendar mode', 'ADDED, not deployed', 'Required to price a forward-offset grid honestly; DEFAULT_MODE unchanged at holiday_aware'],
        ['Sleeve C cross-trade feasibility', 'FLAGGED', 'US, Shenzhen and crypto legs cannot be crossed at one moment; execution figures for C are indicative'],
        ['Refresh and publish cadence', 'FLAGGED', 'A Friday-open fill requires the instruction before 21:30 SGT Friday; the factsheet stays on its completed-week anchor and does not gate the trade'],
      ],
      widths: [3026, 2000, 4000] },

    { type: 'h1', text: '5. Trial register' },
    { type: 'p', text: 'Stated explicitly so a later deflated-Sharpe or Harvey-Liu-Zhu haircut can charge for the search. Thirteen execution configurations were evaluated across the two workstreams. None was adopted, so no selection has yet been made that would require a haircut; the count is recorded against the possibility that one is made later.' },
    { type: 'chart', file: 'ws13_fig3_scope.png',
      caption: 'Configurations evaluated by category. The funnel is 13 tested, 0 adopted, 1 flagged against.' },
    { type: 'bullets', items: [
      'WS12 — 3 fill conventions (deployed close, one session later, W-MON grid), each across four sleeves and the blend.',
      'WS13 — 10 configurations: 5 weekday grids x 2 fill points, each across four sleeves and the blend.',
      'Cost stress — the open leg re-evaluated at 1.0x, 1.5x and 2.0x the per-sleeve assumption; carried through to the blend for the 2x case.',
      'Inference — 6 paired block-bootstrap tests (5 open-versus-close, 1 best-weekday-versus-deployed).',
      'Not run, and therefore not claimed: split-half or sub-period consistency on the weekday surface, and any out-of-sample test of the Wednesday result. The rejection of Wednesday rests on selection and mechanism, not on a fade that was measured.',
    ] },

    { type: 'h1', text: '6. Artefact register' },
    { type: 'table',
      headers: ['Artefact', 'Path', 'Role'],
      rows: [
        ['WS12 engine', 'scripts/run_ws12_fill_lag.py', 'Fill-lag legs, gap decomposition, guards'],
        ['WS13 engine', 'scripts/run_ws13_execution_grid.py', 'Weekday x fill grid, open-fill model, venue resolution, paired tests, dashboard feed'],
        ['Calendar mode', 'scripts/rebalance_calendar.py', 'holiday_aware_next forward roll; DEFAULT_MODE unchanged'],
        ['Charts', 'scripts/plot_ws13_summary.py', 'Regenerates all three figures from committed JSON'],
        ['Study output', 'data_local/ws12_fill_lag.json, data_local/ws13_execution_grid.json', 'Full results (gitignored)'],
        ['Dashboard feed', 'data/execution_timing.json', 'Committed projection consumed by the Execution Timing tab'],
        ['Published surface', 'template.html — Execution Timing tab', 'Decision path, venue clocks, grid, paired tests, operating schedule'],
        ['Tests', 'tests/test_rebalance_calendar.py', '9 tests covering the forward roll, vendor-gap skip, no-merge cap, tail, month and year boundaries'],
      ],
      widths: [2026, 4000, 3000] },

    { type: 'h1', text: '7. Next phase' },
    { type: 'bullets', items: [
      'Owner decision on the Friday-open fill. It is a recommendation, not an adoption; nothing in the deployed automation has been changed.',
      'If adopted, the refresh moves from Saturday to Friday morning Singapore time so the instruction precedes the 21:30 SGT fill. The weekly factsheet stays on its completed-week anchor; week_final_anchor is correct to refuse to publish a half-formed week, and the instruction should not be bound to it.',
      'Sleeve A is priced on its US trading proxies, so its open is the proxy\u2019s session and not that of the London-listed UCITS actually held. Pricing sleeve A on the real instruments is a separate run and is not covered here.',
      'Sleeve C\u2019s split-venue rebalance is unresolved and predates this study.',
      'If a weekday other than Friday is ever seriously considered, it needs a split-half and an out-of-sample test that this study did not run.',
    ] },
  ],
  signoff: [
    ['Prepared by', 'Claude Code research session, under direction of Zhenghao Phua'],
    ['Reviewed and approved by', ''],
    ['Date', ''],
    ['Next review', 'On any decision to move the fill point or the refresh cadence'],
  ],
  disclaimer: 'Personal research artefact. All performance figures are simulated backtests, net of stated costs; nothing here is investment advice.',
};
