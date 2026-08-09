# Data Integrity Policy — USD Multi-Strategy ETF Portfolio

**Owner:** Phua Zheng Hao (CIO)
**Last reviewed:** 2026-08-08
**Repository:** `breadth-thrust-etf`
**Applies to:** signal generation, walk-forward analytics, and dashboard publication for the deployed USD multi-strategy blend.

This document defines the data sources used by the strategy pipeline, the acceptable staleness windows for each, the fallback behaviour when an upstream source is degraded, and the escalation procedure when staleness exceeds policy.

---

## 1. Scope

The deployed portfolio (35% Strategy A / 35% Strategy B / 10% Strategy C / 20% Strategy D, with overlay gates) requires three classes of data on an ongoing basis:

| Class | What it is | Used by |
|-------|-----------|---------|
| **Constituent rosters** | Point-in-time membership of each ETF's underlying index | Strategy A (breadth), Strategy D (breadth) |
| **Daily prices** | Adjusted-close daily price history for ETFs and their constituents | All four strategies |
| **FX rates** | Daily spot FX for non-USD instruments | Strategy C (CNY for 159801.SZ), Strategy D (EUR for Stoxx UCITS) |

This policy covers data-source identification, refresh cadence, staleness limits, and escalation. Walk-forward methodology, signal definitions, and backtest validity are out of scope — see the Method tab of the live dashboard.

---

## 2. Data sources catalogue

### 2.1 Constituent rosters

| Source | Used for | Endpoint pattern | Known issues |
|--------|---------|------------------|--------------|
| BlackRock product-data API | **All 24 ETFs** — both regions (Phase 27, since 2026-08-07) | `https://www.blackrock.com/varnish-api/uk-retail01-product-data/product-data/api/v2/get-product-data?portfolioId=<product_id>&component=holdings&targetSite=ishares-{uk,us}&asOfDate=YYYYMMDD` (full parameter set in `fetch_constituents.product_data_params`) | Serves arbitrary historical `asOfDate` back to at least 2018-01-05. **For a date with no holdings (weekend, holiday, pre-inception, future) it does NOT error: it returns a null roster with `asOfDate` silently rewritten to the LATEST available date.** Accepting that would stamp today's roster onto a historical Friday, so the parser enforces date parity. `hasData` is `true` even for a null roster and must not be used. |
| iShares UK / US `.ajax` CSV | **Retired.** Source of the ~10,400 cached CSVs in `data/raw_ishares/`, still read cache-first for history | `https://www.ishares.com/{uk/individual/en,us}/products/<id>/<slug>/<ajax>.ajax?fileType=csv&fileName=<ETF>_holdings&dataType=fund` | **Dead since the 2026-07 re-platform.** Returns the single-page product shell as HTTP 200 HTML for every date, including dates that previously worked. The iShares US variant was separately Akamai-blocked from ~2026-05-15; the product-data API reaches SOXX via `targetSite=ishares-us` and supersedes both problems. |
| SEC EDGAR (N-PORT-P) | SOXX (Strategy A) — **secondary, since Phase 26.2** | `https://data.sec.gov/submissions/CIK0001100663.json` (filings index) + `https://www.sec.gov/Archives/edgar/data/1100663/<acc>/primary_doc.xml` (per-filing holdings) | Quarterly cadence (filed within 60 days of quarter-end). 28 of 33 holdings resolve to US-listed tickers via OpenFIGI; 5 are foreign primaries without ADRs + cash sweep — denominator hit ~6%, signal-direction-preserving. |
| OpenFIGI (no-auth tier) | SOXX (Strategy A) — CUSIP → US-listed ticker mapping | `https://api.openfigi.com/v3/mapping` | Free tier: 10 mappings per request, 25 req per 6 sec. On-disk cache at `data/cusip_to_ticker_cache.json` so we only call once per new constituent. |

### 2.2 Daily prices

