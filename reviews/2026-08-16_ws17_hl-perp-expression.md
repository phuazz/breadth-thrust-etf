# WS17 — Hyperliquid perp expression of the gate and the thrust (running memo)

**Date:** 2026-08-16 · **Context:** Personal · **Status:** PRE-REGISTERED (this section
committed before any result was computed; results appended below afterwards).

**Owner request:** run the gate/thrust satellite study on xyz:SP500 and SMH — the two
expressions identified in the 2026-08-16 Hyperliquid venue memo (chat): the Phase 19
CSP1 breadth gate executed via the trade.xyz SP500 perp, and the filed SOXX composite
thrust config executed via the trade.xyz SMH perp.

## Ledger check (run first, per house rule)

Verdict **ADJACENT**. Four filed records bind this design:

1. **WS8** (2026-08-01-breadth-thrust-signal-1, † unreviewed extraction): thrust cell as
   an unlevered tilt overlay on 60/40 → **rejected**; the timing loses to randomly-placed
   tilts of identical size and frequency, net of matched costs. WS17's thrust leg differs
   in signal (SOXX composite cell, the one universe where the cell beat its null) and in
   expression (standalone satellite, not a tilt on 60/40) — but WS8's cause of death is
   the reference prior: expect the null to be hard to beat.
2. **WS15/WS16** (2026-08-13): the May-2026 SOXX OOS row is an un-restated vintage; its
   sibling CNDX row collapsed on restatement (every variant below its null median). The
   thrust leg therefore runs as **restate first, price the venue second**.
3. **WS12+WS13** (2026-08-12): execution timing settled — no look-ahead (engines fill at
   the close after the signal close); a one-session delay on the whole weekly book priced
   at −0.0222 Sharpe. Phase 19 flips are ~two orders of magnitude rarer than weekly
   fills, so the prize ceiling for faster flip execution is small a priori.
4. **2026-07-03-market-regime-dashboard-3** (†): any single lens as a standalone market
   timer → rejected. H1 is therefore framed strictly as execution economics of the
   ALREADY-DEPLOYED overlay, not as a standalone timer claim.

## Pre-registered design (frozen)

**H1 — gate-flip bridge (xyz:SP500).** The deployed overlay computes states from
`breadth_csp1.json` `series.ma_breadth` with 0.20/0.50 hysteresis and applies them
`shift(1)` — the backtest fills at the signal close, while a live ETF fill happens at
the next session's close. A perp fills within hours of the panel landing, at
approximately the signal-close price. The measurable prize is therefore, per flip, the
close-T to close-T+1 SPY return × flip direction × 50% NAV, minus perp costs (10bp
round trip on the moved notional per flip pair; funding for a ≤1-session bridge at the
worst band, +6%/yr ≈ 1.6bp/day, charged). Guard: the replicated state series must match
the committed `risk_overlay.json` regime series exactly on shared dates, else stop.
**Success criteria:** if the flip count n < 8, no inferential verdict — file the
measured bound as verdict `measured-bound` (expected path given the gate's rarity).
If n ≥ 8: paired bootstrap 90% CI of per-flip net gap must clear zero for KEEP.

**H2a — thrust restatement gate (SOXX on the corrected panel).**
`run_etf_oos.py --etf SOXX` on the as-committed 2026-08-15 breadth file (post-WS11/WS16
vintage). Focus variant `regime_time_only_delay5_trend` (the filed split-half winner);
the other two variants report for the record. **Proceed gate:** H2b runs only if the
restated MC total-return percentile ≥ 50 (beats its own cost-matched random-entry null
median). Otherwise the thrust leg closes as REJECT-BY-RESTATEMENT (the WS15 outcome
repeating on the home universe) and H2b is reported as an arithmetic bound only.

**H2b — vehicle economics (SMH perp).** Same signal, vehicle SMH
(`run_etf_oos.py --etf SOXX --yf-symbol SMH`; xyz lists SMH, not SOXX). Perp-vs-ETF
per-trade delta = (funding_band + SMH trailing-12m dividend yield, measured from the
dividend history) × holding_days / 365. Fees held EQUAL at 10bp round trip on both
vehicles — conservative: growth-mode HIP-3 taker fees are lower and are not credited.
Funding band frozen at {0, +3, +6} %/yr long drag; the measured 30d actual on xyz:SMH
(−1.9%/yr, longs currently paid) is context only and does not enter the verdict. The MC
percentile is invariant to a uniform per-day drag (strategy and cost-matched null shift
together; trade counts match), so H2b is an absolute-economics gate:
**KEEP-for-shadow** if net Sharpe ≥ +0.40 and net total return > 0 across the ENTIRE
band; **INCONCLUSIVE** if it clears at 0–3% only; **REJECT** otherwise. (+0.40 ≈ half
the May-vintage gross Sharpe — the implementation may cost at most half the edge.)

**No tuning anywhere.** Configs bit-identical to the filed `CONFIGS` in
`run_etf_oos.py`. Any deviation is a new study.

## The ways this could be silently wrong (stated before running)

1. **Panel vintage** — a stale or mixed-vintage breadth file would repeat the WS15 trap.
   Mitigation: breadth files as-committed at the 2026-08-15 refresh (post-WS16); the
   output JSON stamps the breadth coverage window and is quoted in the results.
2. **Total-return vs price basis** — the OHLC cache is total-return (auto_adjust); a
   perp is price-only. Mitigated by subtracting the measured dividend yield in H2b;
   residual error bounded by the yield estimate's error.
3. **The close-T fill assumption in H1** — the panel lands hours after the close, so a
   real perp fill occurs at close-T price plus unmodelled evening drift. The measured
   prize is therefore an UPPER bound on what a perp recovers; the bias direction is
   against H1, which is the conservative direction for a KEEP claim.
4. **Funding-regime correlation** — funding is highest exactly when the thrust is long
   (risk-on runs), so a uniform band could understate realised drag. Mitigated by the
   +6%/yr band edge sitting far above the measured 30d means on the index/sector perps
   (−1.9% to +3.3%/yr as-at 2026-08-16).

## Results

*(appended after the frozen design above; nothing below existed when the design was
committed)*
