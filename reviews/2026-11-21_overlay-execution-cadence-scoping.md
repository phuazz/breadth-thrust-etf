# Overlay execution cadence: scoping note

Date: 2026-11-21 (SGT). Status: scoping only. No study run, no rule changed, no record filed.

## Question

The breadth gate (S&P 500 % above 50-day MA, off 0.20 / on 0.50, 50% de-risk into SHY) and the Phase 22 EEM tilt (EEM/SPY 50-day over 200-day, 10% of NAV from sleeve B) are evaluated daily. Which execution basis should the book run on, and should the published figures match it?

- Backtest (production `run_risk_overlay.py`): a flip trades on the next session (`states.shift(1)` at line 580, `sig.shift(1)` at line 462).
- Live process: `overlay_decision.py` runs only in the core weekly refresh (`refresh_all.py:526`), and `live_targets.py:400` rejects a decision not dated to the Friday anchor. A flip is traded at the next Monday fill, up to about five sessions after the signal.

So the published Sharpe and drawdown for the deployed variant rest on an execution basis the book does not use.

## Prior work

The ledger check run on 2026-11-20 returned ADJACENT. The index is at 100% coverage.

- WS17 (2026-08-16-breadth-thrust-etf-1), no-effect. Bridging gate flips one session earlier with a perp: +18.1bp a year, 90% interval [-152, +446]bp over 19 flips. One-session timing sits inside noise.
- WS18 (2026-08-22-breadth-thrust-etf-1). Friday-to-Monday rebalance adopted on operational grounds. The gate and tilt timing were held identical in every arm, never varied.
- WS12/WS13 (2026-08-12, both †, unreviewed extraction). One-session-late sleeve fill: -0.0222 Sharpe. Sleeves only, not the gate.
- WS23 (2026-10-04-breadth-thrust-etf-1), no-effect. No post-fill give-back. The full published history is SEEN for the fill-placement and fill-convention families.
- R9 (2026-11-02-risk-overlay-lab-1), rejected. The gate's protection over 1991-2018 comes from 2008-09 alone. That window is SEEN for the gate family.

**Not in the ledger:** branch `codex/weekly-overlay-execution` (2026-09-14, unmerged, worktree `worktrees/weekly-overlay-execution`) already restated the overlay to weekly-close execution. It sits in `handoffs/weekly-overlay-execution.md` and `handoffs/claude-weekly-overlay-review.md` on that branch. Its same-input comparison, 2018-10-31 to 2026-09-11, combined gate and tilt:

| | Daily-lag model (published) | Weekly-close candidate |
|---|---:|---:|
| Sharpe | 1.2382 | 1.1730 |
| Annualised return | 14.82% | 14.05% |
| Maximum drawdown | -15.07% | -14.57% |
| Total return | 196.52% | 181.16% |

These figures come from one source: the branch's handoff. I have not re-run them. The review commit (7502a512) also states that the overlay stack's in-sample Sharpe advantage over the un-overlaid blend moves from +0.054 to -0.011 on the weekly basis, while the drawdown protection survives. It judged the branch not deployment-ready, with three blockers:

1. The external monitor adapter still reads raw signal events.
2. Both walk-forward validations need re-running on the corrected basis.
3. One hand-written prose claim asserts a Sharpe lift that the sync rewrites to a negative number.

## What a new study could still add

The 2018-2026 history is already measured on both bases, so re-running the comparison there would re-tread SEEN data. Any confirmatory claim needs history nobody has looked at, and R9 has already used 1991-2018 for the gate family. What remains, at diagnostic grade only:

1. A paired block-bootstrap interval on the daily-minus-weekly difference that the branch already computed. This would say whether the 0.77pp a year gap is distinguishable from noise across about 19 gate flips and the tilt crossings. WS17 suggests it may not be.
2. A split by flip direction: RISK_OFF against RISK_ON, and tilt on against off. WS17 recorded RISK_OFF bridges positive on 8 of 9 pre-2026 flips; its reopen condition is an OFF-only variant on fresh flips after 2026-08-16.
3. Forward accumulation. Every flip from now on is fresh evidence. The daily flip alert (`overlay_flip_alert.yml`) records each one and the date the book matched it.

## Decisions for the owner

1. **File the September branch's result.** Add a ledger row and a register record, so the next ledger check finds it. It is the substantive answer to this question and is currently invisible to the check.
2. **Choose a basis.**
   - (a) Adopt the weekly-close restatement: finish the three blockers, as a coordinated release, so the published figures match the book.
   - (b) Keep the daily basis and build an approved mid-week overlay trade path, so the book matches the published figures.
   - The in-sample evidence on the branch favours (a) on return, about 0.77pp a year, and slightly favours weekly on drawdown. (b) adds operational load for a gap that may sit inside noise.
3. **Optionally, the diagnostic** in item 1 above, labelled SEEN, before deciding between (a) and (b).

Until a basis is chosen, the flip alert covers the operational risk: a flip is reported the morning after it happens, and a mid-week trade remains a logged discretionary override.