| Source | Used for | Refresh cadence | Known issues |
|--------|---------|-----------------|--------------|
| yfinance (Yahoo Finance) | All ETF-level daily closes (Strategies B, C, D), all constituent prices (Strategies A, D) | Real-time on demand; on-disk parquet cache in `data/*_cache.parquet` (gitignored) | Occasional ticker resolution failures for delisted / renamed names — logged as `n_with_price < n_constituents` per day in the breadth output |

### 2.3 FX rates

| Source | Used for | Refresh cadence | Known issues |
|--------|---------|-----------------|--------------|
| yfinance (`USDCNY=X`, `EURUSD=X`) | CNY → USD for 159801.SZ; EUR → USD for Stoxx UCITS reporting | Real-time on demand | Weekend / holiday gaps handled by forward-fill within the `align_series_to_index` helper (`scripts/alignment.py`), capped at `MAX_STALE_DAYS = 7` |

---

## 3. Refresh cadence

| Job | Trigger | Refreshes | Time |
|-----|---------|-----------|------|
| **Daily live mark-to-market** | GitHub Actions cron, Mon-Fri 21:30 UTC (`.github/workflows/daily_live_track.yml`) | `live_track.json` (intra-week NAV vs Friday anchor), `holdings_prices_1y.json` | ~2 min |
| **Weekly factsheet** | GitHub Actions cron, Saturday 02:00 UTC (`.github/workflows/weekly_factsheet.yml`) | SOXX constituents + breadth, Strategy A top-K rotation, Strategies B / C, multi-strategy blend, risk overlay, dashboard, factsheet PDF, weekly email | ~10 min |
| **Manual full refresh** | Operator run, ad-hoc | All constituents (all 23 ETFs), all breadth caches, all rotations, dashboard | ~10-20 min depending on cache warmth |

The weekly workflow runs `fetch_constituents.py --etf SOXX`. Before Phase 27 this existed to invoke the carry-forward mechanism while iShares US was Akamai-blocked; since 2026-08-07 SOXX fetches normally through the product-data API and the step returns a genuinely fresh roster. **Other ETFs (iShares UK and Europe) are refreshed by the operator's manual pipeline runs, which write the breadth JSONs to the repo for CI to consume.**

---

## 4. Fallback chain — primary → secondary → carry-forward

When an upstream constituent source returns an empty / invalid / blocked response, `fetch_constituents.py` traverses a fallback chain to keep the breadth pipeline running:

1. **Primary** (BlackRock product-data API, per Section 2.1). Tried first for each target Friday with the existing walkback up to 5 days.
2. **Secondary** (SEC EDGAR N-PORT-P, where registered in `etf_registry.py`). Tried when primary fails. The EDGAR roster IS used only if its `repPdEnd` date is fresher than the carry-forward alternative — otherwise carry-forward wins. Currently only SOXX has an EDGAR secondary registered.
3. **Carry-forward** (most recent known-good snapshot). Final fallback if primary and secondary both fail or are older than what carry-forward already has.

**Carry-forward applies to a DATA GAP, not to an OUTAGE (Phase 27).** The distinction was added after the 2026-07 incident, in which a dead endpoint carried forward silently for four weeks while reporting the reason as an ordinary holiday gap. The two are now separate outcomes:

| Condition | What it means | Behaviour | Exit |
|---|---|---|---|
| `not_found` | Endpoint healthy; this Friday and the 5 walkback days genuinely have no holdings (market holiday, publication gap) | Carry-forward, `cause: "no_data_in_walkback"` | 0 |
| `endpoint_unavailable` | The transport itself failed — no response, non-200, non-JSON, or a payload we can no longer parse | Walk short-circuits on the FIRST failure; **no carry-forwards emitted**; affected Fridays are absent from `snapshots` and listed in `endpoint_unavailable` | **3** |

A run that cannot reach the endpoint therefore produces a *shorter* series rather than a longer fabricated one. The absence is the honest record, and the downstream staleness guard in `scripts/alignment.py` masks the affected ETF out of the eligible universe rather than trading on an invented roster.

`--carry-forward-on-outage` restores the old behaviour for an operator who explicitly wants a degraded-but-running pipeline. The affected Fridays are then tagged `cause: "endpoint_unavailable"` and the run still exits 3.

