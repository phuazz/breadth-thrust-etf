# Theme constituent monitor

An idea-generation surface: the current published holdings of selected theme
ETFs, priced and ranked so you can see what is moving inside a fund and why.
Built 2026-08-19. Page at `docs/holdings-monitor.html`.

**It is not a signal.** A current roster carries no point-in-time history, so
nothing here can be backtested and no figure on the page is a strategy input.
It feeds human reading. If something found here is ever wanted in a sleeve, it
goes through a registered study first, not through a shared file.

---

## Why it exists separately from the breadth panels

The two look similar and are not the same thing.

| | Breadth panels (`fetch_constituents.py`) | This monitor (`holdings_sources.py`) |
|---|---|---|
| Serves | a backtest | a human reading the page |
| Needs | point-in-time rosters on arbitrary past dates | the current roster only |
| Source | one issuer's API — the only one serving history | each issuer's own daily publication |
| Universe | 38 BlackRock products | any fund with a public daily holdings file |
| Output | `breadth_*.json` → strategy engines | a page → nobody's book |

Strategy C's 25-name universe has **zero** overlap with the 38-ETF roster
registry, because ARK, First Trust, SSGA and Bosera are not BlackRock. That is
what makes constituent breadth unavailable for Sleeve C, and it is a hard
constraint on the backtest question (see `SCOPE_arkg-nport-probe.md`). It is
*not* a constraint here, because monitoring wants today's roster and both
issuers publish one every business day.

---

## Adding a fund

One entry in `MONITOR_FUNDS` in `scripts/holdings_sources.py`, plus one adapter
function if the issuer's format is new. That is the whole contract. An adapter
takes the registry config and returns a `RosterSnapshot`.

```python
"XBI": {
    "etf": "XBI", "label": "...", "issuer": "State Street (SPDR)",
    "adapter": "ssga_xlsx", "url": "https://...xlsx",
    "active": False,                   # index fund — flow is suppressed
    "expected_holdings": (100, 220),   # guard band, not a contract
},
```

Three rules an adapter must obey:

1. **The as-of date is the issuer's, never `today()`.** Read the publication
   date out of the file. If the file carries no date, fail — do not substitute
   one. A snapshot stamped with the fetch date relabels a stale file as
   current, which is the failure mode this repository has already been bitten
   by twice.
2. **Dropped rows are counted, never silent.** Cash lines, unsettled-trade
   placeholders and Bloomberg composites all belong in `dropped` with a reason.
   A roster that quietly lost a tenth of its names still looks healthy.
3. **Do not guess a ticker.** Venue-coded symbols (`ARCT UQ`) are rejected by
   default. To admit one, add a `ticker_overrides` entry with the verification
   recorded beside it — two independent sources agreeing on the security. That
   is an operator's verified mapping, not a pattern strip.

---

## Flow: what the manager actually traded

Published only for funds marked `active: True`. In an index fund the same
arithmetic returns real numbers that measure the index committee rebalancing,
not conviction, so the section is hidden rather than shown with a caveat
nobody reads.

**A naive `weight_now - weight_prev` is wrong** and wrong in a way that looks
right: it is dominated by price. A name that fell 20% shows a weight drop that
reads exactly like selling.

The decomposition instead prices **yesterday's share counts at today's
closes**, renormalises to 100%, and subtracts that counterfactual from today's
actual weight. What is left is the part only a trade could have produced.
Creations and redemptions cancel, because both sides are normalised.

`tests/test_holdings_monitor.py::test_pure_price_move_produces_no_flow` pins
this. If it ever goes red, the flow column is reporting the market as if it
were the manager.

Flow needs two consecutive snapshots, so it is unavailable on the first run
and accrues from the day capture starts. There is no historical flow and none
can be reconstructed — issuer files are today-only.

---

## Running it

```bash
python scripts/holdings_sources.py --all              # probe the sources
python scripts/run_holdings_monitor.py                # capture + metrics
python scripts/check_holdings_monitor_guard.py        # the guard
python scripts/build_holdings_monitor_page.py         # -> docs/
python scripts/scheduled_holdings_monitor.py          # all four, soak mode
```

`data/holdings_monitor/<ETF>/<date>.json` snapshots are **immutable**. A
same-day re-run against identical content is a no-op; against differing
content it raises, because the issuer restated a published file and that is an
event an operator must see rather than a diff to absorb silently.

---

## The guard layer

The vault rule is that nothing runs unattended without something able to catch
a silently-wrong step. A crash is loud and self-reporting; the failure that
matters here is a run that succeeds against a changed, truncated or restated
upstream file and publishes a confident, wrong table.

| Gate | Catches |
|---|---|
| G1 roster age | an issuer's CDN serving a stale file, which looks like a quiet market |
| G2 as-of monotone | an as-of going **backwards** — the CDN served an older file than we hold |
| G3 weight sum | a truncated file that parses cleanly and sums to 60% |
| G4 roster size | a format change that drops a column and so drops rows |
| G5 price coverage | names present but unpriced, rendering as blanks that read as "nothing happening" |
| G6 dropped share | a spike in rejected rows — an upstream format change announcing itself |
| G7 flow turnover | most names changing status overnight means the comparison basis is wrong, not the portfolio |
| G8 payload age | the page is only as good as its last successful build |

A FAIL blocks the page build and the push. G5's floor (0.85) is imported from
`compute_breadth` rather than restated, so there is one definition of "thin"
in the repository.

---

## Schedule

Daily, Windows Task Scheduler, **soak mode first**. Arm with `--push` after two
clean soak runs — the same discipline `scheduled_refresh.py` follows.

Two `fleet_watch.json` rows, because one is not enough: the git heartbeat moves
only when output *changes*, so a run that fires and fails writes nothing and
looks identical to a quiet day. The sentinel at
`logs/holdings_monitor_last_success.txt` is a liveness signal rather than a
change signal. That gap once hid a failed Perp-Funding run for a day.

---

## Measured state at build (2026-08-19)

| | ARKG | XBI |
|---|---|---|
| Issuer | ARK Invest | State Street (SPDR) |
| Format | daily CSV | daily XLSX |
| Roster as of | 2026-08-19 (same day) | 2026-08-18 (one session) |
| Admitted holdings | 33 | 147 |
| Weight sum | 99.53% | 99.88% |
| Rows excluded | 2 | 10 |
| Price coverage | 100% | 100% |
| Flow published | yes (active) | no (index) |

Union of 168 distinct names, 97.0% with a 200-day average. ARKG's 33 admitted
lines reconcile exactly against the 33 in its latest N-PORT filing.

ARK renames funds and the CSV path follows the name — the pre-2025 path
carried `MULTISECTOR` for ARKG and now 404s. When the adapter starts failing,
re-resolve from `GET /api/fund/document-table/<fundId>` on www.ark-funds.com
rather than guessing the new slug.
