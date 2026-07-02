# Research memo — strategy review (REVIEW_PROMPT.md)

Running memo across review sessions. Session 1 covers Workstream 0 (orient) and
Workstream 1 (moving-average robustness). Workstreams 2 (universe) and 3 (heavy
robustness gate) are deferred to later sessions per the staging plan.

- Started: 2026-07-02
- Data as of: caches through 2026-06-16 (EU constituents) to 2026-07-01 (US);
  committed JSONs from the 2026-07-02 weekly refresh.
- Constraint honoured: no edits to `template.html`, `docs/`, or any deployed
  `scripts/run_*.py`. All experiments in NEW scripts (`scripts/run_ws1_*.py`,
  `scripts/ws1_common.py`) writing JSON to `data/ws1_*.json`.

---

## Workstream 0 — signal map (code-verified, with file:line)

### Sleeve engines

| Sleeve | Signal | Horizon | Selection / weighting | Universe | Cost | File:line |
|---|---|---|---|---|---|---|
| **A — US sectors (35%)** | Constituent breadth: share of constituents above their own **200d** MA, made sector-RELATIVE (sector minus cross-sectional mean per date) | 200d (`MA_PERIOD` imported from `run_ma200_sweep.py:55`) | Top K=7 by relative breadth, weight by positive-relative share (`top_k_breadth_weight`), weekly Friday | 13 ETFs: SOXX CSP1 CNDX + 10 iShares UCITS sector slices (IUIT pruned) traded via SPDR proxies (`etf_registry.py:583-605`) | 2 bps (`run_topk_robustness.py:53`) | breadth calc `run_ma200_sweep.py:117-150`; relative transform `run_topk_robustness.py:75-82`; weight fn `run_portfolio.py:199-253`; K=7 `run_topk_robustness.py:92` |
| **B — asset class (35%)** | ETF-level graded momentum: `(close − MA200) / MA200` (distance, not binary above/below) | 200d (`run_asset_class_rotation.py:120`) | Top K=7 among positive-signal names, weight by signal share, deficit slots to SHY cash floor | 13 broad ETFs: SPY IJR QQQ EFA VGK EWJ EEM VNQ GLD DBC TLT IEF TIP (+SHY cash-only) (`run_asset_class_rotation.py:74-115`) | 2 bps (`:126`) | signal `run_asset_class_rotation.py:232-238`; weight fn `:241-280`; K=7 `:136` |
| **C — thematic (10%)** | Same graded momentum as B | 200d (`run_thematic_rotation.py:288`) | Eligibility floor +5% above MA (`:289`); **top K=5 equal-weight** (`:312`, Phase 27); **sleeve-breadth gate: all to SHY when <30% of universe clears the floor** (`:333-334`, Phase 27 "V6"); SHY deficit floor | **25 thematics** incl. BTC-USD (25 bps IBIT drag) and 159801.SZ (CNY→USD, 50 bps drag) (`run_thematic_rotation.py:80-261`) | 5 bps (`:295`) | weight fn + gate `run_thematic_rotation.py:529-599`; FX/drag loaders `:384-521` |
| **D — Europe sectors (20%)** | Constituent breadth, share above **200d** MA — **ABSOLUTE, not relative** | 200d (imported `run_europe_rotation.py:45`) | Top K=3 breadth-weighted (`:65`), weekly Friday | 5 Stoxx Europe 600 sector UCITS: EXV1 EXH1 EXV3 EXH3 EXH9 (`etf_registry.py:614-620`) | 9 bps incl. FX (`run_europe_rotation.py:55`) | engine `run_europe_rotation.py:161-200`; EUR→USD conversion `:128-158` |

### Blend and overlays

| Component | Mechanism | File:line |
|---|---|---|
| **Blend 35/35/10/20** | Fixed-weight, weekly-Friday snap-back, weights drift intra-week, 5 bps on rebal turnover | `run_multi_strategy.py:170-215`, recommended blend built at `:382-387` |
| **Phase 19 regime gate** | CSP1 constituent breadth (**50d** MA — `compute_breadth.py:90` `ma_breadth`) with hysteresis: de-risk 50% of NAV to SHY below 20%, re-engage above 50%; 5 bps per flip | thresholds `run_risk_overlay.py:100-104`; state machine `:164-176`; breadth source `:308-311` |
| **Phase 22 EEM tilt** | EEM/SPY ratio 50d/200d golden cross → tilt 10% of NAV to EEM, funded from B (35→25) | params `run_risk_overlay.py:120-127`; signal `:213-224`; blend math `:227-280` |

### Deployed headline (verified from committed JSONs, common window 2018-11-08 → 2026-06-18)

