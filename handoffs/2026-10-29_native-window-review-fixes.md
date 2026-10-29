# Strategy C native-window hold: fixes for the Codex review of 30ac1ed and 99039858

Date: 2026-10-29 (Thursday; weekday checked with Python `datetime`). Branch `fix/native-window-hold` in the worktree `C:\dev\breadth-thrust-etf-wt-native-window`. **All changes below are uncommitted, for review.** No commit, push, merge, network access or production run was made. The scheduled clone was not switched or written.

## Commits under review

| Commit | Subject |
|---|---|
| `49fe3261` | main, the branch base (`Scanner daily build 2026-10-27`) |
| `30ac1ed1` | Strategy C: hold on any unobserved Shenzhen session in the 200-row window |
| `99039858` | Strategy C: convert the Shenzhen line before the NYSE-only reindex (branch HEAD) |
| working tree | this handoff's fixes, uncommitted |

## Findings

Codex reproduced four defects. Each was reproduced again here, offline, against the unchanged `99039858` code, then re-run on the fixed code. The before and after output is in `2026-10-29_repro-before-after.log`. The driver is `repro_driver.py`; both files are in the isolated directory listed at the end.

### 1. Cache and sidecar from different generations: CONFIRMED, RESOLVED

**Reproduced.**
1. The first download had every native bar, but the final BTC bar was missing.
2. The retry restored BTC but lost the Good Friday Shenzhen bar, so the Qingming row was forward-filled from Thursday.
3. `write_cache_source` failed after the parquet was replaced.
4. On restart the cache was reused: sleeve C was READY with `missing_native_sessions: []`, Qingming at 0.9999 × Thursday, and freshness current through 2026-04-17.

This is a false READY.

**Fix.**
- `native_sessions.sidecar_record(observed, frame)` now records a `content_sha256` (`content_digest`). It is the SHA-256 of the frame's row index and of the columns of the recorded native lines, in canonical bytes.
- `parse_sidecar(sidecar, frame, tickers)` refuses a record whose digest does not match the frame in hand.
- The engine's cache-hit path, its refused-write fallback (recovery) and `strategy_freshness.native_reach` (publication) all pass the frame they actually read.
- The order of the two writes no longer matters: a mismatch in either direction is refused.

**After.** The restart refreshes instead of reusing. Freshness is unknown with "no native-observation record … matching this cache". The fallback path gets no record and HOLDs.

**Scope of the binding: a deliberate choice, open to the reviewer.** The digest covers the row index and the native columns, not the whole parquet file. `repair_price_gaps.py --apply` rewrites the parquet without touching the sidecar. A whole-file digest would therefore void the native record after any repair to a US line: the next run would refresh, and freshness would be unknown until then. As built:
- Any change to a native value, a blanked native cell, or an added or removed row voids the record.
- A change to a US column does not.

**Residual.** A later generation whose native column and index are byte-identical to the recorded one is accepted. The values on every row are then exactly what the observed fetch produced, so the signal is identical. Switching to a whole-file digest is a one-function change if preferred.

### 2. Carried native bounds compared as strings: CONFIRMED, RESOLVED

**Reproduced.** An absent cache with a prior report row `data_through="2026-10-28"` and `last_valid` set to `"2026-9-01"` or to `"garbled"` carried sleeve C as current through 2026-10-28.

**Fix.** In `strategy_freshness.native_cache_reach_or_carried`:
- Both `last_valid` and the carried `data_through` are parsed with `_strict_date`: exactly `YYYY-MM-DD` and a real calendar date. `date.fromisoformat` alone is not enough, because on Python 3.11+ it also accepts `20261028` and week dates.
- Anything else, or a record for another venue, gives unknown.

**After.** Both malformed values give unknown. A well-formed bound still carries. The tests cover a year boundary in both directions.

**Not changed (pre-existing on main, outside this branch).**
- The general carried path for sleeve B passes `data_through` to `classify`, which parses it leniently with `pd.Timestamp`. A carried `"20261028"` classifies as current there, and `"2026-9-01"` as behind 40.
- I left it alone because the brief excludes other sleeves.
- Recommend a separate change.

### 3. Holiday validation could excuse a real withdrawal: CONFIRMED, RESOLVED (with a stated limit)

**Reproduced.**
- `PINNED_CLOSURES[2026]` was set to the library's complete 2026 weekday list plus 2026-09-30, with a source. `_validated()` accepted it.
- `library_conflicts` saw `pinned_only: ["2026-09-30"]` but nothing enforced it.
- With the 30 September bar withdrawn, the sleeve went from HOLD (unpinned) to READY (pinned).
- This happened with a free-text source on the old code, and also with a well-formed source record.

