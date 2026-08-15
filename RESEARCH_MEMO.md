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
| **D — Europe sectors (20%)** | Constituent breadth, share above **200d** MA — **ABSOLUTE, not relative** | 200d (imported `run_europe_rotation.py:45`) | Top K=3 breadth-weighted (`:65`), weekly Friday | 5 Stoxx Europe 600 sector UCITS panels: EXV1 EXH1 EXV3 EXH3 EXH9 (`etf_registry.py` `UNIVERSE_EUROPE_SECTORS`). Traded tickers come from each entry's `yfinance_trading_proxy`, never from the key plus `.DE` — EXH3's panel is Industrial Goods & Services and trades as **EXH4.DE** (corrected 2026-08-03; `EXH3.DE` is the Food & Beverage fund, see `reviews/2026-08-03_sleeve-d-exh3-correction.md`) | 9 bps incl. FX (`run_europe_rotation.py:55`) | engine `run_europe_rotation.py:161-200`; EUR→USD conversion `:128-158` |

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

## 2026-07-18 — WS7 registered: Sleeve C seat review (evidence live, sign-off pending)

The WS3 "KEEP, ON NOTICE" item is now a pre-registered workstream:
`KICKOFF_ws7-c-seat.md` fixes the review date (Fri 2026-10-02), the two
comparisons (rotation vs its own EW-25 basket at the frozen WS3 1x
per-line cost vector; blend with-C vs the registered pro-rata without-C
counterfactual with gate-scaled w_C), a three-way decision rule
(KEEP / SWITCH-to-passive-EW / DROP), a ±2.0pp OOS noise band with a
confirm-or-veto role only, and a one-sided −5pp tripwire — all BEFORE any
out-of-sample evidence was read. The owner deferred the scoping choices
to the recommended options; every gate is marked pending countersign in
the kickoff's sign-off block. No strategy behaviour changes until the
review.

Instrumentation live from this week: `scripts/run_c_seat_watch.py`
(weekly workflow + refresh_all) appends one point-in-time row per
completed week to `data/c_seat_watch.json`; the weekly email carries a
one-line watch with STALE and TRIPWIRE tags (soft-fail is safe because
staleness surfaces in the artefact). Universe membership frozen in
`data/c_universe_pit.json` — future adds join the benchmark only from
their dated entry. First two OOS weeks (to 2026-07-17): rotation vs
EW-25 **−2.58pp**, seat **−0.84pp** — both point the same way as WS3,
and both are inside the registered noise framing; no reading before the
review date. Without-C algebra verified exact against the published
series; 7 unit tests; full suite 334.

## Workstream 6 — single-name implementation of Sleeve A (2026-07-19, CLOSED)

**Question** (kickoff `C:\dev\KICKOFF_ws6-single-name-implementation.md`, BINDING, signed 2026-07-17): can Sleeve A's ETF positions be expressed as constituent baskets without degrading the sleeve's evidence base (Design 1 — trend-screened replication), and does within-sector top-N selection add anything beyond a momentum placebo (Design 2, WS5 bar)? Verdict set {KEEP-ETF, ADOPT-D1, ADOPT-D2}; all bars frozen pre-results; honest prior "D1 plausible, D2 null".

**VERDICT: KEEP-ETF.** Both adoption designs fail their pre-registered bars. Zero deployed changes.

### Gate chain (all bars unchanged throughout; full history in `data/ws6_results.json` `gate_history`)

| Run | G1 coverage (97% bar) | G2 replication (0.95 bar) | Resolution |
|---|---|---|---|
| 1 (engine b12d0f9) | STOP: 68 line-year cells fail, worst IUCM 2018 = 0.577 (survivorship signature) | not reached | A1 — (ticker, date) → Norgate instrument: delisted -YYYYMM suffixes, life-interval disambiguation, verified rename table (commit 54f0f14) |
| 2 (54f0f14) | STOP: 6 cells fail (IUSP 2018-2022, IUCM 2018 = 0.923) | not reached | A2 — base-ticker tenure rule for recycled tickers (HR/DOC/RPT/COR) + rename-at-death completions (FOX/FOXA, LB, PCLN, CBL, RVI, OPI→OPITQ) (dbb6543) |
| 3 (dbb6543) | PASS in full, worst cell 0.9963 | STOP: IUCD 0.9193, IUCM 0.9468 — EW top-15 misprices mega-cap-concentrated heterogeneous lines | A3 — true-weight baskets; Step-0 weights stage parsed all 4,836 snapshots from the local raw cache, zero network (61359de); ZH sign-off 2026-07-19 |
| 4 (61359de) | PASS, worst 0.9963 | PASS all 11 lines, 0.9588 (IUSP) – 0.9985 | Register run ONCE → status COMPLETE (commit 320a4bc) |

### Register (net Sharpe; window ≈ 2018-10 → 2026-06, 1,937 trading days, 389 W-FRI rebalances; full-vector name-level costs; E0 on the deployed 2-9 bps model)

| Arm | @5 bps | @10 bps (2×, binding) | MaxDD @5 | corr vs E0 | TE ann | turnover ×E0 |
|---|---:|---:|---:|---:|---:|---:|
| E0 deployed ETFs | 1.005 | 1.005 | 30.6% | 1.000 | 0.0% | 1.00 |
| I0 unscreened replication (control) | 0.983 | 0.937 | 30.3% | 0.996 | 1.6% | 1.04 |
| I1 screened replication (Design 1) | 0.921 | 0.868 | 28.0% | 0.985 | 3.2% | 1.20 |
| I1-all (no top-M cap, report-only) | 0.928 | 0.876 | 26.8% | 0.985 | 3.2% | 1.15 |
| I2 top-10 strength (Design 2) | 0.885 | 0.814 | 27.8% | 0.948 | 6.1% | 1.79 |
| P2 top-10 momentum placebo | 0.882 | 0.810 | 28.8% | 0.953 | 5.9% | 1.81 |
| I2-N15 (report-only) | 0.961 | 0.897 | 27.2% | 0.965 | 4.9% | 1.55 |
| P2-N15 (report-only) | 0.929 | 0.863 | 27.9% | 0.965 | 5.0% | 1.60 |

### Verdict application (frozen rules, §2 of kickoff)

- **ADOPT-D1 requires all four**: (1) I1 ≥ E0−0.05 @5 bps → 0.921 vs 0.955 **FAIL** (drag 0.083); ≥ E0−0.10 @2× → 0.868 vs 0.905 **FAIL** (drag 0.137). (2) MaxDD ≤ E0+2pp → 28.0 vs 32.6 PASS. (3) corr ≥ 0.95 → 0.985 PASS. (4) screen-drag: I1 ≥ I0−0.03 → 0.921 vs 0.953 **FAIL** (screen costs 0.062, double the allowance, buying 2.2pp MaxDD). → REJECT.
- **ADOPT-D2 requires D1 bars + both margins**: I2 fails D1 bars vs E0 (0.885; corr 0.948 < 0.95); margin vs P2 **+0.003** and vs I1 **−0.036** against the +0.10 bar → REJECT decisively. Jaccard I2-vs-P2 0.678 (< 0.8, formally not momentum-in-disguise) but weekly return correlation 0.988 — different names, same portfolio. N15 disclosure row beats its placebo by +0.032, still a third of the bar. WS5 prior confirmed at the implementation layer.

