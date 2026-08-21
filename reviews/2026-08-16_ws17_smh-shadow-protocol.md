# WS17 shadow protocol — xyz:SMH fill-versus-model log (DESCOPED 2026-08-20)

**CLOSED EARLY, NOT-STARTED (owner decision, 2026-08-20).** Zero fills and zero
probes were logged between activation (2026-08-16) and descope; the Tuesday
probe was missed and is recorded as an ops fact, not a FAIL (the FAIL-OPS
trigger counted missed SIGNAL fires, and none fired). The evaluator task
`BreadthThrust-WS17Shadow` is disabled and its fleet-watch row removed. The
WS17 H2b verdict (KEEP-for-shadow) stands FILED BUT UNEXERCISED — the register
record's reopen condition is unchanged: adoption still requires a
fill-versus-model shadow, whenever one is actually run. The daily evaluator's
signal replays stop with the task; the append-only log remains as the record.

**Date drafted:** 2026-08-16 · **Context:** Personal · **Status:** DRAFT — inert until
the owner countersigns at the foot of this file. Follows from the WS17 H2b verdict
KEEP-FOR-SHADOW (`reviews/2026-08-16_ws17_hl-perp-expression.md`, register record
2026-08-16-breadth-thrust-etf-3). Pattern: the WS6b shadow discipline (frozen
triggers, 1.5× cost multiple) adapted to an event-driven signal.

## 1. Objective and non-objectives

The shadow answers the three questions the WS17 backtest cannot:

1. **Fill quality** — does xyz:SMH, at the operator's actual execution window
   (post-US-close, morning SGT), fill near the modelled daily close? The observed
   near-zero weekend volume on this market is the specific concern.
2. **Realised funding** — does funding actually paid over a hold sit inside the
   frozen {0, +3, +6} %/yr band the verdict rests on?
3. **Operational integrity** — panel lands, signal evaluates, alert fires, order
   window is met, end to end, per the vault's unattended-agent rule.

Non-objectives: the shadow does not re-test the signal (WS17 settled that), does not
size a position, and its success does NOT auto-deploy anything — graduation is a
separate owner decision with its own memo.

## 2. Instrument, account, size

- Instrument **xyz:SMH** perpetual on Hyperliquid, personal account, API agent wallet
  (trade-scope key separate from the master wallet).
- **Leverage frozen at 1×** — position fully collateralised; liquidation should be
  structurally impossible. Any margin call at 1× is itself a FAIL-OPS event.
- Size: **micro-live, US$300 notional per trade** (recommended over paper: quoted
  spreads understate impact; only real fills measure fills). Owner may strike this
  to paper-only at countersign; the log schema is identical either way.
- Verify the market's minimum order size at activation; if US$300 is below the
  minimum, use the minimum and record it.

## 3. Frozen signal machinery

- Config bit-identical to WS17 H2b: `regime_time_only_delay5_trend` on the SOXX
  constituent panel (`data/breadth_soxx.json`), entries at signal + 5 trading days,
  exits on regime exit or 252-day time stop, exactly as `scripts/backtest.py` defines.
- No parameter may change while the shadow runs. A code fix that alters any signal
  date is a numbered amendment to this file, disclosed before the next event.
- Trading-day arithmetic uses the exchange calendar through the repo's session
  machinery (`nyse_sessions.py`) — never hand-computed offsets.

## 4. Build prerequisite — daily evaluation (currently weekly)

The SOXX panel refreshes with the Saturday local refresh, but the 5-day entry delay
and the daily exit conditions need DAILY evaluation. Build items, all before
activation:

- A local daily task (post-US-close, ~07:00 SGT) refreshing the SOXX panel only and
  evaluating the thrust state machine: new fires, entry-due countdowns, exit
  conditions on open positions.
- Guards, per the unattended-agent rule: (a) panel-freshness check — evaluation on a
  panel more than 2 sessions stale writes a MISSED-EVALUATION row, never a silent
  skip; (b) the evaluator only LOGS and ALERTS — it never places orders; order entry
  is manual throughout the shadow; (c) a `fleet_watch.json` row for the task —
  a scheduled automation without one is not done; (d) alert email on fire, entry-due
  and exit-due, reusing the repo's alert plumbing.
- Estimated effort: about half a day.

## 5. The log (append-only, committed)

`data/ws17_shadow_log.json`, append-only; corrections are new rows referencing the
old, never edits. Row types:

- **signal**: fire date, panel as-of, composite/trend values, entry-due date.
- **execution** (entry and exit): timestamp SGT; modelled fill (the backtest close
  for that date); xyz:SMH mark, mid and impact prices at the window open; visible
  depth within ±25bp; realised fill and size; slippage vs modelled close in bp;
  funding rate in force.
