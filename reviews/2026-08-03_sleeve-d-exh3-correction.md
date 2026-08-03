# Sleeve D EXH3 signal/instrument correction (2026-08-03)

## What was wrong

Registry member `EXH3` paired an **Industrial Goods & Services** constituent
panel (iShares product 251948) with **`EXH3.DE`**, which is the Xetra ticker of
the **Food & Beverage** fund. The industrials fund trades as **`EXH4.DE`**.

Sleeve D therefore selected on European industrials breadth and earned the
return of a European food and beverage ETF. The signal was never wrong and the
constituent panel was never wrong — only the instrument the signal was spent on.

Discovered incidentally while building the ETF scanner, from a yfinance fund
name that contradicted the registry comment.

## Evidence

Daily log returns, 2024-08-01 to 2026-08-01 (495–497 observations):

| Test | Correlation |
|---|---:|
| `EXH3.DE` vs this panel's own constituents | **0.244** |
| `EXH4.DE` vs this panel's own constituents | **0.973** |
| `EXH3.DE` vs Food & Beverage majors (NESN.SW, ABI.BR, DGE.L, DANOY, HEIA.AS) | **0.933** |

Annualised volatility: `EXH3.DE` 15.6%, food and beverage basket 16.9%,
industrials basket 24.9%.

The other four sleeve D members sit at 0.935–0.987 against their own
constituents, which is why the defect was invisible: nothing looked broken, and
every label in the repository restated the same wrong sector consistently.
`scripts/pipeline.py` described `EXH3` as "healthcare" — a third answer — which
is the clearest indication that no documentary cross-check could have resolved
this. Only a behavioural one could.

Confirmed independently five times: the constituent holdings themselves (pure
industrials, zero food names), the iShares product slug, the correlation tests
above, `EXH4.DE`'s own fund name, and the volatility signature.

## What was fixed (commits 09f41a9, 63cbb7d)

The traded ticker in `etf_registry.py` is now `EXH4.DE`. The fix was not
one line: three surfaces derived the traded symbol as `f"{key}.DE"` rather than
reading `yfinance_trading_proxy` — `mark_to_market_live._resolve_yf_symbol`,
`export_holdings_prices.resolve_book_symbol`, and the exporter's static OHLC
ticker list. A registry-only change would have corrected the backtest and left
the live book pricing `EXH3.DE`, a new divergence on a surface no test
compared. All three now read the registry.

The dict **key** remains `EXH3`. It identifies the panels on disk
(`constituents_exh3.json`, `breadth_exh3.json`) and in every filed record;
renaming it would rewrite history for no correctness gain. `filename` and
`csv_url_template` also keep `EXH3_holdings`, because the constituent fetch was
always correct and the endpoint accepts that parameter for product 251948.

Two guards added:

- `tests/test_europe_symbol_contract.py` — every surface asserted against the
  registry, in the shape of the existing `test_weights_contract.py`. A future
  member whose Xetra ticker differs from its key now fails here.
- `scripts/check_pair_integrity.py` — correlates each breadth-signalled
  member's priced series against a basket of its own constituents, floor 0.70.
  The 2026-08-03 sweep of all 19 sleeve A and D pairs passes, worst 0.777
  (`IDP6`→`IJR`, an equal-weight small-cap basket against a cap-weighted
  index), best 0.991. **No other member is mispaired** — evidenced, not
  assumed. That range also bounds the floor: it clears the 0.244 defect widely
  but cannot rise far above 0.75 without a false positive on small caps. Not
  yet wired into a workflow.

## Re-run result, and how to read it

The breadth panel was untouched, so this is a clean natural experiment: the
signal is identical and only the return series changed. Allocations came out
bit-identical — same weeks held per member (276/198/184/238/283), same latest
weights, same 391 flips.

Headline, K=3 weekly Friday, fixed and not re-selected:

| Metric | Mismatched | Corrected | Δ |
|---|---:|---:|---:|
| Sharpe | 0.757 | 0.879 | +0.122 |
| CAGR | 12.88% | 16.21% | +3.33pp |
| Total return | +159.7% | +226.9% | +67pp |
| Max drawdown | 33.99% | 35.39% | **+1.40pp worse** |
| Annual turnover | 9.33 | 9.32 | unchanged |
| Walk-forward Sharpe | 0.999 | 1.054 | +0.055 |

### Caveat on the comparison, and a partial-bar finding

The two runs do not end on the same session. The mismatched baseline was
computed 2026-08-01 and ends on 2026-07-31, a completed Friday. The corrected
runs were computed on Monday 2026-08-03 and their equity tails extend to
2026-08-03 itself, because the engine downloads prices to the present and does
not cap at the last completed session.

That surfaced a pre-existing reproducibility problem: two runs of the same
script on the same day gave total return +225.0% and +226.8%, a 1.8pp spread,
because the 2026-08-03 Xetra bar was still forming between them. After the
Xetra close the figures settled — consecutive runs now agree to +0.0002 Sharpe
and +0.001 on total return, which is residual vendor last-bar jitter.

