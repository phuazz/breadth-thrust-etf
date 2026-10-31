# Strategy C native-window hold: the frame names its native record (independent-review P1)

Prepared 2026-10-30 (Friday, +08:00) for independent closing review. Codex's closing verdict was SAFE TO COMMIT (see "Closing review" at the end), and the cumulative patch was then committed on `fix/native-window-hold-rebased` on top of `451e5008`. Not pushed; main integration and publication remain on hold.

## Starting point

- Working directory: `C:\Users\phuaz\Documents\Codex\2026-10-27\task-4\isolated-native-hold-20261028T222552Z-2669296f\temp\rebase-check`
- Branch `fix/native-window-hold-rebased`, HEAD `451e5008`, confirmed before writing.
- Model: Claude Opus 5.5 (`claude-opus-5-5`) in Claude Code. The reasoning-effort setting is not visible from inside the session, so it is not stated here.
- Uncommitted at the start, all preserved: the second-review and atomic-publication changes (`.gitignore`, `scripts/combined_book_risk.py`, `scripts/export_holdings_prices.py`, `scripts/native_sessions.py`, `scripts/price_source.py`, `scripts/strategy_freshness.py`), `tests/test_native_review_2.py`, `tests/test_sidecar_publication.py` and the two earlier 2026-10-30 handoffs.
- No commit, push, fetch, pull, switch, stash, reset, rebase or merge. No network: every run loaded the `block_network` plugin.

## The finding

**P1: numerically identical prices allowed stale native provenance.** CONFIRMED and RESOLVED.

`content_sha256` binds a record to the frame's native values and rows, and `record_sha256` binds the record's fields to one another. Neither says which acquisition observed the dates.

When A serves the 2026-04-03 Shenzhen close at Thursday's price, and B withdraws it so the forward fill carries Thursday's price onto the same NYSE rows, the two frames have identical native values. A's whole record therefore passes both checks beside B's frame, and B's withdrawn Good Friday ranks READY.

Making the price hash cover the whole parquet would not help: with Bitcoin whole on both sides, the two genuine parquets were byte-identical before the fix.

### Reproduction, through the real paths

**Producer:** `run_thematic_rotation.download_prices`, with an offline vendor (`tests/test_native_record_binding._vendor` over `tests/test_native_us_holiday_load_path._vendor`, constant CNY=X 7.0), run under both `BTE_PRICE_SOURCE=norgate` (stubbed selection) and `yfinance`.

**Restart:**
- the real `download_prices` cache-hit path;
- `compute_signal`;
- `live_targets._rank`, with production `top_k_equal_weight(HEADLINE_K)`, `sleeve_gate_state` and `native_inputs`;
- `strategy_freshness.native_cache_reach_or_carried` plus `classify` on the pair on disk.

**The three ways the stale record arose:**
1. **Failed publication.**
   - A runs with Bitcoin's final bar unserved.
   - B restores Bitcoin and withdraws Good Friday; it writes its parquet, then an injected `os.fsync` failure inside `price_source.publish_text` stops its sidecar.
   - A's whole sidecar survives beside B's frame.
2. **Two complete overlapping writers.**
   - The outer writer replaces the parquet; the inner writer runs `download_prices` from start to finish; the outer writer then publishes its sidecar.
   - Tested with both writers as the outer one, and with A's Bitcoin short or whole. With Bitcoin whole, the two parquets were byte-identical before the fix.
3. **The cache-hit reads changing generation.**
   - B's genuine pair is on disk.
   - Between the cache-hit path's record check (`run_thematic_rotation.py` line 733 before the fix) and its adoption of the record (line 745), A's whole sidecar is published.

Logs (isolation folder):
- `2026-10-30_identity-tests-on-pre-fix-tree.log` (before);
- `2026-10-30_identity-tests-after-fix.log` (after).

