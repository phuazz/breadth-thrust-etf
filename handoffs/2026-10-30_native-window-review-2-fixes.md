# Strategy C native-window hold: fixes for the second Codex review (of 451e5008)

Written 2026-10-30 (Friday, +08:00). All changes are uncommitted for independent review.

## Starting point

- Checkout: `C:\Users\phuaz\Documents\Codex\2026-10-27\task-4\isolated-native-hold-20261028T222552Z-2669296f\temp\rebase-check`
- Branch: `fix/native-window-hold-rebased`
- HEAD: `451e5008` (`451e500887d37da04409179b8d4486ca7bc50a02`), confirmed clean before any write.
- The branch holds three commits on `origin/main 44f1a6de`: `077c1e57`, `1273839b` and `451e5008`.
- No fetch, pull, rebase, merge, commit or push was made. Nothing reached the network.

The four original defects that `451e5008` closed stay closed: interrupted cache writes, malformed date strings, conflicting holiday pins and legacy cache construction. Their tests in `tests/test_native_record_binding.py` pass unchanged, and the content digest and `SIDECAR_CONSTRUCTION` checks are kept.

## Findings

Each finding was reproduced on `451e5008` before any code changed (`repro2.py`). The before-and-after log is `2026-10-30_repro-review-2-before-after.log` in the isolation folder.

### 1. P1: freshness combined two generations. CONFIRMED, RESOLVED

**Present cache.** On `451e5008`, `native_reach` read the parquet and `cache_reach` then read it again.

| Case | Result on `451e5008` |
|---|---|
| Generation A alone | behind 2026-04-16 (laggard X) |
| Generation B alone | behind 2026-04-02 (laggard 159801.SZ) |
| A replaced by B between the two reads | 2 reads, **current 2026-04-17** |

**Absent cache.** The prior report was read twice: once by `cache_reach_or_carried` and once by `_prior_row`.

| Case | Result on `451e5008` |
|---|---|
| Legacy report alone | unknown |
| Newer report alone | behind 2026-10-27, carried |
| Report rewritten between the reads | 2 reads, **current 2026-10-28, carried** |

**Fix** (`strategy_freshness.native_cache_reach_or_carried`):
- The parquet is read once. That frame is passed to both `native_reach(..., frame=)` and `cache_reach(..., frame=)`, which keep their file-reading defaults for other callers.
- The sidecar is still read once, separately. If both files change between the two reads, the record's digest does not match the frame in hand, and the line is unknown.
- A cache removed between the existence check and the read is treated as absent.
- On the absent path, `_prior_row` is called once. The bound, the laggards, `measured_at` and the native records all come from that one row, through the new helper `_carried`. `cache_reach_or_carried` uses the same helper, so sleeve B also reads its report once.

**After the fix:** the present-cache swap gives 1 read and behind 2026-04-16 (wholly generation A). The absent-cache swap gives 1 read and unknown (wholly the legacy report).

### 2. P2: malformed provenance aborted the report. CONFIRMED, RESOLVED

On `451e5008`, a `native_provenance` of `"broken"`, `["x"]` or `42` raised `AttributeError`. That aborted `build()`, so no sleeve was reported.

The fix validates the report at three levels.
- **`_prior_row`** treats the report as absent if the blob is not an object or `strategies` is not a list. It skips non-object rows, and drops a non-string `computed_at_utc`.
- **The carried path** requires:
  - `native_provenance` is an object;
  - `laggards` is None or a list of strings.
- **`_covers`** requires each record to be an object with the line's venue, `window_rows >= 200` and a strict `last_valid` no earlier than the bound, as before. It now also requires:
  - `last_observed_native_close`, `required_native_session`, `native_close_carried` and `holiday_data_through` are each None or a strict `YYYY-MM-DD` date;
  - `missing_native_sessions` is None or a list of strict dates;
  - `error` is None or a string.

Anything else makes sleeve C unknown. A fully valid carried record is still carried unchanged (test `test_a_whole_carried_record_is_still_carried`). `build()` with C's provenance set to `"broken"` reports A, B and D as current and C as unknown.

### 3. P2: downstream readers bypassed the effective Shenzhen calendar. CONFIRMED, RESOLVED for the two named readers

With the hypothetical 2027 pin (test input only), `451e5008` gave:

| Check | Value on `451e5008` |
|---|---|
| `native_schedule` last session | 2027-01-28 |
| `combined_book_risk.expected_last_session` | 2027-01-29 |
| Exporter `interior_gaps` on the true series | `["2027-01-29", "2027-02-01"]` |
| `missing_completed_session` | True (a vendor fetch would be requested) |

