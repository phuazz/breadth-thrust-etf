# Handoff — post-fill publication debt, revision evidence, alerting observability

**Working directory:** `C:\dev\breadth-thrust-etf` (scheduled clone `C:\dev\breadth-thrust-etf-sched`)
**Model / effort:** Claude Opus 5, high effort, 2026-09-16
**Inputs:** `reviews/2026-09-16_g1-postfill-review-brief.md`, an external review of the
first implementation pass, `DATA_INTEGRITY_POLICY.md` read in full. No repo-level
`AGENTS.md` or `CLAUDE.md` exists — the vault-level `C:\dev\CLAUDE.md` and the user-level
file govern.

**Status: NOT RESOLVED.** The 14 September fill is still unpublished, G1 still blocks it,
and no alert delivery has ever been demonstrated from either clone. This pass repaired the
instruments; it did not recover the incident.

---

## 1. What this pass was

The previous pass built three modules — publication debt, revision capture, run status —
and an external reviewer found implementation defects in all three. This pass reproduced
each claimed defect, adjudicated it, and fixed the supported ones. Every finding was
reproduced before any code was changed; the reproductions are recorded in section 2 and
each is now a named regression test.

---

## 2. Adjudication — every finding reproduced, every finding upheld

| # | Finding | Verdict | Reproduction |
|---|---|---|---|
| 1a | Debt derives solely from the current `next_fill`, losing an older obligation | **UPHELD** | `evaluate(fill=2026-09-21, through=2026-09-08, asof=2026-09-16)` returned `owed:false` |
| 1b | Missing / malformed targets read as "nothing owed" | **UPHELD** | `required_fill_date(None)` and `{"date":"garbage"}` both returned None -> not owed |
| 1c | Divergent venue fill dates read as "nothing owed" | **UPHELD** | `by_venue {NYSE: 09-08, XETR: 09-07}` -> `required_fill_date` None |
| 1d | `latest_publication` defaults to HEAD; local unpushed commits discharge debt | **UPHELD** | `ref` default was `"HEAD"` |
| 1e | Evidence is a commit subject naming the CSP1 panel date, not the sleeve records | **UPHELD** | by inspection; the subject carries `panel_end`, nothing about `latest_rebalance` |
| 1f | The daily green marker short-circuits before debt evaluation | **UPHELD** | the `already_ran_today` return preceded the debt block |
| 1g | `--catch-up` does not propagate to component children | **UPHELD** | `run_child` built child argv without the flag |
| 2a | Capture runs after preservation, so a raw withdrawal is masked | **UPHELD** | raw-vs-prior = 1 withdrawal; post-preservation = 0 |
| 2b | `diff_frames` compares only intersecting rows/columns | **UPHELD** | a dropped populated row scored `withdrawals 0, rows_removed 1` |
| 2c | `summarise` does not measure restoration cycles | **UPHELD** | keys were runs/fills/withdrawals/revisions/basis_changes only |
| 2d | "auto" treated as a vendor | **UPHELD** | `column_basis({"source":"auto"})` -> `{"__default__": "auto"}` |
| 3a | Malformed prior streak raises in `record_alert` | **UPHELD** | `{"consecutive_failures":"bad"}` -> ValueError |
| 3b | A JSON array prior raises | **UPHELD** | `[1]` -> AttributeError on `prior.get` |
| 3c | An unknown health status returns OK | **UPHELD** | `status:"nonsense"` -> `("OK", ...)` |
| 3d | The planned fleet row watches only `alert_ok.json` age | **UPHELD** | by inspection of the proposed row |
| 4 | The recovery probe names non-constituents | **UPHELD** | measured against the 2026-09-11 rosters — see 5.2 |

Nothing was rejected. Two of the findings (1d, 2a) are the ones that mattered: each made
its module report the reassuring answer in exactly the state it existed to catch.

### 2.1 Baseline re-checked, not inherited

Re-measured 2026-09-16 against the sched clone with the repo's own definitions
(`check_refresh_guard.price_cache_side`, `compute_breadth.priced_sessions`):

