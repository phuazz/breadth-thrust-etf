# WS18 — Move the whole book to a Monday rebalance (pre-registration)

**Date:** Saturday 2026-08-22 · **Context:** Personal · **Repo:** `breadth-thrust-etf`
**Status:** PRE-REGISTERED — frozen before any run. Nothing below may be edited after the
first result is computed; changes go in a dated Amendment section, as WS17 did.

**Owner request (2026-08-22):** move the whole book from a Friday rebalance to a Monday
one, after the vendor-availability probe showed sleeve D cannot rank on the intended
session under the Friday cadence.

---

## 1. Ledger check

Run this session, verdict **ADJACENT**. Direct predecessors:

- **Ledger row 2026-08-12, WS12 + WS13 (execution timing).** Settled the current
  convention: no look-ahead, every engine reads `get_loc(rd)-1`, execution on the Friday
  closing auction. Priced the weekday × open/close grid. Record
  `reviews/2026-08-12_ws12-ws13_execution-timing.docx`.
- **Ledger row 2026-08-10, WS10 (holiday cadence).** Adopted `holiday_aware`. Directly
  load-bearing here, for the reason in §5.3.
- **Ledger rows 2026-08-15 (execution integrity, and the tail-bound regression).** Built
  the probe whose output triggers this study.

No prior study tests a whole-book cadence move. This is new work.

---

## 2. Why this is being done — and the claim it deliberately does NOT rest on

The reason is **operational correctness, not performance.**

The vendor-availability probe (30 samples, 2026-08-15 to 2026-08-21) shows that at the
Friday decision hour the European data is *always* a session behind:

| Probe hour | US proxies | Europe ETF lines | Europe constituents |
|---|---|---|---|
| 00:00 UTC | 6/6 current | **9/9 late** | **6/6 late** |
| Fri 09:03 SGT | current (Thu 20th) | late (Wed 19th) | late (Wed 19th) |
| Fri 14:38 SGT | current (Thu 20th) | **still late (Wed 19th)** | **still late** |

No hour in the safe refresh window (08:00–15:00 SGT, before Xetra opens) has the European
data current. So under a Friday cadence sleeve D can only rank at `rd−2` while A, B and C
rank at `rd−1`. **The live book cannot implement the convention the engines backtest, for
20% of NAV, every week.** A Monday rebalance ranked on Friday's close restores `rd−1` for
all four sleeves.

**WS13's W-MON grid measured +0.0336 Sharpe against the deployed Friday leg. That number
is NOT the justification and must not be used as one.** WS13 rejected its Wednesday grid
(+0.1335, CI clear of zero) as *"best-of-five, uncorrected for multiplicity, sleeves
disagree, no mechanism."* W-MON sits in the same grid. Adopting it on its Sharpe would be
the identical error with a smaller number. The +0.0336 is used here only as prior
reassurance that the move is unlikely to be costly — it enters as a **non-inferiority
prior, never as evidence of improvement.**

---

## 3. The change, precisely

One thing changes: the rebalance day.

- `HEADLINE_FREQ` / `rebalance_freq` `"W-FRI"` → `"W-MON"` in all four engines
  (`run_topk_robustness`, `run_asset_class_rotation`, `run_thematic_rotation`,
  `run_europe_rotation`) and the shared `run_portfolio` default. 52 occurrences of
  `W-FRI` across `scripts/` are in scope for audit; only the cadence ones change.
- Decision session becomes **Friday's close**; fill becomes **Monday's close**.
- The refresh moves back to **Saturday morning SGT**, where it sat until 2026-08-12.

Everything else is held fixed: universes, K, weighting, the Phase 19 gate, the Phase 22
tilt, sleeve weights 35/35/10/20, cost assumptions, `holiday_aware` calendar mode.

---

## 4. Frozen decision rule

Measured on the **deployed variant** `blend_35_35_10_20_gated_eem_tilted`, full history
2018-11-08 to the common end, W-FRI against W-MON.

**ADOPT** if all three hold:

| # | Criterion | Bar |
|---|---|---|
| A1 | Sharpe, paired block bootstrap (60-day blocks, 2000 samples, seed 42) | W-MON **not worse than W-FRI by more than 0.05** |
| A2 | Maximum drawdown | **not worse by more than 2.0pp** |
| A3 | One-way turnover | if it rises **>25% relative**, A1 must still hold at **2× costs** |

