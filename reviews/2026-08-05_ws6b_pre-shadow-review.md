# WS6b pre-shadow review pack

**Purpose.** This is the pre-shadow stop of `C:\dev\KICKOFF_ws6b-unscreened-replication.md`
(BINDING; items 1–4 signed 2026-07-19; item 5 defaulted to
feasibility-as-a-function-of-NAV). Per the amended plan table, the stop is
ZH's review of the T1 model before the T3 shadow is armed. Everything
verdict-relevant is assembled here; nothing below re-runs the register,
adds an arm, or touches a frozen bar.

Drafted 2026-08-05 (verified weekday: Wednesday). Repo: `main`, working tree
clean. WS6b commits: T1 9ccc013…83c29dd (2026-07-19), ops assessment 285e16a
(2026-08-05), T2 02d86d0 (2026-07-19, NOT armed), shadow live-window fix —
see §5 (2026-08-05).

## 1. The question this review must answer

Does the T1 all-in model support proceeding to the shadow for the signed
PARTIAL-5 set (IUES, IUUS, IUCS, SOXX, IUFS)? Accepted outcomes:

- **PROCEED TO SHADOW** — arm the weekly publisher (staged command in §7);
  first countable week ends Friday 2026-08-07 if armed before that close.
- **FIX AND RE-PRESENT** — named defects, model corrected, new stop.
- **KEEP-ETF** — file and stop (any frozen-bar failure already forces this).

## 2. T1 — all-in drag against the signed floors

Window 2018-10-12 → 2026-06-30, 389 W-FRI rebalances, frozen WS6 I0
construction, PARTIAL-5 restriction guards clean (E0-vs-deployed parity
4.4e-16; zero weight leak; zero fallback weeks; 15 names per basket;
114/114 member prices resolved). Active schedule: IBKR Fixed US per-share +
LSE Tiered 0.05%-of-value, resolved PER INSTRUMENT (both arms' LSE legs
priced identically). Verified against the published schedules 2026-07-19 and
re-verified 2026-08-05.

| Sleeve NAV | base (floor 0.05) | 2× trading (floor 0.10) | 2× all-in (floor 0.10) |
|---|---:|---:|---:|
| $100k | 0.082 FAIL | 0.169 FAIL | 0.180 FAIL |
| $150k | 0.043 pass | 0.091 pass | **0.103 FAIL** |
| $200k | 0.023 pass | 0.052 pass | 0.064 pass |
| $250k | 0.012 pass | 0.029 pass | 0.040 pass |
| $350k | −0.002 | +0.002 | 0.013 pass |
| $1m | −0.023 | −0.041 | −0.030 |
| $3.5m | −0.031 | −0.057 | (within) |

- **Minimum viable NAV: $150k on the signed floors; $200k under the stricter
  all-in-2× reading** (the $150k miss is 0.003). By commission schedule:
  $150k Fixed / $50k Tiered / $25k fractional — the fractional per-order
  minimum ($1.00 vs $0.01) is UNRESOLVED on IBKR's published wording and is
  the largest single driver; resolve from an order-ticket preview before
  relying on any sub-$150k figure.
- **Income leg** (conservative "capital" dividend basis): 21.7 bp/yr set-level
  — IUES 6.9, IUUS 8.9, IUCS 2.3, SOXX 1.4, IUFS 2.2 bp. Alternative basis
  19.2 bp; the 2.5 bp spread ≈ 0.0014 Sharpe, not verdict-relevant. The SOXX
  term is the 0.30×TER result (only ~70% of a US fund's TER is recoverable by
  replication); the UCITS terms swap 15% fund-level for 30% investor-level
  withholding net of a proxy-TER credit.
- **Spread-uncertainty sweep** (both legs unverified — no published statistics
  exist for the LSE UCITS lines, and Corwin-Schultz was REJECTED on documented
  mega-cap bias): from $250k the base-floor pass holds across the ENTIRE swept
  space (US names 0.5–5 bp × UCITS 1–12 bp; worst cell 0.043 vs 0.05).
- **Seen-data caveat (binding).** The negative drags above ~$350k contain the
  +0.017 seen-window gross gap the registration bars from counting. The
  adoption case is book structure, not performance.