| Track | Sharpe | CAGR | MaxDD |
|---|---:|---:|---:|
| A standalone | +1.01 | +18.6% | −30.6% |
| B standalone | +1.03 | +11.7% | −13.3% |
| C standalone | +0.75 | +16.7% | −36.1% |
| D standalone | +0.87 | +14.8% | −32.5% |
| Ungated 35/35/10/20 | **+1.20** | +15.8% | −23.8% |
| + Phase 19 gate | +1.29 | +15.4% | −16.4% |
| + Phase 22 tilt (deployed) | +1.29 | +15.5% | −16.3% |

EEM tilt: 11 switches ever, 29.3% of days ON — few distinct bets (WS3 item).

### Drift found (ground-truth block / README vs code)

1. **Sleeves A and D use 200d constituent breadth, not 50d.** The prompt's
   ground-truth block cites `compute_breadth.py MA_PERIOD=50`, but that module
   only feeds the legacy composite signal and the `ma_breadth` series in
   `breadth_*.json`. The deployed sleeves compute breadth from constituent
   parquet caches via `compute_ma200_breadth` with `MA_PERIOD=200`
   (`run_ma200_sweep.py:55`, consumed by `run_portfolio.py:45,105` →
   `run_topk_robustness.py` / `run_europe_rotation.py`).
   **The 50d breadth IS what the Phase 19 gate uses** (`run_risk_overlay.py:309`
   reads `breadth_csp1.json → series.ma_breadth` = share above 50d MA). So the
   architecture has TWO breadth horizons: 200d for sleeve selection, 50d for
   the regime gate. Workstream 1's sweep must treat them separately.
2. **D is absolute breadth, A is relative.** `run_europe_rotation.py:194`
   passes the raw breadth panel; only A applies the cross-sectional demean
   (`run_topk_robustness.py:82`). The prompt implies both are relative.
3. **C: K=5 since Phase 27** (`run_thematic_rotation.py:312`), not K=4; README
   (Phase 24) says K=4 with 23 names. Universe is 25 (PHO, IHI added Phase 25).
4. **C has a Phase 27 sleeve-breadth gate** (30% threshold, exit to SHY) that
   post-dates the README and is absent from the prompt's ground-truth block.
   It is an extra regime overlay inside the sleeve.
5. **A's universe is 13 tradeable lines** (14 registered minus IUIT pruned at
   `etf_registry.py:591` for 0.97 corr with CNDX). README's "14" counts the
   pruned line. Includes non-sector slices SOXX, CSP1, CNDX, IDP6 — so "US
   sectors" is really "US sectors + broad-cap + semis + small-cap".
6. Existing robustness evidence covers the **legacy single-ETF 50/150
   strategy**, not the deployed sleeves: the MA-period sweep {100..300}
   (`run_robustness.py:576-616`) and walk-forward L are on CSP1/SOXX Family-D.
   No parameter surface exists for the deployed top-K sleeve horizons — that
   is the Workstream 1 gap.
7. Untracked `scripts/run_commodity_expansion.py` + `data/commodity_expansion.json`
   (2026-07-01): commodity-SPOT variants for B/C, review-and-propose stage.
   Relevant to Workstream 2, not deployed.

### Existing harness to reuse (not reinvent)

- Sub-period grid (7 regimes 2019→2026): `run_robustness.py:68-76`.
- Block-bootstrap Sharpe CI: `run_robustness.py:305-332`.
- Walk-forward K per sleeve: `run_asset_class_rotation.py:422-499`,
  `run_robustness.py:389-484`.
- Split-half precedent (train/test at 2022-09): `run_split_half.py`.
- Sleeve engines importable without modification: `run_portfolio.run_portfolio`
  + `top_k_breadth_weight`; `run_asset_class_rotation.run_rotation` +
  `top_k_by_signal`; `run_thematic_rotation.run_rotation` +
  `top_k_equal_weight` + loaders; `run_europe_rotation._fx_convert_eur_to_usd`;
  `run_multi_strategy.fixed_blend_4way`.

---

## Ranked plan (Workstreams 1-3)

Ranked by expected OOS-expectancy impact per unit of added complexity:

1. **WS1.1 — MA-period parameter surface for the deployed sleeves and blend**
   (this session). No such surface exists (drift item 6); everything sits on
   an untested single point (200d) inherited from Phase 3. Highest
   information-per-token: if 200d is a sharp peak the whole system is
   fragile; if flat, the invariant is confirmed and 200d stays.
2. **WS1.2 — vol-normalised common-horizon ensemble (2a + 3a jointly)** (this
   session). Candidate replacement formulation with zero per-asset knobs;
   directly addresses the BTC/TLT horizon-heterogeneity critique. Kept only if
   it clears the cheap-reflex OOS bar vs the deployed baseline.
3. **WS1.3 — vol-targeted sizing (3b) and slope gate (3c), each separately**
   (this session). One added degree of freedom each; sized bets only if OOS
   split-half AND sub-period consistency improve.