### Split-half and disclosure

- Ordering preserved in both halves (I1 drags I0 in both: −0.084 first, −0.041 second; I2−P2 = −0.034 first / +0.049 second — noise around zero). E0 0.767/1.352; I0 0.709/1.380.
- Worst single-name weeks (contribution to sleeve week): I1 — ABBV 2020-03-20 −3.07%, XOM 2022-06-17 −1.77%, XOM 2023-10-06 −1.60%. I2 — XOM 2022-06-17 −2.95%, TSLA 2021-02-26 −1.86%. The concentration tail the screen was designed against; its MaxDD is indeed lowest, at the Sharpe price above.
- Screened-arm fallback (<3 names passing → line to ETF): I1 12 of 2,096 line-weeks; every other screened arm 1.
- Trial register: 8 arms × 4 cost points, register run ONCE, no post-hoc arm selection; the three amendments were data/design-layer under gate STOPs with zero results computed. Configurations searched: none beyond the pre-registered set.

### Reported, not verdict-relevant

**I0 — unscreened true-weight replication — passes every numeric fidelity bar it can be read against** (drag 0.022 @5 bps and 0.068 @2× vs the −0.05/−0.10 floors; corr 0.996; TE 1.6%; MaxDD 0.4pp better; turnover 1.04×). It was a frozen control, not a registered adoptable arm, so it cannot be promoted post hoc. If the book-structure motivation (single-stock content, fee de-stacking) still stands, the evidenced path is a NEW one-arm pre-registration of I0-as-deployable with explicit seen-data caveat, ops/corporate-actions scoping, and staged parallel-run after the 2026-08-07 Norgate soak closes. The A3 weights table (complete, validated, cache-only) is now a reusable data asset.

**Filed**: technical record `reviews/2026-07-19_ws6_single-name-implementation.docx`; ledger row updated; kickoff §5b carries A1/A2/A3. Engine chain b12d0f9 → 54f0f14 → dbb6543 → 61359de; gate reports 24aa6d0, 05561d6, 998df92; COMPLETE 320a4bc.

## Workstream 8 — REIT dual-coverage ablation and the overlap-gate repair (2026-08-05, CLOSED)

Builds directly on WS2 (2026-07-02, `reviews/2026-07-02_ws2_universe.docx`). WS2
found the XLRE/VNQ pair at 0.990 weekly return correlation, recorded it as
"deliberate dual-signal coverage, US-only" (`run_ws2_trend_map.py:53`), and
adopted the overlap rule "reject candidates above 0.9 versus an incumbent unless
distinct exposure is argued in writing". It did not test the pair: the two
pre-registered prune bundles were B-VGK and C-{TAN,SKYY,PAVE}, and the rule is
prospective by construction, so no incumbent has ever been screened against it.
WS2 also quantified look-through for the other deliberate duals (SPY, QQQ, IJR)
but not for REITs.

Trigger: owner question, 2026-08-05 — "why do REITs appear in both A and B, is
it supposed to appear in only one category?"

### Pre-registration (fixed before any result was inspected)

Two variants, from correlation evidence alone. No K re-tuning, no floor or gate
changes, no other combinations.

- **V1** — sleeve B drops VNQ, sleeve A unchanged. K_B stays 7 of now-11.
- **V2** — sleeve A drops IUSP, sleeve B unchanged. K_A stays 7 of now-13; the
  cross-sectional demean is recomputed on 13 members, which is the mechanical
  consequence of the drop, not a re-tune.

Both directions were run deliberately. Testing only V1 would presuppose that the
momentum line is the redundant one; the pair is symmetric until evidence says
otherwise.

**Keep bar** (WS2 P1/P2 convention, judged at blend level with the varied sleeve
spliced into 35/35/10/20): test-half Sharpe not worse than the deployed blend,
AND at least 4 of 6 full sub-periods at or above it, AND the sleeve survives 2×
cost. The incumbent wins ties, so "no change" is a legitimate outcome.

### Baseline integrity — the trap that had to be cleared first

The cached WS2 baselines (`data/ws2_baseline_*.parquet`) are NOT usable as the
comparator. Their sleeve B still holds EEM (pre-Phase-29 — EEM carries mean
weight 5.5% and appears in 868 weeks of that frame), and their sleeve D predates
both the Phase 30 European rebuild and the 2026-08-03 EXH3→EXH4 instrument
correction. A VNQ-drop measured against them would have priced three changes as
one. Baselines were therefore rebuilt on today's deployed configuration over the
same fixed window. Drift versus the cached WS2 meta, reported rather than
absorbed: A −0.022, B −0.007, C −0.041, D **+0.039**, blend −0.009. The D
improvement is the EXH4 correction arriving in the blend.

Window 2018-11-08 → 2026-07-17 (n = 1,929 trading days), split 2022-09-08.
Panels read from committed parquet caches rather than through `download_prices()`
so the run is offline and does not rewrite files shared with concurrent sessions.

### Register

Rebuilt baselines: A +0.9695 full / +1.2812 test, MaxDD −30.6%, turnover 16.6×.
B +0.9994 / +0.9334, MaxDD −13.2%, turnover 12.1×. Blend +1.1867 / +1.4690.

| Variant | Sleeve full | Sleeve test | 2× cost | Δ full | Δ test | cons | Blend Δ full | Blend Δ test | Blend cons | Verdict |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| V1 B drops VNQ | +1.0002 | +0.9179 | +0.980 | +0.001 | −0.015 | 3/6 | **+0.004** | **−0.005** | 3/6 | KEEP INCUMBENT |
| V2 A drops IUSP | +0.9573 | +1.2497 | +0.940 | −0.012 | −0.032 | 2/6 | −0.007 | −0.012 | 2/6 | KEEP INCUMBENT |

**Both variants fail all three legs of the bar.** V1 is the closer call — it is
free on the full window and improves sleeve B's max drawdown from −13.2% to
−11.7% — but it loses the test half, manages 3 of 6 sub-periods, and does not
survive 2× cost (+0.980 against a +0.999 baseline). V2 is worse on every axis and
does not even buy drawdown (−30.64% against −30.62%).

**Verdict: KEEP BOTH.** The dual coverage survives its first actual test. It now
rests on evidence rather than on one line of comment.

### Look-through — the WS2 gap, closed

Effective NAV weights on the weekly rebalance grid (473 weeks), sleeve share
applied (A 35%, B 35%):

