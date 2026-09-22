# Review brief: a refused roster carries forward silently

**Repo:** `breadth-thrust-etf` (worktree `reverent-dewdney-451de4`, branch off `main` at `cb3321bb`)
**Prepared:** 2026-09-22 (Tuesday), SGT
**Prepared by:** Claude Code session, from source read on this machine plus the committed payloads
**Status:** open. Nothing has been implemented. One sub-decision is already made by the owner and is marked as such.

Four questions for review are at the end. Everything before them is evidence.
Line references are to the tree named above.

---

## 1. What happened

STOXX moved Greece from EM to DM and four Greek banks entered SX7P. On the
2026-09-18 (Friday) roster, EXV1 — 7.76% of NAV, sleeve D's largest line —
carried ALPHA, ETE, EUROB and TPEIR on the venue string
`"Athens Exchange S.A. Cash Market"`, which `_EXCHANGE_TO_YF_SUFFIX` did not
hold. `report_unmapped_exchanges` refused the roster. The fetcher fell back to
the 2026-09-11 (Friday) snapshot of 57 names, wrote the file, printed
`Staleness OK: last real fetch ... under 14-day warning threshold`, and the
refresh step reported `[OK]`. Sleeve D ranked the fill week on the carried
roster.

The only alert anyone received came from `measure_publication_lag.py`, which
parses the same payload, had no carry-forward to fall back to, and died. That
probe has since been made non-strict, so the accidental signal is gone. The
`KNOWN RESIDUAL GAP` comment at `scripts/measure_publication_lag.py:242-252`
records the state deliberately and defers the fix, which is what this brief
takes up.

The venue has since been mapped (commit `2b63dedb`). Measured on the committed
payload today: EXV1's newest snapshot is target 2026-09-18, actual 2026-09-17
(Thursday, a normal walkback), 57 tickers, a real fetch rather than a
carry-forward, and zero tickers carrying a `.AT` suffix. The immediate incident
is closed. Whether the four banks should by now be present in the D panel is a
separate matter (the staged-roster gate) and is **not established here**; it is
outside this brief.

`UNMAPPED_EXCHANGE_MAX_SHARE` (2%) is correct and is not in question. Nothing
proposed below loosens it.

## 2. The code path, exactly

`report_unmapped_exchanges` runs at the *end* of parsing and raises
(`scripts/fetch_constituents.py:799-807`), from both parse routes
(`:409` for the JSON API, `:1005` for the cached CSV).

`get_snapshot` catches only `EndpointUnavailable` and `PayloadContractError`
(`:1171`), so `UnmappedExchangeError` propagates past it into the walk's
blanket handler:

```python
        except Exception as e:
            print(f"  ERROR on {friday}: {e}", flush=True)
            tickers, actual, status = None, None, "not_found"
```
`scripts/fetch_constituents.py:1295-1297`

Two blocks later, `status == "not_found"` becomes:

```python
            cause = "endpoint_unavailable" if outage else "no_data_in_walkback"
```
`scripts/fetch_constituents.py:1340`

So a refused roster is written into the payload under the same cause label as a
public holiday. The walk continues, the staleness block is computed from the
carried snapshot, and `main()` returns `EXIT_OK`.

The module's own docstring (`:54-60`) states a three-class failure taxonomy —
carry-forward (exit 0), `endpoint_unavailable` (exit 3), staleness critical
(exit 2) — with `EXIT_ENDPOINT_DEGRADED = 5` added later on the stated
principle that a class earns its own code when the operator's action differs.
A refusal is a fourth class and is not in the taxonomy. Exit codes 1, 4 and 6
are unused by this module.

## 3. What is recorded, and what is not

The conflation is present in the committed data, not only in the code. Counted
across all `data/constituents_*.json` today:

```
total carry-forwards across all panels: 134
by cause: {'no_data_in_walkback': 134}
by panel: {'ichn': 76, 'iucm': 37, 'ndia': 20, 'exh2': 1}
```

