# Research memo — strategy review (REVIEW_PROMPT.md)

Running memo across review sessions. Session 1 covers Workstream 0 (orient) and
Workstream 1 (moving-average robustness). Session 2 (same day) covers
Workstream 2 (universe). Session 3 (2026-07-03) covers Workstream 3 (heavy
robustness gate on the frozen shortlist) — see the Workstream 3 section.

- Started: 2026-07-02
- Data as of: caches through 2026-06-16 (EU constituents) to 2026-07-01 (US);
  committed JSONs from the 2026-07-02 weekly refresh. Session 2 adds a fresh
  candidate panel (`data/ws2_prices_cache.parquet`, fetched 2026-07-02).
- Constraint honoured: no edits to `template.html`, `docs/`, or any deployed
  `scripts/run_*.py`. All experiments in NEW scripts (`scripts/run_ws1_*.py`,
  `scripts/ws1_common.py`; session 2: `scripts/run_ws2_*.py`,
  `scripts/ws2_common.py`) writing JSON to `data/ws1_*.json` / `data/ws2_*.json`.
- Filed records: [`reviews/2026-07-02_ws0-ws1_ma-robustness.docx`](reviews/2026-07-02_ws0-ws1_ma-robustness.docx),
  [`reviews/2026-07-02_ws2_universe.docx`](reviews/2026-07-02_ws2_universe.docx),
  detailed test appendix
  [`reviews/2026-07-02_ws2_test-appendix.docx`](reviews/2026-07-02_ws2_test-appendix.docx),
  plain-language summary
  [`reviews/2026-07-03_ws2_universe_summary.docx`](reviews/2026-07-03_ws2_universe_summary.docx),
  [`reviews/2026-07-03_ws3_heavy-gate.docx`](reviews/2026-07-03_ws3_heavy-gate.docx)
  with plain-language summary
  [`reviews/2026-07-03_ws3_heavy-gate_summary.docx`](reviews/2026-07-03_ws3_heavy-gate_summary.docx)
  (supersedes the misdated `2026-07-02_ws3_heavy-gate.docx` — the WS3 session
  ran 2026-07-03; naming convention
  `reviews/<yyyy-mm-dd>_<workstreams>_<topic>.docx`).

---

## Workstream 0 — signal map (code-verified, with file:line)

### Sleeve engines

| Sleeve | Signal | Horizon | Selection / weighting | Universe | Cost | File:line |
|---|---|---|---|---|---|---|
| **A — US sectors (35%)** | Constituent breadth: share of constituents above their own **200d** MA, made sector-RELATIVE (sector minus cross-sectional mean per date) | 200d (`MA_PERIOD` imported from `run_ma200_sweep.py:55`) | Top K=7 by relative breadth, weight by positive-relative share (`top_k_breadth_weight`), weekly Friday | 14 ETFs: SOXX CSP1 CNDX IDP6 + 10 iShares UCITS sector slices (IUIT pruned) traded via SPDR proxies (`etf_registry.py:583-605`) | 2 bps (`run_topk_robustness.py:53`) | breadth calc `run_ma200_sweep.py:117-150`; relative transform `run_topk_robustness.py:75-82`; weight fn `run_portfolio.py:199-253`; K=7 `run_topk_robustness.py:92` |
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
5. **A's universe is 14 tradeable lines** (15 registered minus IUIT pruned at
   `etf_registry.py:591` for 0.97 corr with CNDX). *Correction (session 2):
   the WS0 note said 13 and mis-attributed an error to the README — a recount
   of `UNIVERSE_ETFS` confirms 14, matching the README.* Includes non-sector
   slices SOXX, CSP1, CNDX, IDP6 — so "US sectors" is really "US sectors +
   broad-cap + semis + small-cap".
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
   *Status: items 4-6 completed in session 2 — see the Workstream 2 section.*
7. **WS3 — heavy gate ONCE on the frozen shortlist** (final session):
   deflated/haircut Sharpe over ~28 phases of trials, full-system walk-forward
   (weights, K, gate thresholds, tilt windows), cost stress 1x/2x/3x, EEM-tilt
   bet-count audit, entry-point discipline check.

---

## Workstream 1 — moving-average robustness (session 1 results)

### Method

- New scripts: `scripts/ws1_common.py`, `scripts/run_ws1_ma_surface.py`,
  `scripts/run_ws1_vol_variants.py`, `scripts/run_ws1_wf_horizon.py`,
  `scripts/run_ws1_threshold_surface.py`, `scripts/plot_ws1_surface.py`,
  `scripts/plot_ws1_thresholds.py`.
  Artefacts: `data/ws1_ma_surface.json`, `data/ws1_vol_variants.json`,
  `data/ws1_wf_horizon.json`, `data/ws1_threshold_surface.json`,
  `data/ws1_ma_surface.png`, `data/ws1_ma_dd_surface.png`,
  `data/ws1_threshold_surface.png`, `data/ws1_fx_eurusd_cache.parquet`.
- Lookback grid densified 2026-07-02 from 8 to **13 points (25d steps,
  25→325)** for surface-shape resolution. Grid size is a trial count for
  WS3's deflated-Sharpe audit: log 13 lookbacks x 4 sleeves + blend as
  evaluated (not selected) configurations.
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

**Charts: [`data/ws1_ma_surface.png`](data/ws1_ma_surface.png)** (small
multiples per sleeve + blend with full/train/test lines, plateau band and
deployed marker, plus a sleeve x W heatmap) and
**[`data/ws1_ma_dd_surface.png`](data/ws1_ma_dd_surface.png)** (blend
drawdown surface). Regenerate with `python scripts/plot_ws1_surface.py`.

Full-window Sharpe by lookback (fixed window, deployed costs; table shows
the original 8 columns — the JSON/charts carry the full 13-point grid):

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
- **Dense-grid addendum (13 points, 25d steps).** The new points interpolate
  smoothly (blend: 175d +1.14, 225d +1.18, 275d +1.24, 325d +1.22) — no
  hidden spikes between the original points, which is itself evidence against
  a curve-fit artefact. On the finer grid the blend plateau band (within 0.05
  of the peak) is 250-325 with the peak at 275 (+1.24); deployed 200 sits
  0.04 below the band's edge, at the top of the rising shoulder. Train/test
  rank correlations on 13 points: blend +0.88, B +0.88, A +0.67, D +0.67,
  **C −0.41** — C's horizon preference is confirmed noise, now with the sign
  flipping on a denser grid. W=25 on C is degenerate (the fixed +5% floor
  leaves the sleeve mostly in cash), documented as formulation breakdown, not
  signal.
- **Verdict: keep 200d.** The only defensible alternative reading: the
  flat-middle of the dense-grid plateau is ~275 (200 borders the falling
  fast shoulder); 250-275 beat 200 in 4/6 regimes and in both halves, at
  lower turnover — but the gain (+0.03-0.04 blend Sharpe) is far inside
  noise, and changing a deployed parameter after peeking at this surface is
  exactly the sequential-tuning failure mode the review exists to stop. If
  pursued at all, "common horizon 200 vs 250-275" should enter WS3's
  full-system walk-forward as a re-fit parameter and be decided there, not
  here.

### 1b. Drawdown surface — slow lookbacks are also the drawdown-safe direction

Question raised 2026-07-02: does any other calibration work better on
drawdown? Richer DD metrics added to the surface (`ws1_common.dd_metrics`:
worst rolling-12m return, longest underwater spell, DD measured within the
2020 COVID and 2022 crash windows). Blend row:

| Blend | W=50 | 75 | 100 | 125 | 150 | **200** | 250 | 300 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Max DD | −22.5% | −22.1% | −23.0% | −23.9% | −24.3% | **−23.8%** | −23.8% | −24.3% |
| Worst 12m | −14.8% | −18.2% | −16.6% | −14.4% | −13.0% | **−11.9%** | −10.1% | −10.0% |
| Underwater (days) | 427 | 578 | 431 | 425 | 425 | **163** | 395 | 298 |
| DD in 2022 | −17.3% | −19.6% | −20.2% | −17.9% | −17.0% | **−14.2%** | −13.2% | −13.8% |

Findings:

- **Max DD is horizon-invariant** (−22% to −24% across the whole grid)
  because the binding drawdown is COVID 2020 at every W: a −30%-in-23-days
  crash is faster than ANY moving average in the grid. Sleeve MA calibration
  is structurally the wrong tool for that event — the fast (50d) Phase 19
  gate layer is the right tool, and its audit is WS3.
- **Every conditionable DD metric prefers slow.** Worst rolling 12m improves
  monotonically from −14.8% (W=50) to −10.0% (W=300); 2022 drawdown is
  −17/−20% fast vs −13/−14% slow; per-sleeve minima cluster at 200
  (B worst-12m −5.8%, C max DD −36.1% — C's Phase 27 gate composes best with
  the 200d horizon). The deployed 200d has the shortest underwater spell on
  the grid (163 trading days), though the dense grid shows neighbours 175/225
  at ~270 days — the robust statement is "the slow half recovers materially
  faster than the fast half (270-300d vs 400-580d)", not that 200 is a
  special point.
- **The one place fast wins is COVID for the momentum sleeves** (B@50 −11.0%
  vs B@200 −13.3%; C@50 −19.3% vs C@200 −32.5%): fast signals do exit
  no-warning crashes sooner, but pay for it in every choppy grind. Two crash
  regimes, opposite verdicts — which is precisely the division of labour the
  deployed architecture already encodes: slow horizons for selection, a fast
  50d breadth gate for de-risking. The DD surface therefore confirms the
  two-horizon design rather than suggesting a recalibration.

### 1c. Walk-forward horizon selection — re-fitting LOSES to fixed 200d

Prompted by the chart-reading question (2026-07-02): "the plateau at 250-325
looks worth capturing". The disciplined version of that instinct is an annual
re-fit that may only use data available at each refit date. Protocol: refits
at each year-end 2021-2025 on expanding windows; W chosen from the 13-point
grid by train Sharpe (blend-level for `wf_common`, per sleeve for
`wf_per_sleeve`); 100% one-way turnover charged per sleeve on every W change;
all protocols evaluated on the IDENTICAL OOS window 2022-01-03 → 2026-06-16.
Artefact: `data/ws1_wf_horizon.json` (script `run_ws1_wf_horizon.py`).