**Fix (`china_market_holidays`).**
- **Enforced at use.** `checked_pins(cal, venue)` raises `HolidayPinError` if any pinned year disagrees with the library's own list for that year, in either direction. All pins for the venue are refused until a person resolves the conflict against the notice. `native_sessions.native_schedule` calls it, `provenance` uses that, and the error fails closed: the sleeve is HOLD and freshness is unknown, with the conflicting dates named.
- **Why not at import.** Raising at import would stop `strategy_freshness` for all four sleeves, not only C.
- **The source must be a review record, not free text.** It needs `title`, `issuer`, `issued_on` and `reviewed_on` (strict ISO dates, with the issue date in the pinned year or the year before, and the review not before the issue). It needs a `url` on gov.cn, sse.com.cn or szse.cn, and a named `reviewed_by`.
- **Ceiling.** A year with more than 24 weekday closures is refused. 24 is the library's XSHG peak, in 2002; the range since 2015 is 15–20. Both measured on pandas_market_calendars 5.3.2 and 5.4.0.
- Nothing is pinned for 2027.

**Limit, stated in the module docstring.**
- The checks establish form only. They do not establish that the dates are the notice's.
- For a year the library does not list, which is the only year in which a pin changes anything, authenticity rests on the named reviewer having read the dates against the official notice. The record makes that review auditable; it does not prove it.

### 4. Cache built under the earlier conversion order: CONFIRMED, RESOLVED (it erred towards a false HOLD)

**Reproduced.** A cache written by `download_prices` at `30ac1ed` was reused at `99039858`:
- The record omitted 2026-04-03, and Qingming stayed at 0.9999 × Thursday.
- The sleeve went to HOLD with `missing ["2026-04-03"]`.
- Rebuilding from identical vendor inputs gave READY, with Qingming at 2.4549 × Thursday.

The direction was a false HOLD, not a false READY, but it is still a defect.

**Exposure.** As far as I can find, only the worktree's supervised run 1 ever wrote such a cache; that evidence was saved earlier as `supervised-run-1-pre-fix/`. The scheduled clone's cache was written by main's code and carries no native record.

**Fix.**
- `SIDECAR_CONSTRUCTION = "fx-before-nyse-reindex/2026-10-29"` is written into every record.
- `parse_sidecar` refuses a record whose value is absent or different, so every record written at `30ac1ed` or `99039858` is refused.
- This is separate from the digest in finding 1: a record with a matching digest but an old construction is still refused, and a test pins that.

**After.** The reused 30ac1ed cache is rebuilt and the sleeve is READY, identical to the control.

## Compatibility with `fix/159801-retraction-freshness` (assessed, not merged)

**The branch.**
- One commit, `44a825a4`, cut from `c8d6b927`, which is 194 commits behind main.
- It edits `run_thematic_rotation.py`, `strategy_freshness.py` and five other files.
- Nothing was merged. Its helper was read with `git show` and executed in `replay_159801.py`.

**Codex's claim, confirmed.**
- On the hypothetical 2027 pin, Shenzhen's last served close is 2027-01-28 and 2027-01-29 is pinned shut.
- The branch's `_unserved_native_sessions` reads `get_calendar("SSE")` directly. It calls the 29th unserved, and its mask blanks that NYSE row.
- The sleeve therefore HOLDs on the coverage floor, while the native-window check alone is READY.
- The same helper with one change gives `unserved: []` and READY. The change is that its schedule comes from `native_sessions.native_schedule(venue, start, end)[0]`.
- `native_schedule` applies the pins under both "XSHG" and "SSE", which is tested.

**Required on rebase.**
1. `_unserved_native_sessions` must take its schedule from `native_sessions.native_schedule`, with the venue and dates unchanged and `.schedule` replaced by `[0]`. Both paths then use one effective calendar, and a pin conflict raises in both.
2. Keep this branch's call order in `download_prices`: `_fx_convert_to_usd` before `_reindex_crypto_to_equity_calendar`. The 159801 hunk edits the body of `_fx_convert_to_usd` (the trailing-edge mask), not the call order, so the two compose. Its `native_prices` and `aligned.index` keep their meaning under the reorder.
3. Keep its raw-vendor audit (`price_revisions.record_vendor_observation` on the raw batch, before Norgate). It sits above every step this branch moved.
4. Keep its verdict expiry (`verdict_valid_until` in `strategy_freshness.build`). It does not touch the native functions this branch changed. Expect textual conflicts only from the 194-commit base gap.
5. The trailing-edge mask runs before the native digest is taken, so the digest describes the masked column that is actually cached. NATIVE_OBSERVATIONS still lists only observed dates.

