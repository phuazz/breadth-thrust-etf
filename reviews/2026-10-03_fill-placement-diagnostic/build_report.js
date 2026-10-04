/*
 * Technical findings record for WS23, the fill-placement diagnostic
 * (breadth-thrust-etf). Content spec for the research-review report builder;
 * every figure traces to engine/results/results_declared.json (the declared
 * cells of the held results.json, sha256 eff07b9b…) and to PREREG.md.
 * Build with: node build_report.js
 */
const path = require("path");
const { buildReport } = require("C:/Users/phuaz/.claude/skills/research-review/assets/report_builder.js");

const HERE = __dirname;
const OUT = path.join(HERE, "..", "2026-10-04_ws23_fill-placement-diagnostic.docx");

const spec = {
  meta: {
    title: "WS23 fill-placement diagnostic: do the engines' modelled rebalance-close fills give anything back after the fill?",
    subtitle: "Technical findings record. Pre-registered, red-teamed at the freeze in five passes, run once; verdict NO-GIVE-BACK.",
    dateISO: "2026-10-04",
    weekday: "Sunday",
    headerLeft: "breadth-thrust-etf: WS23 fill-placement diagnostic",
    metaLeftW: 2400,
    assetsDir: path.join(HERE, "charts"),
  },
  metaTable: [
    ["Project / context", "breadth-thrust-etf, Personal. Registration on Fable; build and run on Opus; verdict read on Fable."],
    ["Study", "Scored the same way as the discretionary book's fills (Portfolio-Command-Centre, 2026-10-03), where do the deployed engines' modelled Monday rebalance-close fills sit in their week, and do the lines bought or sold give anything back over the following three sessions? A diagnostic deciding only whether a delay-rule registration is worth writing."],
    ["Evaluation window", "7,768 confirmatory fills from the blend's inception, 2018-10-31, to 2026-07-06 (the last fill with a full 60-session forward pool), in 477 rebalance-date clusters on 58 lines; 3,986 pre-blend and 235 post-cutoff fills as disclosure cells; bars to 2026-10-02."],
    ["Data basis", "Published weekly weight vectors and overlay events at the 2026-10-03 refresh (08cb2980); unadjusted yfinance daily bars, windows rebased to the fill session's dividend factor; parity against the engines' own panels on every fill's window at 0.1 per cent, read locally, statistics only. Frozen content hashes in PREREG.md."],
    ["Method basis", "Adverse rank u and the three-session pre and post legs; forward placebo (the same fill at the close of a random session 4 to 60 bars after it), one offset per cluster per set, 10,000 sets; H-D2 floor 10 bp of price (the blend's modelled round trip), p-test power 0.883; cluster bootstrap for intervals."],
    ["Repository commits", "Freeze 4c3bf5ce (local; on origin under the capture's hash, subject 'Fill-placement diagnostic, step 7: freeze'); vault kickoff 65f47f2d pushed; step-8 record 0ef92009. Full results.json held under sha256 eff07b9b…, declared cells committed."],
    ["Running record", "PREREG.md (registration, amendments 1 to 11, build record, five red-team passes, freeze and run entries); FINDINGS.md (this verdict in the lab's note format)."],
    ["Outcome", "NO-GIVE-BACK. Mean post leg +0.08 bp against a forward placebo of +0.53 bp: effect −0.45 bp, 95% interval −6.45 to +5.66 bp, p 0.546, powered. The delay question is closed for this book at diagnostic grade; nothing changes."],
  ],
  sections: [
    { type: "h1", text: "1. Executive summary" },
    { type: "numbers", items: [
      "The verdict-bearing clause fails as a powered clause: across 7,768 modelled fills the price moved +0.08 bp of price against the book over the three sessions after the fill, where the same fill on a random later session gives +0.53 bp. Effect −0.45 bp, 95% cluster-bootstrap interval −6.45 to +5.66 bp, one-sided p 0.546; the p-test is powered at 0.883 for the registered 10 bp floor. Verdict NO-GIVE-BACK.",
      "The placement picture, descriptive by registration, is the momentum signature at a small size: mean adverse rank 0.535 against 0.500 (effect +0.035, interval +0.029 to +0.041, p 0.0001), after a move of +0.55 per cent in the trade's direction over the prior three sessions (placebo −0.00; p 0.0001), on both sides (buys u 0.572, sells 0.499 against placebos of 0.531 and 0.470).",
      "Nothing is given back on either side: buys went on rising 22 bp over three sessions and the placebo rose 18 bp; sells rose 22 bp against the book and the placebo 19 bp. By sleeve, A, B and D sit at −2 to −4 bp against the placebo; sleeve C's post leg is +28 bp (p 0.050, interval +2 to +57 bp), the one adverse disclosure cell near α among some thirty, carrying no consequence and no declared correction.",
      "Every disclosure null agrees with the verdict: the two-sided blocked null gives +0.41 bp (p 0.456), the independent null +0.44 bp (p 0.421), the notional-weighted cell +0.26 bp (p 0.478), the uniform-intraday variant −0.39 bp (p 0.541). The pre-blend fills of 2007 to 2018 show +5.1 bp (p 0.069), below the floor; the post-cutoff fills −11.6 bp (p 0.745).",
      "Robust to every drop: leave-one-line-out moves the H-D2 effect between −1.3 and +1.7 bp and leave-one-year-out between −2.6 and +2.5 bp; H-D1 stays between 0.033 and 0.037. No calendar year is adverse at p below 0.10; 2023 and 2026 are favourable.",
      "Consequence, fixed at the freeze: the delay question is closed for this book at diagnostic grade; no kickoff; the rebalance-close convention stands as WS12, WS13 and WS18 left it; no engine, parameter, published number or operating rule changes; the discretionary book's cooling-off registration is untouched.",
      "Predictions scored two of three: NO-GIVE-BACK as predicted; the sell-side u within 0.03 of placebo as predicted (+0.029); the buy-side u above placebo by more than 0.05 missed narrowly (+0.041).",
      "Read beside the discretionary book, without any claim of transfer: scored by the same engine, the owner's 2026 fills sat at u 0.639 after a 5.2 per cent move with buys giving back 2.4 points; the systematic fills sit at 0.535 after a 0.55 per cent move with nothing given back. The adverse placement filed on 2026-10-03 is not what momentum trading as such produces when scored this way.",
    ] },

    { type: "h1", text: "2. What ran: registration, amendments, guards and the run" },
    { type: "p", text: "The registration was drafted on 2026-10-03, signed off the same day at the proposed values, and amended eleven times on 2026-10-04 before any outcome existed, each amendment on a count or a data fact and recorded with its reason in PREREG.md: the parity reference read locally from the engines' own panels with statistics only committed; clusters by rebalance date alone after the same-line overlap relation chained weekly fills into one cluster holding three quarters of the sample; an economic floor of 10 bp in place of a mis-specified rounding; thinness guarding a pass only; the confirmatory set from the blend's inception, the earlier sleeve-B series a disclosure; overlay-induced rescalings included as fills; the fill-day wording corrected; the parity guard moved from price levels to the within-window ratios the statistics use; a forward-only null after the red-team showed placebos drawn from the run-up before a fill bias the comparison by about the floor on synthetic no-give-back books; the power statement separated into the p-test's power and the point-estimate branch; and the gate's initial state read from the engine, which made 2018-11-28 a real flip." },
    { type: "p", text: "Five red-team passes ran at the spec-freeze gate under a prohibition on running the engine or computing any statistic on an actual fill. They found one S1 (the selection-window placebo) and four S2 (the kickoff branch's power, the gate's initial state, verdict arithmetic untested, planted books all at one corner), all fixed before the freeze; the engine and spec hashes did not move across the last four passes and the coverage record regenerated byte-identical. The test suite holds 69 tests and 57 planted engine mutants each fail it. The freeze is the content sha256 of twelve files quoted in the registration's header and in the vault's kickoff row, pushed before the run; the run mode refuses to start on any mismatch and stops unless the forward null it draws hashes to the per-draw means recorded at coverage. The run executed once on 2026-10-04; its outputs were held unread until this session checked their hashes." },
    { type: "table",
      headers: ["Exclusion or set", "Fills", "Basis"],
      rows: [
        ["BTC-USD", "44", "Seven-day calendar; a three-session window is a different object"],
        ["159801.SZ", "25 (+4 on Chinese holidays)", "Panel held in another currency, a basis mismatch; the four fell to the fill-date guard"],
        ["Parity windows over 0.1 per cent", "26", "Within-window price ratios against the engines' panels; 11,947 of 11,973 windows agreed"],
        ["Incomplete windows", "6", "Engine's own rule"],
        ["Pre-blend (before 2018-10-31)", "3,986", "Disclosure cell: sleeve B's 2007 to 2018 series, 22 A and 12 D fills of October 2018"],
        ["Post-cutoff (no full forward pool)", "235", "Disclosure cell, two-sided null: 2026-07-13 to 2026-09-28"],
        ["Confirmatory", "7,768", "2018-10-31 to 2026-07-06; 477 clusters; 58 lines; 3,880 buys, 3,888 sells; 385 overlay-induced, 31 overlay legs"],
      ],
      widths: [3000, 1800, 4226], numericFrom: 1 },

    { type: "h1", text: "3. Findings" },
    { type: "h2", text: "3.1 The verdict cell: no give-back after the fill" },
    { type: "p", text: "Figure 1 is the registered test. The grey distribution is the mean post leg of the 7,768 fills placed at the close of random sessions 4 to 60 bars after each fill, one offset per rebalance-date cluster per set; the navy line is the actual mean and the red line the floor at placebo plus 10 bp. The actual sits inside the body of the null." },
    { type: "chart", file: "fig1_verdict_post_leg.png", widthPx: 600,
      caption: "Figure 1. The null distribution of the mean post leg (grey), the registered floor (red) and the actual mean (navy). The footer carries the sets, the floor, the MDE, the p-test power and the point-estimate branch, and the exclusions on the confirmatory basis." },
    { type: "table",
      headers: ["Cell", "n", "Actual", "Placebo", "Effect, bp", "95% interval", "p (adverse)"],
      rows: [
        ["H-D2 post leg, forward null (verdict-bearing)", "7,768", "+0.08 bp", "+0.53 bp", "−0.45", "[−6.45, +5.66]", "0.546"],
        ["Against the two-sided blocked null (disclosure)", "7,768", "+0.08", "−0.34", "+0.41", "[−5.64, +6.50]", "0.456"],
        ["Against the independent-per-fill null (disclosure)", "7,768", "+0.08", "−0.36", "+0.44", "[−5.74, +6.43]", "0.421"],
        ["Notional-weighted post leg", "7,768", "+0.74", "+0.47", "+0.26", "[−10.2, +9.9]", "0.478"],
        ["Uniform-intraday placebo variant", "7,768", "+0.08", "+0.47", "−0.39", "[−6.72, +5.57]", "0.541"],
        ["Pre-blend fills, forward null", "3,986", "+4.09", "−0.99", "+5.08", "[−1.82, +12.51]", "0.069"],
        ["Post-cutoff fills, two-sided null", "235", "−13.97", "−2.33", "−11.63", "[−34.95, +10.52]", "0.745"],
      ],
      widths: [2900, 800, 1000, 1000, 1000, 1426, 900], numericFrom: 1 },
    { type: "p", text: "Thinness: dropping any one line leaves the effect between −1.3 bp (EXH1) and +1.7 bp (SOXX); dropping any one year between −2.6 bp (2020) and +2.5 bp (2023). The sign changes, as it must around zero, and the guard applies to a pass only. The chained same-line null, degenerate at three clusters, was not computed, as registered." },

    { type: "h2", text: "3.2 Placement and the pre leg: the momentum signature at a small size" },
    { type: "p", text: "Figure 2 shows one mark per fill by sleeve. The fills sit a little past the middle of their week, on every sleeve, and above the descriptive reference floor of 0.01 in u; the overlay legs sit on the favourable side. The move before the fill is about half a per cent in the trade's direction, a tenth of what the discretionary book showed." },
    { type: "chart", file: "fig2_u_by_sleeve.png", widthPx: 600,
      caption: "Figure 2. Adverse rank u of each confirmatory fill by sleeve (navy marks), the interquartile band (grey), the sleeve's median (grey line) and its placebo mean (red line). Descriptive: a momentum rotation buys strength by construction." },
    { type: "table",
      headers: ["Cell", "n", "Actual", "Placebo", "Effect", "95% interval", "p (adverse)"],
      rows: [
        ["H-D1 mean u, forward null (descriptive)", "7,768", "0.535", "0.500", "+0.035", "[+0.029, +0.041]", "0.0001"],
        ["Pre leg, per cent of price", "7,768", "+0.552", "−0.001", "+0.554 pp", "[+0.479, +0.636]", "0.0001"],
        ["Buys: u", "3,880", "0.572", "0.531", "+0.041", "[+0.027, +0.056]", "0.0001"],
        ["Sells: u", "3,888", "0.499", "0.470", "+0.029", "[+0.013, +0.045]", "0.0001"],
        ["Buys: pre leg (bought after a rise)", "3,880", "+0.731", "+0.185", "+0.546 pp", "[+0.405, +0.681]", "0.0001"],
        ["Sells: pre leg (sold after a fall)", "3,888", "+0.374", "−0.187", "+0.561 pp", "[+0.359, +0.765]", "0.0001"],
        ["Notional-weighted u", "7,768", "0.546", "0.500", "+0.045", "[+0.034, +0.056]", "0.0001"],
      ],
      widths: [2900, 800, 1000, 1000, 1100, 1426, 800], numericFrom: 1 },

    { type: "h2", text: "3.3 By side, sleeve, year and kind" },
    { type: "chart", file: "fig3_pre_post_by_side.png", widthPx: 600,
      caption: "Figure 3. The three-session move before and after the fill by side, actual (navy) against the forward placebo (grey), with the 95% cluster-bootstrap interval on the actual. The pre leg carries the signature; the post legs match the placebo on both sides." },
    { type: "table",
      headers: ["Group", "n", "u", "Pre leg, pp", "Post-leg effect, bp", "Interval", "p (adverse)"],
      rows: [
        ["Buys", "3,880", "0.572", "+0.73", "−4.1", "[−18.6, +9.9]", "0.689"],
        ["Sells", "3,888", "0.499", "+0.37", "+3.2", "[−13.3, +19.6]", "0.365"],
        ["A, US sectors (proxies)", "2,947", "0.525", "+0.40", "−4.2", "[−12.3, +3.8]", "0.838"],
        ["B, asset classes", "2,816", "0.545", "+0.58", "−1.8", "[−9.1, +6.0]", "0.652"],
        ["C, thematics", "637", "0.573", "+1.56", "+28.4", "[+2.0, +57.3]", "0.050"],
        ["D, Europe sectors (EUR)", "1,332", "0.522", "+0.37", "−2.5", "[−14.2, +8.8]", "0.671"],
        ["Gate leg, SHY", "20", "0.421", "−0.03", "−5.5", "[−22.4, +8.5]", "0.903"],
        ["Tilt leg, EEM", "16", "0.540", "+0.23", "−60.6", "[−152.1, +32.4]", "0.891"],
        ["Overlay-induced rescalings", "385", "0.623", "+2.31", "+2.0", "[−56.5, +66.0]", "0.498"],
      ],
      widths: [2600, 800, 800, 1100, 1300, 1526, 900], numericFrom: 1 },
    { type: "p", text: "By calendar year the post-leg effect runs from −25 bp (2026) and −18 bp (2023, interval clear of zero on the favourable side) to +13 bp (2020, p 0.19) and +9 bp (2018 and 2021); no year is adverse at p below 0.10. Sleeve C's post leg is the one adverse disclosure cell near α among some thirty; it carries no consequence under the registration, no multiplicity correction was declared for disclosures, and if a thematic-sleeve question is ever opened it is the prior for a registration on data not used here." },

    { type: "h2", text: "3.4 The three ways this could have been silently wrong" },
    { type: "table",
      headers: ["Mechanism", "Guard", "Outcome"],
      rows: [
        ["A basis mismatch between the fetched bars and the engines' prices", "Within-window price ratios against the engines' own panels on every fill at 0.1 per cent, read locally; the dividend factor compared on the reproduction month", "11,947 of 11,973 windows agree; 26 excluded; 159801.SZ excluded whole; worst kept ratio 8.5e-4"],
        ["A fill dated on a session the engine did not fill on", "Dates read from the engines' output; every fill date a bar in its series and panel; a fixture that fails first on a shifted date", "Four 159801.SZ fills on Chinese holidays excluded; none re-dated"],
        ["A null that scores clustered fills as independent, or draws placebos from the run-up the signal reacts to", "One offset per rebalance-date cluster; forward offsets only; the two-sided, independent and chained nulls as disclosures", "All disclosure nulls agree with the verdict within 1 bp; the chained null degenerate and not computed"],
      ],
      widths: [3000, 3300, 2726] },

    { type: "h2", text: "3.5 Predictions, scored" },
    { type: "table",
      headers: ["Prediction, registered before the run", "Stated probability", "Outcome"],
      rows: [
        ["P1: buy-side mean u above the placebo by more than 0.05", "70%", "Wrong, narrowly: +0.041 on the registered null"],
        ["P2: H-D2 reads NO-GIVE-BACK", "60%", "Right"],
        ["P3: sell-side mean u within 0.03 of the placebo", "55%", "Right, narrowly: +0.029"],
      ],
      widths: [5026, 1600, 2400] },

    { type: "h1", text: "4. Decisions" },
    { type: "table",
      headers: ["Component", "Decision", "Basis"],
      rows: [
        ["Delay-rule registration for this book", "NOT WRITTEN; question closed at diagnostic grade", "H-D2 fails as a powered clause: −0.45 bp, p 0.546, power 0.883 at 10 bp"],
        ["Rebalance-close fill convention", "STANDS", "As WS12, WS13 and WS18 left it; nothing here measured its P&L"],
        ["Engines, parameters, published numbers, operating rules", "No change", "Fixed at the freeze"],
        ["Discretionary book's cooling-off registration (PCC)", "Untouched", "No transfer claimed in either direction"],
        ["Sleeve C post leg (+28 bp, p 0.050)", "Noted as a prior only", "One disclosure cell among thirty; no consequence under the registration"],
        ["Full run outputs", "Held under recorded hashes; declared cells committed", "results.json is 8.0 MB; the capture publishes main"],
      ],
      widths: [2800, 2600, 3626] },

    { type: "h1", text: "5. Trial register" },
    { type: "p", text: "Two clauses, one verdict-bearing (H-D2); fifteen declared cells including the disclosures; one run on the full published history, which is SEEN for the fill-convention family (WS12, WS13, WS18) and was read here once for a different statistic. The registration carries eleven pre-freeze amendments, each on a count or a data fact, and no outcome was computed before the freeze. Thirteen PCC cells not declared here were disabled in the engine copy." },

    { type: "h1", text: "6. Artefact register" },
    { type: "bullets", items: [
      "reviews/2026-10-03_fill-placement-diagnostic/PREREG.md: registration, owner decisions, amendments 1 to 11, build record, five red-team passes, freeze and run entries with the twelve frozen hashes and the held outputs' hashes.",
      "reviews/2026-10-03_fill-placement-diagnostic/FINDINGS.md: the verdict in the lab's note format.",
      "engine/fill_timing.py (sha256 49fdbe11…), engine/prereg_spec.json (07b4de2b…), engine/charts.py, step8_run.py, freeze_header.py, mutation_check.py; scripts/ws_fill_placement_adapter.py; tests/test_ws_fill_placement.py (69 tests).",
      "engine/results/: fills.json, book_meta.json, bars_used.json, coverage.json (frozen inputs and record); results_declared.json (the declared cells of the run, this read's source); charts/fig1 to fig3 and render_check.json.",
      "Held, uncommitted, under recorded hashes: data_local/ws_fill_placement/held_outputs/results.json (eff07b9b…) and run_stdout.log.",
      "Vault: kickoff row and register entry (65f47f2d); verdict row and records 2026-10-04-breadth-thrust-etf-1 and -2 filed with this record.",
    ] },

    { type: "h1", text: "7. Next phase" },
    { type: "p", text: "None. The question is closed for this book at diagnostic grade. The one open flag is procedural: the registration fixes no consequence for INCONCLUSIVE, SUGGESTIVE, UNRESOLVED or INFEASIBLE, which did not arise and would need stating in any successor." },
  ],
  signoff: [
    ["Prepared by", "Claude Code research session (Fable 5.1), under direction of Zhenghao Phua"],
    ["Reviewed and approved by", ""],
    ["Date", ""],
    ["Next review", "None scheduled"],
  ],
  disclaimer: "Personal research artefact on modelled fills of a published model portfolio. Figures are placements against a simulated zero-skill placebo, not realised profit or loss; nothing here is investment advice.",
};

buildReport(spec, OUT).then((r) => console.log("wrote", r.outPath, r.bytes, "bytes"));
