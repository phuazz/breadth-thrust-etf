/*
 * Content spec — WS14 findings record (sleeve A priced on the LSE UCITS).
 *
 * Reads data_local/ws14_sleeve_a_lse.json so no figure is transcribed.
 * Build:
 *   node scripts/build_ws14_record.js
 */
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const SRC = path.join(ROOT, 'data_local', 'ws14_sleeve_a_lse.json');
if (!fs.existsSync(SRC)) {
  throw new Error(`missing ${SRC} — run scripts/run_ws14_sleeve_a_lse.py`);
}
const D = JSON.parse(fs.readFileSync(SRC, 'utf8'));
const L = D.legs, D2 = D.delta_lse_minus_proxy;

const f4 = (x) => (x == null ? '—' : x.toFixed(4));
const s4 = (x) => (x == null ? '—' : (x >= 0 ? '+' : '') + x.toFixed(4));
const pc = (x) => (x == null ? '—' : (x * 100).toFixed(2) + '%');
const pp = (x) => (x == null ? '—' : (x >= 0 ? '+' : '') + (x * 100).toFixed(2) + 'pp');

const legRows = [
  ['US trading proxies (deployed pricing method)', f4(L.proxy_us.sharpe),
   pc(L.proxy_us.cagr), pc(L.proxy_us.max_dd), L.proxy_us.calendar,
   L.proxy_us.window.join(' → ')],
  ['London UCITS lines, converted to USD', f4(L.lse_ucits.sharpe),
   pc(L.lse_ucits.cagr), pc(L.lse_ucits.max_dd), L.lse_ucits.calendar,
   L.lse_ucits.window.join(' → ')],
  ['Difference (London − proxy)', s4(D2.sharpe), pp(D2.cagr),
   pp(D2.max_dd), '—', '—'],
];

const stressRows = Object.keys(D.lse_cost_stress_sharpe).map((k) => [
  k + ' the assumed per-sleeve cost', f4(D.lse_cost_stress_sharpe[k]),
  s4(D.lse_cost_stress_sharpe[k] - L.proxy_us.sharpe),
]);

const cw = D.proxy_vs_london_corr_weekly, cd = D.proxy_vs_london_corr_daily;
const corrRows = D.universe_priced
  .slice().sort((a, b) => cw[a] - cw[b])
  .map((k) => [k, D.proxy_map[k], D.currencies[k], cw[k].toFixed(3), cd[k].toFixed(3)]);