```
panel  idx_end     pop_end     09-11      09-14      09-15
CNDX   2026-09-15  2026-09-15  102/102    102/102    102/102
CSP1   2026-09-15  2026-09-15  501/504    501/504    501/504
EXH1   2026-09-14  2026-09-14  26/26      26/26      no row
EXH3   2026-09-15  2026-09-14  107/107    107/107    6/107    <- partial, KEPT, FAILS G1
EXH9   2026-09-15  2026-09-14  28/28      28/28      1/28     <- partial, KEPT, FAILS G1
EXV1   2026-09-15  2026-09-14  57/57      57/57      2/57     <- partial, KEPT, FAILS G1
EXV3   2026-09-14  2026-09-14  34/34      34/34      no row
IDP6 IUCD IUCM IUES IUIS IUMS IUSP IUUS SOXX   all 100% to 2026-09-15
IUCS 33/34, IUFS 75/76, IUHC 59/60, CSP1 501/504   same counts on all three dates
```

**The corrected claim.** All 19 traded panels carry the SAME NUMBER of priced roster
names on 11 and 14 September, and all five European panels are fully populated on both.
The four US panels below 100% carry identical counts on all three dates, so the shortfall
is structural rather than tail hollowness. **Equal counts are not identical prices**, and
nothing here measures whether a value changed — that is the open question in section 4,
not a settled one. The previous wording ("priced identically") overstated a coverage
measurement as a value measurement.

The block is confined to the 2026-09-15 row on three European panels, which no part of the
pending book (decision session 2026-09-11, fill 2026-09-14) reads.

---

## 3. Changes implemented

Full suite after the fixes: **2568 passed, 2 skipped** (`python -m pytest tests/ -q`,
125.61s). Nothing committed, nothing pushed, nothing enabled — see sections 7 and 8.

### 3A. `scripts/publication_debt.py` — rewritten

**The obligation is durable.** One entry per `(venue, fill date)` in
`logs/publication_obligations.json`, carried until discharged, superseded or evicted.
Advancing `next_fill` ADDS an obligation and cannot remove the one behind it. An
obligation that can be erased by the thing it polices is not an obligation.

**Explicit states.** `owed` / `pending` / `discharged` / `superseded` / `unknown`. A fill
the published book has moved PAST without ever recording is `superseded` and flagged
`missed: true` — a permanent miss, not a success, and it escalates. `unknown` is never
spelled "nothing owed": the report carries `owed` and `unknown` as separate fields and
`should_run` is true for either.

**Venue-aware.** Obligations are read from `next_fill.by_venue` and discharged by the
sleeves that trade on that venue, from the book's own sleeve/venue map. The divergence is
real and live: on the Labor Day week XETR filled Monday 7 September and NYSE Tuesday the
8th, which is visible in the engines' records (sleeve D 2026-09-07, sleeves A/B/C
2026-09-08).

**Evidence is remote and specific.** Discharge requires that at `origin/main` — never
HEAD — every sleeve of that venue carries `headline.latest_rebalance.date` at or past the
fill. That is the record the surfaces read, and it advances on a HELD week too (verified:
sleeve C last traded 2026-08-24 and its `latest_rebalance` reads 2026-09-08), so a stalled
publication cannot hide behind "nothing changed". A local commit, a failed push, or an
unrelated fresh panel do not discharge anything. `unpushed_commits` is reported beside the
verdict. The commit subject is still parsed and reported, as context only.

**Escalation is bounded.** Once per obligation per day, capped at
`MAX_ESCALATIONS = 5`, after which the obligation stays in every verdict and stops
mailing. A row expected to alarm is how a notifier gets ignored.

**Broker execution is not inferred.** The module measures publication only. `executed` is
untouched and unread.

Live verdict, 2026-09-16:

```
owed true, unknown false, escalate false, oldest_owed_fill 2026-09-14, age_days 2
  NYSE|2026-09-14  owed   sleeves A,B,C   published A/B/C at 2026-09-08
  XETR|2026-09-14  owed   sleeve  D       published D     at 2026-09-07
  evidence: origin/main 1f55cb6, unpushed_commits 0
```

