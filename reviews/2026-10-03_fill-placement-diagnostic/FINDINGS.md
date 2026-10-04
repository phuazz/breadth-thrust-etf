# WS23 fill-placement diagnostic: verdict NO-GIVE-BACK

Personal portfolio research. Verdict read on Sunday 2026-10-04 (weekday verified with the Python `datetime` library) on Fable, from the held outputs after each file's sha256 was checked against the step-8 table of `PREREG.md`; the declared cells of `results.json` only were read. Archetype: decision aid. The registration (`PREREG.md`, frozen 2026-10-04, content hashes in its header) governs every reading below.

## Verdict and consequence

**NO-GIVE-BACK.** Across the 7,768 confirmatory fills the mean three-session move after the fill is +0.08 bp of price against the fill, where the forward placebo (the same fill at the close of a random session 4 to 60 bars later) gives +0.53 bp: an effect of −0.45 bp, 95 per cent cluster-bootstrap interval −6.45 to +5.66 bp, one-sided p 0.546 in the adverse direction, on a p-test powered at 0.883 for the registered 10 bp floor. H-D2 fails as a powered clause. Thinness fires, as a near-zero effect must, and guards a pass only, so it does not apply; the parity guard excluded 0.42 per cent of fills; the weight history reconciled to the factsheet.

Consequence, as fixed at the freeze: the delay question is closed for this book at diagnostic grade. No kickoff is written. The rebalance-close convention stands as WS12, WS13 and WS18 left it. No engine, parameter, published number or operating rule changes, and the discretionary book's cooling-off registration (Portfolio-Command-Centre, adopted 2026-10-05) is untouched.

## Scope and basis

| Field | Basis |
|---|---|
| Fills | The engines' modelled Monday rebalance-close fills from the published weekly weight vectors at the 2026-10-03 refresh (`08cb2980`), overlay legs and overlay-induced rescalings included, BTC-USD excluded (seven-day calendar), 159801.SZ excluded (basis mismatch) |
| Confirmatory set | 7,768 fills from the blend's inception, Wednesday 2018-10-31, to Monday 2026-07-06, the last fill with a full 60-session forward pool; 477 rebalance-date clusters; 58 lines; 3,880 buys, 3,888 sells |
| Disclosure sets | 3,986 pre-blend fills (sleeve B 2007 to 2018, a few A and D of October 2018); 235 post-cutoff fills (2026-07-13 to 2026-09-28) |
| Prices | Unadjusted yfinance daily bars, windows rebased to the fill session's dividend factor; parity against the engines' own panels on every fill's seven-session window at 0.1 per cent, read locally, statistics only |
| Statistic | Post leg: the move from the fill to the t+3 close in the trade's direction, positive against the book. Adverse rank u: the fill's position in the seven-session low-to-high range, 1 the worst price for the side |
| Null | Forward placebo, one offset per rebalance-date cluster per set, 10,000 sets, seed 20261003; cluster bootstrap for intervals; two-sided and independent nulls as disclosures |
| Floor and power | H-D2 floor 10 bp of price, the blend's modelled round trip; MDE 8.8 bp; p-test power 0.883; the point estimate clears 10 bp with probability 0.50 at a true 10 bp and 0.80 at 13.0 bp |
| Engine | The PCC fill-timing engine (sha256 `e762808c…`) with the amendments the build record lists; engine copy `49fdbe11…`; 69 tests, 57 planted mutants each failing the suite |
| Review | Five red-team passes at the spec-freeze gate (one S1, four S2, all fixed before the freeze); amendments 1 to 11 ruled before any outcome existed |

## The verdict cell and its disclosures

| Cell | n | Actual | Placebo | Effect (bp of price unless stated) | 95 per cent interval | p (adverse) |
|---|---:|---:|---:|---:|---|---:|
| H-D2 post leg, forward null (verdict-bearing) | 7,768 | +0.08 bp | +0.53 bp | −0.45 | [−6.45, +5.66] | 0.546 |
| H-D2 against the two-sided null (disclosure) | 7,768 | +0.08 | −0.34 | +0.41 | [−5.64, +6.50] | 0.456 |
| H-D2 against the independent-per-fill null (disclosure) | 7,768 | +0.08 | −0.36 | +0.44 | [−5.74, +6.43] | 0.421 |
| Notional-weighted post leg | 7,768 | +0.74 | +0.47 | +0.26 | [−10.2, +9.9] | 0.478 |
| Uniform-intraday placebo variant | 7,768 | +0.08 | +0.47 | −0.39 | [−6.72, +5.57] | 0.541 |
| Pre-blend fills, forward null (disclosure) | 3,986 | +4.09 | −0.99 | +5.08 | [−1.82, +12.51] | 0.069 |
| Post-cutoff fills, two-sided null (disclosure) | 235 | −13.97 | −2.33 | −11.63 | [−34.95, +10.52] | 0.745 |