Every one is labelled the same way. A genuine vendor gap and a refusal are
indistinguishable on disk, which is why no consumer can tell them apart. Three
consumers read the carry-forward and none can read the reason:

- `scripts/check_refresh_guard.py:861-888` reads `end_friday`,
  `endpoint_health`, `staleness`, the newest snapshot's `actual_date`, and the
  price side. It never reads `carry_forwards`.
- `scripts/capture_status.py:45` writes `"roster_carried": bool(...)` into every
  `breadth_*.json` — a boolean with no cause.
- `scripts/build_data_audit.py:462` counts `carried_forward_from` for display.

## 4. Why no VERIFY step catches it

The gates in `scripts/refresh_all.py:555-569`, checked against the 2026-09-18
state:

- **G2 endpoint health** (`check_refresh_guard.py:371`) — the transport was
  healthy. Passes.
- **G3 roster staleness** (`:381`) — last real fetch 2026-09-11 against
  `end_friday` 2026-09-18 is 7 days, inside the 14-day warn band, so `fresh`.
  Passes.
- **G1 shared end_friday** — the payload still stamps `end_friday` 2026-09-18.
  Passes.
- **G5 no lost state** — the carried snapshot is a snapshot; no key
  disappeared. Passes.
- **G6 roster coverage** — the 57 carried names all price. Passes.
- **W1 universal walkback** (`:618-667`) — warns only when *every* deployed
  panel walked back. One panel did. Passes.
- `check_roster_integrity.py` — covers `endpoint_unavailable` holes only.

No gate reads `carry_forwards[].cause`, so no gate can see this. That is the
answer to "does an existing VERIFY step already cover it": none does.

`check_roster_integrity.py` is nevertheless the precedent, and its opening
argument transfers word for word:

> Nothing consumed it. Grepping refresh_all, check_capture_integrity, pipeline
> and scheduled_refresh for `endpoint_unavailable` returned one hit, in
> build_data_audit, which counts it for display. So the array was written and
> never acted on, and this failure was available.

The same sentence is now true of the carry-forward cause.

## 5. The alerting channel that already exists

A non-zero exit from `fetch_constituents` is already wired to an operator email,
through machinery that is built and in service:

- `scripts/refresh_all.py:325-329` — a failed fetch appends to `failures` and
  `continue`s, so that panel's `compute_breadth` does not run;
- `scripts/refresh_all.py:359-361` — after step 1, any failure returns 1 with
  `"Capture failed; downstream calculations were not started"`. No engine,
  blend, diagnostic or page build runs;
- `scripts/scheduled_refresh.py:1104-1108` — a non-zero `refresh_all` becomes
  `fail(3)`, which emails `[FAIL] Scheduled refresh`, restores tracked outputs,
  writes the run ledger, and commits and pushes nothing.

No CI workflow invokes `fetch_constituents.py`; the callers are `refresh_all.py`
step 1 and manual operator runs. The blast radius of a new exit code is
therefore confined to those two.

By contrast, emitting an alert *without* failing would mean building a second
mail path inside `fetch_constituents`, which has none: duplicating
`scheduled_refresh._email`, its credential handling, and its
`run_status.record_alert` delivery ledger. That ledger exists because five
consecutive failures in September each wrote `[email skipped]` into a log
nobody read (`scripts/scheduled_refresh.py:344-348`). A new unattended
alerting channel needs its own guard layer under the house rule.

## 6. A second defect on the same path — DECIDED, not under review

`load_snapshot_tickers` parses before it caches:

```python
    tickers = parse_holdings_json(
        payload, target, ticker_overrides=overrides,
        apply_exchange_suffix=apply_suffix, symbol=symbol,
        exclude_symbols=excluded,
    )
    if tickers:
        json_path.write_text(json.dumps(payload), encoding="utf-8")
```
`scripts/fetch_constituents.py:1117-1123`

