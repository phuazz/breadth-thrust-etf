# WS7 — Sleeve C seat review (pre-registered specification)

**Status: REGISTERED 2026-07-18 — evidence accumulation live; decision rule
PROPOSED, awaiting owner sign-off. No strategy change is made by this
workstream until the review date.**

Drafted after the owner asked to progress the WS3 "KEEP, ON NOTICE" item
(2026-07-18, in session). The owner deferred the three scoping decisions to
the recommended options; each is marked **(assumed — countersign or amend)**
below. Everything in this file is fixed BEFORE any out-of-sample evidence is
read, so the review cannot tune its own pass bar after seeing results.

---

## 1. Question

Does Sleeve C (25 thematic ETFs, K=5 equal-weight, +5% signal floor, 30%
sleeve-breadth gate; 10% of NAV) justify its seat in the deployed blend?

## 2. Prior evidence (frozen — the base of the decision)

WS3 (filed 2026-07-03, `reviews/2026-07-03_ws3_heavy-gate.docx`; memo
verdict table):

- **Internal test FAILED on 7.5y:** C's rotation Sharpe +0.684 vs its own
  same-universe equal-weight basket +0.759 at the 1x per-line cost vector,
  max drawdown matched (−36% vs −37%). Break-even cost multiple 1.0x — the
  only sleeve that fails at its own cost assumption.
- **Seat test MARGINAL-FAILED on 7.5y:** blend without C +1.2964 vs with C
  +1.2921 (consistency 4/6 sub-periods). Kept because dropping a sleeve on
  a +0.004 margin is tuning on noise.
- **Survivorship quantified, not correctable:** BTC-USD alone 23% of gross
  sleeve contribution (added Phase 15 with backfilled history); top five
  names ≈ 62%; no point-in-time membership before 2026-07-18.
- Walk-forward Sharpe +0.51 — the largest in-sample-vs-OOS gap of the four
  sleeves.

These numbers are settled and will NOT be re-run at the review; the ledger
rule is reuse, not re-tread.

## 3. Review date **(assumed — countersign or amend)**

**Friday 2026-10-02** (weekday verified with a date library): the first
Friday after the Q3 close, giving a full 13-week clean OOS quarter since
WS3 filed. Early trigger: the tripwire in §6 can bring the review forward.

## 4. Registered definitions (fixed 2026-07-18)

- **OOS window:** 2026-07-03 (WS3 filing date) to the review date, weekly
  Friday observations from the engines' own published series. Weeks are the
  sleeve's rebalance calendar; a US-holiday Friday follows the engines'
  existing cadence handling.
- **EW-25 basket (the internal benchmark):** the same 25 risk names (SHY
  cash floor excluded), equal-weighted, rebalanced to equal weight each
  Friday, priced in USD (159801.SZ converted at USDCNY; BTC-USD is USD
  native). Costs: the WS3 per-line one-way spread vector at 1x
  (`data/ws3_cost_stress.json` `per_line_vectors_bps`, e.g. liquid 8 bps,
  thin 12 bps, BTC-USD and 159801.SZ 25 bps) charged on each week's
  rebalance turnover — the identical benchmark definition WS3's break-even
  used, held fixed here.
- **Rotation leg:** Sleeve C's own published `headline_equity` (already net
  of the deployed cost model). No recomputation.
- **Without-C blend (the seat counterfactual):** pro-rata renormalisation.
  Weekly: `r_without_C = (r_blend − w_C × r_C) / (1 − w_C)` where
  `r_blend` is the deployed gated+tilted blend's weekly return, `r_C` is
  Sleeve C's weekly return, and `w_C = 0.10 × equity_scaler` from the
  overlay state (the gate halves every sleeve's NAV share when RISK_OFF;
  the tilt does not change C). This is an algebraic decomposition of the
  published series, not a re-run; registered as THIS review's definition.
- **Universe freeze:** membership as of 2026-07-18 is logged in
  `data/c_universe_pit.json`. Any future addition enters the EW basket and
  the rotation universe only from its dated entry — the OOS window is
  point-in-time by construction. Pre-2026-07-18 membership history remains
  as WS3 characterised it (backfilled, biased, bounded).

## 5. Decision rule at review **(PROPOSED — requires owner sign-off)**

Three-way, applied in order, on the frozen WS3 evidence as base with the
OOS quarter as **confirm-or-veto only**:

1. **KEEP AS-IS** if the rotation beats the EW-25 basket over the OOS
   window by more than the noise band (§5a) — the machinery has started
   earning its costs; the notice is cleared.
2. Else **SWITCH C's 10% to the passive EW-25 basket** if the seat still
   helps: with-C ≥ without-C over the combined evidence (thematic exposure
   retained, rotation machinery retired). This directly implements WS3's
   finding that the exposure was marginal while the machinery was the
   clear loser.
3. Else **DROP**: redistribute 10% pro-rata to A/B/D (38.9/38.9/22.2),
   subject to the house rule that any deployed change must also not
   degrade the blend's walk-forward Sharpe (checked at review on the full
   window, no re-tuning).

### 5a. Noise honesty (binding)

Thirteen weeks cannot statistically settle a Sharpe gap. At C's ~18%
annualised vol, one quarter's tracking difference between two 25-name
baskets has a standard deviation of several percentage points. The OOS
window therefore CONFIRMS or VETOES the 7.5-year evidence; it cannot
overturn it on its own. Registered noise band: the rotation must lead or
trail the EW basket by **more than ±2.0pp cumulative** to count as signal
for rules 1–2; inside the band, the WS3 base evidence decides (which, as
filed, points to rule 2). This band is fixed now, before any data is seen.