**Why the walk short-circuits.** Against a dead endpoint each date costs ~48 seconds (four attempts plus 45s of retry backoff), and the walk is ~448 Fridays per ETF across 24 ETFs. The 2026-08-07 refresh spent roughly 4 of its 4.8 hours re-confirming the same failure and produced only carry-forwards. The breaker trips once per run, so an outage now costs one request per ETF.

**All three are recorded in the per-ETF audit trail:**

1. `data/constituents_<etf>.json` — the `carry_forwards` array lists every target Friday filled by carry-forward, with the source snapshot it came from.
2. `data/constituents_<etf>.json` — the `edgar_used` array (new in Phase 26.2) lists every Friday filled from SEC EDGAR, with accession number, filing date, snapshot date, and the carry-forward date it overrode.
3. `data/constituents_<etf>.json` — the `staleness` block tracks `days_since_last_real_fetch` (real = anything that is not a carry-forward — EDGAR snapshots count as real).
4. Each individual snapshot in `data/constituents_<etf>.json` carries a `source` field when filled from a non-primary source (e.g. `"source": "edgar_nport"`).

**Why this chain is acceptable:**

- Strategy A / D trade the ETF wrapper, not the underlying constituents. A stale roster only affects the SIGNAL (how strong is breadth?), not the POSITION (what we hold).
- Index turnover for SOXX is ~2-3 holdings per year. A 2-4 week stale roster represents <0.5 stocks of drift in a 30-stock universe. Even a worst-case 150-day stale EDGAR roster is only ~1.2 stocks of drift.
- Breadth is computed daily against fresh yfinance prices; only the roster snapshot is stale.
- EDGAR is the authoritative SEC filing — the same data the fund itself filed under regulatory mandate. Substantively equivalent to (in fact, more legally authoritative than) the iShares-published CSV.

---

## 5. Staleness windows

Defined as global defaults in `scripts/fetch_constituents.py`. Per-ETF overrides in `scripts/etf_registry.py` remain supported for a source whose cadence is structurally different, but none is in use — see Section 5b.

### 5a. Global default thresholds

Applied to any ETF that does not carry a `staleness` block in its registry entry.

| Threshold | Days since last real fetch | Behaviour | Operator action |
|-----------|---------------------------|-----------|-----------------|
| **Fresh** | ≤ 14 | No alert; carry-forward continues if applicable | None |
| **Warning** | 15-30 | Prints warning to stdout; surfaces yellow banner on dashboard; exit code 0 | Investigate the upstream source; trigger a manual fetch from an alternative source if possible |
| **Critical** | > 30 | Prints loud alert to stderr; **`fetch_constituents.py` exits code 2**; **`pipeline.py` aborts the dashboard publish entirely** with `SystemExit` | Mandatory: see Section 6 |

The 30-day default is calibrated to a daily-availability source (iShares UK / iShares US when not blocked). The probability of one constituent being delisted or undergoing corporate action becomes non-trivial beyond 30 days; the threshold is short enough that an unattended degradation is caught within ~4 weekly CI cycles.

### 5b. Per-ETF overrides (Phase 26.3)

The mechanism remains available: an ETF whose source cadence is structurally different from the daily-availability norm may declare a `staleness` block in `scripts/etf_registry.py` and have it override the global defaults.

**No ETF currently uses it.** All 38 panels run on the global 14 / 30.

| ETF | warn_days | critical_days | Rationale |
|-----|-----------|---------------|-----------|
| All | 14 | 30 | Daily-availability source (BlackRock product-data API) for every panel; tight thresholds appropriate. |

**SOXX's override was removed on 2026-08-08.** Phase 26.3 had widened it to warn 60 / critical 120 on the reasoning that the iShares US route was Akamai-blocked and EDGAR N-PORT-P was therefore the operative secondary source — quarterly filings plus 60 days of statutory grace put a ~150-day ceiling on achievable freshness, so a 30-day alarm would have fired monthly with nothing the operator could do about it.

