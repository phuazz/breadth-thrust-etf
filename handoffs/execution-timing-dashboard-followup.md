# Execution Timing dashboard follow-up

Working directory: `C:\dev\breadth-thrust-etf`.
Scope: presentation correction following the pre-trade and README repairs.
Review seat: existing Claude Opus session, standard effort, read-only.
Status: verified; the user approved publication of the timing fix only, leaving
the existing light-only theme and longer prose elsewhere unchanged.
Publication checkout: `C:\dev\breadth-thrust-etf\logs\publish-execution-timing`,
branch `codex/publish-execution-timing`, based directly on `origin/main`.
Only source commit `7902444` was cherry-picked. The pre-existing holdings-capture
commit `99da647` is excluded and remains in the original local checkout.
The original working copy and its untracked Word review are preserved; do not
push its local main branch wholesale to publish this correction.

## What changed

- Removed the fixed Singapore-time order-submission instructions from the
  Execution Timing tab. Broker deadlines are explicitly separate from closing
  times and must be confirmed for the venue and order type.
- The headline explanation, weekly cycle and session bars now show each venue's
  two clock regimes independently: CEST/CET for Xetra and EDT/EST for the US.
  Weekday rollover follows the payload's rollover flag through JavaScript Date.
- One helper derives both labels and chart coordinates from the existing
  `sessions_sgt` fields. Missing clocks or rollover flags produce an unavailable
  message, not a summer-time fallback.
- The weekly cycle is four reflowing HTML steps. The session chart has four
  labelled rows and a horizontal scrolling container on narrow screens; its
  unsupported fixed order-submission window is removed.
- Normal-session references are labelled as such, with source provenance.
  They are not forecasts of the live fill date, holiday/early-close schedules,
  or order authorisation. `live_targets.json` remains the fill-date authority.
- Historical study clock literals retain their original daylight-time basis,
  now explicit in the history table heading. Research figures are unchanged.
- Rebuilt `docs/index.html` using `scripts/pipeline.py --dashboard-only`.

## Verification

The testing-strategy skill informed the test plan: pure renderer tests, pipeline
regressions, then the built page in a real emulated browser viewport.

- Nine JavaScript tests passed: both regimes, independent venue changes,
  month/year rollover, geometry/label parity, changed inputs, missing evidence,
  reflowing steps and absence of the old fixed submission instructions.
- A pytest wrapper runs that Node suite in the normal repository test run.
  It explicitly skips if Node is unavailable; Node was available in this run.
- Nine targeted pipeline and existing mobile-selector tests passed.
- Full repository suite: **2,262 passed, 26 skipped**, with 127 existing datetime
  deprecation warnings. The final helper-name/layout cleanup was followed by
  **10 targeted pytest checks passed** (including the nine JavaScript tests)
  and another complete 14-tab/four-width browser pass.
- Static publication checks: zero failures on source and built HTML; existing
  light-only theme warning retained.
- Real browser: all 14 top-level tabs opened at 390, 844, 768 and 1280 px.
  No JavaScript errors, body overflow or uncontained element overflow. Minimum
  rendered font across all tabs: 11 px (rotation accounted for, not mistaken
  for scale). The Execution Timing tab minimum is 11.5 px.

| Requested / measured viewport | Body width | Timing prose characters per line | Uncontained overflow |
|---|---|---|---|
| 390 / 390 | 390 | 45–48; inset lead 36 | 0 |
| 844 / 844 | 844 | 74 | 0 |
| 768 / 768 | 768 | 74 | 0 |
| 1280 / 1280 | 1280 | 74 | 0 |

Every timing table and the chart were scrolled to the right edge and the final
cell/edge was confirmed visible. At 390 px, the chart is 940 px wide inside its
362 px scroller; the process steps stack without scrolling. Screenshots and
measurements are local under `logs/execution-*.png` and
`logs/execution-mobile-metrics.json`.

Existing site-wide exceptions, outside this timing patch: changing `data-theme`
to dark retains the white background and dark text; other tabs have prose near
78 characters per line rather than the 65–75 target. The user explicitly approved
publishing the scoped timing correction with these exceptions unchanged.

## Boundaries and next review

No strategy, scheduled task, pre-trade predicate, production data or research
payload was changed. No production refresh, manual email or order was started.
The unrelated untracked Word review remains untouched. This patch does not
establish that the current book is ready to trade.

For Claude: read this handoff and the final commit, verify the live renderer and
its tests, and report only remaining code-supported inconsistencies. Do not
refresh production data or alter research construction to acknowledge the handoff.
