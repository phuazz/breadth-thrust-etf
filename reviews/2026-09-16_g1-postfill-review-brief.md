# Review brief: the 14 September fill was never recorded

**Repo:** `breadth-thrust-etf` (scheduled clone at `C:\dev\breadth-thrust-etf-sched`)
**Prepared:** 2026-09-16 (Wednesday), ~14:30 SGT
**Prepared by:** Claude Code session, from logs and live state on this machine
**Status:** open, unresolved at time of writing

Three questions for review are at the end. Everything before them is evidence.

---

## 1. Symptom

The published dashboard's "Latest rebalance changes" panel reads `2026-09-08`,
and "Current top holdings" reads `as of 2026-09-08`. The operator expected a
`2026-09-14` rebalance to appear.

## 2. What the engine actually holds

`data/live_targets.json` on `origin/main`:

```json
{
  "computed_at_utc": "2026-09-13T01:15:01.235516+00:00",
  "as_of": "2026-09-11",
  "next_fill": {
    "date": "2026-09-14",
    "decision_session": "2026-09-11",
    "venues_agree": true,
    "by_venue": {"NYSE": "2026-09-14", "XETR": "2026-09-14"},
    "decision_by_venue": {"NYSE": "2026-09-11", "XETR": "2026-09-11"}
  },
  "executed": false
}
```

The 14 September rebalance was computed. Decision session Friday 11 September,
fill Monday 14 September, both venues agreeing. It sits at `executed: false`.