Both halves of that reasoning have since gone:

- **The primary works again.** Phase 27 reaches SOXX through the product-data API with `targetSite=ishares-us`, which bypasses the Akamai block entirely. SOXX went from 84 days stale to 0 in one refresh and is now fetched weekly like every other panel.
- **EDGAR was never actually operative.** `edgar_used` has been 0 throughout its life, because its `repPdEnd` is almost always older than the carry-forward it would replace, so the freshness rule in `fetch_constituents.py` correctly declines it. The 120-day window was calibrated to a fallback that has never once supplied a snapshot.

The practical effect of the revert: if the transport breaks again, SOXX now goes critical within a month rather than four. That is the intent — the wide window existed to stop a known-unfixable condition from crying wolf, and the condition is fixed. EDGAR remains registered as a genuine backstop; its value is that it exists, not that it fires.

Each `data/constituents_<etf>.json` includes the actually-applied thresholds in its `staleness` block (along with `threshold_source: "per_etf_override"` or `"global_default"` and the rationale text), so reading any single file is sufficient to understand the policy applied at that build.

### 5c. Build-time source guards (added 2026-08-08)

Sections 5a and 5b govern the constituent **roster**, at **fetch** time. Two further sources are read at **build** time. Both are gitignored, so every machine holds its own copy and a local rebuild is only ever as fresh as whatever that machine last fetched — and neither was covered by any threshold above.

| Source | Read by | Failure mode if unguarded |
|--------|---------|---------------------------|
| `data/prices_cache_<etf>.parquet` | `build_panel_series.py`, `build_data_audit.py` | A cache that has simply stopped advancing still produces a complete, well-formed series. The weekly resample takes the last observation *within* each week, so a cache ending Tuesday is stamped with Friday's label and carries Tuesday's price. The audit reports the same cache's last observed close as each name's *current* price. |
| `data/raw_ishares/<ETF>_<date>.json` | `build_data_audit.py` | The holdings detail returns `[]` on a missing payload by design ("a partial payload beats a failed build"). Correct for the page, wrong for the committed file: the result is well-formed and simply asserts less. |

**Budget: 2 NYSE sessions** between a cache's last bar and the last completed session. Deliberately not zero — these panels span Xetra, LSE and Asian calendars against an NYSE yardstick, so a Europe-only closure leaves a legitimately current cache one session short, and a two-day one (26 December, when NYSE trades and much of Europe does not) leaves it two. Three is past any real cross-calendar gap, and three is what the near-miss below measured, so the budget must sit below it.

**Behaviour: skip, never write.** Unlike Sections 5a/5b, this does not abort the publish — the dashboard itself is unaffected. The stale or unreproducible output is simply not rewritten, leaving the committed file in place. That file is by definition better than anything the run could produce, so the guard is fail-safe by construction rather than depending on an operator reading a warning. `pipeline.py` prints a stderr banner; a direct CLI run exits 1.

**Override:** `ALLOW_STALE_PANEL_CACHE=1`, matching the `ALLOW_STALE_REGIME` escape hatch. Local one-off rebuilds only — never in CI.

**Practical consequence:** `docs/data_audit.json` and `docs/panel/*.json` can only be rebuilt on a machine holding current price caches and the full `raw_ishares` set. That was already true; the guard makes the failure explicit instead of silent.

