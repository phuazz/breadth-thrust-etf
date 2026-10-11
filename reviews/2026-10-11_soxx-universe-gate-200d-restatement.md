# SOXX universe gate on the deployed 200-day signal — restatement

Date: Sunday 2026-10-11. Context: Personal. Project: breadth-thrust-etf. Status: filed (diagnostic; not pre-registered).
Owner instruction to measure, 2026-10-11; owner approval of the three follow-ups (dashboard prose, gate script, filing), 2026-10-11.

## Question

`scripts/run_strategy_a_universe_gate.py` decides whether an ETF may enter sleeve A. Until 2026-10-11 its `load_breadth_panel()` read `series.ma_breadth` from `data/breadth_<etf>.json`, which `compute_breadth.py` writes with `MA_PERIOD = 50`: the share of constituents above their 50-day average. Sleeve A ranks on the share above the 200-day average (`run_ma200_sweep.compute_ma200_breadth`, called at `run_portfolio.py:114` and `live_targets.py:108`). The script's one filed use is the SOXX retroactive admission test of 2026-05-30 (commit 8bbe82bd), which passed SOXX on all four criteria and was quoted on the dashboard Method tab with an explanation built on the walk-forward K sequence. Does the verdict, and does the explanation, survive on the deployed signal?

## Scope of the defect

- Only two scripts called a loader of the 50-day field for this purpose: the gate script and `run_improvements.py`. `run_improvements.py` belongs to the May 2026 thrust-timing study (SOXX, IUES, IUFS, CNDX, CSP1; `regime_time_only_delay5_trend`), which is designed on 50-day breadth, so it is not affected. The eleven other scripts that import `run_improvements` take only `compute_stats`.
- The gate has been run once, for SOXX. No IBB constituent breadth panel exists in any clone, so IBB was never tested despite the Method tab saying it was queued. No other candidate has a gate output.
- The industry-candidate overlap rule (2026-07-02-breadth-thrust-etf-5) runs through `check_universe_candidates.py`, which ranks on distance from the 200-day average and was rebased by WS8. Not affected.
- WS6 (`single_name_impl.py`) builds its own 200-day panel. Not affected.

## Three ways the comparison could have been silently wrong, and the defence