- **hold** (daily while in a position): funding accrued (hourly sum from the API),
  position mark, cumulative shadow-vs-modelled divergence.
- **ops**: evaluator ran on time; panel fresh; alert delivered; order window met.

## 6. Execution window (fixed)

Modelled fills are the US close (04:00 SGT). The operator executes in a fixed window
**07:30–09:30 SGT** the same morning. The window is fixed so slippage measurement is
honest — the mark at window open is logged even on days no order is placed. Executing
outside the window is permitted only for the exit of a position under trigger 7(d),
and is logged as such.

## 7. Pre-registered triggers (frozen; evaluated as events occur, never tuned)

- **(a) FAIL-EXECUTION** — median realised round-trip cost (entry + exit slippage vs
  modelled closes) exceeds **15bp** (1.5× the modelled 10bp, the WS6b multiple)
  measured over at least 2 completed trades.
- **(b) FAIL-BAND** — realised funding drag over any completed hold, annualised,
  exceeds **+6%/yr** (the band edge the verdict rests on).
- **(c) FAIL-OPS** — two or more signal fires missed (not evaluated within the
  entry-delay window), or any margin call at 1×.
- **(d) Dislocation note** — any mark-versus-oracle dislocation beyond 5% during a
  hold is logged and the position may be exited out-of-window; one such event does
  not fail the shadow, two FAIL it (FAIL-EXECUTION).

## 8. Completion and adoption rule (frozen)

- **Completion:** three completed round-trip trades OR 12 calendar months from
  activation, whichever comes first. Expectation set honestly: the signal fired ~2×
  per year historically, so 12 months may complete with fewer than three trades;
  whatever completed is what is evaluated, and n is reported beside every figure.
- **Graduation gate:** no FAIL trigger fired AND median realised round-trip cost
  ≤ 15bp AND realised funding inside the band on every completed hold. Passing
  produces a sizing proposal memo for separate owner decision — nothing deploys from
  this protocol alone.
- **Any FAIL:** file the shadow as REJECT-EXECUTION against register record
  2026-08-16-breadth-thrust-etf-3's reopen condition and stop; reopen only with a
  venue or liquidity-regime change.

## 9. Reporting

One line per month in the weekly digest's attention strip (state: waiting / in-trade /
n complete), and a filed close-out memo at completion either way.

## 10. Activation

Inert until countersigned. The build items in §4 may be built beforehand but must not
alert or log live until the countersign lands. On countersign: ledger row appended
(shadow-start), activation date recorded here, first evaluator run verified manually.

## Amendment 1 (2026-08-16, at countersign) — 4-week venue-qualification horizon

Owner instruction at countersign: micro-live US$300, and completion within 3–4
weeks. A 4-week window has only ~13% odds of catching a live thrust fire (~2 fires
per year historically), so the completion rule in §8 is REPLACED by a design that
completes on schedule without depending on a fire:

- **Probes.** Two scheduled micro probe trades per week (Tuesday and Friday, executed
  in the §6 window; first pair 2026-08-18 and 2026-08-21): enter US$300 at 1×, exit
  in the next session's window. Target 8 probes, minimum 6. Probe slippage versus the
  modelled prior-close fill feeds the FAIL-EXECUTION trigger (§7a, unchanged 15bp
  median bar), measured on probes and any live-fire trades pooled.
- **Funding verification.** One probe in week 2 is held five sessions instead of one;
  its realised hourly funding accrual is reconciled against the published rates and
  the §7b band. The daily quote rows accrue the venue funding series regardless.
- **Ops soak.** The daily evaluator runs the full window; §7c unchanged.
- **Live fire.** If a signal fires inside the window it is executed per the original
  protocol and pooled into the same triggers.
- **Completion: 2026-09-13** (activation + 28 days). Graduation gate as §8, with
  "completed trades" read as "probes plus any live-fire trades".
- **Residual carried, stated plainly:** probe fills in whatever market the window
  provides do not measure fill quality on an actual event day (thrust entries cluster
  in high-volatility moments). A pass here qualifies the VENUE, not event-day
  execution; the sizing proposal must carry that residual explicitly.

**Countersign (owner):** Zhenghao — instructed in session, micro-live US$300, 4-week
horizon. **Date:** 2026-08-16. **Activation:** 2026-08-16; first evaluator run
verified manually the same day; first probe pair due 2026-08-18 (Tuesday) and
2026-08-21 (Friday), completion 2026-09-13 (Sunday). Dates computed with Python
`datetime` and flagged in the session summary for owner confirmation.