### 3B. `scripts/price_revisions.py` — rewritten around a raw observation

**The primary evidence is now taken before anything transforms the frame.**
`record_vendor_observation` is called in `download_prices` immediately after the raw
download is renamed and reindexed, and BEFORE the cell-preservation merge, the Norgate
overlay and the tail verification. That is the only point in the function where the frame
is what the vendor served.

- **A per-cell state machine** — absent / served / withheld — retains the last served
  value on a withheld cell, so a served -> withheld -> restored CYCLE can be identified
  and the restored value compared with the withdrawn one. `logs/vendor_state/<panel>.json`
  plus a capped `logs/vendor_events.jsonl`.
- **Unanswered is not missing.** An empty frame is an unanswered RUN; a column with
  nothing anywhere in the frame is an unanswered TICKER. Neither produces withdrawals.
- **Unfinished sessions are excluded.** Only dates strictly before the current UTC date
  are admitted, further bounded by `required_through`. A partial intraday bar is not a
  missing one.
- **Provenance is resolved, not assumed.** `auto` is a selection policy, not a vendor:
  columns named in `columns_from_norgate` are `norgate`, the rest are `yfinance`, and a
  strict-Norgate run's unresolved columns are `unknown` and withheld from the evidence.
- **Adjustments are separated from revisions.** A column re-scaled by one constant ratio
  across at least three cells is an `adjustment`, not a set of restatements.
- **Bounds are reported, not hidden.** Window 8 sessions, 6000 cells per panel, 30-day
  retention on a withheld cell, 2000 events. A cell evicted while still withheld is
  counted as a cycle that will never close.

The cache-before-write diff is KEPT as a labelled secondary diagnostic
(`logs/cache_changes.jsonl`, `kind: cache_diff`), now counting populated cells lost to a
removed row or column and carrying its own caveat that it runs post-preservation.

**The verdict refuses to over-read.** `cycle_summary` reports NO EVIDENCE / INSUFFICIENT
EVIDENCE / BOUNDED, NOT PROVEN / REVISION OBSERVED, states cycle coverage, missing
evidence and retention limits, and says in terms that zero cycles is not evidence of
append-only behaviour. Current state: **NO EVIDENCE** — no observation has been recorded,
because the code is not deployed and no refresh has run under it.

### 3C. `scripts/run_status.py` — made safe, and independently observed

- Everything read from disk is validated. A malformed prior record restarts the streak and
  says so on the new record; a file that is not a JSON object is not a record.
- `alert_health` returns **ERROR** for an unrecognised status, a missing record or an
  unreadable `asof`. An unrecognised health value is not health.
- **Never delivered is a BREACH**, not a grace period. An uncommissioned channel is
  actionable now, not after something has failed silently.
- Latest delivery STATUS and last-SUCCESS age are read together, so a success this morning
  followed by a failure this afternoon breaches now rather than in eight days.
- All writes are atomic (`os.replace`), so an interrupted run leaves the previous record
  rather than half of the new one.
- `observe()` is the independent observer's entry point. On OK it refreshes
  `logs/alert_watch_ok.json`; on anything else it leaves that file alone, so a fleet-level
  age row breaches both when the channel is unhealthy AND when the observer itself has
  stopped running.

In `scheduled_refresh.py`, every diagnostic call is wrapped in `_safe()`, which swallows
and logs. This is the belt to the writers' braces: `_email` and the run ledger are both
called from inside `fail()`, and anything that raises there replaces the refresh's actual
failure with a traceback about the instrument.

### 3D. `scripts/scheduled_refresh.py` — the wiring

- **Debt is evaluated BEFORE the green-run marker is consulted.** Evaluating it after —
  where it was — reproduces the 2026-09-13 shape exactly: a green run leaves the fill
  unpublished and the marker suppresses everything behind it.