- **Wider implication, outside this register:** the deployed register costs E0
  at a flat 2 bps; on the published LSE schedule the true E0 trading cost is
  far higher. Every WS-series E0 net-Sharpe figure is optimistic to that
  extent. Related but separate: the 2026-08-05 venue-switch proposal (§8,
  Annexe A) would largely retire this term.

## 3. T1 — ops assessment vs the signed budget (≤ 30 min/week)

From the resolver-resolved caches (commit 285e16a), window 7.7y:

- Corporate deaths while held: **1** (PXD, Exxon close 2024-05). Five more
  names died only after rotation out (APC, CXO, XLNX, HES, WBA) — the live
  book never meets those.
- Spin-off-scale specials while held: **5** (EXC/Constellation +29.8% 2022-02;
  PXD ×2; EOG; DVN — the shale variable-dividend regime).
- Capital-structure events while held: **6** (NEE 4:1, NVDA 4:1 and 10:1,
  AVGO 10:1, ETR 2:1, KLAC 10:1) — broker-automatic under fractional.
- **Operator-touch rate 1.56 events/year.** Renames: upper-bound list only
  (BHGE→BKR, FISV→FI, DPS→KDP, MMC→MRSH), broker-automatic.
- Orders per rebalance: **median 50, p90 65, max 80** (E0: 7). Staged as one
  basket-trader CSV of market-on-close orders, computable pre-session (t−1
  reads).
- **Estimated 20 min/week typical, 22 p90 — inside the budget.** ESTIMATES,
  marked as such in the output; bar (c) is judged on MEASURED time during the
  shadow. The shadow itself is zero-touch; its weekly operator load is
  reviewing one guard line.

## 4. Uncertain figures (every one marked at source, `data/ws6b_params.json`)

1. LSE UCITS half-spreads — NOT FOUND published anywhere; swept 1–12 bp.
2. US mega-cap name half-spreads — no published per-name table; swept 0.5–5 bp.
3. IBKR fractional per-order minimum — ambiguous published wording; brackets
   minimum viable NAV ($25k–$150k).
4. Which LSE sub-schedule applies to USD-quoted UCITS lines (rate identical
   at 0.05% either way; only the minimum differs, immaterial).
5. SPDR proxy TER change-dates — filing dates used; biases against I0
   (conservative).
