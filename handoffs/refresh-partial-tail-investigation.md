# Refresh partial-tail investigation

Working directory: `C:\dev\breadth-thrust-etf`.
Status: local recovery/diagnostic patch; not committed, deployed or published.
Session model/effort is not exposed reliably by the tool interface; no model switch made.

## Observed outcome

The scheduler log `C:\dev\breadth-thrust-etf-sched\logs\scheduled_refresh_2026-09-12.log`
records the 12:00 SGT run failing G1/W1 and restoring unpublished tracked outputs.
The subsequent 15:00 run also failed; Task Scheduler was Ready when inspected,
with the next run scheduled for 09:00 SGT on 13 September. These are observed
timestamps, not changes to the schedule. Calendar date/weekday verified with
PowerShell Get-Date (12 September 2026 was Saturday).

The 12:00 run's thematic tail probe named `159801.SZ` and `BTC-USD` as unserved
before calendar/FX transformations. The subsequent freshness report identified
Bitcoin as the missing signal input. The 15:00 run and a read-only cache replay
both confirm C recovered: all 25 signal inputs exist on 11 September. The later
run produced A/B/C READY, D HOLD. No new source substitution is needed for C
on the evidence currently available.

Four European constituent panels remained partial on 11 September:
EXV1 1/57, EXV3 3/34, EXH3 8/107, EXH9 1/28 current-roster names priced.
Their usable breadth ended on 10 September. G1/W1 correctly blocked publication.
The separate all-empty EXH1 tail had been removed under the existing placeholder
policy. The clone restores JSON/build outputs after failure but retains ignored
price caches; restored JSON is not evidence of the failed run's transient state.

## Why no partial-publication exemption was added

`live_targets._breadth_panel` masks observations after each validated breadth
panel's end date. However, `run_portfolio._build_panels_for` separately calls
`run_ma200_sweep.load_constituent_prices` and `compute_ma200_breadth` on raw
caches without that cap. This is also used by the European engine.

A read-only replay of retained caches produced raw 11 September breadth values
of 100%, 75%, about 88.9% and 0% for EXV1/EXV3/EXH3/EXH9 respectively. These are
diagnostic examples of thin-row contamination, NOT valid signals. The raw-cache
calculation includes historical columns and therefore has a different denominator
from the current-roster counts above; do not compare them as equivalent measures.

An explicit HOLD on the live card alone does not prove that every downstream
portfolio/research output is insulated. G1/W1 remain unchanged and fail closed.
The audit establishes a consumer inconsistency, not that an erroneous trade was
executed. No trade or historical performance claim is made here.

## Local changes

- `scripts/compute_breadth.py`: a partially populated row now triggers a bounded
  healing pass beyond the five-name sample even if the sample serves no bars.
  Records requested, unserved, unanswered and unprocessed names. Existing real
  prices are preserved; unresolved rows remain present and fail the same guards.
  Existing split checks, source exclusions, time budget and floors are unchanged.
- `scripts/live_targets.py`: incomplete-signal HOLDs carry named missing inputs
  in both the reason and an additive `missing_signal_inputs` field. Ranking and
  position rules are unchanged.
- Tests cover unsampled recovery, no-answer versus unserved, preservation of
  partial prices, unchanged guard failure, timeout, named HOLD inputs, and month/
  year boundaries. All fetches in these tests are stubbed.

These changes improve recovery and explanation. They do not establish that the
vendor now serves every missing European bar, and do not resolve publication by
themselves. More single-name requests can increase runtime within the existing
per-panel healing budget; production timing must be observed before deployment.

## Verification

Targeted offline tests: **171 passed, 6 existing datetime deprecation warnings**
in 227.90 seconds across tail verification, live targets, capture chain, refresh
guard and pre-trade alert states. After adding the explicit publication-guard
assertion, that test was rerun separately: **1 passed** (not an additional unique
test). The initial sandboxed run had temporary-directory permission errors;
the successful rerun was outside the sandbox. No full refresh, dashboard build,
live vendor probe, full-suite run or publication performed. `git diff --check`
passed. Tests do not establish live vendor availability.

## Next decision and do-not-touch items

Before enabling partial publication, make the validated cutoff a shared contract
for every production consumer, demonstrate parity on fully priced historical
data, and test that partial current rows cannot influence rankings, held weights,
overlay inputs or published performance. Preserve raw vendor observations for
audit. Keep the book provisional, retain D positions, and do not auto-release a
factsheet/order instruction with HOLDs. This is a separate implementation scope,
not a guard downgrade in this patch.

Do not change registered strategy construction, historical windows or floors.
Do not push local main wholesale: `99da647` is an unrelated unpublished holdings
capture, and timing publication was isolated as remote `ee1938d`. Preserve the
untracked Word review and leave the automation clone and tasks untouched.
The one-off investigation heartbeat remains paused. Gmail failure-email setup
remains a separate operational issue; do not expose or write credentials.

Suggested reviewer task: review only this local diff and the consumer-cutoff
finding. Verify that the healing change cannot erase a real price or waive a
guard, and that named HOLD inputs match the missing signal columns. Do not
refresh, publish, reset, rebase or change research construction. Record findings
in a separate handoff. Suitable model: GPT-5.5 high for the bounded review;
GPT-6 Astra high for the subsequent cross-consumer design if authorised.