- **Outstanding or unknown debt overrides the marker, once per local day**
  (`MAX_DEBT_RERUNS_PER_DAY = 1`). Bounded deliberately: a vendor mid-retraction is not
  cleared by retrying, and an unbounded override would re-run a one-to-four hour refresh
  every hour until the window closed. When the budget is spent the run exits 0 and the log
  and the run ledger name the obligation that is still outstanding.
- **`--catch-up` proceeds when something is owed OR unknown**, and exits in seconds
  otherwise. It now propagates to component children.
- **A failed push leaves the debt standing** by construction, because the evidence is
  `origin/main`. The failure message names the unpushed commit count.
- The post-run debt is recorded, and a green run that leaves its debt standing is called
  out in the log.

### 3E. Tests

| File | Cases | Covers |
|---|---|---|
| `tests/test_publication_debt.py` | 35 | Every reproduction above, against a REAL git repo with an origin: advancing targets, superseded-as-missed, unpushed commit, unrelated fresh panel, unresolvable remote, unreadable sleeve record, split venue dates, malformed/missing targets, corrupt ledger, atomic bounded ledger with eviction order, month boundary (30 Sep read 2 Oct), year boundary (29 Dec read 2 Jan, and a new-year publication discharging an old-year fill), escalation dedupe and cap, the subject contract with `scheduled_refresh` |
| `tests/test_price_revisions.py` | 38 | The preservation mask (both halves in one test), complete cycles identical and changed, zero cycles never read as append-only, unanswered run vs unanswered ticker vs explicit missing bar, unfinished-session exclusion, month and year boundary windows, `auto` resolution, policy flip, Norgate swap, unknown provenance withheld, adjustment vs revision, removed rows and columns, retention and eviction accounting, never raises |
| `tests/test_run_status.py` | 42 | Every malformed prior shape, unknown status as ERROR, never-delivered as BREACH, success-then-failure, stale last success, atomic writes, bounded ledger, no credential value in any record, the observer marker, CLI exit codes, and the integration: an exploding diagnostic does not replace the real failure |
| `tests/test_scheduled_refresh.py` | +9 | The wiring: catch-up with nothing owed / owed / unknown / raising, the marker still suppressing an hourly retry, debt overriding the marker once, the override bounded, the ordering pinned by source inspection, `--catch-up` propagation to children |

---

## 4. The revision question — still open, and why

**A withdrawal alone does not prove that restoration revises previously populated prices.**
It proves the vendor takes back what it served. Whether the value comes back changed is a
separate measurement, and it requires a COMPLETE cycle: the same cell served, withheld,
then served again, with the pre-withdrawal value retained across the gap. That is what the
state machine in 3B exists to capture and it has captured nothing yet.

The previous handoff said `withdrawals` "is the category that settles the review question".
That was wrong, and it is corrected here: withdrawals bound the question, cycles answer it.

**No statistical acceptance threshold is proposed for changing G1.** None is offered here,
and one should not be invented: "N clean cycles" is a number somebody chooses, not a
measurement. What the ledger can supply is a statement of what has been observed and what
has not, with its retention limits attached. The decision remains a judgement made on that
statement.

**G1 stays as it is, precautionarily.** Not because the guard has been shown correct, but
because the behaviour the relaxation would depend on is unmeasured. The proposal recorded
previously — hollow rows strictly after the required book date downgrade to WARN — would
newly admit one specific silently-wrong book: a panel mid-retraction whose EARLIER closes
are later revised, published on 14 September values, with a subsequent refresh restating
those values after the book is out. That is a published number changing after the fact
with no guard between, which is the class this repo has already shipped twice
(2026-08-08 stale-cache near-miss, 2026-08-09 thin-panel incident). Until a cycle has been
observed, the conservative reading is the only one the evidence supports.

---

## 5. Recovery status — NOT recovered

### 5.1 The blocker, measured

The 2026-09-15 European session is still unserved for essentially every name on the three
blocked panels. From the sched clone's own `tail_heal` sidecars, written 2026-09-16
05:29-05:36 UTC, intersected with the 2026-09-11 rosters:

