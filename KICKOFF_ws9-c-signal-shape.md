# WS9 — Sleeve C signal shape: is the constraint the universe or the ranking? (pre-registration)

**STATUS: G0 REGISTERED AND FROZEN 2026-08-06 (Thursday — weekday verified
with a date library); T1, T2 and §7's bars remain DRAFT, pending
countersign. The owner authorised G0 alone to run, in session, on
2026-08-06. G0's specification below — including the trailing window
pinned in §4a — was fixed and committed BEFORE the cell ran; T1 and T2
must be signed without amendment to G0, and a G0 result cannot be used to
retune anything in §5 or §7. A blocked path files a STOP, not an edit.**

Drafted after the owner asked (2026-08-06, in session) how to capture a
"rolling sequence of bubbles" — the claim, made in a social post showing
software, then silver, then semis peaking in succession, that leadership
rotates between themes and that the money is made by being early into each
leg. This registration converts that question into the one form of it the
book has not already answered.

---

## 1. Declared priors (known before this registration — cannot serve as evidence)

Every one of these is filed and settled. None is re-run here; the ledger
rule is reuse, not re-tread.

- **WS3 (filed 2026-07-03):** Sleeve C's rotation Sharpe +0.684 versus its
  own same-universe equal-weight basket +0.759 at the 1x per-line cost
  vector; drawdown matched (−36% vs −37%); break-even cost multiple 1.0x.
  The rotation machinery loses to holding the same 25 names equally.
- **WS3 survivorship, quantified and not correctable:** BTC-USD alone is
  23% of gross sleeve contribution (added Phase 15 with backfilled
  history); the top five names are ≈62%; no point-in-time membership
  exists before 2026-07-18.
- **WS7 (registered 2026-07-18, review Friday 2026-10-02):** the seat is
  under a three-way decision — keep, switch the 10% to the passive EW-25
  basket, or drop. Universe frozen in `data/c_universe_pit.json`; the OOS
  window runs from 2026-07-03 and must stay clean.
- **WS8 universe monitor (monthly, guarded):** every attempt to widen this
  book has cost walk-forward Sharpe — Phase 5 sub-industries −0.10,
  **Phase 16 SLV −0.18 on sleeve B**, Phase 17 KWEB −0.13, WS2 commodity
  thread killed at every level, WS2 country sleeve killed outright. Phase
  25's two accepted additions came in at +0.001, described at source as
  expecting no return uplift.
- **Structural:** the candidate gate requires five years of overlapping
  history, so a newly launched theme is ineligible for five years unless a
  longer-history proxy exists. Catching a genuinely new bubble early is
  therefore barred by the admission rule, not by the signal.

The two legs of the owner's chart map onto this directly: silver has been
tested and rejected (Phase 16), and the AI complex was refused entry on
the correlation gate (Phase 15, AIQ max-corr 0.891 vs SKYY, 0.881 vs BOTZ,
because NVDA/MSFT/GOOGL/META are already the top holdings of incumbents).

## 2. What this study is NOT (scope exclusions, fixed)

Registered exclusions, each with its reason. None may be reintroduced
after results exist.

1. **No universe widening.** No semis vehicle, no silver vehicle, no
   additions of any kind. Both candidate themes were identified by reading
   a chart of what has already run, which is the issuer behaviour the book
   exists to avoid, and the widening prior is six-for-six negative.
2. **No sizing change.** Whether a 10% sleeve is the right size for a
   fat-left-tail exposure is a separate decision requiring its own
   registration and its own entry-point discipline.
3. **No engine change and no interference with WS7.** This study writes
   nothing to `data/`, does not touch `data/c_universe_pit.json`, and does
   not alter any deployed configuration. Its results do NOT enter the
   2026-10-02 WS7 decision, which is settled by its own frozen rule.
4. **No deployment before 2026-10-02** regardless of outcome.

## 3. Question

Holding the universe fixed, is Sleeve C's *ranking statistic* — distance
above the 200-day moving average, a level — the binding constraint on
capturing rotating theme leadership? A level ranks a decelerating +30%
identically to an accelerating +30%; the phenomenon described is convex.

## 4. Gate G0 — leadership persistence (runs FIRST; a fail is a STOP)

Before any signal is built: across the frozen 25-name universe, compute the
Spearman rank correlation between each name's trailing-window return rank
and its forward holding-period return rank, at the deployed weekly
rebalance cadence, over the full eligible history.

- **Bar:** mean rank correlation > 0 with block-bootstrap
  P(> 0) ≥ 0.90, blocks of 13 weeks to respect autocorrelation.
- **If G0 fails, the study STOPS and files.** No ranking rule of any
  specification can work on a universe whose leadership does not persist,
  and the result would independently explain WS3's finding that the
  rotation loses to equal weight. That is a complete and useful answer at
  a fraction of the cost, and it closes the owner's question honestly.

G0 is registered as a gate, not as evidence for any trial.

### 4a. G0 windows (pinned 2026-08-06, before the cell ran)

The draft under-specified the trailing window. Pinned here, before any
computation, and not amendable afterwards:

- **Primary:** trailing 13-week total return rank → forward 1-week return
  rank, weekly (Friday) observations. Thirteen weeks matches the
  acceleration window already frozen for T1; one week forward matches the
  deployed rebalance cadence, so the statistic measures the horizon the
  sleeve actually trades.
- **Report-only (declared now, barred from deciding G0):** trailing 26w →
  forward 1w, and trailing 13w → forward 4w. Reported for shape, never to
  rescue a failing primary.