4. **WS2.1 — EEM coherence + overlap control** (next session). EEM double-count
   is a live architectural incoherence; correlation-cluster rule is cheap to
   compute from existing caches.
5. **WS2.2 — trend-opportunity map + country-momentum sleeve evaluation**
   (next session). Uses registry country scaffolding (IJPN NDIA ICHN ITWN
   caches already on disk). Add only if top-K country momentum beats EEM+EFA
   net of cost.
6. **WS2.3 — universe adds/drops with data-integrity gates** (next session).
7. **WS3 — heavy gate ONCE on the frozen shortlist** (final session):
   deflated/haircut Sharpe over ~28 phases of trials, full-system walk-forward
   (weights, K, gate thresholds, tilt windows), cost stress 1x/2x/3x, EEM-tilt
   bet-count audit, entry-point discipline check.

---

## Workstream 1 — moving-average robustness (session 1 results)

### Method

- New scripts: `scripts/ws1_common.py`, `scripts/run_ws1_ma_surface.py`,
  `scripts/run_ws1_vol_variants.py`. Artefacts: `data/ws1_ma_surface.json`,
  `data/ws1_vol_variants.json`, `data/ws1_fx_eurusd_cache.parquet`.
- Deployed engines imported unchanged (look-ahead protection inherited);
  ONE fixed evaluation window for every variant: **2018-11-08 → 2026-06-16**
  (end bound by the EU constituent caches), split-half at **2022-09-08**
  (`run_split_half.py` precedent); deployed per-sleeve costs (A 2 / B 2 /
  C 5 / D 9 bps one-way) plus a 2x-cost stress on every run; sub-period grid
  copied from `run_robustness.py:68-76`.
- Object of comparison: the UNGATED 35/35/10/20 blend. The Phase 19 gate
  (50d), EEM tilt windows and all overlay parameters are deliberately out of
  scope here — they are WS3's overlay audit. Stated assumption: sleeve-horizon
  conclusions do not depend on the overlays, which sit on top of the blend.
- Regression check: the harness reproduces the committed track at W=200
  (blend +1.196 vs committed +1.202; per-sleeve within ±0.02 — differences
  are the two-day window-end mismatch).
- Cheap-reflex bar per variant (all three to KEEP): OOS test-half ΔSharpe ≥ 0;
  ≥4 of 6 full sub-periods not worse than baseline; survives 2x cost.

### 1. Parameter surface — the 200d point sits on a one-sided plateau

Full-window Sharpe by lookback (fixed window, deployed costs):

| W | 50 | 75 | 100 | 125 | 150 | **200 (deployed)** | 250 | 300 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| A | +0.81 | +0.77 | +0.81 | +0.85 | +0.91 | **+0.99** | +1.02 | +1.02 |
| B | +0.81 | +0.83 | +0.78 | +0.88 | +0.84 | **+1.01** | +1.07 | +1.09 |
| C | +0.63 | +0.63 | +0.64 | +0.52 | +0.72 | **+0.73** | +0.72 | +0.77 |
| D | +0.55 | +0.52 | +0.67 | +0.69 | +0.74 | **+0.87** | +0.89 | +0.82 |
| Blend | +0.94 | +0.92 | +0.96 | +1.00 | +1.06 | **+1.20** | +1.23 | +1.22 |

Blend split-half: train (2018-11→2022-09) rises monotonically +0.67 → +1.00
across the grid; test (2022-09→2026-06) +1.29 → +1.52. **Both halves prefer
slow.** Train/test rank correlation across the grid: blend +0.93, D +0.81,
A +0.71, B +0.57, **C +0.05 (noise)**.

Findings:

- **The surface is one-sided, not peaked.** Fast lookbacks (50-125) are
  uniformly worse; the slow side (200-300) is flat: blend spread over
  {200,250,300} is 0.035 Sharpe against a Sharpe SE of ±0.4. 200d is not a
  lucky peak — it is the edge of a plateau whose flat direction extends to 300.
- **The 2022 inflation shock is the discriminator**: blend sub-period Sharpe
  at W=50..150 is −0.51..−0.84; at 200/250/300 it is −0.09/−0.11/−0.03. Fast
  signals chop-traded 2022; slow ones sidestepped it. Every fast W beats 200
  in only 1/6 regimes; 250 and 300 beat 200 in 4/6 but by small margins.
- **Costs do not decide anything at weekly cadence**: 2x cost moves full-window
  Sharpe by ≤0.03 at W≥150 (worst case D@50 −0.11). Turnover falls with W
  (A 30x@50 → 14x@300), so slow is also the cheap direction.
- **C's horizon is statistically noise** (rank corr +0.05, non-monotone
  surface). Do not ever tune C's window separately; it stays on the common
  horizon by parsimony.