**REJECT** otherwise. On rejection the fallback is a split cadence — sleeve D alone on
Monday — which is a *different* design and would need its own pre-registration; it is not
adopted by default here.

**Why 0.05.** WS13's fill-timing effects ran ±0.03 to ±0.05 with paired CIs straddling
zero on four of five grids. That is the noise band on this book for this class of change.
A move justified on correctness must merely be **indistinguishable** from the incumbent —
so the bar is non-inferiority at the noise band, not superiority. Any tighter and noise
would veto a correctness fix; any looser and a real cost could pass.

**The unpaired ~0.36 Sharpe SE used elsewhere in this book is the wrong yardstick** and is
not used. These are correlated same-history comparisons; all inference is paired, per the
method WS13 established.

---

## 5. The three ways this could be silently wrong (stated before running)

**5.1 The two legs see different data, so the diff mixes cadence with vendor drift.**
This has already bitten twice in this repo — the tail-extension verification failed its
first control for exactly this, and two further controls passed it for the wrong reason.
**Mitigation, mandatory:** both legs run in ONE process against ONE pinned price frame,
the `--fixed-prices` technique from `tools/verify_tail_extension.py`. A diff computed
across two downloads is not admissible evidence in this study.

**5.2 Turnover and cost differences are unmodelled.** The backtest charges fixed bps per
unit of weight change. A Monday rebalance crosses different liquidity, and the weekend gap
may change how much the weights move. **Mitigation:** report one-way turnover for both
legs explicitly; A3 exists to catch it. Do not assume the cost model transfers.

**5.3 Monday holidays are far more frequent than Friday ones, so the calendar mode
carries much more load.** Measured over the sample:

| Venue | Mondays closed | Fridays closed |
|---|---|---|
| **NYSE** | **39 / 406 (9.6%)** | 15 / 407 (3.7%) |
| XETR | 17 / 406 (4.2%) | 15 / 407 (3.7%) |

**NYSE Mondays are shut 2.6× as often as Fridays**, and sleeves A, B and C — 70% of NAV —
trade there. WS10 adopted `holiday_aware` because a holiday rebalance was silently
skipping a whole week; under W-MON that path is exercised 2.6× more. **Mitigation:** count
resolved rebalance dates and rolls in both legs, confirm no week is silently dropped, and
verify every roll still ranks strictly before its fill. A cadence that quietly trades 24
fewer weeks is not a like-for-like comparison.

---

## 6. Explicitly NOT tested here

Sleeve weights; K; the +5% thematic floor; the Phase 19 gate parameters; the Phase 22
tilt; the universe; the cost calibration; open-versus-close (settled — **Monday open is
FLAGGED AGAINST at −0.0508 with the paired 90% CI clear of zero, the only leg in WS13
whose CI cleared zero; execution is market-on-close and that is not reopened**).

No parameter is tuned. Configurations are bit-identical to the filed ones apart from the
cadence string.

---

## 7. What adoption entails, and why it is not a config tweak

Adoption **restates every published number** — Sharpe, CAGR, drawdown, trade history,
factsheet, dashboard, and the gate thresholds derived from the blend. The deployed record
becomes a different series. It is therefore executed as one change, with before/after
attribution filed, and nothing publishes until the ledger row and register records exist.

The guards that fought the Friday cadence all weekend — G1, G4, `week_final_anchor` — were
built for a Saturday-refresh cadence and are expected to stop conflicting. That is a
prediction, recorded here so it can be checked rather than claimed afterwards.

---

## 8. Stop conditions

- Any leg that cannot be run against the pinned frame → **halt**, do not substitute a
  second download.
- Rebalance-date counts differing by more than the holiday table above explains → **halt**
  and reconcile before reading any performance number.
- A1/A2/A3 failure → **REJECT**, file the null, do not proceed to the split-cadence
  fallback without a fresh pre-registration.

---

## 9. Sign-off

| | |
|---|---|
| Prepared by | Claude Opus 5 (Claude Code), under ZH direction |
| Design frozen | 2026-08-22 (Saturday), before any run |
| Approved to run | ________________________ (Zhenghao) |
| Result to be filed as | `reviews/2026-08-<dd>_ws18_monday-cadence.docx` + ledger row + register records |

*Personal research artefact. Not investment advice. All figures simulated; no live track
record.*