**Fix, `native_sessions`:**
- `EffectiveCalendar` wraps the shared library handle without mutating it. Its `schedule` drops the `checked_pins` closures and raises `HolidayPinError` on a conflict; every other attribute passes through.
- `effective_calendar(venue)` returns the wrapper for XSHG/SSE and the library calendar for every other venue.
- `completeness_calendar(venue)` returns `(effective calendar, None)`. On a pin conflict it returns `(library calendar, conflict message)`.
- `native_schedule` is now built on `effective_calendar`.

**Fix, `export_holdings_prices`:**
- `_venue_calendar` returns `completeness_calendar(name)[0]`, so `interior_gaps`, `completed_sessions_only`, `drop_unclosed_published_bars` and `missing_completed_session` all read the effective calendar.
- New `calendar_problem(name)` and `interior_gap_check(entry, ticker) -> (gaps, problem)`.
- `interior_gaps` keeps its signature.
- The export payload gains `completeness_unverified: {ticker: problem}` and a `CALENDAR:` log line.

**Fix, `combined_book_risk`:**
- `expected_last_session` reads the exporter's calendar.
- `load_prices` uses `interior_gap_check` and adds a reason for any calendar problem.
- A raising `expected_last_session` becomes a reason, not a crash.

**Fail-closed semantics:**
- **Pin conflict.** The pins are refused and the library calendar is used. It lists at least every session the pins would remove, so a gap check demands more bars, never fewer. The conflict is still reported, as a `combined_book_risk` reason ("expected sessions not established") and as an exporter `completeness_unverified` entry.
- **Calendar fault.** On `451e5008`, `interior_gaps` returned `[]` on any exception, which was a silent pass. It now returns a problem. `combined_book_risk` turns that problem into a reason, so its verdict is not verified.
- **NYSE and XETR.** Both are the library calendars by identity (`_venue_calendar(v) is get_calendar(v)`), with or without a conflicting XSHG pin.
- **2027.** No 2027 closure is pinned in source (test `test_no_2027_closure_is_pinned_in_source`).

**After the fix**, under the hypothetical pin: `expected_last_session` is 2027-01-28, `interior_gaps` is `[]`, `missing_completed_session` is False, and there are no risk reasons. Under a conflicting 2026 pin, both readers report the conflict. The NYSE and XETR expected sessions are unchanged (2026-10-28).

### 4. P3: the digest truncated nanoseconds. CONFIRMED, RESOLVED

On `451e5008`, a +1 ns index shift kept the digest and the record was accepted. `content_digest` now hashes the index as little-endian int64 nanoseconds since the epoch, prefixed by the tz and a scheme tag.

Kept unchanged: resolution independence (s, ms, us, ns), stable parquet round trips, column-order independence, NaN normalisation (`NaN`, `None` in an object column, `pd.NA` in `Float64`), and the scope of native columns plus the index.

What the tests show:
- Repairing a US column on an existing row keeps the record.
- Changing a native value by 1e-12 invalidates it, as does adding or removing a row.
- A tz-aware index hashes differently from a naive one.

**Consequence:** every sidecar written under `451e5008` stops matching, so the thematic cache refreshes once on its next run. Until then sleeve C reads unknown on the freshness page and HOLDs in live targets. This errs on the safe side and is expected.

## Sibling branch `fix/159801-retraction-freshness` (44a825a4): specification, not merged

**Disagreement confirmed.** Switching `_unserved_native_sessions` to `native_schedule` resolves the holiday disagreement. But when a pin conflicts with the library, `native_schedule` raises `HolidayPinError`, and the branch's call site in `_fx_convert_to_usd` does not catch it. The thematic run would then abort rather than HOLD.

**Required handling at the call site:**

```python
try:
    unserved = _unserved_native_sessions(native_last, aligned.index, venue)
except Exception as exc:  # calendar not established, HolidayPinError included
    aligned = aligned.mask(aligned.index > native_last)
    print(f"  WARN: {ticker} {venue} calendar not established ({exc}); rows "
          f"after the last served close {native_last.date()} left blank",
          flush=True)
else:
    if len(unserved):
        aligned = aligned.mask(aligned.index >= unserved.min())
        ...existing WARN...
```

Here `_unserved_native_sessions` takes its schedule from `native_sessions.native_schedule(venue, native_last + 1 day, later.max())[0]`. Blanking every row after the last served close leaves the line short on the decision row, so the coverage floor HOLDs the sleeve. Independently, the native-window check in `live_targets` reads the same calendar and fails closed on the same conflict. The specification leaves three things unchanged: conversion before the crypto reindex, the raw-vendor audit and verdict expiry.

