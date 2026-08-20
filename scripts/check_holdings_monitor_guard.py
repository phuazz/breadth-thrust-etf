"""Guard layer for the theme-constituent monitor.

The vault rule is that no unattended job ships without something able to
catch a silently-wrong step before its output is trusted. This monitor
runs daily against two third-party CDNs and publishes a page a human reads
to generate ideas, so the failure that matters is not a crash — a crash is
loud and self-reporting. It is the run that succeeds against a changed,
truncated or restated upstream file and publishes a confident, wrong table.

Every check below exists because that specific failure would otherwise be
invisible on the rendered page:

  G1 roster age      an issuer's CDN serving a stale file looks identical
                     to a quiet market
  G2 as-of monotone  an as-of that goes BACKWARDS means the CDN served an
                     older file than we already hold; the page would
                     silently regress (this repository has been bitten by
                     exactly this on a price panel)
  G3 weight sum      a truncated file parses cleanly and sums to 60%
  G4 roster size     a format change that drops a column drops rows
  G5 price coverage  names present but unpriced render as blank cells that
                     read as "nothing happening"
  G6 dropped share   a spike in rejected rows is an upstream format change
                     announcing itself
  G7 flow turnover   if most of a fund's names change status overnight, the
                     comparison basis is wrong, not the portfolio
  G8 payload age     the page is only as good as its last successful build

Usage:
    python scripts/check_holdings_monitor_guard.py
    python scripts/check_holdings_monitor_guard.py --json
Exit 1 on any FAIL. WARN does not fail the run.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from holdings_sources import MAX_ROSTER_AGE_DAYS, MONITOR_FUNDS  # noqa: E402
from run_holdings_monitor import LATEST_PATH, SNAP_DIR  # noqa: E402

# Weights are a published percentage and should sum to ~100. The band is
# wide enough for rounding across 150 lines and for a fund holding a little
# cash, and tight enough that a truncated file cannot pass.
WEIGHT_SUM_MIN, WEIGHT_SUM_MAX = 97.0, 103.0

# Imported intent, not a second opinion: the same 0.85 floor the breadth
# panels use (compute_breadth's WARN floor, enforced at commit by G6 there).
# One definition of "thin" across the repository.
PRICE_COVERAGE_FLOOR = 0.85

# Above this share of rejected rows the upstream format has changed.
# Measured 2026-08-19: ARKG 2/35 = 5.7%, XBI 10/157 = 6.4%.
DROPPED_SHARE_MAX = 0.20

# A day in which more than this share of an active fund's names changed
# status is a comparison-basis fault. ARK is a high-turnover manager but
# does not replace half its book overnight.
FLOW_TURNOVER_MAX = 0.50

# The payload behind the page.
PAYLOAD_MAX_AGE_HOURS = 36


class Result:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def add(self, gate: str, scope: str, status: str, detail: str) -> None:
        self.rows.append({"gate": gate, "scope": scope,
                          "status": status, "detail": detail})

    @property
    def failed(self) -> bool:
        return any(r["status"] == "FAIL" for r in self.rows)


def _stored_as_ofs(etf: str) -> list[date]:
    d = SNAP_DIR / etf.upper()
    if not d.exists():
        return []
    out = []
    for f in d.glob("*.json"):
        try:
            out.append(date.fromisoformat(f.stem))
        except ValueError:
            continue
    return sorted(out)


def run_checks(today: date | None = None) -> Result:
    res = Result()
    today = today or datetime.now(timezone.utc).date()

    if not LATEST_PATH.exists():
        res.add("G8", "-", "FAIL", f"{LATEST_PATH.name} does not exist")
        return res
    payload = json.loads(LATEST_PATH.read_text(encoding="utf-8"))

    built = datetime.fromisoformat(payload["built_at_utc"])
    age_h = (datetime.now(timezone.utc) - built).total_seconds() / 3600
    res.add("G8", "payload",
            "FAIL" if age_h > PAYLOAD_MAX_AGE_HOURS else "OK",
            f"built {age_h:.1f}h ago (cap {PAYLOAD_MAX_AGE_HOURS}h)")

    funds = payload.get("funds", {})
    for etf in sorted(MONITOR_FUNDS):
        f = funds.get(etf)
        if f is None:
            res.add("G0", etf, "FAIL", "registered fund missing from payload")
            continue
        cfg = MONITOR_FUNDS[etf]
        as_of = date.fromisoformat(f["as_of"])

        age = (today - as_of).days
        res.add("G1", etf, "FAIL" if age > MAX_ROSTER_AGE_DAYS else "OK",
                f"roster as of {as_of} ({age}d old, cap {MAX_ROSTER_AGE_DAYS}d)")

        stored = [d for d in _stored_as_ofs(etf) if d != as_of]
        newest_other = max(stored) if stored else None
        if newest_other and as_of < newest_other:
            res.add("G2", etf, "FAIL",
                    f"as-of {as_of} is BEHIND stored snapshot {newest_other}; "
                    f"the source served an older file than we already hold")
        else:
            res.add("G2", etf, "OK",
                    f"as-of {as_of} is the newest held"
                    + (f" (prior {newest_other})" if newest_other else ""))

        ws = f["weight_sum_pct"]
        res.add("G3", etf,
                "FAIL" if not (WEIGHT_SUM_MIN <= ws <= WEIGHT_SUM_MAX) else "OK",
                f"weights sum to {ws:.2f}% "
                f"(band {WEIGHT_SUM_MIN}-{WEIGHT_SUM_MAX}%)")

        lo, hi = cfg["expected_holdings"]
        n = f["n_holdings"]
        res.add("G4", etf, "FAIL" if not (lo <= n <= hi) else "OK",
                f"{n} holdings (band {lo}-{hi})")

        cov = f["price_coverage"]
        res.add("G5", etf, "FAIL" if cov < PRICE_COVERAGE_FLOOR else "OK",
                f"price coverage {cov:.1%} (floor {PRICE_COVERAGE_FLOOR:.0%})")

        n_drop = len(f.get("dropped", []))
        share = n_drop / max(1, n + n_drop)
        res.add("G6", etf, "WARN" if share > DROPPED_SHARE_MAX else "OK",
                f"{n_drop} rows rejected ({share:.1%} of file, "
                f"cap {DROPPED_SHARE_MAX:.0%})")

        rows = f.get("rows", [])
        basis = f.get("flow_basis")
        if not basis:
            res.add("G7", etf, "WARN",
                    "no prior snapshot yet, so flow is unavailable "
                    "(expected on the first run only)")
        else:
            moved = sum(1 for r in rows
                        if r.get("fs") in ("new", "added", "trimmed"))
            moved += len(f.get("exits", []))
            frac = moved / max(1, len(rows))
            res.add("G7", etf, "FAIL" if frac > FLOW_TURNOVER_MAX else "OK",
                    f"{frac:.1%} of names moved vs {basis} "
                    f"(cap {FLOW_TURNOVER_MAX:.0%})")
    return res


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--json", action="store_true", help="emit machine-readable")
    a = p.parse_args(argv)

    res = run_checks()
    if a.json:
        print(json.dumps({"failed": res.failed, "checks": res.rows}, indent=1))
    else:
        width = max(len(r["scope"]) for r in res.rows) if res.rows else 6
        for r in res.rows:
            mark = {"OK": "  OK ", "WARN": " WARN", "FAIL": " FAIL"}[r["status"]]
            print(f"{mark} {r['gate']:3} {r['scope']:<{width}}  {r['detail']}")
        n_fail = sum(1 for r in res.rows if r["status"] == "FAIL")
        n_warn = sum(1 for r in res.rows if r["status"] == "WARN")
        print(f"\n{len(res.rows)} checks — {n_fail} FAIL, {n_warn} WARN")
    return 1 if res.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
