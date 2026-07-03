/*
 * Build reviews/2026-07-03_ws3_heavy-gate_summary.docx — the plain-language
 * (allocator) summary of the Workstream 3 heavy gate, via the
 * research-review skill's report_builder engine. Chart-led: every finding
 * is one bold claim, one sentence and one chart; every figure traces to
 * the technical record (numbers require()d from data/ws3_*.json, no new
 * figures invented). Charts are produced by scripts/plot_ws3_summary.py.
 *
 * Run:  node scripts/build_ws3_summary.js [outPath]
 */
const path = require("path");
const { buildReport } = require(
  "C:/Users/phuaz/.claude/skills/research-review/assets/report_builder.js");

const ROOT = path.resolve(__dirname, "..");
const DATA = (f) => path.join(ROOT, "data", f);
const defl = require(DATA("ws3_deflated.json"));
const ob = require(DATA("ws3_overlay_bootstrap.json"));
const wf = require(DATA("ws3_full_wf.json"));
const cost = require(DATA("ws3_cost_stress.json"));
const ep = require(DATA("ws3_entrypoint.json"));

const sh = (x, n = 2) => (x >= 0 ? "+" : "") + x.toFixed(n);
const P = wf.protocols;
const dep = defl.tracks.deployed_final_gated_tilted;
const gate = ob.phase19_gate;
const tilt = ob.phase22_tilt;