**Verification.** `replay_159801_v2.py` in the isolation folder runs in memory, with no checkout or merge. It reads the helper by `git show`, asserts the branch's call site verbatim and composes it with `live_targets._rank`. Log: `2026-10-30_sibling-composed-replay.log`.

| Case | Result |
|---|---|
| Valid hypothetical 2027 pin, specified handling | READY, nothing unserved |
| Conflicting 2026 pin, the branch's call site unchanged | raises `HolidayPinError` |
| Conflicting 2026 pin, specified handling | HOLD (coverage floor), last row blank, no exception |

## Changed files (uncommitted)

| File | Change |
|---|---|
| `scripts/strategy_freshness.py` | single-snapshot reads, `_carried`, report and record validation |
| `scripts/native_sessions.py` | `EffectiveCalendar`, `effective_calendar`, `completeness_calendar`, `native_schedule` built on them, nanosecond digest |
| `scripts/export_holdings_prices.py` | effective calendar in `_venue_calendar`, `calendar_problem`, `interior_gap_check`, `completeness_unverified` in the payload |
| `scripts/combined_book_risk.py` | effective calendar, calendar problems become reasons |
| `tests/test_native_review_2.py` | new, 51 tests |
| `handoffs/2026-10-30_native-window-review-2-fixes.md` | this file |

No change to signal construction, ranking rules, weights, gates, rebalance schedules or other sleeves. The shared calendar change touches the exporter and the risk reader for XSHG/SSE lines only.

## Validation

Interpreter: `C:\Users\phuaz\AppData\Local\Python\pythoncore-3.14-64\python.exe` (3.14). Environment: `PYTHONNOUSERSITE=1`, `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONPATH=...\regress-v7\plugin`. Flags: `-p no:cacheprovider -p block_network`. Every `--basetemp` is under `<isolation>\temp\bt-review2-*`.

**New regressions.** `tests/test_native_review_2.py`: 51 passed on the fix. On unmodified `451e5008` (a `git archive` copy), 35 failed and 16 passed; the log is `2026-10-30_new-tests-on-451e5008.log`.
- The failures that demonstrate defects:
  - both generation-swap tests;
  - every malformed-provenance, laggards and report-shape case;
  - the `build()` case;
  - the two 2027 reader tests;
  - the nanosecond test.
- Some failures are only missing names on the old code, not defects: the NYSE/XETR identity checks, and parts of the conflict and fault tests, which call the new helpers.
- The 16 that pass on the old code confirm behaviour the fix must preserve: neither generation alone is current, a valid carry is kept, and digest stability.

**Affected suites** (18 files): 664 passed, 0 failed. The files are:
- capture_chain, check_freshness_headroom, china_market_holidays, combined_book_risk, derived_freshness, engine_tail_heal;
- export_holdings_prices, live_targets, native_record_binding, native_review_2, native_session_hold, native_signal_window;
- native_us_holiday_load_path, session_bounds, strategy_freshness, strategy_freshness_native, vendor_retraction_tripwire, venue_calendars.

**Full suite:** 4,273 passed, 28 skipped, 0 failed in 17 min 6 s. That is the 4,222 passed at `451e5008` plus the 51 new tests. Log: `2026-10-30_rebase-check-full.log`.

## Protected state (checked 2026-10-30 06:20 +08:00)

**`C:\dev\breadth-thrust-etf-sched`:**
- main at `49fe3261`, 16 modified files, 4 stashes;
- the 16 JSONs equal the backup (`compare16.py`: "all work==backup: True").

**`C:\dev\breadth-thrust-etf-wt-native-window`:**
- `fix/native-window-hold` at `4308a672`;
- the three modified data JSONs are unchanged, with SHA-256 prefixes `c67c61e4f3452846`, `98ecba0782e80880` and `7e216b92a6fef06f`;
- the parquet (`e621fb97ab45f079`) and sidecar (`85d99d007aecbcc1`) are unchanged;
- 110 ignored data files are present.

