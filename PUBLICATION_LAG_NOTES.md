# Publication-lag probe — how to read the record

Read this before drawing any conclusion from `data/publication_lag_log.jsonl`.
The record changes sampling partway through, and pooling across that break
produces a wrong answer that looks well-evidenced.

Instrument: `scripts/measure_publication_lag.py`. Guard: `scripts/check_lag_probe.py`.
Stratified read: `python scripts/check_lag_probe.py --summary`.

---

## 1. The finding: publication lag splits by fund DOMICILE

As of the first widened run, 2026-08-09:

| Domicile | Funds | Latest holdings served | Sessions behind NYSE |
|---|---|---|---|
| US | SOXX, IVV, IWM | 2026-08-07 (Fri) | 0 |
| UK / Irish UCITS | CSP1, CNDX, EXV1, IJPN | 2026-08-06 (Thu) | 1 |

The separation is clean, and two comparisons inside the sample make it hard to
explain any other way:

- **IVV against CSP1** is the decisive pair. Same index, same constituents, same
  market close, different domicile. One serves Friday, the other does not.
- **EXV1 (Xetra, closes 15:30 UTC) against IJPN (Tokyo, 06:00 UTC)** are no
  further ahead than CSP1 despite closing hours earlier. The underlying market's
  close does not drive publication.

## 2. The sampling break — do not pool across it

The probe originally sampled CSP1, SOXX, EXV1, IJPN: **three UCITS and one US
fund**. Every fund read Thursday, and that was written up as a uniform lag
batched fund-administrator-side. It was wrong, and the sample could not have
caught it — a single US series cannot distinguish "US funds publish sooner"
from "SOXX published sooner". The owner's challenge ("I would expect Friday's
07 Aug data should be in?") is what surfaced it.

| Vintage | Rows | Probe runs | Domicile mix | Usable for the domicile question? |
|---|---|---|---|---|
| Before 2026-08-09 (no `domicile` field) | 24 | 6 | 18 UCITS : 6 US | **No** — 3:1, confounded |
| From 2026-08-09 (has `domicile` field) | 7 | 1 | 4 UCITS : 3 US | Yes |

**The presence of the `domicile` field is the vintage marker.** Rows lacking it
predate the widening. `--summary` splits on exactly this and will not pool them.

A pooled "average sessions behind" over the whole log is meaningless: it mixes a
3:1-UCITS-weighted period with a balanced one, so it drifts toward the UCITS
number for reasons of sampling rather than fact, and understates how promptly
the US cohort publishes.

## 3. What this changes in the cadence decision

The framework in `measure_publication_lag.py`'s docstring is written around a
single lag `L`. There are two, and **the binding one is the UCITS lag**:

- 23 of 24 deployed panels are UK/Irish UCITS. SOXX is the only US-domiciled
  one. All 14 Europe supersector candidates are UCITS.
- The factsheet workflow fires on the push touching `data/breadth_csp1.json` —
  CSP1, a UCITS fund.

So the US cohort's 0-session result is diagnostic (it proves the endpoint and
the transport are fine, and isolates the cause to domicile) but it is **not**
the constraint. Apply the docstring's `L` thresholds to the UCITS number alone.
Reading a pooled or US-flattered `L` would move the Saturday refresh on the
strength of funds this book does not hold.

## 4. Evidence bar, unchanged

Each Friday contributes one lag observation. The bar stated when the instrument
was built still stands: **a few probes per day for one to two weeks, spanning at
least two weekends**, before setting any cadence. Treat fewer than two Friday
observations as anecdote.

At the time of writing the balanced sample has **one run, on one day, covering
zero completed weekends**. It establishes that the split exists. It does not yet
measure either lag.

Note also that IVV and IWM are measurement instruments, not panels this book
trades. They carry their configuration inline in `PROBE_TARGETS` rather than
joining `scripts/etf_registry.py`, so that nothing downstream reads them as
holdings.

---

*Written 2026-08-09, after the widened probe's first run.*
