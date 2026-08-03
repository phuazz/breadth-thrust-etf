# Strategy Research-Review Prompt — breadth-thrust-etf

Prompt for a Claude Code session (Fable 5) to review and improve the strategies behind
[phuazz.github.io/breadth-thrust-etf](https://phuazz.github.io/breadth-thrust-etf/).

## How to run it

- **Model / settings:** Fable 5, **highest reasoning/thinking effort**, **Fast mode OFF** (`/fast`
  toggles it). This is deep multi-hypothesis research where reasoning depth matters and latency does
  not.
- **Mode:** review-and-propose first. Do not let it edit `template.html` / `docs/` until a specific
  change is approved.
- **Stage it across sessions** (protects against running out of tokens mid-run):
  - Session 1 → Workstream 0 + Workstream 1 (moving averages).
  - Session 2 → Workstream 2 (universe).
  - Session 3 → Workstream 3 (heavy robustness gate on the frozen shortlist). Deferrable.
- Tell it to write intermediate findings to `data/` and a running memo so nothing is lost between
  sessions.

---

## The prompt (paste from here)

```text
# CONTEXT
- Repo: breadth-thrust-etf (this folder). Personal research artefact; live dashboard at
  phuazz.github.io/breadth-thrust-etf. Stage: RESEARCH — everything is tweakable.
- Objective: improve robust OUT-OF-SAMPLE expectancy of the strategies. Not in-sample Sharpe.
- Deployed strategy: 4-sleeve breadth+momentum ETF rotation, blend 35/35/10/20 (A:B:C:D),
  plus a CSP1 breadth regime gate (Phase 19) and an EEM/SPY golden-cross tilt (Phase 22).
  ~28 phases of sequential iteration on a single 2018-2026 window.
- Architecture: signals in scripts/run_*.py -> data/*.json -> scripts/pipeline.py injects into
  template.html -> docs/index.html. template.html is 539KB; docs/index.html and most
  data/*.json are large.

# GROUND TRUTH (verify before trusting — the README stops at Phase 24; code is at Phase 28.7)
Moving averages currently in use (single-horizon, NO vol adjustment anywhere):
- Sleeve A (US sectors) & D (Europe sectors): constituent breadth = share of constituents above
  their 50d MA (scripts/compute_breadth.py MA_PERIOD=50), used as sector-RELATIVE breadth
  (sector minus cross-sectional mean), top-K long-only via top_k_breadth_weight
  (scripts/run_portfolio.py:199).
- Sleeve B (asset-class, 13) & C (thematic, 25): ETF-level momentum = % above own 200d MA,
  top-K by signal (run_asset_class_rotation.py:288, run_thematic_rotation.py).
- EEM tilt: 50d/200d golden cross on the EEM/SPY ratio (run_risk_overlay.py:120).
Sleeve membership:
- B = SPY IJR QQQ EFA VGK EWJ EEM VNQ GLD DBC TLT IEF TIP (+ SHY cash floor).
- C = ARKK CIBR SKYY BOTZ BLOK ICLN TAN LIT URA XBI ARKG JETS GDX COPX MOO XME WOOD REMX
      CQQQ 159801.SZ PAVE ITA BTC-USD PHO IHI.
- D = EXV1 EXH1 EXV3 EXH3 EXH9 (panel keys; traded as EXV1.DE EXH1.DE EXV3.DE
      **EXH4.DE** EXH9.DE — EXH3's panel is Industrial Goods & Services and its
      fund is EXH4.DE, corrected 2026-08-03. All pre-2026-08-03 D and blend
      statistics are superseded; see
      `reviews/2026-08-03_sleeve-d-exh3-correction.md`).
Existing robustness harness — USE and EXTEND, do NOT reinvent:
  run_robustness.py (walk-forward L, borrow-cost, 6 sub-periods, block-bootstrap Sharpe CI,
  MA-period sweep {100,150,200,250,300}), run_topk_robustness.py, run_phase7_bootstrap.py,
  run_split_half.py, run_oos_validation.py, run_risk_overlay_validation.py,
  run_em_tilt_validation.py, run_thematic_exit_robustness.py.

# PRINCIPLE — robustness is staged and pragmatic, not academic
Optimise hedge-fund style: fit on a train window, keep only what holds OOS. Two tiers:
- CHEAP REFLEX — run inline on EVERY experiment: (i) one train/test split, and the RANKING must
  hold OOS (this project already found rank is robust, magnitude is not); (ii) sub-period
  consistency — works in >=4 of the 6 regimes; (iii) a 2x-cost check. ~3 numbers. Kill ideas that
  fail on contact. Prefer parameter PLATEAUS over peaks and FEWER knobs. Accept a lower but stable
  OOS number over a high fragile in-sample one.
- HEAVY GATE — run ONCE at the end on the SHORTLIST of surviving changes; DEFERRABLE to a separate
  session if tokens run short. Deflated / haircut Sharpe across the whole search + full-system
  walk-forward. FREEZE the shortlist before this gate; do NOT re-optimise after seeing the haircut.
Write intermediate findings to data/ and a running memo so work survives across sessions.

# TWO DESIGN INVARIANTS (do not violate without explicit evidence)
1. PARAMETER ROBUSTNESS: a chosen lookback/threshold must sit on a FLAT region of its parameter
   surface, not a peak. Fewer knobs wins ties. A high in-sample number on a sharp peak is a red
   flag, not a result.
2. SIGNAL-BY-STRUCTURE: BREADTH for concentrated, correlated single-sector universes (Sleeves A/D
   home turf); PRICE MOMENTUM for anything diversified (Sleeves B/C and any country sleeve).
   Grounded in this project's own finding that breadth dilutes on diversified baskets (CSP1
   underperformed a random-entry null while SOXX did not), plus momentum being a well-replicated
   factor that avoids survivorship and foreign-constituent-data problems.

# WORKSTREAM 0 — ORIENT (read-only; deliver a written map BEFORE any experiment)
Confirm the ground-truth block above against the code. Note any drift from the README.
Produce: a one-page map of every signal, horizon, weight, overlay and its file:line; and a
ranked plan for Workstreams 1-3. Do not edit deployed scripts to run experiments — new
experiments go in NEW scripts under scripts/ (run_*.py) writing JSON to data/.

# WORKSTREAM 1 — MOVING-AVERAGE ROBUSTNESS
Reframe: the goal is NOT to find the best-performing lookback (curve-fitting on one 2018-2026
path). The goal is a formulation whose performance is FLAT across reasonable parameters and
STABLE across sub-periods. Deploy from the flat middle of the surface, not the peak.
1. Parameter surface: for each sleeve independently AND for the blend, extend the MA-period
   sweep to report the Sharpe / CAGR / maxDD SURFACE vs lookback. Flat = robust; peaked =
   fragile. Report the peak-to-plateau gap.
2. Cross-asset horizon heterogeneity — a single window is wrong across asset types (200d on BTC or
   50d on TLT are economically meaningless). Solve it with the FEWEST knobs, in this order:
   (a) PREFERRED — vol-normalised, common horizon: convert the signal to a vol-scaled z-score
       (price - MA)/(sigma*MA) and THEN ensemble across {50,100,150,200}. Vol-normalisation makes
       one horizon set behave comparably across BTC, TLT and equities — cross-asset granularity
       with zero per-asset knobs. Test this jointly with the vol-adjustment in item 3, not
       separately.
   (b) FALLBACK, only if (a) does not equalise behaviour — bucket the universe into 3 momentum-
       speed groups with economic priors: FAST (crypto, single-country EM, thematics), MEDIUM
       (broad/US equity, sectors), SLOW (rates, credit, gold, broad commodity); each bucket gets
       its own ensemble window set. Bucket-level, NEVER per-ticker.
   Either way, chosen windows must sit on a FLAT region of the asset/bucket parameter surface.
   Reject sharp peaks. Fewer knobs wins ties.
3. Vol-adjustment — test each SEPARATELY (and item 2a jointly); each is a new degree of freedom
   that must clear the cheap-reflex OOS bar to be kept:
   a. Graded distance-to-MA normalised by realised vol: (price - MA)/(sigma*MA) instead of a
      binary above/below count.
   b. Vol-targeted sizing: scale each sleeve/holding weight inversely to trailing realised vol so
      ex-ante blend risk is stable across regimes (risk-parity-lite).
   c. Vol-normalised MA slope / ROC gate to separate genuine trend from chop.
4. For every variant report: does it improve the OOS split-half AND sub-period consistency, or
   only the in-sample headline? Reject anything that only improves in-sample. Any turnover
   increase must survive realistic costs.

# WORKSTREAM 2 — UNIVERSE (holistic, comprehensive, well-defined; no missed trends, no overlap)
1. Trend-opportunity map: lay out the exposure space as a grid — US sectors / ex-US DM sectors /
   regions & countries (DM + EM + frontier) / broad asset classes (equity, rates, credit,
   commodity, gold, REIT, crypto) / styles & factors (value, momentum, quality, small, min-vol) /
   thematics. Plot current coverage on it. Identify GAPS where a real persistent trend could run
   with no representation, and REDUNDANCIES.
2. EEM coherence: EEM is currently double-counted — a member of Sleeve B AND the standalone
   overlay. Decide ONE coherent role. Evaluate: overlay only / B-member only / decompose EM into
   a country or regional sleeve. The registry ALREADY has unused country scaffolding
   (etf_registry.py: IJPN NDIA ICHN ITWN, UNIVERSE_COUNTRIES, merge-vs-separate) — reuse it.
3. Overlap control: build a rolling weekly-return correlation matrix over ALL existing sleeve
   members + candidates (full window + trailing 1y). Precedent: IUIT was pruned for 0.97 corr
   with CNDX (etf_registry.py:591). Set an explicit rule — cluster by correlation, keep the most
   liquid/representative per cluster, flag any candidate with >0.9 corr to an incumbent unless it
   adds distinct exposure. Quantify current intra-blend overlap (SPY/QQQ in B vs US sectors vs
   US thematics in C).
4. Signal for countries = PRICE MOMENTUM, not constituent breadth — evidence-led, not merely
   convenient. The project's own result is that breadth works on concentrated correlated universes
   (SOXX) and DILUTES on diversified baskets (CSP1 underperformed a random-entry null). A single-
   country index (~30-80 names across sectors) is a diversified basket, so breadth would dilute the
   same way. Price momentum is a well-replicated factor (Jegadeesh-Titman, AQR) and avoids the
   survivorship and foreign-constituent-data problems. Coherent architecture: BREADTH for
   concentrated single-sector sleeves (A/D), PRICE MOMENTUM for everything diversified (B/C/
   countries). Pair country momentum with a simple own-200d / vol risk gate (do not run it naked).
   Candidates: EWZ EWW EWY INDA EWT EWA EWS EWG EWU EWJ + FM + broad EM (EEM/IEMG). Add the sleeve
   ONLY if a top-K country-momentum basket beats holding EEM+EFA net of cost.
5. Data-integrity gates for ANY addition:
   - Breadth sleeves (A/D) need point-in-time constituents (expensive, iShares/EDGAR); momentum
     sleeves (B/C, countries) need only ETF price (cheap). Prefer the cheap bucket.
   - Survivorship: Sleeve C is already survivorship-biased (25 hand-picked survivors). Adding more
     thematics worsens it unless membership is point-in-time. Prefer long-lived liquid country/
     asset ETFs over more survivor thematics.
   - FX: everything must resolve to USD total return (D is EUR->USD; 159801.SZ is CNY; use the
     USD-listed line or FX-convert). Verify.
   - Capacity: flag names with real fund-capacity limits (thematics, 159801.SZ A-shares,
     BTC-USD/IBIT).
Deliverable: a proposed target universe — one-line rationale per sleeve, the correlation matrix,
and specific adds/drops with evidence.

# WORKSTREAM 3 — HEAVY ROBUSTNESS GATE (runs ONCE on the frozen shortlist; deferrable to its own
# session; do NOT re-optimise after seeing these numbers)
1. Multiple-testing audit: ~28 phases of sequential tuning on the same 2018-2026 path is the
   dominant overfitting risk, and the README concedes several overlays were tuned in-sample.
   Compute a deflated / haircut Sharpe (Bailey-Lopez de Prado deflated Sharpe or Harvey-Liu-Zhu
   haircut) for the deployed blend, given the number of configurations tried. State how many
   effective trials the phase history represents and what Sharpe survives.
2. Full-system walk-forward, not just within-sleeve K: re-fit the WHOLE configuration (sleeve
   weights, per-sleeve K, regime thresholds, EEM MA windows, signal floors) on rolling/expanding
   windows and report OOS.
3. Entry-point discipline (CLAUDE.md): report sub-period returns and the worst 12-month rolling
   window. Confirm deployment is being judged after a flat/negative stretch, not off a strong run.
4. Cost & execution stress: current 2-9 bps is conservative. Stress at 1x / 2x / 3x with wider
   spreads on UCITS lines, thematics, A-shares, BTC, plus the 25 bps IBIT drag and weekly turnover.
5. Structural re-checks: look-ahead (.shift(1) on every new signal), survivorship in C, FX
   consistency, stale-data degradation path.
6. Overlay reality check: the EEM tilt has few distinct ON-events — quantify how many independent
   bets it actually is and whether its contribution is inside noise. Same for the regime gate.
Deliverable: an honest per-component verdict — keep / demote to overlay-sized bet / drop — with the
deflated numbers.

# CONSTRAINTS (house rules)
- Plan first. Deliver the Workstream-0 map + ranked plan BEFORE editing anything. Review-and-
  propose; do not change template.html or docs/ until a specific change is approved.
- Never open template.html, docs/index.html, or any data/*.json > 200KB in full. Check size with
  wc -c; use grep -n + ranged reads + targeted json queries.
- Realistic costs always. Walk-forward where possible. No look-ahead. USD total return throughout.
- Flag every uncertain number. Verify tickers against 2+ sources before adding. British/Singapore
  spelling, no contractions.
- Backtest integrity: before writing ANY new backtest, state the three ways it could be silently
  wrong, then address each in the code.

# SUCCESS CRITERIA
- Must: a research memo with ranked, evidence-backed recommendations for (1) MA formulation,
  (2) target universe, (3) which components survive a robustness haircut.
- Must: every recommendation shows OOS / walk-forward / deflated evidence, not in-sample headline.
- Must: any added degree of freedom (multi-horizon, vol-adjust, new sleeve) justified against a
  stated OOS bar; reject those that only improve in-sample.
- Nice: reusable experiment scripts under scripts/ + JSON under data/.
- Out of scope this pass: rebuilding the dashboard, deploying changes, refetching constituents.

# OUTPUT FORMAT
- Start with the Workstream-0 map and a numbered plan; wait for go-ahead before large edits.
- Findings as a markdown memo; tables for parameter surfaces / correlation matrix / cost stress.
- Any patches via grep anchors + str_replace, shown before applying.
```
