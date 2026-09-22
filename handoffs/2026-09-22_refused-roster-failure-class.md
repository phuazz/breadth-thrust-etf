# Handoff: refused rosters become their own failure class

**Working directory:** `C:\dev\breadth-thrust-etf\.claude\worktrees\reverent-dewdney-451de4`
**Branch:** `claude/reverent-dewdney-451de4` (worktree of `C:\dev\breadth-thrust-etf`)
**Review baseline:** `d9d72454`
**Model / effort:** Claude Opus 5, high effort, single interactive session
**Prepared:** 2026-09-22 (Tuesday), SGT
**Status:** COMPLETE. Committed, not merged, not pushed, not published.

---

## 1. Objective

Close the gap the brief at `reviews/2026-09-22_refused-roster-carry-forward-review-brief.md`
documents: a roster the fetcher REFUSED was written under the same cause label
as a public holiday, carried forward, and exited 0, so no operator surface said
anything. Implement the owner's accepted decisions — refusal becomes its own
named failure class and fails the fetch step; the vendor-gap class stays soft;
a state-based gate backs up the exit code; refused vendor responses are
retained.

## 2. Commits and changed files

| Commit | Subject |
| --- | --- |
| `d9d72454` | Review brief (baseline, prior session) |
| `e6c21f21` | A refused roster is its own failure class, and fails |

```
scripts/check_refresh_guard.py |  65 ++
scripts/fetch_constituents.py  | 451 ++++++++++++++++++---
scripts/refresh_all.py         |  73 ++++
scripts/scheduled_refresh.py   |  66 ++++
tests/test_roster_refusal.py   | 729 +++++++++++++++++++++++++++++++++++
5 files changed, 1354 insertions(+), 30 deletions(-)
```

No file under `data/`, `docs/` or `build/` was touched. Verified with
`git diff --name-only | grep -E "^(data|docs|build)/"` returning nothing.

## 3. Implemented behaviour

### 3.1 Classification (`scripts/fetch_constituents.py`)

`UnmappedExchangeError` now carries `symbol`, `as_of`, `exchanges`,
`affected_symbols`, `n_affected`, `n_equity_rows`, `share`, plus
`as_record(target_friday)`. Every consumer reads these attributes; nothing
parses `str(exc)`. `parse_holdings` gained an `as_of` parameter so the cached
CSV route carries the date too, which it previously dropped.

The walk classifies four outcomes where it previously had two:

| Status | Cause written | Soft? |
| --- | --- | --- |
| `not_found` | `no_data_in_walkback` | yes, exit 0 — unchanged |
| `endpoint_unavailable` | `endpoint_unavailable` | no, exit 3 — unchanged |
| `roster_refused` | `roster_refused` | no, exit 6 — NEW |
| `unexpected_error` | `unexpected_error` | no, exit 7 — NEW |

`EndpointDegraded` is re-raised explicitly before the blanket handler, so
`cli()`'s documented contract (it must unwind past every write) survives if it
is ever raised deeper than the top of the loop.

`roster_refusals` and `walk_errors` are top-level payload arrays, recorded
**independently of `carry_forwards`**. That independence is load-bearing:
three outcomes that follow a refusal write no carry-forward at all — no prior
snapshot (skipped), an EDGAR fallback (a real snapshot lands over it), and a
later successful Friday (the newest snapshot reads healthy).

`UnmappedExchangeError` is deliberately still not caught inside `get_snapshot`,
so a refusal aborts that Friday's walkback rather than stepping back a day. A
walkback that found a parseable older date would return an older roster under a
`walkback` label — a carry-forward wearing a capture's clothes.

### 3.2 Exit precedence

Stated once in `EXIT_PRECEDENCE` and applied once in the pure
`walk_exit_code()`:

```
3  EXIT_ENDPOINT_UNAVAILABLE    transport dead
6  EXIT_ROSTER_REFUSED          roster refused
7  EXIT_UNEXPECTED_WALK_ERROR   unclassified failure
2  EXIT_STALENESS_CRITICAL      roster aged past policy
0  EXIT_OK
```

`EXIT_ENDPOINT_DEGRADED` (5) sits outside the ladder; it unwinds as an
exception and is handled in `cli()` before anything is written.

Causes outrank symptoms, extending the rule the pre-existing "a dead endpoint
outranks stale data" comment already applied. **The guarantee the ladder must
not break:** the exit code names one class, but every class that fired still
prints its own stderr alert block and writes its own payload array. A refusal's
venues and symbols are recorded and printed whether or not the refusal wins the
code. Nothing may infer "no refusal" from an exit code that is not 6; that
question is answered by `roster_refusals`.

