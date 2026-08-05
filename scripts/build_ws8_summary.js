/*
 * Plain-language summary of the 2026-08-05 universe questions.
 * Companion to reviews/2026-08-05_ws8_reit-dual-coverage.docx.
 *
 * Audience: a reader who wants to know what was asked, what was found and
 * what changed — without the method. Every number here traces to the
 * technical record; nothing new is computed. Charts from
 * scripts/plot_ws8_summary.py.
 *
 * Run:  node scripts/build_ws8_summary.js
 */
const path = require("path");
const { buildReport } = require("C:/Users/phuaz/.claude/skills/research-review/assets/report_builder.js");

const ASSETS = path.resolve(__dirname, "..", "reviews", "assets");
const OUT = path.resolve(__dirname, "..", "reviews", "2026-08-05_ws8_universe-questions_summary.docx");

const spec = {
  meta: {
    title: "Three questions about what the portfolio owns",
    subtitle: "Plain-language summary — why oil and gas is absent, why energy is not, whether REITs are counted twice, and whether anything watches for new funds",
    dateISO: "2026-08-05",
    weekday: "Wednesday",
    headerLeft: "Universe questions — plain-language summary",
    headerRight: "Nothing changed; two things fixed",
    metaLeftW: 2400,
    assetsDir: ASSETS,
  },
  metaTable: [
    ["The questions", "Why is oil and natural gas not in the asset-class book? Why are energy funds not in the thematic book? Why do property funds appear in two books at once — should they be in only one? Should something scan the market each month for newly launched funds?"],
    ["The answers", "Oil and gas was tested and made things worse. Energy is not missing — it is the largest holding in the portfolio, in two other books. Property is deliberately held twice, and testing now confirms removing either copy costs more than it saves. Nothing watched for new funds; something does now, monthly."],
    ["How it was tested", "Two ways of removing a property holding were written down in advance, then run through the same simulated history the portfolio uses, and judged on the out-of-sample half — the later years the rules were never tuned on"],
    ["What changed in the portfolio", "Nothing. No holding was added, removed or resized"],
    ["What changed in the tooling", "The check that is meant to stop the portfolio buying the same thing twice was not working. It has been repaired, and a monthly scan for newly launched funds now runs"],
    ["The full detail", "reviews/2026-08-05_ws8_reit-dual-coverage.docx"],
  ],
  sections: [
    { type: "callout", text: "The short version: the portfolio was already right on all three counts, and the thing worth fixing was not any of them — it was the safety check that was supposed to catch this sort of problem, which turned out not to be working." },

    // ------------------------------------------------------------------
    { type: "h1", text: "Question 1 — why is oil and natural gas not in the asset-class book?" },
    { type: "h2", text: "Because it was tried, and every version of it made the portfolio worse." },
    { type: "p", text: "The book already holds a broad commodity fund; adding oil and gas on top was tested at seven different levels of detail, over nineteen years of history, and every single one lost ground." },
    { type: "chart", file: "ws8_fig4_commodity.png",
      caption: "Each bar is one way of adding commodity exposure to the asset-class book. The measure is return earned per unit of the worst loss along the way — higher is better, so every bar pointing left is a version that made the portfolio worse. Buying oil and natural gas funds directly (red) was the worst of the seven, and it also deepened the worst loss from 16% to 27%." },
    { type: "p", runs: [
      { text: "There is a reason, not just a result. ", bold: true },
      { text: "Funds that hold oil or natural gas directly do not hold the commodity — they hold contracts that expire and must be replaced every month, usually at a worse price. That rolling cost drags the fund's price down over time even when the commodity itself is flat. The portfolio's rule buys things that are trending up and sells things trending down, so it reads that steady, structural drag as a genuine downtrend. It is a mismatch between the instrument and the rule, not a matter of picking a better moment." }] },

    // ------------------------------------------------------------------
    { type: "h1", text: "Question 2 — why are energy funds not in the thematic book?" },
    { type: "h2", text: "Because energy is already the single largest position in the portfolio, held in two other books." },
    { type: "p", text: "At the most recent rebalance the portfolio held 18.1% of its value in energy shares — 11.2% in US energy and 6.9% in European oil and gas — plus a broad commodity fund on top; energy is the biggest thing the portfolio owns." },
    { type: "callout", text: "The thematic book deliberately holds the OTHER side of energy — solar, uranium, lithium, clean power. Traditional oil and gas funds were formally tested for it and turned down, precisely because they duplicated the US energy holding the portfolio already had. That is the duplication check working exactly as intended, which makes what Question 3 uncovered all the more surprising." },

    // ------------------------------------------------------------------
    { type: "h1", text: "Question 3 — are property funds wrongly counted twice?" },
    { type: "h2", text: "They are held twice on purpose — and it is a smaller overlap than two others already accepted." },
    { type: "p", text: "The two property holdings move almost identically, which is what makes the question a fair one, but they are a smaller share of the portfolio than the two overlapping US share holdings that a previous review examined and kept." },
    { type: "chart", file: "ws8_fig2_lookthrough_plain.png",
      caption: "Four holdings that the portfolio reaches through two separate books at once. The left panel is how much of the portfolio each one accounts for on average; the right is how often both books held it simultaneously. Property (red) is a smaller overlap than the two large US share holdings above it on both measures — so if the property pair is a problem, those are a bigger one, and a previous review looked at them and kept them." },
    { type: "p", runs: [
      { text: "The 13% figure that prompted the question is unusually high, not typical. ", bold: true },
      { text: "On the most recent rebalance the two property holdings together came to 13.1% of the portfolio. The long-run average is 3.6%, and the highest it has ever been is 20.3%. So the reading that raised the question was real and near the top of its range, but it is not where the portfolio normally sits." }] },

    { type: "h2", text: "Removing either copy was tested, and both made the portfolio slightly worse." },
    { type: "p", text: "Two removals were written down in advance — take property out of one book, then out of the other — and both failed on the later years of history that the rules were never tuned on." },
    { type: "chart", file: "ws8_fig1_ablation_plain.png",
      caption: "Left: what happens to risk-adjusted return — return earned for each unit of bumpiness, higher is better — when each property holding is removed. Both bars for the out-of-sample years (teal) point down, which is the test being failed. Right: the same four results drawn against the margin of error for a history this short. The differences are real enough to decide by, but small enough that the honest conclusion is that it does not matter much either way — which is the argument for leaving a working portfolio alone." },
    { type: "p", runs: [
      { text: "One thing to keep an eye on. ", bold: true },
      { text: "Because two separate books can both buy property at the same time, a strong stretch for property can put a fifth of the portfolio into it. That has happened before, at 20.3%. It is not a fault, but it is worth knowing that it can happen." }] },

    // ------------------------------------------------------------------
    { type: "h1", text: "The finding nobody asked for" },
    { type: "h2", text: "The check meant to stop the portfolio buying the same thing twice was not doing its job." },
    { type: "p", text: "Answering Question 3 meant looking at the duplication check itself, and it had three faults — one of which would have silently approved anything." },
    { type: "bullets", items: [
      [{ text: "It was measuring the wrong thing. ", bold: true },
       { text: "The pass mark was set for one kind of measurement and applied to another, which made it easier to pass than intended." }],
      [{ text: "It only looked at part of the portfolio. ", bold: true },
       { text: "New funds proposed for the thematic book were checked against the rest of the portfolio, as they should be. New funds for the asset-class book were checked only against that book — so a duplicate of something held elsewhere could walk straight in. That is exactly how the property overlap arose." }],
      [{ text: "It could quietly approve everything. ", bold: true },
       { text: "The portfolio holds funds trading in New York, Frankfurt, Shenzhen and around the clock in crypto. Their different holiday calendars broke the calculation in a way that produced no error and no warning — just an empty result, which the check read as \"no similarity found\" and passed. A broken check that says yes looks identical to a working check that says yes." }],
    ] },
    { type: "p", runs: [
      { text: "All three are fixed, and the repair was checked against past decisions. ", bold: true },
      { text: "The rebuilt check reproduces the conclusions of four earlier reviews to within a thousandth. Re-tested against today's portfolio, it now correctly flags the property fund it previously waved through." }] },

    { type: "h2", text: "Running the rule backwards over what is already owned found two overlaps nobody had examined." },
    { type: "p", text: "The rule had only ever been applied to new proposals, never to holdings already in the portfolio, so a sweep of the existing holdings was the first time the question had been asked of them." },
    { type: "chart", file: "ws8_fig3_audit_plain.png",
      caption: "Every pair of holdings that moves closely enough to trip the rule. Grey pairs are a quirk of measurement, not real duplication — the portfolio's European-listed funds are priced using their US equivalents, so those pairs are effectively being compared with themselves. Navy pairs have all been examined by an earlier review. Only the two red pairs — a European shares holding against a European industrials holding — have never been looked at, and they are now on the list to examine." },

    // ------------------------------------------------------------------
    { type: "h1", text: "Question 4 — should something watch for newly launched funds?" },
    { type: "h2", text: "Yes, nothing did, and now something does — monthly." },
    { type: "p", text: "A monthly job now compares the full list of US-listed funds against the previous month, screens anything new against everything the portfolio already owns, and reports funds that have closed." },
    { type: "bullets", items: [
      [{ text: "It cannot buy anything. ", bold: true },
       { text: "It produces a report and a watchlist. Any actual change to the portfolio still requires a full test of the kind described above." }],
      [{ text: "A brand-new fund cannot qualify for five years anyway. ", bold: true },
       { text: "The testing method needs five years of history, so the realistic output is a watchlist with a date against each name, not a buy list." }],
      [{ text: "Funds closing matters as much as funds launching. ", bold: true },
       { text: "If a fund the portfolio actually holds stops trading, the job raises an alert by email. A frontier-markets fund was once caught this way, but only after it had already closed." }],
      [{ text: "It is built so that a broken data feed cannot look like a quiet month. ", bold: true },
       { text: "A stale or truncated list of funds would report \"nothing new\" and look perfectly healthy. Three checks stop the job rather than let that happen — and they immediately caught a genuine bug in the job's own first run." }],
      [{ text: "One known limitation. ", bold: true },
       { text: "The free data source lists every fund but not how much money each holds, so the screen cannot yet rank new funds by size. Fixing that means paying for a data feed — a decision left open." }],
    ] },

    // ------------------------------------------------------------------
    { type: "h1", text: "What this cost and what it changed", pageBreakBefore: true },
    { type: "p", text: "Twenty-four separate checks were run across the portfolio. Not one of them resulted in a change to what the portfolio holds." },
    { type: "chart", file: "ws8_fig5_scope.png",
      caption: "The work behind this summary. Every check either confirmed the portfolio was already right or was recorded for later; none produced a change to the holdings. The two flagged items are the pair of European overlaps marked in red in the duplication chart under Question 3." },
    { type: "h2", text: "The three things worth remembering" },
    { type: "numbers", items: [
      [{ text: "The portfolio was right on all three questions. ", bold: true },
       { text: "Oil and gas is absent because it was tested and failed. Energy is not absent at all — it is the largest holding. Property is held twice deliberately, and now with evidence rather than only an assertion." }],
      [{ text: "The safety check was the actual problem. ", bold: true },
       { text: "The check designed to prevent duplicate holdings had been applying the wrong standard, to only part of the portfolio, in a way that could silently approve anything. It has been repaired and verified against past decisions." }],
      [{ text: "A rule that only screens new arrivals never examines what is already there. ", bold: true },
       { text: "That is why a near-duplicate pair sat unexamined without anything appearing to be broken. Running the rule backwards over existing holdings is now possible, and is part of the monthly job." }],
    ] },
    { type: "p", runs: [
      { text: "A note on how firm these conclusions are. ", bold: true },
      { text: "The portfolio has about seven and a half years of simulated history. That is enough to decide between clearly different options, and not enough to split hairs. The property test came out clearly in one direction, but the size of the difference is smaller than the uncertainty in the measurement itself — so the fair reading is \"no reason to change\", rather than \"proved best\"." }] },
  ],
  signoff: [
    ["Prepared by", "Claude Code (Opus 5), on instruction"],
    ["Reviewed and approved by", "Zhenghao Phua — pending"],
    ["Date", "2026-08-05 (Wednesday)"],
    ["Full technical record", "reviews/2026-08-05_ws8_reit-dual-coverage.docx"],
  ],
  disclaimer: "Personal research artefact. Not investment advice, not affiliated with any regulated fund, and not a representation of any managed product. Every return figure quoted here is simulated on historical data; there is no live track record. Figures in this summary are drawn from the technical record and are not recomputed here.",
};

buildReport(spec, OUT).then((r) => console.log("wrote", r.outPath, r.bytes));
