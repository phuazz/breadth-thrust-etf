# WS21 — Sleeve C Bitcoin line: rank on IBIT, spot-proxy history before 2024-01-11 (pre-registered amendment)

Registered 2026-09-19 (Saturday — weekday verified with `datetime`). Owner:
Zhenghao Phua. Status: **PROPOSED — every gate in §10 awaits countersign.**
Frozen at the commit that adds this file; the commit date is the provenance.

This amendment is staged and inert until §3 is satisfied. Nothing in it
changes the live book, the published record or the WS7 evidence before the
WS7 verdict is filed.

## 1. Question

Sleeve C trades Bitcoin through IBIT (iShares Bitcoin Trust ETF) but ranks it
on a spot proxy: Yahoo's `BTC-USD` UTC-day close, reindexed to the NYSE
calendar, with a modelled 25 bp/yr expense drag (Phase 15.2, 2026-05-26).
Should the line be ranked on the instrument it trades, from the first session
that instrument exists, with the proxy retained only where no ETF existed?

The operational trigger is recorded for honesty but is NOT the reason for the
change: on 2026-09-19 Yahoo served no 2026-09-18 bar for `BTC-USD` at all,
sleeve C held on the coverage floor, and the weekend publication failed on the
release contract. The contract defect is fixed separately (an authorised HOLD
on any sleeve). WS21 stands or falls on instrument fidelity.

## 2. Prior evidence (frozen — read before anything else)

- **Phase 15.2 (2026-05-26, `run_thematic_rotation.py` UNIVERSE entry):**
  `BTC-USD` spot with IBIT's 25 bp/yr drag as the backtest series; IBIT the live
  execution vehicle; GBTC rejected for its premium/discount.
- **Ledger row 2026-08-22, breadth-thrust-etf (defect-correction, no register
  record):** three substitutions "ASSESSED AND ALL THREE DECLINED": Norgate has
  no crypto database; **IBIT "cannot be the series — chain-linking splices at
  Jan 2024 and changes the basis (fee, NYSE hours against 24/7,
  premium/discount to NAV)"**; GBTC's premium/discount spans 0.28–1.11.
  Durable finding: "the source was the wrong lever" — the defect then was a
  one-bar gap amplified into a ten-month exclusion, since fixed.
- **Register 2026-08-22-breadth-thrust-etf-3 (confirmed, authored):** Binance
  BTCUSDT returns are equivalent to the cached series (correlation 0.9998,
  median 3.2 bp). Reopen condition, verbatim: "Return-equivalence only, and
  only for ISOLATED bars. It does NOT license replacing the series,
  backfilling a run, or splicing on level."
- **Register 2026-08-12-breadth-thrust-etf-3 (no-effect, extracted †):**
  sleeve A priced on the London lines it holds rather than US proxies moves
  the record by less than noise. Precedent for an instrument-substitution
  test; unreviewed extraction.
- **WS7 (`KICKOFF_ws7-c-seat.md`, registered 2026-07-18):** review Friday
  2026-10-02; §4 frozen; OOS window opens 2026-07-03; the EW-25 benchmark
  prices `BTC-USD` USD-native; the tripwire fired 2026-07-31 and the owner held
  the review date.

**What is different from 2026-08-22.** That assessment asked which source
could fill a hole without changing the basis, and correctly answered none.
This amendment asks whether the basis SHOULD change, and answers the three
objections directly:

1. *Fee.* IBIT's price is net of the same 25 bp/yr the model already charges,
   so the post-launch segment carries the registered fee by construction
   rather than by model.
2. *Hours.* Every other sleeve C name is measured at the 16:00 ET close. The
   incumbent measures Bitcoin at 00:00 UTC, three to four hours later. IBIT
   puts the line on the peers' clock; the incumbent is the like-for-like
   breach, not the ETF.
3. *Premium/discount.* Not assumed either way. Measured in §5 M1 before any
   flip, with a stop condition.

## 3. Timing — contingent on WS7 (PROPOSED)

- WS7 is decided on the incumbent basis. Its §4 is frozen and the `BTC-USD`
  series is an input to both of its legs (rotation and EW-25).
- The live default flips only after the WS7 verdict is filed in the ledger,
  and only if that verdict is KEEP or SWITCH. Under DROP the live flip is
  moot; the §5 measurements are still filed and WS21 closes as such.