| Line | Mean of NAV | Max of NAV | Weeks held |
|---|---:|---:|---:|
| IUSP (A) | 1.61% | 10.65% | 32.8% |
| VNQ (B) | 1.96% | 12.18% | 35.9% |
| **Combined** | **3.57%** | **20.26%** | both held 28.5% |

For comparison, WS2's accepted duals: SPY mean 3.98% / max 10.36% / both 43.2%;
QQQ 6.79% / 24.08% / 42.7%. **The REIT pair is a smaller structural double-count
than either**, on both mean look-through and simultaneous-holding frequency.

Note the live snapshot that prompted the question is an outlier, not the norm:
at the 2026-07-31 anchor IUSP sat at 9.77% and VNQ at 3.31% of NAV, 13.07%
combined — high against a 3.57% mean, but inside the historical 20.26% peak.

### Companion finding — the overlap gate was not enforcing either rule

Auditing the book to place the REIT pair in context exposed three defects in the
candidate gates (`check_universe_candidates.py`, `check_thematic_candidates.py`),
all of which let overlap through:

1. **Basis mismatch.** Both scripts correlated weekly RETURNS and compared to
   0.85, citing the Phase 5 threshold — but Phase 5 (`run_phase5_correlation.py:11`)
   and the Phase 25 screen (`run_thematic_universe_screen.py:18`) both measured
   weekly SIGNAL correlation, the quantity the sleeves rank on. Signal series are
   slow and strongly autocorrelated, so 0.85 on returns is a looser gate than
   specified.
2. **Sleeve-scope asymmetry.** Sleeve C candidates were screened against sleeve
   A's sector slate as well as C's members — that is how XOP, OIH and AMLP were
   rejected, all three on XLE. Sleeve B candidates were screened against B
   incumbents only. Applied to one sleeve, not the other.
3. **Silent NaN collapse.** A whole-frame `rolling(200, min_periods=200)` over a
   panel spanning the NYSE, Xetra, Shenzhen and 24×7 crypto calendars returns
   all-NaN — every 200-row window of every column contains another calendar's
   dates. Every pair is then skipped for want of overlap and every candidate
   returns PASS having been compared against nothing. This is the failure mode
   that looks exactly like success.

Both rules now run book-wide against the deployed universe resolved through
`scanner_universe.resolve_universe()`, so the gate, the daily scanner and the
engines cannot disagree about what is held. Signal is computed per column.
Regression-checked against documented figures: XOP 0.948 (Phase 5 0.947), AMLP
0.854 (0.853), SLV vs GLD 0.780 (Phase 16 0.78), ITB passes as it did in Phase 5.
**VNQ re-screened as a fresh B candidate now fails on both rules against IUSP
(signal 0.984, return 0.990); under the old B-side gate its top within-B
correlation was 0.743 and it would have passed.**

### Retrospective audit (`--audit`)

18 incumbent pairs sit above the 0.90 rule. Three are labelled PROXY-IDENTITY —
sleeve A is priced through the very ticker sleeve B holds (CSP1→SPY, CNDX→QQQ,
IDP6→IJR), so the panel carries one series under two names and the ~1.000 is
structural, not measured. The exposure overlap is real (WS2's US-beta cluster,
mean 46.8% / peak 83.5% of NAV) but the coefficient is not evidence about it.
Labelling them keeps three permanent false positives off the top of the list,
which is how a guard trains its readers to skip it.

Of the 15 measured breaches, one is not covered by any prior study: **VGK (B) ~
EXH3 (D) at 0.913 and EFA (B) ~ EXH3 (D) at 0.903** — sleeve B's Europe lines
against sleeve D's Europe sector line, now correctly priced as EXH4.DE
(industrials) after the 2026-08-03 correction. The others are documented (EFA/VGK
0.984 prune-tested and rejected in WS2 P1; XLI~PAVE 0.954 prune-tested and
rejected in WS2 P2; TLT/IEF 0.918 a deliberate duration ladder; ICLN/TAN 0.925
inside the rejected P2 bundle).

### Trial register

Configurations evaluated: 2 (V1, V2), each at 1× and 2× cost, registered once,
no post-hoc variant selection and no third direction added after seeing results.
The look-through table and the `--audit` sweep are descriptive, carry no
free parameters, and are not verdict-relevant.

### Caveats

- Both sleeves keep their REIT line on evidence of *no improvement from
  removal*, not evidence of positive contribution. The test asked whether the
  second line earns its place, and the answer is that removing either costs
  more than it saves. That is a weaker claim than "REITs add alpha" and should
  not be quoted as one.
- V1 removes B's line while A keeps IUSP, so the blend result measures the
  marginal value of the SECOND REIT line, not of REIT exposure.
- Sample noise dominates the margins: the blend deltas (+0.004 / −0.007 full)
  are far inside the ±0.4 Sharpe standard error the README already flags.
  The honest reading is that the choice does not matter much either way, which
  is itself the argument for leaving a working configuration alone.
- The 15 measured audit breaches are reported, not actioned. Each would need
  its own pre-registered ablation; WS8 is the worked example of what that costs.

**Filed**: technical record `reviews/2026-08-05_ws8_reit-dual-coverage.docx`;
plain-language summary covering all four owner questions
`reviews/2026-08-05_ws8_universe-questions_summary.docx`;
evidence `data/ws8_reit_overlap.json`; engine `scripts/run_ws8_reit_overlap.py`
(commit ede814a); gate repair + 5 guard tests (commit fc4234a); ledger row added.

### Follow-through — the universe monitor (question 4)

`scripts/run_universe_monitor.py`, scheduled monthly by
`.github/workflows/universe_monitor.yml` (1st, 07:00 UTC). Diffs the
Nasdaq-traded symbol directory against `data/etf_catalogue_snapshot.csv`,
screens launches through the same book-wide gate, reports closures and alerts
by email if a closed line is one the book holds. Advisory: it runs no engine
and any addition still needs a pre-registered ablation.

Two things worth recording because they were found the hard way. First, the
row-count guard caught a bug in the monitor's own snapshot writer on the first
run — a comma in the provenance line made `csv.writer` quote it, the loader's
comment filter missed it, and the baseline parsed to zero rows; an unreadable
snapshot is now fatal rather than being treated as empty. Second, the first
live diff surfaced a genuine launch (DLCU) that yfinance had no history for,
which produced no screening record at all; once the snapshot advanced the line
would have been lost for good — the monitor would have seen the launch it
exists to catch and dropped it silently. Unscreened lines now carry forward in
the report and are re-screened every run until they can be evaluated.

The free symbol directory carries **no AUM**, so the liquidity screen is not
yet meaningful; whether to buy a catalogue that does is open. 10 guard tests
in `tests/test_universe_monitor.py`; suite 678 passing.

## WS12 / WS13 — execution timing (2026-08-12)

Filed record: [`reviews/2026-08-12_ws12-ws13_execution-timing.docx`](reviews/2026-08-12_ws12-ws13_execution-timing.docx).
Engines `scripts/run_ws12_fill_lag.py`, `scripts/run_ws13_execution_grid.py`;
charts `scripts/plot_ws13_summary.py`; record spec `scripts/build_ws13_record.js`.
Commits 4083fbd, 28e1a61, 69a2c5d, 56b8c7c.

