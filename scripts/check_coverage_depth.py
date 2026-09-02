"""Coverage-depth guard: does this refresh still carry the delisted-name
history the filed basis carries?

The sixth VERIFY step, added 2026-09-02.

WHAT HAPPENED. The 2026-09-02 post-fill refresh ran from the automation
clone, and the clone's gitignored price caches (data/prices_cache_*.parquet)
had never received the WS11 (2026-08-10) and WS16 (2026-08-13) Norgate
delisted-archive backfills that live only in the main tree's caches, nor
the Norgate columns the 2026-08-31 hand run wrote into them. Every one of
the fifteen US-constituent panels was rebuilt on the SURVIVOR basis: the
delisted names -- XLNX and MXIM in SOXX, SIVB and FRC in CSP1 and IUFS,
TWTR and ATVI in IUCM -- priced nothing, so they dropped out of both the
numerator and the denominator of every breadth reading before their
delisting. 2018 coverage (sum of n_with_price over sum of n_constituents
across the year's sessions) fell on all fifteen: SOXX 0.9997 -> 0.8193,
IUCM 0.9978 -> 0.5385, IUES 1.0000 -> 0.6282, IDP6 0.8349 -> 0.6230.
Sleeve A's headline Sharpe rose 0.9196 -> 0.9623 and the ungated blend
1.1864 -> 1.2011: an unsanctioned restatement upward, published at 62292ed.
Every guard passed -- refresh guard, price-panel guard, 1,752 tests --
because none of them watches coverage DEPTH. Capture and refresh guards ask
whether the newest bars arrived and whether the panels agree with each
other at the tail; G6 reads roster coverage on the LAST row only, where a
delisted name is no longer in the roster and so cannot be missed. Record:
reviews/2026-08-30_ws19_norgate-constituent-prices.md, section 13.

WHAT THIS CHECKS
  C1  Per panel, per calendar year: coverage = sum(n_with_price) /
      sum(n_constituents) over the sessions in that year, measured on the
      panel just written and compared with data/coverage_baseline.json --
      the same measure on the panels committed at the filed basis. Any
      panel-year more than COVERAGE_TOLERANCE BELOW its baseline FAILS. A
      baseline year with no sessions in the new panel FAILS too (history
      vanished -- the G5 class, seen from the other side). A panel-year
      ABOVE its baseline by more than the tolerance WARNS: a rise cannot be
      the survivor mechanism, and blocking it would block the sanctioned
      adoption of --price-source auto on the panels it improves, but it is
      still a restatement of the record and the WARN is the prompt to
      re-baseline it deliberately, with sign-off.
  C2  Per panel, the named delisted PROBES carry prices in this tree's
      price cache. These are names whose whole history exists only because
      of the backfills; yfinance serves nothing for a name delisted in
      2022, so an empty probe column in a cache that exists means the
      backfill is not in that cache and every panel built from it is on
      the survivor basis. FAILS. A cache that is absent (gitignored -- the
      normal state on a runner or in a worktree) is SKIPPED, not failed:
      absence of evidence is not evidence of thinness. C2 is deliberately
      redundant with C1 on a healthy tree; it is the guard on the guard,
      because a baseline written by mistake from a survivor tree would
      make C1 pass while every probe is empty.

LIKE FOR LIKE. The current panel is measured over the BASELINE's own date
window (start_date to end_date). Closed years are unaffected; the open year
is compared on the sessions both sides hold rather than on a year the new
panel has since extended. Sessions beyond the baseline's end are the tail,
and the tail is the other guards' business.

TOLERANCE. See COVERAGE_TOLERANCE, calibrated on measurement, and pinned by
tests/test_check_coverage_depth.py so that retuning it has to argue with
the data.

BASELINE. data/coverage_baseline.json is COMMITTED and carries provenance:
the commit it was measured on, when, and why that commit. It is written
ONLY by hand:

    python scripts/check_coverage_depth.py --write-baseline --ref 670ca1c \\
        --why "..."

and re-baselining is the sign-off act for a sanctioned restatement -- the
WS10 / WS11 / WS16-class decision, not an operator step. A guard whose
baseline moves with the thing it guards is not a guard, which is why the
writer refuses to overwrite an existing baseline without --force and why
refresh_all never calls the writer.

Exit codes: 0 all clear or WARN only; 1 FAIL; 2 the guard could not run
(no baseline, unreadable baseline). Both non-zero codes fail refresh_all.
Pure verdict logic lives in per_year_coverage / compare_panel / probe_frame
and is unit-tested; only main() touches disk and git.

Python datetime months are 1-indexed (January = 1). No calendar arithmetic
happens here: years are the first four characters of the panel's ISO date
strings, and window bounds are compared as ISO strings, whose lexical order
is chronological order.

Usage:
    python scripts/check_coverage_depth.py
    python scripts/check_coverage_depth.py --verbose
    python scripts/check_coverage_depth.py --write-baseline --ref 670ca1c --why "..."
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

import pandas as pd
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from etf_registry import get_etf  # noqa: E402
from refresh_all import ETFS_ALL  # noqa: E402  (single source of truth)

DATA_DIR = ROOT / "data"
BASELINE_PATH = DATA_DIR / "coverage_baseline.json"
BASELINE_SCHEMA = 1

OK = "OK"
WARN = "WARN"
FAIL = "FAIL"
SKIP = "SKIP"

# How far a panel-year may sit below its baseline before the run fails.
#
# CALIBRATED 2026-09-02 on every commit that rewrote the panels between
# 2026-08-15 and 2026-08-31 (1237546, faf9a22, 3718550, 43a21d1, 670ca1c),
# and on the regression itself (670ca1c -> 62292ed):
#
#   same-basis drift, closed years, all 15 US panels ........ 0.0000
#     (identical to four decimal places on every rebuild -- past sessions
#      are read from the cache and the cache only grows)
#   same-basis drift, the OPEN year, worst US panel ........... 0.0021
#     (IUCS 2026: 0.9979 -> 1.0000 as the tail filled in; the like-for-like
#      window removes most of this, and it is the ceiling the tolerance must
#      clear so a healthy refresh does not fire every week)
#   the regression, SMALLEST first-year fall of the 15 ........ 0.0350
#     (IUUS 2018: 1.0000 -> 0.9650, one delisted name in a 31-name panel;
#      SOXX fell 0.1804, IUCM 0.4593, IUES 0.3718)
#
# 0.01 sits 5x above the worst healthy drift and 3.5x below the smallest
# regression. It is one whole percentage point of roster-days in a year, and
# a legitimate roster change (an alias override landing, an entitlement line
# excluded) moves the denominator by far less than that on a US panel. The
# tests pin both bounds.
COVERAGE_TOLERANCE = 0.01

# Binary slack on the band edges, so a panel-year sitting at exactly the
# tolerance is inside it. Baseline coverage is stored to six decimals and the
# smallest real move on any panel is one roster-day in ~7,500, so 1e-9 can
# neither excuse a regression nor manufacture one.
FLOAT_SLACK = 1e-9

# The named delisted probes (C2). Each is a name that yfinance has not served
# since its delisting, so its column can carry prices only through the
# Norgate delisted-archive backfill. Chosen as the names WS11 / WS16 were
# measured on: XLNX (acquired by AMD, 2022-02) and MXIM (by ADI, 2021-08) in
# the semiconductor panel; SIVB and FRC (the March 2023 bank failures) in the
# S&P 500 and its financials slice; TWTR (taken private 2022-10) and ATVI
# (acquired by Microsoft, 2023-10) in communication services.
PROBES: dict[str, tuple[str, ...]] = {
    "SOXX": ("XLNX", "MXIM"),
    "CSP1": ("SIVB", "FRC"),
    "IUFS": ("SIVB", "FRC"),
    "IUCM": ("TWTR", "ATVI"),
}


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------
def us_panels(deployed: Sequence[str] = ETFS_ALL) -> list[str]:
    """The US-constituent members of the deployed set.

    The discriminator is the registry's ``apply_exchange_suffix`` flag, not
    the trading calendar: the four country panels (IJPN, NDIA, ICHN, ITWN)
    sit on the default NYSE calendar but carry exchange-suffixed tickers,
    and the Norgate delisted archive that the backfills came from is a US
    product, so those panels never held what the survivor cache lost. On
    2026-09-02 they were unchanged to four decimal places while all fifteen
    no-suffix panels fell. Fifteen, not fourteen: IUIT was pruned from
    sleeve A's universe on 2026-05-23 but is still deployed, rebuilt by the
    same step from the same class of cache, and regressed identically.
    """
    return [etf for etf in deployed
            if not (get_etf(etf) or {}).get("apply_exchange_suffix", False)]


# ---------------------------------------------------------------------------
# C1 -- per-year coverage
# ---------------------------------------------------------------------------
def per_year_coverage(series: dict, start: str | None = None,
                      end: str | None = None) -> dict[str, dict]:
    """``{year: {n_with_price, n_constituents, sessions, coverage}}`` for the
    sessions of ``series`` inside ``[start, end]`` (ISO dates, inclusive;
    either bound may be None).

    Coverage is the ratio of the two SUMS -- roster-days priced over
    roster-days in the roster -- not the mean of daily ratios, so a day
    with a large roster weighs what it weighs.
    """
    dates = series.get("dates") or []
    priced = series.get("n_with_price") or []
    roster = series.get("n_constituents") or []
    if not (len(dates) == len(priced) == len(roster)):
        raise ValueError(
            f"series arrays disagree in length: dates {len(dates)}, "
            f"n_with_price {len(priced)}, n_constituents {len(roster)}")
    out: dict[str, dict] = {}
    for d, a, b in zip(dates, priced, roster):
        if start is not None and d < start:
            continue
        if end is not None and d > end:
            continue
        row = out.setdefault(d[:4], {"n_with_price": 0, "n_constituents": 0,
                                     "sessions": 0})
        row["n_with_price"] += int(a)
        row["n_constituents"] += int(b)
        row["sessions"] += 1
    for row in out.values():
        row["coverage"] = (row["n_with_price"] / row["n_constituents"]
                           if row["n_constituents"] else None)
    return out


def _row(panel: str, year: str | None, status: str, evidence: str,
         check: str = "coverage", baseline: float | None = None,
         current: float | None = None) -> dict:
    return {"check": check, "panel": panel, "year": year, "status": status,
            "baseline": baseline, "current": current, "evidence": evidence}


def compare_panel(etf: str, base: dict, current: dict | None,
                  tolerance: float = COVERAGE_TOLERANCE) -> list[dict]:
    """C1 verdicts for one panel: one row per baseline year, or a single
    FAIL when the panel itself is missing or unreadable."""
    if not current:
        return [_row(etf, None, FAIL,
                     "panel missing or unreadable -- the deployed set has "
                     "lost a US panel")]
    try:
        now = per_year_coverage(current.get("series") or {},
                                start=base.get("start_date"),
                                end=base.get("end_date"))
    except ValueError as e:
        return [_row(etf, None, FAIL, f"panel series unreadable: {e}")]

    window = f"{base.get('start_date')}..{base.get('end_date')}"
    rows: list[dict] = []
    for year, b in sorted((base.get("years") or {}).items()):
        b_cov = b.get("coverage")
        c = now.get(year)
        if b_cov is None:
            rows.append(_row(etf, year, SKIP,
                             "baseline has no coverage for this year"))
            continue
        if c is None or c.get("coverage") is None:
            rows.append(_row(etf, year, FAIL,
                             f"no sessions in {year} inside the baseline "
                             f"window {window}; history has vanished "
                             f"(baseline held {b.get('sessions')} sessions)",
                             baseline=b_cov))
            continue
        delta = c["coverage"] - b_cov
        # A fall of exactly the tolerance passes (inclusive band), and the
        # slack keeps 0.99 - 1.0 from reading as "below 0.01" in binary.
        detail = (f"{c['coverage']:.4f} vs baseline {b_cov:.4f} "
                  f"(delta {delta:+.4f}, tolerance {tolerance:g}); "
                  f"{c['n_with_price']:,}/{c['n_constituents']:,} "
                  f"roster-days priced over {c['sessions']} sessions, "
                  f"baseline {b['n_with_price']:,}/{b['n_constituents']:,} "
                  f"over {b['sessions']}")
        if delta < -tolerance - FLOAT_SLACK:
            status, note = FAIL, "; BELOW the filed basis"
        elif delta > tolerance + FLOAT_SLACK:
            status, note = WARN, ("; ABOVE the filed basis -- a restatement; "
                                  "if sanctioned, re-baseline with "
                                  "--write-baseline")
        else:
            status, note = OK, ""
        rows.append(_row(etf, year, status, detail + note,
                         baseline=b_cov, current=c["coverage"]))
    return rows


# ---------------------------------------------------------------------------
# C2 -- delisted probes in the price cache
# ---------------------------------------------------------------------------
def probe_frame(etf: str, frame: pd.DataFrame | None,
                probes: Sequence[str]) -> list[dict]:
    """C2 verdicts for one panel given its cache frame (None = no cache)."""
    if frame is None:
        return [_row(etf, None, SKIP,
                     "price cache absent (gitignored; normal on a runner or "
                     "in a worktree) -- probes not checked", check="probe")]
    rows: list[dict] = []
    for name in probes:
        if name not in frame.columns:
            rows.append(_row(etf, None, FAIL,
                             f"{name}: not a column of the cache -- the "
                             f"delisted-archive backfill is not in this "
                             f"cache; every panel built from it is on the "
                             f"survivor basis", check="probe"))
            continue
        col = frame[name].dropna()
        if col.empty:
            rows.append(_row(etf, None, FAIL,
                             f"{name}: column present but EMPTY -- the "
                             f"delisted-archive backfill is not in this "
                             f"cache; every panel built from it is on the "
                             f"survivor basis", check="probe"))
            continue
        first, last = col.index.min(), col.index.max()
        rows.append(_row(etf, None, OK,
                         f"{name}: {len(col):,} obs "
                         f"{pd.Timestamp(first).date()}.."
                         f"{pd.Timestamp(last).date()}", check="probe"))
    return rows


def load_probe_columns(cache_path: Path,
                       probes: Sequence[str]) -> pd.DataFrame | None:
    """Only the probe columns of a cache, or None when there is no cache.
    Columns the cache lacks are simply absent from the frame."""
    if not cache_path.exists():
        return None
    names = set(pq.read_schema(cache_path).names)
    present = [p for p in probes if p in names]
    if not present:
        return pd.DataFrame(index=pd.DatetimeIndex([]))
    return pd.read_parquet(cache_path, columns=present)


# ---------------------------------------------------------------------------
# Baseline I/O
# ---------------------------------------------------------------------------
def panel_from_disk(etf: str, data_dir: Path = DATA_DIR) -> dict | None:
    path = data_dir / f"breadth_{etf.lower()}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def panel_from_git(etf: str, ref: str, root: Path = ROOT) -> dict | None:
    """The committed panel at ``ref``, or None when it is not there."""
    try:
        proc = subprocess.run(
            ["git", "show", f"{ref}:data/breadth_{etf.lower()}.json"],
            cwd=root, capture_output=True, text=True, encoding="utf-8",
            errors="replace",
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def describe_ref(ref: str, root: Path = ROOT) -> dict:
    """Full hash, committer date and subject of ``ref`` -- the provenance
    that travels with the numbers."""
    proc = subprocess.run(
        ["git", "show", "--no-patch", "--format=%H%n%ci%n%s", ref],
        cwd=root, capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=True,
    )
    sha, committed_at, subject = proc.stdout.strip().split("\n", 2)
    return {"ref": ref, "commit": sha, "committed_at": committed_at,
            "subject": subject}


def build_baseline(panels: dict[str, dict], basis: dict, why: str,
                   now: datetime | None = None) -> dict:
    """The baseline document for ``panels`` (``{etf: panel dict}``)."""
    now = now or datetime.now(timezone.utc)
    entries: dict[str, dict] = {}
    for etf, panel in panels.items():
        series = panel.get("series") or {}
        years = per_year_coverage(series)
        entries[etf] = {
            "computed_at_utc": panel.get("computed_at_utc"),
            "start_date": panel.get("start_date"),
            "end_date": panel.get("end_date"),
            "n_trading_days": panel.get("n_trading_days"),
            "trading_calendar": panel.get("trading_calendar"),
            "years": {y: {"coverage": (round(v["coverage"], 6)
                                       if v["coverage"] is not None else None),
                          "n_with_price": v["n_with_price"],
                          "n_constituents": v["n_constituents"],
                          "sessions": v["sessions"]}
                      for y, v in sorted(years.items())},
        }
    return {
        "schema_version": BASELINE_SCHEMA,
        "adopted_at_utc": now.isoformat(),
        "generated_by": ("scripts/check_coverage_depth.py --write-baseline "
                         f"--ref {basis['ref']}"),
        "basis": {**basis, "why": why},
        "metric": ("per calendar year: sum(series.n_with_price) / "
                   "sum(series.n_constituents) over the panel's sessions in "
                   "that year. The guard measures the current panel over "
                   "this baseline's own start_date..end_date so the "
                   "comparison is like for like."),
        "scope": ("US-constituent panels in refresh_all.ETFS_ALL, i.e. "
                  "registry entries without apply_exchange_suffix; see "
                  "check_coverage_depth.us_panels"),
        "tolerance_note": ("the tolerance is COVERAGE_TOLERANCE in "
                           "scripts/check_coverage_depth.py, one definition, "
                           "pinned by tests; it is not stored here"),
        "panels": entries,
    }


def load_baseline(path: Path = BASELINE_PATH) -> dict:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if doc.get("schema_version") != BASELINE_SCHEMA:
        raise ValueError(f"baseline schema {doc.get('schema_version')!r}, "
                         f"expected {BASELINE_SCHEMA}")
    if not doc.get("panels"):
        raise ValueError("baseline lists no panels")
    return doc


def write_baseline(doc: dict, path: Path = BASELINE_PATH) -> None:
    """LF line endings on every platform, so a baseline regenerated on
    Windows is byte-identical to the committed one rather than a CRLF copy
    git has to normalise."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8",
                    newline="\n")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def run_checks(baseline: dict, data_dir: Path = DATA_DIR,
               cache_dir: Path | None = None,
               tolerance: float = COVERAGE_TOLERANCE,
               probes: dict[str, tuple[str, ...]] | None = None) -> list[dict]:
    """C1 and C2 over every panel in the baseline. Pure apart from reads."""
    cache_dir = cache_dir or data_dir
    probes = PROBES if probes is None else probes
    results: list[dict] = []
    for etf, base in baseline["panels"].items():
        results.extend(compare_panel(etf, base, panel_from_disk(etf, data_dir),
                                     tolerance))
        names = probes.get(etf)
        if names:
            frame = load_probe_columns(
                cache_dir / f"prices_cache_{etf.lower()}.parquet", names)
            results.extend(probe_frame(etf, frame, names))
    return results