- Until then: code staged behind a flag that scheduled runs never set; the
  seal refuses to publish under the flag; measurements read the SEEN window
  only — nothing from 2026-07-03 onward is computed under the new basis
  before 2026-10-02.

## 4. Registered definitions (fixed 2026-09-19 — ONE construction, no menu)

- **Column key** stays `BTC-USD` in the sleeve C universe, cache and every
  published JSON, so history labels do not move. Label becomes
  "Bitcoin — IBIT from 2024-01-11; spot proxy with 25 bp/yr drag before".
  Reader-facing surfaces print the traded line, IBIT, through the registry.
- **Cut-over `c` = 2024-01-11** (Thursday — weekday verified), IBIT's first
  NYSE close (Yahoo 26.63; Norgate serves the same first session). No
  seasoning offset. No alternative cut-over, ETF or level will be evaluated.
- **Before `c`:** the incumbent synthetic series exactly as the main clone's
  sleeve C cache carries it at the freeze commit — Yahoo `BTC-USD` UTC-day
  close, reindexed to the NYSE calendar, compounded drag
  `(1 − 0.0025)^(days/365)` from its 2018 first bar. Frozen into
  `data/btc_proxy_history_pre_ibit.parquet` with a SHA-256 and its derivation
  in a sidecar, committed, and never refetched. A later Yahoo revision of
  pre-2024 Bitcoin history deliberately does not reach the record.
- **From `c`:** `S_t = S_c × IBIT_t / IBIT_c`, where `S_c` is the frozen
  synthetic value on `c` and `IBIT_t` is the total-return close (Norgate
  TOTALRETURN under the strict path, Yahoo `auto_adjust` otherwise; identical
  while IBIT pays no distribution, pinned by §5 M4). **No expense drag on this
  segment.**
- **Level:** the synthetic (spot-like) level is retained for cache
  continuity; the `ma_distance` signal is scale-free. Price evidence and
  factsheet returns for the line use IBIT's own close via the registry
  trading proxy.
- **Freshness:** the column's last bar is IBIT's last NYSE close. The spot
  ticker leaves the download list; the `crypto_24x7` reindex and the drag no
  longer apply to the live segment.
- **Registry:** `BTC-USD` gains a registry entry naming IBIT as the traded
  line (the Europe-sleeve mechanism, where the key is a panel id) and as the
  price-source proxy.
- **Out of scope, unchanged:** the WS3 per-line cost vector (`BTC-USD`
  25 bps), the C gate (30% of 25 names above +5%), the 200-session window,
  the universe, and every other sleeve. IBIT's launch fee waiver (0.12% on
  the first USD 5 bn for twelve months — from memory, verify against the
  prospectus) is not modelled; its level effect is of the order of 0.1% and
  is disclosed, not corrected.

## 5. Measurements (disclosure on the SEEN window only: 2024-01-11 → 2026-07-02, 620 NYSE sessions)

- **M1 Premium/discount.** Daily log-return difference between IBIT's close
  and Binance BTCUSDT at the NYSE close instant (20:00 UTC under EDT, 21:00
  UTC under EST — resolved per date with a timezone library, never
  hard-coded); report median, p5, p95, worst, and the cumulative ratio's
  range. **Stop condition:** p5–p95 outside ±1.0% halts the flip pending
  owner review. (GBTC's range was 0.28–1.11; a spot ETF is expected inside
  a few tenths of a per cent — expectation, not evidence.)
- **M2 Restatement.** Sleeve C on the seen window, incumbent basis against
  the new: count of weekly decisions whose basket differs (names or weights),
  count of gate-state flips, the dates of each, and Sharpe / CAGR / MaxDD /
  turnover for C and Sharpe for the deployed blend, both bases. No tuning.
  Published with the flip under the "restate, do not re-rank" convention
  (commit 3afbd49 precedent).
- **M3 Join behaviour.** The 200-session window straddles `c` until
  2024-10-25 (the 200th NYSE session counting `c` as the first). Report the
  maximum absolute signal difference in that span.
- **M4 Source agreement.** Norgate TOTALRETURN against Yahoo `auto_adjust`
  for IBIT over its whole span: maximum relative difference. **Stop
  condition:** above 1e-4, investigate before proceeding.

