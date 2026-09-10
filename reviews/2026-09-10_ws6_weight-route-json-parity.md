# WS6 A3 — the JSON weight route, and the parity that admits it

**Status:** BUILT and PROVEN 2026-09-10. Plumbing only: no bar, floor, window or
construction parameter of `KICKOFF_ws6b-unscreened-replication.md` is touched,
and the adopted set's frozen weights are reproduced byte for byte.

## 1. Why

The A3 weight tables are built from the `Weight (%)` column of the raw iShares
holdings CSV. That endpoint began serving anti-bot HTML for **UCITS and US lines
alike** (probed live 2026-09-09 on IUES and SOXX), which froze every line's
weights in mid-2026. Under the WS6b §6.3 STRICT ruling that reverted every
adopted line every week, so the shadow published weeks in which `I0 == E0`
exactly and measured nothing — while still counting toward bar (b), because a
fired fallback is registered as resolved rather than as a gap.

The deployed membership pipeline had already migrated to the product-data JSON
API, which keeps serving and carries the same quantity as `holdingPercent`. The
weights were therefore on disk the whole time; only a parser was missing.

## 2. What was built

`fetch_ws6_weights.parse_holdings_weights_json` mirrors
`fetch_constituents.parse_holdings_json` row for row — the `asOfDate` echo
check, `Asset Class == "Equity"`, the `-` placeholder skip, overrides, dot to
dash, the `-.` guard, first-occurrence dedup — exactly as the CSV weight parser
mirrors `parse_holdings`. That is what lets the weight keys join the membership
snapshots, and the filter-parity guard now asserts it on both routes.

Route order in `build_line` is **CSV cache → JSON cache → JSON network → CSV
network**, and the order is load-bearing: the cached CSV is the basis every
published weight was built from, so trying it first reproduces those weights and
the JSON route can only ADD a date the CSV route never had. Provenance travels
with the table — `source.route_by_snapshot` names the route per snapshot.

## 3. The parity proof

**The two caches on disk are disjoint.** Every (line, date) is stored as CSV or
as JSON, never both: **0 overlapping pairs** across all eleven lines. So parity
could not be measured on what was already held; the overlap had to be created by
requesting JSON for historical dates that already had a CSV. The API serves
those, and each payload is cached under the deployed pipeline's own name, so the
fetches are not wasted and a re-run is free and offline.

| | |
|---|---|
| Pairs compared | **330** (30 per line × 11 lines, seeded sample) |
| Window | 2018-01-05 … 2026-07-10 |
| Criterion | **exact equality**, not a tolerance |
| Ticker key set identical | **330 / 330** |
| Top-15 renormalised pool set identical | **330 / 330** |
| Max per-name weight difference | **0.0** |
| Max renormalised pool difference | **0.0** |

Report: `data_local/ws6/weight_parity_report.json`. Re-runnable offline with
`python scripts/prove_ws6_weight_parity.py --per-line 30 --offline`, and
extensible with a larger `--per-line`.

### 3.1 The first run FAILED, and that is why the proof exists

The first run reported **9 failures of 330**, every one a single name differing
by exactly 0.01 — one unit in the last CSV decimal. Cause: the CSV publishes to
2 dp and the API to 4–5, and **Python's `round` rounds half to EVEN while the
publisher rounds half UP**. `holdingPercent` 0.435 became 0.43 under `round` and
is 0.44 in the published CSV (IUES 2018-02-02, NFX; eight more across IUHC,
IUUS, IUMS, IUSP). `_round_like_csv` uses `Decimal(repr(v))` with
`ROUND_HALF_UP` — `Decimal(0.435)` would take the binary float 0.434999… and
round down, reintroducing the same defect. Re-run: 330 / 330 exact.

A tolerance would have passed all 330 on the first run and hidden this.

### 3.2 Precision is matched deliberately, not maximised

The JSON route stores at the CSV's 2 dp. That is the conservative choice, not
the accurate one: 4,836 snapshots are already on the 2 dp basis, and a table
that changed precision part-way through would put a step into exactly the
basis-point-scale divergence series WS6b exists to measure. The finer precision
remains in the cached payload.

## 4. Rebuild — a superset for the adopted set

`--force --window-end 2026-09-11`, **4,946 snapshots, zero network calls, zero
failures, zero anomalies.** All eleven lines now reach 2026-09-04.

