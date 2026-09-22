# Handoff: fifth-round finding closed

**Working directory:** `C:\dev\breadth-thrust-etf\.claude\worktrees\reverent-dewdney-451de4`
**Branch:** `claude/reverent-dewdney-451de4`
**Reviewed commit:** `e1e6d445`
**Model / effort:** Claude Opus 5, high effort
**Prepared:** 2026-09-22 (Tuesday), SGT
**Status:** COMPLETE and pushed. Not merged, not published. No live refresh run.

Sixth and shortest handoff on this branch — one finding, one narrow patch.
Prior handoffs: `…refused-roster-failure-class.md` (design),
`…refusal-review-findings-closed.md` (r1), `…refusal-second-round-closed.md`
(r2), `…refusal-third-round-closed.md` (r3),
`…refusal-fourth-round-closed.md` (r4).

---

## 1. Commit and changed files

| Commit | Subject |
| --- | --- |
| `ad30011b` | **Validate holdings cell types, so a bad cell cannot block recovery** |

```
scripts/fetch_constituents.py |  37 ++  5-      (32 lines of source change)
tests/test_roster_refusal.py  | 120 ++
```

No file under `data/`, `docs/` or `build/` touched.

## 2. The finding and the correction

**P2 — malformed cell values still prevented recovery.**

Round four validated datapoint objects, column lists and column lengths — the
*shape* of a payload — but not the cells inside it. A single numeric value in
a correctly sized column reached `.strip()` and raised `AttributeError`, a
class no caller handles.

For a **retained** payload that meant recovery never reached the endpoint: the
date was stuck on a Python error rather than on anything about the roster, on
every retry, with a valid issuer correction available. Reproduced separately
for `assetClass`, `exchange` and `countryOfRisk` before any code changed —
two attempts each, two `AttributeError`s, zero endpoint calls.

**Correction.** A `text_cell` helper validates the cells that string
operations are about to be applied to, at the point of use. Null becomes `""`
— the value every caller already treats as absent — and anything that is
neither text nor null raises `PayloadContractError`. That gives both routes a
policy they already have: retained-data corruption enters damaged-evidence
recovery, and malformed network data follows the existing contract-failure
path. Validated rather than caught, so genuine programming errors still
surface.

`ticker` keeps its existing `str()` normalisation and is deliberately not
routed through the check.

**Optional-field validation was NOT loosened.** The review found no observed
issuer shape justifying it, and the 24 stored responses sampled carried object
datapoints and list columns throughout. That is sampled evidence, not an
exhaustive contract guarantee, so the bound stays where it is.

## 3. Proving tests

| Requirement | Test |
| --- | --- |
| Malformed cells across all three fields | `test_malformed_cell_is_a_contract_error_not_an_attribute_error` (3 fields × 6 bad types) |
| Retained cells do not escape as `AttributeError` | `test_malformed_retained_cell_does_not_escape_as_attribute_error` |
| Vendor absence preserves refusal across repeated runs | same test — two runs, refusal still held |
| A valid correction is fetched, resolves, clears state | `test_malformed_retained_cell_resolves_on_a_valid_correction` |
| Malformed **network** cells keep contract-failure policy | `test_malformed_fresh_network_cell_is_a_contract_failure` |
| Legitimate null/empty cells unchanged | `test_null_and_empty_cells_remain_legitimate` |
| Valid payload behaviour unchanged | `test_valid_payload_behaviour_is_unchanged` |
| Ticker normalisation preserved | `test_numeric_ticker_normalisation_is_preserved` |

## 4. Validation

| Check | Result |
| --- | --- |
| `pytest tests/test_roster_refusal.py` | **156 passed** (was 124) |
| `pytest tests/` (full suite) | **3,080 passed, 4 skipped, 0 failed** (274s) |
| `check_refresh_guard.py` | 0 FAIL, 4 WARN — `OK G8 roster refusals: 24 panels carry no refused roster` |
| Conflict-marker check | OK, 857 tracked files |
| `sync_claude_memory.py --gate` | no strict term in any tracked file |
| `data/`, `docs/`, `build/` diff | empty |
| Pre-fix verification | **27 of 32** new tests fail against `e1e6d445`; the other 5 pin behaviour that already held |

Preserved, and covered by the existing suite: the latency-breach enforcement,
evidence-identity-by-content, report isolation and per-panel gate ordering
from earlier rounds; the 6/6/6/0 recovery sequence; historical issuer
recovery; offline mapping repair; soft vendor gaps; malformed-root rejection.

## 5. Remaining limitations

Unchanged from `…refusal-fourth-round-closed.md` §5, plus one narrowed:

1. Zero recorded refusals is not zero historical refusals.
2. Quarantined files are never pruned.
3. `find_archived_evidence` hashes every archive for a date on each damaged
   pass; unmeasured against a date with many.
4. `os.link` is the exclusive-claim primitive; the fallback has a narrow
   same-content race, untested without hard-link support.
5. Recovery costs one request per unresolved date per run.
6. A degraded abort writes no payload, so that run's refusal record exists
   only in the retained log.
7. `unexpected_error` has the widest blast radius and no production history.
8. **NEW, and the reason this patch is narrow:** cell validation now applies
   on the live network route as well as the retained route. A live payload
   whose `exchange` or `countryOfRisk` cells are not text would become a
   contract failure and, on the network path, trip the `EndpointCircuit` for
   that panel. The 24 sampled stored responses show no such shape, but the
   sample is not a contract guarantee. If an issuer ever does vary there, the
   symptom will be a panel-wide outage rather than a dropped row.

## 6. Boundaries observed

No threshold change, no suppression flag, no venue exemptions, no
staged-roster activation, no candidate-policy change, no strategy, membership
or registration change. No live refresh, no historical data repair, no merge,
no publication. No redesign: 32 lines of source, one helper, three call sites.