6. SOXX basket yield (1.31% window average) — no published semis yield
   series; exposure small (the line's drag is TER-driven).
7. Fund-level 15% withholding is the treaty rate, not per-fund verified
   (published analogue: VUSA FY2014 at 14.74%).
8. IBKR Tiered third-party pass-throughs disclosed, not modelled.
9. USD/GBP 1.30 for the LSE minimum — assumption; the percentage leg binds.

## 5. T2 — publisher, guard, and what the pre-arm verification found

Committed 02d86d0 with tests (guard layer: capture-integrity anchored to the
true NYSE calendar; weight integrity; unresolved-gaps with fallback-as-
resolved semantics; return sanity; divergence with BOTH bars logged weekly;
turnover on the running average; hash-chained append-only log).

**Pre-arm dry run №1 (2026-08-05, as committed) — REFUSED, correctly, and
exposed a real defect.** The guard fired on three independent axes for the
week ending 2026-07-17: capture-integrity (sector data 14 days behind),
weight-integrity (all four held baskets EMPTY), unresolved-gaps (60 names).
Diagnosis: the member-price fetch, the membership/resolution window and the
A3 weights builder were all clamped to the frozen study end 2026-06-30, so
every basketed line silently reverted to its ETF (I0 = E0 exactly, gap
+0.0 bp) — a shadow that would have measured nothing, on every live week.
A second bug compounded it: the fallback classification used a cumulative
counter that missed the all-reverted week, so the reversion surfaced as a
weight failure rather than as the registered, logged fallback.

**Fix (2026-08-05, this commit):** a live `end`/`window_end` parameter
threaded through `load_or_fetch_member_prices`, `line_member_universe` use,
and `fetch_ws6_weights.build_line` (defaults preserve frozen T1 semantics
byte-identically); the shadow refreshes PARTIAL-5 weights from the raw
snapshot cache before building; fallback classification now reads the
reconstructed baskets themselves. Two regression tests pin the end-threading
and the all-fallback week; suite green.

**Pre-arm dry run №2 (after fix):**

Week ending 2026-07-17 recomputed end-to-end: **I0 −1.4014% vs E0 −1.5073%,
gap +10.6 bp** (vs 66 bp registered / 42.9 bp adopted-set), turnover 0.2154
(bar 0.5087). Baskets sum to 1.0; book preserves E0 weight to 1e-12; zero
unresolved gaps. The ONLY failing check is capture-integrity (sector data
14 days behind) — the honest refusal this clone's stale caches deserve.
PUBLISHABLE: False, nothing written, tree clean. Each guard axis has now
been seen to fire independently (№1) and to clear independently (№2); the
green path can only be proven on current data at the first armed run — the
first-run-clean convention.

№2 also surfaced two live-data facts:

- **iShares snapshot fetches for the four LSE UCITS lines currently return
  anti-bot HTML** — twelve post-cache snapshots (2026-07-17/24/31 across
  IUES, IUUS, IUCS, IUFS) are absent, and weights carry forward from
  2026-07-10 under the engine's t−1 semantics. This raises ruling §6.3, and
  it is almost certainly the same wall behind the deployed pipeline's stale
  caches (§7).
- **Mapping-level unresolved names exist in the extended window** (IUES 3,
  IUHC 2, IUCM 2, IUCD 1, IUSP 1). None were consulted by this week's
  baskets (week-level gaps: none), but a new index entrant among them will
  surface as a gap or fallback the week it reaches a top-15 basket.
  Extending the A1/A2 resolver tables as corporate actions arrive is
  exactly the live ops load §3 estimates at 1.56 events/year.

Register compliance of the fix: publisher plumbing and guard reporting only —
no bar, floor, arm, window or construction parameter touched. Shadow weeks
count only from the commit per §2; zero weeks have been published.

## 6. Rulings requested from ZH (recorded in `ws6b_shadow.py`, surfaced at T2)

1. **Divergence bar.** The registered "≈66 bp" parenthetical derives from
   FULL-11's tracking error; the SIGNED set's own 3×TE is 43 bp. Backtest
   breaches: 0/404 weeks at 66 bp (a gate nothing trips), 5/404 at 43 bp.
   Both are logged every week; `BINDING_DIVERGENCE_BAR` holds "registered"
   until ZH rules. No shadow week ever needs re-running under either ruling.
2. **Turnover bar reading.** Per-week reading fails 15.2% of backtest weeks
   on normal behaviour; evaluated on the RUNNING AVERAGE across shadow weeks
   (the only reading under which the bar discriminates). Confirm.
3. **Missing-snapshot semantics.** The registration's fallback clause reads
   "any line-week with a missing snapshot or weights … reverts that line to
   its ETF for the week". The engine's live behaviour is SOFTER: an absent
   current snapshot carries the latest available one forward under t−1
   semantics (the backtest never exercised the difference — its snapshot
   series was complete weekly). While the iShares anti-bot wall persists
   (§5), live weeks will routinely run on snapshots one to three weeks old.
   Rule which reading governs: (a) STRICT — a snapshot older than the
   week's t−1 date fires the fallback (more valve weeks, all visible);
   (b) CARRY-FORWARD as t−1 semantics (current behaviour). Every published
   week records per-line snapshot dates either way, so no shadow week needs
   re-running after the ruling.

## 7. Arming plan — STAGED ONLY, nothing armed

Chain, as one weekly task (Saturday 08:30 SGT, after the 06:00 weekly cache
refresh push and the 07:15 Norgate feed run):

1. `git -C C:\dev\breadth-thrust-etf pull --rebase origin main`
2. `python scripts\run_ws6b_shadow.py`  (the publisher now refreshes member
   weights and member prices to its own window end internally)

```
schtasks /create /tn "breadth-thrust WS6b shadow (weekly)" /sc weekly /d SAT /st 08:30 /tr "cmd /c cd /d C:\dev\breadth-thrust-etf && git pull --rebase origin main && C:\Users\phuaz\AppData\Local\Python\pythoncore-3.14-64\python.exe scripts\run_ws6b_shadow.py >> data_local\ws6b\shadow_task.log 2>&1"
```

Timing arithmetic (weekdays verified by date library): armed on Thursday
2026-08-06 clearance → first fire Saturday 2026-08-08 08:30 SGT → computes
the week ending Friday 2026-08-07 → week 1 of 8 counts on 2026-08-07 →
week 8 ends Friday 2026-09-25. Bar (d) is satisfied by construction (the
2026-08-07 Norgate soak close precedes any adoption decision; the shadow
may run before it). Every week slipped moves the earliest ADOPT verdict a
week.

First-run-clean convention applies to the 2026-08-08 fire; verify and
commit the log state after it.

**Data-freshness dependency, stated plainly:** the committed sector caches
this clone holds reach 2026-07-21. The weekly refresh task (Saturdays 06:00
SGT from the scheduling clone, which commits `data/` and pushes) last ran
Saturday 2026-08-01 at 18:00 — twelve hours after its boundary — and the
2026-07-21 ceiling suggests that run did not deliver current caches. If
Saturday 2026-08-08's refresh does not land before 08:30, capture-integrity
will refuse the week (proven behaviour, §5) and week 1 slips. This is
deployed-pipeline territory, outside this register — owner to verify the
refresh completes, or run it manually before the shadow fires.

The likely root cause is one problem, not two: the dry run caught iShares
serving anti-bot HTML on the UCITS snapshot endpoints (§5), and the same
wall would explain the weekly refresh delivering nothing current since
mid-July. Beyond WS6b this degrades the DEPLOYED book: breadth staleness
past 7 days is NaN'd by design and sleeves go flat rather than trade on
stale signal. The deployed fetch path needs owner attention this week
regardless of the shadow decision.

## 8. Annexe A — BVI / venue-switch thread (outside this register)

`reviews/2026-08-05_sleeve-a-venue-switch.md` (commit 7279233) proposes
holding Sleeve A's thirteen LSE lines as their US-listed engine-proxy
equivalents, on the BVI-vehicle premise (estate-situs moot at a corporate
holder; withholding unchanged; net ≈ +$33k/yr at $3.5m sleeve). Status
PROPOSED — pending vehicle confirmation, tax counsel, ZH decision. **§5b
question for ZH:** if executed, E0's cost basis changes materially; the note
carries a drafted §5b log line, and the T4 all-in read must use the T1
machinery against the instruments actually held at verdict date. The income
differential on switched lines collapses (both sides 30%) and I0's remaining
case is the ~8 bp TER de-stack plus single-stock content. Nothing about the
switch alters the shadow's construction-fidelity measurement.

## 9. Annexe B — worktree housekeeping (observed, nothing removed)

`.claude/worktrees`: `pensive-tesla-637480` (fa359b9) and
`sleepy-thompson-4574bb` (9b2c0e0) are ancestors of main — stale, safe to
remove on approval. `distracted-bose-68ba95` (29794cf, branch
claude/hungry-ritchie-74f660) is **NOT an ancestor of main** — it holds
unmerged work and must not be removed without inspection. No removals
executed (destructive; needs individual approval).

## 10. Sign-off (ZH)

- [ ] §2 T1 model reviewed — floors, NAV bracketing, income leg accepted
- [ ] §3 ops within budget accepted (measured time governs bar (c))
- [ ] §6.1 divergence bar ruling: registered 66 bp / adopted-set 43 bp
- [ ] §6.2 turnover running-average reading confirmed
- [ ] §7 arming approved (or withheld) — target Thursday 2026-08-06
- [ ] NAV band supplied at T1-start (optional input, item 5 default stands)
- [ ] §8 §5b amendment question noted (decision separate from this stop)

**Outcome:** PROCEED TO SHADOW / FIX AND RE-PRESENT / KEEP-ETF

---
*Registration: KICKOFF_ws6b-unscreened-replication.md. Evidence:
data_local/ws6b/t1_friction_results.json, t1_ops_assessment.json,
book_mechanics.json; data/ws6b_params.json; commits 9ccc013…83c29dd,
285e16a, 02d86d0, 7279233. All weekday assertions verified with Python
`datetime`.*
