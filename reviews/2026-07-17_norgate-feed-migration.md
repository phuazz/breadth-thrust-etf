# Norgate breadth-feed migration — reconciliation study and staged proposal

**Date**: 2026-07-17 (Friday — weekday verified with Python `datetime`).
**Status**: REVIEW-AND-PROPOSE. Nothing deployed changes with this filing;
every stage below is behind its own approval.
**Scope**: the Phase 19 regime gate's S&P 500 breadth input only. Sleeve A
sector breadth and Sleeve D Stoxx breadth have no Norgate equivalent (UCITS
sector / Europe internals are not in the package) and keep the scrape
pipeline regardless; they are out of scope here.
**Artefacts**: reconciliation engine
`scripts/run_norgate_feed_reconciliation.py` → statistics
`data/norgate_feed_reconciliation.json` (derived stats and states only);
proposal publisher `scripts/publish_norgate_breadth.py` (NOT wired into any
pipeline); vendor panel git-ignored under `data_local/` (licence guard).

---

## 1. Why

The deployed gate input rides two third-party legs: weekly iShares CSP1
roster scrapes and yfinance constituent prices. Both have failed before
(the repo's own sentinel and freshness-headroom machinery exist because of
it), and the 2026-07 implementation audit's D3 staleness cap
(`GATE_MAX_STALE_DAYS = 10`, deployed) is the honest patch over that
fragility, not a cure. Norgate Platinum (subscribed 2026-07-04,
personal-use licence) ships `#SPX%MA50` — "% Stocks above MA50" — the same
measure computed by the vendor from official point-in-time membership,
updated by the local NDU service that three other vault projects already
run against.

## 2. Method

`data/breadth_csp1.json → series.ma_breadth` (deployed) inner-joined with
`#SPX%MA50`/100 (candidate) on raw dates, NO forward-fill. Both series then
drive the DEPLOYED hysteresis — `_compute_states`, OFF 0.20 / ON 0.50,
**imported from `run_risk_overlay`**, not re-implemented (the run records
`state_machine_source`). Licence guard: vendor values only ever written to
git-ignored `data_local/`; the committed JSON carries statistics, dates and
derived 0/1 states.

Three silent-failure modes and defences are stated in the engine docstring:
basis mismatch read as data error (bias reported separately from noise;
neither series tuned toward the other); hysteresis drift (deployed import);
overlap illusion (raw-date join; per-source missing-day counts).

## 3. Findings

| Reconciliation, 2018-01-05 → 2026-07-10 (2,138 joint days) | Value |
|---|---:|
| Level correlation | **0.9986** |
| Median signed diff (Norgate − deployed) | **−1.24 pp** |
| IQR / p95 / max abs diff | 1.6 / 3.37 / 5.79 pp |
| Max-diff date | 2018-03-19 (early-panel era) |
| Gate-state agreement | **98.60 %** |
| Regime flips, deployed vs Norgate | **24 vs 24 — all 24 paired, none unmatched** |
| Same-day paired flips / within 1 day | **17 of 24** / 20 of 24 |
| Flip-timing outliers | +3, +11, +21 calendar days (all ON-side) |
| Threshold-zone side disagreements, OFF 0.20 / ON 0.50 | **8 days / 60 days** |
| Candidate series depth | **1957-03-04 →** (deployed panel: 2018-01-05 →) |

**Reading.** The candidate is feed-equivalent for gate purposes. The small
systematic bias (Norgate ~1.2 pp lower; definitional — official membership
vs scraped roster, vendor price basis vs yfinance adjusted closes) never
altered flip structure across 8.5 years: every de-risk and every re-entry
exists on both feeds. The asymmetry favours migration: at the 0.20
protection line the sides disagree on 8 days in 8.5 years and every OFF
flip pairs at ≤1 day (one −7d case where Norgate de-risked EARLIER); the
lag cases are slow crawls across the 0.50 re-entry line, where a few days'
delay costs basis points of recovery, not protection. The 1957 depth is a
free research option (six decades of gate-input history for a future
robustness study — ledger-gated, not exercised here).