Not verified: that branch's own test suite against a composed tree. That needs an actual rebase, which was out of scope.

## Changed files (uncommitted)

```
 scripts/china_market_holidays.py        | 112 +++++++++++++++++++++++---
 scripts/native_sessions.py              | 135 ++++++++++++++++++++++++------
 scripts/run_thematic_rotation.py        |  19 +++--
 scripts/strategy_freshness.py           |  45 ++++++++---
 tests/test_china_market_holidays.py     |   2 +-
 tests/test_engine_tail_heal.py          |   2 +-
 tests/test_native_session_hold.py       |  25 +++---
 tests/test_native_signal_window.py      |  19 ++---
 tests/test_strategy_freshness.py        |   2 +-
 tests/test_strategy_freshness_native.py |   9 ++-
 tests/test_native_record_binding.py     | new, 51 tests
 handoffs/2026-10-29_native-window-review-fixes.md | new (this file)
```

**Scope.** No change to signal construction, ranking, weights, the rebalance rules or any other sleeve. The existing test edits only change the call signature: `sidecar_record` and `parse_sidecar` now take the frame instead of `through`.

## Validation

**Environment.** All runs used:
- interpreter: `C:\Users\phuaz\AppData\Local\Python\pythoncore-3.14-64\python.exe` (3.14.3; pandas 3.0.0, numpy 2.4.2);
- environment variables: `PYTHONNOUSERSITE=1`, `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONPATH=<ISO>\regress-v7\plugin`, and `MPLCONFIGDIR`, `XDG_CACHE_HOME`, `TMP`, `TEMP` and `YFINANCE_CACHE_DIR` all set to `<ISO>\temp`;
- flags: `-p no:cacheprovider -p block_network --basetemp=<own folder>`.

**Clean location.** All runs were in `<ISO>\temp\fixcheck`, a detached worktree at `99039858` with only the changed `scripts/` and `tests/` files copied in. It has no thematic cache and no modified data JSONs, so the supervised-run caches and JSONs could not influence the results.

**Results.**
- **New adversarial file.** `tests/test_native_record_binding.py`: 51 passed.
  - Against unfixed `99039858` it gave 38 failed and 13 passed. The 13 are either controls (a well-formed bound still carries, a matching pair is still reused, a complete source passes) or malformed inputs that the old string comparison happened to refuse (`""`, `None`, `"2026-02-30"`, a leading space, and three malformed `data_through` values).
  - The generation and carried-bound tests fail on their behaviour assertions.
  - The pin and construction tests fail on API the old code lacks; their behavioural failure on the old code is the reproduction log.
- **Focused suites** (the seven files from the review plus `test_engine_tail_heal.py`): 210 passed.
- **Full suite:** 4,030 passed, 28 skipped, 0 failed, 0 errors (162 warnings) in 9 min 38 s. That is the 3,979 recorded at `99039858` plus the 51 new tests. Log: `<ISO>\fixcheck-full.log`.

## Protected state (checked 2026-10-29 22:56 +08:00)

**Scheduled clone, `C:\dev\breadth-thrust-etf-sched`.**
- Branch `main` at `49fe326182c090ef90e69ce192439a4b4f1c5582`, not switched.
- 16 modified files, and all 16 JSONs still equal the verified backup `recovery\backup-16-json-20261028T090737Z-5d1d48b7`.
- 4 stashes, intact.
- Two read-only `git show` calls were made against it; the worktree list gained `<ISO>\temp\fixcheck` (detached, removable).

**Native-window worktree.**
- Its three modified data JSONs and the thematic cache and sidecar are unchanged since supervised run 2 (mtimes 21:11–21:12):

  | File | SHA-256 prefix |
  |---|---|
  | `live_targets.json` | `c67c61e4f3452846` |
  | `strategy_freshness.json` | `98ecba0782e80880` |
  | `thematic_rotation.json` | `7e216b92a6fef06f` |
  | parquet | `e621fb97ab45f079` |
  | sidecar | `85d99d007aecbcc1` |

- There are 110 ignored data files. That cache was written at `99039858`, so the fixed code will refuse its record and refresh on the next supervised run. This is intended.

## Remaining merge risks

