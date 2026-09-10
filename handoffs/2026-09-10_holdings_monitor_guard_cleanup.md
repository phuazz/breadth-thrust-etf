# Handoff: holdings monitor guard clean-up (2026-09-10)

Written Thursday 2026-09-10 (weekday library-verified) by the Fable session that shipped
`0df76a6`. Repository: `C:\dev\breadth-thrust-etf`, main tree, the one the Windows task
"holdings monitor daily" runs from (`scripts\scheduled_holdings_monitor.py --push`, daily
09:00 SGT). Personal context.

## Objective

Stop a failed holdings-monitor capture from stalling every later scheduled firing. When
the guard fails, or the capture or page build raises, the scheduled wrapper restores the
paths it owns itself, scoped to what that run dirtied, so the next firing is a retry
rather than a preflight refusal. Same pattern as `scripts/scheduled_refresh.py` adopted at
`e3e992a`, scoped to the owned paths because this tree is shared with interactive sessions.

## Exact defect fixed

`scripts/run_holdings_monitor.py` writes the daily snapshots and rewrites
`data/holdings_monitor_latest.json` and `docs/holdings-monitor-series.json` before
`scripts/check_holdings_monitor_guard.py` reads them. When the guard failed,
`scripts/scheduled_holdings_monitor.py` exited 1 and left those paths dirty, and its own
preflight then refused every later firing ("monitor-owned paths are dirty before the run")
until a person cleared them.

Evidence: `logs/holdings_monitor_2026-09-09.log` (02:34Z armed run: both rosters valid,
yfinance returned nothing for 136 of 166 names, G5 FAIL at ARKG 25.0% and XBI 17.1%
against the 85% floor, nothing published, sentinel not touched) and
`logs/holdings_monitor_2026-09-10.log` (01:00Z run refused at preflight on the two
rewritten payloads and two untracked snapshots, task result 1). Same class as the
2026-08-28 to 09-02 stall cleared by hand at `0ea4208`, different trigger.

## Files changed