`"Staleness OK: last real fetch ..."` is suppressed when a refusal fired. On
2026-09-18 that sentence was true and entirely beside the point, and it is what
made the run read as healthy.

### 3.3 Raw-response preservation

`load_snapshot_tickers` parsed before it cached, so a refused payload was
discarded and every later run re-fetched, re-raised and threw it away again.
iShares serves a bounded history window, so a refused date eventually became
unrecoverable even after the venue was mapped — removing the remedy the whole
class depends on.

A refused payload is now retained at
`data/raw_ishares/<SYM>_<YYYYMMDD>.refused.json`, read back after both positive
caches and before the network, re-parsed under the current mapping, and
**promoted to the ordinary positive cache with the sidecar dropped** once it
resolves. Reconstruction therefore does not depend on the vendor window.

Two safety properties, both tested:

- Retention happens **only** in the `except UnmappedExchangeError` handler,
  which sits after the contract check, the `asOfDate` parity check and the
  column-length check. A payload reaching it is proven well-formed, non-empty
  and for the date requested, so a wrong-date, empty or malformed response
  cannot be laundered into a holdings cache by this path.
- `retain_refused_payload` writes only when no sidecar exists. The first
  capture is the one contemporaneous with the record made against it; a later
  different write would replace the evidence under an unchanged record.

Skipped under `refresh=True`, which means "revalidate against the endpoint";
answering it from a stored response would defeat it.

### 3.4 Secondary state-based gate

`check_refresh_guard.check_roster_refusals` is gate **G8** (pure,
unit-tested, alongside G2 and G3). `read_roster_refusals` tolerates the key's
absence — a pre-2026-09-22 payload has no array, and absence reads as clean,
which is precisely why the fetcher's exit code is the primary control.

`refresh_all._check_refusals_on_disk` imports and runs **the same function** as
a new step 1b, before step 2 and therefore before anything calculates on a
roster. It reads state, not exit codes, which is what survives
`--skip-soxx-fetch`, a cache-served re-run, or a manual invocation whose exit
code nobody kept. Scope is `select_panels(ETFS_ALL, args.component)` —
identical to the VERIFY step, so the 14 screening candidates stay outside it for
the reason given in that module's scope note. An unreadable payload is reported
as a failure, never read as clean. The step-7 VERIFY check is retained
unchanged.

### 3.5 Scheduled failure path

`scheduled_refresh.refusal_report()` renders every refusal on disk as text and
is called from `fail()` **before** `restore_tracked_outputs`, via the existing
`_safe` diagnostic wrapper so it can never replace the failure it reports on.
It writes into the retained run log and appends to the existing failure email
body. No second mail channel; the existing delivery ledger is untouched.

Rollback survival, verified rather than assumed:

- `data/constituents_*.json` is TRACKED, so `git checkout -- data/` destroys
  `roster_refusals`. Hence the copy into the log.
- `logs/` is gitignored, so the log survives.
- `data/raw_ishares/` is gitignored and `restore_tracked_outputs` runs
  `git clean -fd` **without** `-x`, so the retained responses survive. A test
  asserts both the ignore rule and the absence of `-x`, so the property cannot
  rot silently.

## 4. Tests and guards run

| Check | Result |
| --- | --- |
| `pytest tests/test_roster_refusal.py` (new, 36 tests) | 36 passed |
| `pytest tests/` (full suite) | **2960 passed, 4 skipped, 0 failed** (280s) |
| Directly affected modules (guard, API parity, scheduled refresh, suffix contract, throttle) | 286 passed, 2 skipped |
| `python scripts/check_refresh_guard.py` | 0 FAIL, 4 WARN — `OK G8 roster refusals: 24 panels carry no refused roster` |
| Conflict-marker check (commit hook) | OK, 852 tracked files |
| `python C:\dev\scripts\sync_claude_memory.py --gate` | no strict term in any tracked file |

The four guard WARNs are pre-existing and environmental: the gitignored price
caches and engine caches do not exist in this worktree (G1 price side, G7 B and
C), and W1's universal walkback reflects the committed 2026-09-17 rosters.

New tests cover every case the instruction listed: newest-Friday refusal with
persisted detail; historical refusal beside a later successful Friday; refusal
with no prior snapshot; EDGAR fallback neither erasing the refusal nor turning
the run green; ordinary vendor gap still soft and still exit 0; retention and
rebuild after a mapping repair **with the endpoint dead**; wrong-date, empty,
malformed and negative-cache protections intact; unexpected exception failing
rather than taking the vendor-gap label; the G8 gate wired into `main()` and
reused by `refresh_all` on the skipped-fetch path; scheduled failure retaining
evidence before rollback; and successful reconstruction clearing the state.

