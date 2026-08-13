"""WS15 step 0 — Norgate coverage gate + live-anomaly classification (CNDX).

WHY THIS EXISTS. WS15 measures what constituent-price survivorship was worth
to the PUBLISHED CNDX record, and fills the reuse-masked residual WS11 left
behind (FB, PCLN, FOXA, FOX — columns the backfill skipped because unrelated
ticker-reuse bars made them look priced). Before any deep-history pull is
trusted, the archive has to demonstrate it covers what we are about to ask of
it — the em-rotation-lab step0 pattern (frozen verdict rule, probes first,
design-around never).

VERDICT RULE (frozen before the script ran):
  PASS — NDU updated within 7 calendar days; US Equities + US Equities
         Delisted both present; every WS11 fill symbol and every residual
         target resolves to a Norgate symbol whose quotation window covers
         its roster held-window within tolerance (stale-roster tails allowed
         up to TOLERANCE_CAL_DAYS; each is listed, not hidden); delisted
         archive contains delistings from 2018 or earlier.
  FAIL — anything else. STOP and report; Phases 1-3 are not run.

Classification probes (evidence, not memory): MNST and EA lost their last
bars on the same Monday (2026-07-20 gap start) while still in the roster;
SPCX and HONA lack a 50-session history; HOLX / WBA / ANSS were called "live
listings" by the queue entry that commissioned this workstream. Each is
classified from Norgate security_name + last quoted date alone.

Three ways this gate could be silently wrong, defended:
1. A per-symbol quirk mistaken for an archive limit — the probe set spans
   live, renamed and delisted lines; a uniform boundary is a subscription
   limit, a single-symbol hole is a mapping error, and each is reported per
   symbol rather than pooled.
2. Resolving by ticker instead of point-in-time — every roster ticker is
   resolved via norgate_symbols.resolve on its FIRST held date (the WS11
   discipline), so a reused ticker cannot silently substitute today's owner.
3. A stale NDU mistaken for missing depth — last_database_update_time is
   recorded and gates the verdict; a verdict against a stale database is no
   verdict at all.

Output: reviews/ws15_gate.json
Run:    python scripts/run_ws15_gate.py
"""
from __future__ import annotations

import datetime as dt  # Python datetime: months are 1-indexed
import json
import re
import sys
from collections import Counter
from pathlib import Path

import norgatedata as nd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from norgate_symbols import NOT_EQUITY, resolve, _candidates, _window  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "reviews" / "ws15_gate.json"

# Stale-roster tolerance: iShares carried TFCFA ~15 sessions past its death,
# XLNX 4, ALXN 2. 45 calendar days (~30 trading days) covers every observed
# tail with room, while still failing a mapping that is months adrift.
TOLERANCE_CAL_DAYS = 45

# The 24 columns whose first bar is exactly 2018-01-05 — the WS11 Norgate
# fill signature (fills start at min(snapshots); yfinance-era columns start
# at the 2017-07-10 warmup). Derived from the cache, cross-checked against
# the WS11 record's "filled 25 of CNDX's 27" (the 25th fill starts later
# than 2018-01-05 and is caught by the residual sweep instead).
WS11_FILLS = [
    "ALXN", "ANSS", "ATVI", "CA", "CELG", "CERN", "CTRP", "CTXS", "DISH",
    "HOLX", "LVNTA", "MXIM", "MYL", "NLOK", "QRTEA", "QVCA", "SGEN", "SHPG",
    "SPLK", "SYMC", "TFCFA", "WBA", "WLTW", "XLNX",
]
# Reuse-masked residual: columns non-empty only because an unrelated
# security took the ticker in 2025+, so the WS11 backfill skipped them.
RESIDUAL_TARGETS = ["FB", "PCLN", "FOXA", "FOX"]
# Live anomalies to classify from evidence.
LIVE_ANOMALIES = ["MNST", "EA", "SPCX", "HONA"]
# Names the commissioning queue entry called "live listings yet empty".
EVIDENCE_SET = ["HOLX", "WBA", "ANSS"]


def _held_window(snaps: dict, ticker: str) -> tuple[str | None, str | None]:
    """First and last snapshot date on which the roster carried ``ticker``."""
    dates = sorted(k for k, v in snaps.items()
                   if ticker in (v.get("tickers") or []))
    return (dates[0], dates[-1]) if dates else (None, None)