When the parse raises, the vendor payload is discarded. Every subsequent run
re-fetches the date, re-raises, and throws the response away again. iShares
serves a bounded history window, so once that window closes the data is gone and
the hole is unrepairable even after the venue is mapped — which removes the
remedy the rest of this brief depends on. The 2026-09-18 roster was recovered on
2026-09-22 only because the window was still open.

Whether we can map a venue is our problem, not a reason to discard the issuer's
response. **The owner has already decided to fold the fix in** (cache the
payload before parsing, with its own test). It is recorded here for completeness
and is not one of the questions below.

**Not verified:** `data/raw_ishares/` is gitignored and absent from this
worktree, so the cache state at the time of the incident could not be inspected
directly. The claim rests on the code path above, which is unambiguous.

---

## Questions for review

### Q1 — Should a refusal-caused carry-forward fail the fetch step?

The three candidates, and the case each way.

**(a) A refusal becomes its own failure class and fails the step.** New exit
code; `cause="roster_refused"` plus the venue strings and affected tickers
recorded in the payload; vendor-gap carry-forward stays soft and unchanged. The
refresh stops at step 1 before any engine runs, and the existing email fires
with no new channel.

The case for: the remedies differ in kind. **A vendor gap heals on its own; a
refusal never does.** Carry-forward is the right response to a transient event
that will resolve itself, which is exactly why `no_data_in_walkback` should stay
soft. A venue string will not map itself, so every further day of carry-forward
drifts the roster with zero probability of self-repair, and the only backstop is
a 14-day staleness warning — by which point the roster has been wrong through
two or three rebalances. The error costs are also asymmetric in the same
direction: a wrong FAIL costs one skipped refresh and one email, with the
committed book intact and the catch-up machinery already built for it, while a
wrong pass costs a live fill ranked on a roster missing 7.76% of NAV for four
days, silently.

The case against, which should be weighed rather than waved through: this is a
production behaviour change that would have failed the 2026-09-18 refresh
outright. A refusal is bounded at 2% of a roster by construction, so there is a
reading in which a 2.1% refusal blocking the entire weekly publication is
disproportionate — the guard's own calibration note (`:752-759`) says genuine
one-offs sit at or below 0.3%, but it was calibrated on history, not on venue
renames. If the reviewer believes benign refusals at the margin are more likely
than that calibration implies, the balance shifts towards (b).

**(b) Keep the step soft and emit a distinct operator alert.** Records the cause
and warns loudly, but the stale roster still publishes and still ranks the fill.
Requires the second mail path described in section 5, and a guard layer over
that channel under the house rule. The case for it is that publication continues
while a human decides — which is also precisely the failure mode of 2026-09-18,
where a loud log body reached nobody.

**(c) Tighten the staleness threshold only for refusal-caused carry-forwards.**
Reaching an email requires crossing `critical`, which means running the full
sequence — engines, blend, diagnostics, page build — before failing at step 7,
rather than stopping at step 1. It also describes the event wrongly (the roster
is not stale; it was published, fetched, read and refused) and collides with the
per-ETF overrides, since SOXX runs 60/120 rather than 14/30.

The session's own recommendation is **(a)**. The reviewer is asked to adjudicate
rather than ratify, and the disproportionality argument above is the strongest
thing that can be said against it.

### Q2 — If (a): should a refusal on a historical Friday fail, or only on the newest one?

The walk covers every Friday from `start_friday` to `end_friday` on every run,
re-parsing cached responses, so a historical date can refuse too.

**Any refusal fails.** The objection is `check_refresh_guard`'s own rule that a
guard firing on every run is worse than none (`:50-54`). The reply is that a
refusal has two permanent registered remedies — map the venue, or declare it
routeless in `_EXCHANGE_ROUTE_UNAVAILABLE` (`:685-691`) — so there is no
legitimate steady state in which it keeps firing. The measured base rate of
refusals in the committed payloads is zero.