## 4. Proposal — staged, each stage behind approval

**Design principle (licence)**: the raw vendor series never enters this
public repo. The handover artefact is a **derived gate-states file** —
the publisher runs the deployed `_compute_states` locally over the FULL
vendor history (hysteresis with complete context) and writes only
`{dates, state 0/1, provenance, freshness}`. Derived states are exactly
what the public dashboard already displays; series values are not.

- **Stage 0 — filed now**: this study + the publisher script existing
  unwired. No behaviour change (this commit).
- **Stage 1 — parallel-run (approval #1)**: a local Task Scheduler job
  (pattern: the sentiment-composite daily task) runs
  `publish_norgate_breadth.py` each trading morning after NDU's US close
  update (~07:15 SGT), writing the states preview into `data_local/` and
  a one-line divergence check vs the scrape feed (state mismatch or level
  diff > 5 pp → flag) appended to the existing freshness sentinel. Soak
  2–4 weeks. CI and consumers untouched.
- **Stage 2 — swap behind the existing cap (approval #2)**: the publisher
  starts committing `data/gate_states_norgate.json` (derived states only),
  and `run_risk_overlay` prefers it when fresh: states consumed directly
  (bypassing `_compute_states` on the scrape series), falling back to the
  scrape path exactly as today whenever the states file is stale past
  `GATE_MAX_STALE_DAYS`. Machine off → file goes stale → automatic
  fallback → sentinel flags. Patch sketch (not applied):
  at [run_risk_overlay.py:321](scripts/run_risk_overlay.py:321) load
  `gate_states_norgate.json` if present and fresh → use its states and
  record `gate_feed: "norgate-local"` in the provenance block
  ([run_risk_overlay.py:594](scripts/run_risk_overlay.py:594) already
  carries feed provenance); else current behaviour verbatim,
  `gate_feed: "csp1-scrape"`.
- **Stage 3 — steady state (approval #3, earliest at the December 2026
  renewal decision)**: scrape retained indefinitely as the automatic
  fallback (it is already built and free); decision then is only whether
  the WEEKLY scrape cadence can drop. Recommendation: keep both feeds —
  belt and braces on a load-bearing gate.

**Rollback at any stage** = delete (or stop refreshing) the states file;
the loader's ordering makes the scrape path resume without a code change.

## 5. Ops runbook (Stage 1/2 job)

- Schedule: Tue–Sat 07:15 SGT (after NDU's US-EOD update lands; NDU
  auto-starts at boot). Task pattern copied from the sentiment-composite
  scheduled tasks.
- Freshness check INSIDE the publisher: read the actual last BAR date of
  `#SPX%MA50` — never `last_quoted_date`, which NDU leaves unset on
  market-closed days (lesson filed in the event-studies feed-gate
  incident, 2026-07-04). Holiday-aware: no alert when the US market was
  simply closed.
- Failure ladder: NDU down or stale → publisher exits without writing →
  states file ages → (Stage 2) loader falls back to scrape + sentinel
  email; the deployed 10-day cap and hold-state degradation remain the
  last line, unchanged.
- Package hygiene: `norgatedata` 1.0.74 → 1.0.77 upgrade available;
  apply at the first maintenance window, never silently mid-cycle.
  Entitlement changes require an NDU restart (2026-07-04 lesson).

## 6. Out of scope, stated

Sleeve A (UCITS sector breadth) and Sleeve D (Stoxx) stay on the scrape —
no vendor equivalent exists in the package. A separate Tier-2 #4 audit
covers breadth-thrust-signal / market-regime-dashboard gauge upgrades
(`#SPXZWBT`, McClellan, NH−NL families all confirmed available). The
tilt's EEM/SPY feed already carries its own deployed staleness cap (D4)
and is untouched here.

## 7. Approval asks

1. Stage 1 go/no-go (local parallel-run job; zero deployed impact).
2. Stage 2 go/no-go after the soak (the actual swap; patch to be
   presented as a concrete diff at that point).
3. Note for the ledger: standing follow-up (1) — the EEM/SPY staleness
   cap — is DEPLOYED (D3/D4 caps live in `run_risk_overlay.py`); the
   follow-ups block should be amended accordingly.

## 8. Addendum — approvals and Stage 1 LIVE (2026-07-17)

ZH approved both asks same day ("both ok", in session). **Stage 1 is
LIVE**: scheduled task `breadth-thrust norgate feed parallel-run`,
Tue–Sat 07:15 SGT (first fire 2026-07-18, capturing Friday's US close),
wrapper `scripts/run_norgate_publisher.bat`, all output to git-ignored
`data_local/` (`publisher.log` + `gate_states_norgate.preview.json`).
Validated with two manual runs: divergence check ok, both feeds RISK_ON
on the 2026-07-16 bar. One delta from §4 as filed: the Stage-1
divergence flag surfaces via the local log and the soak review, NOT via
the CI freshness sentinel — sentinel integration belongs to Stage 2,
keeping Stage 1 strictly zero-deployed-impact. **Stage 2 is approved in
principle**; execution waits on the soak review — due **Friday
2026-08-07** (weekday verified) — where the concrete loader diff will be
presented against the soak log before anything deploys.

## 9. Addendum — pre-soak hardening and prepared Stage-2 diff (2026-07-17 PM)

A soak-review prompt fired prematurely the same afternoon (soak log at
that point: only the §8 validation runs). Per the §8 gate the Stage-2
diff was NOT put up for approval; instead the remaining preparable work
was front-loaded. All items below are Stage-1-scope or unapplied:

1. **Licence-guard defect fixed.** `.gitignore`'s `data_local/` entry
   carried a trailing comment on the same line; gitignore does not
   support trailing comments, so **`data_local/` (raw vendor panel
   included) was not ignored at all** — one `git add .` from a licence
   breach. Pattern moved to its own line; `git check-ignore` verified.
2. **False-FLAG bug fixed pre-soak.** The publisher labelled the off
   state `"DERISK"` while the deployed file says `"RISK_OFF"`, and the
   divergence check used a substring test — every both-feeds-OFF day
   would have printed a spurious FLAG into the soak log. Labels pinned
   to deployed naming (`STATE_LABELS`), exact-equality compare, guard
   tests in `tests/test_norgate_publisher_labels.py` (commit 76e75ff).
3. **`norgatedata` 1.0.74 → 1.0.77** upgraded before the first scheduled
   fire (the never-mid-cycle window §5 reserved), so the soak runs on
   the configuration Stage 2 would keep.
4. **Scheduler-path smoke tests.** The Task Scheduler trigger itself
   (never exercised by the §8 manual runs) was fired on demand twice —
   pre- and post-upgrade — exit 0, clean divergence line both times.
   The four 2026-07-17 log entries (2 manual, 2 on-demand smoke) are
   **not soak evidence**; the soak window is the ~15 scheduled Tue–Sat
   fires 2026-07-18 → 2026-08-07.
5. **Missed-run semantics.** The task had `StartWhenAvailable=False`
   (machine off/asleep at 07:15 = silent skip, which doubles as
   fallback rehearsal); flipped to True on approval so late boots still
   capture the bar — the publisher is idempotent per the repeated
   same-day runs.
6. **Stage-2 diff prepared, tested, UNAPPLIED.** Branch
   `norgate-stage2-loader` (local), filed as
   `reviews/2026-07-17_norgate-stage2-loader.patch` (applies cleanly to
   main): `run_risk_overlay` consumes `data/gate_states_norgate.json`
   when fresh under the deployed `GATE_MAX_STALE_DAYS`, scrape path
   verbatim otherwise; `gate_feed` / `gate_feed_last_bar` provenance in
   payload and console; publisher `--push` (single-file add, fail-soft)
   for Stage-2 publication. Ten loader guard tests incl. the 10-day cap
   at a month boundary and a year boundary, and an equivalence pin that
   ffilled published states equal the scrape path's NaN-hold
   degradation. Full suite in the worktree: **265 passed**.
7. **Dry-run findings the 2026-08-07 review should expect.** Driving
   the patched pipeline with the real preview file: `gate_feed`
   flips to `norgate-local`, states run **six days fresher** than the
   weekly scrape panel (2026-07-16 vs 2026-07-10), current state
   agrees (RISK_ON), and the historical-revision detector surfaces a
   **one-time ~10-entry event diff** at cutover — the §3 flip-date
   shifts (+1/+3/+11d ON-side, the −7d earlier OFF in 2026-03) plus one
   start-context artefact (an added 2018-11-28 ON event: full-1957
   hysteresis context resolves the 2018-Q4 episode differently from the
   2018-panel-start-ON assumption). Expected, explained, and surfaced
   by design; `current_state_since` moves 2026-04-13 → 2026-04-14
   (+1d). Stage-2 activation after approval = apply the patch + add
   `--commit-path --push` to the wrapper.

## 10. Soak review — 2026-08-07 (Friday, weekday verified)

**Verdict: CLEAN SOAK.** Zero state disagreements, zero misses, zero
divergence flags, no mid-soak package change. Per the §8 gate the
Stage-2 loader diff is now **presented for approval** (§10.5). It has
not been applied.

### 10.1 Run census

`data_local/publisher.log` carries **19** `---- ` headers. Four are the
2026-07-17 pre-soak entries (two manual validations, two on-demand
scheduler smoke tests) and are excluded per §9 item 4. **Soak runs = 15.**

Expected fires, enumerated with Python `datetime` (Tue–Sat inclusive,
2026-07-18 → 2026-08-07, no NYSE holidays in the window): **15**. The
observed set equals the expected set exactly.

| Check | Result |
|---|---|
| Scheduled fires expected / observed | **15 / 15** |
| Misses (gaps in the Tue–Sat sequence) | **0** |
| Divergence lines not ending `-> ok` | **0 of 19** |
| `zone=True` days (threshold zone) | **0 of 19** |
| State pairs observed | 19 × `deployed=RISK_ON norgate=RISK_ON` |
| `norgatedata` init on soak runs | **15 / 15 on v1.0.77** |

Task Scheduler corroborates: task `breadth-thrust norgate feed
parallel-run` — State Ready, Last Result **0**, `NumberOfMissedRuns` **0**,
last run Fri 2026-08-07 09:55 SGT, next Sat 2026-08-08 07:15,
`DaysOfWeek = 124` (Tue+Wed+Thu+Fri+Sat), `StartWhenAvailable = True`.

The three `v1.0.74` inits in the log are the pre-upgrade 2026-07-17
entries; the upgrade landed before the first scheduled fire, as §9 item 3
records. No version change occurred inside the soak window.

### 10.2 Deviations, all explained

1. **Six late fires** — 2026-07-21 15:52, 2026-07-22 10:58, 2026-07-30
   09:02, 2026-08-04 10:56, 2026-08-06 15:03 and 2026-08-07 09:55 SGT ran
   after the 07:15 trigger. These are `StartWhenAvailable=True` catch-ups
   on a machine off or asleep at trigger time (§9 item 5). All six
   completed, wrote, and produced the correct prior-session bar. Under
   the pre-soak `False` setting each would have been a silent skip.
2. **One bar lag** — the Fri 2026-07-24 07:15 SGT run (Thu 2026-07-23
   23:15 UTC) wrote `last bar 2026-07-22` rather than 2026-07-23. The
   2026-07-23 bar is present in the vendor series today, so NDU had not
   yet loaded Thursday's US close at that moment. Calendar gap 2 days
   against `STALE_TRADING_DAYS = 3`: inside tolerance, so the publisher
   correctly wrote rather than exiting. Every other run captured the
   immediately prior session. Maximum bar age across the soak: **1
   trading day.**
3. **Two pypi-reachability warnings** — 2026-07-30 and 2026-08-05 logged
   "Unable to obtain version data of norgatedata from pypi". That is the
   package's own update check failing on a network reach, not the data
   path; both runs initialised v1.0.77 and wrote the correct bar.

### 10.3 What the soak does and does not evidence

Stated plainly, because a run of 19 identical `-> ok` lines invites
over-reading. Deployed `ma_breadth` sat at roughly **0.60–0.72** through
the window — far above the 0.50 re-entry line and nowhere near the 0.20
protection line. Hence `zone=False` every day and a constant RISK_ON on
both feeds.

- **Evidenced**: the scheduled job fires reliably, survives late boots,
  runs on the pinned package, respects the licence guard (all output in
  git-ignored `data_local/`), and agrees with the deployed feed in a calm
  regime.
- **Not evidenced**: agreement at a regime flip or inside the threshold
  zone. The soak contains **n = 0 flips** and **n = 0 zone days**. That
  claim rests entirely on §3's 2,138-day reconciliation (24/24 paired
  flips, 8 zone-disagree days at 0.20, 60 at 0.50) — not on the soak.
- **Not rehearsed**: zero misses means the Stage-2 staleness fallback
  never fired in production. It is covered by the patch's ten loader
  guard tests (10-day cap at a month boundary and a year boundary) and,
  benignly, by the 2026-07-24 lag which exercised the freshness read.
  Recorded as a residual, not as a pass.

One control worth stating: the divergence check compares against a live
deployed side, not a frozen file. `data/risk_overlay.json` was refreshed
eight times inside the window (2026-07-18, 07-24, 07-27 ×2, 07-30, 08-01,
08-02), so the constant "ok" is not an artefact of a stale comparand.

### 10.4 Freshness at review

Vendor preview last bar **2026-08-06**; deployed `panel_end_date`
**2026-07-31**. The vendor feed runs **six days fresher**, unchanged from
the July dry run (§9 item 7). Both current states RISK_ON.

### 10.5 Stage-2 diff — presented for approval, NOT applied

`reviews/2026-07-17_norgate-stage2-loader.patch` **re-verified today**:
`git apply --check` passes against current main (`9d9882d`). Branch
`norgate-stage2-loader` present locally at `16f3682`. No rebase needed.

Diff: 3 files, +305 / −1 — `scripts/run_risk_overlay.py` (+83),
`scripts/publish_norgate_breadth.py` (+50),
`tests/test_norgate_gate_states.py` (+173, new). Tested 2026-07-17: 10
loader guard tests; full suite **265 passed** in a worktree; dry run
against the real preview file.

**Expected cutover effects, restated from §9 item 7:**

- `gate_feed` flips `csp1-scrape` → **`norgate-local`**.
- A one-time historical-revision surfacing of **~10 event-date shifts**:
  the §3 flip-date differences (+1 / +3 / +11 days, all ON-side crawls;
  the −7d earlier OFF in 2026-03) plus **an added 2018-11-28 RISK_ON
  event** — full-1957 hysteresis context resolves the 2018-Q4 episode
  differently from the 2018-panel-start-ON assumption. Expected, by
  design, and surfaced rather than silent.
- `current_state_since` moves **+1 day** (2026-04-13 → 2026-04-14 as of
  the July dry run).

**Activation, only on ZH's explicit written approval**: apply the patch,
run the full test suite, then add `--commit-path --push` to
`scripts/run_norgate_publisher.bat`.

*Dates in this addendum — 2026-08-07 Friday, 2026-07-17 Friday,
2026-07-18 Saturday — verified with Python `datetime`. Flagged for owner
confirmation per house rules.*