def report(results: list[dict], baseline: dict, verbose: bool = False) -> None:
    basis = baseline.get("basis") or {}
    print(f"Baseline: {basis.get('ref')} ({str(basis.get('commit'))[:12]}, "
          f"{basis.get('committed_at')}) -- {basis.get('subject')}")
    print(f"Adopted {baseline.get('adopted_at_utc')}; "
          f"{len(baseline.get('panels') or {})} panel(s); "
          f"tolerance {COVERAGE_TOLERANCE:g}\n")
    by_panel: dict[str, list[dict]] = {}
    for r in results:
        by_panel.setdefault(r["panel"], []).append(r)
    for etf, rows in by_panel.items():
        base = (baseline["panels"].get(etf) or {})
        cov = [r for r in rows if r["check"] == "coverage"]
        prb = [r for r in rows if r["check"] == "probe"]
        n_ok = sum(1 for r in cov if r["status"] == OK)
        worst = min((r for r in cov if r["current"] is not None),
                    key=lambda r: r["current"] - r["baseline"], default=None)
        worst_txt = (f"min delta {worst['current'] - worst['baseline']:+.4f} "
                     f"in {worst['year']}" if worst else "no comparable year")
        flags = sorted({r["status"] for r in cov if r["status"] != OK})
        head = (f"{etf:5} {n_ok}/{len(cov)} years within tolerance "
                f"({worst_txt})"
                + (f"  <-- {', '.join(flags)}" if flags else ""))
        print(head)
        if verbose or flags:
            for r in cov:
                tag = f"{r['status']:4}"
                print(f"        {r['year'] or '----'}  {tag}  {r['evidence']}")
        for r in prb:
            print(f"        probe {r['status']:4}  {r['evidence']}")
        print(f"        window {base.get('start_date')}..{base.get('end_date')}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--baseline", type=Path, default=BASELINE_PATH,
                    help="baseline JSON (default data/coverage_baseline.json)")
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR,
                    help="directory holding breadth_*.json (default data/)")
    ap.add_argument("--cache-dir", type=Path, default=None,
                    help="directory holding prices_cache_*.parquet "
                         "(default: --data-dir)")
    ap.add_argument("--tolerance", type=float, default=COVERAGE_TOLERANCE,
                    help=f"per panel-year fall that fails "
                         f"(default {COVERAGE_TOLERANCE:g})")
    ap.add_argument("--verbose", action="store_true",
                    help="print every panel-year, not only the flagged ones")
    ap.add_argument("--no-probes", action="store_true",
                    help="skip C2 (the cache probes)")
    ap.add_argument("--write-baseline", action="store_true",
                    help="REGENERATE the baseline from the panels committed "
                         "at --ref, then exit. A sign-off act, never part of "
                         "a refresh.")
    ap.add_argument("--ref", default=None,
                    help="commit whose committed panels become the baseline "
                         "(required with --write-baseline)")
    ap.add_argument("--why", default=None,
                    help="why that commit is the basis (required with "
                         "--write-baseline; recorded verbatim)")
    ap.add_argument("--force", action="store_true",
                    help="allow --write-baseline to overwrite an existing "
                         "baseline")
    args = ap.parse_args(argv)

    if args.write_baseline:
        if not args.ref or not args.why:
            print("--write-baseline needs both --ref and --why: the baseline "
                  "carries its provenance, and a baseline without a stated "
                  "reason is the thing this guard exists to prevent.")
            return 2
        if args.baseline.exists() and not args.force:
            print(f"{args.baseline} exists; re-baselining is a sign-off act. "
                  f"Pass --force if that sign-off has happened.")
            return 2
        basis = describe_ref(args.ref)
        panels: dict[str, dict] = {}
        missing: list[str] = []
        for etf in us_panels():
            doc = panel_from_git(etf, args.ref)
            if doc is None:
                missing.append(etf)
            else:
                panels[etf] = doc
        if missing:
            print(f"panels not committed at {args.ref}: {missing}; refusing "
                  f"to write a partial baseline")
            return 2
        doc = build_baseline(panels, basis, args.why)
        write_baseline(doc, args.baseline)
        print(f"wrote {args.baseline} from {basis['commit'][:12]} "
              f"({basis['committed_at']}): {len(panels)} panel(s)")
        for etf, entry in doc["panels"].items():
            years = entry["years"]
            first = min(years)
            print(f"  {etf:5} {entry['start_date']}..{entry['end_date']}  "
                  f"{first} coverage {years[first]['coverage']:.4f}")
        return 0

    try:
        baseline = load_baseline(args.baseline)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"[COVERAGE-DEPTH] cannot run: {args.baseline}: {e}")
        return 2

    results = run_checks(baseline, data_dir=args.data_dir,
                         cache_dir=args.cache_dir, tolerance=args.tolerance,
                         probes={} if args.no_probes else None)
    report(results, baseline, verbose=args.verbose)

    n_fail = sum(1 for r in results if r["status"] == FAIL)
    n_warn = sum(1 for r in results if r["status"] == WARN)
    n_skip = sum(1 for r in results if r["status"] == SKIP)
    print(f"\n{'=' * 72}")
    if n_fail:
        failed_panels = sorted({r["panel"] for r in results
                                if r["status"] == FAIL})
        print(f"{n_fail} FAIL, {n_warn} WARN, {n_skip} SKIP across "
              f"{len(baseline['panels'])} panel(s); failing: "
              f"{', '.join(failed_panels)}")
        print(
            "\n[COVERAGE-DEPTH] A US panel has lost delisted-name history "
            "against the filed basis, or its cache no longer carries the "
            "delisted-archive backfill. Breadth on those panels is on the "
            "survivor basis: sleeve A, the blend, the overlay, "
            "docs/index.html and the factsheet inherit it. Do NOT commit or "
            "publish this state. Restore this tree's "
            "data/prices_cache_*.parquet from a tree that carries the "
            "backfills (the main tree), re-run compute_breadth for the "
            "failing panels, then re-run the refresh. If the change is a "
            "SANCTIONED restatement, re-baseline with --write-baseline "
            "--force and file the sign-off."
        )
        return 1
    if n_warn:
        print(f"0 FAIL, {n_warn} WARN, {n_skip} SKIP: coverage above the "
              f"filed basis somewhere -- a restatement to file, not a fault")
        return 0
    print(f"All clear: {len(baseline['panels'])} panel(s) within "
          f"{COVERAGE_TOLERANCE:g} of the filed basis"
          + (f", {n_skip} probe(s) skipped for want of a cache"
             if n_skip else "") + ".")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