**Numbering note.** These two workstreams were begun and committed as "WS11"
and "WS12" before the ledger was read. WS11 was already taken by the
constituent-price survivorship study filed 2026-08-10, so both were renumbered
to WS12 and WS13 at filing. The pushed commit messages 4083fbd and 28e1a61
still say WS11/WS12 in their bodies; the scripts, data, dashboard and this
record carry the corrected numbers. Read the ledger first — that rule exists
for exactly this.

**Question.** Does the deployed Thursday-signal / Friday-close convention carry
look-ahead; what does a later fill cost; and does the weekday or the
open-versus-close choice matter?

**Answers.**

- **No look-ahead.** Every engine reads `get_loc(rd) - 1`, so a Friday
  rebalance ranks on Thursday's close and the position first earns the
  Friday→Monday return — arithmetically a fill at Friday's close. The external
  reconstruction reproduces each engine's own equity to 0.0 absolute error, and
  re-running WS10 on the same panel gives blend Sharpe 1.1533 against WS12's
  1.1530 baseline.
- **Filling the same decision one session later** costs the blend −0.0222
  Sharpe / −0.15pp CAGR. Pessimistic bound: it also leaves the signal a session
  staler than the live workflow would.
- **A W-MON grid** (weekly-close signal, Monday fill) returns +0.0336 Sharpe.
  It required the new `holiday_aware_next` forward-roll mode, and it does not
  solve the operational problem — the Monday close is 04:00 SGT Tuesday.
- **Open versus close ties on four of five grids.** Monday is the exception:
  −0.0508 with a 90% paired interval clear of zero. Its opening auction prices
  the whole weekend in one print.
- **Friday open is the recommendation** (not adopted): +0.0299 against the
  Friday close with the interval straddling zero, −0.0065 at doubled cost, and
  it moves the fill from 04:00 SGT Saturday to 21:30 SGT Friday. It is the
  deployed decision executed earlier in the same session.
- **Wednesday tests best and is rejected** — 1.2958 against the deployed
  1.1623, interval clear of zero, but selected as the best of five, uncorrected
  for multiplicity, sleeves disagree on the best day, and no mechanism.
- **Sleeve C cannot cross a rebalance at one moment** (US + Shenzhen + crypto).
  A, B and D each sit on one venue.

**Method notes worth carrying forward.**

- The ~0.36 unpaired Sharpe SE used elsewhere in this book is the WRONG
  yardstick for these comparisons. Weekday grids and fill points run on one
  history and are heavily correlated, so the SE of the *difference* is far
  smaller. All inference here uses the paired moving-block bootstrap from
  `run_phase7_bootstrap` (60d blocks, 2000 samples, seed 42).
- Guards that earned their place: the mirrored open-panel builder must
  reproduce the engine's Close panel (RELATIVE tolerance — yfinance recomputes
  `auto_adjust` factors between fetches, so a cached and a fresh panel differ
  by ~2e-6 relative); the two-stage open-fill formula must collapse to the
  close fill when O = C; a forward roll must keep one decision per ISO week.
- A guard that was wrong: an early direction test asserted no rebalance later
  than Wednesday, which rejected correct behaviour when Xetra shut Mon 24 to
  Wed 26 December 2018 and the roll correctly landed on Thursday 27th.

**Not run, and therefore not claimed:** split-half or sub-period consistency on
the weekday surface; any out-of-sample test of the Wednesday result; sleeve A
priced on the LSE-listed UCITS actually held rather than its US proxies.

**Open.** Owner decision on the Friday-open fill and the implied Friday-morning
refresh. Nothing in the deployed automation has been changed.

## WS14 — sleeve A priced on the London UCITS lines (2026-08-12)

Filed record: [`reviews/2026-08-12_ws14_sleeve-a-lse-pricing.docx`](reviews/2026-08-12_ws14_sleeve-a-lse-pricing.docx).
Engine `scripts/run_ws14_sleeve_a_lse.py`; chart `scripts/plot_ws14_summary.py`;
record spec `scripts/build_ws14_record.js`. Commit 8766cd6. Closes the open
item WS13 left behind.

**Question.** Sleeve A signals on UCITS constituent breadth but PRICES through
US trading proxies (CSP1→SPY, CNDX→QQQ, IUES→XLE). Does its result survive on
the instruments actually held?

**Answer: yes.** Like-for-like on 13 names from 2018-10-12:

| Pricing basis | Sharpe | CAGR | Max DD |
|---|---|---|---|
| US proxies (deployed method) | +0.8139 | +13.55% | −29.68% |
| London UCITS lines | +0.8107 | +12.82% | −30.19% |
| Difference | −0.0032 | −0.72pp | −0.50pp |

The venue substitution is not what drives sleeve A. The CAGR gap is the more
real of the two and is consistent with UCITS fee and tracking drag. London-leg
cost stress: 0.8107 / 0.7900 / 0.7693 at 1× / 2× / 3×.

**Three things assumption would have got wrong.**

- **CSP1.L and IUSP.L quote in GBp (pence)**; the other eleven in USD. CSP1.L
  prints ~61,798 — as pence USD 834, as USD absurd. Currency is read per
  ticker and an unknown one is fatal, so a mixed-currency sleeve cannot be
  built by accident.
- **SOXX has no London line** (`SOXX.L` 404s). Both legs therefore run the
  same 13 so the comparison isolates venue, not universe. Dropping SOXX costs
  ~0.10 Sharpe against the deployed 14-name sleeve — a separate, universe
  effect.
- **The proxies track related but different indices** — capped Select Sector
  against plain GICS. Weekly correlations 0.883 (XLRE/IUSP) to 0.954
  (XLE/IUES). The deployed price series differs from the held instrument by
  index construction as well as venue.

**Two guard corrections, both mine.** The pair check first ran on DAILY returns
with a 0.55 floor and rejected the two pence lines; the LSE closes at 11:30 New
York, so daily returns cover different windows and every pair scores 0.46–0.75
regardless of correctness — the test belongs on weekly returns. A 0.90 weekly
floor then rejected XLP/IUCS and XLRE/IUSP, asserting a precision the proxy
substitution never had; the floor is 0.70, which still catches a wrong fund
near zero.

**Not addressed.** The tax dimension (Irish-domiciled UCITS at 15% treaty
withholding against 30% on US-domiciled for a Singapore holder, and US estate
tax on US-situs assets) is a question for a tax adviser, not a backtest. What
would replace SOXX in a London-listed implementation is open, as is the
realised spread on the thinner lines.

### WS12/WS13 — DECISION: Friday-open fill ADOPTED (2026-08-12)

> **SUPERSEDED the same evening.** This section is kept as the record of
> what was decided at the time; see the reversal section below. Execution
> is the Friday CLOSING auction via market-on-close orders.