```
EXH3  roster 107   requested 101   unserved 101   priced 6   row KEPT     -> G1 FAIL
EXH9  roster  28   requested  27   unserved  27   priced 1   row KEPT     -> G1 FAIL
EXV1  roster  57   requested  55   unserved  55   priced 2   row KEPT     -> G1 FAIL
EXH1  roster  26   priced 0   row DROPPED   (wholly empty rows are dropped; G1 passes)
EXV3  roster  34   priced 0   row DROPPED
```

Every missing name was requested and the vendor declined; nothing was unattempted. The
asymmetry is the structural finding worth keeping: a WHOLLY empty newest row is dropped by
`verify_price_tail` and G1 passes, while a PARTIALLY populated one is kept and G1 fails.
When a European session is partially published the two rules disagree, and the block
persists until the vendor completes the session. No number of retries clears it, which is
why widening the window is not the fix.

### 5.2 The probe was wrong, and how to generate a right one

The previous handoff's probe named `NESN.SW`, `ULVR.L` and `DGE.L` for EXH9, and `SAP.DE`
and `ASML.AS` for EXH3. Measured against the 2026-09-11 snapshots: **none of those five is
in the panel it was named for.** EXH9 is utilities (`IBE.MC`, `ENEL.MI`, `NG.L`, ...);
EXH3 is industrials and contains `SIE.DE` but not `SAP.DE`; `ASML.AS` is an EXV3
constituent. The EXV1 names were correct.

A probe is generated from the active roster and the unresolved tail record, never typed:

```python
sc  = json.load(open(f"data/prices_cache_{panel}.source.json"))
snaps = json.load(open(f"data/constituents_{panel}.json"))["snapshots"]
roster = set(snaps[max(snaps)]["tickers"])
row = next(r for r in sc["tail_heal"]["rows"] if r["date"] == required_session)
probe = [t for t in row["unserved"] if t in roster]
```

Correct probe heads, 2026-09-16: EXH3 `SIE.DE, SU.PA, RR.L, SAF.PA, AIR.PA`;
EXH9 `IBE.MC, ENEL.MI, NG.L, ENGI.PA, EOAN.DE`; EXV1 `HSBA.L, SAN.MC, BBVA.MC, UCG.MI,
BNP.PA`.

**A sample can demonstrate a remaining blocker; it cannot certify whole-panel
completeness.** Five names coming back served says the retraction may be clearing; it does
not say the panel is whole. Only the guard, over the full roster, says that. No vendor
probe was run in this pass.

### 5.3 The resolution criterion

`owed: false` alone is NOT the incident-resolution criterion. It says a publication
reached origin carrying sleeve rebalance records at or past the fill. Resolution requires
all of:

1. the required rebalance published through UNMODIFIED guards;
2. the obligations for `NYSE|2026-09-14` and `XETR|2026-09-14` recorded `discharged`, not
   `superseded` — a book that jumps to the following week leaves the fill permanently
   unrecorded;
3. a failure notification demonstrated from a REAL failure, not a diagnostic send;
4. the independent observer installed and returning OK from the sched clone.

Whether to reconstruct 14 September at all remains an owner call. A later refresh can
reconstruct a MODELLED rebalance for that fill; it cannot confirm broker fills, and the
two must never be conflated in any published record.

---

## 6. Unverified operational claims, stated as such

- **The previous handoff's "delivery proven" claim is NOT corroborated by the tree.** It
  reported a diagnostic email sent through `_email` with `logs/alert_delivery.json`
  recording `status: sent`. That file does not exist in either clone, and no log in the
  main repo mentions the send. The send may well have happened; the artefact that would
  evidence it does not exist. Treat the alert path as **unproven end to end**.
- What IS corroborated: `logs/cred_probe_under_task.txt`, written 2026-09-16, shows a
  probe running as a scheduled task under the same principal sees both credentials
  (`configured: true`). So the task-environment question is settled; the SMTP question is
  not.
- The observer says so, on both clones, today:
  `run_status.py --repo <clone>` -> `ERROR: no alert-delivery record: alerting is
  unproven`, exit 2. **"Never demonstrated delivery" is a commissioning failure, and it is
  the largest open item in this handoff.**
