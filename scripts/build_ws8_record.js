/*
 * WS8 technical findings record — build spec + runner.
 * REIT dual-coverage ablation (IUSP in sleeve A, VNQ in sleeve B) plus the
 * companion overlap-gate repair. Verdict: KEEP BOTH.
 *
 * Every embedded number traces to data/ws8_reit_overlap.json (baselines,
 * variants, keep bar, look-through) or data/overlap_audit.json (breach list).
 * The three WS2 comparison figures trace to data/ws2_correlation.json.
 * Charts from scripts/plot_ws8_summary.py.
 *
 * Run:  node scripts/build_ws8_record.js
 */
const path = require("path");
const { buildReport } = require("C:/Users/phuaz/.claude/skills/research-review/assets/report_builder.js");

const ASSETS = path.resolve(__dirname, "..", "reviews", "assets");
const OUT = path.resolve(__dirname, "..", "reviews", "2026-08-05_ws8_reit-dual-coverage.docx");

const spec = {
  meta: {
    title: "WS8 — REIT dual coverage: does the second line earn its place?",
    subtitle: "A pre-registered ablation of the IUSP/VNQ pair, and the repair of the overlap gate that never tested it",
    dateISO: "2026-08-05",
    weekday: "Wednesday",
    headerLeft: "WS8 — REIT dual coverage (breadth-thrust-etf)",
    headerRight: "KEEP BOTH",
    metaLeftW: 2500,
    assetsDir: ASSETS,
  },
  metaTable: [
    ["Project / context", "breadth-thrust-etf — Personal research artefact"],
    ["Study", "Whether US REIT exposure should be reached by one sleeve or two. Sleeve A holds IUSP (priced through XLRE) on constituent breadth; sleeve B holds VNQ on price momentum; the pair correlates 0.990 on weekly returns"],
    ["Trigger", "Owner question, 2026-08-05: \"why do REITs appear in both A and B, is it supposed to appear in only one category?\""],
    ["Evaluation window", "2018-11-08 to 2026-07-17 (1,929 trading days), train/test split 2022-09-08 — the WS2 fixed window, extended to the current cache end"],
    ["Data basis", "Committed parquet caches read directly (no refetch, no cache rewrite): asset-class and thematic price panels post-adjustment, sleeve A/D constituent and proxy OHLC caches, EUR/USD from the WS1 FX cache"],
    ["Method basis", "Deployed engines only (run_portfolio for A, run_asset_class_rotation.run_rotation for B); prior-day signal row, weights.shift(1) x returns; deployed costs A 2 bps / B 2 bps plus a 2x stress leg; WS2 P1/P2 keep bar judged at blend level"],
    ["Repository commits", "ablation ede814a; overlap-gate repair and guard tests fc4234a"],
    ["Running memo", "RESEARCH_MEMO.md (Workstream 8 section)"],
    ["Outcome", "KEEP BOTH — neither line can be removed without cost, and the pair is a smaller double-count than two duals WS2 already accepted. Separately: the candidate gate was enforcing neither of the project's two overlap rules; now repaired"],
  ],
  sections: [
    // ---------------------------------------------------------------- 1
    { type: "h1", text: "1. Executive summary" },
    { type: "numbers", items: [
      [{ text: "KEEP BOTH REIT lines. ", bold: true },
       { text: "Both pre-registered drops fail all three legs of the keep bar. Removing VNQ from sleeve B costs -0.005 blend Sharpe out of sample at 3 of 6 sub-periods and does not survive 2x cost (+0.980 against a +0.999 sleeve baseline); removing IUSP from sleeve A is worse at -0.012 and 2 of 6. The dual coverage now rests on evidence rather than on a one-line comment." }],
      [{ text: "The question was well posed; the magnitude was not what it looked like. ", bold: true },
       { text: "Across 473 weeks the pair reaches a mean 3.57% of NAV with both sleeves holding simultaneously in 28.5% of weeks, against WS2's already-accepted SPY dual at 3.98% and 43.2%, and QQQ at 6.79% and 42.7%. The REIT pair is the SMALLER double-count on both measures." }],
      [{ text: "The live snapshot that prompted the question is an outlier, not the norm. ", bold: true },
       { text: "At the 2026-07-31 anchor IUSP sat at 9.77% of NAV and VNQ at 3.31%, 13.07% combined — high against the 3.57% historical mean, but inside the 20.26% historical peak." }],
      [{ text: "Every margin here is far inside what the sample can resolve. ", bold: true },
       { text: "The largest blend delta is 0.012 Sharpe against a standard error of approximately 0.40 on 7.7 years of weekly data. The honest reading is that the choice does not matter much either way — which is itself the argument for leaving a working configuration alone." }],
      [{ text: "The companion finding is the larger one: the candidate gate was enforcing neither overlap rule. ", bold: true },
       { text: "Three defects — a basis mismatch (return correlation compared to a threshold defined on signal correlation), a sleeve-scope asymmetry (sleeve C candidates screened against sleeve A, sleeve B candidates not), and a silent all-NaN collapse on the mixed-calendar panel that would have returned PASS for any candidate compared against nothing." }],
      [{ text: "The retrospective audit finds 18 incumbent pairs above the 0.90 rule, of which two are genuinely unstudied. ", bold: true },
       { text: "Three are measurement artefacts (sleeve A priced through the very ticker sleeve B holds) and thirteen are covered by prior filed work. The two new ones are VGK~EXH3 at 0.913 and EFA~EXH3 at 0.903 — sleeve B's Europe lines against sleeve D's Europe sector line." }],
      [{ text: "No deployed change. ", bold: true },
       { text: "2 configurations evaluated, 0 adopted, 2 overlaps placed on the watch list. Sleeves C and D and the two overlays were out of scope and are neither changed nor cleared by this study." }],
    ] },

    // ---------------------------------------------------------------- 2
    { type: "h1", text: "2. What runs, and what WS2 left open" },
    { type: "p", text: "US REIT exposure enters the book twice by design. Sleeve A ranks IUSP (iShares US Property Yield UCITS, priced and traded through XLRE) on the share of its constituents above their own 200-day moving average, demeaned across the 14 sector lines. Sleeve B ranks VNQ (Vanguard Real Estate) on its own distance above its 200-day moving average. Two different signals, two different wrappers, one underlying exposure." },
    { type: "p", runs: [
      { text: "WS2 saw the pair and did not test it. ", bold: true },
      { text: "The 2026-07-02 universe review measured XLRE against VNQ at 0.990 weekly return correlation — the highest pair in the book among instruments actually held — recorded it as \"deliberate dual-signal coverage, US-only; global REIT gap minor\", and adopted the overlap rule \"reject candidates above 0.9 versus an incumbent unless distinct exposure is argued in writing\". Two things follow. First, the rule is prospective by construction: it screens CANDIDATES, so no incumbent has ever been tested against it. Second, WS2 pre-registered exactly two prune bundles — VGK from B, and {TAN, SKYY, PAVE} from C — and the REIT pair was not among them. The written argument on file is one line of source comment." }] },
    { type: "p", runs: [
      { text: "WS2 also quantified look-through for the other duals, but not for this one. ", bold: true },
      { text: "Its blend look-through table covers SPY, QQQ and IJR — the three lines sleeves A and B reach simultaneously — with mean weight, peak weight and the share of weeks both sleeves held them. REITs are absent from that table. WS8 closes both gaps: the ablation WS2 did not run, and the look-through it did not compute." }] },
    { type: "callout", text: "Scope note. V1 removes sleeve B's REIT line while sleeve A keeps IUSP. The result therefore measures the marginal value of the SECOND REIT line, not the value of REIT exposure. Nothing here says REITs add or subtract; it says that once one sleeve holds them, removing the other sleeve's line costs more than it saves." },

    // ---------------------------------------------------------------- 3
    { type: "h1", text: "3. Findings" },

    { type: "h2", text: "3.1 Baseline integrity — the comparator had to be rebuilt first" },
    { type: "p", text: "The cached WS2 baselines are not usable for this test. Their sleeve B still holds EEM, which Phase 29 removed on 2026-07-02 (EEM carries a 5.5% mean weight and appears in 868 weeks of that cached frame), and their sleeve D predates both the Phase 30 European rebuild of 2026-08-01 and the EXH3-to-EXH4 instrument correction of 2026-08-03. Measuring a VNQ drop against them would have priced three changes as one and attributed the sum to REITs. Baselines were therefore rebuilt from today's deployed configuration on the same fixed window, and the drift is reported rather than absorbed." },
    { type: "table",
      headers: ["Sleeve", "Rebuilt full Sharpe", "Cached WS2 meta", "Drift", "Cause"],
      rows: [
        ["A US sectors", "+0.9695", "+0.9913", "-0.022", "cache-end shift only"],
        ["B asset class", "+0.9994", "+1.0065", "-0.007", "EEM removal (Phase 29)"],
        ["C thematic", "+0.6927", "+0.7341", "-0.041", "cache-end shift only"],
        ["D Europe sectors", "+0.9054", "+0.8665", "+0.039", "Phase 30 rebuild + EXH3 to EXH4"],
        ["Blend 35/35/10/20", "+1.1867", "+1.1961", "-0.009", "net of the above"],
      ],
      widths: [2200, 1750, 1600, 1100, 2376], numericFrom: 1 },
    { type: "p", runs: [
      { text: "The sleeve D line is the EXH3-to-EXH4 correction arriving in the blend. ", bold: true },
      { text: "It is the first blend-level figure computed on the corrected instrument, and it is reported here as a by-product, not as a validated restatement — the README's supersession notice on all pre-2026-08-03 D and blend statistics still stands until the engines re-run in full." }] },

    { type: "h2", text: "3.2 The ablation — neither line can be dropped" },
    { type: "p", text: "Two variants, pre-registered from correlation evidence alone before any result was inspected. V1 drops VNQ from sleeve B with K held at 7 of now-11; V2 drops IUSP from sleeve A with K held at 7 of now-13, the cross-sectional demean recomputing on 13 members as the mechanical consequence of the drop. Both directions were run deliberately: testing only V1 would presuppose that the momentum line is the redundant one. The keep bar follows the WS2 P1/P2 convention and is judged at blend level — out-of-sample Sharpe not worse, at least 4 of 6 sub-periods at or above the deployed blend, and the sleeve survives 2x cost. The incumbent wins ties." },
    { type: "chart", file: "ws8_fig1_ablation.png",
      caption: "Change in blend Sharpe from dropping each REIT line. Left: on a scale where the numbers are legible, both variants fall below zero out of sample (teal) and so fail the keep bar. Right: the same four numbers drawn against the sample's own margin of error — one standard error is approximately 0.40 Sharpe on 7.7 years of weekly data, and every bar is a hairline inside it. The decision is clear; the effect is not large." },
    { type: "table",
      headers: ["Variant", "Sleeve full", "Sleeve OOS", "At 2x cost", "Blend d full", "Blend d OOS", "Sub-periods", "Verdict"],
      rows: [
        ["Deployed (both held)", "B +0.9994 / A +0.9695", "B +0.9334 / A +1.2812", "—", "—", "—", "—", "incumbent"],
        ["V1 B drops VNQ", "+1.0002", "+0.9179", "+0.9798", "+0.004", "-0.005", "3 of 6", "KEEP INCUMBENT"],
        ["V2 A drops IUSP", "+0.9573", "+1.2497", "+0.9404", "-0.007", "-0.012", "2 of 6", "KEEP INCUMBENT"],
      ],
      widths: [1700, 1500, 1300, 1000, 900, 900, 900, 826], numericFrom: 1 },
    { type: "p", runs: [
      { text: "V1 is the closer call and still fails on all three legs. ", bold: true },
      { text: "Dropping VNQ is free on the full window (+0.001 at sleeve level) and improves sleeve B's maximum drawdown from -13.19% to -11.75% while cutting turnover from 12.12x to 11.49x. It nonetheless loses the out-of-sample half by 0.016 at sleeve level, manages only 3 of 6 sub-periods, and falls to +0.980 at 2x cost against a +0.999 baseline. V2 fails more plainly and does not even buy drawdown: -30.64% against -30.62%." }] },

    { type: "h2", text: "3.3 Look-through — the pair is smaller than the duals already accepted" },
    { type: "p", text: "Effective NAV weight is the within-sleeve weight times the sleeve's blend share (35% for both A and B), sampled on the weekly rebalance grid across 473 weeks. This is the same construction WS2 applied to SPY, QQQ and IJR, so the figures are directly comparable." },
    { type: "chart", file: "ws8_fig2_lookthrough.png",
      caption: "How much of the book each dual-coverage pair reaches twice, and how often both sleeves hold it at once. The REIT pair (red) carries a smaller mean overlap than the SPY and QQQ pairs WS2 examined and accepted, and both sleeves hold it simultaneously far less often. The comparison figures are WS2's own, from data/ws2_correlation.json." },
    { type: "table",
      headers: ["Pair", "Mean of NAV", "Peak of NAV", "Weeks both sleeves held it", "Status"],
      rows: [
        ["QQQ (A CNDX + B QQQ)", "6.79%", "24.08%", "42.7%", "accepted, WS2"],
        ["SPY (A CSP1 + B SPY)", "3.98%", "10.36%", "43.2%", "accepted, WS2"],
        ["REITs (A IUSP + B VNQ)", "3.57%", "20.26%", "28.5%", "accepted, WS8"],
        ["IJR (A IDP6 + B IJR)", "2.11%", "13.69%", "2.5%", "accepted, WS2"],
      ],
      widths: [2600, 1500, 1500, 2100, 1326], numericFrom: 1 },
    { type: "p", runs: [
      { text: "Individually: ", bold: true },
      { text: "IUSP averages 1.61% of NAV with a 10.65% peak and is held in 32.8% of weeks; VNQ averages 1.96% with a 12.18% peak and is held in 35.9% of weeks. The peak of the combined pair, 20.26%, is the figure to watch operationally — the live 13.07% reading of 2026-07-31 sits well inside it, but a REIT-favourable regime can put a fifth of the book into one exposure through two doors." }] },

    { type: "h2", text: "3.4 The overlap gate was enforcing neither of the project's two rules" },
    { type: "p", text: "Auditing the book to place the REIT pair in context exposed three defects in the candidate gates. Each of them lets overlap through, and the third does so silently." },
    { type: "numbers", items: [
      [{ text: "Basis mismatch. ", bold: true },
       { text: "Both check scripts correlated weekly RETURNS and compared the result to 0.85, citing the Phase 5 threshold. Phase 5 and the Phase 25 screen both measured weekly SIGNAL correlation — distance above the 200-day moving average, the quantity the sleeves actually rank on. Signal series are slow and strongly autocorrelated, so their correlations sit above return correlations for the same pair, and applying 0.85 to returns was a looser gate than specified." }],
      [{ text: "Sleeve-scope asymmetry. ", bold: true },
       { text: "Sleeve C candidates were always screened against sleeve A's sector slate as well as C's own members — that is precisely how XOP, OIH and AMLP were rejected, all three on their correlation with XLE. Sleeve B candidates were screened against B incumbents only. The same standard was applied to one sleeve and not the other." }],
      [{ text: "Silent all-NaN collapse. ", bold: true },
       { text: "A whole-frame 200-day rolling mean with min_periods=200 over a panel spanning the NYSE, Xetra, Shenzhen and 24x7 crypto calendars returns all-NaN: every 200-row window of every column contains another calendar's dates. Every pair is then skipped for want of overlapping observations and every candidate returns PASS having been compared against nothing. This is the failure mode that looks exactly like success." }],
    ] },
    { type: "p", runs: [
      { text: "Repair and regression check. ", bold: true },
      { text: "Both rules now run book-wide against the deployed universe resolved through scanner_universe.resolve_universe(), so the gate, the daily scanner and the sleeve engines cannot disagree about what is held; signal is computed per column on its own observed dates; an empty signal panel raises rather than returning verdicts. The rewritten gate reproduces the documented figures: XOP 0.948 against Phase 5's 0.947, AMLP 0.854 against 0.853, SLV against GLD 0.780 against Phase 16's 0.78, and ITB passes as it did in Phase 5. Re-screening VNQ as though it were a fresh sleeve B candidate now fails on both rules against IUSP (signal 0.984, return 0.990); under the old B-side gate its highest within-B correlation was 0.743 and it would have passed unremarked." }] },

    { type: "h2", text: "3.5 Retrospective audit of the deployed book" },
    { type: "p", text: "Running the WS2 rule backwards over the 57 deployed lines — the sweep the prospective rule never performed — returns 18 pairs above 0.90." },
    { type: "chart", file: "ws8_fig3_audit.png",
      caption: "Every incumbent pair above the 0.90 overlap rule, classified. Grey pairs are measurement artefacts: sleeve A is priced through the very ticker sleeve B holds, so the panel carries one price series under two names and the coefficient is structural rather than measured. Navy pairs are covered by prior filed work. Only the two red pairs — sleeve B's Europe lines against sleeve D's Europe sector line — are measured overlaps that no study has examined." },
    { type: "p", runs: [
      { text: "The three artefacts matter for how the guard is read, not for the book. ", bold: true },
      { text: "CSP1 is priced as SPY, CNDX as QQQ and IDP6 as IJR, so those pairs self-correlate at approximately 1.000. The exposure overlap is entirely real — it is what WS2 quantified as the US-beta cluster at a mean 46.8% and peak 83.5% of NAV — but the coefficient is not evidence about it, and three permanent false positives sitting at the top of a guard's output is how a guard trains its readers to skip it. They are now labelled rather than mixed in." }] },
    { type: "p", runs: [
      { text: "The one new finding. ", bold: true },
      { text: "VGK against EXH3 at 0.913 and EFA against EXH3 at 0.903 are sleeve B's Europe lines against sleeve D's Europe sector line, now correctly priced as EXH4.DE after the 2026-08-03 correction. No prior study covers this pair. It is reported, not actioned: on WS8's own evidence a 0.99-correlated pair was not worth unwinding, so a 0.91 pair does not obviously clear that bar either, and finding out costs a full pre-registered ablation." }] },

    // ---------------------------------------------------------------- 4
    { type: "h1", text: "4. Decisions" },
    { type: "table",
      headers: ["Component", "Decision", "Basis"],
      rows: [
        ["Sleeve A IUSP (REIT line)", "KEEP", "V2 drop costs -0.012 blend OOS at 2 of 6 sub-periods, fails 2x cost (3.2)"],
        ["Sleeve B VNQ (REIT line)", "KEEP", "V1 drop costs -0.005 blend OOS at 3 of 6 sub-periods, fails 2x cost (3.2)"],
        ["REIT dual coverage as an architecture", "KEEP, now evidenced", "Smaller mean look-through and lower simultaneous-holding rate than the SPY and QQQ duals WS2 accepted (3.3)"],
        ["Candidate gate — correlation basis", "FIXED", "Signal basis restored for Rule 1; return basis retained for Rule 2 (3.4)"],
        ["Candidate gate — sleeve scope", "FIXED", "Both rules now screen book-wide, resolved from the engines (3.4)"],
        ["Candidate gate — NaN collapse", "FIXED, guarded", "Per-column signal plus a hard raise; 5 guard tests added (3.4)"],
        ["VGK / EFA against EXH3", "FLAGGED", "0.913 and 0.903, measured, no prior study; needs its own pre-registered ablation (3.5)"],
        ["Combined REIT peak exposure", "FLAGGED, operational", "20.26% of NAV at the historical peak through two sleeves (3.3)"],
      ],
      widths: [2500, 1500, 5026], numericFrom: 99 },

    // ---------------------------------------------------------------- 5
    { type: "h1", text: "5. Trial register" },
    { type: "p", text: "Configurations evaluated: 2 — V1 (sleeve B drops VNQ) and V2 (sleeve A drops IUSP) — each additionally run at 2x cost as a robustness leg rather than as a separate arm. Both were registered in the script docstring before the run executed, both directions were declared together, and no third direction was added after results were seen. No K was re-tuned, no signal floor or sleeve gate was altered, and no variant was selected post hoc. The look-through table and the book-wide audit are descriptive, carry no free parameters, and are not verdict-relevant. A deflated-Sharpe haircut over N=2 would be immaterial here, since neither arm was adopted and neither delta approaches the sample's standard error." },

    // ---------------------------------------------------------------- 6
    { type: "h1", text: "6. Artefact register" },
    { type: "table",
      headers: ["Artefact", "Path", "Role"],
      rows: [
        ["Ablation engine", "scripts/run_ws8_reit_overlap.py", "Pre-registration, baselines, V1/V2, look-through"],
        ["Ablation evidence", "data/ws8_reit_overlap.json", "Every figure in sections 3.1-3.3"],
        ["Overlap gate", "scripts/check_universe_candidates.py", "Both rules, book-wide; --audit mode"],
        ["Sleeve C entry point", "scripts/check_thematic_candidates.py", "Thin wrapper so the logic cannot drift in two copies"],
        ["Audit evidence", "data/overlap_audit.json", "The 18 breaches in section 3.5"],
        ["Guard tests", "tests/test_overlap_gate.py", "5 tests; full suite 653 passing"],
        ["Charts", "scripts/plot_ws8_summary.py", "Figures 1-3, reproducible from the JSON"],
        ["Running memo", "RESEARCH_MEMO.md", "Workstream 8 section"],
        ["Commits", "ede814a, fc4234a", "Ablation; gate repair and tests"],
      ],
      widths: [2300, 3200, 3526], numericFrom: 99 },

    // ---------------------------------------------------------------- 7
    { type: "h1", text: "7. Next phase" },
    { type: "bullets", items: [
      [{ text: "No deployed change from this study. ", bold: true },
       { text: "Both REIT lines stay. The blend, the sleeve weights and the two overlays are untouched." }],
      [{ text: "Two overlaps on the watch list. ", bold: true },
       { text: "VGK/EXH3 (0.913) and EFA/EXH3 (0.903). Each would need its own pre-registered ablation; WS8 is the worked example of what that costs and of how likely it is to change nothing." }],
      [{ text: "Run the audit on a schedule, once a monitor exists. ", bold: true },
       { text: "The --audit sweep is the component that would have surfaced the REIT pair without being asked. It writes JSON and touches nothing, so it is safe to run unattended, but per the vault rule it needs a capture-integrity check on its input panel before it is trusted: a silently truncated panel reads as \"no breaches\", which is the failure mode that looks like success." }],
      [{ text: "Sleeve C remains out of scope. ", bold: true },
       { text: "The seat review is pre-registered for Friday 2026-10-02 and nothing here bears on it." }],
    ] },
  ],
  signoff: [
    ["Prepared by", "Claude Code (Opus 5), on instruction"],
    ["Reviewed and approved by", "Zhenghao Phua — pending"],
    ["Date", "2026-08-05 (Wednesday)"],
    ["Next review", "With the next scheduled universe review, or when a new line is proposed for any sleeve"],
  ],
  disclaimer: "Personal research artefact. Not investment advice, not affiliated with any regulated fund, and not a representation of any managed product. Every return figure is simulated; there is no live track record. Sharpe figures on this sample carry a standard error of approximately 0.40 and should be read as ranges, not point estimates.",
};

buildReport(spec, OUT).then((r) => console.log("wrote", r.outPath, r.bytes));