Owner approved the Friday-open fill. Three things were settled with it.

**Execution.** The book now fills at the rebalance date's OPEN, not its close.
The decision is unchanged — a W-FRI grid still ranks on Thursday's close — so
only the moment of execution moved: 21:30 SGT Friday (22:30 winter) for
sleeves A/B/C on the US session, 15:00 SGT for sleeve D on Xetra, instead of
04:00 SGT Saturday.

**The engines are NOT being rebuilt to model it, deliberately.** Every
published figure remains close-to-close. The gap was measured before the
decision: +0.0299 Sharpe at the blend, −0.0065 once the assumed cost is
doubled, paired interval straddling zero. Rebuilding four sleeve data paths to
carry opening prices — including sleeve C's crypto-calendar / FX /
expense-ratio chain — would restate every published number in order to move
one that does not move. So the record models a fill about six and a half hours
later than the one actually taken, and that is disclosed in the Execution
Timing tab's opening verdict and in a callout under the sequence, not
footnoted.

**Refresh moves off Saturday to Friday morning SGT**, so the instruction exists
before the fill rather than after it. It is operator-run, not scheduled: the
per-constituent caches are gitignored, so CI cannot compute sleeve A or D
breadth. No cron, no gate and no alarm was changed — the weekly factsheet keeps
its completed-week anchor and continues to publish over the weekend, because it
explains a rebalance rather than gating one.

Filed record `reviews/2026-08-12_ws12-ws13_execution-timing.docx` revised the
same day to carry the decision; the version filed earlier recorded it as
recommended and not adopted.

## WS15 — survivorship on the published CNDX record, and the residual WS11 missed (2026-08-13)

**Question.** WS11 corrected the constituent-price panels and restated the deployed
blend, but the CNDX-specific published surfaces were never re-measured: the
cross-ETF OOS backtest (`data/backtest_cndx_oos.json`, computed 2026-05-17 on the
survivor panel, 87 signal-fire days) still carries the pre-correction figures, and
the README's cross-ETF OOS table cites them (`0.19/22 · 0.29/19 · 0.51/39`). What
was survivorship worth on that record, and is the WS11 correction itself complete?

**Method.** Five legs, each isolating one change, all trading one freshly-pulled
QQQ/SPY basis: T1 May code x May panel (must reproduce the published file — it
does: trade counts and win rates exact, return/Sharpe to ~3e-4 relative, seeded
MC within ±2pp); T2 today's code x May panel; T3 today's code x the last survivor
panel (git `1ada87b`, end 2026-08-07); T4 x the committed WS11-corrected panel
(same window, roster file and breadth code as T3 — the clean survivorship pair);
T5 x the WS15 residual-fixed panel. Breadth legs computed by a driver that first
reproduced the committed corrected series EXACTLY (2,158 days, 43 signals, every
6dp value and signal identical) before being trusted on the patched cache. The
Norgate coverage gate (em-rotation-lab step0 pattern) PASSED before any pull:
NDU same-day, delisted archive 21,099 symbols back to 1990, all 27 fill/evidence
symbols name-verified with held windows covered.

**Finding 1 — the WS11 "corrected" panel still dropped Facebook for 4.4 years.**
The backfill only treated absent or all-NaN columns, so a column holding
unrelated ticker-reuse bars counted as priced: FB (1,115 roster-days missing,
2018-01-05 to 2022-06-09; the column held only the 2025+ ProShares ETF), FOXA/FOX
(295/296 days, 21st Century Fox era; columns held only post-split Fox Corp),
PCLN (38 days). Plus two 2026 defects: EA's final 11 tradable sessions (yfinance
stopped serving it three weeks before its 2026-08-04 delisting; Norgate
`EA-202608` has them) and MNST's 14-session hole around its 2-for-1 split of
2026-08-11. WS15 filled all of these on a working COPY (point-in-time resolution,
every mapping verified against `security_name`, same-security splices rescaled
onto the column's own basis — MNST's ratio came out at exactly 2.000000, the
split factor — and era barriers so no indicator window spans two securities).
Residual roster-day gaps: 1,805 → 35, all stale-roster tails or by-design
exclusions. The 24 WS11 fill columns were also extended through the 2017-07
warmup they lacked. 2018 median coverage: survivor 81.6% → WS11 97.1% → WS15
100.0%.

**Finding 2 — the published OOS row was three-quarters data-vintage artefact.**
Headline variant (`regime_time_only_delay5_trend`): published +44.5% / Sharpe
0.51 / MC 39.6. Code evolution since May: nil on deterministic stats. The August
roster rebuild + vendor re-basing + 3 more months (T2→T3): −26.2pp total return,
Sharpe 0.51 → 0.25, and only 35 of the 87 published signal-fire days survive the
refresh at all. Survivorship (T3→T4, the clean pair): +4.5pp / +0.04 Sharpe —
correction IMPROVES the OOS stats (8 fire days suppressed, 1 fabricated), the
OPPOSITE sign to the sleeve-A blend effect WS11 measured, which is why direction
is measured and never assumed. The WS15 residual (T4→T5): −2.8pp / −0.02. Net:
the current-truth row is +20.0% / 0.27 / MC 18.3, and the other variants are
worse (baseline_2xATR −18.6% / −0.32 / MC 1.9; regime_time_only +4.4% / 0.10 /
MC 5.7). Every variant sits well below its random-entry null median; the
published row already did (MC 39.6), and the restatement makes it starker. The
CNDX thrust edge, as published, does not survive its own data corrections.

**Finding 3 — a latent Monte Carlo defect in `backtest.py`, fixed.** The Phase
10.2 non-overlap sampler (2026-05-25) placed each random entry uniformly over
all remaining feasible positions, reserving room at only the MINIMUM holding:
on the CNDX re-run (13 trades, 596 holding sessions, ~1,750-session window) all
1,000 paths came back partial, every one was discarded, and every MC field was
None. No committed artefact carries the damage — nothing was regenerated between
that commit and today — but any regeneration would have shipped an empty null.
Replaced with a gap-transform placement (bootstrap holdings, sort iid draws on
the exact feasible box, shift by occupied space) that cannot dead-end a feasible
configuration; pinned by `tests/test_mc_nonoverlap_sampler.py` (35 backtest
tests pass). MC percentiles are comparable within T2–T5 (one definition, one
seed); T1's MC is the published (pre-non-overlap) definition.

**Finding 4 — classifications, from evidence.** MNST: LIVE in Norgate; the
yfinance series is currently MIS-ADJUSTED (pre-split bars unhalved beside
post-split bars, Monday 2026-08-10 missing entirely) — an ingestion hazard for
the next refresh, flagged as urgent-operational below. EA: delisted 2026-08-04
(`EA-202608`). SPCX (SpaceX Class A) and HONA (Honeywell Aerospace): live young
listings inside their 50-session warmup, no defect. HOLX/WBA/ANSS — the queue
entry called them "live listings yet empty": all three were genuine delistings
(2026-04-06 / 2025-08-27 / 2025-07-16), correctly filled by WS11; the claim was
stale when written. `VSNTV UW` was already rejected at the parser by WS11
(`fetch_constituents._us_symbol`).

