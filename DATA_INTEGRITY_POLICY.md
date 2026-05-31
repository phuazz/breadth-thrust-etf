# Data Integrity Policy — USD Multi-Strategy ETF Portfolio

**Owner:** Phua Zheng Hao (CIO)
**Last reviewed:** 2026-05-31
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
| iShares UK | 13 Strategy A sectors + 5 Strategy D UCITS + country UCITS for the (deferred) Country sleeve | `https://www.ishares.com/uk/individual/en/products/<id>/<slug>/<ajax>.ajax?fileType=csv&fileName=<ETF>_holdings&dataType=fund` | Generally reliable; occasional rate-limiting handled by the 1.5s + jitter throttle in `fetch_constituents.py` |
| iShares US | SOXX (Strategy A) | `https://www.ishares.com/us/products/239705/<slug>/<ajax>.ajax?fileType=csv&fileName=SOXX_holdings&dataType=fund` | **Akamai-blocked from automated requests since at least 2026-05-15.** Returns a 10MB warmup HTML page instead of the CSV. Carry-forward fallback active. |

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

The weekly workflow runs `fetch_constituents.py --etf SOXX` to invoke the carry-forward mechanism when iShares US is blocked. This keeps the SOXX breadth series extending against the most recent known-good roster even when fresh constituents are unavailable. **Other ETFs (iShares UK and Europe) are refreshed by the operator's manual pipeline runs, which write the breadth JSONs to the repo for CI to consume.**

---

## 4. Carry-forward policy

When an upstream constituent source returns an empty / invalid / blocked response, `fetch_constituents.py` carries the most recent known-good roster forward into the new Friday snapshot. This protects the pipeline from silently dropping an ETF from the eligible universe (which the staleness guard in `scripts/alignment.py` would otherwise trigger after 7 days).

**Carry-forward is recorded in two places:**

1. `data/constituents_<etf>.json` — the `carry_forwards` array lists every target Friday that was filled by carry-forward, including the source snapshot it was carried from.
2. `data/constituents_<etf>.json` — the `staleness` block tracks `days_since_last_real_fetch` so freshness can be monitored.

**Why carry-forward is acceptable for short windows:**

- Strategy A / D trade the ETF wrapper, not the underlying constituents. A stale roster only affects the SIGNAL (how strong is breadth?), not the POSITION (what we hold).
- Index turnover for SOXX is ~2-3 holdings per year. A 2-4 week stale roster represents <0.5 stocks of drift in a 30-stock universe.
- Breadth is computed daily against fresh yfinance prices; only the roster snapshot is stale.

---

## 5. Staleness windows

Defined in `scripts/fetch_constituents.py`:

| Threshold | Days since last real fetch | Behaviour | Operator action |
|-----------|---------------------------|-----------|-----------------|
| **Fresh** | ≤ 14 | No alert; carry-forward continues if applicable | None |
| **Warning** | 15-30 | Prints warning to stdout; surfaces yellow banner on dashboard; exit code 0 | Investigate the upstream source; trigger a manual fetch from an alternative source if possible |
| **Critical** | > 30 | Prints loud alert to stderr; **`fetch_constituents.py` exits code 2**; **`pipeline.py` aborts the dashboard publish entirely** with `SystemExit` | Mandatory: see Section 6 |

The critical threshold is set at 30 calendar days because:

- SOXX's PHLX Semiconductor Index rebalances annually with ~2-3 swaps. A 30-day stale roster represents ~0.25 stocks of drift in a 30-stock universe — still negligible.
- Beyond 30 days, the probability of one constituent being delisted or undergoing corporate action becomes non-trivial.
- 30 days is short enough that an unattended degradation is caught within ~4 weekly CI cycles.

---

## 6. Escalation procedure

When `pipeline.py` aborts publication with a "PUBLISH ABORTED" message or `fetch_constituents.py` exits code 2:

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
```bash
python -c "
import requests
r = requests.get('https://www.ishares.com/us/products/239705/ishares-phlx-semiconductor-etf/1467271812596.ajax?fileType=csv&fileName=SOXX_holdings&dataType=fund', headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
print('HTTP', r.status_code, '|', len(r.content), 'bytes')
print(r.text[:200])
"
```

If the response is the 10MB HTML page, the Akamai block is still in place.

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
| 2026-05-31 | SOXX | Warning (21 days stale) | iShares US holdings endpoint Akamai-blocked since ~2026-05-15. No CI workflow was invoking `fetch_constituents.py --etf SOXX`, so the staleness guard in `scripts/alignment.py` masked SOXX out of the eligible Strategy A universe for the 2026-05-22 rebal — SOXX picks were dropped silently. | Phase 26: added SOXX-specific refresh steps to `.github/workflows/weekly_factsheet.yml`. Phase 26.1: built the staleness-alarm framework (this document, plus exit-code-2 in fetcher, plus publish-abort in `pipeline.py`) so the next occurrence fails loudly within 30 days. SOXX roster carried forward from 2026-05-08; will re-fetch from a residential IP if staleness crosses 30 days before iShares US block resolves. |

---

## 10. Review schedule

- Annually (target: first week of June each calendar year).
- After any critical-severity incident (Section 9 entry with severity = critical).
- When a new ETF is added to the deployed universe (verify the source is added to Section 2 catalogue and a refresh cadence is defined in Section 3).
- When the deployed universe changes substantively (sleeve added / removed; major rebalance to weighting scheme).