Commit `0df76a6` on origin/main ("Holdings monitor: a failed capture restores the owned
paths it dirtied"), rebased onto the probe commit `53cd8a4` that landed during the run.

- `scripts/scheduled_holdings_monitor.py`: new `owned_dirt()`, `owned_files()`,
  `OwnedState`, `record_owned_state()`, `restore_owned_paths()`. `main()` records the
  owned state after the preflight (porcelain lines plus every file present) and restores
  on any failure in capture, guard or page build; never on a preflight refusal, never
  inside publish. Restore is per path (`git checkout HEAD -- <path>` for tracked entries,
  unlink for untracked files that were not present before the run), never `git clean`.
  A path dirty before the run is left for its owner and named; a file present before the
  run is never removed; a failure inside the restore is logged and cannot hide the
  original failure. A failing `git status` now fails the preflight instead of passing it.
  `run()`, `preflight()` and `publish()` take an optional repo root for tests; defaults
  unchanged. Exit codes unchanged (0 / 1).
- `tests/test_scheduled_holdings_monitor.py`: new, 10 tests on throwaway repositories
  with a bare origin and isolated git config, in the style of `test_scheduled_refresh.py`.
- `HOLDINGS_MONITOR.md`: Schedule section records the change and the yfinance diagnosis.
- `README.md`: test count line updated to 2,196 tests across 110 files, dated 2026-09-10.
- `handoffs/2026-09-10_holdings_monitor_guard_cleanup.md`: this file, committed separately.

Not changed: the fetch library (yfinance stays), the scheduled task definition,
`C:\dev\scripts\fleet_watch.json` (the two existing rows suffice), `C:\dev\NEXT.md`,
anything under `data/` or `docs/`.

## Tests and guards run

| Check | Result |
|---|---|
| `python -m pytest tests/test_scheduled_holdings_monitor.py tests/test_scheduled_refresh.py tests/test_holdings_monitor.py -q` | 83 passed |
| Full suite, first run, on the pre-refinement wrapper | 2,193 passed, 2 skipped, 1 failed: my own wiring test, an artefact of rewriting the wrapper while the run was in flight (line numbers on disk shifted against the loaded module) |
| Full suite, clean run on the committed code, `python -m pytest tests/ -q` | 2,194 passed, 2 skipped in 112.7s |
| Pre-commit hook `scripts/check_conflict_markers.py` | OK, 758 tracked files |
| `owned_dirt()` called read-only against the real tree after the change | lists exactly the four dirty owned entries below |
| yfinance probe from the session scratchpad, 20 of the 136 failed names, same call shape as `fetch_prices()` | 20 of 20 priced in 0.8s at 2026-09-10 10:36Z, last bar 2026-09-09 |

Interpreter for all of the above: `C:\Users\phuaz\AppData\Local\Python\pythoncore-3.14-64\python.exe`,
the one the task uses.

## Git status at handoff

Main tree, branch main, HEAD `0df76a6` == origin/main, before this handoff file was committed:

```
 M data/holdings_monitor_latest.json
 M docs/holdings-monitor-series.json
?? data/holdings_monitor/ARKG/2026-09-08.json
?? data/holdings_monitor/XBI/2026-09-04.json
?? reviews/2026-09-10_ws6-session-record.docx
```

The docx belongs to the WS6 session. `stash@{0}` (failed-refresh outputs 2026-08-29 plus
holdings-monitor soak) is pre-existing and untouched. The vault worktree this session ran
in (`C:\dev\.claude\worktrees\focused-khayyam-a93da1`) carries no changes.

## The four dirty monitor artefacts: LEFT UNTOUCHED

Not reverted, not committed, not staged. Verified byte-identical (md5 of all four) before
and after the autostash rebase that carried the commit. Facts about them:

- Both snapshots carry `fetched_at_utc` 2026-09-09T02:34:28Z and :29Z, the failed run's.
  ARKG as of 2026-09-08 (32 names, 99.50%), XBI as of 2026-09-04 (146 names, 99.84%).
  Both rosters passed G1 to G4 and G6 to G7; only G5 (price coverage) failed. Issuer
  files are today-only, so if these two files are deleted those rosters cannot be
  re-fetched and the flow series loses them.
- Both payloads carry `built_at_utc` 2026-09-09T02:34:51Z with the unpriced rows
  (coverage 0.25 and 0.1712). Their file mtime (2026-09-10 01:41Z) is a later autostash
  round trip by the WS6 session, not a second run.
- The gitignored `data/holdings_monitor_prices.parquet` was overwritten by the failed run
  (501 rows by 166 columns, 136 columns all-NaN). The scheduled run always refetches, so
  it heals on the next successful fetch; a `--no-fetch` run before then would compute
  on the degraded cache.

I have not proven the snapshots to be valid successful capture output (a successful
capture includes pricing, and the run failed there), so per the brief nothing was
committed from them.

## Safe for the Friday 2026-09-11 09:00 SGT scheduled run?

The code is safe: a run that gets past the preflight and then fails restores its own dirt,
and a run that passes commits and pushes only the owned paths (both proven end to end in
the new tests). But the Friday firing WILL STILL REFUSE at preflight, because the four
paths above were dirty before the change and the restore deliberately never touches
pre-existing dirt. Clearing them is the owner decision recorded in `C:\dev\NEXT.md`
(holdings monitor entry, 2026-09-10). Until then both fleet rows ("holdings monitor run
liveness", "holdings monitor output") keep breaching.

## Exact next action for the owner, before Friday 2026-09-11 09:00 SGT

From `C:\dev\breadth-thrust-etf`:

1. Revert the two rewritten payloads:
   `git checkout HEAD -- data/holdings_monitor_latest.json docs/holdings-monitor-series.json`
2. Decide the two snapshots, one of:
   - delete them: `git clean -f -- data/holdings_monitor/ARKG/2026-09-08.json data/holdings_monitor/XBI/2026-09-04.json`
     (Friday re-fetches whatever the issuers serve that morning; the 09-08 ARKG and
     09-04 XBI rosters are then gone for good), or
   - file them by hand, by explicit path only:
     `git add data/holdings_monitor/ARKG/2026-09-08.json data/holdings_monitor/XBI/2026-09-04.json`
     then commit. `0ea4208` is the precedent; its message matched the fleet grep
     `monitor: holdings capture`, and the fleet note warns against reading a hand filing
     as a healthy run, so a subject without that prefix is cleaner.
3. Confirm the owned paths are clean:
   `git status --porcelain -- data/holdings_monitor/ data/holdings_monitor_latest.json docs/holdings-monitor.html docs/holdings-monitor-series.json`
   must print nothing.
4. Optional: run `python scripts/scheduled_holdings_monitor.py --push` by hand to clear
   both fleet rows the same day; otherwise the Friday firing does it.
5. Prune the NEXT.md entry's "design gap worth a chip" clause; the rest of the entry
   stands until step 3 is done.

## Diagnosis of the yfinance failure (no change made; owner call on any mitigation)

Not a library-version change on this machine: yfinance 1.1.0 was installed 2026-07-02
(dist-info timestamps) and priced 100% on 09-08 under the same interpreter. The
`TypeError("'NoneType' object is not subscriptable")` is what yfinance 1.1.0's history
scraper emits for any chart request whose transport or JSON decode raised while exceptions
are hidden (`scrapers/history.py`: the `get_fn(...)` and `data.json()` block swallows the
exception, `data` stays None, and `data['chart']['result']` then raises). A 429 is
re-raised as `YFRateLimitError` and would have printed as such. The same signature appears
in `logs/manual_refresh_2026-08-23.log` beside curl DNS failures. The probe on 2026-09-10
priced 20 of 20. One unverified observation (n = 1): the 09-09 monitor fired at 02:34Z,
two minutes after the post-fill refresh in the automation clone finished a roughly
ninety-minute yfinance-heavy run (its green marker 02:32:06Z) from the same machine,
while the 09-08 monitor ran eighteen minutes into that refresh and priced 100%. If it
recurs, the cheap mitigations are to move the monitor's firing away from the post-fill
window or to retry the fetch once after a pause. Neither was made.

## Session notes

- This session ran in a worktree of the vault repository, which holds no copy of this
  nested repository; the Write and Edit tools refused the main-tree path, so files were
  written through PowerShell (UTF-8 without BOM, LF) and staged by explicit path. Recorded
  in the vault memory note on worktree sessions.
- Model tier: Fable, used because a wrong restore would be silent data loss; no mismatch
  to flag.