**Finding 5 — the class is far larger outside CNDX (inventory only, per scope).**
The same held-window sweep across the other 13 US panels: 36,862 residual
roster-days across 199 names. CSP1 9,789 (SIVB 1,269, FB 1,115, FRC 1,091, INFO
1,047, COG 946, LB 902 — the 2023 bank failures are still absent from financials
breadth during the crisis they defined, IUFS carries SIVB/FRC/SBNY too); IUSP
14,387 across 41 names (structural, needs its own diagnosis); IUCS: BFB
(Brown-Forman B) unpriced for the ENTIRE history — an iShares "BFB" vs yfinance
"BF-B" normalisation gap in a live mega-cap. Any fix moves sleeve A's restated
0.9132 again; unmeasured here, queued as a follow-on decision.

**Trial register.** No parameters were tuned and nothing was selected: 5 legs ×
3 pre-existing fixed variants re-priced, 0 new configurations searched.

**Restatement package — STOPPED for sign-off, nothing published touched.**
(1) regenerate `data/backtest_cndx_oos.json` on the corrected (T4) or
residual-fixed (T5) panel with the repaired MC; (2) correct the README cross-ETF
OOS CNDX row and annotate the early-phase per-ETF table (same vintage class);
(3) adopt or decline the WS15 residual fill into the live cache + a
held-window-aware `backfill_delisted_prices` (moves sleeve A a third time);
(4) the MNST basis guard / refetch before Friday's refresh (urgent-operational,
separate from the restatement); (5) the cross-panel residual follow-on. The live
cache, `data/breadth_cndx.json`, `backtest_cndx_oos.json`, README, docs/ and the
factsheet are all UNTOUCHED by WS15.

**Artefacts.** Gate `scripts/run_ws15_gate.py` → `reviews/ws15_gate.json`; fill
`scripts/run_ws15_residual_fill.py` (workdir copy only); breadth driver
`scripts/run_ws15_breadth_legs.py` (exact-reproduction guard); OOS legs
`scripts/run_ws15_oos_legs.py`; compare `scripts/build_ws15_breadth_compare.py`;
charts `scripts/plot_ws15_summary.py` → `reviews/charts/ws15_*.png`; evidence
`reviews/ws15/*.json`; MC fix `scripts/backtest.py` +
`tests/test_mc_nonoverlap_sampler.py`; record
`reviews/2026-08-13_ws15_cndx-survivorship-restatement.docx`.

### WS12/WS13 — DECISION REVERSED the same day: Friday CLOSE, market-on-close (2026-08-12)

The Friday-open fill adopted earlier on 2026-08-12 was reversed the same
evening. Execution is the **Friday closing auction**, via market-on-close
orders submitted Friday evening SGT: Xetra clears 23:30 SGT, the US 04:00 SGT
Saturday, both unattended.

**Two findings drove it, neither in the original work.**

1. **The cost stress did not go far enough.** Widening it from 2× to 4× put
   the break-even at **1.82×**: the open fill stops being an improvement once
   executing there costs about 80% more than executing at the close. An
   opening auction being 80% dearer than a closing one is unremarkable,
   particularly for sleeve D's Xetra lines and sleeve C's thin thematics. The
   tab said "costs nothing measurable"; that was too strong.

2. **The premise was wrong.** The whole change rested on a Singapore operator
   being unable to trade a 04:00 SGT close. A **market-on-close order** is
   submitted during the session and executes in the closing auction with
   nobody awake — NYSE cut-off 15:50 ET, Nasdaq 15:55 ET, so an order entered
   ~22:00 SGT Friday fills hours later. The operational objection had a
   standard order type as its answer and I did not consider it.

**What the close buys beyond cost.** The deepest and best-discovered print of
the day, no spread crossed, and — the reason that does not depend on any
number above — the record is computed on closing prices, so executing at the
close makes the published figure and the fill the same number. The engines
were deliberately not rebuilt for opening prices, so the open would have left
that wedge open permanently.

**The performance case for the open, for the record.** +42bp active return on
80bp tracking error, IR +0.53 against an SE near 0.36, paired 90% CI
[−0.0121, +0.0754] straddling zero, and a sign that reverses across
neighbouring weekdays (MON −0.051, TUE −0.002, WED −0.022, THU +0.014,
FRI +0.030). It survives only while both auctions are charged the same cost.

**Unchanged by the reversal:** the Friday-morning refresh cadence (it precedes
both auctions comfortably), the `holiday_aware_next` mode, the pre-trade CI
check, and the weekday grid.

**Open.** Sleeve D's Xetra MOC support on IBKR is unverified — IBKR may
*simulate* order types where an exchange lacks them, and a simulated MOC is a
market order near the close, not an auction order. Not a blocker: the Xetra
close is 23:30 SGT Friday, an attendable hour, so D can be traded into the
close directly. The higher-value open item remains realised per-sleeve costs
from broker fills, which would settle both this and the 2/2/5/9 bps
assumptions that now do all the work.

## WS16 — cross-panel survivorship closure and the blend restatement (2026-08-13)

Owner-commissioned follow-on to WS15 ("rerun (1)"). The held-window sweep found 84
unpriced names across the 14 US panels; twenty new RENAMED entries (each verified
against Norgate security_name AND quotation window), explicit era fills for the
two-lives tickers (old Chesapeake ← CHKAQ-202102, old Arconic ← HWM, the 21CF
classes in IUCD/IUCM), and a "BFB"→"BF-B" vendor-symbol override cut the residual
from 36,897 to 2,360 roster-days — stale tails, composite roster rows, and old
CBL & Associates (absent from every archive, documented). The 2023 bank failures
(SIVBQ-202411, FRCB, SBNY) now price through their crisis; four 2026 vendor
ticker changes were recovered (MPW→MPT, SATS→ECHO, VSCO→VSXY, AHH→AHRT). Basis
audit: every filled era on one adjustment basis.

RESTATED (owner reviewed the rebuilt dashboard and instructed the push; commits
e4f8eeb machinery, 45bdc92/f201d50 record): A 0.9132→0.9258, B 1.0254→1.0322,
C 0.6447→0.6534, D 0.9440→0.9513, blend 1.1481→1.1613 (CAGR 15.20%→15.34%),
deployed 1.2224→1.2363. Direction UP — opposite WS11 for the third time. B/C/D
carry no constituent exposure, so their ~+0.007 moves are the vendor-drift
controls: roughly half the blend lift is routine re-basing, the remainder the
survivorship fix through sleeve A and the gate's corrected CSP1 breadth feed.
Sleeve D remains survivor-biased (European delisted prices; procurement).

