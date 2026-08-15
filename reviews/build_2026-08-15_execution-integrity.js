// Content spec for the 2026-08-15 execution-integrity record.
// Build:  node reviews/build_2026-08-15_execution-integrity.js
const path = require('path');

module.exports = {
  meta: {
    title: 'Execution integrity — the Friday cadence meets the vendor',
    subtitle: 'Correction and defect record · breadth-thrust-etf · Personal',
    dateISO: '2026-08-15',
    weekday: 'Saturday',
    headerLeft: 'breadth-thrust-etf',
    headerRight: 'Execution integrity — 2026-08-15',
    assetsDir: path.join(__dirname, 'assets'),
    metaLeftW: 2600,
  },

  metaTable: [
    ['Project / context', 'breadth-thrust-etf · Personal · not investment advice'],
    ['Review scope', 'Whether the Friday-morning cadence adopted by WS12/WS13 can be executed as specified. Five defects found and fixed; one open cadence question placed under measurement.'],
    ['Classification', 'CORRECTION AND DEFECT RECORD — nothing pre-registered. One testable claim inside it (panel tail extension is value-preserving) was tested with a control.'],
    ['Builds on', 'WS12/WS13 execution timing (ledger 2026-08-12), which adopted the cadence; WS16 page anchoring (2026-08-13); implementation audit (2026-07-04), which found execution timing CLEAN.'],
    ['Evaluation window', 'Live data 2026-08-12 to 2026-08-15; value-preservation control on full panel history 2018-01-05 to 2026-08-13.'],
    ['Data basis', 'yfinance daily closes (auto-adjusted); iShares point-in-time rosters; NYSE and XETR calendars via pandas_market_calendars.'],
    ['Method basis', 'Defect reproduction against live vendor responses; exact-equality control with the price frame pinned; 320-timestamp cross-check on the session helpers.'],
    ['Repository commits', '466646b, 9c03cbb, e710bc5, ea884c7, 1237546, c090f76, b4bfd13, 79a7b2c, 5225b59'],
    ['Running memo', 'RESEARCH_MEMO.md — "Execution integrity — the Friday cadence meets the vendor (2026-08-14/15)"'],
    ['Outcome', 'Five defects fixed; deployed convention UNCHANGED (rank Thursday, fill Friday close). Nothing was traded on the defective signal. Cadence viability now under measurement, not decided.'],
  ],

  sections: [
    { type: 'h1', text: 'Executive summary' },
    { type: 'numbers', items: [
      'A Strategy D rebalance dated 14 August — EXH3 (traded EXH4.DE) out, EXV3 in, 5.9 per cent of NAV — was decided on the WRONG SESSION and reverses on the right one: Wednesday EXV3 73.6 against EXH3 71.6, Thursday EXH3 73.0 against EXV3 71.7. Two compounding causes: a bar stamped 14 August served while Xetra was two hours from its close, and Thursday 13 August absent from the .DE series, so the engine fell back to Wednesday silently. Caught in preparation; nothing traded.',
      'The partial-bar guard already existed. Sleeves B and C called it; A and D did not, both served by run_portfolio — and D could not have, it being NYSE-only. The sleeve that broke is the one the guard cannot express.',
      'The breadth panel was bounded by the last published ROSTER Friday rather than the last completed session, costing four sessions weekly. The guard for it had never executed: the validating run used preflight_only, which skips the check.',
      'The publish guard was INVERTED — it permitted a five-session-stale panel and refused a one-session-fresh one. The committed data carries the passing case.',
      'The tail extension is VALUE-PRESERVING (exact equality, IUUS/EXH9/CSP1, price frame pinned). Two earlier controls passed it for the wrong reason and are recorded so the error is not repeated.',
      'Deployed convention UNCHANGED. Whether the Xetra lag makes the Friday cadence unworkable for sleeve D is OPEN, now sampled four times daily, and the present evidence is explicitly too thin to move a rebalance day.',
    ]},

    { type: 'h1', text: 'The defect, in one exhibit' },
    { type: 'p', text: 'Strategy D holds the top three of five European sector funds by constituent breadth. The vendor hole moved the ranking session back one day, and across that single day ranks three and four exchange places. Nothing else in the sleeve moves.' },
    { type: 'chart', file: '2026-08-15_decision_session_reversal.png',
      caption: 'The number inside each bar is that day\u2019s rank; a bold rank is one of the three the sleeve holds. EXV3 is held on Wednesday and dropped on Thursday, EXH4 the reverse. The margin is 1.3 percentage points, which is why a corrupted input decides it: on a wide margin the wrong session would still have produced the right book.' },
    { type: 'callout', text: 'A narrow margin is not a reason to relax about a data defect. It is the reason the defect matters — a wrong input changes the outcome only where the decision was close, which is precisely where it is least visible afterwards.' },

    { type: 'h1', text: 'Verified architecture, and where the written record had drifted' },
    { type: 'p', text: 'Three statements in the repository were wrong against the code, and each had been read as settled.' },
    { type: 'table',
      headers: ['Written record', 'What the code does', 'Consequence'],
      widths: [2800, 3200, 3026],
      rows: [
        ['cap_to_last_completed_session guards the weekly engines against partial bars', 'It guards sleeves B and C. Sleeves A and D, both served by run_portfolio, never called it', 'Strategy D ingested a live intraday quote as a close'],
        ['The refresh is "operator-run, not scheduled" (ledger row 2026-08-12)', 'It has been Windows scheduled task BreadthThrust-WeeklyRefresh since 2026-08-12', 'Stale in the ledger; corrected in the row filed with this record'],
        ['The re-timed anchor guard was validated by the 2026-08-12 run', 'That run used preflight_only=True, under which the anchor check is explicitly skipped', 'The guard had never executed; the panel bound it would have caught survived'],
      ]},

    { type: 'h1', text: 'Findings' },

    { type: 'h2', text: 'F1 — The partial-bar guard existed and could not reach the sleeve that needed it' },
    { type: 'p', text: 'The docstring on cap_to_last_completed_session names this failure exactly: "a weekly engine could stamp a rebalance on it". The guard is NYSE-only, which is not an oversight in isolation — sleeves B and C are US — but it means the European sleeve had no equivalent and no way to obtain one. On a US-holiday Friday an NYSE cutoff would also truncate a completed European session, so the fix had to be venue-aware rather than merely applied more widely.' },
    { type: 'table',
      headers: ['Sleeve', 'Venue', 'Guard before', 'Guard after'],
      widths: [1500, 1500, 3013, 3013],
      rows: [
        ['A', 'NYSE', 'None (served by run_portfolio)', 'session_bounds.trim_to_completed, venue-aware'],
        ['B', 'NYSE', 'cap_to_last_completed_session', 'Unchanged; wrapper now delegates to the shared implementation'],
        ['C', 'NYSE', 'cap_to_last_completed_session', 'Unchanged; wrapper now delegates to the shared implementation'],
        ['D', 'XETR', 'None, and none available — the guard cannot express a non-US venue', 'session_bounds.trim_to_completed on the XETR calendar'],
      ]},
    { type: 'p', text: 'The two implementations were cross-checked at 320 timestamps spanning 40 days before the wrapper was made to delegate; they agreed at every one, so unifying them could not change behaviour.' },

    { type: 'h2', text: 'F2 — A roster bound was being used as a computation bound' },
    { type: 'p', text: 'compute_breadth ended its daily loop at end_friday, the last published roster Friday. That is a fact about when the constituent LIST refreshed. It was serving as a bound on how far breadth could be computed, which is a different question. On 14 August the newest roster was 7 August, so the panel ended 7 August while the decision that morning reads 13 August — the wrapper anchor guard demanded data the pipeline declined to produce.' },
    { type: 'p', text: 'The roster being a week old is not a defect and never was: rosters publish weekly, so every mid-week day already resolves against the most recent snapshot at or before T. Thursday 13 August is an ordinary mid-week day under the 7 August roster, exactly as Wednesday 12 August is.' },

    { type: 'h2', text: 'F3 — The tail extension is value-preserving, and two controls said so for the wrong reason' },
    { type: 'p', text: 'The claim under test: extending the panel produces the identical numbers earlier, because next week\u2019s run computes 13 August against the 7 August roster too — 14 August is not at or before 13 August. This is the one part of the week\u2019s work that is a test rather than a repair, and it did not pass first time.' },
    { type: 'table',
      headers: ['Control', 'Result', 'Why it did not answer the question'],
      widths: [2300, 2100, 4626],
      rows: [
        ['Wide bound then narrow bound, live download', 'FAIL — 2 discrete highs_breadth values moved in 2022, ~1,100 z-scores downstream', 'Valid failure signal, but the two runs saw different price frames, so the cause was unattributable'],
        ['Old bound twice', 'PASS', 'VACUOUS. Both runs hit a cache an earlier wide run had left, so the download asymmetry under investigation never occurred'],
        ['Old bound twice, cache deleted', 'PASS', 'VACUOUS. Run one then had no prior to compare against, which disarms _revert_vendor_step_defects — a guard that can revert an entire column. Whether it fires is itself a function of the download'],
        ['One price frame pinned for both runs', 'PASS — bit-identical', 'The only clean isolation. Removes the download from the experiment, leaving the schedule bound alone'],
      ]},
    { type: 'p', text: 'Under the pinned control, IUUS, EXH9 and CSP1 are bit-identical on every shared date — both venues and the widest universe — gaining four days each with signals unchanged on the shared window.' },
    { type: 'callout', text: 'A control must hold the confounder STILL, not merely repeat the procedure. Both vacuous controls repeated the run faithfully and neither reproduced the asymmetry they were meant to rule out. A passing control is evidence only once you can say what it held constant.' },

    { type: 'h2', text: 'F4 — A separate defect surfaced by the failed control, not fixed here' },
    { type: 'p', text: 'The first IUUS result is real on its own terms: recomputing a panel against a re-fetched price frame can move historical breadth values. That is pre-existing behaviour, was not introduced by this work, and is not repaired by it. It is recorded here so it is not rediscovered as a symptom of the tail extension.' },

    { type: 'h2', text: 'F5 — The publish guard permitted the dangerous direction' },
    { type: 'p', text: 'assert_payload_usable required as_of == panel_end_date. Because as_of is min(panel_end, live_anchor) and the page trims every displayed series to it, the one-date contract already held in both directions; the equality added nothing to it. What it tested in practice was that the panel is not FRESHER than the NAV curve — true only when the panel is the staler of the two.' },
    { type: 'table',
      headers: ['Case', 'as_of', 'Old guard', 'Corrected guard'],
      widths: [3400, 1600, 2013, 2013],
      rows: [
        ['Panel 2026-08-07, curve 2026-08-12 (committed state)', '2026-08-07', 'PASS — published a regime headline five sessions stale', 'FAIL — panel behind the book'],
        ['Panel 2026-08-13, curve 2026-08-12 (routine, post-fix)', '2026-08-12', 'FAIL — unpublishable', 'PASS — one-session lead, recorded'],
        ['Panel far ahead of the curve', 'curve date', 'FAIL', 'FAIL — beyond five sessions the pair is broken, not out of step'],
      ]},
    { type: 'p', text: 'The benign direction is now routine rather than exceptional: the Xetra lines publish about a session late and Strategy D\u2019s price line caps the blend, so the NAV curve trails the US breadth panel most weeks. Under the old assertion that is unpublishable as the normal state. The failure mode the old guard permitted is the same shape as the one that left the 2026-03-27 de-risk invisible for eleven weeks.' },

    { type: 'h1', text: 'Decisions' },
    { type: 'table',
      headers: ['Component', 'Decision', 'Basis'],
      widths: [2500, 1600, 4926],
      rows: [
        ['Rank Thursday, fill Friday close (MOC)', 'KEEP', 'Unchanged. It is what the engines backtest (get_loc(rd)-1) and what WS12/WS13 settled. Nothing here reopens it'],
        ['Venue-aware partial-bar trim in all four sleeves', 'ADOPTED', 'F1. Historically a no-op — every bar in a finished session is complete — so it removes a tail and cannot move a backtest'],
        ['Panel runs to the last completed session', 'ADOPTED', 'F2, F3. Value-preserving under the pinned control; both bounds take max() against the old value so the window can only lengthen'],
        ['decision_date recorded in every trade', 'ADOPTED', 'F1. All four engines computed it and discarded it, which is why the substitution was unreadable from the output'],
        ['Book assembled at or before the as-of date', 'ADOPTED', 'Mixed-vintage defect. Divergence between sleeves is reported, not refused — sleeve C legitimately lags, trading only when its basket changes'],
        ['Publish guard bounded lead, hard-fail on a stale panel', 'ADOPTED', 'F5. Not a weakening: it refuses the direction the old assertion permitted'],
        ['live_targets.py as the Friday-morning artefact', 'ADOPTED', 'Engines emit a rebalance only where an execution bar exists, which is right for a backtest and useless before the fill'],
        ['Move sleeve D to a separate rebalance day', 'FLAGGED, NOT DECIDED', 'Requires two to three weeks of decision-hour samples. Current evidence is a handful of observations'],
        ['Historical breadth moving on a re-fetch', 'FLAGGED', 'F4. Pre-existing, out of scope here, needs its own study'],
      ]},

    { type: 'h1', text: 'Trial register' },
    { type: 'p', text: 'No parameter search was run and no configuration was selected on a result, so no deflated-Sharpe haircut applies. For completeness, the configurations evaluated were: 4 value-preservation controls (three vacuous or failing, one clean) across 3 ETFs; 2 session-helper implementations cross-checked at 320 timestamps; 3 publish-guard cases; and 7 probe lines across 2 venues at 2 sampling times. The deployed configuration was not varied at any point — this record changes machinery, not strategy parameters.' },

    { type: 'h1', text: 'The open question, and why it is not answered here' },
    { type: 'p', text: 'The Xetra price lines appear to publish about a session late. If systematic, sleeve D is structurally short on a Friday decision rather than occasionally short, which is a materially stronger objection to the cadence than the historical sweep suggests — 514 of 516 Xetra sessions are served in history, because history backfills. Only the live edge lags.' },
    { type: 'callout', text: 'An earlier statement in this work — that sleeve D\u2019s signal "was never late" and only the ETF wrapper lagged — was one observation taken on a Friday afternoon and does not generalise. The first probe sample has the European CONSTITUENTS one session behind as well, against zero for the US proxies. What survives is narrower: the constituents lead the wrapper by roughly a session, but both trail the live session. Whether the constituents carry Thursday at 08:00 SGT on a Friday, the only moment the question turns on, is UNMEASURED.' },
    { type: 'p', text: 'probe_vendor_availability.py samples it four times daily at 00, 06, 12 and 18 UTC, the 00:00 slot being the decision hour itself. It is deliberately separate from the holdings-publication probe: different series, never to be pooled, and folding them together would have re-timed a log whose sampling already changed once. Its guard fails the job when nothing was appended, when the newest row is not from that run, or when every line came back empty; a partial result passes, because one venue answering while another does not is the asymmetry being measured.' },

    { type: 'h1', text: 'Artefact register' },
    { type: 'table',
      headers: ['Artefact', 'Path'],
      widths: [3400, 5626],
      rows: [
        ['Venue-aware session bounds', 'scripts/session_bounds.py'],
        ['Friday-morning target book', 'scripts/live_targets.py'],
        ['Vendor availability probe', 'scripts/probe_vendor_availability.py'],
        ['Probe guard', 'scripts/check_vendor_probe.py'],
        ['Roster integrity guard', 'scripts/check_roster_integrity.py'],
        ['Value-preservation control', 'tools/verify_tail_extension.py'],
        ['Chart script', 'scripts/plot_execution_integrity.py'],
        ['Probe schedule', '.github/workflows/vendor_probe.yml'],
        ['Fleet watch rows', 'C:/dev/scripts/fleet_watch.json — "breadth-etf vendor probe", "breadth-etf vendor probe build"'],
        ['Tests', 'tests/test_session_bounds.py (19), tests/test_panel_tail_bound.py (12), tests/test_live_targets.py (14)'],
        ['Running memo', 'RESEARCH_MEMO.md'],
        ['Observation log', 'data/vendor_availability_log.jsonl'],
      ]},
    { type: 'p', text: 'Suite at filing: 1,239 passed, 2 skipped, from 1,154 at the start of the week.' },

    { type: 'h1', text: 'Next phase' },
    { type: 'bullets', items: [
      'Collect two to three weeks of decision-hour probe samples, then answer the Friday-versus-Monday question on that evidence. Do not move a rebalance day before it.',
      'Open a separate study on historical breadth moving when a panel is recomputed against a re-fetched price frame (F4).',
      'IJPN breadth remains unwritten after its roster repair — the thin-breadth floor refused a panel with zero roster coverage. Research universe only, not deployed, and unchased.',
      'Confirm Xetra market-on-close support in the broker before the next European rebalance; the sleeve is priced on a 9 bps one-way assumption whose break-even is superseded.',
    ]},
  ],

  signoff: [
    ['Prepared by', 'Claude Opus 5 (Claude Code), under ZH direction'],
    ['Reviewed and approved by', '________________________  (Zhenghao)'],
    ['Date', '2026-08-15 (Saturday)'],
    ['Next review', 'On two to three weeks of decision-hour probe samples'],
  ],

  disclaimer: 'Personal research artefact. Not investment advice, not a solicitation, and not affiliated with any regulated fund. All performance figures are simulated; there is no live track record. Figures quoted here trace to RESEARCH_MEMO.md and the repository commits listed above.',
};