| Protocol | OOS Sharpe | Ws used |
|---|---:|---|
| fixed 200 (deployed) | **+1.183** | 200 |
| fixed 250 | +1.181 | 250 |
| fixed 275 | +1.227 | 275 |
| wf_common (annual re-fit) | +1.170 | 275→325 |
| wf_per_sleeve (annual re-fit) | +1.107 | 25…325 |
| oracle (hindsight) | +1.227 | 275 |

Reading: the hindsight optimum IS 275 (+0.044 over 200) — but no re-fit
process run on information available at the time captures it. Re-fitting the
common horizon annually UNDERPERFORMS never touching 200 (+1.170 vs +1.183),
and per-sleeve re-fitting — more knobs — is worse again (+1.107; one refit
picked W=25, noise-chasing in action). The plateau's gradient is smaller than
the selection variance of any honest calibration process. This upgrades
"keep 200d" from "the alternative is inside noise" to "recalibration
demonstrably loses out of sample".

### 1d. Can the parameter be IMPROVED, not just trusted? (2026-07-03)

Question raised after the review: beyond robustness, is a better lookback
being left on the table? Paired daily-return test of the surface's best
rivals against the deployed 200d blend (`run_ws1_paired_test.py`,
`data/ws1_paired_test.json`) — the paired form is the sharpest available
test because the variants hold near-identical portfolios (daily-return
correlation 0.99), which shrinks the standard error far below the ±0.4 of
a headline-Sharpe comparison:

| Rival | Ann. return edge | t (NW-10) | Data needed for 2σ |
|---|---:|---:|---:|
| 250d | +0.62%/yr | +1.07 | ~26 years |
| 275d | +0.83%/yr | +1.20 | ~21 years |

Both rivals sit around one standard error from zero on 7.6 years — BEFORE
any multiple-testing haircut, and each was selected as the max of 13
trials, so the honest expected edge is lower still. Combined with 1c (every
real-time capture process loses money), the answer is: **no — the lookback
has no harvestable improvement at this sample size.** The improvement
budget lives elsewhere (Sleeve C quality, universe composition, execution),
not in the trend parameter. Re-visit only on a material universe change
(re-run the surface as a check) or after roughly another decade of data;
choosing 250-300d on a regime FORECAST (persistent 2022-like chop) would be
a discretionary macro bet to be named and logged as such, not calibration.
(Trials logged: 2 paired comparisons.)

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

### 4. Threshold surfaces — Phase 19 gate flat (keep); C floor/gate bumpy

First surfaces ever built for the deployed thresholds (2026-07-02, prompted
by the calibration question). Artefacts: `data/ws1_threshold_surface.json`,
chart [`data/ws1_threshold_surface.png`](data/ws1_threshold_surface.png)
(scripts `run_ws1_threshold_surface.py`, `plot_ws1_thresholds.py`). Trials
logged for WS3: 25 C cells + 25 gate cells + 5 WF-horizon protocols.

- **Phase 19 gate (off x on hysteresis): FLAT — the in-sample-tuning caveat
  is materially defused.** All 25 valid pairs land Sharpe +1.22 to +1.33
  against +1.20 ungated, and EVERY cell beats ungated on both Sharpe and max
  DD (−14.7/−16.4% vs −23.8%). Deployed (20%/50%) sits mid-plateau at
  +1.287. Earlier de-risking (off 25-30%) buys ~1.7pp shallower DD at 2-3x
  the switch count — a taste choice, not an edge. Whatever pair the Phase 19
  12-variant sweep had picked, the outcome would have been similar: the
  gate's value is structural (de-risk on breadth collapse), not parameter
  luck. Keep 20/50.
- **Sleeve C floor x gate: genuinely bumpy — and the attractive ridge is
  train-concentrated.** The high-gate cells that look best full-window
  (e.g. floor 0%/gate 50%: +0.97) earn it in the train half (+1.23) and fade
  to +0.72 OOS; the high-floor/high-gate corner is degenerate (average
  invested share down to 24% — SHY wearing a thematic badge). This is
  independent confirmation of the Phase 27 bake-off finding that the 50%
  gate fails OOS. Deployed (5%/30%) has the smallest train/test gap in its
  neighbourhood.
- **One pre-registered candidate passed the cheap reflex: REMOVE the +5%
  floor, keep the 30% gate.** (0%,30%) vs deployed: full +0.78 vs +0.74,
  test +0.83 vs +0.69, 5/6 regimes, survives 2x cost, one knob fewer, lower
  turnover. BUT max DD degrades −36% → −48%, with the floor's entire value
  concentrated in the 2022 grind (sub-period Sharpe −2.49 vs −0.94).
  Sharpe says remove, drawdown says keep. Verdict: leave deployed; "drop C
  floor" goes on the WS3 shortlist with the trade-off stated, to be
  re-examined after WS2 potentially restructures C's universe.

### WS1 bottom line

