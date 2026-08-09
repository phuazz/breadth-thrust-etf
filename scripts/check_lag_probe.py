"""Guard layer for the scheduled publication-lag probe.

CLAUDE.md requires that any unattended run ship with something able to
catch a silently-wrong step before its output is trusted. The failure mode
here is not a crash — it is a probe that exits 0 having recorded nothing
useful, so the log grows, the workflow stays green, and two weeks later the
lag question is still unanswerable.

Three ways that happens, each checked below:
  1. The run appended no rows at all (probe crashed after its own guard, or
     wrote to the wrong path).
  2. It appended rows, but every ETF errored — an endpoint outage recorded
     as data. The rows exist; they carry no observation.
  3. It appended rows whose timestamp is not from this run — the log was
     not actually written, and the check would otherwise pass on last
     night's data.

It also carries the one read of the log that is safe to do unsupervised.
``--summary`` stratifies by fund domicile and refuses to pool across the
2026-08-09 sampling change, because the naive read of this record — mean
sessions-behind over every row — is wrong in a way that looks evidenced.
See PUBLICATION_LAG_NOTES.md; the short version is that the probe ran
3-UCITS-to-1-US for its first 24 rows, so a pooled average tracks the
sample's composition rather than the funds' behaviour.

Run:
    python scripts/check_lag_probe.py                 # after a probe run
    python scripts/check_lag_probe.py --max-age-min 45
    python scripts/check_lag_probe.py --summary       # stratified read of the log
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "data" / "publication_lag_log.jsonl"

# The probe writes one row per ETF per run. Imported rather than restated:
# a second hardcoded list would drift the moment the probe is widened, and
# the guard would then pass a run that had silently lost a fund.
sys.path.insert(0, str(ROOT / "scripts"))
from measure_publication_lag import PROBE_TARGETS  # noqa: E402

EXPECTED_ETFS = set(PROBE_TARGETS)


def load_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            # A malformed line is itself a finding: the log is append-only
            # and a partial write means a run died mid-flush.
            rows.append({"_malformed": True})
    return rows


def check(rows: list[dict], now: datetime, max_age_min: int) -> tuple[int, list[str]]:
    msgs: list[str] = []
    if not rows:
        return 1, ["FAIL no rows in the log at all — the probe wrote nothing"]

    if any(r.get("_malformed") for r in rows):
        msgs.append("FAIL log contains a malformed line — a run died mid-write")

    cutoff = now - timedelta(minutes=max_age_min)
    fresh = []
    for r in rows:
        ts = r.get("probe_utc")
        if not ts:
            continue
        try:
            when = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when >= cutoff:
            fresh.append(r)

    if not fresh:
        newest = max((str(r.get("probe_utc") or "") for r in rows), default="none")
        msgs.append(
            f"FAIL no row newer than {max_age_min} min — this run appended "
            f"nothing (newest in log: {newest})")
        return 1, msgs

    got = {r.get("etf") for r in fresh}
    missing = EXPECTED_ETFS - got
    if missing:
        msgs.append(f"FAIL this run is missing ETFs: {sorted(missing)}")

    # An observation is only useful if the endpoint actually answered.
    with_data = [r for r in fresh if r.get("latest_with_data")]
    if not with_data:
        msgs.append(
            "FAIL every ETF in this run failed to return holdings — the log "
            "grew but recorded no observation. Endpoint down, or the "
            "transport has changed again")
    elif len(with_data) < len(fresh):
        quiet = sorted(r.get("etf") for r in fresh if not r.get("latest_with_data"))
        msgs.append(f"WARN partial run — no data returned for {quiet}")

    erroring = sorted(r.get("etf") for r in fresh if r.get("errors"))
    if erroring:
        msgs.append(f"WARN per-date errors recorded for {erroring}")

    if not any(m.startswith("FAIL") for m in msgs):
        lags = {r.get("etf"): r.get("sessions_behind_nyse") for r in with_data}
        msgs.append(f"OK   {len(fresh)} rows this run; sessions behind NYSE: {lags}")

    return (1 if any(m.startswith("FAIL") for m in msgs) else 0), msgs


# Rows written before the probe was widened carry no "domicile" key. That
# absence IS the vintage marker — those runs sampled three UCITS against one
# US fund, which cannot separate "US funds publish sooner" from "SOXX
# published sooner". Inferring domicile for them would make them look
# comparable to the balanced rows; they are not, so they are reported apart.
_US_PROBE_TARGETS = {
    etf for etf, spec in PROBE_TARGETS.items()
    if (spec or {}).get("ishares_region") == "us"
}


def row_domicile(row: dict) -> tuple[str, bool]:
    """(domicile, balanced_vintage). Never guesses a row into the good cohort."""
    stated = row.get("domicile")
    if stated:
        return str(stated), True
    return ("US" if row.get("etf") in _US_PROBE_TARGETS else "UK"), False


def summarise(rows: list[dict]) -> list[str]:
    """Stratified read. Deliberately reports no pooled average — see
    PUBLICATION_LAG_NOTES.md section 2."""
    out: list[str] = []
    balanced = [r for r in rows if row_domicile(r)[1]]
    legacy = [r for r in rows if not row_domicile(r)[1]]

    def cohort(rs: list[dict], dom: str) -> list[dict]:
        return [r for r in rs if row_domicile(r)[0] == dom
                and r.get("sessions_behind_nyse") is not None]

    out.append(f"BALANCED SAMPLE (domicile-tagged, 2026-08-09 onward): "
               f"{len(balanced)} rows, "
               f"{len({r['probe_utc'][:10] for r in balanced if r.get('probe_utc')})} day(s)")
    if not balanced:
        out.append("  none yet — nothing here can answer the domicile question")
    for dom in ("US", "UK"):
        c = cohort(balanced, dom)
        if not c:
            continue
        behind = sorted(r["sessions_behind_nyse"] for r in c)
        funds = sorted({r["etf"] for r in c})
        out.append(f"  {dom:<3} n={len(c):<3} sessions behind NYSE "
                   f"min={behind[0]} max={behind[-1]}  funds={funds}")

    if legacy:
        days = len({r["probe_utc"][:10] for r in legacy if r.get("probe_utc")})
        mix = {d: sum(1 for r in legacy if row_domicile(r)[0] == d)
               for d in ("UK", "US")}
        out.append("")
        out.append(f"LEGACY ROWS (no domicile field, pre-widening): {len(legacy)} "
                   f"rows over {days} day(s), mix {mix}")
        out.append("  NOT comparable — sampled 3 UCITS : 1 US, so these cannot")
        out.append("  separate domicile from fund. Excluded from the cohorts above.")
        out.append("  Do NOT average them together with the balanced rows.")

    fridays = {r["probe_utc"][:10] for r in balanced
               if r.get("latest_with_data")
               and date.fromisoformat(r["latest_with_data"]).weekday() == 4}
    out.append("")
    out.append(f"Friday observations in the balanced sample: {len(fridays)} "
               f"(evidence bar is >=2, spanning two weekends)")
    if len(fridays) < 2:
        out.append("  BELOW BAR — this is anecdote, not a lag measurement. "
                   "Do not set cadence from it.")
    out.append("Binding lag for cadence is the UK/UCITS cohort: 23 of 24 "
               "deployed panels are UCITS,")
    out.append("and the factsheet fires on CSP1. See PUBLICATION_LAG_NOTES.md.")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--summary", action="store_true",
                     help="Stratified read of the whole log instead of "
                          "checking the latest run. Never pools across the "
                          "2026-08-09 sampling change.")
    ap.add_argument("--log", default=str(LOG))
    ap.add_argument("--max-age-min", type=int, default=45,
                     help="How recent a row must be to count as this run. "
                          "Generous by default: GitHub cron can fire well "
                          "behind schedule, and the probe itself takes "
                          "minutes.")
    args = ap.parse_args()

    rows = load_rows(Path(args.log))

    if args.summary:
        for m in summarise([r for r in rows if not r.get("_malformed")]):
            print(m)
        return 0

    code, msgs = check(rows, datetime.now(timezone.utc), args.max_age_min)
    for m in msgs:
        print(f"  {m}")
    print(f"\ntotal rows in log: {len(rows)}")
    if code:
        print("\nPROBE GUARD FAILED — this run's observations are not "
              "trustworthy. Do not read the lag record as if it covered "
              "this window.", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