| Case | Before (this tree before the fix) | After |
|---|---|---|
| Control: B frame, B record | HOLD, `{}`, behind | unchanged |
| Control: A frame (Bitcoin whole), A record | READY, current | unchanged |
| 1. Failed publication: freshness on the pair on disk | **current** | unknown |
| 1. Failed publication: restart | reused; **READY, `{"SHY": 1.0}`, current** | refreshed; HOLD, `{}`, behind (B's own verdict) |
| 2. Overlap, B frame beside A record (both Bitcoin variants) | record accepted; reused; **READY, `{"SHY": 1.0}`, current** | record refused; freshness unknown; refreshed; HOLD, `{}`, behind |
| 2. Overlap, A frame beside B record | record accepted; freshness behind (a wrong record, but a fail-closed verdict) | record refused; freshness unknown; refreshed; HOLD, `{}`, behind |
| 3. Cache-hit generation change | two reads; A's dates adopted; **READY, `{"SHY": 1.0}`, current** | one read; B's record; HOLD, `{}`; freshness unknown (A's sidecar now beside B's frame) |

The before figures match Codex's.

## The fix: the frame names its record

**The binding invariant.** A native record is accepted for a frame only if the frame itself carries that record's `record_sha256`. The name is read from the same parquet read that produced the frame. The earlier conditions also still apply:
- the construction;
- `through`;
- the 400-day window;
- `content_sha256` over the native columns and row index;
- `record_sha256` over the record's own fields.

**1. `scripts/native_sessions.py`**
- `FRAME_ATTR = "native_record_sha256"`.
- `sidecar_record(observed, frame)` now also stamps `frame.attrs[FRAME_ATTR] = rec["record_sha256"]`. pandas writes `df.attrs` into the parquet's own schema metadata (`PANDAS_ATTRS`, pandas 3.0.0 with pyarrow 23.0.1 here) and restores it on `read_parquet`.
- `parse_sidecar` refuses a record unless `frame.attrs.get(FRAME_ATTR) == rec["record_sha256"]`. The check sits after the `record_sha256` self-check, so the name refers to a record that is whole.
- The module docstring gains a "THE FRAME NAMES ITS RECORD" paragraph.

**2. `scripts/run_thematic_rotation.py`**
- **Writer:** the native record is built before `df.to_parquet(PRICE_CACHE)`, so the parquet carries the name. With no record, the attribute is removed before the write, so a stale name inherited by `df` cannot be persisted. The sidecar carries the same record object.
- **Cache hit:** the record is read once (`cached_native := _cached_native_observations(cached)`). The record checked is the record adopted; the second sidecar read is gone.
- **The refused-fetch fallback** already read once. It now also requires the name, through `parse_sidecar`.

Not changed:
- `content_digest` and the native-column-plus-index binding;
- the construction check;
- `record_sha256`;
- atomic publication (`publish_text`);
- `repair_price_gaps.py`;
- signal construction, ranking, weights, gates, rebalance rules and other sleeves;
- the seven acknowledged decisions, the SSE/XSHG caution and the risk snapshot.

### How the name survives each path

| Path | Effect on the name | Evidence |
|---|---|---|
| Parquet round trip | kept (`PANDAS_ATTRS` metadata) | `test_the_parquet_names_the_record_beside_it` |
| Cache-hit transformations (`mask_uncompleted_crypto` copy, column slice) | kept; the name is checked on the frame read from disk | cache-hit tests and the controls |
| `strategy_freshness` single-frame read | kept: one `read_parquet` supplies both the frame and the name | freshness assertions in every test |
| `repair_price_gaps --apply`, US-only (`read_parquet`, then `.loc` fill, `sort_index`, `to_parquet`) | kept; `content_sha256` unchanged | `test_a_us_only_repair_keeps_the_native_record`, through the real `repair_cache(apply=True)` |
| A native value or row changed on disk | name kept, `content_sha256` fails: refused | `test_a_native_change_on_disk_still_voids_the_record` |
| A legacy parquet, or a writer that drops attrs | no name: refused (refresh, unknown, HOLD) | `test_a_frame_without_its_records_name_has_no_record` |

## Changed files

Cumulative and uncommitted. "This round" marks the files touched for this finding.

| File | Status | This round |
|---|---|---|
| `scripts/native_sessions.py` | modified | `FRAME_ATTR`, stamping in `sidecar_record`, the name check in `parse_sidecar`, docstring |
| `scripts/run_thematic_rotation.py` | modified | record before parquet; single cache-hit read (first change to this file on the branch's working tree) |
| `tests/test_native_record_identity.py` | new, 28 tests | yes |
| `tests/test_native_session_hold.py` | modified | source contract updated for the single read, and strengthened: the hit branch must adopt `cached_native` and must not read again |
| `tests/test_strategy_freshness_native.py`, `tests/test_native_signal_window.py`, `tests/test_china_market_holidays.py`, `tests/test_strategy_freshness.py`, `tests/test_engine_tail_heal.py` | modified | fixture order only: build the record, then write the parquet, as production now does |
| `tests/test_native_review_2.py` | new (earlier round) | one fixture builds the record before writing the parquet |
| `handoffs/2026-10-30_native-record-frame-identity.md` | new | this file |
| `scripts/price_source.py`, `scripts/strategy_freshness.py`, `scripts/export_holdings_prices.py`, `scripts/combined_book_risk.py`, `.gitignore`, `tests/test_sidecar_publication.py`, the two earlier handoffs | modified or new in earlier rounds | unchanged here |

The fixture changes relax no assertion. Those tests wrote the parquet first and the record second, which production no longer does, and the new check refuses that order, as intended.

## Regressions: `tests/test_native_record_identity.py` (28 tests; each case runs under norgate and yfinance)

- **Controls:** the B pair HOLDs, behind; the A pair is READY, current; both are reused; the two acquisitions' native digests are equal.
- **Failed publication** (the injected fsync failure): A's sidecar survives whole and `content_sha256` matches B's frame. The freshness reader then gives unknown, and the restart refreshes to HOLD, `{}`, behind.
- **Two complete overlapping writers:** both orders, each with the parquets differing or identical. The record is refused, freshness is unknown, and the restart refreshes to HOLD, `{}`, behind.
- **The cache-hit reads changing generation:**
  - one read;
  - B's record adopted;
  - HOLD, `{}`;
  - freshness unknown.
- **A missing or mismatched name** (none, a wrong hash, a non-string): freshness unknown; the restart refreshes and rebuilds the genuine pair, READY, current.
- **The parquet names the sidecar's record**, and the pair is accepted.
- **A US-only repair** through the real `repair_price_gaps.repair_cache(apply=True)`, with the vendor stubbed:
  - the value is spliced;
  - the name is unchanged;
  - freshness is current;
  - the restart reuses the pair, READY, current.
- **A native value changed or a row dropped on disk**, with the name kept: refused, refreshed.
- **The original torn-write P1** stays closed: `tests/test_sidecar_publication.py` passes 25 of 25 on this tree.

## Validation

Interpreter: `C:\Users\phuaz\AppData\Local\Python\pythoncore-3.14-64\python.exe`.
Environment: `PYTHONNOUSERSITE=1`, `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONPATH=<isolation>\regress-v7\plugin`.
Flags: `-p no:cacheprovider -p block_network --basetemp=<isolation>\temp\bt-identity-*`.

**New regressions on the pre-fix tree: 26 failed, 2 passed.**
- 14 failures reproduce the defect:
  - failed publication: freshness current on the stale pair (2);
  - the overlap: the stale record accepted (8);
  - the cache-hit generation change: A's dates adopted (2);
  - a legacy parquet with no name: the record accepted, freshness current (2).
- 12 failures are the missing name `FRAME_ATTR` only:
  - the wrong-name cases (4);
  - the name check (2);
  - the US repair (2);
  - native change (4).
- The 2 passes are the controls.

**After the fix:** `test_native_record_identity.py` plus `test_sidecar_publication.py`: 53 passed.

**Affected suites: 2,030 passed, 1 skipped, exit 0, 348.54 s.** That is the previous 1,937, plus the 28 new tests, plus 65 tests in the four newly listed files.
- 57 files: the 52 of the previous round, plus the new file and four test files that import `repair_price_gaps`, `run_thematic_rotation` or `live_targets` and were not listed before (`test_completed_vendor_prices.py`, `test_readiness_regressions.py`, `test_repair_price_gaps.py`, `test_repair_price_gaps_calendar.py`).
- List: `2026-10-30_identity-affected-files.txt`. Log: `2026-10-30_identity-affected-suites.log`.
- The first run, before the fixture updates, gave 17 failed, 2,013 passed and 1 skipped. All 17 were the fixture-order and source-contract cases above.

**Full suite: 4,326 passed, 28 skipped, 0 failed, exit 0.** That is the previous 4,298 plus the 28 new tests; the skip count is unchanged.
- Log: `2026-10-30_identity-full-suite.log`.
- Wall time was 11,826 s, against 888 s last round. It ran straight after the affected suites, roughly 17:10 to 20:28 +08:00 (derived from the logged durations, not stamped). No test failed or timed out. The slowdown is not explained: the likely cause is the machine sleeping or contention, but that was not measured.

## Protected state (checked 2026-10-30 17:07 and again at 20:30 +08:00, Friday; identical both times)

**`C:\dev\breadth-thrust-etf-sched`**
- main `49fe3261`, 16 modified, 4 stashes;
- the 16 JSONs still equal the verified backup (`compare16`: "all work==backup: True").

**`C:\dev\breadth-thrust-etf-wt-native-window`**
- `fix/native-window-hold` `4308a672`, three modified data JSONs;
- 110 ignored files present;
- SHA-256 prefixes unchanged:

| File | SHA-256 prefix |
|---|---|
| `live_targets.json` | `c67c61e4f3452846` |
| `strategy_freshness.json` | `98ecba0782e80880` |
| `thematic_rotation.json` | `7e216b92a6fef06f` |
| parquet | `e621fb97ab45f079` |
| sidecar | `85d99d007aecbcc1` |

Nothing was written to either protected checkout. One hash-reading command changed the shell's working directory into the native-window worktree, and it only read files. Test artefacts are under `<isolation>\temp\bt-identity-*` only.

## Residual limitations

1. **The name relies on pandas `attrs`.** The API is marked experimental. A tool that writes the cache without going through pandas `attrs`, or a future pandas change that stops round-tripping them, leaves an unnamed parquet. That is fail-closed (refresh, unknown, HOLD), but it would refresh on every run. `test_the_parquet_names_the_record_beside_it` would catch the round-trip change.
2. **`repair_price_gaps` is unchanged.** It keeps the name only because its read, fill, sort and write sequence preserves `attrs`; this is pinned by the real-path test. A future edit that rebuilds the frame would drop the name, and the result is fail-closed.
3. **Two acquisitions with identical records are interchangeable.** If the observation dates also match, the record is true for either frame, and accepting it is correct.
4. **The parquet and the sidecar are still two files**, with no lock spanning both. Every mismatched pair tested is now refused, so no tested order reaches READY.
5. **The freshness reader reads the disk independently of the run.** In the cache-hit generation case the rank used the record it checked (HOLD), while freshness reported unknown. That disagreement is fail-closed.
6. **One-off refresh on deployment.** No existing cache carries the name, so the first run after deployment refreshes. That is the third forced one-off refresh on this branch; the earlier two came from the digest change and `record_sha256`. If the preserved native-window worktree cache were ever run through this code, it would be refused and refreshed (it is evidence, not to be run).
7. **The previous round's limitation 8 was wrong.** It called the second cache-hit read fail-closed; it could adopt another acquisition's record that matched on content. It is superseded by the single read.
8. **`sidecar_record` mutates the frame it is given.** The frame returned by `download_prices` carries the attribute. No other reader of the native record exists (`parse_sidecar` has two callers, `run_thematic_rotation` and `strategy_freshness`).
9. **Operational follow-ups, unchanged:**
   - rebuild the saved combined-book risk snapshot after its calculator change;
   - the SSE/XSHG projection caution for sibling integration;
   - steps 3 and 4 remain on hold.

## Artefacts (isolation folder)

- `2026-10-30_identity-tests-on-pre-fix-tree.log` and `2026-10-30_identity-tests-after-fix.log`
- `2026-10-30_identity-affected-files.txt` and `2026-10-30_identity-affected-suites.log`
- `2026-10-30_identity-full-suite.log`

## Next review prompt

```
Review the uncommitted changes on `fix/native-window-hold-rebased` (HEAD `451e5008`) in
`C:\Users\phuaz\Documents\Codex\2026-10-27\task-4\isolated-native-hold-20261028T222552Z-2669296f\temp\rebase-check`
as an independent, adversarial closing reviewer. This round closes your P1 "numerically identical
prices allow stale native provenance". The parquet now names its native record:
native_sessions.sidecar_record stamps frame.attrs["native_record_sha256"] with the record's
record_sha256 (pandas persists attrs in the parquet metadata), run_thematic_rotation builds the
record before writing the parquet, parse_sidecar refuses a record the frame does not name, and the
cache-hit path reads the record once. Context: handoffs/2026-10-30_native-record-frame-identity.md.
New tests: tests/test_native_record_identity.py. Five tracked test files changed fixture order only
(record before parquet: test_strategy_freshness_native, test_native_signal_window,
test_china_market_holidays, test_strategy_freshness, test_engine_tail_heal), as did one fixture in
the untracked tests/test_native_review_2.py; tests/test_native_session_hold.py's source contract
was updated and strengthened.

Read-only: do not edit, commit, stash, reset, rebase, switch, merge, fetch, pull or push. Do not
switch or write `C:\dev\breadth-thrust-etf-sched` (16 modified JSONs) or
`C:\dev\breadth-thrust-etf-wt-native-window` (three modified data JSONs and ignored caches). Run
nothing that reaches the network.

First re-run your own reproductions (the fsync-failed publication, the two complete overlapping
writers in both orders, and the cache-hit reads changing generation) through the real producer and
restart reader, and say whether each is closed. Then try to break the fix:
- Any path where a whole record from another acquisition is still accepted beside a frame,
  including frames whose parquet bytes are identical, the refused-fetch fallback, and the
  freshness reader.
- Any way the name is lost, carried to the wrong frame, or inherited stale: the writer when no
  record exists, restore_recorded_btc_gaps or other frames derived from the cache, column slices,
  parquet round trips, repair_price_gaps --apply, and restart reads. A lost name must fail closed;
  a wrong name must never validate.
- Whether a genuine matching pair is ever refused (other than the one-off legacy refresh), and
  whether a US-only repair keeps the record while a native value or row change still voids it.
- Any remaining fail-open path to READY or current with native provenance that does not belong
  to the frame in hand.
- Confirm nothing changes signal construction, ranking, weights, gates, rebalance rules or other
  sleeves, and that the fixture-order test changes relax no assertion.

Run tests with `C:\Users\phuaz\AppData\Local\Python\pythoncore-3.14-64\python.exe`,
PYTHONNOUSERSITE=1, PYTEST_DISABLE_PLUGIN_AUTOLOAD=1, PYTHONDONTWRITEBYTECODE=1,
PYTHONPATH=C:\Users\phuaz\Documents\Codex\2026-10-27\task-4\isolated-native-hold-20261028T222552Z-2669296f\regress-v7\plugin,
flags `-p no:cacheprovider -p block_network --basetemp=<a new temporary folder>`. Results to verify,
not trust: test_native_record_identity.py 28 passed (26 fail on the pre-fix tree, 12 of them for
the missing FRAME_ATTR name only); test_sidecar_publication.py 25 passed; affected suites (57 files,
listed in C:\Users\phuaz\Documents\Codex\2026-10-27\task-4\isolated-native-hold-20261028T222552Z-2669296f\2026-10-30_identity-affected-files.txt)
2,030 passed, 1 skipped; full suite 4,326 passed, 28 skipped, 0 failed.

The seven acknowledged decisions, the SSE/XSHG projection caution and the risk-snapshot rebuild
are known and are not findings unless you show a live defect. Mark each finding BLOCKING (a wrong
live target, a wrong freshness verdict, a crash, or a fail-open path) or FOLLOW-UP (hardening that
can land after the commit).

Output: findings ranked by severity, each with file:line, the triggering input or state and the
wrong output, marked CONFIRMED (reproduced) or PLAUSIBLE and BLOCKING or FOLLOW-UP; then a one-line
verdict: SAFE TO COMMIT, or NOT SAFE with the blocking finding numbers. No contractions, British
spelling. Modify no file.
```

## Closing review (Codex, independent, 2026-10-30)

**Verdict: SAFE TO COMMIT. No BLOCKING and no FOLLOW-UP findings.**

Everything in this section is Codex's independent review, as relayed by the owner. It is not a fresh Claude test run. Claude ran no tests for this section. Its only checks were these:
- read the final two lines of Codex's full-suite log;
- confirm the branch, HEAD and the 19-file status;
- run `git diff --check`, then the staged check before committing.

**Original reproductions, all confirmed closed by Codex:**
- **Fsync-failed publication:** the stale record is refused, freshness is unknown, and the restart refreshes to HOLD.
- **Two complete overlapping writers, both orders:** the mismatched pairs are refused, and the restart refreshes to HOLD.
- **Cache-hit generation change:** one native-record read; B's observations are kept; HOLD.

**Additional probes, confirmed by Codex:**
- a missing or malformed name fails closed;
- the no-record writer removes an inherited name;
- the Bitcoin restoration (`restore_recorded_btc_gaps`) keeps the fresh frame's attributes;
- copies, column slices and parquet round trips preserve correct matching;
- a US repair to an existing row keeps the native record;
- a native value or row change invalidates it;
- the fixture-order edits relax no assertion, and the cache-hit source contract is stronger;
- signal construction, ranking, weights, gates, rebalance rules and other sleeves are unchanged.

**Verified offline by Codex:**

| Run | Result |
|---|---|
| `test_native_record_identity.py` | 28 passed |
| `test_sidecar_publication.py` | 25 passed |
| Affected suites | 2,030 passed, 1 skipped |
| Full suite | 4,326 passed, 28 skipped, 0 failed |
| In-memory reversal of this round | 26 failed, 2 passed; 12 of the failures solely the missing `FRAME_ATTR` name |

- Codex's full-suite log: `C:\Users\phuaz\AppData\Local\Temp\codex-identity-full-f628.log`. Its final line reads "4326 passed, 28 skipped, 161 warnings in 870.95s".
- That 871 s run indicates the 11,826 s wall time recorded in Validation above came from the environment, not the patch.
- All 19 reviewed files kept their initial hashes to the end of Codex's review.

**Committed scope.** These 19 files, staged explicitly:
- `.gitignore`;
- the six scripts: `combined_book_risk.py`, `export_holdings_prices.py`, `native_sessions.py`, `price_source.py`, `run_thematic_rotation.py`, `strategy_freshness.py`;
- the six modified tracked tests: `test_china_market_holidays.py`, `test_engine_tail_heal.py`, `test_native_session_hold.py`, `test_native_signal_window.py`, `test_strategy_freshness.py`, `test_strategy_freshness_native.py`;
- the three new tests: `test_native_record_identity.py`, `test_native_review_2.py`, `test_sidecar_publication.py`;
- the three 2026-10-30 handoffs.

No data JSON, cache, sidecar or temporary file is included.

**Outstanding operational items** (not part of this commit; each needs separate authorisation):
- the seven acknowledged decisions, which stay as recorded;
- the SSE/XSHG projection caution, for integrating the sibling branch `fix/159801-retraction-freshness`;
- the combined-book risk snapshot, which needs rebuilding after its calculator change;
- a one-off cache refresh on first deployment (residual limitation 6);
- main integration, the scheduled-clone fast-forward and push (step 3) and restoring its 16 JSONs (step 4). All remain on hold.

**Protected paths, not written:**
- `C:\dev\breadth-thrust-etf-sched`: 16 deliberately modified JSONs;
- `C:\dev\breadth-thrust-etf-wt-native-window`: three modified data JSONs and ignored caches, kept as evidence.