This bounds the mismatch in the table above. One extra session moves Sharpe by
roughly 0.004 (0.8753 mid-session against 0.8791 settled), which is two orders
of magnitude below the +0.122 effect being reported, so the comparison holds
comfortably. The allocation series is unaffected either way: the last rebalance
date is 2026-07-31 in both runs, so no partial bar ever reached the signal —
only the mark-to-market tail.

The underlying fix is the `last_completed_session` cap already on the deferred
list (project memory `rebalance-cadence-deferred`), which is a multi-engine
change needing sign-off and is deliberately not made here. Until it lands,
sleeve D figures quoted from a same-day run are reproducible only to about
±2pp of total return, and anything published should be run after the venue
close.

**This is attribution, not validation.** The pairing fix is correct on
mechanism grounds whatever the numbers did, and most of the improvement is a
window artefact: European industrials beat staples across 2018–2026,
particularly through the 2023–2026 defence and capex run, and industrials carry
24.9% annualised volatility against food and beverage's 16.9%. Swapping a
low-volatility laggard for a high-volatility leader raises return and Sharpe
and widens drawdown, which is exactly the pattern above. In a window where
staples led, the corrected configuration would have looked worse.

Nothing in this table should be cited as evidence that the fix improves the
strategy, and had it come out worse the fix would still be correct. The live
question in that case would have been whether the Industrials member belongs at
all, not whether to keep a mismatched pair because the mismatch scored better.

## Superseded figures and decisions

**Numeric, superseded:**

- `reviews/2026-07-03_ws3_heavy-gate.docx` — the sleeve D cost-stress row
  (+0.754 at 1x, +0.670 at 2x, +0.586 at 3x, break-even ~1.75x, EW bench
  +0.70 / −37%) and every blend-level row that embeds sleeve D, including the
  ungated blend +1.153. The **keep-Phase-29-unchanged verdict is not
  disturbed** — it was a decision about ~217 configurations, and sleeve D
  improved rather than degraded — but the D and blend magnitudes are stale.
- `README.md` blend stats (Sharpe +1.30, CAGR +15.5%, max DD −16.2%) and the
  "D is ~+0.85" walk-forward figure. Corrected in place with a pointer here.
- `reviews/2026-08-02_diagnostic_equity-curve-crossover.html` and
  `results/ec_crossover_diag.json` (risk-overlay-lab) — **sleeve D was the sole
  surviving cell** of that diagnostic (+6.70pp full and ex-rescue, both halves
  positive, live 1.049 vs placebo 0.809). That cell now rests on the mismatched
  pair as well as on pre-Phase-30 breadth. It already carried a Phase 30
  revalidation caveat; this adds a second, independent reason. The diagnostic
  adopted nothing, so no deployed decision follows from it.

**Decision-level, worth re-examining:**

- `reviews/2026-07-02_ws2_universe.docx` — the "Sleeve D universe (5 sectors)
  KEEP" verdict was reached on a universe in which one of the five members was
  a different sector from the one recorded. The overlap and correlation work
  behind that verdict used the food and beverage return series for the
  industrials slot, so the sleeve's internal diversification was measured on
  the wrong pair. The KEEP conclusion may well survive — industrials is less
  correlated with banks, energy, technology and utilities than staples is, so
  the corrected universe is plausibly better diversified, not worse — but it
  has not been retested.

**Not affected:**

- `reviews/2026-08-01_phase30_residual-constituents.md` — concerns constituent
  price coverage. Its `EXH3` row (210 identifiers) describes the industrials
  panel, which was always correct. A clarifying note has been added.
- `reviews/2026-07-19_ws6_single-name-implementation.docx` — the 0.767 / 0.709
  figures are single-name arm Sharpes, unrelated to sleeve D.
- `reviews/2026-07-04_implementation-audit.docx` — mentions sleeve D only for
  the uncapped FX forward-fill (D4), since fixed. That audit examined signal
  paths, execution timing and parity; it did not verify that a member's priced
  instrument matches its constituent panel, and no audit in this project did
  until now. Worth adding to the standing audit scope.
- Sleeves A, B and C, and the Phase 19 gate and Phase 22 tilt, are untouched.
  The 19-pair sweep confirms sleeve A's proxies are all correctly paired.

## Outstanding

1. The blend, factsheet and dashboard have not been rebuilt, so published
   blend numbers still embed the old sleeve D until the next engine run.
2. `check_pair_integrity.py` is a manual script; wire it into the weekly
   workflow so the guard actually guards.
3. This run's walk-forward selected K=2 in all six segments against the
   deployed K=3, and the grid's best cell is K=2 at month-end (+1.10 against
   the deployed +0.88). The prior run's K sequence is not persisted in
   `europe_rotation.json`, so whether that preference predates this fix cannot
   be established from the artefact. Persist it, then treat K as its own
   out-of-sample question. Deliberately not acted on here.
4. WS2's sleeve D universe verdict to be retested at the next scheduled review.