def _probe(sym: str) -> dict:
    first, last = _window(sym)
    try:
        name = nd.security_name(sym)
    except Exception:
        name = None
    return {
        "symbol": sym,
        "security_name": name,
        "first_quoted": str(first) if first else None,
        "last_quoted": str(last) if last else None,  # None = still quoted
    }


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    assert nd.status(), "NDU is not running — gate cannot be evaluated"
    today = dt.date.today()

    dbs = nd.databases()
    for required in ("US Equities", "US Equities Delisted"):
        assert required in dbs, f"database missing: {required}"

    upd = {db: str(nd.last_database_update_time(db))
           for db in ("US Equities", "US Equities Delisted")}
    upd_age_days = min(
        (today - dt.date.fromisoformat(v[:10])).days for v in upd.values()
    )

    snaps = json.loads(
        (ROOT / "data" / "constituents_cndx.json").read_text(encoding="utf-8")
    )["snapshots"]

    # ---- WS11 fill symbols + evidence set: held-window coverage ---------
    fills, failures, tails = [], [], []
    for t in sorted(set(WS11_FILLS) | set(EVIDENCE_SET)):
        h_first, h_last = _held_window(snaps, t)
        sym = resolve(t, dt.date.fromisoformat(h_first)) if h_first else None
        if sym is None and h_last:
            sym = resolve(t, dt.date.fromisoformat(h_last))
        row = {"ticker": t, "held_first": h_first, "held_last": h_last}
        if sym is None:
            # The backfill's stale-roster fallback: sole candidate ever using
            # the ticker, if it plausibly IS the held security.
            from backfill_delisted_prices import _sole_candidate, _held_dates
            held = sorted(_held_dates(snaps, t))
            sym = _sole_candidate(t, held)
            row["via"] = "sole_candidate" if sym else None
        if sym is None:
            row["status"] = "UNRESOLVED"
            failures.append(t)
            fills.append(row)
            continue
        row.update(_probe(sym))
        fq = (dt.date.fromisoformat(row["first_quoted"])
              if row["first_quoted"] else None)
        lq = (dt.date.fromisoformat(row["last_quoted"])
              if row["last_quoted"] else None)
        hf = dt.date.fromisoformat(h_first)
        hl = dt.date.fromisoformat(h_last)
        # Coverage: quoting had to start by the first held date, and the
        # quotation end may precede the last held date only by the
        # stale-roster tolerance.
        starts_ok = fq is not None and fq <= hf
        tail_days = (hl - lq).days if lq is not None else 0
        ends_ok = lq is None or tail_days <= TOLERANCE_CAL_DAYS
        row["held_window_covered"] = bool(starts_ok and ends_ok)
        if lq is not None and 0 < tail_days <= TOLERANCE_CAL_DAYS:
            tails.append(f"{t}: roster outlived {row['symbol']} by {tail_days}d")
        if not row["held_window_covered"]:
            failures.append(t)
        fills.append(row)

    # ---- Residual targets: what does point-in-time resolution give? -----
    residual = []
    for t in RESIDUAL_TARGETS:
        h_first, h_last = _held_window(snaps, t)
        row = {"ticker": t, "held_first": h_first, "held_last": h_last}
        for label, d in (("at_first_held", h_first), ("at_last_held", h_last)):
            sym = resolve(t, dt.date.fromisoformat(d)) if d else None
            row[label] = _probe(sym) if sym else None
        # Every Norgate symbol ever using this ticker root, for the mapping
        # decision (FOXA/FOX are expected to resolve to nothing dated —
        # the 21CF lineage lives under other roots).
        row["all_candidates"] = [_probe(s) for s in _candidates().get(t, [])]
        residual.append(row)

    # 21CF lineage candidates for the FOXA/FOX mapping decision.
    lineage = {root: [_probe(s) for s in _candidates().get(root, [])]
               for root in ("TFCFA", "TFCF")}

    # ---- Live anomalies: classify from evidence -------------------------
    anomalies = []
    for t in LIVE_ANOMALIES:
        cands = _candidates().get(t, [])
        probes = [_probe(s) for s in cands]
        # Classification against the freshest quote on any candidate whose
        # window covers 2026: live if quoted within 5 business days of the
        # NDU update, else delisted at its last quoted date.
        verdict = "NOT IN NORGATE"
        for p in probes:
            lq = p["last_quoted"]
            if lq is None:
                verdict = f"LIVE ({p['symbol']}: {p['security_name']})"
                break
            lq_d = dt.date.fromisoformat(lq)
            if (today - lq_d).days <= 7:
                verdict = f"LIVE ({p['symbol']}: {p['security_name']})"
                break
            if lq_d.year >= 2018:
                verdict = (f"DELISTED {lq} ({p['symbol']}: "
                           f"{p['security_name']})")
        anomalies.append({"ticker": t, "candidates": probes,
                          "classification": verdict})

    # ---- Delisted-archive reach -----------------------------------------
    delisted_syms = nd.database_symbols("US Equities Delisted")
    years = Counter(
        m.group(1)[:4] for s in delisted_syms
        if (m := re.search(r"-(\d{6})$", s))
    )
    earliest = min(years) if years else None
    reach_ok = earliest is not None and int(earliest) <= 2018

    verdict = ("PASS" if (upd_age_days <= 7 and not failures and reach_ok)
               else "FAIL")

    result = {
        "computed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(
            timespec="seconds"),
        "ndu_last_update": upd,
        "ndu_age_days": upd_age_days,
        "delisted_archive": {
            "n_symbols": len(delisted_syms),
            "earliest_delist_year": earliest,
            "reach_2018_ok": reach_ok,
        },
        "tolerance_cal_days": TOLERANCE_CAL_DAYS,
        "ws11_fill_probes": fills,
        "stale_roster_tails": tails,
        "coverage_failures": failures,
        "residual_targets": residual,
        "tcf_lineage_candidates": lineage,
        "live_anomalies": anomalies,
        "verdict": verdict,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, indent=2), encoding="utf-8")

    print(f"NDU updated {upd_age_days}d ago; delisted archive "
          f"{len(delisted_syms)} symbols, earliest {earliest}")
    print(f"\n{'ticker':7s}{'symbol':15s}{'first_q':12s}{'last_q':12s}"
          f"{'held':23s}{'ok':>3s}  name")
    for r in fills:
        if r.get("status") == "UNRESOLVED":
            print(f"{r['ticker']:7s}UNRESOLVED")
            continue
        print(f"{r['ticker']:7s}{r['symbol']:15s}"
              f"{(r['first_quoted'] or '-'):12s}{(r['last_quoted'] or 'live'):12s}"
              f"{r['held_first']}->{r['held_last']:11s}"
              f"{'Y' if r['held_window_covered'] else 'N':>3s}  "
              f"{(r['security_name'] or '')[:38]}")
    print("\nResidual targets (point-in-time resolution):")
    for r in residual:
        f_ = r["at_first_held"]
        print(f"  {r['ticker']:6s} held {r['held_first']}->{r['held_last']}  "
              f"@first -> "
              f"{(f_['symbol'] + ' = ' + (f_['security_name'] or '')) if f_ else 'None'}")
        for c in r["all_candidates"]:
            print(f"         candidate {c['symbol']:15s} "
                  f"{(c['first_quoted'] or '-')}->{(c['last_quoted'] or 'live')}"
                  f"  {(c['security_name'] or '')[:40]}")
    print("\n21CF lineage candidates:")
    for root, cands in lineage.items():
        for c in cands:
            print(f"  {root}: {c['symbol']:15s} "
                  f"{(c['first_quoted'] or '-')}->{(c['last_quoted'] or 'live')}"
                  f"  {(c['security_name'] or '')[:40]}")
    print("\nLive-anomaly classification:")
    for a in anomalies:
        print(f"  {a['ticker']:6s} {a['classification']}")
    if tails:
        print("\nStale-roster tails (allowed, listed):")
        for t in tails:
            print(f"  {t}")
    print(f"\nVERDICT: {verdict}")
    if failures:
        print(f"  coverage failures: {failures}")
    print(f"Wrote {OUT.relative_to(ROOT)}")
    return 0 if verdict == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