**Writes outside this checkout:** only under `<isolation>\temp\` and the isolation folder's logs.

## Open decisions

1. **`mixed_market_tail.venue_sessions`** still reads the raw XSHG schedule for `compute_breadth` and `build_panel_series` (sleeves A and D). No Shenzhen line is in those panels today, so it was left out of scope. Routing it through `effective_calendar` is a one-line change if wanted.
2. **`probe_vendor_availability`** uses "SSE" and `ADHOC_HOLIDAY_VENUES`, and raises `CalendarUnknown` past the library's holiday data, so it already fails closed. It does not apply the pins. Decide whether it should.
3. **Sleeve B's carried `data_through`** is still accepted in any truthy form (e.g. an integer). Only C's bound is parsed strictly. Making B strict changes B's behaviour, which this brief excluded.
4. **Whole-file versus native-column binding.** This remains native columns plus the index, as instructed, so `repair_price_gaps.py --apply` on a US line keeps the native record.
5. **Pin-conflict fallback in the exporter.** The library calendar is used and the conflict reported, rather than the export being aborted. The alternative is a non-zero exit, which would stop the whole holdings panel over one line.
6. **Sidecar non-native fields after a failed write.** These remain as noted in the 2026-10-29 handoff.
7. **One-off refresh.** The digest change makes every existing sidecar mismatch once. Sleeve C reads unknown and HOLD until the next thematic refresh.

Steps 3 and 4 of the earlier plan stay on hold pending this review and the owner's approval: fast-forwarding sched main to the rebased fix and pushing, then restoring the 16 JSONs.

## Artefacts (isolation folder)

- `2026-10-30_repro-review-2-before-after.log`, from `repro2.py`
- `2026-10-30_sibling-composed-replay.log`, from `replay_159801_v2.py`
- `2026-10-30_new-tests-on-451e5008.log`
- `2026-10-30_affected-suites.log`
- `2026-10-30_rebase-check-full.log`
- `temp/before-451e5008-full/`: a `git archive` copy of `451e5008` with the new test file, used for the old-code run

## Next review prompt

Review the uncommitted changes on `fix/native-window-hold-rebased` (HEAD `451e5008`) in
`C:\Users\phuaz\Documents\Codex\2026-10-27\task-4\isolated-native-hold-20261028T222552Z-2669296f\temp\rebase-check`
as an independent, adversarial reviewer. Read them with `git -C <that folder> diff` and
`tests/test_native_review_2.py`; context is `handoffs/2026-10-30_native-window-review-2-fixes.md`.

Read-only: do not edit, commit, stash, reset, rebase, fetch, pull or push. Do not switch or write
`C:\dev\breadth-thrust-etf-sched` (16 modified JSONs) or `C:\dev\breadth-thrust-etf-wt-native-window`
(three modified data JSONs and ignored caches). Run nothing that reaches the network.

First re-run your four reproductions (generation combination, malformed provenance, calendar bypass,
nanosecond digest) and say for each whether it is closed. Then try to break the fixes:
- strategy_freshness: any remaining path that takes parts of one verdict from two reads of the parquet,
  the sidecar or the report; any malformed report or record shape that still raises or still carries;
  any valid carried record now refused.
- The effective calendar: any reader of XSHG/SSE dates on this branch that still uses the raw library
  schedule (mixed_market_tail and probe_vendor_availability are known and listed as open decisions);
  whether the library fallback on a pin conflict can pass a check that should fail (gap checks,
  missing_completed_session, the two completed-session trims); whether EffectiveCalendar's
  pass-through lets any caller count sessions without the pins; NYSE/XETR unchanged.
- The digest: any false match or false mismatch across resolution, tz, dtype, NaN form, column order
  and parquet round trip; whether the one-off refresh it forces has any effect beyond sleeve C reading
  unknown or HOLD until the next refresh.
- The sibling specification: whether blanking rows after the last served close on any calendar fault
  is sufficient and reaches HOLD on every path (replay_159801_v2.py in the isolation folder), and any
  interaction with conversion before crypto reindexing, the raw-vendor audit or verdict expiry.
- Confirm nothing changes signal construction, ranking, weights, gates, rebalance rules or other sleeves.

Run tests with
`C:\Users\phuaz\AppData\Local\Python\pythoncore-3.14-64\python.exe`, PYTHONNOUSERSITE=1,
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1, PYTHONDONTWRITEBYTECODE=1,
PYTHONPATH=<isolation>\regress-v7\plugin, flags `-p no:cacheprovider -p block_network
--basetemp=<your own folder>`. Results to verify, not trust: test_native_review_2.py 51 passed
(35 fail on 451e5008); affected suites 664 passed; full suite 4,273 passed, 28 skipped, 0 failed.

This is intended as the closing review. The seven open decisions above are known and are not findings
unless you show a live defect on the current data. Mark each finding BLOCKING (a wrong live target, a
wrong freshness verdict, a crash, or a fail-open path) or FOLLOW-UP (hardening that can land after the
commit), so the commit is gated on BLOCKING findings only.

Output: findings ranked by severity, each with file:line, the triggering input or state and the wrong
output, marked CONFIRMED (reproduced) or PLAUSIBLE and BLOCKING or FOLLOW-UP; then a one-line verdict:
SAFE TO COMMIT, or NOT SAFE with the blocking finding numbers. No contractions, British spelling.
Modify no file.