- **Verdict: keep 200d.** The only defensible alternative reading: 250 is the
  literal flat-middle (200 borders the falling fast shoulder), beats 200 in
  4/6 regimes and in both halves, at lower turnover — but the gain (+0.03
  blend Sharpe) is far inside noise, and changing a deployed parameter after
  peeking at this surface is exactly the sequential-tuning failure mode the
  review exists to stop. If pursued at all, "common horizon 200 vs 250"
  should enter WS3's full-system walk-forward as a re-fit parameter and be
  decided there, not here.

### 2. Vol-normalised / ensemble variants — all fail on contact

Decomposition (each step isolated; fixed window; sleeve-level full Sharpe,
deployed baseline in bold):

| Variant | A | B | C | D |
|---|---:|---:|---:|---:|
| **V0 deployed (binary breadth / raw distance, 200d)** | **+0.99** | **+1.01** | **+0.73** | **+0.87** |
| V1a graded raw distance @200 (grading alone, A/D) | +0.88 | — | — | +0.65 |
| V1 vol-normalised z @200 (item 3a) | +0.82 | +0.96 | +0.69 | +0.56 |
| V3 ensemble {50,100,150,200}, no vol (item 2a alone) | +0.88 | +0.84 | +0.65 | +0.67 |
| V2 vol-normalised ensemble (items 2a+3a joint — prompt-preferred) | +0.75 | +0.78 | +0.72 | +0.43 |

- **16 of 17 sleeve-level variants KILLED** by the cheap reflex: test-half
  deltas negative nearly everywhere (A −0.19..−0.37, B −0.12..−0.31,
  C −0.08..−0.09, D −0.16..−0.18 for V1/V2), sub-period consistency ≤3/6,
  and 2x cost makes each strictly worse (ensembles raise turnover 25-90%).
- **Attribution is clean: every step hurts independently.** Grading the
  breadth count hurts (A −0.11, D −0.21); vol-normalising hurts more
  (shrinks exactly the strong-trend, high-vol names the top-K should hold);
  multi-horizon ensembling hurts (blends in the fast horizons the surface
  already showed are inferior). Composing them (V2) is worst: the all-V2
  blend is **+0.89 vs +1.20 deployed**.
- Sleeve C detail: variants changed only the top-K ranking (floor and Phase 27
  gate kept on the deployed raw-200d panel — zero re-tuned knobs), and still
  lost. D detail: graded signals introduce an implicit cash floor (93-94%
  invested) and still lose — the damage is concentrated in the train half
  (D graded train −0.01..−0.28 vs +0.41 deployed).
- **Fallback 2(b) — bucketed horizon groups — skipped, evidence-led.** Two
  grounds: (i) ranking across buckets with different windows inside one top-K
  requires vol-normalised magnitudes to be comparable, and vol-normalisation
  just failed decisively; (ii) the premise (fast assets want fast windows) is
  contradicted by the data — B, the genuinely cross-asset sleeve (bonds, gold,
  commodity, EM, equities), is monotone toward SLOW for the whole basket, and
  C (crypto + thematics, the nominal "fast" bucket) shows a noise-level
  horizon preference, mildly slow if anything. Cross-asset horizon
  heterogeneity is a real theoretical concern that this system's data simply
  does not exhibit at the sleeve level. Re-open only if WS2 adds a country
  sleeve.

### 3. Overlays (each tested separately, on deployed-baseline sleeves)

- **S1 vol-targeted sleeve sizing (3b): KILL.** Blend +1.181 vs +1.196
  (full −0.015, test −0.001, consistency 2/6). It drifts average weights to
  B (43%) at the expense of A (31%) — de-rating the highest-CAGR sleeve for
  no OOS payoff. Adds a knob, pays nothing.
- **S2 slope gate on B (3c): passes the formal bar, do not deploy.** Sleeve
  +0.008 full / +0.009 test, consistency 4/6; blend +0.006 full / +0.021
  test. The one survivor of seventeen variants — and the effect is a rounding
  error. Fewer-knobs-wins-ties says leave it; logged as a WS3 candidate to
  re-check on the frozen shortlist, not a deployment.
- **S2 slope gate on C: KILL** (−0.017 full, −0.087 test).

### WS1 bottom line

The deployed single-horizon 200d formulation — binary constituent breadth for
A/D, raw MA-distance for B/C — survives an 8-point parameter surface, a
17-variant vol/ensemble decomposition and two overlay tests without a single
economically meaningful OOS improvement appearing. Both design invariants are
confirmed rather than revised: the deployed point sits on a flat (one-sided)
plateau, and every added degree of freedom failed the OOS bar. **Recommended
change to the MA formulation: none.** The valuable outputs are the surface
itself (now on file for WS3's deflated-Sharpe audit), the 2022-regime
evidence against ever shortening the horizon, and the C-horizon-is-noise
result. Next session: Workstream 2 (universe) per the ranked plan.