The dashboard panel renders the last *executed* rebalance, which remains the
8 September fill (recorded by commit `60900d7`, "Local post-fill refresh
2026-09-09"). The last commit to touch `data/live_targets.json` is `a5e46de`,
"Local weekly refresh 2026-09-13". Nothing has been written since.

**Not verified:** whether the 14 September trades were actually placed at the
broker. `executed: false` is the engine's record, not a statement about the
market. If the trades were filled, the deployed book and the recorded book have
been apart since Monday.

## 3. Why nothing was written

Only the local `scheduled_refresh.py` run can record a fill. The cloud
workflow `daily_live_track.yml` marks NAV to market and, by its own header
comment, explicitly does not re-run signal generation.

The scheduled task:

```
Name       : BreadthThrust-PostFillRefresh
Action     : python.exe scripts\scheduled_refresh.py --push --cadence post-fill
WorkingDir : C:\dev\breadth-thrust-etf-sched
Principal  : phuaz, LogonType=Interactive, RunLevel=Limited
Triggers   : DaysOfWeek=4 (Tue) and 8 (Wed), Start 09:00 +08:00,
             Repetition Interval=PT1H Duration=PT6H
```

Every firing since Sunday 13 September has failed.

### Tuesday 15 September

```
FAIL  Strategy C (thematic): ends 2026-09-11, 1 session(s) behind expected
      2026-09-14  [strict: this run must include the latest completed session]

FAIL  G1 populated price tail: HOLLOW cache tail on traded panels
      EXV3: populated to 2026-09-11, rows to 2026-09-14 (2/34 roster names non-NaN on newest row)
      EXH3: populated to 2026-09-11, rows to 2026-09-14 (3/107)
      EXH9: populated to 2026-09-11, rows to 2026-09-14 (1/28)
      (an earlier attempt that day also had IUMS 21/25 and EXH3 1/107)
```

### Wednesday 16 September — four firings, all failed

```
09:00  FAILED exit 3  (G1)
10:00  FAILED exit 3  (G1)
11:04  FAILED exit 2  (git pull --rebase: could not resolve host github.com)
13:00  FAILED exit 3  (G1, detail below)
```

The 13:00 run reached VERIFY:

```
OK    G1 shared end_friday: 24 constituents panels all stamp 2026-09-11
FAIL  G1 populated price tail: HOLLOW cache tail on traded panels
      EXV1: populated to 2026-09-14, rows to 2026-09-15 (2/57 non-NaN on newest row)
      EXH3: populated to 2026-09-14, rows to 2026-09-15 (6/107)
      EXH9: populated to 2026-09-14, rows to 2026-09-15 (1/28)
WARN  G1 populated price tail: hollow cache tail, but OUTSIDE the traded book
      so it cannot move it (G6 split)
      NDIA: populated to 2026-09-11, rows to 2026-09-15 (137/165)
1 FAIL, 1 WARN, 10 ok
```

Then `FAILED exit 3` → tracked outputs restored (`RESTORE_ON_EXIT_CODES = (3, 4)`)
→ `[email skipped]`.

The guard is behaving as designed. This is the European overnight-retraction
cycle: the vendor withdraws recent European closes as NaN placeholder rows and
restores them roughly T+2. G1 refuses to let a published book rest on rows whose
constituents are not priced.

### The detail that motivates the review

Between Tuesday and Wednesday the three European panels moved from
`populated to 2026-09-11` to `populated to 2026-09-14`. The Monday closes
arrived. What now fails the guard is a **2026-09-15 row** — a session *newer*
than anything the pending book needs, since the fill is dated 2026-09-14 and the
decision session is 2026-09-11.

**Caveat on scope:** the FAIL names three panels. It is not established that
every traded panel is complete through 2026-09-14; only that these three are
populated through it. A reviewer should confirm the full set before acting on
the asymmetry below.

## 4. The retry window never overlaps the vendor's restoration

Repetition is hourly for six hours from 09:00 SGT, so firings run 09:00–14:00
and today's window is exhausted. The scheduler now reports:

```
BreadthThrust-PostFillRefresh  Next = Tuesday 22 September 09:00
BreadthThrust-WeeklyRefresh    Next = Saturday 19 September 09:00
```

The vendor restores withdrawn European closes overnight (roughly 01:00 UTC =
09:00 SGT for the T+2 bar). The window opens at 09:00 SGT and closes at 14:00
SGT the same day. A retraction that clears after 14:00 SGT is not retried until
the next calendar trigger — six days later for the post-fill cadence. The
post-fill moment for this week has been missed regardless of what happens next.

## 5. Every failure was silent

All four of Wednesday's failures, and Tuesday's, ended with:

```
[email skipped: GMAIL_USER / GMAIL_APP_PASSWORD not set]
```

`_email()` in `scripts/scheduled_refresh.py:293` requires both variables:

```python
user = os.environ.get("GMAIL_USER")
pw = os.environ.get("GMAIL_APP_PASSWORD")
if not user or not pw:
    log.write("\n[email skipped: GMAIL_USER / GMAIL_APP_PASSWORD not set]\n")
    return
```

Measured on the machine: `GMAIL_APP_PASSWORD` was set at User scope;
`GMAIL_USER` was not. One missing variable — an email address, not a secret —
silenced the alerting on the only job that can publish a rebalance. The
failure was found by a human noticing a date on a dashboard, three days late.

`GMAIL_USER` has since been set to `phuazz@gmail.com` at User scope. The path
remains **unproven**: no send has been exercised, and the Task Scheduler service
has not yet been restarted to pick up the new environment. A secondary risk is
that the stored password is 19 characters including spaces (the displayed
four-block form); whether `smtplib` login accepts that form here is untested.

By contrast, both cloud workflows (`daily_live_track.yml` in this repo,
`daily_monitor.yml` in `multi-strategy-portfolio`) have working `if: failure()`
email steps. The gap is local-only.

## 6. Downstream state

The `multi-strategy-portfolio` monitor consumes this engine's outputs. Its
published health blob currently reads `level: ok` after a manual re-run today,
but its strategy feed reflects the stale book:

```
ok  Price / NAV (live_track)          asOf 2026-09-15  lag 0/0
ok  Breadth / regime panel            asOf 2026-09-11  lag 3/8
ok  Strategy equity (multi_strategy)  asOf 2026-09-11  lag 3/12
ok  Benchmark S&P 500 (engine export) asOf 2026-09-15  lag 0/1
```

The 3-session lag on the strategy feeds sits inside a 12-session budget, so
nothing downstream flags it. A stalled upstream publish is invisible to the
consumer for up to twelve sessions.

---

## Questions for review

**Q1 — Should the hollow-tail guard be evaluated against the date the book
needs, rather than the cache's newest row?**

A row dated later than the decision session and later than the fill date cannot
move the book, yet it fails the run closed. The codebase already contains an
analogous carve-out on a different axis: the G6 split downgrades a hollow tail
to WARN when the panel is outside the traded book. A date-based sibling —
hollow rows strictly after the required book date downgrade to WARN — is the
obvious symmetric move.

The strongest argument *against*, which should be weighed properly rather than
dismissed: a hollow newest row is evidence the vendor is mid-retraction, and
that may mean the 14 September data is itself provisional rather than settled.
If restoration can revise earlier rows and not merely append, the current
conservative behaviour is correct and the fix belongs elsewhere. Determining
which is true is the crux.

**Q2 — Is the retry window misaligned with the failure mode it exists to
absorb?**

Six hours from 09:00 SGT, on Tuesday and Wednesday only. The vendor behaviour it
must survive resolves overnight on an unpredictable schedule. Options include
widening the duration, adding an evening trigger, adding a catch-up trigger on
the following days, or making the post-fill cadence retry until it succeeds
rather than until a clock expires. Note `BreadthThrust-CatchupCleanup` exists
but last ran 2026-08-27 with result `4294770688` and has no next run scheduled —
whether it was meant to cover this case is worth establishing.

**Q3 — Should a scheduled run that cannot alert refuse to start?**

The house rule is that no unattended agent ships without a guard layer that can
catch a silently-wrong step. Here the guard layer itself was disabled by a
missing environment variable, and nothing said so. A startup assertion — verify
alerting is configured, and fail loudly to the log and the task exit code if it
is not — would have converted three days of silence into a visible first-run
failure. The counter-consideration is that refusing to run would also stop the
refresh from publishing in a case where alerting is broken but the refresh
itself would have succeeded.

## Relevant files

- `scripts/scheduled_refresh.py` — `_email()` at line 293,
  `RESTORE_ON_EXIT_CODES` and `restore_tracked_outputs()` below it
- `scripts/compute_breadth.py` — `verify_price_tail`, the constituent-panel
  tail check (may DROP a row the vendor confirms unserved)
- `scripts/vendor_tail.py` — the price-frame sibling: `cache_current_through`
  (least-current column) and `heal_hollow_tail` (single-ticker refill)
- `scripts/live_targets.py:338` — where `"executed": False` is written
- `.github/workflows/daily_live_track.yml` — the cloud job, which does not
  re-run signals
- `C:\dev\breadth-thrust-etf-sched\logs\scheduled_refresh_2026-09-15.log`
  and `..._2026-09-16.log` — full evidence for sections 3 and 5
