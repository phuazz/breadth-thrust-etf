# Holdings monitor repair handover

Status: repair and guarded external publication complete. The dates below were
confirmed by the owner's 2026-09-26 instruction to proceed and are copied from
scheduler, log, payload and commit evidence.

## Root cause

The task was enabled and armed, not paused or migrated:

- Task Scheduler task: `holdings monitor daily`; enabled, `Ready`.
- Trigger: daily at 09:00 Singapore time; next trigger observed as
  2026-09-27 09:00 SGT.
- Action: `C:\Users\phuaz\AppData\Local\Python\pythoncore-3.14-64\python.exe
  "C:\dev\breadth-thrust-etf\scripts\scheduled_holdings_monitor.py" --push`.
- Working directory: `C:\dev\breadth-thrust-etf`.
- Account: `phuaz`, interactive-only; last run 2026-09-26 09:00:01 SGT,
  result 1. Battery start was disabled. No intentional pause was found.

The first failing pipeline step on 2026-09-22 through 2026-09-26 was G7,
after source capture and pricing completed. XBI was fresh and internally
valid: as-of 2026-09-24, 157 holdings, 99.86% weight sum, 100% price coverage,
and 10 dropped rows. G7 nevertheless counted 98.7% of names as moved against
the last persisted 2026-09-17 snapshot, with fund scale -9.30%, and blocked
the page build and publication. The wrapper restored the run-owned files and
left the success sentinel unchanged. The earlier `git pull --rebase` warning
was caused by unrelated dirty files in the shared root, but it was explicitly
non-fatal and was not the first failing step.

XBI is registered as an inactive equal-weight index fund. Its broad changes
at index rebalancing are mechanical, not manager activity. State Street
describes XBI as tracking a modified equal-weight biotechnology index; the
official prospectus states that the index rebalances and reconstitutes
quarterly on the third Friday: [SSGA XBI prospectus](https://www.ssga.com/library-content/products/fund-docs/etfs/emea/us-etf/XBI_SUMPRO_EMEA.pdf).

## Bounded repair

The G7 decision was extracted into `flow_turnover_check`. It remains blocking
for active funds. For inactive/index funds it remains visible as `WARN`, but
does not block a current roster from publishing. No freshness threshold,
source parser, cadence, signal, construction, portfolio weight or registered
research parameter changed.

The page had two pre-existing 10.5px base declarations that failed the
repository publication floor. Both were raised to 11px; the existing mobile
override and page content were otherwise unchanged.

Changed commits on the repair branch:

- `a9c92469` — G7 index-fund exception and regression tests.
- `9143c18` — page font-floor correction and rebuilt page.

Changed files: `scripts/check_holdings_monitor_guard.py`,
`tests/test_holdings_monitor_guard.py`, `HOLDINGS_MONITOR.md`,
`holdings_monitor_template.html`, and `docs/holdings-monitor.html`.

## Validation

Focused regression suite:

```text
49 passed in 29.85s
```

Fresh supported safe-mode wrapper, isolated clean worktree, started
2026-09-26 08:00:53 UTC:

- ARKG source as-of 2026-09-25; XBI source as-of 2026-09-24.
- 177 names priced; 177/177 priced; 170 with a 200-day average.
- Guard: 15 checks, 0 FAIL, 1 WARN. The only warning was XBI G7 mechanical
  rebalancing.
- Page build: inline script parse passed; `RESULT: OK`.
- Safe mode did not push.

Required static publication check passed after the 11px correction:

```text
0 fail, 1 warn, 9 ok
```

The available browser session rendered the page at its actual 710px client
width with 710px document width, 11px smallest visible font and three visible
sections. The session did not expose the required viewport-resize capability,
so the four required 390/844/768/1280px emulated measurements are not claimed.

The existing failure contract remains covered by
`tests/test_scheduled_holdings_monitor.py`: a guard failure restores only the
run-owned paths, does not touch the sentinel, and cannot report `RESULT: OK`.

## Before and after records

Before the repair:

- Success sentinel: `2026-09-20T02:11:49+00:00`.
- Payload `built_at_utc`: `2026-09-20T02:11:27+00:00`.
- Filesystem modification time was newer, but did not represent a successful
  capture.
- Task attempts on 2026-09-21 through 2026-09-26 either failed at source
  transport or G7; no heartbeat advance was claimed.

After the repair, in the isolated armed-path test against a local bare origin:

- Payload `built_at_utc`: `2026-09-26T08:04:12+00:00`.
- Success heartbeat: `2026-09-26T08:04:20+00:00`.
- Commit: `c1f0abb5`, `monitor: holdings capture 2026-09-26`.
- Local bare `main` received the commit; this is not the external GitHub
  remote.

The heartbeat therefore advanced after capture, guard, build, commit, rebase
and local publication. The subsequent authorised external run completed the
same sequence against GitHub:

- Payload `built_at_utc`: `2026-09-26T08:20:06+00:00`.
- Success heartbeat: `2026-09-26T08:20:23+00:00`.
- Commit: `1eac83b2`, `monitor: holdings capture 2026-09-26`.
- Remote `origin/main`: `1eac83b20c41fd9a180f95d88c5f80947e3ea8af`.

## Remaining limitations and next run

- The scheduled task still runs from the shared root. Its existing ownership
  and dedicated clone roles were not consolidated or changed.
- XBI G7 is intentionally a warning because it is an inactive index fund;
  source age, as-of monotonicity, roster size, weights, price coverage and
  dropped-row guards still block on failure.
- External publication completed at 2026-09-26 08:20:23 UTC: remote
  `origin/main` now points to `1eac83b20c41fd9a180f95d88c5f80947e3ea8af`.
- The external capture payload was built at 2026-09-26 08:20:06 UTC and the
  success heartbeat advanced at 2026-09-26 08:20:23 UTC. The heartbeat is
  later than the payload and the wrapper reported `RESULT: OK`.
- The next expected scheduled run is 2026-09-27 09:00 SGT, subject to owner
  confirmation of the dated handover.