The deployed single-horizon 200d formulation — binary constituent breadth for
A/D, raw MA-distance for B/C — survives an 8-point parameter surface, a
17-variant vol/ensemble decomposition and two overlay tests without a single
economically meaningful OOS improvement appearing. Both design invariants are
confirmed rather than revised: the deployed point sits on a flat (one-sided)
plateau, and every added degree of freedom failed the OOS bar. **Recommended
change to the MA formulation: none.** The drawdown surface (1b) reaches the
same verdict independently: max DD is horizon-invariant (COVID binds at every
W), every conditionable DD metric prefers slow, and the deployed 200d holds
the grid's shortest underwater spell. The walk-forward test (1c) closes the
calibration question outright: annually re-fitting the horizon on the
plateau would have UNDERPERFORMED fixed 200 out of sample (+1.170 vs
+1.183), and per-sleeve re-fitting is worse again. The threshold surfaces
(4) clear the Phase 19 gate (flat plateau, in-sample-tuning caveat defused,
keep 20/50) and put exactly one candidate on the WS3 shortlist: dropping
C's +5% floor (better Sharpe OOS, one knob fewer, but −12pp max DD
concentrated in 2022). The valuable outputs are the surfaces themselves
(now on file for WS3's deflated-Sharpe audit), the 2022-regime evidence
against ever shortening the horizon, the C-horizon-is-noise result, and the
confirmed slow-selection / fast-gate division of labour. Next session:
Workstream 2 (universe) per the ranked plan.

---

## Workstream 2 — universe (session 2 results)

### Method

- New scripts: `scripts/ws2_common.py` (deployed-baseline cache at W=200,
  regression-checked — rebuilt ungated blend Sharpe **+1.196**, identical to
  the WS1 harness), `scripts/run_ws2_fetch_panel.py`,
  `run_ws2_correlation.py`, `run_ws2_country_sleeve.py`,
  `run_ws2_commodity_fixed.py`, `run_ws2_prune_tests.py`,
  `run_ws2_eem_coherence.py`, `run_ws2_trend_map.py`.
  Artefacts: `data/ws2_*.json`, charts
  [`data/ws2_correlation.png`](data/ws2_correlation.png) /
  [`data/ws2_trend_map.png`](data/ws2_trend_map.png), caches
  `data/ws2_prices_cache.parquet`, `data/ws2_baseline_*.parquet`,
  `data/ws2_ticker_verification.json`.
- Same fixed window (2018-11-08 → 2026-06-16), split (2022-09-08), deployed
  costs + 2x stress and sub-period grid as WS1. Every add/drop was
  PRE-REGISTERED from correlation or scope evidence before its performance
  was seen, and judged on the cheap reflex with the BLEND-level delta as the
  decision number. Kill on contact.
- Ticker verification against 2+ sources (yfinance metadata,
  `data/ishares_catalogue.csv`, issuer/aggregator pages): all candidates
  verified. **FM (iShares Frontier and Select EM) is a dead fund** — last
  price 2025-01-08, liquidation completed 2025-01 — caught by the
  verification gate and excluded. Frontier is currently uninvestable via
  clean US-listed ETFs.

### 1. Overlap control — rule, matrix, look-through

Artefacts: `data/ws2_correlation.json`,
[`data/ws2_correlation.png`](data/ws2_correlation.png). 72 lines (all sleeve
members incl. D converted EUR→USD with the deployed FX series, plus all
candidates), weekly W-FRI returns, 397 obs full window + trailing 52.

**The rule (adopted):** cluster at pairwise weekly correlation ≥0.80 on the
full window (connected components); keep the most liquid/representative line
per cluster; **reject any CANDIDATE >0.90 to an incumbent** unless it adds
exposure the incumbent cannot express (IUIT/CNDX 0.97 prune is the
precedent). Incumbents are NOT auto-dropped by correlation: a deployed name
is removed only when OOS evidence positively supports the drop (see the
prune tests — correlation is necessary but not sufficient, the Phase 16 SLV
lesson in both directions).

Findings — 18 pairs >0.90 full-window, of which the load-bearing ones:

| Pair | Corr | Reading |
|---|---:|---|
| EEM / IEMG | 0.998 | candidate IEMG auto-rejected |
| XLRE(A) / VNQ(B) | 0.990 | cross-sleeve dual-signal REIT — deliberate, quantified |
| EFA / VGK (both B) | 0.984 | within-B duplicate → prune tested (P1) |
| VGK / EWG, EFA / EWG, VGK / EWU | 0.930-0.966 | candidates EWG, EWU auto-rejected |
| DBC / GSG, DBC / DBE | 0.939-0.958 | commodity adds duplicate incumbent DBC |
| XLI(A) / PAVE(C) | 0.954 | thematic that is A's industrials beta → prune tested (P2) |
| SPY(A,B) / QQQ(A,B) | 0.930 | the deployed dual-coverage core |
| ICLN / TAN, CIBR / SKYY (C) | 0.930 / 0.901 | within-C duplicates → prune tested (P2) |
| TLT / IEF (B) | 0.918 | deliberate duration ladder — keep |

Trailing-1y additions (context, no decisions): EEM/EWT 0.908 and EEM/EWY
0.906 — Taiwan and Korea have converged onto broad EM (EM is currently a
semis trade), further undermining the "distinct exposure" case for a country
sleeve; IEF/SHY 0.901 (short-end compression).

**Intra-blend look-through** (ungated 35/35/10/20, sleeve weight panels,
weekly drift ignored): the US-beta cluster (28 blend lines link into one
component at ≥0.8) averages **46.8% of NAV, peaking at 83.5%**; QQQ alone
averages 6.8% with a **24.1% peak** (held by A and B simultaneously in 43%
of weeks); SPY 4.0% mean / 10.4% max; IJR 2.1% / 13.7%. Stance recorded:
cross-sleeve same-beta duplication (SPY/QQQ/IJR, XLRE-vs-VNQ) is the
architecture's deliberate signal diversification — two different signals on
the same beta — and is retained; the look-through concentration number is
the honest cost of that choice and goes to WS3 as context.

### 2. Pre-registered within-sleeve prunes — both REJECTED

`data/ws2_prune_tests.json`. Exactly two bundles, fixed from correlation
evidence alone:

- **P1 — B drop VGK** (0.984 to EFA): a wash. Sleeve dFull +0.007 /
  dTest −0.029 / 3 of 6; blend dFull +0.004 / dTest −0.003 / 3 of 6.
  Fails the keep bar → **no change**; a deployed-config change on a wash
  loses to the incumbent (entry-point discipline).
- **P2 — C drop {TAN, SKYY, PAVE}** (0.930/0.901 within-C; PAVE 0.954 to
  XLI): actively harmful. Sleeve dFull **−0.111** / dTest +0.006 / 1 of 6,
  max DD −40.7% vs −36.1%; blend dFull −0.021 / 1 of 6. The dropped names
  carried C's train half (2020 solar, 2021 infrastructure) → **no change**.
  Corollary recorded: correlation redundancy alone is not sufficient grounds
  to REMOVE an incumbent, mirroring the SLV lesson that a passed corr gate
  is not sufficient grounds to ADD one.

### 3. Country momentum sleeve — KILLED on the pre-registered bar

`data/ws2_country_sleeve.json`. Design (pre-registered): the deployed B
formulation — graded (close−MA200)/MA200, top-K by signal share among
positive names, SHY deficit floor (which IS the own-200d risk gate) — on
U10 = EWZ EWW EWY INDA EWT EWA EWS EWG EWU EWJ; headline K=3 (Idea 3 /
Phase 23 precedent), 5 bps one-way incl. cash legs (conservative), U11
(+EEM) and K∈{2,4} reported not selected. Bar: beat 50/50 EEM+EFA
(weekly-rebalanced, 2 bps) on full AND test AND ≥4/6 sub-periods AND at 2x
cost.

| Config | Full | 2x | Train | Test | MaxDD | Turn |
|---|---:|---:|---:|---:|---:|---:|
| 50/50 EEM+EFA benchmark | +0.60 | — | +0.18 | +1.16 | −33.8% | — |
| U10 K=2 | +0.74 | +0.70 | +0.03 | +1.30 | −38.6% | 19.8x |
| **U10 K=3 (headline)** | **+0.70** | **+0.65** | **−0.04** | **+1.27** | **−35.3%** | 17.3x |
| U10 K=4 | +0.66 | +0.61 | −0.09 | +1.23 | −31.4% | 17.2x |
| U11+EEM K=3 | +0.66 | +0.61 | −0.10 | +1.25 | −35.3% | 18.3x |

Bar result: full ✓, test ✓, 2x ✓, **sub-periods 3/6 ✗ → KILL** (no blend
run). The failure shape is the instructive part: the train half is NEGATIVE
(−0.04) against the benchmark's +0.18 — the ranking flips across the split
and the entire edge sits in the 2022+ half. Long-window reconciliation with
the Idea 3 rejection (23y evidence, `test_phase22_eem_overlay.py` origin
note): the sleeve beats the benchmark 2004-2010 (0.91 vs 0.50) and 2022-now
(0.74 vs 0.61), loses 2011-2013 and 2014-2021. Country momentum is a
REGIME BET on EM leadership, not an all-weather edge — and the architecture
already expresses exactly that bet through the Phase 22 overlay at 10% with
11 switches. Adding a sleeve would duplicate the overlay with more knobs.
EEM inside the sleeve universe (U11) is strictly worse everywhere.

### 4. Commodity-spot thread — KILLED on the fixed window

`data/ws2_commodity_fixed.json` (re-run of the untracked 2026-07-01 thread
`scripts/run_commodity_expansion.py` + `data/commodity_expansion.json`).
Method review of the original: engine faithful (validated to 1e-9 against
the deployed engine — re-asserted), costs sensible (10 bps on adds, roll
inside ETF NAV), but `common_window()` inner-join truncated the C
comparison to 2020-11, the headline was MAR on its own windows, and there
was no split / sub-period / blend-level view. Its own numbers were already
uniformly negative (dMAR negative at EVERY start year 2008-2023, negative
even at 0 bps add-cost). Fixed-window re-run (end 2026-06-12, commodity
cache bound, baseline re-sliced identically):

| Variant | dFull | dTest | Consistency | MaxDD |
|---|---:|---:|---:|---:|
| B + {DBA,DBB,DBE} (sleeve) | −0.093 | −0.343 | 2/6 | −16.2% vs −13.3% |
| C + {DBC,DBA,DBB,DBE} (sleeve) | −0.015 | −0.142 | 2/6 | −46.7% vs −36.1% |
| Blend, B widened | −0.007 | −0.095 | 3/6 | — |
| Blend, C widened | +0.005 | −0.016 | 3/6 | — |
| Blend, both widened | −0.008 | −0.124 | 2/6 | — |

**KILL.** The widened B's train half (+1.15) exceeds its test half (+0.66):
the additions are a backward-looking bet on the 2021-22 commodity bull —
the in-sample-only improvement pattern the reflex exists to catch. The
correlation matrix independently shows DBE/GSG at 0.94-0.96 to incumbent
DBC: the adds are mostly duplicate beta with wider spreads. B's existing
GLD + DBC remain the commodity expression. Recommend deleting the untracked
thread artefacts or filing them as-rejected (owner's call at approval).

### 5. EEM coherence — DECISION: overlay-only

`data/ws2_eem_coherence.json`. 2x2 ablation on the fixed window, ungated
decision numbers (Phase 19 gate applied afterwards as context; gate
reimplementation validated at +1.286 vs committed +1.287):

| Variant | Full | Train | Test | MaxDD | Gated |
|---|---:|---:|---:|---:|---:|
| V0 status quo (EEM in B + tilt) | +1.202 | +0.920 | +1.540 | −23.6% | +1.279 |
| **V1 overlay-only (B w/o EEM + tilt)** | **+1.210** | +0.933 | **+1.544** | −23.6% | +1.289 |
| V2 B-member-only (no tilt) | +1.201 | +0.947 | +1.509 | −23.6% | +1.286 |
| V3 neither | +1.207 | +0.964 | +1.506 | −23.6% | +1.295 |

All four cells sit within 0.009 full-window Sharpe — statistically
indistinguishable — so the decision is ARCHITECTURAL, not performance-led:

- **Adopt V1 (overlay-only): remove EEM from Sleeve B's rotation universe
  (B becomes 12 rotation lines + SHY); the Phase 22 golden-cross tilt
  becomes the system's ONE designated EM expression.**
- Why: it eliminates a real double-count (look-through EEM peaked at
  **15.0% of NAV**; both roles held EEM simultaneously on 26% of days;
  mean look-through 4.5%); it is weakly dominant ungated (best full and
  test of the four cells, 5/6 consistency vs V0); B does not miss EEM
  (standalone +1.02 without vs +1.01 with; *corrected 2026-07-03: the
  overlap was NOT rare — B held EEM on 45% of days, mean 12% of the book
  when held, and on 88% of tilt-ON days both routes held it together,
  which is exactly the stacking the ablation shows added nothing*);
  and it PRESERVES the EM-turn thesis expression whose walk-forward
  validation is on file (`em_tilt_validation.json`: fixed 50/200 beats
  baseline OOS). Choosing V3 on its +0.006 gated edge would be sequential
  tuning on noise. The tilt's own keep/kill (11 switches ≈ few distinct
  bets) remains a WS3 audit item, deliberately NOT decided here.
- Deployment note (review-and-propose): the change touches
  `run_asset_class_rotation.py` UNIVERSE/TICKERS and downstream pipeline;
  patch to be proposed for approval, not applied in this session.
- **Deployment update (2026-07-02, later the same day): APPROVED by
  Zhenghao and LANDED as Phase 29.** EEM removed from the
  `run_asset_class_rotation.py` UNIVERSE (12 rotation lines + SHY); full
  pipeline chain re-run in the `refresh_all.py` order and
  `docs/index.html` + factsheet rebuilt; README / template / factsheet
  descriptions updated (B counts, EEM role, stale K=4/23-name C rows
  corrected to the verified map; equal-weight benchmark label de-numbered,
  JSON key kept for compatibility). Post-change deployed track
  (gated + tilted): Sharpe **+1.2956**, CAGR +15.5%, max DD −16.24%
  (previously +1.29 / +15.5% / −16.3% with EEM in B — inside noise, as
  the ablation predicted). B walk-forward K refit stays K=7 every year,
  WF Sharpe +0.77 (was +0.79). Verified: zero EEM entries across B's 926
  rebalances; tilt intact (EM_TILT_ON since 2025-04-07, 11 switches);
  180 pytest tests pass. WS3's baseline is therefore the NEW
  architecture; shortlist item S3 is closed.

### 6. Trend-opportunity map

Chart: [`data/ws2_trend_map.png`](data/ws2_trend_map.png)
(`run_ws2_trend_map.py`). Bottom line: the exposure space is either
covered, deliberately not covered, or was evaluated and killed this
session. Named GAPS with status: **frontier** (uninvestable — FM
liquidated); **non-US rates** (no clean USD-listed liquid vehicle set;
low priority); **styles/factors** (marginal by construction: QUAL 0.985
to SPY auto-rejects, USMV/MTUM/VLUE 0.889-0.896 sit ON the 0.9 flag
boundary, and a factor sleeve adds a factor-timing knob with weak prior —
defer with a written distinct-exposure bar); **ex-US DM sectors beyond
D's five** (widening needs point-in-time EU constituents — the expensive
data bucket; defer); **credit** (deliberate gap, HYG removal documented).
REDUNDANCIES are quantified in section 1 and either deliberate
(dual-signal core, duration ladder) or resolved (EEM double-count → V1;
candidate auto-rejects; prune bundles tested and rejected).