Guarded by `tests/test_panel_cache_staleness.py`. One test asserts the budget sits below the observed near-miss, so widening it fails the suite. Session gaps in those tests are measured against the real NYSE calendar, including two boundary cases where a calendar-day count disagrees with the session count (31 Jul → 4 Aug is four days but two sessions; 31 Dec → 2 Jan is two days but **one**, New Year's Day being a holiday).

### 5d. Roster coverage floor on the breadth step (added 2026-08-09)

Sections 5a–5c catch data that is **absent or old**. This catches data that is **thin**, which is harder, because breadth is a *ratio*: a partial vendor download does not fail and does not look wrong. It returns a plausible number computed on whatever came back.

Measured in `compute_breadth.py` as `n_with_ma50 / n_constituents` on the latest date — of the names actually in the index today, how many the vendor priced deeply enough to carry a 50-day average.

**This is not the same as the pre-existing `tickers_with_any_yf_data / universe_size`,** which runs 70–90% on healthy panels because the universe carries every name that has ever been a constituent. That ratio cannot separate "delisted names, as expected" from "the vendor returned nothing today", which is why it never flagged the incident below.

| Band | Coverage | Behaviour |
|------|----------|-----------|
| **OK** | ≥ 85% | Coverage printed and recorded in `data_quality.roster_coverage_latest` |
| **Warning** | 50–85% | Warning to stdout; panel still written |
| **Fail** | < 50% | Loud stderr banner; **`compute_breadth.py` exits 2 and does NOT write**, leaving the previous panel in place |

Floors calibrated against all 38 committed panels on 2026-08-09. Healthy sits at 97–100% (30 of 38 at exactly 100%), with a structural tail at ITWN 89.7% and ICHN 93.6% where some Taiwanese and Chinese lines genuinely lack yfinance history. WARN sits below that tail so a structurally-lower panel does not warn every week and teach the operator to ignore it; FAIL sits below anything a real roster has produced.

Unlike Section 5a/5b this does not abort the publish, and unlike 5c it is not silent about a WARN-band panel — a 61% panel is written, because refusing to write it would leave a *stale* panel in place, which is worse than a thin but current one. The judgement it encodes is that below 50% the number is not worth having at all.

**Override:** `ALLOW_THIN_BREADTH=1`. Never in CI.

Guarded by `tests/test_breadth_coverage_floor.py`, which parametrises the real observed coverage of all 38 panels rather than invented numbers. Loosening a floor past a healthy panel, or tightening one onto the structural tail, fails the suite.

**The WARN band is blocked at commit time, not at write time.** Writing a thin panel is tolerable because the alternative is a stale one; *committing* one is not — a 61.5% panel reached main on 2026-08-08 and changed Strategy A's holdings. So `check_refresh_guard.py` gained **G6**, which FAILS the pre-commit guard for any deployed panel below the 85% WARN floor, importing the floor from `compute_breadth` so there is one definition. It prefers `data_quality.roster_coverage_latest` and falls back to deriving `n_with_ma50 / n_constituents` from the series, because every panel written before 2026-08-09 lacks the recorded field and a check that skipped 23 of 24 panels would be worse than none. An unreadable panel warns rather than fails: absence of evidence is not evidence of thinness.

---

## 6. Escalation procedure

When `pipeline.py` aborts publication with a "PUBLISH ABORTED" message, or `fetch_constituents.py` exits **code 2** (roster aged past policy) or **code 3** (transport unavailable):

Exit 3 takes precedence over exit 2 when both apply, because a dead endpoint is the cause and staleness is the symptom. For an exit 3, read `endpoint_health.detail` in the affected `data/constituents_<etf>.json` first — it carries the actual transport failure — and go straight to Step 2.

### Step 1 — Verify the alert
```bash
python -c "
import json
from pathlib import Path
for p in sorted(Path('data').glob('constituents_*.json')):
    s = json.loads(p.read_text())['staleness']
    if s and s['status'] != 'fresh':
        print(p.stem, s['status'], s['days_since_last_real_fetch'])
"
```

### Step 2 — Diagnose the upstream source

For SOXX (iShares US):
Probe the live endpoint through the fetcher's own transport, so the check exercises exactly what the pipeline uses (substitute the affected ETF):

```bash
python -c "import sys; sys.path.insert(0,'scripts'); from datetime import date; import fetch_constituents as fc; from etf_registry import get_etf; cfg=get_etf('SOXX'); p=fc.fetch_product_data(date(2026,7,31), cfg); t=fc.parse_holdings_json(p, date(2026,7,31), ticker_overrides=cfg.get('ticker_overrides',{}), apply_exchange_suffix=cfg.get('apply_exchange_suffix',False)); print(len(t), t[:8])"
```

Read the outcome as follows:

- **A ticker list** — the endpoint is healthy; the failure was transient. Re-run the fetch.
- **`EndpointUnavailable`** — transport dead. If the body is HTML, the route has been re-platformed or bot-blocked again; find the API the current product page calls (open the page, search the markup for `get-product-data`) and update `PRODUCT_DATA_API` / `product_data_params`.
- **`PayloadContractError`** — the endpoint answered but the payload shape changed. The message names the missing key. Fix the parser, then re-cut the fixtures per `tests/fixtures/constituents_parity/README.md`.
- **`0` tickers** — not an outage: that date genuinely has no holdings, or the API echoed a different `asOfDate` and the date-parity guard rejected it. Try an adjacent trading day.

Then confirm the offline contract still holds:

```bash
python -m pytest tests/test_constituent_api_parity.py -q
```

### Step 3 — Refresh from an alternative source

Options in priority order:

1. **Manual fetch from a residential IP.** The iShares US endpoint frequently responds correctly to residential IPs that have not been classified as bot traffic. From a non-blocked machine: `python scripts/fetch_constituents.py --etf SOXX`. Commit the refreshed `constituents_*.json`.
2. **PHLX SOX Index direct.** NASDAQ publishes the SOX index methodology and components. Build a one-off override JSON file at `data/static_roster_<etf>.json` and bypass the fetcher temporarily.
3. **Paid index data subscription.** For production fund operation, switch to a fund-grade index source (SIX, Refinitiv, Bloomberg). The integration cost is one-off; the data is the authoritative source for the underlying index.
4. **Drop the ETF from the universe.** If no alternative is available within the operator's escalation window, remove the affected ETF from the deployed universe (edit `scripts/etf_registry.py`) until the source is restored. For SOXX specifically, the K-refit will fall back from K=7 to K=3-5 — the strategy still works (see the SOXX gate-test in `scripts/run_strategy_a_universe_gate.py`).

### Step 4 — Document the incident

Append a one-line note to this document's **Incident log** (Section 9) with date, ETF, root cause, and remediation taken. Commit and push.

---

## 7. Roles and responsibilities

| Role | Responsibility |
|------|----------------|
| **CIO** (Phua Zheng Hao) | Policy owner. Reviews this document at least annually. Approves changes to staleness thresholds. Decides on escalation outcomes when an upstream source remains degraded beyond 30 days. |
| **CEO** (Eileen Cheng Ma) | Notified of any incident requiring removal of an ETF from the deployed universe or any change to the deployed K-refit configuration arising from a data integrity event. |
| **Operator** (CIO acting in operations capacity until separate engagement) | Runs the weekly manual pipeline refresh. Triggers Step 3 remediation when CI alerts fire. Maintains the incident log. |

---

## 8. Audit log location

All data-integrity-relevant artefacts are versioned in the git history:

- **Per-ETF roster history**: `data/constituents_<etf>.json` includes every Friday snapshot from `start_friday` to the current date, with `walkbacks` and `carry_forwards` arrays documenting every non-exact fetch.
- **Per-ETF breadth history**: `data/breadth_<etf>.json` includes the full daily breadth time series.
- **Pipeline build provenance**: `docs/index.html` contains `window.DATA.built_at` (UTC timestamp of build), `window.DATA.signals_asof` (per-strategy last signal date), and `window.DATA.data_integrity` (current non-fresh roster list).
- **CI run history**: GitHub Actions retains 90 days of workflow logs (Actions tab in the repository).
- **Incident log**: Section 9 of this document.

For a regulatory audit, the combination of git commit history (immutable, signed via GitHub) + the JSON payloads at any historical commit + CI logs reconstructs the full state of every signal-generation cycle from inception.

---

## 9. Incident log

| Date | ETF | Severity | Root cause | Remediation |
|------|-----|----------|-----------|-------------|
| 2026-08-09 | IDP6, EXH2 | Warning (no wrong number published; one deployed panel published on a thin sample) | A refresh committed two panels built on partial yfinance downloads. EXH2 got 2 of 70 tickers (97.4% missing) and so published breadth on 2 of 37 current constituents; the display guard correctly suppressed the bar, so nothing false was shown. IDP6, a DEPLOYED Strategy A panel, got 593 of 1283 and published `ma_breadth` 0.6334 computed on 371 of 603 constituents. Recomputing from a machine whose fetch succeeded gives **0.66** on 600 of 603 — the thin sample was **2.7pp** out. (An earlier estimate of 1.6pp was wrong: it took the share over the cache's full historical universe of 904 names rather than the current 603-name roster, so it was not comparing like with like. Corrected on the same day.) Nothing objected at any stage: breadth is a ratio, so a partial download returns a plausible number; the existing `tickers_with_any_yf_data / universe_size` ratio runs 70–90% on healthy panels and so could not distinguish this from normal delisting; and the gitignored price caches mean two machines running the same code the same day produced materially different coverage, with whoever pushed last setting what was published. | Section 5d — a coverage floor on the CURRENT roster in `compute_breadth.py`, the denominator that can actually tell the two cases apart. Warn below 85%, refuse to write below 50% (exit 2, previous panel left in place). Floors calibrated against all 38 panels so the structural tail (ITWN 89.7%, ICHN 93.6%) does not cry wolf. `tests/test_breadth_coverage_floor.py` parametrises the real observed coverage, so loosening a floor past a healthy panel or tightening onto the tail fails the suite. Note this would have caught EXH2 at fetch time but only WARNED on IDP6 at 61.5% — deliberate, since refusing to write there would leave a stale panel, which is worse than a thin current one. |
| 2026-08-08 | All 38 (24 on the price-cache leg) | None (near-miss — caught in review before commit; nothing was published) | Two build-time sources are gitignored and therefore machine-local, and no threshold in Section 5 reached them. A local `pipeline.py` run rebuilt `docs/panel/*.json` from a price cache whose last bar was 2026-08-04, three NYSE sessions behind. The output was complete and well-formed: the weekly resample takes the last observation *within* each week, so the point labelled 2026-08-07 carried Tuesday's close — ADI would have been committed at 380.29 against a true 389.93, across 25 files, with nothing in the output saying so. Nothing downstream could catch it, because the ETF-level line is fetched live, so the chart would have shown a current ETF line over stale constituent lines and looked healthy. The same run separately replaced 3,370 populated holdings rows across all 38 panels in `docs/data_audit.json` with empty lists, because the `raw_ishares` payloads that detail is parsed from are absent locally and the parser returns `[]` by design. Both were reverted by hand. | Section 5c. Both readers of the price caches share one policy (`check_cache_freshness`, one budget, one exception type) so they cannot drift; `build_panel_series` skips the stale panel, `build_data_audit` refuses to write at all. The missing-payload case is guarded the same way — different cause, identical consequence, same function — because guarding only the caches would have left the next local rebuild still emptying the file. Stale output is never written, so the committed file survives regardless of whether the warning is read. `tests/test_panel_cache_staleness.py` covers both legs and pins the budget below the observed three-session gap. |
| 2026-08-08 | SOXX | None (housekeeping) | Per-ETF staleness override (warn 60d / critical 120d, Phase 26.3) had outlived its rationale. It was set because the iShares US route was Akamai-blocked and EDGAR N-PORT-P was believed to be the operative secondary source. Phase 27 restored the primary via `targetSite=ishares-us` (84 days stale to 0 in one refresh), and EDGAR turned out never to have been operative at all — `edgar_used` has been 0 throughout, because its `repPdEnd` is almost always older than the carry-forward it would replace, so the freshness rule correctly declines it. The 120-day window was calibrated to a fallback that has never supplied a snapshot. | Override removed; SOXX runs on the global 14 / 30 like every other panel. If the transport breaks again SOXX now goes critical within a month rather than four, which is the intent. EDGAR stays registered as a genuine backstop. `tests/test_edgar_nport.py` now asserts the ABSENCE of the override and asks for a current justification if anyone re-adds it. |
| 2026-08-07 | **All 24** | Warning (21 days stale; 84 for SOXX) — would have gone critical on the first run on or after 2026-08-15 | iShares re-platformed its product pages between the 2026-07-10 and 2026-07-17 refreshes. The legacy `<ajax_id>.ajax?fileType=csv` route stopped serving CSV and began returning the single-page product shell as HTTP 200 HTML for **every** `asOfDate`, including dates that had previously succeeded. Detection failed for four weeks not because the fetch was wrong — `looks_like_ishares_holdings_csv` correctly rejected the HTML, `fetch_with_retry` correctly raised, and no bad data was ever cached — but because `main()` folded the transport exception into an ordinary carry-forward whose reason read "no holdings data within 5 days back from target Friday". A dead endpoint was therefore indistinguishable from a run of public holidays. Secondary cost: ~4 of the 4.8-hour refresh was spent re-confirming the same failure at ~48s per dead date, growing ~24 dead dates per week. | Phase 27. (1) Transport swapped to the BlackRock product-data JSON API (Section 2.1), which also reaches SOXX via `targetSite=ishares-us` and so retires the separate Akamai block — SOXX went from 84 days stale to 0. (2) `EndpointCircuit` short-circuits the Friday walk on the first hard failure, so an outage costs one request per ETF instead of ~450. (3) Outage and data gap are now distinct outcomes with distinct exit codes (3 vs 0) and no carry-forward is emitted for an outage; `--carry-forward-on-outage` is the opt-in escape hatch. (4) A changed payload raises `PayloadContractError` rather than parsing to an empty roster. (5) Date parity is enforced: the API answers an unavailable date with a null roster and `asOfDate` rewritten to the latest date, which would otherwise stamp today's roster onto a historical Friday. (6) `tests/test_constituent_api_parity.py` pins both transports to an identical roster for 8 funds including all 6 exchange-suffix ones. Side effect: 5 historical holiday Fridays (e.g. 2020-04-10, 2020-12-25) that previously carried a week-stale roster now resolve to the correct prior trading day, marginally changing historical breadth inputs. |
| 2026-05-31 | SOXX | Warning (21 days stale) | iShares US holdings endpoint Akamai-blocked since ~2026-05-15. No CI workflow was invoking `fetch_constituents.py --etf SOXX`, so the staleness guard in `scripts/alignment.py` masked SOXX out of the eligible Strategy A universe for the 2026-05-22 rebal — SOXX picks were dropped silently. | Phase 26: added SOXX-specific refresh steps to `.github/workflows/weekly_factsheet.yml`. Phase 26.1: built the staleness-alarm framework (this document, plus exit-code-2 in fetcher, plus publish-abort in `pipeline.py`) so the next occurrence fails loudly within 30 days. Phase 26.2: built SEC EDGAR N-PORT-P fallback (`scripts/edgar_nport.py`, registered as SOXX's `edgar_nport` secondary source). EDGAR is loaded once per fetch run and only used when its `repPdEnd` date is fresher than the carry-forward source — currently iShares carry-forward (2026-05-08) is fresher than the latest EDGAR filing (`repPdEnd 2026-03-31`) so EDGAR is loaded as a standby but not currently injecting snapshots. When the next quarterly N-PORT-P lands (~2026-08-29, `repPdEnd 2026-06-30`), EDGAR will automatically resume freshness if iShares US stays blocked. Phase 26.3: added per-ETF staleness overrides (Section 5b); SOXX moves to warn=60d / critical=120d so the alarm thresholds match EDGAR's quarterly cadence rather than the global default 14d / 30d (which were calibrated to daily-availability sources). The dependence on a single operator's residential IP is now structurally removed. |

---

## 10. Review schedule

- Annually (target: first week of June each calendar year).
- After any critical-severity incident (Section 9 entry with severity = critical).
- When a new ETF is added to the deployed universe (verify the source is added to Section 2 catalogue and a refresh cadence is defined in Section 3).
- When the deployed universe changes substantively (sleeve added / removed; major rebalance to weighting scheme).