The chained same-line null was degenerate (three clusters, spread 0.0004 of the forward null's) and was not computed, as the registration provides.

## Placement and the pre leg (descriptive)

| Cell | n | Actual | Placebo | Effect | 95 per cent interval | p (adverse) |
|---|---:|---:|---:|---:|---|---:|
| H-D1 mean u, forward null | 7,768 | 0.535 | 0.500 | +0.035 | [+0.029, +0.041] | 0.0001 |
| H-D1 against the two-sided null | 7,768 | 0.535 | 0.499 | +0.037 | [+0.031, +0.043] | 0.0001 |
| Pre leg, per cent of price | 7,768 | +0.552 | −0.001 | +0.554 pp | [+0.479, +0.636] | 0.0001 |
| Buys: u | 3,880 | 0.572 | 0.531 | +0.041 | [+0.027, +0.056] | 0.0001 |
| Sells: u | 3,888 | 0.499 | 0.470 | +0.029 | [+0.013, +0.045] | 0.0001 |
| Buys: pre leg (bought after a rise) | 3,880 | +0.731 | +0.185 | +0.546 pp | [+0.405, +0.681] | 0.0001 |
| Sells: pre leg (sold after a fall) | 3,888 | +0.374 | −0.187 | +0.561 pp | [+0.359, +0.765] | 0.0001 |
| Buys: post leg | 3,880 | −22.0 bp | −17.9 bp | −4.1 bp | [−18.6, +9.9] | 0.689 |
| Sells: post leg | 3,888 | +22.1 bp | +18.9 bp | +3.2 bp | [−13.3, +19.6] | 0.365 |

The fills sit a little past the middle of their week and follow a move of about half a per cent in the trade's direction: the signature of a rotation that buys strength and sells weakness, present on both sides and above the reference floor of 0.01 in u, and about a tenth of the size the discretionary book showed. After the fill nothing is given back: buys went on rising 22 bp over three sessions, sells went on rising 22 bp against the book, and the placebo did the same.

## By sleeve, year and kind

| Group | n | u | Pre leg, pp | Post leg effect, bp | Post-leg interval | p (adverse) |
|---|---:|---:|---:|---:|---|---:|
| A, US sectors (proxies) | 2,947 | 0.525 | +0.40 | −4.2 | [−12.3, +3.8] | 0.838 |
| B, asset classes | 2,816 | 0.545 | +0.58 | −1.8 | [−9.1, +6.0] | 0.652 |
| C, thematics | 637 | 0.573 | +1.56 | +28.4 | [+2.0, +57.3] | 0.050 |
| D, Europe sectors (EUR) | 1,332 | 0.522 | +0.37 | −2.5 | [−14.2, +8.8] | 0.671 |
| Gate leg, SHY | 20 | 0.421 | −0.03 | −5.5 | [−22.4, +8.5] | 0.903 |
| Tilt leg, EEM | 16 | 0.540 | +0.23 | −60.6 | [−152.1, +32.4] | 0.891 |
| Overlay-induced rescalings | 385 | 0.623 | +2.31 | +2.0 | [−56.5, +66.0] | 0.498 |
| Overlay legs, EEM and SHY | 31 | 0.429 | −0.38 | −34.1 | [−80.5, +7.6] | 0.958 |

By calendar year the post-leg effect runs from −25 bp (2026, n 528) and −18 bp (2023, n 1,118, interval clear of zero on the favourable side) to +13 bp (2020, p 0.19) and +9 bp (2018, 2021); no year is adverse at p below 0.10. Leave-one-line-out moves the H-D2 effect between −1.3 and +1.7 bp, leave-one-year-out between −2.6 and +2.5 bp; H-D1 stays between 0.033 and 0.037 on every drop.

One cell is worth naming without being read as a result. Sleeve C's post leg, +28 bp against the placebo with p 0.050 on 637 fills, is the only adverse cell near α among some thirty disclosure cells, and its interval reaches down to +2 bp; it carries no consequence under the registration and no multiplicity correction was declared for disclosures. If a thematic-sleeve question is ever opened, that cell is the prior, and it would need its own registration on data not used here.

## Predictions, scored

| Prediction | Stated | Outcome |
|---|---|---|
| P1: buy-side mean u above the placebo by more than 0.05 | 70 per cent | Wrong, narrowly: +0.041 on the registered forward null (+0.050 on the uniform-intraday disclosure variant) |
| P2: H-D2 reads NO-GIVE-BACK | 60 per cent | Right |
| P3: sell-side mean u within 0.03 of the placebo | 55 per cent | Right, narrowly: +0.029 |

Two of three. The direction of every prior inherited from WS12, WS13, WS18 and stock-radar held.

## Read beside the discretionary book

The registration forbids claiming that either result transfers to the other book, and none is claimed. The two filed facts sit side by side: scored by the same engine against a zero-skill placebo, the discretionary book's 2026 fills sat at u 0.639 after a 5.2 per cent three-session move against the owner, with buys giving back 2.4 points against drift; the systematic book's modelled fills sit at u 0.535 after a 0.55 per cent move, with nothing given back. The adverse placement filed on 2026-10-03 is therefore not a property that momentum trading as such produces when scored this way; what the two books share is the direction of the move before the fill, at a tenfold difference in size, and what they do not share is what follows it.

## Limitations

The fills are modelled, not executed, and describe the engines' convention. Sleeve A is scored on its US proxies, not the London lines. The weight history is the current universe's, survivorship-limited, and carries the restatements the record names. The parity guard checked closes on the actual windows only. Consecutive weekly fills on one line share two bars of their windows, treated as independent by the date blocks. Cells read against a two-sided null carry the selection-window bias the forward null removed from the verdict, the post-cutoff cell most. The run's full outputs (`results.json`, 8.0 MB) are held in the ignored folder under their recorded hashes; the declared cells are committed as `engine/results/results_declared.json` with the three charts.

## Sign-off

Prepared by the Claude Code research session (Fable 5.1) under direction of Zhenghao Phua; verdict read 2026-10-04. Reviewed and approved by: pending. Next review: none scheduled; the question is closed at diagnostic grade.