- No email of any kind was sent in this pass, per the instruction.
- The `logs/scheduler_rollback/*.xml` records for the two existing tasks exist and were
  not regenerated. `BreadthThrust-PostFillCatchup` is registered **Disabled** (verified
  2026-09-16).
- No vendor probe, refresh, publish or push was run in this pass.

---

## 7. Deployment sequence — in order, nothing skipped

Production activation stays GATED until step 6 passes.

1. **Push the commit** (section 8) after the owner approves. Nothing below works until the
   code is on `origin/main`.
2. **Verify the scheduled clone has it.**
   ```bash
   git -C C:/dev/breadth-thrust-etf-sched pull --rebase origin main
   git -C C:/dev/breadth-thrust-etf-sched log -1 --oneline
   python C:/dev/breadth-thrust-etf-sched/scripts/scheduled_refresh.py --help | grep catch-up
   ```
   The clone was at `31c8daf` when this was written. `--catch-up` must appear in the
   clone's own help output; the catch-up task will fail on an unrecognised argument
   otherwise.
3. **Read the debt from the clone, without writing anything.**
   ```bash
   python C:/dev/breadth-thrust-etf-sched/scripts/publication_debt.py --fetch --no-persist
   ```
   Exit 0 nothing owed, 1 owed, 2 escalation, 3 unknown. Three is not a pass.
4. **Let the first scheduled post-fill run happen on its own schedule** (Tue/Wed 09:00
   SGT, hourly for six hours). It writes `logs/alert_delivery.json` on its first alert
   attempt, whatever the outcome, and `logs/run_outcomes.jsonl` on any exit.
5. **Demonstrate a real delivery.** The first run's own notice — `[OK] ... pushed`,
   `[FAIL] ...` or the escalation — is the proof. Do not substitute another diagnostic
   send: the point is that the path the failures use works.
   ```bash
   python C:/dev/breadth-thrust-etf-sched/scripts/run_status.py --repo C:/dev/breadth-thrust-etf-sched
   ```
   must return `OK`.
6. **Commission the independent observer**, on its own schedule and in its own process.
   ```powershell
   $a = New-ScheduledTaskAction -Execute "python.exe" -Argument "scripts\run_status.py --repo C:\dev\breadth-thrust-etf-sched --observe --notify" -WorkingDirectory "C:\dev\breadth-thrust-etf-sched"
   $t = New-ScheduledTaskTrigger -Daily -At 09:00
   $t.Repetition = (New-ScheduledTaskTrigger -Once -At 09:00 -RepetitionInterval (New-TimeSpan -Hours 6) -RepetitionDuration (New-TimeSpan -Hours 24)).Repetition
   Register-ScheduledTask -TaskName "BreadthThrust-AlertWatch" -Action $a -Trigger $t -Description "Independent observer of the refresh alert channel"
   ```
   Six hours is chosen against the incident: three days of silence is what happened, and a
   six-hourly check bounds a repeat to a quarter of a day. Run it once by hand and confirm
   it writes `logs/alert_watch_ok.json` only on OK.
7. **Add the fleet_watch meta-guard**, after step 6 has produced the marker once. In
   `C:\dev\scripts\fleet_watch.json`, type `file`, path
   `C:/dev/breadth-thrust-etf-sched/logs/alert_watch_ok.json`, `max_age_hours` 18 (three
   observer cycles). Pointed at the OBSERVER's marker, not at `alert_ok.json`: the
   observer's marker goes stale both when the channel is unhealthy and when the observer
   is dead, which is the pair of failures a single age row otherwise cannot cover. Then
   `python C:\dev\scripts\fleet_watch.py --selftest`.
8. **Only then enable the catch-up task.**
   ```powershell
   Enable-ScheduledTask -TaskName "BreadthThrust-PostFillCatchup"
   ```
9. **After two weeks of runs, read the vendor evidence** — see section 10.

---

## 8. The commit