For the five adopted lines the extension is a strict superset: **0 restated**,
11–17 snapshots added each (SOXX 2026-05-15…09-04; the others the unresolved
holidays plus the walled tail).

## 5. An unguarded look-ahead the proof exposed, on six NON-adopted lines

The rebuild restated **18 snapshots** — 3 dates × 6 non-adopted lines (IUCD,
IUCM, IUHC, IUIS, IUMS, IUSP), on 2020-04-10, 2020-12-25 and 2021-01-01. All
three are US market holidays whose snapshots carry a walkback `actual_date`, and
none has a cached CSV.

These are **corrections, not regressions**. In the old table IUCD's 2020-12-25
and 2021-01-01 weights were byte-identical to each other *and to 2020-12-18*:
the CSV route has **no `asOfDate` echo check**, so for a date the endpoint could
not serve it silently stored a different date's holdings. The cached JSONs echo
their requested walkback dates correctly (20201224, 20201231) and the two dates
now differ. `parse_holdings_json`'s echo check — carried into the weight parser
— is what makes this visible; the CSV route never had it.

**Not adopted, so nothing WS6b consumes is affected**, and WS6 is closed with
verdict KEEP-ETF, so no decision turns on it. Flagged for the owner because it
touches the frozen A3 table and because the same defect class may sit in any
CSV-derived series that lacks an echo check.

## 6. Demonstration

Shadow dry run, week ending 2026-09-04 (t−1 read 2026-09-03), after the repair:

```
SS6.3 STRICT: adopted ['IUES','IUUS','IUCS','SOXX','IUFS'] | withheld: none
  every line: membership 2026-08-28 | weights 2026-08-28
I0 +0.8436%  E0 +0.7166%  gap +12.7bp  turnover 0.3290
  [ok] basket_weights_sum_to_one: all baskets sum to 1.0
  [ok] divergence_within_bar: |gap| 12.7bp vs 66.0bp bar
PUBLISHABLE: True
```

Against **gap +0.0 bp with all five withheld** before it. The 12.7 bp sits
close to the pre-arm dry run №2's +10.6 bp, which is an independent check, and
inside both the registered 66 bp bar and the stricter 43 bp figure.

The published week 2026-09-04 is **not restated** — it is an honest record of a
week that ran on a walled feed, and the log is append-only. The first published
week with current weights is the scheduled fire of Sat 2026-09-12, for the week
ending Fri 2026-09-11.

## 7. A second latent defect, fixed on the way

`run_ws6b_shadow.compute_week` reconstructed each line's basket by excluding
only `SINGLE_NAMED_LINES`. `restricted_to` expresses the other *single-named*
lines as ETFs, but the three broad slices (CSP1, CNDX, IDP6) are held as their
own ETFs in every arm and remained positive-weight columns, so a broad slice's
weight was divided by the line's and counted into its basket. On the week ending
2026-09-04 the baskets summed to 1.077 (IUES), 1.370 (IUCS) and 1.075 (IUFS),
and `basket_weights_sum_to_one` would have **refused WS6b's first real week** —
reading like a data problem rather than a reconstruction bug, with bar (b)'s
clock not starting. Latent only because §6.3 had withheld every line while the
weight route was walled; repairing that route is precisely what would have made
it bite. Found by the WS6c session (`7d4b385`), which had copied the same shape.
The exclusion set is now the whole sector book, pinned by test.

## 8. Open for the owner

1. **Week 1 measured nothing.** The published week ending 2026-09-04 counts
   toward bar (b) (`consecutive_publishable: 1`) while `weeks_fully_reverted: 1`
   records that it never traded as a basket. Whether the 8-week count restarts
   from 2026-09-11 is a call for the owner, not a script.
2. **The 18 restated snapshots** (§5) touch the frozen A3 table on non-adopted
   lines. Kept, because the new values are the guarded ones.
3. **Sample size.** 330 pairs proves the routes agree; a larger `--per-line`
   costs only throttled fetches if more assurance is wanted before T4.

---
*Evidence: `data_local/ws6/weight_parity_report.json`;
`data_local/ws6/weights_backup_2026-09-10_pre-json-route/` (pre-rebuild tables);
`scripts/prove_ws6_weight_parity.py`; `tests/test_ws6_weight_json_route.py`.*
