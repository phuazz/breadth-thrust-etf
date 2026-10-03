# Run brief: fill-placement diagnostic (breadth-thrust-etf)

Paste into a NEW Claude Code session started in `C:\dev\breadth-thrust-etf`, on Opus (fast mode is fine). This brief covers the build, the freeze and the single run. It stops before the verdict is read: the read and the filing happen on Fable in a separate session, from the declared cells of `results/results.json` only.

```
Context: Personal. Repo C:\dev\breadth-thrust-etf. Tier: Opus (mechanical build and one run,
reviewable). Read README.md and CLAUDE.md here first, then
reviews/2026-10-03_fill-placement-diagnostic/PREREG.md WHOLE: it is the registration, signed off
2026-10-03, and nothing in it changes after the freeze. Start-of-session: git status first. This
tree is shared with other sessions; stage only the files you create, never `git add -A`, and do
not pull, rebase or push without checking the owner has closed the other session.

Done = (1) the adapter, the amended engine copy, the tests and the frozen inputs committed;
(2) the outcome-blind coverage record written and its numbers (fills, clusters, null sd, the
H-D2 floor δ₂ and the power at it) appended to the PREREG header and decision section; (3) the
vault red-team run at the spec-freeze gate with S1 and S2 findings fixed; (4) the freeze commit,
the ledger kickoff row and the register kickoff entry filed with all four guards green; (5) the
run executed ONCE, results/results.json and charts written, and this session STOPPED without
reading any result into prose. The verdict read, the memo section, the ledger row, the register
records and the docx are a separate Fable session.

Build, in this order, each step its own commit:

1. Engine. Copy C:\dev\Portfolio-Command-Centre\reviews\2026-10-03_fill-timing\fill_timing.py
   and prereg_spec.json into reviews/2026-10-03_fill-placement-diagnostic/engine/. Record the
   PCC engine's sha256 (e762808c…) in the PREREG. Amend the copy ONLY as the PREREG allows:
   (a) placebo price = the placebo session's close, with the uniform-intraday rule kept as a
   disclosure cell; (b) unit FX (every rate 1.0; sizes are |Δw| × NAV in NAV units); (c) the P&L
   share, commission comparison and sell-regret cells disabled (not declared here); (d) a
   --fills argument naming the fills file. Change nothing else: window, offsets, blocked null,
   seeds, bar-defect rules, alignment rule, dividend rebasing, stop conditions and the parity
   check stay as they are. Run the PCC engine's own compile and a planted-fills fixture whose u
   the copy must reproduce.

2. Fills. Write scripts/ws_fill_placement_adapter.py. Locate the engines' published weekly
   target-weight vectors and NAV for sleeves A, B, C and D and the blend overlays (the series the
   factsheet's weight tables read; confirm by reproducing one month of the factsheet's weight
   table from them, to 4 dp, and record the reproduction). A fill is every line whose target
   weight changes on a rebalance date: side B if the weight rises, S if it falls, size |Δw| ×
   NAV, price the line's unadjusted close on that session, date the engine's own fill date.
   Price sleeve A on its registry trading proxies (etf_registry.py), B and C on their own US
   tickers, D on the Xetra lines. Exclude BTC-USD and count it. Write fills in the engine's
   trades.json row shape and a minimal book.json meta (ticker, yf, exchange code, ccy) so the
   engine's time-zone mapping and proxy-feed rule behave as in the PCC study.

3. Bars. Fetch unadjusted OHLC and adjusted close for every priced line from yfinance, from
   120 sessions before the earliest fill to the latest session, once, into an ignored
   data_local/ folder; write results/bars_used.json in the engine's extract shape with a
   provenance header (source, fetched_at, sha256). Parity guard: on every fill date the fetched
   unadjusted close times its adjustment factor must agree with the engine's price panel to
   0.1 per cent; exclude and count any line that fails; stop if more than a tenth of fills are
   excluded. No Norgate, no constituent data: ETF bars only.

4. Tests. tests/test_ws_fill_placement.py: the guard-2 fixture (a shifted fill date changes u,
   the correct date does not, failing first against the wrong date); the planted-fills fixture;
   the adapter's weight-to-fill derivation on a toy weight history (a rise is B, a fall is S, an
   unchanged weight is no fill, BTC-USD excluded); the parity guard failing on a planted 1 per
   cent mismatch. Run pytest on the new file only (the full suite is long and shared).

5. Coverage, outcome-blind. Run the engine copy's coverage mode: fills, clusters (fills on one
   rebalance date are one cluster; same-line overlapping windows join it), null sd of the mean
   post leg and of the mean u, the MDE at 0.80 power, δ₂ = that MDE rounded up to 0.01 of price,
   power at δ₂. Append these to the PREREG. Do NOT run the run mode. Do NOT compute u, the pre
   leg or the post leg on any actual fill.

6. Red-team. Spawn the vault red-team agent at the spec-freeze gate with the PREREG, the engine
   copy, the adapter, the tests and coverage.json in scope, the same prohibitions as step 5
   (no run mode, no actual-fill statistic), and the PCC study's review as the catches checklist.
   Fix every S1 and S2 before the freeze; regenerate coverage; record dispositions in the PREREG.

7. Freeze. One commit with the PREREG (header stating the freeze hashes), engine copy, adapter,
   tests, bars_used.json, fills.json, coverage.json. Then in C:\dev: a kickoff row in
   STUDIES_LEDGER.md after the last breadth-thrust-etf row and a non_hypothesis_rows kickoff
   entry in studies/hypotheses.yaml; run ledger_parse.py --check, check_register.py,
   build_register_index.py and --check; commit and push the vault. Quote the freeze commit
   hash only after it is pushed; if the tree cannot be pushed because another session is
   active, record the content hashes and say so.

8. Run. Execute the run mode once from the frozen inputs (it refuses on any hash mismatch).
   Write results/results.json and the charts (reuse the PCC charts.py pattern: the verdict
   chart on the post leg, the strip of fills by sleeve, the pre and post legs by side, every
   figure carrying its as-at line). Commit. Then STOP and report only: the run completed, the
   file paths, and the hashes. Do not state the verdict, the p-values or the effect sizes.

Hold to: no change to any engine, parameter, published number or operating rule; nothing
licensed read; every date verified with a date library; no contractions, British spelling;
flag any step where the registration is ambiguous rather than resolving it silently.
```

After step 8, start a Fable session in the same folder with:

```
Context: Personal. Repo C:\dev\breadth-thrust-etf. Tier: Fable (verdict read). Read
reviews/2026-10-03_fill-placement-diagnostic/PREREG.md whole, then results/coverage.json and
the declared cells of results/results.json only. Read the verdict exactly as the PREREG's
mapping says (H-D2 alone verdict-bearing; H-D1 descriptive; demotion and thinness rules as
written), score the three predictions, and file: the memo section in RESEARCH_MEMO.md, the
technical record via the research-review skill, the ledger row and the register records
(one per clause), index regenerated, all four guards green. Apply the consequences exactly as
the PREREG fixes them and nothing more.
```