1. **The rebase onto current `origin/main` has not been done.** No fetch was made.
2. **The 159801 branch needs the `native_schedule` change above, or the two paths disagree on every pinned closure.** Until 2027 is pinned they agree, because both read the unpinned library.
3. **Whole-file versus native-column binding** (finding 1) is a judgement call for the reviewer.
4. **The sleeve B carried-bound leniency** (finding 2, adjacent) is pre-existing and unfixed.
5. **A sidecar write failure after the parquet is replaced** still pairs the new parquet with the old sidecar's other fields (`source`, `completed_crypto_through`, `tail_heal`). This is pre-existing, and this branch only binds the native record. Writing to a temporary file and then `os.replace` for both files would narrow it; not done.
6. **The first run after merge** refreshes any cache carrying a 99039858-era record. The production cache has none, so it refreshes anyway, as it would have under 99039858.

## Artefacts

`<ISO>` = `C:\Users\phuaz\Documents\Codex\2026-10-27\task-4\isolated-native-hold-20261028T222552Z-2669296f`

| File in `<ISO>` | Contents |
|---|---|
| `2026-10-29_review-fixes-v8.patch` | `scripts/` and `tests/` diff plus the new test file; 60,541 bytes, SHA-256 `6954e680…6502` |
| `2026-10-29_repro-before-after.log` | the four reproductions before and after, plus the composed replay |
| `repro_driver.py`, `replay_159801.py` | the drivers |
| `fixcheck-full.log` | the full-suite log |

## Next review prompt

```
Review the uncommitted working-tree changes on fix/native-window-hold in
C:\dev\breadth-thrust-etf-wt-native-window (git diff 99039858 plus the
untracked tests/test_native_record_binding.py) as an independent, adversarial
reviewer. Read-only: do not edit, commit, stash, reset, fetch, pull or push,
do not switch or write C:\dev\breadth-thrust-etf-sched (its main carries 16
deliberately modified JSON files), and run nothing that reaches the network.
Ignore the worktree's three modified data JSONs and ignored price caches;
run tests in a clean location (a detached worktree at 99039858 with only the
changed scripts/ and tests/ files copied in).

Context: handoffs/2026-10-29_native-window-review-fixes.md (uncommitted, in
the worktree). The changes answer four reproduced defects: (1) the native
record is bound to the frame by a SHA-256 over the row index and native
columns (native_sessions.content_digest), checked by parse_sidecar on the
cache-hit path, the refused-write fallback and strategy_freshness.native_reach;
(2) carried native bounds are parsed strictly (strategy_freshness._strict_date);
(3) china_market_holidays.checked_pins refuses all pins on any disagreement
with the library, the source must be a review record on an official host,
and a 24-closure ceiling applies; (4) records carry SIDECAR_CONSTRUCTION.

Try to break them:
- Find any reader of the thematic cache's native record that does not go
  through parse_sidecar with the frame it actually uses, or that reads the
  frame and the sidecar at different moments.
- Find a sequence of writes, failures and restarts that pairs a record with
  a frame whose native values or rows it does not describe and is accepted.
  Judge whether binding to the native columns plus index (not the whole
  file) is right, given repair_price_gaps.py --apply.
- Digest stability: datetime resolution, tz, dtype, NaN forms, column order,
  pandas 2 versus 3, parquet round trip. Any false mismatch (needless
  refresh) or false match.
- _strict_date and the carried path: any input that still carries a bound it
  should not, or refuses one it should.
- checked_pins: a pin that changes the effective schedule for a year the
  library lists without raising; a library update that should raise and does
  not; whether a raise anywhere reaches a false READY rather than HOLD or
  unknown. Does the source check claim more than it establishes?
- native_schedule as the single effective calendar: any other reader of the
  Shenzhen schedule on this branch that bypasses it.
- fix/159801-retraction-freshness (44a825a4, not merged): confirm or refute
  that switching its _unserved_native_sessions to native_schedule is
  sufficient for the two paths to agree; name any other semantic conflict.
- Confirm nothing changes signal construction, ranking, weights, rebalance
  rules or other sleeves.

Use C:\Users\phuaz\AppData\Local\Python\pythoncore-3.14-64\python.exe with
PYTHONNOUSERSITE=1, PYTEST_DISABLE_PLUGIN_AUTOLOAD=1, PYTHONDONTWRITEBYTECODE=1,
PYTHONPATH=C:\Users\phuaz\Documents\Codex\2026-10-27\task-4\isolated-native-hold-20261028T222552Z-2669296f\regress-v7\plugin
and flags -p no:cacheprovider -p block_network --basetemp=<your own folder>.
Reported results to verify, not trust: new file 51 passed; focused suites
210 passed; full suite 4,030 passed, 28 skipped, 0 failed.

Output: findings ranked by severity, each with file:line, the triggering
input or state and the wrong output, marked CONFIRMED (reproduced) or
PLAUSIBLE; then a one-line verdict on whether the change is safe to commit
on the branch. No contractions, British spelling. Do not modify any file.
```