Also shipped: the reduced public page now pins its ENTIRE view (curve, stats
recomputed on the trimmed series, holdings) to the last completed weekly anchor —
under the WS13 Friday-morning cadence the record curves legitimately end on
Thursday's close, so the old build-time date-equality could never hold again
(latest_completed_friday returns LAST Friday on a Friday); parity tests updated
to the anchored-window contract; twelve bound template literals updated and
verified by the figure guard. Date erratum: earlier WS15/WS16 notes said
"Friday 2026-08-15"; Friday is 2026-08-14 (2026-08-13 is a Thursday, verified).
Technical record docx: to follow in a filing session; this memo section and the
ledger row are the interim record.

## Execution integrity — the Friday cadence meets the vendor (2026-08-14/15)

CORRECTION AND DEFECT RECORD, not a pre-registered study. Follow-on to WS12/WS13
(execution timing, 2026-08-12), which ADOPTED the Friday-morning cadence, and to
WS16 (2026-08-13), which anchored the reduced page. Neither tested the
precondition the cadence rests on: that the data a Thursday ranking needs is
actually AVAILABLE on Friday morning. It is not, for sleeve D.

### How it surfaced

A Friday 21:15 SGT run — six hours after Xetra opened — emitted a Strategy D
rebalance dated 14 August switching EXH3 (traded EXH4.DE) out for EXV3, 5.9% of
NAV. It was wrong twice over.

yfinance served a bar stamped 2026-08-14 while Xetra was still two hours from
its 15:30 UTC close. A live quote, taken as a close. Separately, Thursday
13 August — a real Xetra session — was absent from the .DE series entirely:
verified against the calendar, still absent when today's bar was excluded, so a
genuine gap rather than displacement. Because build_trade_history takes
`decision_date = full_idx[i - 1]`, the hole moved the decision to WEDNESDAY.

On Wednesday EXV3 breadth 73.6 beat EXH3 71.6. By Thursday that had reversed:
EXH3 73.0 against EXV3 71.7. A 1.3pp call decided by the wrong session, with no
error raised anywhere. Caught in preparation; nothing was traded on it.

### Five defects, all fixed

1. **No partial-bar guard on sleeves A and D.** `cap_to_last_completed_session`
   has existed for weeks and its docstring names this exact failure — "a weekly
   engine could stamp a rebalance on it". B and C call it. A and D do not: both
   are served by `run_portfolio`, which never did. And D could not have used it
   anyway — it is NYSE-only, and D trades Xetra. The one sleeve that broke is
   the one the existing guard cannot express. Fixed by `session_bounds.py`
   (venue-aware `trim_to_completed`); `cap_to_last_completed_session` now
   DELEGATES to it, verified identical across 320 timestamps spanning 40 days.

2. **The panel stopped at the last published roster Friday.** A bound on when
   the constituent LIST refreshed, used as a bound on how far breadth could be
   computed. Cost four sessions every Friday: the panel ended 7 August while the
   decision read 13 August, so the wrapper's anchor guard could not pass on data
   that was never produced. It stayed hidden because the 2026-08-12 run meant to
   validate the re-timed guard ran `preflight_only=True`, under which the check
   is skipped — it had never once executed.

3. **The decision session was unrecorded.** All four engines computed
   `decision_date` and discarded it, so a rebalance could not say which session
   decided it. That is why the 12-versus-13 August substitution was unreadable
   from the output. Now emitted in every trade record.

4. **Mixed-vintage book.** `_collect_deployed_holdings` took `trades[-1]` per
   sleeve regardless of date, composing a book NOBODY EVER HELD: D on 14 August
   beside A and B on 7 August and C on 31 July. The as-of argument was already
   passed and used for NAV weights; only the trade selection ignored it.

5. **The publish guard had its comparison inverted.** `assert_payload_usable`
   required `as_of == panel_end_date`. Since `as_of = min(panel_end,
   live_anchor)` and the page trims every displayed series to it, the one-date
   contract already held in both directions and the equality added nothing to
   it. What it actually tested was "the panel is not FRESHER than the NAV
   curve", true only when the panel is the staler of the two. It therefore
   PERMITTED the dangerous direction and REFUSED the benign one. The committed
   data proves the cost: panel_end 2026-08-07 against live_anchor 2026-08-12
   gave as_of = 2026-08-07 = panel_end, and passed — publishing a regime
   headline five sessions stale. Same shape as the failure that left the
   2026-03-27 de-risk invisible for eleven weeks.

### The one thing that was tested rather than fixed

Defect 2's repair extends the panel tail, and the claim it rests on is that
this is VALUE-PRESERVING: rosters publish weekly, so every mid-week day already
resolves against the most recent snapshot <= T, and next week's run computes
13 August against the 7 August roster too, because 14 August is not <= 13 August.

`tools/verify_tail_extension.py` runs the real pipeline twice per ETF and
compares every shared date. Its first IUUS run FAILED — 2 discrete
highs_breadth values moved in 2022 and ~1,100 z-scores downstream of them. Two
weaker controls then cleared it for the WRONG REASON: old-bound-twice let both
runs hit a cache an earlier wide run had left, so the download asymmetry never
occurred; old-bound-twice-with-the-cache-deleted removed the `prior` that
`_revert_vendor_step_defects` compares against, disarming a guard that can
revert a whole column. A control must hold the download STILL, not merely
repeat it. With one price frame pinned for both runs, IUUS, EXH9 and CSP1 are
BIT-IDENTICAL on every shared date — both venues, widest universe, 4 days
gained, signals unchanged on the shared window.

SEPARATE FINDING, not introduced here and not fixed here: that first IUUS
result means recomputing a panel against a re-fetched price frame can move
historical breadth. Pre-existing; deserves its own investigation.

### The open question

The Xetra price lines appear to publish about a session late. If systematic,
sleeve D is STRUCTURALLY short on a Friday decision rather than occasionally
short, which is a materially stronger objection to the cadence than the "2
missing sessions in 516 over two years" the historical sweep showed — history
is essentially complete because it backfills; only the live edge lags.

A CORRECTION TO MY OWN FIRST DIAGNOSIS. I recorded that sleeve D's signal "was
never late" and only the ETF wrapper lagged. That was one observation taken on
Friday afternoon and it does not generalise. The first probe sample has the
European CONSTITUENTS one session behind as well (SAP.DE, SIE.DE), against zero
for the US proxies. What survives is narrower: the constituents lead the wrapper
by roughly a session, but both trail the live session. Whether the constituents
carry Thursday at Friday 08:00 SGT — the only moment the cadence question turns
on — is UNMEASURED.

`scripts/probe_vendor_availability.py` measures it; `.github/workflows/vendor_probe.yml`
runs it four times daily at 00/06/12/18 UTC, the 00:00 slot being the decision
hour itself. Deliberately separate from `publication_lag.yml`, which measures
HOLDINGS publication — different series, never to be pooled, and folding this in
would have re-timed a probe whose log already changed sampling once. Guarded by
`check_vendor_probe.py` (fails on nothing appended, a stale row, or every line
empty; a PARTIAL result passes, because one venue answering while another does
not IS the asymmetry). Two fleet_watch rows, git and run.

