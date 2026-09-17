/*
 * WS20 technical findings record — content spec for report_builder.js.
 *
 *   node scripts/build_ws20_record.js
 *
 * Every figure below was read from data_local/ws20/results.json, the output of
 * the single registered pass (reproduced byte-identically on a second run).
 * Charts come from scripts/plot_ws20_summary.py, which reads the same file, so
 * the record, the charts and the run cannot drift apart.
 */
const path = require("path");
const { buildReport } = require("C:/Users/phuaz/.claude/skills/research-review/assets/report_builder.js");

const spec = {
  meta: {
    title: "WS20 — within-panel trend dispersion",
    subtitle: "Does the spread of constituent trend carry information the breadth level does not?",
    dateISO: "2026-09-18",
    weekday: "Friday",
    headerLeft: "WS20 — trend dispersion (Layer A)",
    headerRight: "NO-INFORMATION",
    metaLeftW: 2400,
    assetsDir: "C:/dev/breadth-thrust-etf/reviews/charts",
  },
  metaTable: [
    ["Project / context", "breadth-thrust-etf — Personal"],
    ["Study", "Layer A: does the cross-sectional dispersion of per-name trend within a sleeve A panel carry information about forward returns beyond the breadth level?"],
    ["Registration", "KICKOFF_ws20-trend-dispersion.md, frozen at vault-docs 1cd0626 on 2026-09-18 before the module existed; ten drafter decisions confirmed at sign-off"],
    ["Evaluation window", "375 weekly decision dates, 2019-03-29 to 2026-05-29; forward windows required to complete by 2026-06-30, so the holdout after that date was never touched"],
    ["Data basis", "14 sleeve A constituent panels, Norgate per-column (WS19b superset rule), BTE_PRICE_SOURCE=norgate enforced by the runner"],
    ["Method basis", "Weekly cross-sectional regression of the forward 4-week return on demeaned breadth AND dispersion; predictors read at t-1 exactly as the deployed engine reads them"],
    ["Repository commits", "412e3af (engine, runner, 24 selftests, before any figure)"],
    ["Running memo", "RESEARCH_MEMO.md, section WS20"],
    ["Outcome", "NO-INFORMATION — all three gates fail. Layer B never opened. Zero deployed changes."],
  ],
  sections: [
    { type: "h1", text: "1. Executive summary" },
    { type: "numbers", items: [
      "VERDICT NO-INFORMATION. The average weekly coefficient on dispersion is +0.00027 with a 95% interval of [-0.00918, +0.01022] — an interval 37 times the point estimate, straddling zero.",
      "The estimate sits at the 54th percentile of a null built by permuting dispersion across panels within each week. Scrambling the very pairing under test reproduces the measured result.",
      "The sign flips between halves (-0.00104 then +0.00158), and flips in every reported leg as well: 1-week, 13-week, the standard-deviation variant and the panel-size control.",
      "All three gates fail independently, so the verdict does not rest on any one of them, and Layer B — the sleeve A challenger at WS5's +0.10 bar — never opened.",
      "Nothing is adopted, proposed or built: no engine arm, no dashboard panel, no scanner column. The study cost one day, which is what the two-layer design was for.",
    ] },

    { type: "h1", text: "2. What was registered, and when" },
    { type: "p", text: "The question came from a third-party note on semiconductor breadth received on Thursday 2026-09-17. Three routes were put to the owner — a dashboard panel, a scanner group view, a pre-registered study — and the study was chosen. The registration was frozen in a dated commit before the module existed and before any figure bearing on any gate had been computed; the engine and its selftests were committed before the run, and the run was a single scripted pass." },
    { type: "p", text: "Four filed records were declared as priors before any data was seen. The per-name relative-trend leg was already rejected (2026-07-10-breadth-thrust-etf-1), the standing abs-versus-relative diagnostic panel already downgraded (-2), narrowness-as-bearish already rejected at index level (2026-07-03-breadth-thrust-signal-1), and our own lead-triage filter already records that only price-based vendor leads reproduce on this data (2026-08-08-event-studies-3). WS20 tested the one object none of them covers: a second moment of the trend distribution rather than its level." },
    { type: "callout", text: "The design turns on one decision. Dispersion and breadth are mechanically related — at 0% or 100% breadth every name sits on the same side of its average and the spread is compressed by construction — so an unconditional sort on dispersion would rediscover the breadth level in different clothing. The verdict therefore reads the coefficient on dispersion CONDITIONAL on demeaned breadth, and the verdict function is given no other argument." },

    { type: "h1", text: "3. Construction" },
    { type: "p", text: "For each panel and session, the per-name trend distance is close divided by its own 200-day moving average, less one, over the names valid on the deployed mask (price present and the average computable at min_periods 180 — the same denominator that produces the breadth reading being tested against). The panel statistic is the cross-sectional interquartile range of that distance." },
    { type: "bullets", items: [
      "Interquartile range rather than standard deviation: the trend distance is unbounded above, so a single name at several multiples of its own average would own a variance. A planted outlier moves the standard deviation by more than its own level while moving the interquartile range by under a fifth of its own — pinned by test.",
      "Standardised within the panel against its own strictly-prior history (expanding, minimum 252 sessions) rather than compared raw across panels: the 14 panels carry between 21 and 602 constituents and differ systematically in sector volatility, so a raw cross-panel comparison is not like-for-like.",
      "Read at t-1 and projected onto the trade calendar through the deployed freshness-aware helper, exactly as breadth is. A final-bar perturbation test asserts that no quantity at a date before T moves when the last bar is mutated.",
    ] },

    // Page break: without it the first chart lands at the foot of page 2 and
    // its caption is orphaned onto page 3.
    { type: "h1", text: "4. Findings", pageBreakBefore: true },
    { type: "h2", text: "4.1 The measured result is what scrambling the data also produces" },
    { type: "p", text: "The null permutes dispersion across the 14 panels within each week, leaving breadth, the returns and the week structure untouched. It therefore preserves the weekly cross-sectional distribution of dispersion exactly and destroys only the panel-to-dispersion pairing — which is the thing the hypothesis claims is informative." },
    { type: "chart", file: "ws20_null.png", caption: "The vertical red line is the measured estimate; the bars are 1,000 runs in which dispersion was randomly reassigned between panels each week. The measured value falls at the 54th percentile — squarely inside what chance produces. The shaded band is the central 95% of the scrambled runs." },
    { type: "table",
      headers: ["Quantity", "Value"],
      rows: [
        ["Mean weekly coefficient", "+0.00027"],
        ["95% interval (13-week circular block bootstrap, 2,000 resamples)", "[-0.00918, +0.01022]"],
        ["Bootstrap standard error", "0.00490"],
        ["Null median / standard deviation (1,000 paths, seed 20260918)", "+0.00003 / 0.00256"],
        ["Null central 95%", "[-0.00497, +0.00470]"],
        ["Percentile of the measured estimate within the null", "54th"],
      ],
      widths: [6026, 3000], numericFrom: 1 },
    { type: "p", text: "The block length is registered at 13 weeks, more than three times the 4-week forward overlap. Consecutive weekly observations of a 4-week forward return share three quarters of their window, so an independent resample would understate the standard error; on a persistent series the registered block widens the interval against a one-week block by more than half again, which is why it was fixed in advance rather than chosen after seeing an interval." },

    // Without this the 4.2 heading strands at the foot of the page, its chart
    // having moved to the next one.
    { type: "pagebreak" },
    { type: "h2", text: "4.2 Every way of cutting it lands on zero" },
    { type: "chart", file: "ws20_legs.png", caption: "Each point is the average weekly coefficient for one leg; zero means dispersion carries no information once breadth is accounted for. The shaded band is the primary estimate's 95% interval — every other leg falls inside it, and no leg is distinguishable from zero." },
    { type: "table",
      headers: ["Leg", "Mean coefficient", "First half", "Second half"],
      rows: [
        ["Primary — interquartile range, 4-week horizon", "+0.00027", "-0.00104", "+0.00158"],
        ["1-week horizon (non-overlapping)", "-0.00069", "-0.00361", "+0.00223"],
        ["13-week horizon", "-0.00128", "-0.01136", "+0.00880"],
        ["Standard deviation in place of the interquartile range", "+0.00025", "-0.00747", "+0.00792"],
        ["With a panel-size control", "-0.00138", "—", "—"],
        ["Without the breadth control (confounded)", "-0.00202", "—", "—"],
      ],
      widths: [3626, 1800, 1800, 1800], numericFrom: 1 },
    { type: "p", text: "The 1-week leg matters beyond consistency: it is non-overlapping, so it is free of the standard-error hazard the block bootstrap exists to handle. It is null too, and it also flips sign between halves." },

    { type: "h2", text: "4.3 The confound the design was built against proved weak" },
    { type: "p", text: "Pooled over 5,250 panel-weeks, the correlation between the standardised dispersion and demeaned breadth is -0.040, and between raw dispersion and breadth -0.102. The mechanical relation argued in the registration is real in principle but small in this data, so the conditional and unconditional estimates sit close together — both null. The control was therefore not load-bearing here. That is worth recording plainly: a future reader should not conclude that the breadth control is what killed the result, because it is not." },

    { type: "h2", text: "4.4 The thinness gate returned nothing usable, by construction" },
    { type: "p", text: "The thinness gate scores each panel's and each calendar year's share of the total coefficient, and can force INCONCLUSIVE on an otherwise passing result. With a total of +0.00027 and individual contributions an order of magnitude larger in both directions, those shares explode — the largest panel share reads -12.3 and the largest year share +12.5 — and the flag trips meaninglessly. It did not enter this verdict, which was already settled by the three primary gates." },
    { type: "callout", text: "General lesson for any future registration: a share-of-total decomposition is undefined when the total is approximately zero. It should be specified as evaluated only on a pass, which is how this one was used but not how it was worded." },

    { type: "h1", text: "5. Decisions" },
    { type: "table",
      headers: ["Component", "Decision", "Basis"],
      rows: [
        ["Dispersion as a sleeve A ranking input", "REJECT", "Layer A fails all three gates; Layer B never opened"],
        ["Dispersion as a standing dashboard panel", "REJECT", "No information to display; consistent with 2026-07-10-breadth-thrust-etf-2"],
        ["Dispersion as a scanner group view", "REJECT", "Was conditional on a Layer A pass"],
        ["Deployed sleeve A breadth leg", "KEEP — untouched", "Not a variable in this study"],
        ["Thinness-gate wording for future registrations", "FLAGGED", "Undefined when the total is approximately zero (section 4.4)"],
      ],
      widths: [3026, 2000, 4000] },

    { type: "h1", text: "6. Trial register" },
    { type: "p", text: "Six cells were computed and all six are reported: the primary, the confounded univariate, the panel-size control, the 1-week and 13-week horizons, and the standard-deviation variant. No cell was computed and withheld; the results file lists every one, so a later reader can count them. The 2,000 bootstrap resamples and 1,000 null paths are inference on a single estimate, not additional trials." },
    { type: "p", runs: [
      { text: "No parameter was swept. ", bold: true },
      { text: "One dispersion statistic, one standardisation, one specification, one primary horizon, one null — the registration forbids a neighbour cell precisely because computing several and reporting the best is what converts a declared choice into a fitted one. Nothing here requires a multiple-testing haircut, because nothing was selected." },
    ] },

    { type: "h1", text: "7. Artefact register" },
    { type: "table",
      headers: ["Artefact", "Path"],
      rows: [
        ["Registration", "C:\\dev\\KICKOFF_ws20-trend-dispersion.md (freeze 1cd0626)"],
        ["Engine", "scripts/ws20_dispersion.py"],
        ["Runner", "scripts/run_ws20_dispersion.py"],
        ["Selftests (24, all passing)", "tests/test_ws20_dispersion.py"],
        ["Results", "data_local/ws20/results.json (gitignored; local only)"],
        ["Charts", "scripts/plot_ws20_summary.py -> reviews/charts/ws20_null.png, ws20_legs.png"],
        ["Running memo", "RESEARCH_MEMO.md, section WS20"],
        ["This record", "reviews/2026-09-18_ws20_trend-dispersion.docx"],
      ],
      widths: [3626, 5400] },
    { type: "p", text: "The pass was run twice. The second run followed an amendment to persist the null draws and the weekly coefficient series, under the registration's provision that a re-run after a code change restates the whole results file; every figure was identical to the first pass." },

    { type: "h1", text: "8. What this study cannot conclude" },
    { type: "bullets", items: [
      "Nothing about forward information. This is one path, 14 panels a week, 375 decision weeks — an effective cross-section of 14 is small, so a null here is weak evidence of absence rather than evidence of nothing.",
      "Nothing about the source note's own claim. Their statistic is a bounded proprietary composite over a broader semiconductor universe; ours is an interquartile range over an exchange-traded fund's holdings. A null here does not falsify their read, and their construction was deliberately not replicated.",
      "Nothing about sleeves B, C or D, or the European panels, none of which were in scope.",
    ] },

    { type: "h1", text: "9. Next phase" },
    { type: "p", text: "None. The study closes at Layer A with zero deployed changes. Reopening requires a genuinely new per-name object and its own registration — not a neighbour of this one, and not this one at a different horizon." },
  ],
  signoff: [
    ["Prepared by", "Claude Code research session, under direction of Zhenghao Phua"],
    ["Reviewed and approved by", ""],
    ["Date", ""],
    ["Next review", "None — the study is closed at Layer A"],
  ],
  disclaimer: "Personal research artefact. All performance figures are simulated backtests, net of stated costs; nothing here is investment advice. The third-party note that prompted the study is licensed content and is neither reproduced nor distributed here.",
};

module.exports = spec;

if (require.main === module) {
  const out = path.resolve(__dirname, "..", "reviews", "2026-09-18_ws20_trend-dispersion.docx");
  buildReport(spec, out.replace(/\\/g, "/"))
    .then((r) => console.log("wrote", r.outPath, r.bytes, "bytes"))
    .catch((e) => { console.error(e); process.exit(1); });
}