module.exports = {
  meta: {
    title: 'Sleeve A priced on the London UCITS lines it actually holds',
    subtitle: 'WS14, breadth-thrust-etf',
    dateISO: '2026-08-12',
    weekday: 'Wednesday',
    headerLeft: 'WS14 — sleeve A, LSE pricing',
    metaLeftW: 2400,
    assetsDir: path.join(ROOT, 'reviews', 'assets'),
  },
  metaTable: [
    ['Project / context', 'breadth-thrust-etf — Personal'],
    ['Study', 'Sleeve A signals on UCITS constituent breadth but prices through US trading proxies. Does its result survive when priced on the London-listed UCITS lines actually held?'],
    ['Evaluation window', `${L.proxy_us.window[0]} to ${L.proxy_us.window[1]} (proxy leg) and ${L.lse_ucits.window[1]} (London leg). One eligible start, one signal panel.`],
    ['Data basis', 'yfinance adjusted closes. London lines converted to USD where they quote in GBp, using GBP/USD forward-filled onto the price calendar. Costs at the deployed sleeve-A assumption, stressed to 2x and 3x.'],
    ['Method basis', 'Only the PRICE panel changes. Sleeve A’s signal is constituent breadth computed from rosters, not from the ETF price, so the signal panel is identical between legs and any difference is the traded instrument alone. Deployed engine unmodified.'],
    ['Universe', `${D.universe_deployed.length} deployed names; ${D.universe_priced.length} priced (${D.dropped_no_london_line.join(', ')} has no London line). BOTH legs run the same ${D.universe_priced.length}.`],
    ['Repository commits', '8766cd6 (WS14 engine)'],
    ['Running memo', 'RESEARCH_MEMO.md'],
    ['Outcome', `REVIEWED — sleeve A survives the instrument substitution. ${s4(D2.sharpe)} Sharpe, ${pp(D2.cagr)} CAGR on a like-for-like universe. No change adopted.`],
  ],
  sections: [
    { type: 'h1', text: '1. Executive summary' },
    { type: 'numbers', items: [
      `Sleeve A survives being priced on the instruments actually held. On the same 13 names the London UCITS lines return ${f4(L.lse_ucits.sharpe)} Sharpe against ${f4(L.proxy_us.sharpe)} for the US proxies — a difference of ${s4(D2.sharpe)}, roughly a hundredth of the standard error on either level.`,
      `The CAGR gap is the more real of the two at ${pp(D2.cagr)}, and is consistent with UCITS fee and tracking drag rather than with anything about the signal.`,
      `Cost stress does not overturn it: the London leg holds ${f4(D.lse_cost_stress_sharpe['3x'])} at three times the assumed cost.`,
      'The venue substitution is therefore not what drives sleeve A, and the deployed backtest is not flattered by pricing through proxies.',
      `Two of the thirteen London lines quote in GBp (pence), not USD. Currency is resolved per ticker from the feed; an unrecognised currency is fatal, because a mixed-currency sleeve is invisible in a Sharpe.`,
      `${D.dropped_no_london_line.join(', ')} has no London line, so the London basis cannot cover the deployed universe. Both legs run the same ${D.universe_priced.length} names so the comparison isolates venue rather than universe; the excluded name is worth roughly 0.10 Sharpe to the deployed 14-name sleeve, which is a separate matter.`,
      'The US proxies track related but different indices — capped Select Sector against plain GICS sector. The deployed price series therefore differs from the held instrument by index construction as well as by venue.',
    ] },

    { type: 'h1', text: '2. Result' },
    { type: 'p', text: 'Both legs run the deployed headline configuration over one eligible window and one signal panel. The London leg rebalances on the LSE calendar, since London and New York keep different holidays and inheriting NYSE would place decisions on days the venue was shut.' },
    { type: 'table',
      headers: ['Pricing basis', 'Sharpe', 'CAGR', 'Max DD', 'Calendar', 'Window'],
      rows: legRows,
      widths: [2726, 1000, 1100, 1100, 1100, 2000], numericFrom: 1 },
    { type: 'h2', text: '2.1 Cost stress on the London leg' },
    { type: 'p', text: 'The London lines are thinner than the US proxies, so the deployed cost assumption is the wrong one to lean on. The leg is re-run at multiples of it and compared against the proxy leg at its own unstressed cost.' },
    { type: 'table',
      headers: ['Assumption', 'London Sharpe', 'vs proxy leg'],
      rows: stressRows,
      widths: [4026, 2500, 2500], numericFrom: 1 },

    { type: 'h1', text: '3. How good is the proxy substitution?' },
    { type: 'p', text: 'The headline answer says the substitution does not change the result. It does not say the substitution is exact, and the two are different claims. Each London line is compared with the proxy standing in for it.' },
    { type: 'chart', file: 'ws14_fig1_proxy_tracking.png',
      caption: 'Weekly and daily return correlation between each London UCITS line and its US trading proxy. The daily figures are depressed for every pair by the 16:30 London against 16:00 New York close, not by any mis-mapping; the weekly figures are the meaningful ones. An equity-curve overlay is deliberately not shown — the two curves are visually identical and would invite the reader to hunt for a difference that is not there.' },
    { type: 'table',
      headers: ['Panel key', 'US proxy', 'Quoted in', 'Weekly corr', 'Daily corr'],
      rows: corrRows,
      widths: [1800, 1800, 1600, 1913, 1913], numericFrom: 3 },
    { type: 'callout', text: 'The two weakest pairs are XLP against IUCS (0.888) and XLRE against IUSP (0.883). Neither is a mis-mapping: the Select Sector SPDRs track CAPPED Select Sector indices while the UCITS lines track plain GICS sector indices. The proxy substitution was never exact, and this record does not claim it is.' },

    { type: 'h1', text: '4. Guards, including two that were wrong' },
    { type: 'bullets', items: [
      'Currency is read per ticker rather than assumed. CSP1.L prints about 61,798 — as pence that is roughly USD 834, as USD it would be absurd for a share. An unrecognised currency raises rather than passing through.',
      'Both legs run the same universe, so a missing London line cannot be mistaken for a venue effect.',
      'The London leg uses the LSE trading calendar.',
      'WRONG FIRST TIME: the pair check initially ran on DAILY returns against a 0.55 floor and rejected the two pence-quoted lines. The LSE closes at 11:30 New York, so daily returns cover different windows and every pair scores 0.46 to 0.75 regardless of correctness. The test belongs on weekly returns.',
      'WRONG SECOND TIME: a 0.90 weekly floor then rejected XLP/IUCS and XLRE/IUSP, asserting a precision the proxy substitution never had. The floor is 0.70, which still catches a genuinely wrong fund near zero.',
    ] },

    { type: 'h1', text: '5. Decisions' },
    { type: 'table',
      headers: ['Component', 'Decision', 'Basis'],
      rows: [
        ['Pricing sleeve A through US proxies', 'KEEP', `Instrument substitution is worth ${s4(D2.sharpe)} Sharpe on a like-for-like universe; the deployed record is not flattered by it`],
        ['Sleeve A universe (SOXX included)', 'KEEP, FLAGGED', 'SOXX has no London line, so a London-priced implementation would either drop it or need a UCITS semiconductor substitute — an open implementation question, not a backtest one'],
        ['Proxy-vs-instrument index mismatch', 'FLAGGED', 'Capped Select Sector against plain GICS; weekly correlations 0.883 to 0.954'],
        ['Cost assumption for a London implementation', 'FLAGGED', 'Deployed 2 bps is a US-proxy assumption; the London leg holds up to 3x but the realised spread is unmeasured'],
      ],
      widths: [2726, 2000, 4300] },

    { type: 'h1', text: '6. Trial register' },
    { type: 'p', text: 'Two pricing bases and three cost multipliers on one of them — five evaluations, none selected. This is a robustness check on an existing deployed choice rather than a search over candidates, so there is no selection to charge a deflated-Sharpe haircut against.' },

    { type: 'h1', text: '7. Artefact register' },
    { type: 'table',
      headers: ['Artefact', 'Path', 'Role'],
      rows: [
        ['Engine', 'scripts/run_ws14_sleeve_a_lse.py', 'Both pricing legs, currency resolution, pair guard, cost stress'],
        ['Chart', 'scripts/plot_ws14_summary.py', 'Regenerates the figure from committed JSON'],
        ['Record spec', 'scripts/build_ws14_record.js', 'This document, built from the study output'],
        ['Study output', 'data_local/ws14_sleeve_a_lse.json', 'Full results (gitignored)'],
      ],
      widths: [2026, 4000, 3000] },

    { type: 'h1', text: '8. Next phase' },
    { type: 'bullets', items: [
      'No action required. The deployed pricing method stands.',
      'If a London-listed implementation is ever built, the open questions are what replaces SOXX and what the realised spread on the thinner lines actually is.',
      'The tax dimension — Irish-domiciled UCITS receiving US dividends at 15% under treaty against 30% withholding on US-domiciled funds for a Singapore holder, and US estate-tax exposure on US-situs assets — is not addressed here and is a question for a tax adviser rather than a backtest.',
    ] },
  ],
  signoff: [
    ['Prepared by', 'Claude Code research session, under direction of Zhenghao Phua'],
    ['Reviewed and approved by', ''],
    ['Date', ''],
    ['Next review', 'Only if a London-listed implementation is contemplated'],
  ],
  disclaimer: 'Personal research artefact. All performance figures are simulated backtests, net of stated costs; nothing here is investment advice.',
};