## 6. Tripwire (live from 2026-07-18)

If the rotation trails the EW-25 basket by **5.0pp or more cumulative**
within the OOS window, the weekly email's watch line flags TRIPWIRE and
the review is brought forward to the next scheduled weekly. One-sided by
design: early outperformance changes nothing (entry-point discipline —
no rewarding a strong run early).

## 7. Instrumentation (measurement only — no strategy change)

`scripts/run_c_seat_watch.py`, run in the weekly pipeline after the
engines refresh:

- Appends one row per completed week to `data/c_seat_watch.json`
  (append-only; prior rows are never recomputed — the accumulated series
  is itself point-in-time): rotation return, EW-25 return (net, per-line
  1x costs), blend return, without-C return, cumulative gaps, tripwire
  state.
- The weekly email renders one watch line ("C seat watch: rotation vs EW
  −0.4pp OOS since 2026-07-03 · seat +0.1pp"); the factsheet gains the
  line at the review, not before **(assumed — countersign or amend)**.
- Unit tests cover the EW cost charge, the without-C algebra, append-only
  behaviour and the tripwire threshold.

## 8. Trial register

Zero configurations evaluated by this registration. The review itself
evaluates zero new configurations: it applies §5 to two pre-registered
comparisons. Nothing here contributes to a future deflated-Sharpe
haircut beyond the two named comparisons.

## 9. Three ways this could be silently wrong (stated before build)

1. **Benchmark drift** — an EW basket that quietly differs from WS3's
   (costs, rebalance day, FX) would move the goalposts. Mitigation: the
   per-line vector is read from the committed WS3 artefact, and the
   definition above is frozen in this file.
2. **Survivorship leaking into the OOS window** — a name added in August
   with backfilled history would repeat the Phase-15 BTC-USD pattern
   inside the very window meant to be clean. Mitigation: the PIT log +
   append-only series; additions join only from their dated entry.
3. **Noise read as signal** — a 13-week gap treated as decisive.
   Mitigation: §5a's registered ±2.0pp band and the confirm-or-veto role,
   fixed before any evidence exists.

## 10. Sign-off (owner)

| Gate | Status |
|---|---|
| Review date 2026-10-02 | ASSUMED — pending countersign |
| Decision rule §5 (three-way) + §5a band | PROPOSED — pending countersign |
| Tripwire §6 (−5.0pp, one-sided) | PROPOSED — pending countersign |
| Instrumentation §7 (email line now, factsheet at review) | ASSUMED — pending countersign |

*Registered 2026-07-18 (Saturday — weekday verified). Owner: Zhenghao
Phua. Amendments before 2026-10-02 are permitted only to gates not yet
touched by accumulated evidence; the definitions in §4 are frozen.*

## 11. Event log (append-only — records events, amends nothing)

**2026-08-16 (Sunday — weekday verified). Tripwire fired; early review
declined; scheduled review held.**

The §6 tripwire breached on week ending **2026-07-31** at −6.60pp and has
held past the limit for three consecutive weeks (−6.60, −7.07, −5.82).
Under §6 that brought the review forward to the next scheduled weekly,
2026-08-07. **The owner has declined the early review and held the review
at 2026-10-02**, so the full 13-week quarter runs as originally specified.

Reasoning, recorded now rather than reconstructed at review:

- The accumulated evidence indicts the **rotation machinery**, and mostly
  not from these six weeks: WS3's 7.5-year internal test already had the
  rotation losing to its own same-universe equal-weight basket at a 1.0x
  break-even cost multiple. The OOS window confirms the direction —
  −5.82pp, outside the §5a band, losing in 5 of 6 weeks, still outside the
  band with the worst week removed — which is the confirm-or-veto role §5a
  assigns it. It has not vetoed.
- It does **not** evidence the EW-25 basket as the destination, which is
  why §5 rule 2 was not exercised early. §4 defines that basket with the
  SHY cash floor excluded, so it carries none of the sleeve's risk
  machinery, and the entire OOS window sits inside a RISK_ON regime
  running since 2026-04-14 — no stress, and therefore no test of the
  machinery whose purpose is stress.
- Survivorship (§2) contaminates **both** legs of the 7.5-year comparison
  and cuts against the benchmark: EW-25 holds all twenty-five names
  permanently, including those admitted with backfilled history, while the
  rotation at least selects among them.
- The **seat** question remains unevidenced in both directions: the OOS
  seat gap of −0.84pp of NAV is inside the §5a band, and WS3's 7.5-year
  margin was +0.004 Sharpe. Nothing here supports rule 3.

**No gate was amended and no definition changed.** The §6 tripwire remains
at −5.0pp. This is not a matter of preference: the sign-off note above
permits amendment "only to gates not yet touched by accumulated evidence",
and the tripwire gate has now been touched by exactly that. Moving it
after it has fired is closed off by the specification's own rule, which is
the point of having written it down in advance.

**Carried to the review.** The reviewer must record that the early trigger
was exercised and declined, so the full-quarter window is not read as one
that never breached. Machine-readable record:
`data/c_seat_tripwire_log.json`, which the weekly email reads so that an
acknowledged breach is reported as a standing notice rather than an
outstanding action.

*Deployed configuration unchanged throughout: Sleeve C remains at 10% of
NAV running its rotation.*