Reviewed implementation and tests, plus the two named documents. Explicit paths only: the
working tree carries STAGED holdings-monitor changes belonging to another process
(`data/holdings_monitor/...`, `data/holdings_monitor_latest.json`,
`docs/holdings-monitor*`) which must remain staged and uncommitted. `worktrees/` is
untracked and stays out.

```
scripts/publication_debt.py
scripts/price_revisions.py
scripts/run_status.py
scripts/scheduled_refresh.py
scripts/compute_breadth.py
tests/test_publication_debt.py
tests/test_price_revisions.py
tests/test_run_status.py
tests/test_scheduled_refresh.py
handoffs/post-fill-publication-debt-and-alerting.md
reviews/2026-09-16_g1-postfill-review-brief.md
```

## 9. Rollback

```bash
git -C C:/dev/breadth-thrust-etf revert <commit>
```

Before the commit exists, or to unwind the working tree:

```bash
git -C C:/dev/breadth-thrust-etf checkout -- scripts/compute_breadth.py scripts/scheduled_refresh.py tests/test_scheduled_refresh.py
rm C:/dev/breadth-thrust-etf/scripts/{price_revisions,publication_debt,run_status}.py
rm C:/dev/breadth-thrust-etf/tests/test_{price_revisions,publication_debt,run_status}.py
```

The new artefacts all live under `logs/`, which is gitignored and outside
`RESTORE_PATHS`; deleting them loses diagnostics and nothing else. To unwind the scheduler:

```bash
powershell -NoProfile -Command "Unregister-ScheduledTask -TaskName 'BreadthThrust-PostFillCatchup' -Confirm:\$false"
powershell -NoProfile -Command "Unregister-ScheduledTask -TaskName 'BreadthThrust-AlertWatch' -Confirm:\$false"
powershell -NoProfile -Command "Register-ScheduledTask -Xml (Get-Content 'C:\dev\breadth-thrust-etf\logs\scheduler_rollback\BreadthThrust-PostFillRefresh_2026-09-16.xml' -Raw) -TaskName 'BreadthThrust-PostFillRefresh' -Force"
```

## 10. Do not touch

- **G1, the coverage floors, the partial-row/whole-row asymmetry, forward-fill,
  `executed: false`.** All unchanged here. Section 4 before revisiting any of them.
- **The existing `BreadthThrust-PostFillRefresh` and `-WeeklyRefresh` tasks.** Unmodified;
  rollback XML above.
- **The existing `breadth-etf post-fill refresh` fleet_watch row.** It already covers the
  missed-publication case and was improved around, not duplicated.

## 11. Next prompt

> Work in `C:\dev\breadth-thrust-etf`. Read
> `handoffs/post-fill-publication-debt-and-alerting.md` and
> `reviews/2026-09-16_g1-postfill-review-brief.md`. Treat both as hypotheses and
> re-measure rather than trusting their tables.
>
> 1. **Re-measure the state.** Panel coverage from the sched clone's caches with the
>    repo's own definitions; the debt with
>    `python scripts/publication_debt.py --fetch --no-persist`; the alert channel with
>    `python scripts/run_status.py --repo C:/dev/breadth-thrust-etf-sched`. Report the
>    exit codes, not a summary of them.
> 2. **Read the vendor evidence.** `python scripts/price_revisions.py` — report
>    withdrawals, restorations, complete cycles and revisions PER PANEL, European panels
>    separately, together with how many runs are covered, how many cycles closed, what was
>    lost to retention and what was never observed because a panel was served from cache.
>    Until at least one complete served -> withheld -> restored cycle exists, report the
>    append-versus-revise question as open. Do not propose an acceptance threshold.
> 3. **Decide the G1 date-split on that evidence, or decline to.** Section 4 states the
>    dependency and the exact silently-wrong book the change would newly admit.
> 4. **Close the operational loop** against all four criteria in section 5.3, not against
>    `owed: false` alone. Commit with explicit paths so the staged holdings-monitor
>    changes are not swept in.
>
> Do not describe the incident as resolved until the rebalance has published through
> unmodified guards AND a failure notification has been demonstrated from a real failure.