1. **Data vintage confounded with the MA period.** The 2026-05-30 panels predate the WS11 survivorship restatement, the WS16 cross-panel restatement and the WS19b Norgate re-source. Defence: the 50-day arm was re-run on today's data, so vintage drift is reported on its own line.
2. **Window mismatch.** 200-day breadth becomes valid later than 50-day breadth, which moves `eligible_start`, Sharpe, drawdown and the walk-forward segments regardless of the signal. Defence: every arm is scored on one common window (2018-11-02, the stored run's start); native windows are reported beside it.
3. **Construction confounded with definition.** The JSON field uses a strict 50-row window in `compute_breadth.py`; the deployed series uses 90% `min_periods` and the validated-end cap. Defence: a third arm computes 50-day breadth through `compute_ma200_breadth(cp, 50)`.

## Method

One pinned snapshot of the 14 sleeve A panels (breadth JSON, roster JSON, constituent price cache) copied from the `-sched` clone on 2026-10-11, checksums in `2026-10-11_soxx-universe-gate-200d/snapshot_SHA256SUMS`. One yfinance price frame of the registry trading proxies, 2017-09-11 to Friday 2026-10-09, shared by every arm. The gate's own engine (`run_topk_backtest`, `walk_forward_K`, `compute_stats`, thresholds) unchanged; only the breadth panel differs.

- J50: `series.ma_breadth` from the JSON (what the script read).
- C50: `compute_ma200_breadth(cp, 50)`, validated-end capped (construction control).
- C200: `compute_ma200_breadth(cp, 200)`, validated-end capped (the deployed signal).

Script and full output: `2026-10-11_soxx-universe-gate-200d/gate_ma_basis.py`, `gate_ma_basis_out.json` (re-run from the filed copy reproduces the output exactly).

## Results (common window 2018-11-02 to 2026-10-09)

| | Stored 2026-05-30 (J50) | J50 today | C50 today | C200 today |
|---|---|---|---|---|
| Max weekly signal corr (vs CNDX), bar < 0.85 | 0.826 | 0.805 | 0.813 | **0.806** |
| Sharpe at K=7, 13 ETFs / 14 ETFs | 0.660 / 0.716 | 0.631 / 0.707 | 0.662 / 0.775 | **0.828 / 0.920** |
| Δ Sharpe, bar > −0.05 | +0.056 | +0.075 | +0.113 | **+0.092** |
| Δ MaxDD, bar > −5pp | −0.33pp | −0.28pp | −0.22pp | **−0.82pp** |
| Walk-forward Sharpe, 13 / 14 | 0.83 / 1.02 | 0.78 / 0.96 | 0.77 / 1.12 | **1.11 / 1.40** |
| Walk-forward K sequence, 13 → 14 | [3,3,3] → [7,7,7] | [3,3,9] → [3,9,9] | [3,3,3] → [3,5,3] | **[7,7,7] → [9,9,9]** |
| All four gates | PASS | PASS | PASS | **PASS** |

On the native C200 window (from 2018-05-08) the gate also passes: Sharpe 0.785 → 0.873 (+0.089), MaxDD −0.82pp, walk-forward +0.29. This is the figure the corrected script now prints and writes to `data/strategy_a_gate_SOXX.json`.

Signal comparison, weekly, common window: J50 against C50 correlates 0.96 to 0.997 per panel and the Thursday top-7 sets overlap 91% (400 weeks); C50 against C200 correlates 0.57 to 0.69 per panel and the top-7 sets overlap 71%. The construction path barely matters; the horizon matters a great deal.

Full-window Sharpe across K (descriptive, common window):

| K | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 |
|---|---|---|---|---|---|---|---|---|---|
| C200, 13 ETFs | 0.845 | 0.786 | 0.795 | 0.820 | 0.828 | 0.826 | 0.826 | 0.826 | 0.826 |
| C200, 14 ETFs | 0.880 | 0.889 | 0.889 | 0.904 | 0.920 | 0.925 | 0.923 | 0.924 | 0.924 |
| J50, 13 ETFs | 0.620 | 0.625 | 0.638 | 0.633 | 0.631 | 0.633 | 0.636 | 0.635 | 0.635 |
| J50, 14 ETFs | 0.699 | 0.666 | 0.709 | 0.708 | 0.706 | 0.707 | 0.709 | 0.711 | 0.711 |

The surface is flat from K=7 upward (positive-only weights cap effective holdings), so the walk-forward K pick on three annual segments is noise.

## Verdicts

1. **SOXX admission holds on the deployed signal.** All four criteria pass with margin on the 200-day basis, on both windows. No universe change.
2. **The Method-tab explanation is rejected.** The claim that without SOXX the cross-section is too correlated to run K=7 (the refit collapsing to K=3) and that SOXX restores K=7 does not hold: on 200-day breadth the 13-ETF baseline already picks K=7 every year and SOXX moves it to K=9, on a surface flat within 0.005 Sharpe from K=7. Even on the 50-day field, today's data gives [3,3,9] → [3,9,9].

Caveats: three walk-forward segments only; the gate's engine is a slim reproduction (Friday rebalance, US trading proxies, 2 bps), not the deployed book; the C200 14-ETF Sharpe of 0.920 sits close to the filed sleeve A figure of 0.926 (2026-08-13 restatement), a cross-check on a different window and cost basis, so indicative only. The run was not pre-registered; the four criteria and thresholds were fixed in the script from 2026-05-30.

## Changes made (owner-approved 2026-10-11)

- `template.html` and `docs/index.html`: Method-tab SOXX passage restated to the 200-day figures, the K explanation replaced by the flat-surface statement, the "IBB queued" line replaced by "no other candidate, IBB included, has been through the gate".
- `scripts/run_strategy_a_universe_gate.py`: `load_breadth_panel()` builds the panel with `compute_ma200_breadth` and `cap_signal`, as `run_portfolio.py` does (verified identical to the C200 arm, max absolute difference 0.0); gate 4 docstring now describes the walk-forward Sharpe test the code applies, with the K sequence printed for reference only.
- `data/strategy_a_gate_SOXX.json` regenerated on the 200-day basis.

## Trial register

Three signal arms × two windows of the unchanged four-criterion gate (6 evaluations), plus a descriptive K sweep (9 values × 2 universes × 2 arms). Nothing selected from the sweep; no parameter changed.

## Builds on

2026-07-02-breadth-thrust-etf-1 (200-day on a flat plateau), 2026-07-02-breadth-thrust-etf-5 (overlap rule), 2026-08-05-breadth-thrust-etf-1 (WS8 basis-mismatch precedent), 2026-08-10-breadth-thrust-etf-1 and 2026-08-13-breadth-thrust-etf-2 (survivorship restatements behind the vintage drift).
