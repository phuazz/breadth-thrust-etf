"""Pre-trade readiness check — is the instruction built before the next fill?

WHY THIS EXISTS, and why it is not check_factsheet_gate.py.

Since WS18 (2026-08-22), the book ranks on Friday's close and normally fills
in the Monday CLOSING auctions. The instruction is produced by the local weekend
refresh pair; Sunday 09:00 SGT is intended to capture sleeve D's settled Friday
Xetra close. Settlement is checked, not assumed. It cannot run in CI because
the per-constituent parquet caches are gitignored. If the weekend pair does not land, nothing is
built, and this check reports it before Monday's trade. The existing Sunday
09:00 UTC factsheet check is a reconciliation question, not a pre-trade one.

This asks the pre-trade question instead: does the committed panel reach the
session the decision reads? It is deliberately a different question from
publishability, because week_final_anchor is unanswerable mid-week — it points
at the previous week by design, so a check built on it would pass on a Friday
morning while the panel sat six days stale.

Every pre-trade observation must match the required completed venue session
exactly. The local refresh guard's reach-at-least predicate is not sufficient
here: a future-dated observation is invalid, not ready. Full readiness also
requires the committed instruction and every A/D source panel.

Sunday's review checkpoint records pending refreshes and explicit HOLDs without
emailing. The later deadline checkpoint escalates both; invalid data alerts in
either phase. Neither phase depends on a local success marker or claims that a
refresh is running. A HOLD is never relabelled as a missing instruction.

WHAT IT DOES NOT DO. It cannot rebuild the panel — only the operator's machine
can. It reports, and it never blocks: exit is always 0, and an internal error
sets warn=true, so the failure mode is a spurious email rather than silence.

Outputs (stdout always; appended to $GITHUB_OUTPUT when set, which is what the
conditional email step in the workflow reads):
  warn    'true' | 'false' — email trigger
  status  'ready' | 'pending' | 'hold' | 'not_ready' | 'error'
  tag     'OK' | 'REVIEW' | 'HOLD' | 'PRE-TRADE' | 'DATA-ERROR' | 'WARN'
  summary one line, used as the email subject tail
  detail  multi-line block, used as the email body

Python datetime months are 1-indexed (January = 1).

Usage:
    python scripts/check_pretrade_ready.py
    python scripts/check_pretrade_ready.py --now 2026-09-13T06:00:00Z   # test
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from nyse_sessions import last_completed_session  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PANEL = REPO_ROOT / "data" / "breadth_csp1.json"


def build_report(panel_path: Path, now_utc: datetime) -> dict:
    """Decide readiness and compose the operator-facing text."""
    try:
        blob = json.loads(panel_path.read_text(encoding="utf-8"))
        panel_end = date.fromisoformat(blob["end_date"])
    except Exception as exc:  # noqa: BLE001
        # Fail toward alerting: an unreadable panel is not evidence of health.
        return {
            "warn": "true", "status": "error", "tag": "WARN",
            "summary": f"panel unreadable ({type(exc).__name__})",
            "detail": (
                f"Could not read {panel_path.name} to decide pre-trade "
                f"readiness: {exc!r}\n\n"
                "Treating this as NOT ready. Check the panel file and the "
                "last local refresh before trading."),
        }

    needed = last_completed_session(now_utc)
    if panel_end > needed:
        return {"warn": "true", "status": "error", "tag": "DATA-ERROR",
                "summary": f"future-dated panel: {panel_end}, requires {needed}",
                "detail": "The panel is later than the last completed session; do not use it."}
    ready = panel_end == needed
    stale_days = (needed - panel_end).days

    if ready:
        return {
            "warn": "false", "status": "ready", "tag": "OK",
            "summary": f"panel current to {panel_end.isoformat()}",
            "detail": (
                f"Broad-market capture PASSED at {now_utc.isoformat()}.\n"
                f"  panel end_date          : {panel_end.isoformat()}\n"
                f"  last completed session  : {needed.isoformat()}\n\n"
            "The broad-market panel reaches the required session. "
            "The instruction must also be checked across all four sleeves."),
        }

    return {
        "warn": "true", "status": "not_ready", "tag": "PRE-TRADE",
        "summary": (f"panel at {panel_end.isoformat()}, needs "
                    f"{needed.isoformat()} ({stale_days}d behind)"),
        "detail": (
            f"Pre-trade check FAILED at {now_utc.isoformat()}.\n"
            f"  panel end_date          : {panel_end.isoformat()}\n"
            f"  last completed session  : {needed.isoformat()}\n"
            f"  behind by               : {stale_days} calendar days\n\n"
            "The committed panel does not reach the required completed session. "
            "Current capture for the next instruction is not established.\n\n"
            "Possible causes include an incomplete local refresh, missing "
            "vendor data, or a completed refresh that has not been pushed. "
            "The scheduled task starts at 09:00 "
            "SGT, retries hourly until early afternoon SGT, and starts when "
            "the machine becomes available.\n\n"
            "To act manually:\n"
            "  use the dedicated automation clone, not an interactive tree\n"
            "  python scripts/scheduled_refresh.py     (soak: validates, no push)\n"
            "  then review and push, which triggers the rest of the chain.\n\n"
            "The next fill is normally in the Monday CLOSING auctions: Xetra "
            "23:30 SGT on Monday evening (sleeve D), US 04:00 SGT on Tuesday "
            "(sleeves A/B/C), one hour later in winter. Check the exchange "
            "calendar for a holiday displacement. Market-on-close orders must "
            "be in before 15:50 New York time. Do not trade on the stale card."),
    }


def build_book_report(panel_path: Path, now_utc: datetime, phase: str = "deadline") -> dict:
    """Check the committed instruction and every sleeve, using its own venue.

    The broad-market panel is a capture alarm, not proof that the book exists.
    No cache downloads occur in this CI check. Python months are 1-indexed.
    """
    import pandas as pd
    import pandas_market_calendars as mcal
    from session_bounds import last_completed_session_on
    from etf_registry import UNIVERSE_ETFS, UNIVERSE_EUROPE_SECTORS

    if phase not in ("review", "deadline"):
        raise ValueError("phase must be review or deadline")
    errors, pending, holds = [], [], []
    held_names = set()
    data_dir = panel_path.parent

    def observation(value, expected, label, allow_missing=False):
        if value is None and allow_missing:
            return False
        try:
            observed = date.fromisoformat(value)
        except (TypeError, ValueError):
            errors.append(f"{label}: missing or invalid observation date")
            return False
        if observed > expected:
            errors.append(f"{label}: future-dated {observed}, requires {expected}")
        return observed == expected

    try:
        book = json.loads((data_dir / "live_targets.json").read_text(encoding="utf-8"))
        sleeves = book.get("sleeves") or []
        if len(sleeves) != 4 or {s.get("sleeve") for s in sleeves} != set("ABCD"):
            errors.append("instruction must contain exactly sleeves A, B, C and D")
        if not isinstance(book.get("targets_final"), bool):
            errors.append("targets_final must be a boolean")
        built = datetime.fromisoformat(book["computed_at_utc"].replace("Z", "+00:00"))
        if built.tzinfo is None or built > now_utc:
            raise ValueError("instruction build timestamp is naive or in the future")
        for sl in sleeves:
            name = sl.get("sleeve")
            venue = "XETR" if name == "D" else "NYSE"
            cal = mcal.get_calendar(venue)
            last = last_completed_session_on(cal, now_utc)
            if last is None:
                raise ValueError(f"no completed session for {venue}")
            expected = last.date()
            close = cal.schedule(start_date=last, end_date=last).iloc[0]["market_close"]
            fresh_build = pd.Timestamp(built) >= close
            if not fresh_build:
                pending.append(f"sleeve {name}: instruction predates the required {expected} close")
            decision_current = observation(sl.get("decision_session"), expected,
                                           f"sleeve {name} decision", allow_missing=sl.get("status") == "HOLD")
            if sl.get("status") == "HOLD":
                evaluated_current = observation(sl.get("last_completed_session"), expected,
                                                f"sleeve {name} evaluated session")
                if not sl.get("reason"):
                    errors.append(f"sleeve {name}: HOLD has no recorded reason")
                # A HOLD must carry the held book, not stale-rank BUY/SELL lines.
                lines = book.get("lines")
                if not isinstance(lines, list):
                    errors.append(f"sleeve {name}: HOLD has no position-line evidence")
                else:
                    import math
                    for line in lines:
                        if line.get("sleeve") != name:
                            continue
                        held, target, delta = (float(line[k]) for k in ("held", "target", "delta"))
                        if not all(math.isfinite(x) for x in (held, target, delta)) or held != target or delta != 0:
                            errors.append(f"sleeve {name}: HOLD changes a position")
                if fresh_build and evaluated_current:
                    held_names.add(name)
                    holds.append(f"sleeve {name}: HOLD — {sl.get('reason')}; retain existing holdings")
                else:
                    pending.append(f"sleeve {name}: old HOLD has not been evaluated for {expected}")
            elif sl.get("status") == "READY":
                if not decision_current:
                    pending.append(f"sleeve {name}: decision {sl.get('decision_session')}, requires {expected}")
            else:
                errors.append(f"sleeve {name}: unknown status {sl.get('status')}")
            if sl.get("decision_session_for_fill") != str(expected):
                pending.append(f"sleeve {name}: next fill is not yet decided by {expected}")
            fill = pd.Timestamp(sl.get("fill_date"))
            if pd.isna(fill) or fill.date() < now_utc.date():
                pending.append(f"sleeve {name}: missing or expired fill date")
            else:
                schedule = cal.schedule(start_date=last + pd.Timedelta(days=1), end_date=fill)
                if not len(schedule) or schedule.index[-1].date() != fill.date():
                    errors.append(f"sleeve {name}: fill is not a future exchange session")
                elif len(schedule) != 1:
                    pending.append(f"sleeve {name}: fill is not the next exchange session")
        if holds and book.get("targets_final") is True:
            errors.append("instruction declares targets_final despite a HOLD sleeve")
        elif not holds and book.get("targets_final") is False:
            pending.append("instruction remains provisional")
    except FileNotFoundError:
        pending.append("instruction/input verification failed: live_targets.json has not been built")
    except Exception as exc:
        errors.append(f"instruction/input verification failed: {type(exc).__name__}: {exc}")

    # Inspect available panels even when the instruction is absent or invalid.
    # Missing build output must not mask a future-dated or malformed source.
    try:
        for name, universe, venue in (("A", UNIVERSE_ETFS, "NYSE"),
                                      ("D", UNIVERSE_EUROPE_SECTORS, "XETR")):
            last = last_completed_session_on(mcal.get_calendar(venue), now_utc)
            paths = {data_dir / f"breadth_{etf.lower()}.json" for etf in universe}
            if name == "A":
                paths.add(panel_path)
            for path in sorted(paths):
                try:
                    panel = json.loads(path.read_text(encoding="utf-8"))
                except FileNotFoundError:
                    pending.append(f"missing source panel: {path.name}")
                    continue
                current = observation(panel.get("end_date"), last.date(), path.name)
                if not current and name not in held_names:
                    pending.append(f"{path.name}: ends {panel.get('end_date')}, requires {last.date()}")
    except Exception as exc:
        errors.append(f"source-panel verification failed: {type(exc).__name__}: {exc}")
    if errors:
        status, tag, warn, summary = "error", "DATA-ERROR", True, "invalid pre-trade inputs — review required"
    elif pending:
        status, tag, warn = ("pending", "REVIEW", False) if phase == "review" else ("not_ready", "PRE-TRADE", True)
        summary = "refresh not yet verified" if phase == "review" else "deadline check: current instruction unavailable"
    elif holds:
        status, tag, warn = "hold", "HOLD", phase == "deadline"
        summary = "instruction contains HOLD sleeves — retain their existing holdings"
    else:
        status, tag, warn, summary = "ready", "OK", False, "all four sleeves ready for the next fill"
    detail = [f"Pre-trade {phase} checkpoint at {now_utc.isoformat()}: {summary}."]
    for label, values in (("Data errors", errors), ("Pending verification", pending), ("Recorded HOLDs", holds)):
        if values:
            detail.append(label + ":\n" + "\n".join(dict.fromkeys(values)))
    if pending:
        detail.append("A local refresh may be incomplete or not yet published; this check cannot observe its process state. "
                      "Inspect the dedicated automation clone's run log before starting another refresh.")
    if phase == "review":
        detail.append("This is a progress checkpoint, not a completion deadline. The later deadline check runs even if no refresh succeeds.")
    if errors or pending or holds:
        detail.append("Do not create new orders for an unverified or HOLD sleeve. Review live_targets.json and the named evidence.")
    return {"warn": str(warn).lower(), "status": status, "tag": tag,
            "summary": summary, "detail": "\n\n".join(detail)}


def write_github_output(report: dict) -> None:
    """Append step outputs for the workflow's conditional email step.
    No-op outside GitHub Actions."""
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        return
    with open(out, "a", encoding="utf-8") as fh:
        for key in ("warn", "status", "tag", "summary"):
            fh.write(f"{key}={report[key]}\n")
        fh.write("detail<<PRETRADE_DETAIL_EOF\n")
        fh.write(report["detail"].rstrip("\n") + "\n")
        fh.write("PRETRADE_DETAIL_EOF\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--panel", default=str(DEFAULT_PANEL))
    ap.add_argument("--now", default=None,
                    help="ISO-8601 UTC instant to evaluate at, for testing.")
    ap.add_argument("--phase", choices=("review", "deadline"), default="deadline",
                    help="Review suppresses pending/HOLD email; invalid data still alerts. Default: deadline.")
    args = ap.parse_args(argv)

    if args.now:
        now = datetime.fromisoformat(args.now.replace("Z", "+00:00"))
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
    else:
        now = datetime.now(timezone.utc)

    try:
        report = build_book_report(Path(args.panel), now, phase=args.phase)
    except Exception as exc:  # noqa: BLE001
        report = {
            "warn": "true", "status": "error", "tag": "WARN",
            "summary": f"checker error ({type(exc).__name__})",
            "detail": f"check_pretrade_ready itself failed: {exc!r}",
        }

    print(f"[{report['tag']}] {report['summary']}")
    print(report["detail"])
    write_github_output(report)
    # Never blocks: the workflow decides what to do with `warn`.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
