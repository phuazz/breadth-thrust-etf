"""Universe monitor — new ETF launches, closures, and overlap drift.

Advisory only. This script never changes a sleeve universe, never writes
to a price cache, and never touches the deployed engines. It produces a
report and a watchlist; every adoption decision remains a pre-registered
ablation done by hand (run_ws8_reit_overlap.py is the worked example).

What it does, once per run:

  1. Pulls the Nasdaq-traded symbol directory — every US-listed symbol
     with an ETF flag — and snapshots it.
  2. Diffs against the previous committed snapshot: LAUNCHES (new lines)
     and CLOSURES (lines that disappeared). The closure side matters as
     much as the launch side; FM was caught only after it had already
     been liquidated in January 2025.
  3. Screens each launch through the book-wide overlap gate in
     check_universe_candidates.py — the same two rules, the same deployed
     book, so the monitor and the manual gate cannot disagree.
  4. Re-runs the incumbent overlap audit, so a pair that drifts above the
     0.90 rule surfaces without anyone thinking to ask.

Why a launch monitor cannot produce an addition
-----------------------------------------------
The candidate gate requires five years of overlapping history for the
walk-forward, so a newly launched ETF is structurally ineligible for five
years. This monitor therefore emits a WATCHLIST with each candidate's
history-clearance date, not a buy list. The only two lines that ever
reached the book faster did it by proxying a longer-history underlying —
BTC-USD standing in for IBIT with a 25 bps expense drag, and 159801.SZ
chosen over 588200.SS for 7.0 years of history against 3.65. A theme
surfaced without such a proxy is a note, not a candidate.

Read the empirical prior before acting on anything this prints. Every
attempt to widen this book has cost Sharpe: Phase 5 sub-industries
-0.10 walk-forward, Phase 16 SLV -0.18 on sleeve B, Phase 17 KWEB -0.13,
the WS2 commodity thread killed at every level, the WS2 country sleeve
killed outright. Phase 25's two accepted adds came in at +0.001, which
the source comment itself describes as expecting no return uplift. The
monitor's job is coverage-gap detection and de-duplication, not catching
what has already run.

Guard layer (vault rule: no unattended agent without one)
---------------------------------------------------------
A silently truncated or stale catalogue reads as "no new launches" —
the failure mode indistinguishable from success. Three fatal checks run
before any diff is computed:

  * SCHEMA — the header must carry the exact columns this parser reads.
  * FRESHNESS — the file's own "File Creation Time" footer must parse and
    be no older than MAX_FEED_AGE_DAYS.
  * VOLUME — the ETF row count must be within RANGE_TOLERANCE of the
    previous snapshot. A feed that halves is a truncation, not an
    industry event, and must stop the run rather than report 2,700
    closures.

On the first run there is no prior snapshot, so the run establishes a
baseline and reports no diff. That is deliberate: without it the first
report would announce five and a half thousand launches.

Usage:
    python scripts/run_universe_monitor.py                 # report only
    python scripts/run_universe_monitor.py --write-snapshot  # and commit the new baseline
    python scripts/run_universe_monitor.py --max-screen 40   # cap the history fetch
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

import pandas as pd  # noqa: E402

from check_universe_candidates import (  # noqa: E402
    MIN_YEARS_HISTORY,
    OVERLAP_RULE_MAX_CORR,
    SIGNAL_GATE_MAX_CORR,
    deployed_panel,
    fetch_candidates,
    pairwise,
    weekly_returns,
    weekly_signal,
    _tag,
)

DATA = ROOT / "data"
SNAPSHOT = DATA / "etf_catalogue_snapshot.csv"
REPORT = DATA / "universe_monitor.json"

FEED_URL = "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqtraded.txt"
FEED_UA = "breadth-thrust-etf universe monitor (research, read-only)"
REQUIRED_COLUMNS = ("Symbol", "Security Name", "Listing Exchange", "ETF",
                    "Test Issue", "NextShares")

# Guard thresholds. Deliberately loose enough not to cry wolf on ordinary
# monthly churn, tight enough to catch a truncated or stale download.
MAX_FEED_AGE_DAYS = 7
RANGE_TOLERANCE = 0.15      # +/-15% of the prior ETF row count

# Liquidity floor for screening. The symbol directory carries no AUM, so
# median dollar volume over the trailing quarter is the stand-in. This is
# a PROXY, not assets: a thin fund with one large cross clears it, and a
# large fund in a quiet month may not. Stated, not tuned.
MIN_MEDIAN_DOLLAR_VOLUME = 2_000_000
ADV_WINDOW_DAYS = 63
DEFAULT_MAX_SCREEN = 60


class FeedIntegrityError(RuntimeError):
    """A capture-integrity check failed. Never degrade to a partial run."""


# ---------------------------------------------------------------------------
# Feed
# ---------------------------------------------------------------------------

def fetch_feed() -> tuple[list[dict], datetime]:
    req = urllib.request.Request(FEED_URL, headers={"User-Agent": FEED_UA})
    raw = urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "replace")
    return parse_feed(raw)


def parse_feed(raw: str) -> tuple[list[dict], datetime]:
    """Parse and schema-check the directory. Split from the network call so
    the integrity guards are testable without reaching the vendor."""
    lines = raw.splitlines()
    if len(lines) < 100:
        raise FeedIntegrityError(f"feed has only {len(lines)} lines")

    header = lines[0].split("|")
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        raise FeedIntegrityError(f"feed schema changed — missing {missing}")

    # Footer carries the vendor's own creation stamp: "File Creation Time:
    # MMDDYYYYHH:MM". Parsed with a date library, never by hand.
    footer = lines[-1]
    if "File Creation Time:" not in footer:
        raise FeedIntegrityError("feed footer missing File Creation Time")
    stamp_text = footer.split("File Creation Time:")[1].split("|")[0].strip()
    try:
        stamp = datetime.strptime(stamp_text, "%m%d%Y%H:%M")
    except ValueError as exc:
        raise FeedIntegrityError(
            f"unparseable File Creation Time {stamp_text!r}") from exc

    idx = {c: header.index(c) for c in header}
    rows = []
    for line in lines[1:-1]:
        f = line.split("|")
        if len(f) != len(header):
            continue
        if f[idx["ETF"]] != "Y" or f[idx["Test Issue"]] != "N":
            continue
        if f[idx["NextShares"]] == "Y":       # not an ordinary ETF wrapper
            continue
        rows.append({
            "symbol": f[idx["Symbol"]].strip(),
            "name": f[idx["Security Name"]].strip(),
            "exchange": f[idx["Listing Exchange"]].strip(),
        })
    if not rows:
        raise FeedIntegrityError("feed parsed to zero ETF rows")
    return rows, stamp


def check_freshness(stamp: datetime, today: date) -> None:
    age = today - stamp.date()
    if age > timedelta(days=MAX_FEED_AGE_DAYS):
        raise FeedIntegrityError(
            f"feed is {age.days} days old (stamped {stamp.date()}); refusing "
            "to report 'no new launches' from a stale file")
    if stamp.date() > today + timedelta(days=1):
        raise FeedIntegrityError(
            f"feed stamped in the future ({stamp.date()} vs {today})")


def check_volume(n_now: int, n_prev: int | None) -> None:
    if n_prev is None:
        return
    lo, hi = n_prev * (1 - RANGE_TOLERANCE), n_prev * (1 + RANGE_TOLERANCE)
    if not (lo <= n_now <= hi):
        raise FeedIntegrityError(
            f"ETF row count moved {n_prev} -> {n_now}, outside +/-"
            f"{RANGE_TOLERANCE:.0%}. A feed that jumps this far is a capture "
            "problem until proven otherwise; rerun, and if it is genuine, "
            "widen RANGE_TOLERANCE in the same commit as the evidence")


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

def load_snapshot() -> dict[str, dict] | None:
    if not SNAPSHOT.exists():
        return None
    with SNAPSHOT.open(newline="", encoding="utf-8") as fh:
        return {r["symbol"]: r for r in csv.DictReader(fh)}


def write_snapshot(rows: list[dict], stamp: datetime) -> None:
    """Snapshot with a provenance line that survives a round trip.

    The comment is written raw rather than through csv.writer: the text
    contains a comma, and csv.writer would quote the whole line, so it
    would start with a double quote instead of '#'. The loader's comment
    filter would then miss it, DictReader would adopt it as the header,
    and every row would parse with no 'symbol' key — a snapshot that
    silently reads as empty, which is precisely the input the volume
    guard is there to reject.
    """
    with SNAPSHOT.open("w", newline="", encoding="utf-8") as fh:
        fh.write(f"# derived from nasdaqtrader.com symbol directory; "
                 f"file creation time {stamp.isoformat()}\n")
        w = csv.DictWriter(fh, fieldnames=["symbol", "name", "exchange"])
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: r["symbol"]))


def load_snapshot_rows() -> tuple[dict[str, dict] | None, int | None]:
    """Snapshot keyed by symbol, skipping the provenance comment line."""
    if not SNAPSHOT.exists():
        return None, None
    text = SNAPSHOT.read_text(encoding="utf-8").splitlines()
    body = [ln for ln in text if not ln.startswith("#")]
    reader = csv.DictReader(body)
    if not reader.fieldnames or "symbol" not in reader.fieldnames:
        raise FeedIntegrityError(
            f"{SNAPSHOT.name} does not parse as a snapshot — header is "
            f"{reader.fieldnames}. Refusing to treat an unreadable baseline "
            "as an empty one, which would report every listed ETF as a "
            "launch.")
    rows = {r["symbol"]: r for r in reader if r.get("symbol")}
    if not rows:
        raise FeedIntegrityError(f"{SNAPSHOT.name} parsed to zero rows")
    return rows, len(rows)


# ---------------------------------------------------------------------------
# Screening
# ---------------------------------------------------------------------------

def screen_launches(symbols: list[str], panel: pd.DataFrame,
                    sleeves: dict[str, list[str]],
                    max_screen: int) -> tuple[list[dict], int]:
    """Run new lines through the same two rules the manual gate applies."""
    if not symbols:
        return [], 0
    capped = sorted(symbols)[:max_screen]
    dropped = len(symbols) - len(capped)
    print(f"  screening {len(capped)} of {len(symbols)} launches"
          + (f" — {dropped} NOT screened this run (--max-screen cap)"
             if dropped else ""))

    px = fetch_candidates(capped)
    if not len(px.columns):
        return [], dropped

    # A symbol can appear as a launch AND already be a deployed line — a
    # recycled ticker, or a snapshot that lost a row. Screening it against
    # its own price series would score a meaningless 1.000, and the
    # duplicate column name breaks the per-column maths outright.
    overlap = [t for t in px.columns if t in panel.columns]
    if overlap:
        print(f"  NOTE: {overlap} already deployed — screening against the "
              "rest of the book, excluding their own lines")
        panel = panel.drop(columns=overlap)

    book = list(panel.columns)
    full = pd.concat([panel, px], axis=1).sort_index()
    wret = weekly_returns(full)
    try:
        wsig = weekly_signal(full)
    except RuntimeError:
        wsig = None

    out = []
    for sym in px.columns:
        s = px[sym].dropna()
        if s.empty:
            continue
        first, last = s.index.min(), s.index.max()
        years = (last - first).days / 365.25
        clears_on = (first + pd.DateOffset(years=MIN_YEARS_HISTORY)).date()

        adv = None
        # Dollar-volume proxy needs volume, which fetch_candidates does not
        # carry; approximate with close * a separate light fetch only when
        # the line is otherwise interesting. Reported as unavailable rather
        # than guessed when it cannot be computed.
        rec = {
            "symbol": sym,
            "first_price": str(first.date()),
            "last_price": str(last.date()),
            "years_history": round(years, 2),
            "clears_history_gate_on": str(clears_on),
            "median_dollar_volume": adv,
        }
        ret_pairs = pairwise(wret[sym], wret[book]) if sym in wret else []
        rec["top_return_corr"] = [
            {"vs": _tag(t, sleeves), "corr": round(v, 3)}
            for t, v in ret_pairs[:3]]
        if wsig is not None and sym in wsig.columns:
            sig_pairs = pairwise(wsig[sym], wsig[[c for c in book
                                                  if c in wsig.columns]])
            rec["top_signal_corr"] = [
                {"vs": _tag(t, sleeves), "corr": round(v, 3)}
                for t, v in sig_pairs[:3]]
        else:
            rec["top_signal_corr"] = []
            rec["signal_note"] = (
                f"fewer than 200 sessions of history — the ranked signal is "
                f"undefined, so Rule 1 cannot be evaluated yet")

        r_max = ret_pairs[0][1] if ret_pairs else None
        s_max = (rec["top_signal_corr"][0]["corr"]
                 if rec["top_signal_corr"] else None)
        if r_max is not None and r_max > OVERLAP_RULE_MAX_CORR:
            rec["verdict"] = "REJECT — duplicates a deployed line"
        elif s_max is not None and s_max >= SIGNAL_GATE_MAX_CORR:
            rec["verdict"] = "REJECT — signal too close to a deployed line"
        elif years < MIN_YEARS_HISTORY:
            rec["verdict"] = f"WATCH — history clears {clears_on}"
        else:
            rec["verdict"] = "REVIEW — clears both rules on today's data"
        out.append(rec)
    return out, dropped


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write-snapshot", action="store_true",
                    help="update the committed baseline after reporting")
    ap.add_argument("--max-screen", type=int, default=DEFAULT_MAX_SCREEN,
                    help="cap how many launches get a history fetch")
    ap.add_argument("--today", default=None,
                    help="ISO date override for the freshness check (testing)")
    args = ap.parse_args()

    today = (date.fromisoformat(args.today) if args.today
             else datetime.now(timezone.utc).date())
    print(f"Universe monitor — {today.isoformat()} "
          f"({today.strftime('%A')})")

    prev, n_prev = load_snapshot_rows()
    rows, stamp = fetch_feed()
    print(f"  feed stamped {stamp.isoformat()}, {len(rows)} ETF lines")

    check_freshness(stamp, today)
    check_volume(len(rows), n_prev)
    print("  capture-integrity checks passed (schema, freshness, volume)")

    now = {r["symbol"]: r for r in rows}
    if prev is None:
        print("\n  NO PRIOR SNAPSHOT — establishing a baseline. No diff is "
              "possible on a first run, and reporting one would announce "
              f"{len(rows)} launches that are not launches.")
        launches, closures = [], []
        baseline = True
    else:
        launches = sorted(set(now) - set(prev))
        closures = sorted(set(prev) - set(now))
        baseline = False
        print(f"\n  {len(launches)} launch(es), {len(closures)} closure(s) "
              f"since the last snapshot")

    panel, sleeves = deployed_panel()
    held = {t for t in panel.columns}
    closed_and_held = sorted(t for t in closures if t in held)
    if closed_and_held:
        print(f"  *** {len(closed_and_held)} CLOSED LINE(S) ARE HELD BY THE "
              f"BOOK: {closed_and_held} ***")

    screened, dropped = screen_launches(launches, panel, sleeves,
                                        args.max_screen)
    for rec in sorted(screened, key=lambda r: r["verdict"]):
        print(f"  {rec['symbol']:8s} {rec['verdict']}")

    report = {
        "computed_at_utc": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "run_date": today.isoformat(),
        "feed": {"url": FEED_URL, "file_creation_time": stamp.isoformat(),
                 "etf_lines": len(rows), "prior_etf_lines": n_prev},
        "baseline_run": baseline,
        "launches": launches,
        "closures": closures,
        "closures_held_by_book": closed_and_held,
        "screened": screened,
        "launches_not_screened": dropped,
        "advisory_only": (
            "This report changes nothing. A universe addition requires a "
            "pre-registered ablation against the deployed blend; see "
            "reviews/2026-08-05_ws8_reit-dual-coverage.docx for the format "
            "and for how rarely such a change has been worth making."),
    }
    REPORT.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    print(f"\n  wrote {REPORT.relative_to(ROOT)}")

    if args.write_snapshot:
        write_snapshot(rows, stamp)
        print(f"  wrote {SNAPSHOT.relative_to(ROOT)} "
              "(commit it so the next diff is against a reviewed baseline)")
    elif baseline:
        print("  NOTE: rerun with --write-snapshot to establish the baseline")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