Date logic introduced is limited to `%Y%m%d` sidecar stamping and ISO
round-tripping, pinned at a month boundary (2026-01-30, 2026-02-27) and a year
boundary (2026-12-25, 2027-01-01).

One test guards the fixtures themselves: `ATHENS` was mapped on 2026-09-22
(`2b63dedb`), so parser-level tests use a venue asserted absent from both
`_EXCHANGE_TO_YF_SUFFIX` and `_EXCHANGE_ROUTE_UNAVAILABLE`. Without that
assertion those tests would fail open the moment the fixture venue were mapped.

## 5. Unresolved issues and boundaries

**Not done, by instruction — do not treat as oversights:**

- No staged-roster activation. The four Greek banks still do not reach the
  sleeve D panel; that is the staging gate and a separate decision.
- No strategy, threshold, membership-rule or registration change.
  `UNMAPPED_EXCHANGE_MAX_SHARE` (0.02) and `UNMAPPED_EXCHANGE_MIN_ROWS` (3) are
  unchanged and pinned by a test.
- No runtime suppression flag, and no new `_EXCHANGE_ROUTE_UNAVAILABLE`
  entries.
- No live refresh, no historical data repair, no publication. Nothing was
  pushed or merged.

**Open items for the owner:**

1. **Zero recorded refusals is not zero historical refusals.** Every committed
   payload predates the array, so `read_roster_refusals` returns `[]` for all
   24 panels today and G8 is green. That means "not recorded", not "none
   happened". The 134 existing carry-forwards (ICHN 76, IUCM 37, NDIA 20,
   EXH2 1) all carry `no_data_in_walkback`, and whether any of them was in fact
   a refusal cannot be determined from the payloads — it would need a re-walk.
   The first full refresh after this lands is what populates the array
   truthfully.
2. **The first post-merge refresh may fail on a genuine historical refusal**
   that has been carried silently. That is the intended behaviour, but it
   should be expected rather than treated as a regression, and the remedy is to
   map the venue the alert names.
3. **`unexpected_error` has a wider blast radius than the refusal class.** Any
   transient exception that previously degraded to a carry-forward now fails
   the run. That is what the instruction asked for and I believe it is right,
   but it is the change most likely to produce a surprise failure, and it has
   no production history behind it.
4. **The `refresh_all` step 1b gate excludes the 14 screening candidates**, to
   match `check_refresh_guard`'s scope note. A refusal on a candidate is caught
   by the fetcher's own exit code in step 1; it is NOT caught by the gate on a
   skipped or cache-served path. Whether that asymmetry is right is a judgement
   worth a second opinion.
5. **`refusal_report` scans `data/constituents_*.json` by glob**, so it reports
   candidates and deployed panels alike. That is deliberate for a diagnostic,
   but it is a different scope from the gate above.

## 6. Next review prompt, and notes for Codex

> Review commit `e6c21f21` on branch `claude/reverent-dewdney-451de4` of
> `breadth-thrust-etf` against the accepted decisions in
> `reviews/2026-09-22_refused-roster-carry-forward-review-brief.md` and this
> handoff. The change makes a refused roster its own failure class. Assess
> correctness and blast radius, not design — the design was already
> adjudicated. Report findings ranked by severity.

Specific things worth adversarial attention:

- **Exit precedence.** `walk_exit_code` is pure and exhaustively tested, but
  the claim that a lower-precedence class is never *hidden* rests on the alert
  blocks and payload arrays being written unconditionally before the return.
  Confirm there is no path that returns before them.
- **The retention safety argument.** The whole case that a wrong-date or
  malformed payload cannot be retained rests on
  `report_unmapped_exchanges` being the LAST statement in
  `parse_holdings_json`, after the parity and column checks. If a future
  refactor moves it earlier, the guarantee is silently void and no test would
  obviously catch it. Consider whether that ordering deserves its own
  assertion.
- **Sidecar promotion.** On re-parse success the retained payload is written to
  the positive cache and the sidecar unlinked. Check the failure modes: a
  partial write, a read-only directory, or two processes walking the same ETF
  concurrently. The refresh is single-instance by Task Scheduler, but the
  worktree model means two Claude sessions could run a fetch at once.
- **`unexpected_error` scope.** The blanket handler now fails the run. Is there
  a known-benign exception class that reaches it — a vendor client raising on a
  transient socket condition, say — that ought to be classified as transport
  rather than unclassified?
- **G8 absence semantics.** `read_roster_refusals` reads a missing array as
  clean. That is correct for backward compatibility but means the gate cannot
  distinguish "not recorded" from "none happened". Is the fetcher's exit code a
  sufficient primary control given that, or should a payload written by a
  post-2026-09-22 fetcher be required to carry the key?
- **Scope of the step 1b gate** — see open item 4 above.
