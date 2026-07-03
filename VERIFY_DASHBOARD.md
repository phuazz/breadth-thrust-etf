# VERIFY_DASHBOARD.md — freshness + refresh integrity audit

Reusable prompt for auditing the live dashboard at
https://phuazz.github.io/breadth-thrust-etf/. Paste the block between the
rules into a Claude Code session at the project root. The audit is
read-only — it changes nothing and proposes fixes rather than applying them.

Written 2026-07-03, immediately after the 26 Jun – 1 Jul incident (four
consecutive failed daily runs plus the failed scheduled weekly, all caused by
the `breadth_csp1.json` freshness guard tripping once the local heavy refresh
fell behind). Keep the checklist in sync with `.github/workflows/*.yml` and
`scripts/pipeline.py` when those change.

---

```
[CONTEXT]
- Dashboard: breadth-thrust-etf — USD 4-sleeve ETF rotation, live on GitHub Pages.
- Architecture: template.html (~550KB) + data/*.json + scripts/pipeline.py
  → docs/index.html (~7MB, data inlined at build time).
- Refresh surface (three layers):
  1. .github/workflows/daily_live_track.yml — cron '30 21 * * 1-5'
     (Mon–Fri 21:30 UTC, after US close): mark_to_market_live.py →
     pipeline.py → pytest → commit "Daily live track refresh YYYY-MM-DD".
  2. .github/workflows/weekly_factsheet.yml — cron '0 22 * * 5'
     (Fri 22:00 UTC): Strategy B + C engines → blend → risk overlay →
     mark-to-market → pipeline.py → pytest → commit
     "Weekly refresh YYYY-MM-DD" → email factsheet PDF.
  3. Local-only heavy refresh (scripts/refresh_all.py): constituent rosters
     + breadth panels for sleeves A and D. The per-ETF parquet price caches
     are gitignored, so CI can NEVER regenerate these — the committed
     breadth_*.json, europe_rotation.json and topk_robustness.json are
     canonical and only advance when the refresh is run locally and committed.
- Hard freshness guard: pipeline.py::assert_source_panel_fresh_vs_today
  aborts EVERY build (daily and weekly) when data/breadth_csp1.json
  end_date lags the run date by MORE than 5 numpy business days.
  numpy.busday_count counts weekdays only — NYSE holidays count against the
  budget, so around every US holiday the guard trips one trading day earlier
  than a true market calendar would. That is deliberate fail-early
  behaviour, not a bug; mirror it when forecasting the guard.
- Ops alerting (added 2026-07-03): both workflows email GMAIL_USER on any
  failure (if: failure() step) and send a freshness warning from
  weekday-lag 4 via scripts/check_freshness_headroom.py. When auditing,
  a recent [WARN] email plus green runs is a consistent state, not a
  contradiction.
- Silent-wrong-data defences (added 2026-07-03): in-run,
  scripts/check_capture_integrity.py anchors freshly-fetched series to
  the true NYSE calendar (warn at 1 session behind, job-fail at 2+ or a
  corrupt tail). Outside-in, .github/workflows/sentinel.yml (daily
  03:35 UTC — sized to GitHub's cron-delay tail, measured up to ~4h on
  this repo; do not move earlier than ~02:30 UTC) fetches the DEPLOYED
  factsheet_meta.json and emails [SENTINEL] on an as-of mismatch vs the
  calendar. Include sentinel.yml
  in the run-health sweep (gh run list --workflow=sentinel.yml) — it is
  itself evidence for check 2, but do not substitute it for fetching the
  deployed URL directly.
- Cadence rule (Zhenghao, 2026-07-03): the weekly factsheet runs every
  Friday after the US close even on US market holidays, publishing the
  latest populated close — a Friday-holiday factsheet dated Thursday is
  correct, not stale.

[TASK]
Audit two things: (a) the DEPLOYED dashboard shows the latest datapoints it
should as of now, and (b) the refresh pipeline is healthy — not merely green
on its last run, but with enough headroom that it stays green through the
next several scheduled runs. Read-only. Propose fixes; do not apply them.

Before running any check, state the three ways this audit could be silently
wrong, and design around each:
1. Auditing the repo instead of the site. Local files can lead or lag what
   Pages actually serves. Every claim about "the dashboard" must be
   evidenced from a deployed URL or the Pages deployment SHA, never from
   the working tree alone.
2. Wrong calendar. On a weekend or NYSE holiday, "no new datapoint" is
   correct; naive weekday arithmetic makes stale data look fresh or fresh
   data look stale. Derive the last completed NYSE session with
   pandas_market_calendars (pinned in requirements.txt). Never compute
   weekdays or holidays from memory. Use the NYSE calendar to decide what
   data SHOULD exist; use numpy weekday counting only to predict what the
   pipeline guard WILL do.
3. Green run ≠ fresh data. A workflow can succeed while committing nothing
   or carrying a stale roster forward, and a cron can silently not fire at
   all (GitHub disables schedules after 60 days of repo inactivity and
   delays them under load) — a missing run leaves no failure to look at.
   Verify data end-dates and commit heartbeats, never run status alone.

[CHECKS — run all nine; report each with evidence]

1. Reference dates. With pandas_market_calendars ('NYSE'): the last
   completed session as of now (UTC), the next session, and any holiday in
   the past 7 calendar days. Every later check compares against these.

2. Deployed as-of. Fetch
   https://phuazz.github.io/breadth-thrust-etf/factsheet_meta.json —
   asof_iso must equal the last completed NYSE session from check 1, and
   computed_at_utc must postdate that session's close. Tolerance: if the
   current UTC time is before ~23:00 UTC on a session day, the daily job
   (21:30 UTC + ~1h) may not have landed yet and asof legitimately reads
   the prior session — report that as PASS with the cutover noted.

3. Deployed = HEAD. gh api repos/phuazz/breadth-thrust-etf/pages/builds/latest
   — status "built" and sha equal to origin/main HEAD. A failed LATEST
   Pages build is a FAIL even if an earlier build succeeded; failed
   intermediate builds superseded by a green latest one are non-issues.

4. Scheduled-run health, both workflows.
   gh run list --workflow=daily_live_track.yml --limit 10
   gh run list --workflow=weekly_factsheet.yml --limit 5
   Every scheduled run on a NYSE session day should be success. For any
   failure: gh run view <id> --log-failed, quote the actual exception, and
   classify it — freshness-guard abort / upstream fetch block / test
   failure / timeout / email step. Also verify the daily cron actually
   FIRED on each of the last 5 session days (compare run list against the
   calendar; a dropped cron produces no run and no failure).

5. Commit heartbeat. git log --grep on main: the latest
   "Daily live track refresh" commit must carry the date of the last NYSE
   session (no commit on a holiday is correct — do not flag); the latest
   "Weekly refresh" commit must be within the last 8 days.

6. Data anchor table. For each file report: end/max date, weekday-lag
   (numpy, as the guard sees it), true NYSE trading-day lag, and status
   against its threshold:
   - data/breadth_csp1.json end_date — THE gating panel, budget 5 weekday-lag
   - data/live_track.json — last live_dates entry, computed_at_utc, anchor_date
   - data/multi_strategy.json common_end — the A/D-constrained blend anchor
   - data/asset_class_rotation.json and data/thematic_rotation.json —
     CI-refreshed weekly, expect ≤ 7 days
   - data/europe_rotation.json, data/breadth_soxx.json,
     data/topk_robustness.json — advance only on local refresh
   - data/risk_overlay.json — regime gate + EEM tilt state date; the tilt
     and regime shown on the page are only as fresh as this file

7. Guard headroom forecast. Recompute the guard's numpy lag for
   breadth_csp1.json at each of the next 5 scheduled run datetimes. Report
   the first run (UTC and SGT) that will FAIL if scripts/refresh_all.py is
   not run and committed before it. An audit that only says "green today"
   has not done this check.

8. Rendered page. curl the live page into the scratchpad (docs/index.html
   is ~7MB — NEVER read it whole into context; grep the scratch copy).
   Confirm: built_at stamp consistent with the latest pipeline run; the
   Data Health panel rows agree with check 6; the stale-data banner is
   present/absent as the thresholds say it should be; displayed regime
   state and EEM tilt state match risk_overlay.json / live_track.json.
   If browser tooling is available, also load the URL and check console
   and network errors; otherwise the grep evidence suffices.

9. Cross-consistency. deployed_key identical across live_track.json,
   factsheet_meta.json and multi_strategy.json. The dated factsheet PDF
   named in factsheet_meta.json returns HTTP 200 at the deployed URL. The
   email step of the last weekly run concluded success.

[SUCCESS CRITERIA]
- Must: every check gets PASS / WARN / FAIL / UNVERIFIED with a command
  output or URL as evidence — no verdict from memory or assumption.
- Must: a one-line overall verdict first — is the deployed dashboard
  showing the latest datapoint it should, yes or no, as of which session.
- Must: an "actions required" list with deadlines in UTC and SGT
  (e.g. "run refresh_all.py and commit before Mon 21:30 UTC / Tue 05:30
  SGT or the daily run fails").
- Must: flag every date in the report for user confirmation (house rule).
- Out of scope: applying fixes, running refresh scripts, dispatching
  workflows, ALLOW_STALE_REGIME overrides, editing anything.

[CONSTRAINTS — house rules]
- Never open docs/index.html (~7MB) or template.html (~550KB) whole;
  grep -n plus line-range reads only.
- All date arithmetic via a date library / market calendar; never weekday
  or holiday reasoning from memory.
- Read-only: no writes outside the scratchpad.
- If gh is unauthenticated or an API call is denied, mark the affected
  checks UNVERIFIED and say so — do not silently narrow the audit.

[OUTPUT FORMAT]
1. Verdict line.
2. Check table: check / status / one-line evidence.
3. Incidents found, each with the root cause quoted from logs.
4. Actions required, with deadlines (UTC + SGT).
5. Watch items — what goes stale next, and on which date.
```