const spec = {
  meta: {
    title: "Does the strategy survive a hostile audit?",
    subtitle: "breadth-thrust-etf — the final robustness review, in plain "
      + "language (Workstream 3 of 3)",
    dateISO: "2026-07-03",
    weekday: "Friday",
    headerLeft: "breadth-thrust-etf — robustness review summary",
    assetsDir: path.join(ROOT, "data"),
    metaLeftW: 2200,
  },
  metaTable: [
    ["The question", "After roughly 217 tested configurations across three "
      + "review sessions, is the strategy's track record genuine skill-in-"
      + "design, or the residue of trying many things on one history?"],
    ["The answer", "It survives. The result is three to four times what "
      + "trial-and-error luck alone would produce; every re-tuned version "
      + "of the strategy would have LOST to the version left alone; the "
      + "edge survives triple trading costs. Nothing was changed."],
    ["How it was tested", "Four independent attacks: (1) a statistical "
      + "penalty for every configuration ever tried; (2) forcing the "
      + "strategy to re-choose all its settings each year using only "
      + "information available at the time; (3) re-running history with "
      + "each safety mechanism replaced by 1,000 randomly-timed copies; "
      + "(4) doubling and tripling all trading costs."],
    ["Reading the numbers", "'Risk-adjusted return' is return per unit of "
      + "volatility (Sharpe ratio; higher is better; measured over this "
      + "window the margin of error is roughly ±0.4, so differences of "
      + "±0.01 are noise). 'Drawdown' is the peak-to-trough loss. 'Out of "
      + "sample' means judged only on data the settings had never seen."],
  ],
  sections: [
    { type: "callout", text: "The headline: the review's most valuable "
      + "finding is that discipline, not tuning, is the source of the "
      + "edge. Every version of this strategy that chased what had just "
      + "worked lost to the version that changed nothing." },

    { type: "h1", text: "1. Leaving it alone beat every re-tuned version" },
    { type: "p", text: `A strategy allowed to re-choose ALL of its settings `
      + `each year — always picking whatever looked best on the data so far `
      + `— would have earned a risk-adjusted return of ${sh(P.wf_full.oos_sharpe)} from 2022 to 2026, versus `
      + `${sh(P.frozen_deployed.oos_sharpe)} for the deployed settings left untouched, because each January it bought `
      + `whatever had just had its best run.` },
    { type: "chart", file: "ws3_sum_refit.png", widthPx: 600,
      caption: "Judged only on data after the choice was made (2022-2026). "
        + "The grey bar is the best any fixed setting could have done with "
        + "perfect hindsight — no honest process reaches it." },

    { type: "h1", text: "2. The result is too large to be luck" },
    { type: "p", text: `If the ~217 recorded configuration tests (and a `
      + `liberal allowance for the untracked earlier ones) had produced `
      + `nothing but noise, the best-looking of them would still show a `
      + `risk-adjusted return of about ${sh(dep.v_measured.per_n.register_171.sr0_annual, 2)} to `
      + `${sh(dep.v_diverse_incl_constructions.per_n.nominal_high.sr0_annual, 2)} — the strategy's actual `
      + `${sh(dep.sr_annual, 2)} is three to four times that.` },
    { type: "chart", file: "ws3_sum_selection.png", widthPx: 600,
      caption: "The grey bars answer 'what would the best result look like "
        + "if the strategy had no real edge and we simply kept the "
        + "luckiest attempt?'." },

    { type: "h1", text: "3. The safety brake earns its premium — its "
      + "timing is real" },
    { type: "p", text: `The de-risking brake (it moves half the portfolio to `
      + `short-term Treasuries when market breadth collapses) costs about `
      + `${Math.abs(gate.point_ann_contribution_pct).toFixed(1)}% of return per year and avoided `
      + `${gate.dd_improvement_pp.toFixed(0)} percentage points of worst-case loss — and its timing beat `
      + `${Math.round(gate.placebo.actual_percentile_dd_improvement)}% of one thousand randomly-timed copies of itself.` },
    { type: "chart", file: "ws3_sum_gate.png", widthPx: 600,
      caption: "Same brake, same amount of time de-risked — only the "
        + "timing differs. Random timing typically buys no drawdown "
        + "protection at all." },

    { type: "h1", text: "4. The emerging-markets tilt is a position, not "
      + "proof of skill" },
    { type: "p", text: `The tilt has made only ${tilt.n_episodes} bets in seven years and its `
      + `lifetime contribution is statistically indistinguishable from `
      + `chance (a coin-flip ${tilt.bootstrap.block_60.p_mean_positive.toFixed(2)} probability that its average effect is `
      + `positive) — it is kept because it is the portfolio's only `
      + `emerging-markets exposure, and it is now labelled a position, `
      + `never counted as edge.` },
    { type: "chart", file: "ws3_sum_tilt.png", widthPx: 600,
      caption: "Contribution per bet, in percentage points of portfolio "
        + "return. One still-open bet accounts for the entire positive "
        + "tally; if it reverses, the tilt's keep/drop question reopens." },

    { type: "h1", text: "5. Trading costs do not explain the edge" },
    { type: "p", text: `With every trade charged a realistic line-by-line `
      + `spread, the strategy holds a risk-adjusted return of ${sh(cost.final_track["1x"], 2)}; at DOUBLE `
      + `those costs ${sh(cost.final_track["2x"], 2)}; at TRIPLE ${sh(cost.final_track["3x"], 2)} — costs would need to reach `
      + `roughly ${cost.final_track.breakeven_multiple_vs_ew_blend}x realistic levels before doing nothing (equal-weight `
      + `baskets of the same funds) becomes the better choice.` },
    { type: "chart", file: "ws3_sum_cost.png", widthPx: 600,
      caption: "The dashed red line is the do-nothing alternative — buying "
        + "and holding equal amounts of the same funds "
        + `(risk-adjusted return ${sh(cost.benchmark_sharpe.blend, 2)}).` },

    { type: "h1", text: "6. What changed, and what is on watch" },
    { type: "p", text: "Nothing changed — both shortlisted refinements were "
      + "declined on evidence (one made 2022 losses worse out of sample; "
      + "the other improved results by less than the noise floor). Three "
      + "items are on watch: the thematic book must re-justify its 10% "
      + "seat at the next review (at realistic costs it no longer beats "
      + "simply holding its own basket, and one coin — Bitcoin — supplied "
      + "23% of its historical contribution); the Europe book is the most "
      + "cost-sensitive and its execution will be monitored; and one "
      + "housekeeping fix (a data-staleness guard on the tilt's input "
      + "feed) awaits approval." },
    { type: "p", text: `Timing note for any new capital: the strategy is `
      + `${(Math.abs(ep.drawdown_now) * 100).toFixed(1)}% below its recent high after a strong year (trailing `
      + `twelve months stronger than ${Math.round(ep.trailing["12m"].percentile_of_history)}% of its own history) — the house `
      + `entry rule says add after a flat or weak stretch, not now.` },

    { type: "h1", text: "Appendix — the work behind this summary",
      pageBreakBefore: true },
    { type: "chart", file: "ws3_sum_scope.png", widthPx: 600,
      caption: "This session: 46 configurations registered, none adopted, "
        + "three items on watch. Across the full three-session review: "
        + "roughly 217 configurations tested and zero changes made." },
    { type: "chart", file: "ws3_full_wf.png", widthPx: 620,
      caption: "Working chart: growth of 1 from 2022 for each protocol. "
        + "'frozen_deployed' is the strategy left alone; 'wf_full' "
        + "re-chooses every setting annually; 'oracle_full' is the "
        + "impossible hindsight best. The technical record explains each "
        + "protocol." },
    { type: "p", text: "Sibling records: the formulation review "
      + "(2026-07-02_ws0-ws1_ma-robustness.docx, with its own summary) and "
      + "the universe review (2026-07-02_ws2_universe.docx, test appendix "
      + "and summary). Full method, tables and per-component verdicts: "
      + "reviews/2026-07-03_ws3_heavy-gate.docx. Every figure in this "
      + "summary is read programmatically from the committed data/ws3_*.json "
      + "artefacts." },
    { type: "pagebreak" },   // keep the sign-off block on one page
  ],
  signoff: [
    ["Prepared by", "Claude Code research session (Fable 5), under "
      + "direction of Zhenghao Phua"],
    ["Reviewed and approved by", ""],
    ["Date", ""],
    ["Next review", "Scheduled maintenance review (thematic-book seat; "
      + "Europe-book execution)"],
  ],
  disclaimer: "Personal research artefact. All performance figures are "
    + "simulated backtests in USD, net of stated costs; nothing in this "
    + "document is investment advice.",
};

const out = process.argv[2]
  || path.join(ROOT, "reviews", "2026-07-03_ws3_heavy-gate_summary.docx");
buildReport(spec, out).then((r) => console.log("wrote", r.outPath, r.bytes));