**Newest Friday only.** Narrower, but it carries a property worth naming: the
alarm silences itself by ageing. A refusal that is fatal this week becomes a
permanently mis-built historical snapshot next week, once a newer Friday exists.
In the EXV1 case the refusal would have recurred on each subsequent newest
Friday, because an index change persists — so the narrow rule would have held
there. It would not hold for a venue that appears for one Friday and vanishes.

### Q3 — If (a): does the durable gate belong in `check_refresh_guard.py`?

The proposal is that the fetcher exit is primary, and a gate reading
`carry_forwards[].cause` is secondary, because the exit code is the thing that
gets lost — a manual run, `--skip-soxx-fetch`, or a re-run from cache can leave
a refused snapshot on disk with nothing having failed. The gate makes the state
on disk self-describing.

`check_refresh_guard.py` already loads every panel's constituents JSON in the
loop at `:850-888`, already carries the FAIL/WARN/OK vocabulary, already hosts
the two sibling classes (G2 transport, G3 age), already runs as a `refresh_all`
VERIFY step, and keeps verdict logic pure and unit-tested offline. The cost is
one field read and one function.

The alternative reading is that `check_roster_integrity.py` is the closer
sibling — it is the script written for exactly this shape of "written and never
consumed" defect, and it compares against the committed baseline rather than
judging a payload in isolation. Note it runs from `scheduled_refresh.py:1124`
only, so it does **not** cover a manual `refresh_all` run; `check_refresh_guard`
does. That asymmetry is itself worth a view.

### Q4 — Should there be a run-time escape hatch?

`--carry-forward-on-outage` is the precedent for an explicit operator override,
though it still exits non-zero and so does not unblock a refresh.

The session's position is **no new flag**. `_EXCHANGE_ROUTE_UNAVAILABLE` is the
escape hatch and is the right one, because it is a named, reviewed, permanent
declaration rather than a run-time suppression, and because its existing entry
(BSE) carries a documented probe justifying it. It is deliberately not a quick
mute: a routeless venue keeps its raw local code and its names still drop out of
breadth, so declaring a 7.76%-of-NAV venue routeless would defeat
`UNMAPPED_EXCHANGE_MAX_SHARE` rather than remedy it. That friction is the point.

The counter-consideration: if a venue needs research before its yfinance suffix
can be verified, the weekly publication is blocked in the meantime. The reviewer
should say whether that is acceptable, and if not, whether the right relief is a
flag or a narrower default.

## Relevant files

- `scripts/fetch_constituents.py` — `report_unmapped_exchanges` `:762-807`;
  `_EXCHANGE_ROUTE_UNAVAILABLE` `:685-691`; `load_snapshot_tickers` parse/cache
  order `:1117-1123`; `get_snapshot` catch set `:1171`; the walk's blanket
  handler `:1295-1297`; cause assignment `:1340`; carry-forward record
  `:1355-1368`; staleness block `:1393-1418`; exit codes `:148-160`
- `scripts/check_refresh_guard.py` — gate docs `:18-116`; `check_endpoint_health`
  `:371`; `check_staleness` `:381`; `check_universal_walkback` `:618`; panel load
  loop `:850-888`
- `scripts/check_roster_integrity.py` — the precedent, header
- `scripts/refresh_all.py` — step 1 failure handling `:325-329`, `:359-361`;
  VERIFY block `:514-574`
- `scripts/scheduled_refresh.py` — `fail()` `:692-707`; `refresh_all` exit
  handling `:1104-1108`; alert-delivery ledger rationale `:344-348`
- `scripts/measure_publication_lag.py:229-267` — the non-strict decision and the
  `KNOWN RESIDUAL GAP` note this brief takes up
- `scripts/capture_status.py:45`, `scripts/build_data_audit.py:462` — the two
  display surfaces that see a carry-forward without its cause
