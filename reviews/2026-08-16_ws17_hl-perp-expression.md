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

## Amendment 1 (2026-08-16, before any H1 result was computed)

The H1 guard as frozen assumed the deployed states derive from
`breadth_csp1.json`. The first run stopped on that guard and surfaced a fact I did
not know when freezing: `risk_overlay.json` reports `gate_feed: norgate-local` — the
deployed overlay sources states from the Norgate-derived local feed, and the two
panels genuinely disagree near the thresholds (four re-engage dates differ by one
session: 2022-07-20/21, 2022-10-26/27, 2023-03-31/04-03, 2023-11-03/14; and the
committed 2026-03-20 → 2026-04-14 RISK_OFF/ON pair does not occur at all on the
iShares-panel replication). Amendment, disclosed before results: the flip population
is the committed deployed events list itself (which is the record under study and
needs no replication to be valid); the failed replication is refiled as a
**feed-divergence finding**; and the guard is replaced by one on what H1 actually
depends on — the three largest bridge returns must agree between the SPY cache and
an independently fetched ^GSPC series within 15bp. Success criteria unchanged.

## Results (2026-08-16, all runs same day; artefacts in `data/ws17_*.json`)

**H1 — gate-flip bridge: NO-EFFECT.** Population = the 19 committed deployed events
(2018-11-28 → 2026-04-14, 7.8y; `risk_overlay.json`, gate_feed norgate-local). Price
guard passed (three largest bridge returns agree with independent ^GSPC within 15bp).
Gross bridge capture +251.2bp of NAV cumulative; net of the frozen perp costs
+140.5bp, i.e. **+18.1bp/yr**. n=19 ≥ 8, so the inferential rule applies: bootstrap
90% CI of the cumulative net sum **[−152.4, +445.5]bp — straddles zero**, so the
pre-registered KEEP criterion fails. The bridge is worth its costs on the sample mean
and is not distinguishable from noise. Texture (not a registered hypothesis, flagged
only): RISK_OFF bridges were positive on 8 of 9 pre-2026 flips; RISK_ON bridges were
mixed with several large negatives. Any OFF-only variant would be a NEW pre-registered
study, not a re-slice of this one. **Feed-divergence finding** (from the failed
replication guard, Amendment 1): the deployed norgate-local states and an
iShares-panel replication disagree by one session on four re-engage dates
(2022-07-20/21, 2022-10-26/27, 2023-03-31/04-03, 2023-11-03/14) and the committed
2026-03-20 → 2026-04-14 RISK_OFF/ON pair does not occur on the iShares panel at all —
near-threshold flips are feed-dependent, which is worth knowing wherever gate states
are consumed. Artefact: `data/ws17_gate_bridge.json`.

**H2a — restatement gate: the SOXX edge SURVIVES the corrected panel.** Focus variant
`regime_time_only_delay5_trend` on the as-committed 2026-08-15 breadth file: n=15,
win 66.7%, total +147.9%, MaxDD −25.0%, **Sharpe +0.65, MC percentile 63.9** (May-2026
vintage read +0.74 / 65.0 on a window ending 2026-05-15). Variant ordering preserved
(baseline 8.8 < regime 40.1 < winner 63.9). The CNDX-style collapse did NOT repeat on
the home universe. Proceed gate (≥ 50) passed. The restated
`data/backtest_soxx_oos.json` supersedes the May vintage (WS15 precedent); a copy is
pinned at `data/ws17_soxx_restated.json`.

**H2b — SMH vehicle economics: KEEP-for-shadow across the entire band.** Same signal
traded on SMH (`data/ws17_soxx_smh.json`): n=15, 34% of days in trade, gross Sharpe
+0.70 (runner convention; +0.75 on the equity-curve daily recompute used for the band
arithmetic — both quoted, the band DELTA is the meaningful number), MC percentile 57.3.
SMH trailing-12m dividend yield measured at **0.19%** (1.1050 / 587.82) — semis forgo
almost nothing on a price-only perp. Under the frozen band (funding + dividend drag on
in-trade days):

| band | total drag /yr | Sharpe | total return | MaxDD |
|---|---|---|---|---|
| 0% | 0.19% | +0.74 | +152.5% | −25.0% |
| +3% | 3.19% | +0.71 | +139.3% | −25.7% |
| +6% | 6.19% | +0.67 | +126.9% | −26.4% |

Worst-band Sharpe +0.67 ≥ +0.40 with positive total return ⇒ **KEEP-for-shadow** by
the frozen rule. The implementation drag at the band edge costs −0.07 Sharpe — the
expression is cheap because the strategy is in the market only ~a third of the time,
and the measured 30d funding on xyz:SMH was −1.9%/yr (longs paid) against the band's
0% floor. Context: at 15 trades the Sharpe SE is wide; the KEEP is for SHADOW, not
deployment, and inherits every caveat on the underlying signal (small sample, WS8's prior
that thrust deployment historically loses to random placement on other universes).

## Verdicts

- H1 (perp bridge of Phase 19 flips): **no-effect** — measured bound +18.1bp/yr net,
  90% CI straddles zero. Do not build.
- H2a (restatement): **confirmed** — the home-universe thrust survives the corrected
  panel at MC 63.9, Sharpe +0.65.
- H2b (SMH perp expression): **KEEP-for-shadow** — clears the frozen bar across the
  entire funding band; next step, if taken, is a shadow log of live xyz:SMH fills
  against modelled fills, not capital.

Filing: ledger row + three register records (2026-08-16-breadth-thrust-etf-1/2/3) +
index regeneration, all four guards green. This memo is the filed record (WS10/WS12
precedent for studies of this scale); a `.docx` technical record can be built from it
on request.
