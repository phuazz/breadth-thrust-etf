/*
 * WS5 technical findings record — build spec + runner.
 * Constituent relative-trend challenger to Sleeve A (SentimenTrader relative-
 * trend-score concept). Verdict: KEEP A0.
 *
 * Every embedded number traces to data/ws5_results.json (register, verdict
 * conditions, DSR, overlap, parity). Charts from scripts/plot_ws5_summary.py.
 *
 * Run:  node scripts/build_ws5_record.js
 */
const path = require("path");
const { buildReport } = require("C:/Users/phuaz/.claude/skills/research-review/assets/report_builder.js");

const ASSETS = path.resolve(__dirname, "..", "reviews", "assets");
const OUT = path.resolve(__dirname, "..", "reviews", "2026-07-10_ws5_relative-trend.docx");

const spec = {
  meta: {
    title: "WS5 — Constituent relative-trend breadth: a challenger to Sleeve A",
    subtitle: "SentimenTrader relative-trend-score concept, tested as a pre-registered Sleeve-A signal swap",
    dateISO: "2026-07-10",
    weekday: "Friday",
    headerLeft: "WS5 — Relative-trend challenger (breadth-thrust-etf)",
    headerRight: "KEEP A0",
    metaLeftW: 2500,
    assetsDir: ASSETS,
  },
  metaTable: [
    ["Project / context", "breadth-thrust-etf — Personal research artefact"],
    ["Study", "Swap ONLY Sleeve A's per-constituent trend condition — absolute (deployed) vs relative-to-SPY vs dual — holding Phase 20 demeaning, Phase 20.1 top-K weighting, universe and costs fixed"],
    ["Evaluation window", "2018-10-12 to 2026-06-30 (registered 2018-Q4 to 2026-Q2, = deployed backtest window). Walk-forward: initial train to 2020-12-31, then 6 annual K-refits, out-of-sample 2021-01 to 2026-06"],
    ["Data basis", "Norgate-grade constituent adjusted closes for all 14 Sleeve-A ETFs (33-1,267 names each), SPY benchmark for the relative leg; ~91.9% of the cached calendar is post-warm-up (identical across ETFs by construction)"],
    ["Method basis", "Deployed Sleeve-A path unchanged downstream of the per-name condition; 2 bps deployed cost; 2x-cost robustness leg on identical folds; momentum placebo control; DSR haircut"],
    ["Repository commits", "engine 75acc3d; asymmetric-window extension 837c9ff; registered run fda73ad"],
    ["Running memo", "RESEARCH_MEMO.md (WS5 section)"],
    ["Outcome", "KEEP A0 — no challenger clears the pre-registered rule; the relative leg is real but redundant, losing to the deployed absolute leg and to plain momentum"],
  ],
  sections: [
    // ---------------------------------------------------------------- 1
    { type: "h1", text: "1. Executive summary" },
    { type: "numbers", items: [
      [{ text: "KEEP A0 (the deployed absolute leg). ", bold: true },
       { text: "No challenger clears the frozen adopt rule. Making the per-constituent trend relative to the benchmark reduces walk-forward out-of-sample Sharpe from +1.128 (A0) to +0.947 (A1 relative) and +0.906 (A2 dual); the adopt bar was A0 + 0.10 = +1.228." }],
      [{ text: "The relative leg loses even to a naive momentum control. ", bold: true },
       { text: "Plain 126-day sector relative-momentum (P) walk-forwards at +1.031 — above all four relative/dual arms. A signal that cannot beat price momentum adds nothing over the incumbent." }],
      [{ text: "The signal is real, not noise — but redundant. ", bold: true },
       { text: "The best challenger's own Sharpe is highly unlikely to be luck (deflated Sharpe 0.989 over N=8). The relative leg produces a genuinely positive strategy; it is simply inferior to what is already deployed, and 0.95-correlated to the momentum control." }],
      [{ text: "A positive by-product: Sleeve A's mechanism is re-validated. ", bold: true },
       { text: "Constituent absolute breadth (A0, +1.128) beats plain ETF momentum (P, +1.031) by +0.097 walk-forward — direct evidence that per-name breadth carries information beyond price momentum, which is the premise Sleeve A rests on." }],
      [{ text: "No deployed change; scope respected. ", bold: true },
       { text: "7 engine arms evaluated, 0 adopted, 0 on watch. WS5 tested Sleeve A only; B, C, D and the two overlays were out of scope and are neither changed nor cleared by this study." }],
    ] },

    // ---------------------------------------------------------------- 2
    { type: "h1", text: "2. What was built and what runs" },
    { type: "p", text: "The SentimenTrader sector table aggregates two per-stock 0-10 trend composites — one on absolute price, one on the price ratio to the S&P 500 — and highlights the share of members scoring high on BOTH. Sleeve A already runs the absolute leg (share of a sector's constituents whose close is above their own 200-day moving average, demeaned cross-sectionally across the 14 ETFs, top-K positive-weighted). The one genuinely new object is the per-constituent RELATIVE leg: the same trend test computed on each stock's SPY ratio rather than its raw price. WS1 had tested only reformulations of the absolute measure (a flat plateau; all lost out of sample), so this is the first structurally different per-name signal since Phase 20." },
    { type: "p", runs: [
      { text: "Shared validity mask (the key design choice). ", bold: true },
      { text: "All arms use one denominator: a constituent counts on a given day only if BOTH legs are computable that day. This guarantees the arms differ ONLY in the per-name condition, never in which names are eligible. Because the ratio inherits the close's missing-data mask (SPY prints every US session), the shared mask collapses to the absolute leg's own mask — so the module's absolute arm reproduces the deployed compute_ma200_breadth to the float. That parity is an asserted selftest and the first line of the results (breadth max-diff 0.0)." }] },
    { type: "p", runs: [
      { text: "Engine frozen before results. ", bold: true },
      { text: "scripts/relative_trend.py (the three legs plus 150d/250d relative-window variants) and 13 selftests were committed before the run harness executed once, per the pre-registration (em-rotation-lab s1.9b precedent). Selftests cover the three registered failure modes — ratio-leg look-ahead (future-mutation and final-bar-perturbation invariance), denominator asymmetry (shared-mask equality, missing-name drop) and the deployed-parity anchor — plus structural invariants and month/year date boundaries." }] },

    // ---------------------------------------------------------------- 3
    { type: "h1", text: "3. Findings" },

    { type: "h2", text: "3.1 The registered comparison — no arm clears the bar" },
    { type: "p", text: "The seven registered arms were run once through identical walk-forward folds (annual K-refit over K in {3,5,7,9}). The adopt rule required a challenger to beat both the incumbent A0 and the momentum control P by at least +0.10 walk-forward Sharpe, hold drawdown within 2pp, survive the same test at 2x cost, and keep weekly selections distinct from P (Jaccard < 0.8). The result is unambiguous." },
    { type: "chart", file: "ws5_fig1_wf_sharpe.png",
      caption: "Walk-forward out-of-sample Sharpe (2 bps) by arm. The deployed absolute leg (navy) and its union with the relative leg (OR) sit at ~+1.13; every relative or dual arm sits below the momentum-placebo line (teal, +1.031) and far below the adopt threshold (red, +1.228). Making the per-name trend relative destroys information rather than adding it." },
    { type: "table",
      headers: ["Arm", "Role", "Full Sharpe", "WF OOS", "WF 2x", "MaxDD"],
      rows: [
        ["A0 absolute", "Deployed incumbent", "+1.009", "+1.128", "+1.107", "-30.6%"],
        ["A1 relative", "Challenger", "+0.923", "+0.947", "+0.929", "-31.2%"],
        ["A2 dual (200d)", "Challenger", "+0.905", "+0.906", "+0.887", "-30.9%"],
        ["A2 dual (150d rel)", "Neighbour (report-only)", "+0.887", "+0.955", "+0.934", "-31.1%"],
        ["A2 dual (250d rel)", "Neighbour (report-only)", "+0.929", "+0.984", "+0.966", "-30.8%"],
        ["OR (A0 or A1)", "Union", "+1.031", "+1.126", "+1.106", "-30.9%"],
        ["P momentum", "Placebo control", "+0.924", "+1.031", "+1.016", "-33.1%"],
      ],
      widths: [1826, 2400, 1200, 1200, 1200, 1200], numericFrom: 2 },
    { type: "p", runs: [
      { text: "Verdict conditions. ", bold: true },
      { text: "Both A1 and A2 fail conditions 1, 2 and 4 (walk-forward Sharpe below A0, below P, and the same at 2x cost) and pass only 3 and 5 (drawdown within tolerance; selections distinct from the placebo, Jaccard 0.64). Passing 3 and 5 is not adoption — a distinct-but-inferior signal is still inferior. The 150d/250d relative-window neighbours were report-only and non-adoptable by design; both also sit below the placebo, so no nearby window rescues the leg." }] },

    { type: "h2", text: "3.2 The relative leg is real, but redundant and market-correlated" },
    { type: "p", text: "The absence of edge is not an absence of signal. The best challenger (A1) carries an annualised Sharpe of 0.92 against an expected maximum under the null of 0.08 across the N=8 search — a deflated Sharpe of 0.989 (z = 2.29, T = 1,937 days). Read correctly, this says the relative leg produces a genuinely positive standalone strategy; it does not say the leg should be adopted. Adoption is comparative, and the leg loses the comparison twice over — to the deployed absolute leg and to a naive momentum control — while tracking that control at 0.95 return-correlation (weekly-selection Jaccard 0.64). It is a more expensive route to roughly what price momentum already expresses." },
    { type: "p", runs: [
      { text: "Why relative underperforms (mechanism). ", bold: true },
      { text: "Phase 20 already demeans sector breadth cross-sectionally, removing the common market component at the sector level. Measuring each constituent's trend relative to SPY subtracts a market-beta component a second time, at the name level. In a broad up-market with concentrated leadership — the 2019-2026 sample — that beta is precisely what the rotation signal wants to ride; double-subtracting it leaves a noisier residual with less forward information, not more. This is consistent with WS4's market-level finding that strength entries carry nothing, and with the standing house read that breadth suits concentrated sleeves while price momentum suits diversified baskets." }] },

    { type: "h2", text: "3.3 Robustness and coverage" },
    { type: "bullets", items: [
      "2x cost: the ordering is unchanged and no arm crosses the bar — the failure is structural, not a cost artefact (A0 +1.107, A1 +0.929, A2 +0.887 at 2x).",
      "Drawdown: all arms cluster at -30.6% to -31.2% full-sample; the momentum control is the outlier at -33.1%. The relative arms do not buy their lower return with lower risk.",
      "Coverage: breadth is computable on 91.9% of the cached calendar for every ETF; the ~8% gap is the shared 200-day warm-up before the evaluation window and is identical across sleeves by construction (common cache start 2017-07-10, US calendar). Within the tested window, coverage of the US constituents is effectively complete.",
      "Parity: A0-vs-deployed breadth max-diff 0.0; the local walk-forward reproduces the canonical helper at matched 5 bps to 0.0. The verdict runs at Sleeve A's deployed 2 bps.",
    ] },

    // ---------------------------------------------------------------- 4
    { type: "h1", text: "4. Decisions" },
    { type: "table",
      headers: ["Component", "Decision", "Basis"],
      rows: [
        ["Sleeve A per-name condition (A0 absolute)", "KEEP", "Challenger loses on the frozen rule; A0 also beats the momentum control (+0.097 WF), re-validating the mechanism"],
        ["Relative leg (A1) / dual leg (A2)", "REJECT", "WF Sharpe below both A0 and P; 0.95-correlated to the momentum control; fails at 2x cost"],
        ["Tier-1 dual-trend dashboard panel (T5)", "DOWNGRADE", "Verdict shows the abs-vs-rel divergence carries no rotation-relevant edge; retire the standing panel, keep only an on-demand digest read (engine exists, tested)"],
        ["IBD Power Trend", "PARK", "Index-level trend-gate class; competes with the twice-defended Phase 19 gate; off-book habitat (hyper-growth). Reference only"],
        ["walk_forward_sharpe cost hardcode", "FLAG", "Shared helper hardcodes Strategy C's 5 bps (imports from run_thematic_rotation); mis-costs any 2 bps sleeve if reused. Cheap fix, not a deployed-engine risk"],
      ],
      widths: [3026, 1500, 4500] },

    // ---------------------------------------------------------------- 5
    { type: "h1", text: "5. Trial register" },
    { type: "p", runs: [
      { text: "Seven engine arms were evaluated and none selected: ", },
      { text: "A0 absolute, A1 relative, A2 dual, OR (A0 union A1), two asymmetric-window neighbours (dual with a 150d and 250d relative window), and the momentum placebo P. ", bold: false },
      { text: "The deflated-Sharpe haircut is charged at N = 8 — these seven arms plus one blend-context trial — a deliberate conservative pad. The K-grid {3,5,7,9} is the within-arm walk-forward selection, not a separate multiple-testing axis (identical for every arm). Nothing was selected, so no in-sample headline is carried forward." }] },
    { type: "chart", file: "ws5_fig2_scope.png",
      caption: "Configurations evaluated by category. The funnel: 7 arms evaluated, 0 adopted, 0 on watch. The DSR is charged at the more conservative N=8." },

    // ---------------------------------------------------------------- 6
    { type: "h1", text: "6. Artefact register" },
    { type: "bullets", items: [
      "Engine: scripts/relative_trend.py (shared-mask legs + asymmetric relative windows); selftests tests/test_relative_trend.py (13 tests).",
      "Run harness: scripts/run_ws5_relative_trend.py; results data/ws5_results.json.",
      "Charts: scripts/plot_ws5_summary.py -> reviews/assets/ws5_fig1_wf_sharpe.png, ws5_fig2_scope.png.",
      "Record build: scripts/build_ws5_record.js -> this document.",
      "Pre-registration: C:/dev/KICKOFF_ws5-relative-trend.md (sign-off 2026-07-10). Running memo: RESEARCH_MEMO.md (WS5).",
      "Commits: 75acc3d (engine + selftests), 837c9ff (asymmetric windows), fda73ad (registered run + verdict).",
    ] },

    // ---------------------------------------------------------------- 7
    { type: "h1", text: "7. Next phase" },
    { type: "p", text: "The study is complete and closes with no deployed change. The Tier-1 diagnostic is downgraded to an on-demand digest read (no standing panel). The SentimenTrader concept is exhausted as a source of Sleeve-A alpha; its residual value is occasional narrative colour on sector broadening, which the frozen engine can produce cheaply when topical. Reopening would require a genuinely new object, not a re-fit of this one. The standing review follow-ups for B, C, D and the overlays are unaffected by WS5 and remain governed by the 2026-07 staged review." },
  ],
  signoff: [
    ["Prepared by", "Claude Code research session, under direction of Zhenghao Phua"],
    ["Reviewed and approved by", ""],
    ["Date", ""],
    ["Next review", "Event-driven — a new per-name object, or a Sleeve-A re-open at the next scheduled review"],
  ],
  disclaimer: "Personal research artefact. All performance figures are simulated backtests, net of stated costs; nothing here is investment advice. SentimenTrader source material is licensed and remains private; only mechanical definitions and own-computed numbers appear here.",
};

module.exports = spec;

if (require.main === module) {
  buildReport(spec, OUT).then((r) => console.log("wrote", r.outPath, r.bytes)).catch((e) => {
    console.error(e);
    process.exit(1);
  });
}
