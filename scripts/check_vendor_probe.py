"""Did this vendor-availability probe record anything usable?

WHY (CLAUDE.md: no unattended run without a guard).

The failure mode is not a crash. It is a probe that exits 0 having recorded
nothing usable — yfinance answers with empty frames, every last_bar comes back
null, the log grows a row, the workflow stays green, and the cadence question
stays unanswered. A run of those silently costs the measurement window the
probe exists to fill, and a hole is indistinguishable from "nothing to report"
after the fact.

So this fails the job when:
  - the log gained no row from this run,
  - the newest row is not actually from this run (a stale file re-read), or
  - every probed line came back with no last_bar (the network answered nothing).

A PARTIAL result deliberately PASSES. One venue answering while the other does
not is itself an observation about that venue, and it is the observation the
probe is for — refusing it would discard the very asymmetry being measured.
The row records which lines were empty, so the analysis can see it.

The workflow runs this BEFORE committing, so the log never gains rows the
guard has not endorsed.

RETRACTION TRIPWIRE (2026-08-29). A second failure mode, found the hard way.
A vendor never un-prints a close — except this one did:

    2026-08-28T21:41Z   SPY  last_bar 2026-08-28
    2026-08-29T05:25Z   SPY  last_bar 2026-08-27
    2026-08-29T12:12Z   SPY  last_bar 2026-08-27

yfinance served Friday's US closes on Friday evening and withdrew them
overnight, replacing each with a placeholder row carrying a NaN close. Every
downstream guard behaved correctly — the caches refused the shorter write, the
export held back the published series, capture integrity refused to publish —
but NOTHING said why, so two full local refreshes (about ninety minutes) were
spent re-running against data the vendor had taken back. The log already held
the evidence; nobody was looking at it.

A retraction does NOT fail the job. The row is a true observation and must be
committed — losing it would destroy the only record of the retraction. It is
reported loudly and, in CI, emitted as a step output the workflow emails on.
Use --fail-on-retraction to make it blocking for an operator who wants that.

Note the tripwire only sees what the probe measures, and the probe measures
last_bar as `close[ticker].dropna().index.max()` — the last bar WITH A CLOSE,
not the last row. That distinction is the whole of this bug: a placeholder row
dated Friday with a NaN close reads as Friday to anything checking the index
and as Thursday to anything checking the values.

Exit 0 = usable. Exit 1 = nothing usable, do not commit. Exit 2 = cannot tell.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG = PROJECT_ROOT / "data" / "vendor_availability_log.jsonl"

# How many earlier rows a retraction is judged against. The probe runs at
# 00/06/12/18 UTC, so twelve rows is about three days — long enough to span a
# weekend (the 2026-08-29 retraction was only visible across a Friday-evening
# row and a Saturday-morning one) and short enough that a bar legitimately
# absent for a week does not keep re-alerting forever.
RETRACTION_LOOKBACK_ROWS = 12

# Python datetime months are 1-indexed (January = 1). All dates in the log are
# ISO 'YYYY-MM-DD'; they are parsed to date objects rather than compared as
# strings so an ill-formed value raises here instead of mis-ordering silently.


def _as_date(v) -> date | None:
    if not v:
        return None
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def detect_retractions(history: list[dict],
                       lookback_rows: int = RETRACTION_LOOKBACK_ROWS
                       ) -> list[dict]:
    """Lines whose newest last_bar is EARLIER than one already recorded.

    Judged against the high-water mark over the lookback window rather than
    against the immediately preceding row: the probe's own partial results are
    legitimate, so a line can be absent from one row without that being a
    retraction, and comparing only to the previous row would miss a withdrawal
    that straddles such a gap.

    A line that had a bar and now reports none at all counts too — that is a
    total withdrawal, and it is the shape a whole-venue outage takes.
    """
    if len(history) < 2:
        return []
    window = history[-(lookback_rows + 1):]
    newest, earlier = window[-1], window[:-1]

    # Only COMPLETED sessions count towards the high-water mark. The probe
    # runs at 00/06/12/18 UTC, so two of every four fire while Xetra or the
    # NYSE is open, and an in-progress bar is served with the current date and
    # then legitimately reshaped as the session finishes. Judged naively, the
    # five Xetra lines all "retracted" 2026-08-28 between the Friday 08:48 and
    # Saturday 05:25 probes — 08:48 UTC is mid-session at Xetra, so that bar
    # was never a completed one and its removal is the vendor being correct.
    # Comparing against the row's own last_completed_session keeps the
    # tripwire on withdrawals of settled data, which is the only kind that
    # invalidates a refresh.
    high_water: dict[str, tuple[date, str]] = {}
    for row in earlier:
        stamp = row.get("probed_at_utc") or "?"
        for r in row.get("rows") or []:
            d = _as_date(r.get("last_bar"))
            if d is None:
                continue
            lcs = _as_date(r.get("last_completed_session"))
            if lcs is not None and d > lcs:
                continue  # in-progress bar, not yet a promise
            seen = high_water.get(r.get("ticker"))
            if seen is None or d > seen[0]:
                high_water[r.get("ticker")] = (d, stamp)

    out: list[dict] = []
    for r in newest.get("rows") or []:
        ticker = r.get("ticker")
        was = high_water.get(ticker)
        if was is None:
            continue
        now_bar = _as_date(r.get("last_bar"))
        if now_bar is not None and now_bar >= was[0]:
            continue
        out.append({
            "ticker": ticker,
            "now": r.get("last_bar"),
            "was": was[0].isoformat(),
            "was_seen_at": was[1],
            "venue": r.get("venue"),
        })
    return sorted(out, key=lambda x: x["ticker"])


def evaluate(log_path: Path, now_utc: datetime | None = None,
             max_age_minutes: int = 90) -> dict:
    now = now_utc or datetime.now(timezone.utc)
    if not log_path.exists():
        return {"ok": False, "undetermined": True,
                "summary": f"no log at {log_path}"}
    lines = [x for x in log_path.read_text(encoding="utf-8").splitlines() if x.strip()]
    if not lines:
        return {"ok": False, "undetermined": True, "summary": "log is empty"}
    try:
        latest = json.loads(lines[-1])
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "undetermined": True,
                "summary": f"newest row is not readable JSON: {exc!r}"}

    stamped = latest.get("probed_at_utc")
    try:
        when = datetime.fromisoformat(stamped)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        return {"ok": False, "undetermined": True,
                "summary": f"newest row has an unreadable timestamp: {stamped!r}"}

    age = now - when
    if age > timedelta(minutes=max_age_minutes):
        return {"ok": False, "undetermined": False, "rows": len(lines),
                "summary": (f"newest row is {int(age.total_seconds()//60)} min "
                            f"old ({stamped}) — this run appended nothing")}

    rows = latest.get("rows") or []
    if not rows:
        return {"ok": False, "undetermined": False, "rows": len(lines),
                "summary": "this run's row carries no probed lines"}
    served = [r for r in rows if r.get("last_bar")]
    empty = [r["ticker"] for r in rows if not r.get("last_bar")]
    if not served:
        return {"ok": False, "undetermined": False, "rows": len(lines),
                "summary": (f"all {len(rows)} probed line(s) came back empty — "
                            f"the network answered nothing, so this run "
                            f"measured nothing")}
    # History for the retraction tripwire. Rows that will not parse are
    # skipped rather than fatal: a corrupt older row must not stop this run's
    # observation being endorsed and committed, which is the guard's own rule
    # applied one level up.
    history: list[dict] = []
    for raw in lines[-(RETRACTION_LOOKBACK_ROWS + 1):]:
        try:
            history.append(json.loads(raw))
        except Exception:  # noqa: BLE001
            continue
    retractions = detect_retractions(history)

    summary = (f"{len(served)}/{len(rows)} lines served"
               + (f"; empty: {', '.join(empty)}" if empty else ""))
    if retractions:
        summary += (f"; RETRACTED: "
                    + ", ".join(f"{r['ticker']} {r['was']}->{r['now']}"
                                for r in retractions))
    return {
        "ok": True, "undetermined": False, "rows": len(lines),
        "served": len(served), "empty": empty,
        "retractions": retractions,
        "summary": summary,
    }


def _emit_outputs(retractions: list[dict], detail: str) -> None:
    """Append step outputs for the workflow's conditional email step.
    No-op outside GitHub Actions. Same heredoc convention as
    check_freshness_headroom._emit_outputs."""
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        return
    with open(out, "a", encoding="utf-8") as fh:
        fh.write(f"retracted={'true' if retractions else 'false'}\n")
        fh.write(f"retracted_count={len(retractions)}\n")
        summary = ", ".join(f"{r['ticker']} {r['was']}->{r['now'] or 'none'}"
                            for r in retractions) or "none"
        fh.write(f"retracted_summary={summary}\n")
        fh.write("retracted_detail<<VENDOR_RETRACTION_EOF\n")
        fh.write(detail.rstrip("\n") + "\n")
        fh.write("VENDOR_RETRACTION_EOF\n")


def _retraction_detail(retractions: list[dict]) -> str:
    if not retractions:
        return "No vendor retraction detected."
    lines = [
        "A vendor WITHDREW bars it had already served. The bar is not merely",
        "missing: it was recorded earlier in this log and is now gone, usually",
        "replaced by a row dated correctly whose close is NaN.",
        "",
    ]
    for r in retractions:
        lines.append(f"  {r['ticker']} ({r.get('venue') or 'venue unknown'}): "
                     f"last bar was {r['was']} (seen {r['was_seen_at']}), "
                     f"now {r['now'] or 'no bar at all'}")
    lines += [
        "",
        "What this means in practice: a local refresh_all.py run started now",
        "will rebuild panels one session SHORT and capture integrity will",
        "refuse to publish. That is the guards working, not a repo fault.",
        "Wait for the vendor to restore the bar before re-running; the",
        "committed caches and published series are protected meanwhile.",
        "",
        "Check on values, never on the index — a placeholder row carries the",
        "right date and a NaN close:",
        "  python -c \"import yfinance as yf; s=yf.Ticker('SPY').history("
        "period='5d', auto_adjust=True)['Close'].dropna(); print(s.index[-1]"
        ".date(), float(s.iloc[-1]))\"",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--log", default=str(LOG))
    ap.add_argument("--max-age-minutes", type=int, default=90)
    ap.add_argument("--fail-on-retraction", action="store_true",
                    help="exit 1 when a vendor retraction is detected "
                         "(default: report it, still endorse the row)")
    args = ap.parse_args(argv)

    r = evaluate(Path(args.log), max_age_minutes=args.max_age_minutes)
    print(f"Vendor probe guard — {r.get('rows', '?')} row(s) in the log")
    print(f"VERDICT: {r['summary']}")

    retractions = r.get("retractions") or []
    detail = _retraction_detail(retractions)
    if retractions:
        print("")
        print(f"RETRACTION TRIPWIRE — {len(retractions)} line(s) went "
              f"BACKWARDS since an earlier row:")
        for x in retractions:
            print(f"  {x['ticker']}: {x['was']} (seen {x['was_seen_at']}) "
                  f"-> {x['now'] or 'no bar at all'}")
    _emit_outputs(retractions, detail)

    if r.get("undetermined"):
        return 2
    if not r["ok"]:
        return 1
    # A retraction is a true observation: the row still gets committed unless
    # the operator explicitly asked otherwise.
    return 1 if (retractions and args.fail_on_retraction) else 0


if __name__ == "__main__":
    raise SystemExit(main())