### Proposed target universe (WS2 deliverable — review-and-propose)

| Sleeve | Proposal | One-line rationale |
|---|---|---|
| A — US sectors (35%) | UNCHANGED: 14 lines (SOXX CSP1 CNDX IDP6 + 10 slices) | breadth on concentrated single-sector/cap slices is the signal's home turf; IUIT stays pruned |
| B — asset class (35%) | **ONE change: remove EEM** → 12 rotation lines + SHY | EEM's role moves to overlay-only; B unchanged otherwise (VGK wash, TLT/IEF deliberate ladder) |
| C — thematic (10%) | UNCHANGED: 25 names, +5% floor, 30% gate | prune bundle rejected on evidence; survivorship + capacity flags stand (BTC 25 bps, 159801.SZ 50 bps drag) |
| D — Europe sectors (20%) | UNCHANGED: 5 Stoxx supersectors | absolute breadth works here; widening blocked on point-in-time constituent cost |
| Overlays | Phase 19 gate unchanged; **Phase 22 tilt = the ONE EM expression** | gate cleared in WS1; tilt bet-count audit reserved for WS3 |
| Adds | **NONE** | countries killed (3/6), commodities killed (2/6), factors deferred-marginal, IEMG/EWG/EWU/DBE/GSG auto-rejected at >0.9 |

### WS2 trial register (deflated-Sharpe input for WS3)

Counting convention as WS1 (each evaluated configuration counts once; 2x
cost is a stress report of the same configuration; benchmarks count).

- Country sleeve: 6 sleeve configs (U10/U11 × K∈{2,3,4}) + 1 benchmark = 7
- Commodity fixed-window re-run: 2 sleeve + 3 blend splices = 5
- Commodity thread of 2026-07-01, retro-logged: 2 sleeve pairs + 7
  narrow-subset probes + 2 diversification sub-sleeves = 11
- Within-sleeve prunes: 2 sleeve + 2 blend splices = 4
- EEM coherence: 4 blend cells + 1 B-without-EEM sleeve = 5
- Correlation matrix / look-through / trend map / factor-corr check:
  descriptive, 0 trials

**Session 2 total: 21 new + 11 retro-logged = 32 trials.** Cumulative
review trials for WS3's deflated-Sharpe audit: ~139 (WS1) + 32 = **~171**.

### WS2 bottom line

The deployed universe survives a full overlap audit with ZERO forced
changes: both pre-registered within-sleeve prunes were rejected by the
cheap reflex, and every proposed widening — country momentum sleeve,
commodity-spot additions, factor lines — was killed or deferred on OOS
evidence rather than taste. The one structural change proposed is
architectural, not performance-led: EEM becomes overlay-only (V1), ending
the Sleeve-B/Phase-22 double-count that peaked at 15% look-through NAV.
The overlap rule is now explicit (cluster ≥0.8, reject candidates >0.9 to
incumbents, incumbents protected by kill-on-contact), the blend's true
US-beta concentration is quantified (mean 46.8%, max 83.5%) and handed to
WS3, and the trial register stands at ~171 configurations. Next session:
Workstream 3 — heavy gate on the frozen shortlist (deflated Sharpe,
full-system walk-forward, cost stress, EEM-tilt bet-count audit, C-floor
candidate from WS1).

---

## Workstream 3 — heavy robustness gate (session 3 results)

### Pre-session state check and baseline