No performance gate governs adoption: the decision is instrument fidelity,
made at registration. M1 and M4 are stop conditions; M2 and M3 are
disclosure.

## 6. Instrumentation (staged — measurement only until §3)

- Environment flag `BTE_C_BTC_BASIS`, values `incumbent` (default) and
  `ibit`. Scheduled runs never set it. With the flag unset the cache, the
  signal and every published number are bit-identical to the incumbent —
  pinned by a value-preservation test to the standard of record
  2026-08-15-breadth-thrust-etf-1.
- `component_release.seal` refuses to seal while the flag is set (the
  `BTE_APPLY_STAGED_ROSTER` rule, mirrored), so a staged run cannot publish.
- The rebuilt cache writes a declared basis for the `BTC-USD` column into the
  price-cache sidecar (`ibit-spliced@<sha256 prefix>`), so `price_revisions`
  classifies the first rebuild as a basis change rather than a same-basis
  revision of roughly 430 populated cells.
- The measurement script refuses dates after 2026-07-02 unless the WS7
  verdict row exists in the ledger.
- **Promotion** = change the default to `ibit` by a dated commit after the
  WS7 verdict is filed, with M2 published alongside, and the first live
  week's commentary annotated where `signals_prev` (incumbent basis) is
  compared with `signals` (new basis) for this line.

## 7. Trial register

Zero configurations evaluated. One construction, fixed above. Nothing here
contributes to a future deflated-Sharpe haircut.

## 8. Three ways this could be silently wrong (stated before build)

1. **Anchor drift.** If `S_c` or the pre-`c` segment moves under a vendor
   revision, the whole post-`c` segment shifts by the ratio and the record
   restates silently. Mitigation: the frozen artefact and its hash; a guard
   test fails if the hash changes.
2. **A basis change read as a vendor retraction, or not read at all.** The
   first rebuild changes every populated cell from `c` onward. Without the
   declared basis the revision guard either warns of a retraction or, worse,
   logs it as ambiguous and moves on. Mitigation: the sidecar basis tag in §6
   and a test that classifies a synthetic old/new pair as `basis_changes`.
3. **Strict-Norgate completeness inverted.** IBIT becomes a US line the strict
   path must TAKE, while the spot ticker leaves the list; the "2 unresolved"
   expectation becomes 1 (159801.SZ). A loader that still counts `BTC-USD`
   as never-expected would record a Yahoo frame as Norgate-built. Mitigation:
   `assert_norgate_complete` expectations re-pinned by test.

Also guarded: the cross-basis `signals_prev` comparison on the flip week
(§6); the tail heal asking Yahoo for spot when the column now follows IBIT
(the single-ticker fetch must ask IBIT under the flag).

## 9. Filing

Ledger row on registration (kickoff, PRE-REGISTERED, no register record until
a verdict lands). At promotion: a ledger row with M1–M4, register records for
the two stop conditions, `build_register_index.py` regenerated, the four
guards green, and the WS7 memory updated.

## 10. Sign-off (owner)

| Gate | Status |
|---|---|
| Cut-over `c` = 2024-01-11, no seasoning, no alternative | PROPOSED — pending countersign |
| Level retained at the synthetic scale; IBIT price via registry | PROPOSED — pending countersign |
| Timing §3: flip only after the WS7 verdict, KEEP or SWITCH only | PROPOSED — pending countersign |
| Frozen pre-`c` artefact, never refetched | PROPOSED — pending countersign |
| M1 stop band ±1.0% (p5–p95); M4 stop 1e-4 | PROPOSED — pending countersign |
| Scope exclusions in §4 (cost vector, gate, window, universe) | PROPOSED — pending countersign |

*Amendments before promotion are permitted only to gates not yet touched by
evidence; the construction in §4 is frozen at the registering commit.*

## 11. Event log (append-only — records events, amends nothing)

**2026-09-19 (Saturday — weekday verified). Registered.** Prompted by the
weekend publication failure of the same morning (release contract refusing a
sleeve C coverage-floor HOLD; fixed separately). The 2026-08-22 decline of
IBIT as the series was read before registration and is answered in §2, not
overturned by silence.