- **Signal-agnostic by design.** G0 ranks on returns, not on the deployed
  distance-above-200d-MA statistic. Using the deployed signal would make
  G0 a test of the incumbent rather than of the phenomenon, and T1 exists
  precisely to ask whether a different statistic helps — a gate built on
  the incumbent could not legitimately gate its own challenger.
- **Sample:** `data/thematic_prices_cache.parquet`, 2018-01-02 to
  2026-07-17 (read-only). The cache ends the day before WS7's 2026-07-18
  freeze, so G0 cannot touch the WS7 out-of-sample window at all.
- **Eligibility:** a name enters from its first date with 13 weeks of
  history; NaN signals are excluded pairwise. SHY is the cash proxy and is
  excluded from the cross-section.

## 5. Registered trials (2 — none may be added after results exist)

- **T1 (primary) — acceleration-augmented ranking.** Rank by the rate of
  change of the 200-day-MA distance over a fixed 13-week window, blended
  50/50 with the deployed level. All other sleeve mechanics unchanged: K,
  the +5% signal floor, the 35% per-name cap, the SHY cash floor, weekly
  rebalance.
- **T2 — dispersion-gated rotation.** The deployed signal, unchanged, but
  the sleeve rotates only when cross-sectional dispersion of trailing
  theme returns sits in its top tercile, and holds the equal-weight basket
  otherwise. This is the owner's hypothesis stated mechanically: leadership
  is worth chasing when themes are separating, and not otherwise.

Report-only, excluded from the pool and from any adoption path: the T1+T2
combination; per-theme attribution; the same two cells at K−1 and K+1.

**Parameter grid frozen here:** one acceleration window (13 weeks), one
blend weight (50/50), one dispersion threshold (top tercile). Three cells
including G0, plus six report-only. No sweep. Any widening of this grid
after results exist is prohibited and would require a fresh registration
carrying the multiplicity forward.

## 6. Definitions (frozen)

- **Universe:** the 25 names in `data/c_universe_pit.json` as at
  2026-07-18. Read-only.
- **Benchmark, binding:** the EW-25 basket exactly as WS7 §4 defines it —
  equal-weighted, rebalanced weekly, USD, 159801.SZ converted at USDCNY,
  costs from `data/ws3_cost_stress.json` `per_line_vectors_bps` at 1x
  charged on realised turnover. The deployed rotation is a secondary
  reference only; beating a benchmark that already loses to equal weight
  is not a result.
- **Scoring:** walk-forward Sharpe, the house convention, not in-sample.
- **Costs:** 1x per-line vector as primary; a 1.5x stress as a required
  robustness bar, because an acceleration term raises turnover and the
  deployed sleeve's break-even cost multiple is already 1.0x.

## 7. Gate — ALL bars required per trial; any fail ⇒ FAIL_EW25_STANDS

- **G1 (primary):** walk-forward Sharpe exceeds the EW-25 basket, net, at
  1x costs.
- **G2 (cost robustness):** G1's sign holds at 1.5x costs.
- **G3 (survivorship strip):** G1's sign holds with BTC-USD and 159801.SZ
  removed from both the trial and the benchmark. **A result that survives
  only with BTC-USD included is recorded as a FAIL**, given its 23% share
  of gross contribution on backfilled history.
- **G4 (drawdown floor):** maximum drawdown not worse than the EW-25
  basket by more than 3pp. A signal that buys convexity must not simply be
  buying more of the left tail.

## 8. Verdict enum and decision linkage

Per trial: **CANDIDATE** (all bars pass) / **FAIL_EW25_STANDS** / **STOP**.

CANDIDATE adopts nothing. It opens a post-WS7 discussion and only in the
branch where WS7 leaves rotation machinery in place; if WS7 lands on drop,
a CANDIDATE here becomes an input to a fresh decision about whether
thematic exposure returns at all, made under entry-point discipline. A
FAIL closes the signal-shape question for this book and, taken with the
six-for-six widening prior, is the documented answer to whether this sleeve
can chase rotating leadership: not with this universe, and not with this
ranking.

## 9. Three ways this could be silently wrong (stated before build)

1. **Hindsight in the framing.** The question arrived from a chart of
   three episodes that had already happened, and acceleration was chosen
   because it fits them. Guards: no ticker additions (§2.1); the parameter
   grid frozen to one cell per idea (§5); the benchmark is EW-25 rather
   than the deployed rotation, so the study cannot pass by clearing a bar
   already known to be low.
2. **Survivorship dressed as signal.** The universe is today's survivors,
   two lines carry backfilled or proxied history, and the top five names
   are 62% of gross contribution. Guards: G3's strip test with a
   pre-committed FAIL verdict; results reported with and without.
3. **Multiplicity.** Momentum has an unbounded knob space, and this book
   has already screened many cells across WS2, WS3 and Phase 25. Guards:
   three registered cells total, the grid frozen above, report-only cells
   named and barred from adoption, and every cell entering the
   deflated-Sharpe register that WS3 established.

## 10. Sign-off asks (five)

1. G0 as a genuine STOP gate — a persistence failure closes the study and
   files, rather than proceeding to build signals anyway.
2. Two registered trials only (T1 acceleration, T2 dispersion gate), with
   the parameter grid frozen at one cell each.
3. EW-25 as the binding benchmark, not the deployed rotation.
4. G3's pre-committed FAIL for any result that depends on BTC-USD.
5. Non-interference with WS7: no engine or universe writes, no deployment
   before 2026-10-02, results excluded from the WS7 decision.

*Prepared 2026-08-06 (Thursday) by Claude at ZH's direction. Owner:
Zhenghao Phua. Freezes on countersign; any blind amendment is recorded in
this file before any cell runs.*