Phase 29 (EEM overlay-only) LANDED before this session (commit 9bdfb8c) —
verified in `run_asset_class_rotation.py` UNIVERSE. The gate baseline is
therefore the NEW architecture; the frozen shortlist is **S1** (drop
Sleeve C's +5% floor, keep the 30% gate) and **S2** (slope gate on B);
**S3 is closed**. Decision bar for S1/S2 (frozen before any number was
computed): survives the deflated haircut AND not worse in the full-system
walk-forward OOS AND survives 2x cost — at BLEND level.

### Method

- New scripts: `scripts/ws3_common.py` (baselines; reuses the WS2 cached
  A/C/D, rebuilds B on the 12-line universe, replicates the validated
  WS2 tilt/gate composition), `run_ws3_precompute.py` (45-curve sleeve
  grid for the walk-forward), `run_ws3_deflated.py`,
  `run_ws3_overlay_bootstrap.py`, `run_ws3_full_wf.py`,
  `run_ws3_cost_stress.py`, `run_ws3_entrypoint.py`,
  `run_ws3_structural.py`. Artefacts: `data/ws3_*.json`,
  `data/ws3_grid_*.parquet`, `data/ws3_baseline_*.parquet`, chart
  [`data/ws3_full_wf.png`](data/ws3_full_wf.png).
- Regression checks all passed: rebuilt B +1.0217 (= WS2 reference);
  composed ungated blend +1.2070 (= WS2 V3 cell); composed gated+tilted
  final track +1.2921 (WS2 V1 gated reference +1.2891, within tolerance —
  the tilt ratio here comes from the deployed `em_regime_context.parquet`
  rather than the WS2 panel); grid deployed cells reproduce A/B/C/D
  baselines exactly. Committed live track +1.2956 (own window, diagnostic).
- Every script states its three silent-failure modes and defends each in
  code (docstrings); every verdict rule below was pre-registered in the
  script before results were seen.

### 1. Deflated Sharpe (`data/ws3_deflated.json`)

Trial accounting: register lower bound **171** (WS1 ~139 + WS2 32);
pre-review phases estimated at 192-405 configurations (per-phase table in
the JSON; estimates, not logs) → nominal totals 363-576, ceiling 1000.
Cross-trial dispersion measured from the 65 blend-level trials on file:
**sd(Sharpe) = 0.108** (0.101 ex the degenerate W=25 point; 0.136 when the
14 committed construction tracks — single sleeves, 2/3/4-way blends,
meta-rotation, Sharpe +0.59 to +1.21 — are included as a diverse-family
stress). Measured mean pairwise correlation of representative variant
tracks: **0.986** (Satterthwaite N_eff ≈ 3-9; the trials are near-copies
of one strategy, which is why the register N overstates the search).

| Track | Sharpe | DSR @171 | DSR @576 | DSR @576 diverse-V | E[maxSR] @171 |
|---|---:|---:|---:|---:|---:|
| Deployed final (gated+tilted) | +1.292 | 0.996 | 0.994 | 0.989 | +0.29 |
| Ungated blend | +1.207 | 0.991 | 0.989 | 0.980 | +0.29 |
| S1 final | +1.282 | 0.995 | 0.994 | 0.988 | +0.29 |
| S2 final | +1.304 | 0.996 | 0.995 | 0.990 | +0.29 |

**All four tracks SURVIVE the deflated haircut** on the pre-registered
bar. The expected maximum Sharpe a pure selection process would have
produced from this search is +0.29 (register) to +0.42 (diverse-V,
liberal N) — the observed +1.29 is 3-4x that. Even counting every
WF-internal candidate evaluation (~233k), DSR ≈ 0.98. The honest
boundary: modelling the history as ≥576 INDEPENDENT trials drawn from a
family with Sharpe sd ≥0.30 pushes DSR to 0.77-0.84 — but the measured
dispersion (0.108-0.136) and correlation (0.986) say that model does not
describe this project. Worst-case bound: HLZ-style Bonferroni (full
independence fiction) would haircut the deployed Sharpe 49% at N=171
(p_adj 0.073); reported for transparency, not used for the verdict.

### 6. Overlay reality check (`data/ws3_overlay_bootstrap.json`)

Block bootstrap (20/60/120d, run_robustness precedent) of each overlay's
daily contribution, plus 1000 circular-rotation placebos (rotation
preserves switch count, ON share and block structure exactly — "the same
overlay shape with no information content"), measured on the new
architecture. Pre-registered rules in the script docstring.

| Overlay | Point contribution | dSharpe | dDD | P(mean>0) 60d | Placebo pct (contrib / Sharpe / DD) | Episodes |
|---|---:|---:|---:|---:|---|---:|
| Phase 22 tilt | +0.13%/yr | +0.005 | −0.0pp | 0.56 | 82 / 87 / 36 | 6 |
| Phase 19 gate | −0.62%/yr | +0.081 | **+7.4pp** | 0.28 | 71 / **90** / **92** | 9 |

- **Tilt: KEEP AS POSITIONAL.** Six distinct bets ever; the contribution
  is statistically indistinguishable from a random 29%-ON overlay
  (bootstrap a coin flip, placebo percentiles below the 90 bar). This
  confirms the README's own label ("low-cost positional bet, not
  robustly-evidenced alpha") with numbers. It stays only because it is
  the architecture's ONE designated EM expression (WS2/Phase 29); it
  should never be counted as edge in any capacity claim.
- **Gate: KEEP — STRUCTURAL, and the timing is real.** The gate costs
  −0.62%/yr in premium and buys +7.4pp max DD and +0.08 Sharpe; against
  1000 randomly-timed de-risk overlays of identical shape its Sharpe
  lands at the 90th percentile and its DD improvement at the 92nd — the
  50d-breadth timing adds value beyond mechanical vol reduction. Return
  contribution alone is noise-to-negative, which is the correct shape
  for insurance.

### 2. Full-system walk-forward (`data/ws3_full_wf.json`, chart ws3_full_wf.png)

Annual expanding re-fit of EVERY knob — common horizon {200,250,275},
six weight sets, per-sleeve K, C floor, gate pair (incl. OFF), tilt
windows (incl. OFF) — 46,656 candidates per refit, chosen by full-system
train Sharpe; identical OOS calendar 2022-01-03 → 2026-06-16; ws1_wf
switch-cost protocol.

| Protocol | OOS Sharpe | Max DD |
|---|---:|---:|
| **frozen_deployed** | **+1.173** | −9.6% |
| frozen_S1 (drop C floor) | +1.101 | −11.6% |
| frozen_S2 (B slope gate) | +1.185 | −9.3% |
| wf_full (re-fit everything) | +0.968 | −11.6% |
| wf_weights_only | +1.121 | −9.2% |
| oracle_full (hindsight) | +1.333 | −10.6% |

**Re-fitting the whole configuration LOSES −0.205 Sharpe OOS to never
touching it** — the WS1 single-parameter result generalises, with more
damage per knob (WS1's horizon-only re-fit lost −0.013). The mechanism is
visible in the picks: the end-2021 refit chose 25/25/25/25 weights, C
floor 0 and K_C=7 — the in-sample peak of the 2020-21 thematic bull — and
paid test Sharpe −0.43 through 2022. Every refit dropped the C floor and
the tilt in-sample; both choices lost OOS. Weights-only re-fitting also
loses (+1.121): the deployed 35/35/10/20 was never picked by train Sharpe
and still beat every re-fit. The oracle shows +0.16 of hindsight Sharpe
existed; no honest process captures it.

### 4. Cost/execution stress (`data/ws3_cost_stress.json`)

Per-line one-way spread vectors replace the per-sleeve scalars (A 2 bps;
B 2 with DBC 5/TIP 3/SHY 1; C liquid 8 / thin 12 / BTC-USD 25 /
159801.SZ 25; D UCITS 15 — stated estimates), scaled 1x/2x/3x; holding
drags stay embedded in loader prices (not double-charged). Break-even =
multiple at which Sharpe falls to the same-universe equal-weight basket
(benchmark cost FIXED at 1x — conservative). Reconstruction validated:
weights x closes at deployed scalars reproduces every cached sleeve curve
to 1e-6.

| Level | 1x | 2x | 3x | Break-even | EW benchmark |
|---|---:|---:|---:|---:|---:|
| A | +1.013 | +0.995 | +0.977 | 12.25x | +0.812 (DD −36%) |
| B | +0.996 | +0.973 | +0.950 | 5.75x | +0.887 (DD −22%) |
| C | +0.684 | +0.616 | +0.548 | **1.0x** | +0.759 (DD −37%) |
| D | +0.754 | +0.670 | +0.586 | 1.75x | +0.696 (DD −37%) |
| Blend ungated | +1.153 | +1.101 | +1.049 | 6.25x | +0.885 (DD −31%) |
| **Final track** | **+1.234** | **+1.164** | **+1.094** | **6.0x** | +0.885 |

The BLEND is not a cost artefact (break-even 6x a deliberately-wide
vector). Two sleeve-level flags: **C already fails to beat its own EW
basket at the 1x per-line vector** (+0.684 vs +0.759, with matching max
DD −36% vs −37%) — its rotation edge does not survive realistic thematic
spreads standalone; and **D is the cost-fragile sleeve** (break-even
1.75x of a 15 bps assumption ≈ 26 bps one-way — execution quality on the
UCITS lines matters more than anywhere else in the system).
Shortlist 2x leg (final-track level): S1 +1.1549 vs deployed +1.1637 —
**FAIL**; S2 +1.1748 vs +1.1637 — PASS.

### 3. Entry-point discipline (`data/ws3_entrypoint.json`)

Final track, data as of 2026-06-16: worst rolling 12m **−5.4%** (ending
2022-10-24); longest underwater 302 trading days; DD within the 2020
COVID window −16.2%, within 2022 −9.3%; currently −1.28% from the high
set 13 days ago; trailing 3m/6m/12m = +7.9%/+19.5%/+39.5% = p78/p85/**p91**
of the track's own history. Pre-registered rule (6m AND 12m above p75):
**deployment today follows a STRONG RUN** — entry-point discipline says
do not add capital now; stage any adds after a flat/negative stretch.
(The review's outcome is parameter-neutral, so nothing new deploys; the
statement is on record for capital decisions.)

### 5. Structural re-checks (`data/ws3_structural.json`)

- **Look-ahead: CLEAN.** All ten prior-day-signal / shift(1) cites
  verified programmatically against live source: `run_portfolio.py:154,
  165` (A and, via `run_europe_rotation.py:194`, D), 
  `run_asset_class_rotation.py:311,320`, `run_thematic_rotation.py:678,
  687`, `run_risk_overlay.py:270` (tilt lag), `:370` (gate lag),
  `run_multi_strategy.py:201` (blend ordering).
- **NaN degradation, demonstrated by probe:** stale A/D breadth (7-day
  cap, `alignment.py:30`) → sleeve goes FULLY UNINVESTED (zeros, not
  cash); stale B/C signal → 100% SHY; gate holds state on NaN.
- **FLAG: the Phase 22 ratio ffill has NO staleness cap**
  (`run_risk_overlay.py:269-270`) — a stopped EEM/SPY cache would freeze
  the tilt state indefinitely and mark the 10pp tilt at 0% daily return
  while ON. Patch proposed (below), not applied in-session.
- **C survivorship, quantified:** gross arithmetic contribution
  +146.9pp over the window; **BTC-USD alone +33.2pp (23%)**, added Phase
  15 (2026-05) with history backfilled to 2018; top five names (BTC-USD,
  BLOK +17.5, REMX +16.4, TAN +12.4, ARKK +11.1) ≈ 62% of sleeve
  contribution; PHO/IHI (Phase 25) and CQQQ/159801.SZ (Phase 17) are
  also post-hoc adds. No PIT membership exists; the bias cannot be
  corrected retroactively, only bounded — mitigants are the momentum
  eligibility, the 10% blend cap and the Phase 27 gate.
- **FX consistency:** D EUR→USD (`run_europe_rotation.py:128-158`), C
  CNY→USD with 10-day cap (`run_thematic_rotation.py:430-479`); cached
  EURUSD anchors sane (2022-09 parity trough 0.969; latest 1.146);
  offline session — anchors not re-verified against a second source
  today (series was two-source verified at Phase 20.2).

### WS3 verdict table (the deliverable)

| Component | Verdict | Evidence (deflated / WF OOS / cost) |
|---|---|---|
| Sleeve A (14 lines, breadth top-7) | **KEEP** | in all surviving tracks; cost break-even 12.25x |
| Sleeve B (12 lines + SHY, momentum top-7) | **KEEP** | post-Phase-29 rebuild +1.0217; break-even 5.75x |
| Sleeve C (25 thematics, K=5, floor+gate) | **KEEP, ON NOTICE** | loses to own EW basket at realistic spreads (+0.684 vs +0.759, DD matched); blend seat adds ~nothing (without-C diagnostic +1.2964 vs +1.2921, 4/6); survivorship quantified (BTC-USD 23% of contribution). No change now — dropping a sleeve on a +0.004 margin is tuning on noise — but C must justify its seat at the next scheduled review |
| Sleeve D (5 UCITS, breadth top-3) | **KEEP, EXECUTION-WATCH** | cost-fragile: break-even 1.75x of 15 bps; monitor realised UCITS spreads vs the 9 bps assumption |
| Blend weights 35/35/10/20 | **KEEP** | weights-only WF re-fit loses (+1.121 vs +1.173); never picked by train Sharpe yet beats every re-fit |
| Phase 19 gate (20/50, 50% derisk) | **KEEP — structural** | DSR-clean; timing real (placebo p90 Sharpe / p92 DD); −0.62%/yr premium buys +7.4pp DD |
| Phase 22 tilt (50/200, 10pp) | **KEEP AS POSITIONAL — not edge** | 6 bets ever; bootstrap P(>0) 0.56; placebo 82/87/36; retained solely as the designated EM expression |
| S1 — drop C +5% floor | **REJECT** | DSR pass; WF OOS FAIL (+1.101 vs +1.173, DD worse); 2x cost FAIL (+1.1549 vs +1.1637). The floor's 2022 value is real |
| S2 — slope gate on B | **PASSES THE BAR; NOT DEPLOYED (parsimony)** | DSR pass; WF OOS +1.185 vs +1.173; 2x PASS; consistency 4/6 — every margin ≈ +0.01, inside noise; fewer-knobs-wins-ties (WS1 verdict re-confirmed on the new architecture) |
| S3 — EEM overlay-only | **CLOSED** | landed as Phase 29 before this session |
| Full-config annual re-fit | **REJECT (evidence, not taste)** | −0.205 Sharpe OOS vs frozen; every refit bought the in-sample peak |

**Final proposed configuration: the deployed Phase 29 system, unchanged.**
Zero parameter changes survive the heavy gate with an economically
meaningful margin. Proposed patch list (maintenance, not tuning), for
approval:
1. `run_risk_overlay.py` — add a staleness cap (e.g. 10 trading days,
   mirroring the C FX cap) to the EEM/SPY ratio ffill at :269-270, with a
   WARN + tilt-hold-flat degradation path.
2. README "Known caveats" — update the Phase 22 line to cite the WS3
   bootstrap numbers (6 bets, P(mean>0) 0.56, placebo 82nd pct); add the
   C survivorship quantification (BTC-USD 23% of contribution) and the
   C-on-notice / D-execution-watch flags.
3. Docs/factsheet: no changes (numbers unchanged).

### WS3 trial register

Counting convention as WS1/WS2 (each evaluated configuration once; stress
reports and diagnostics of the same configuration do not count; WF
protocols count once each; benchmarks count):

- Grid sleeve configs new to the register: A 6, B 8 (new architecture),
  C 14, D 6 = 34
- S2 on the new architecture: 1
- WF protocols: frozen_S1, frozen_S2, wf_full, wf_weights_only,
  oracle_full = 5
- EW cost benchmarks: 4 sleeves + 1 blend = 5
- Blend-without-C diagnostic composition: 1
- Deflated/bootstrap/placebo/entry-point: diagnostics on registered
  configurations = 0

**Session 3 total: 46 new. Cumulative register: ~171 + 46 = ~217.**

### WS3 bottom line

The heavy gate closes the review with the strongest possible statement a
robustness audit can make: **the deployed system survives everything, and
every alternative loses.** The deployed Sharpe is 3-4x what pure
selection would have manufactured from the documented search (DSR ≥ 0.99
at the register count, ≥ 0.98 at the liberal bound); re-fitting any
subset of the configuration annually — one parameter (WS1), the weights,
or everything at once — loses out of sample, with the full re-fit losing
−0.21 Sharpe; the blend's edge survives 6x deliberately-wide per-line
costs; and the structural audit finds the look-ahead discipline intact.
Both shortlist survivors resolve without a deployment: S1 fails two of
three legs (the C floor's 2022 drawdown value is real and shows up in
exactly the OOS window that matters), and S2 passes all three legs at a
+0.01 margin that parsimony declines. The honest debits are now on the
record with numbers: the tilt is a positional bet, not alpha (6 bets,
coin-flip bootstrap); C's rotation does not beat its own basket at
realistic spreads and carries a quantified survivorship bias (BTC-USD =
23% of contribution); D's edge is the most cost-sensitive; and today is
a strong-run entry point (trailing 12m at p91), so capital adds should
wait. The review ends where it began, deliberately: no changes — now
with ~217 registered configurations of evidence that no change was the
right answer.

---

## Implementation & pipeline audit (session 4, 2026-07-04)

Correctness audit of the code and pipelines behind the WS0-WS3 evidence and the
daily live path — NO backtests, no performance numbers. Read-only on deployed
code in BOTH repos (engine `breadth-thrust-etf`; consumer
`navigo-systematic-trend`). Every defect is CONFIRMED (minimal reproduction) or
PLAUSIBLE; severity S1 (invalidates review evidence) → S4 (hygiene). Filed
record: [`reviews/2026-07-04_implementation-audit.docx`](reviews/2026-07-04_implementation-audit.docx).
Pre-session state verified: WS3 record filed; Phase 29 (`9bdfb8c`) the last
config commit; the EEM/SPY staleness-cap patch still PENDING; the 3-Jul ops
commits (`913e8a6`, `b89c002`, `7843a02`, `280eeed`, `63b4678`) audited as new
code. Probe: `scripts/run_audit_probe_capture.py` → `data/audit_capture_probe.json`.

### Scope 1 — signal-path correctness: every deployed path CLEAN on the audited axes

For A/B/C/D and both overlays, verified beyond WS3's closed shift(1)/FX-conversion/
survivorship/stale-cap checks:

- **Execution timing** is uniformly prior-day signal → rebalance,
  `weight.shift(1) * pct_change` close-to-close, costs charged on the rebalance
  day (`run_portfolio.py:154-168`, `run_asset_class_rotation.py:307-325`,
  `run_thematic_rotation.py:668-692`, D via `run_portfolio`). No decision-vs-fill
  mismatch; a holiday-Friday rebalance is silently skipped (weights ffill), a
  conservative, immaterial choice.
- **Total-return basis** consistent — every yfinance loader uses `auto_adjust=True`.
- **Resampling** uniformly `.resample("W-FRI").last()` (right-labelled Friday).
- **Threshold operators** match spec exactly (all strict, matching "below/above/
  fewer-than"): gate de-risk `v < 0.20` / re-engage `v > 0.50`
  (`run_risk_overlay.py:171-173`); tilt golden cross `fast > slow` (`:224`); C
  sleeve-gate `sleeve_breadth < 0.30` (`:575`).
- **Venue calendars** handled correctly. The blend runs on the INTERSECTION of
  the four sleeve equity indices and takes `pct_change` on the sliced curves, so
  a Xetra-only or US-only session compounds into the next common day — no return
  lost or double-counted (`run_multi_strategy.py:181-215`). BTC-USD's 7-day week
  is reindexed to the NYSE calendar (weekend prints dropped,
  `run_thematic_rotation.py:408-427`) and the weekend move folds into the Monday
  close-to-close return. CNY→USD uses the CAPPED `align_series_to_index(max_stale_days=10)`.
- **FX date alignment** (timing, not the conversion WS3 closed): D and C both
  align the FX rate to the price date (same-day merge / ffill), no off-by-one.

Verdict per path: **CLEAN**. Two items carried to the register: C's `SIGNAL_FLOOR`
docstring says "require >= 5%" but the code is strict `valid > SIGNAL_FLOOR`
(measure-zero effect; doc/code mismatch, S4); D's EUR→USD ffill is UNCAPPED
(`run_europe_rotation.py:154`), inconsistent with C's capped FX (S3).

### Scope 2 — backtest↔live parity: DUAL-BUT-EQUIVALENT

The daily live track does NOT share the backtest code path. `mark_to_market_live.py`
reads the Friday anchor from `risk_overlay.json` (declared source of truth) and
REIMPLEMENTS the effective NAV weights in `_build_effective_weights` (`:127-170`),
explicitly mirroring the dashboard JS `renderPositionsPreview` — a third
implementation of the same weight logic. The reconstruction is EQUIVALENT to the
backtest gate∘tilt composition (backtest gates the tilted-ungated blend at
`run_risk_overlay.py:414-419`; live RISK_OFF → sleeves halved, EEM 0.10×0.5 = 5%,
residual to SHY): the same portfolio, valued buy-and-hold from the anchor.
Micro-differences only, immaterial over ≤5 days (backtest daily-rebalances the
50/50 gate split and snaps the blend weekly; live is pure buy-and-hold; live
marks a missing price flat). No shared function — equivalence holds by manual
mirroring across three surfaces. That fragility, not a present error, is the
finding.

### Defect register

| ID | Repo | File:line | Class | Sev | Status | Impact |
|---|---|---|---|---|---|---|
| D1 | engine | `check_capture_integrity.py:82` + `mark_to_market_live.py:216` | pipeline / ops | **S2** | CONFIRMED | `evaluate_target` grades any live series with `len(dates) < 2` as `fail`; `_project_daily_equity` emits only points strictly after the Friday anchor, so the first session after a fully-fresh anchor (Monday) and the weekly re-anchor (0 points) fail the non-`continue-on-error` capture step → daily/weekly job dies before publish, false `[FAIL]` email, dashboard not updated. Self-heals next session. |
| D2 | consumer | `portfolios/navigo-systematic-trend.json:47` (+ `adapter.py` `build_weights` grouping) | contract drift | S2/S3 | CONFIRMED (inspection) | `etf_meta.EEM.sleeve = "B"` predates Phase 29 (EEM now overlay-only). With the tilt ON (current), the 10% EEM rolls into `by_sleeve["B"]` and the TILT bucket shows ~0 — the consumer's sleeve breakdown misattributes the overlay to Sleeve B. |
| D3 | engine | `run_risk_overlay.py:363` | silent staleness | S3 | PLAUSIBLE | Phase 19 gate breadth `reindex(common, method="ffill")` is UNCAPPED — same class as the flagged tilt ratio (`:269`), but NOT in the pending patch. A stalled CSP1 feed freezes the gate on stale breadth (not NaN), so the WS3 "gate holds state on NaN" degradation path is not reached on a stopped feed. |
| D4 | engine | `run_europe_rotation.py:154`; live `mark_to_market_live.py:284` | silent staleness | S3 | CONFIRMED (inspection) | Sleeve D EUR→USD ffill UNCAPPED — inconsistent with Sleeve C's capped 10-day FX (`run_thematic_rotation.py:477`). A stalled EURUSD freezes D at a stale rate. |
| D5 | both | engine `run_risk_overlay.py:253-256`, `mark_to_market_live.py:139-144`; consumer registry `sleeves.*.alloc` + `adapter.py:556,563` | duplicated constant | S3 | CONFIRMED | Blend weights 35/35/10/20 + tilt 10%/fund-from-B restated in ≥4 places, none cross-checked. `validate.py` reconciles equity-curve stats only, not weights — a future reweight/tilt change drifts the consumer's attribution and weight-history silently. |
| D6 | consumer | `portfolios/navigo-systematic-trend.json:11` | doc/config | S4 | CONFIRMED | `cost_assumption_bps: 5` does not match the engine's per-sleeve costs (A2/B2/C5/D9). Display / future-valuation metadata only (thin-renderer does not recompute returns). |
| D7 | engine | `run_thematic_rotation.py:538,580` | doc/code | S4 | CONFIRMED | `SIGNAL_FLOOR` docstrings say "require >= 5%"; code is strict `> SIGNAL_FLOOR`. Measure-zero economic effect. |
| D8 | engine | `run_europe_rotation.py:136-158` | doc/code | S4 | CONFIRMED | `_fx_convert_eur_to_usd` docstring says it fetches `USDEUR=X` and inverts; code fetches `EURUSD=X` and multiplies (correct result, stale docstring). |

### Scope 3 — per-workflow failure modes (loud vs silent)

| Workflow | Loud (fails / aborts / emails) | Silent risk remaining |
|---|---|---|
| `daily_live_track.yml` | pipeline hard guard abort (CSP1 lag > budget); capture-integrity 2+ behind/corrupt-tail; pytest; any step error → `[FAIL]` email | **D1 false-fail on the first-session-after-anchor**; A/D sleeves never re-run in CI (stale up to the CSP1 guard window, ~6 weekdays) |
| `weekly_factsheet.yml` | freshness warning (lag 4); capture-integrity `--targets all` (B/C/live); pytest; email step; `[FAIL]` on any failure | **D1** if the live target has 0 points (local A/D refresh lands the same Friday); A/D staleness masked by the live splice |
| `sentinel.yml` | outside-in: fetches the live Pages `factsheet_meta.json`, compares `asof_iso` to the NYSE calendar; `[SENTINEL]` email on mismatch; retries + cache-buster | Cannot see stale A/D *signals* — the live splice keeps the as-of fresh, so a weeks-stale sleeve pick passes the as-of check |

pipeline.py guards are strong: freeze detection (`:286`), source-panel freshness abort (`:351` busday_count), derived-freshness (`:415`), `built_at` assertion (`:478`), roster-staleness `PUBLISH ABORTED` (`:1155`), regime-STALE abort (`:1183`). The new 3-Jul scripts are otherwise well-built (`check_freshness_headroom.py` fail-safe-to-alert; `nyse_sessions.py` true exchange calendar; `sentinel_check.py` retries) — D1 is the one defect among them.

### Scope 4 — engine↔consumer contract

Navigo is a THIN RENDERER of the engine's `live_track.json` in production; the
valuation layer that would restate weights/cost/FX is real code, flag-gated OFF
(`config.py:38`, `NAVIGO_VALUATION_LAYER`). It READS from engine outputs (equity
curves, sleeve equities, gate parameters — thresholds/derisk/fallback,
regime/tilt state, effective_weights, sleeve within-weights) and RESTATES:

- registry `deployed_key`/`gated_key`/`ungated_key` — JSON key strings; a rename
  KeyErrors in `adapter.build_equity` (LOUD, not silent).
- registry `sleeves.*.alloc`/`alloc_tilt` (35/35/10/20; B→25, TILT 10%) and the
  SAME constants hardcoded again at `adapter.py:556,563` — used for attribution
  and reconstructed weight-history; **silent** drift on any reweight.
- registry `etf_meta` sleeve map (drift D2 already live) and `cost_assumption_bps`
  (D6).

**Would `validate.py` catch drift? No** — it reconciles recomputed *equity-curve*
stats vs the engine's figures (always the engine's own curve → always passes) and
checks freshness + regime-since consistency, but never the weights/allocations/
universe. Weight-constant drift passes silently.

**Pending patch safety:** the EEM/SPY staleness-cap patch changes only the tilt's
degradation behaviour (WARN + hold-flat) — no key/weight/threshold change — so it
does NOT break any restated consumer constant.

**Proposed ONE contract test (spec):** in navigo, parse the blend weights from
the engine `deployed_key` string (it literally encodes `blend_35_35_10_20_…`) and
read `phase22.parameters.tilt_weight`/`fund_from_sleeve` from `risk_overlay.json`;
assert they equal (a) registry `sleeves.*.alloc`/`alloc_tilt`, (b) the hardcoded
`alloc` dict in `adapter.build_weight_history`, and (c) that every
`effective_weights` ticker maps to a live engine sleeve (EEM not under B). Fail
the build loudly on any mismatch — this is exactly the drift `validate.py` misses.

### Scope 6 — test adequacy (per repo)

- Engine: `test_no_lookahead.py`, `test_backtest_math.py`, `test_wtd_logic.py`,
  `test_stale_breadth.py`, `test_dates.py`, `test_nyse_sessions.py` pin
  CORRECTNESS. `test_check_capture_integrity.py` pins verdict logic but ENSHRINES
  D1: every ok/warn case uses a 2-point series and `test_fail_on_length_mismatch`
  codifies `<2 → fail`; no single-point cadence case exists. Others
  (`test_derived_freshness.py`, `test_backtest.py`) are REGRESSION SNAPSHOTS.
- Consumer: `test_adapter.py`, `test_metrics.py`, `test_validate.py`,
  `test_valuation.py`, `test_capture_integrity.py`, `test_nyse_sessions.py`.
  navigo's `check_capture_integrity.py` is a DIFFERENT design (checks the baked
  dataset's single as-of via `sessions_behind`; `fail` only on corrupt/missing) —
  it does NOT inherit D1.

Highest-value missing tests — engine: (1) capture-integrity on a legitimate
single-point live series returns ok/warn, not fail (would have caught D1);
(2) `mark_to_market_live` NAV reconciles to the backtest gated+tilted curve at a
shared anchor within tolerance (parity guard for scope 2); (3) uncapped-ffill
degradation: a stalled gate-breadth / D-FX feed produces WARN/NaN, not a frozen
value (D3/D4). Consumer: (4) a config-drift contract test (scope-4 spec above);
(5) `by_sleeve` puts the EEM tilt under TILT not B (would catch D2);
(6) `build_weight_history` last reconstructed week equals `live_track` weights
(the docstring claims it; no test enforces it).

### Scope 5 — accretion (deletions only, no refactors)

- **Legacy SOXX 50/150 path** — `live_signal.py`, `backtest.py`, `run_etf_oos.py`
  operate the pre-deployment single-ETF thrust strategy (`backtest_soxx_oos.json`),
  wired into no current workflow. Candidate for removal or an explicit `legacy/`
  quarantine; confirm nothing external reads `live_signal.json` first.
- **Dead-but-deliberate:** `top_k_by_signal_capped` (thematic; `WEIGHTER_FACTORY =
  top_k_equal_weight`), retained for reversibility; EEM/HYG/IEF colour-palette
  entries kept for old payloads — harmless, leave.
- **Duplicated constants** (see D5) — the blend/tilt weights should collapse to one
  engine-published source the consumer reads, not four hand-kept copies.
- `_safe`/`round_series` are copy-pasted across ~8 `run_*.py`; cosmetic, out of
  scope for a deletions-only pass.

### Closing statement

**No CONFIRMED S1.** Every confirmed defect sits in the publication/alerting/
consumer-reporting plumbing (D1, D2, D6) or in silent-degradation ffill paths
(D3, D4) or is documentation drift (D5, D7, D8) — none touches the backtest
signal computation or accounting that produced the WS0-WS3 evidence, which ran
on fresh caches. The signal paths (A/B/C/D + Phase 19 gate + Phase 22 tilt) are
CLEAN on execution timing, total-return consistency, FX timing, venue-calendar
handling, threshold operators and look-ahead. **The WS0-WS3 conclusions and the
keep-Phase-29-unchanged decision STAND — nothing found requires the review
evidence to be revisited.** Maintenance follow-ups (review-and-propose, not
in-session): fix D1 before the next Monday (a false `[FAIL]` poisons the one
alert channel the sentinel design says must stay trusted); fold D3 (gate breadth)
and D4 (D FX) into the already-pending tilt-ratio staleness-cap patch for a
single consistent degradation policy; correct D2 in the consumer registry; and
add the scope-4 contract test so the next config change cannot drift the consumer
silently.

## Staleness-cap patch — landed against a live incident (2026-07-06)

Proposal #1 (WS3) plus defects D3/D4 landed as one staleness-cap patch, approved
(Fable review, 2026-07-05). A single 10-calendar-day cap
(`scripts/alignment.align_series_to_index`, mirroring the Sleeve C FX cap) now sits
at four sites: the Phase 22 EEM/SPY tilt feed (`run_risk_overlay._build_eem_tilted_blend`
— WARN + hold-flat past the cap), the Phase 19 gate breadth (`run_risk_overlay.main`
— NaN past the cap, so `_compute_states` holds the last regime state), the Sleeve D
EUR→USD leg (`run_europe_rotation._fx_convert_eur_to_usd`), and the live path EUR
**and** CNY legs (`mark_to_market_live._fetch_usd_prices`). Five tests added in
`tests/test_stale_breadth.py`; README "Known caveats" updated. D2/D5 deliberately
left out of scope.

### The live incident (the patch caught a real one)

`data/em_regime_context.parquet` was frozen at **2026-05-27** — the weekly refresh
does not update this cache (`_load_eem_data` uses it as-is whenever the EEM+SPY
columns are present). Against the blend as-of **2026-07-01** that is a 35-calendar-day
gap: **17 sessions stale from 2026-06-08**. The pre-patch uncapped forward-fill was
marking the 10pp EEM sleeve at a **frozen 0% daily return** while the tilt still read
ON (ratio frozen at 0.0910). The patch was validated against this incident, not a
synthetic one:

- **Stale cache (rebuild before refresh).** The WARN fires —
  `Phase 22 EEM/SPY feed stale > 10 days at as-of 2026-07-01 — tilt held flat
  (baseline blend, no EEM tilt).` The tilt is held flat across the 17 stale sessions.
- **Refreshed cache (rebuild after refresh).** The WARN is silent and the tilt reads
  real EEM again — ratio 0.0910→**0.0902**, fast MA 0.0887→**0.0903**, slow MA
  0.0835→**0.0851**.
- **The cap is a pure no-op on fresh data** — patched equals pre-patch to eight
  decimal places on the refreshed cache. It is a safety net, not a signal change, as
  designed.

### Impact — measured, and a correction to the pre-approval figure

The pre-approval note quoted "tilt ungated tail moves +0.059%". Re-measured against
the live pipeline at full precision (ungated tilted total return, as-of 2026-07-01)
**that figure does not reproduce**. The reconciled numbers:

| State | Ungated tilted total return | Move |
| --- | --- | --- |
| Deployed pre-patch (EEM frozen at 0%) | 202.9170% | — |
| Patched, held flat (stale cache) | 202.9442% | **+0.0272 pp** — patch effect: funding back to Strategy B beats a frozen-0% EEM over the 17 sessions |
| Patched, real EEM (refreshed cache) | 202.3403% | **−0.6039 pp** vs held-flat |

Deployed **gated** `total_return` (the dashboard figure): 1.9340 → 1.9343 → **1.9285**.

Two points of substance:

1. **The refresh moves the tail down, not up.** Real EEM *fell* 2.5% over the frozen
   window (68.17 on 2026-05-27 → 66.48 on 2026-07-01), so un-freezing the 10pp EEM
   sleeve costs ~0.60 pp on the ungated tail (~0.58 pp on the gated) versus the
   held-flat rebuild. Refreshing to real data is still the correct end state, but it
   is a small **reduction** in the deployed track's stated total return — recorded
   here so the number is not misread as accretive.
2. **The +0.059% was a mischaracterisation.** It described the held-flat-vs-frozen
   move (the WARN-firing stale rebuild), which measures **+0.0272 pp** here — not
   +0.059%, and not the refresh. Superseded by the table above; flagged to Zhenghao
   for reconciliation against the source of the original figure.

The patch mechanism (WARN, hold-flat, NaN-holds-state, no-op-on-fresh) is validated
regardless; only the impact magnitude and direction differed from the pre-approval
description.

### D-class follow-ups registered (deliberate carve-outs from proposal-#1 scope)

| ID | site | class | note |
| --- | --- | --- | --- |
| **D9** | engine `run_risk_overlay.py:464` | silent staleness (display only) | The tilt **diagnostic** `sig_aligned = eem_signal.reindex(common, method="ffill")` — feeding `phase22_eem_tilt.current_state` / `current_ratio` / `daily_series` — is UNCAPPED, left out of the patch (a cap would need `int(NaN)` display handling). Consequence: after the patch, on a stalled cache the RETURN path correctly holds flat, but the dashboard tilt diagnostic still reads `EM_TILT_ON` at a frozen ratio. Reporting only; the equity is safe. Fix: cap the diagnostic alignment and render NaN as "STALE / no signal". |
| **D10** | engine `run_risk_overlay.py:406` | deliberate non-cap | The Phase 19 risk-off fallback `fallback_aligned = fallback.reindex(common, method="ffill")` (SHY, 1-3y Treasury) was deliberately left UNCAPPED — it is a price PROXY, not a signal feed, so a staleness cap does not apply. Logged so the decision is on record; revisit only if SHY sourcing changes. |

---

## Workstream 5 — constituent relative-trend challenger (2026-07-10)

Origin: the SentimenTrader relative-trend-score concept (emails 2026-07-02 financials, 2026-07-07 semis; PDFs stay in OneDrive, IP firewall — own binary definitions only). Their sector table aggregates two per-stock 0–10 trend composites (absolute price + price/S&P-500 ratio) and highlights the dual-≥8 share. Sleeve A already runs the ABSOLUTE per-name leg (close > own 200d MA, Phase-20 cross-sectionally demeaned, Phase-20.1 top-K positive-weighted). The one genuinely new object is the per-name RELATIVE leg (trend on the stock/SPY ratio) and the dual AND. WS1 tested only reformulations of the absolute measure (flat plateau, all lost OOS), so this is the first structurally different per-name signal since Phase 20 — a new-mechanism test, not a re-fit.

### Method
Swap ONLY the per-name condition; hold demeaning, top-K, universe (14 Sleeve-A ETFs) and 2 bps cost fixed. Engine `scripts/relative_trend.py` uses ONE shared cross-leg validity mask (a name counts on a day only if BOTH legs are computable) so arms differ solely in the condition, never in eligibility. Because ratio = close/SPY inherits the close's NaN mask, the shared mask collapses to the absolute leg's own mask → A0 reproduces the deployed `compute_ma200_breadth` to 0.0 (asserted selftest). 13 selftests (look-ahead ×2, denominator symmetry ×2, deployed parity, invariants, date boundaries) committed BEFORE the run (75acc3d, 837c9ff) per em-rotation §1.9b. Registered window 2018-10-12→2026-06-30; WF = initial train to 2020-12-31 then 6 annual K-refits (K∈{3,5,7,9}) OOS 2021→2026-Q2. Verdict rule (frozen, 4 sign-off items confirmed 2026-07-10): a challenger must (1) WF Sharpe ≥ A0 + 0.10, (2) ≥ P + 0.10, (3) MaxDD within 2pp, (4) survive both at 2× cost, (5) weekly Jaccard vs P < 0.8.

### Result — VERDICT KEEP A0 (`data/ws5_results.json`, run harness `scripts/run_ws5_relative_trend.py`)

| Arm | Full Sharpe | WF OOS | WF 2× | MaxDD |
|---|---:|---:|---:|---:|
| A0 absolute (deployed) | +1.009 | **+1.128** | +1.107 | −30.6% |
| OR (A0 ∪ A1) | +1.031 | +1.126 | +1.106 | −30.9% |
| P momentum placebo (126d) | +0.924 | +1.031 | +1.016 | −33.1% |
| A2 dual rel-250d (neighbour) | +0.929 | +0.984 | +0.966 | −30.8% |
| A2 dual rel-150d (neighbour) | +0.887 | +0.955 | +0.934 | −31.1% |
| A1 relative | +0.923 | +0.947 | +0.929 | −31.2% |
| A2 dual (rel-200d) | +0.905 | +0.906 | +0.887 | −30.9% |

Adopt bar = A0 + 0.10 = +1.228. Both A1 and A2 fail cond 1/2/4, pass only 3/5 (Jaccard 0.64 vs P). No relative or dual arm reaches even the momentum placebo (+1.031). The failure is structural (holds at 2× cost) and not a drawdown trade (all arms −30.6% to −31.2%; the placebo is the −33.1% outlier).

**Interpretation.** The relative leg is real, not noise — best challenger A1's annual Sharpe 0.92 vs expected-max-under-null 0.08 gives DSR 0.989 (z 2.29, N=8) — but redundant: it loses to the deployed absolute leg AND to plain momentum, tracking the placebo at 0.95 return-correlation. Mechanism: Phase 20 already demeans sector breadth cross-sectionally (removes market beta at the sector level); measuring each name relative to SPY subtracts a market-beta component a SECOND time at the name level — in a broad up-market with concentrated leadership (2019–26) that beta is what the rotation signal rides, so double-subtracting leaves a noisier residual. Consistent with WS4 (strength entries carry nothing) and the house read (breadth for concentrated sleeves, momentum for diversified baskets). **Positive by-product:** A0 beats the ETF-momentum placebo by +0.097 WF, direct re-validation of Sleeve A's constituent-breadth premise.

### Trial register
7 engine arms evaluated, 0 selected: A0, A1, A2, OR, dual-rel150, dual-rel250, P. DSR charged at N=8 (the 7 arms + 1 blend-context pad). K-grid is within-arm WF selection, not a separate testing axis. Parity: A0-vs-deployed breadth 0.0; `_wf_local` ≡ canonical `walk_forward_sharpe` at matched 5 bps 0.0 (verdict runs at the deployed 2 bps).

### Decisions
- **A0 KEEP** (frozen rule); **A1/A2 REJECT**.
- **Tier-1 dual-trend panel DOWNGRADED** to an on-demand digest read — the verdict shows the abs-vs-rel divergence carries no rotation-relevant edge, so no standing dashboard panel; the frozen engine can produce the divergence table for the weekly digest when topical.
- **IBD Power Trend PARKED** (index-level gate class; Phase 19 defended twice).
- **FLAG (tooling, not deployed-risk):** `walk_forward_sharpe` hardcodes Strategy C's 5 bps (imports `COST_FRAC` from `run_thematic_rotation`) — mis-costs any 2 bps sleeve if reused. Cheap one-line fix, separate from this study.

### WS5 bottom line
0 deployed changes. The SentimenTrader concept is exhausted as a source of Sleeve-A alpha; reopening needs a genuinely new object, not a re-fit of this one. **WS5 tested Sleeve A ONLY** — B, C, D and the two overlays were out of scope and are neither changed nor cleared; their standing is governed by the 2026-07 staged review. Record `reviews/2026-07-10_ws5_relative-trend.docx`.

## Norgate breadth-feed migration study — 2026-07-17 (REVIEW-AND-PROPOSE; 0 deployed changes)

The Phase 19 gate input (scrape-built CSP1 `ma_breadth`) reconciled
against Norgate `#SPX%MA50` over the full overlap (2,138 joint days,
2018-01-05 → 2026-07-10): level correlation 0.9986, median bias −1.24 pp
(definitional: official membership + vendor price basis vs scraped roster
+ yfinance adjusted closes), gate-state agreement 98.60%, and **all 24
regime flips paired across feeds** (17 same-day, 20 within one day; the
three laggards are ON-side threshold crawls — protection triggers are
effectively identical, 8 zone-disagree days at 0.20 in 8.5 years). The
hysteresis used was `_compute_states` IMPORTED from `run_risk_overlay`,
not re-implemented. Candidate depth reaches 1957-03-04.

**Staged proposal filed** (`reviews/2026-07-17_norgate-feed-migration.md`),
each stage behind its own approval: S1 local parallel-run (Task Scheduler,
sentiment-composite pattern), S2 derived-states swap with automatic scrape
fallback under the deployed 10-day cap, S3 keep-both steady state; rollback
= delete the states file. Licence design: the raw vendor series never
enters this public repo — the handover is a derived 0/1 states file
(`scripts/publish_norgate_breadth.py`, currently UNWIRED; raw pulls
confined to git-ignored `data_local/`). Live smoke test 2026-07-17: both
feeds read RISK_ON on the 2026-07-16 bar. Sleeves A/D stay on the scrape
(no vendor equivalent for UCITS sectors / Stoxx). Stale ledger follow-up
corrected in passing: the D3/D4 staleness caps are DEPLOYED in
`run_risk_overlay.py`.

**2026-07-17 — both approvals given (ZH, in session). Stage 1 LIVE**:
task `breadth-thrust norgate feed parallel-run`, Tue–Sat 07:15 SGT,
wrapper `scripts/run_norgate_publisher.bat`, output confined to
git-ignored `data_local/`. Stage 2 approved in principle; soak review
due Friday 2026-08-07 with the concrete loader diff. Record §8.

## 2026-07-18 — Reporting-layer robustness audit (CLOSED, REMEDIATED)

**Scope**: calculation/reporting layer only — `build_email_body.py`,
`build_factsheet.py`, `template.html`, `build_risk_visuals.py` and their
feeds; strategy logic out of scope (2026-07-04 implementation audit covers
it). Method: cross-artefact reconciliation with per-artefact formula clones
calibrated against stored engine fields, then independent date-library
recomputation; shipped vintages pinned per git commit.

**Result: ten confirmed silent numerical defects, all fixed and deployed
the same day** (4efa087, 19552f8, 7671120, 40e59c7; 317 tests; live deploy
verified). The three most material: (1) the factsheet attribution charts
silently dropped 47.6% of NAV in every weekly build — sleeves A ex-SOXX
and D could never resolve against the panel's proxy keys — and the EEM bar
(10% NAV) covered the wrong week; (2) the same delivery printed email 1Y
+32.19% against PDF +30.75% (252-bar "1Y" = 374 calendar days on the
~246.5-bar/yr intersect calendar) while the dashboard's default view showed
YTD +13.95% against +15.26% everywhere else and Sharpe 1.333 beside the
header's 1.236; (3) eight published daily builds carried phantom non-NYSE
bars after US holidays (tail-only session cap), shifting the 2026-07-06
WTD by −0.25pp. Latent state divergences (tilt-flip priors ≤5.7pp NAV,
missing 10% EEM trade rows, RISK_OFF holdings at 2× live target) are now
unified behind `scripts/overlay_state.py` and its JS port — point-in-time
sleeve weights from the overlay event log, flip-day inclusive.

**Convention guard going forward**: any new metric surface must reuse
`overlay_state` / `ytd_ret` / the shared JS window helpers and ship a
cross-artefact agreement test (pattern: `tests/test_wtd_logic.py`).
Watchpoint: first US-holiday-with-Xetra-open after any live-track change
(next: Labor Day, Monday 2026-09-07) — regression test covers the
mechanism. Record: `reviews/2026-07-18_reporting-audit.docx`. Follow-up
spawned for the monitor repo's registry coverage (digest proximity chart).