DO NOT MOVE A REBALANCE DAY on fewer than two or three weeks of those samples.

### Deployed position, unchanged

Rank on Thursday's close, fill Friday at the close, market-on-close. That is
what the engines backtest (`get_loc(rd)-1`), and WS12/WS13 settled it. Nothing
in this week's work reopens that decision; it repairs the machinery that was
supposed to implement it. `scripts/live_targets.py` is the Friday-morning
artefact — it ranks each sleeve on its own signal at the last completed session
on its own venue and reports HOLD rather than ranking a session early.

Commits: 466646b (degraded-endpoint circuit), 9c03cbb (roster integrity),
e710bc5 (panel tail), ea884c7 (session bounds), 1237546 (publish guard),
b4bfd13 (live targets + probe), 79a7b2c (probe schedule), 5225b59 (README).
Suite 1,154 -> 1,239.

### Addendum, same day — the fix above shipped with its own bound defect

Found while investigating why the 14 candidate Europe supersectors would not
recompute. There were no broken tickers: current-roster coverage is 100% on 13
of the 14 and exactly ONE ticker is unresolved across all of them (BT.A.L).
The 404s in the logs — 1624733D.PA, KYGA.IR, SPSN1.SW — are long-delisted
historical constituents, not holdings.

The panels reported "0 of 8 current constituents carry a 50-day average"
because the file-level coverage floor reads the FINAL ROW, and the tail
extension had pushed that row one session past the data: EXH6's roster priced
to 2026-08-13 against a last completed XETR session of 2026-08-14.

`schedule_end` took the last completed session on the VENUE CALENDAR — a fact
about the exchange — where it needed whether the vendor had published the
CONSTITUENT prices for it. THE SAME CONFLATION AS THE SLEEVE D DEFECT, third
occurrence this week.

NOT confined to the research panels. Verified before fixing: EXV1, EXH1 and
EXH9 all carried prices only to 2026-08-13, so every DEPLOYED Europe panel
would have failed identically on its next run — 20% of NAV, surfacing next
Friday as "the European sleeve stopped updating".

THE FIX NEEDED A SECOND PASS, which is the part worth carrying. The first cap
used MIN_BREADTH_NAMES — the ROW-level floor of 5 — while the guard that
refuses the write is the FILE-level 50% coverage floor. For small panels they
agree; EXH3 had 8 of 107 priced on 2026-08-14, above 5 and far below 50%, so
the cap admitted a session the guard then rejected. 17 of 19 wrote, the two
largest refused, one a deployed sleeve member. A CAP THAT DOES NOT SHARE THE
GUARD'S CRITERION IS A SECOND OPINION, NOT A BOUND. It now derives its
threshold from MIN_ROSTER_COVERAGE_FAIL.

Does not disturb the value-preservation result: that concerns historical values
under the extension and still holds bit-identical. This is a bound defect at
the tail, a different failure.

Also corrected the same day: the Data tab labelled ICHN, IJPN, ITWN and NDIA
`deployed` when they are research-only against a sleeve rejected in record
2026-07-02-breadth-thrust-etf-2, and emitted `registered` for pruned IUIT — a
value the page's own legend and filter do not carry, so Monitored matched
nothing and IUIT was unreachable by any filter. Now 19 / 14 / 5.

All 38 panels current: 33 end 2026-08-13, 5 end 2026-08-14 where constituents
price a session further. Commits 72ff181, 9a78a6d. Suite 1,239 -> 1,256.

## The public page refused to build over the overlay's cash reserve (2026-08-15/16)

CORRECTION, not a study — nothing pre-registered. Third correction of the
2026-08-14/15 refresh cluster, and the last of the open items the refresh
commit (b897774) filed as separate work.

The 2026-08-14 book left SHY in `effective_weights` at 6e-05 of NAV, and
`build_simple_page` raised "held but in no sleeve: ['SHY'] — the split would
not sum to NAV", taking all 19 page-parity tests down with it. The suite was
red on main from the refresh (Sat 2026-08-15 21:27 SGT, committed over the
failure on explicit instruction) until the fix pushed on Sunday morning
(2026-08-16 07:30 SGT; red run 31889823354, green run 31914895758).

THE RESIDUAL IS ROUNDING DUST MADE POSITION. Sleeves A and B store
within-sleeve weights at 4dp, each summing to 0.9999; the NAV composition in
`mark_to_market_live._build_effective_weights` leaves 0.35 x 0.0001 +
0.25 x 0.0001 = 6e-05 unallocated, and its cash-residual rule parks anything
above 1e-6 in SHY, the overlay's `fallback_ticker`. Sixty dollars of
Treasuries per million of NAV, under RISK_ON. (The fix commit's "one basis
point" is a display-side round-up; the measured weight is 6e-05.)

THE GUARD WAS RIGHT, AND THE REFUSAL WAS THE MESSENGER. Refusing beats hiding
a position the sleeve split cannot place. The real finding is structural: the
builder special-cased exactly one overlay-held position (EEM, the tilt), so
the page could never have rendered a RISK_OFF book — the state in which the
gate parks derisk_fraction of NAV, half the book on current parameters, in
precisely this instrument. The dust surfaced in calm conditions a failure
that would otherwise have first appeared mid-de-risk.

THREE GAPS IN THE SAME CHAIN (commit f51c21d), so fixing the first alone
would have swapped one build failure for another: the sleeve map had no
bucket; `data/etf_names.json` had no display name, and the page refuses to
print a bare ticker to a non-specialist reader; and `fetch_etf_names.py`
could not have supplied the name, its universe being `resolve_universe()` —
the scanner's 54 instruments, which exclude the fallback.

IT GETS ITS OWN BUCKET, labelled "Cash reserve" — naming the exposure without
disclosing the mechanism, per the SLEEVE_LABELS constraint. Both shortcuts —
folding it into the tilt, dropping it as sub-threshold dust — die on the same
fact: the weight is not always small. The fallback ticker is read from
`risk_overlay.json` at all three sites, so a parameter change cannot silently
reopen any of the gaps. The new parity test pins both ends of the range — the
6e-05 residual and a 50% de-risk — and fails when the reserve is folded into
the tilt, the specific tempting mistake; the module now collects 34 tests.

PALETTE CAVEAT, STANDING. The sixth sleeve hue (green) is NOT CVD-validated:
`validate_palette.js` validated the first five and is not in this repository.
Green was chosen on two established grounds — neither --g nor --r is
referenced in the template, so it carries no gain/loss meaning; and a cash
reserve reading as safety beats one reading as danger. Nothing should be read
as saying it passed the separation checks the first five did.

Commit f51c21d (pushed with e68ab8f). Suite: red 1,251 passed / 27 skipped /
19 errors at bf60f49 -> green 1,346 passed / 27 skipped at the push head (the
count spans the ITWN resolver work landed in the same push).
