"""DEPLOYED publisher for the Norgate breadth feed.

STATUS. Stage 2 is LIVE: the scheduled job runs it with --commit-path --push
and it publishes data/gate_states_norgate.json to origin/main. The fleet
row 'norgate gate-states publish' (scripts/fleet_watch.json, max_age 48h)
watches that output. See reviews/2026-07-17_norgate-feed-migration.md.

This header read "PROPOSAL ARTEFACT — NOT WIRED into any pipeline, workflow,
or scheduled task" until 2026-09-02, long after Stage 2 went ahead. Nobody
was misled into breaking anything, but a reader auditing what touches the
committed data/ tree would have cleared this file on its own say-so. Prose
goes stale exactly like a number does and is not covered by any test —
the same failure as the WS18 execution-timing surfaces.

What it does per run:
  1. Pulls #SPX%MA50 from the local NDU (full history, padding NONE).
  2. Freshness: reads the actual last BAR date — never last_quoted_date,
     which NDU leaves unset on market-closed days (event-studies
     feed-gate lesson, 2026-07-04). If the last bar is older than
     STALE_TRADING_DAYS trading days, exits WITHOUT writing (the
     downstream staleness cap and scrape fallback then govern).
  3. Runs the DEPLOYED hysteresis (_compute_states imported from
     run_risk_overlay, OFF 0.20 / ON 0.50) over the FULL vendor history.
  4. Writes the DERIVED STATE SERIES plus the CURRENT BREADTH SCALAR. The
     licence line is between a derived value and the vendor's series, not
     between states and levels: norgate_prices.py permits published derived
     values and names this percentage as its example, and risk_overlay.json
     has always committed it. So `series` carries states only — committing
     the daily levels would republish #SPX%MA50 — while current_breadth
     carries one number, as at last_bar.
       Stage 0/1 -> data_local/gate_states_norgate.preview.json
       Stage 2   -> data/gate_states_norgate.json   (flag --commit-path)
  5. Divergence check: compares its current state against the scrape-fed
     state in data/risk_overlay.json and prints/flags any mismatch or a
     level residing in the 0.20/0.50 threshold zone.

WHY THE SCALAR IS PUBLISHED (added 2026-09-02). Two reasons, the first
being the one that matters.

FEED CONSISTENCY. On the norgate-local feed the deployed pipeline takes its
STATES from this file and its LEVEL from the CSP1 scrape (run_risk_overlay
:714) — not because anyone chose a mixed basis, but because this file
carried no level to take. run_risk_overlay's own NOTE anticipates the
consequence: a level may "sit on the other side of the threshold" from the
state. Measured 2026-09-01: scrape 0.5060 vs vendor 0.4573, a 4.87pp gap
straddling the 0.50 re-engage threshold. Any consumer quoting a breadth
level was therefore measuring its distance-to-threshold on a series that
does not decide the gate. The 2026-07-17 migration review recorded exactly
this as an open residual — 60 zone-side disagreements at 0.50, "agreement
at a regime flip or inside the threshold zone: NOT EVIDENCED... recorded as
a residual, not as a pass". This closes the half of it that was a plumbing
gap; the underlying feed disagreement is a separate question.

TIMELINESS. Measured against this file's own 17,492-session state history,
reading the gate k sessions late is wrong on 1.03% of sessions at k=1,
3.09% at k=3, 5.11% at k=5 — and the wrong days are not scattered: at k=3
they number exactly 3 x 180 flips, so every one sits in the window straight
after a regime change. Staleness here is harmless except precisely when the
gate turns, which is the worst error profile a risk trigger can have.

CAVEAT for anyone comparing the two files: a difference between this
current_breadth and risk_overlay's is EXPECTED and is a feed difference,
not a staleness one. The publisher's divergence check below compares STATES
and a narrow zone band only, so a level gap of this size passes it silently.

Run:    python scripts/publish_norgate_breadth.py [--commit-path]
"""
from __future__ import annotations

import argparse
import datetime as dt  # Python datetime: months are 1-indexed
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATA_LOCAL = ROOT / "data_local"
sys.path.insert(0, str(ROOT / "scripts"))

from run_risk_overlay import (_compute_states, OFF_THRESHOLD,  # noqa: E402
                              ON_THRESHOLD)

SYMBOL = "#SPX%MA50"
STALE_TRADING_DAYS = 3

# State labels MUST match run_risk_overlay's deployed naming ("RISK_ON" /
# "RISK_OFF") exactly: the Stage-1 divergence check compares state strings
# against data/risk_overlay.json, and Stage-2 consumers display
# current_state. The original "DERISK" label would have printed a false
# FLAG on every both-feeds-OFF soak day (fixed 2026-07-17, pre-soak,
# before the first scheduled fire; review addendum §9).
STATE_LABELS = {1.0: "RISK_ON", 0.0: "RISK_OFF"}


def _git_publish(path: Path) -> None:
    """Stage-2 publication: commit and push ONLY the states file.

    Explicit single-file ``git add`` (never ``-A`` — interactive sessions
    share this working tree), commit, rebase, push. Fail-soft throughout:
    on any git failure the file is still written locally and the message
    says so; CI keeps consuming the scrape path until the next successful
    push, which is the designed degradation ladder (review §5). A refusal
    caused by unrelated dirty files self-heals on a later run — local
    commits accumulate and push together, always inside the 10-day cap.
    """
    import subprocess

    rel = str(path.relative_to(ROOT)).replace("\\", "/")

    def run(*cmd: str) -> subprocess.CompletedProcess:
        return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)

    if not run("git", "status", "--porcelain", "--", rel).stdout.strip():
        print("git publish: states file unchanged — nothing to push")
        return
    for cmd in (
        ("git", "add", "--", rel),
        ("git", "commit", "-m",
         f"Norgate gate-states publish (last bar per file; "
         f"run {dt.date.today().isoformat()})"),
        ("git", "pull", "--rebase", "origin", "main"),
        ("git", "push", "origin", "main"),
    ):
        res = run(*cmd)
        if res.returncode != 0:
            print(f"git publish FAILED at `{' '.join(cmd[:3])}`: "
                  f"{(res.stderr or res.stdout).strip()[:300]} — file "
                  f"written locally; scrape fallback governs in CI")
            if cmd[1] == "pull":
                run("git", "rebase", "--abort")
            return
    print(f"git publish: pushed {rel}")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit-path", action="store_true",
                    help="write to data/ (Stage 2, post-approval only)")
    ap.add_argument("--push", action="store_true",
                    help="git add/commit/push the states file after "
                         "writing (Stage 2; single-file add, fail-soft; "
                         "requires --commit-path)")
    args = ap.parse_args()

    import norgatedata as nd
    if not nd.status():
        print("NDU not running — exiting without writing (fallback governs)")
        return 1
    df = nd.price_timeseries(
        SYMBOL,
        stock_price_adjustment_setting=nd.StockPriceAdjustmentType.TOTALRETURN,
        padding_setting=nd.PaddingType.NONE,
        timeseriesformat="pandas-dataframe",
    )
    close = df["Close"]
    scale = 100.0 if close.max() > 1.5 else 1.0
    breadth = close / scale

    # freshness on the ACTUAL last bar (never last_quoted_date)
    last_bar = breadth.index[-1].date()
    today = dt.date.today()
    # trading-day distance approximated by calendar days net of weekends;
    # holidays intentionally lenient (a holiday gap must not false-alarm)
    cal_gap = (today - last_bar).days
    if cal_gap > STALE_TRADING_DAYS + 4:  # 3 trading days + weekend slack
        print(f"stale: last bar {last_bar}, {cal_gap} calendar days old — "
              "exiting without writing")
        return 1

    states = _compute_states(breadth, OFF_THRESHOLD, ON_THRESHOLD)
    out = {
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(
            timespec="seconds"),
        "source": "norgate-local #SPX%MA50 (derived states plus the current "
                  "scalar; the vendor SERIES is never committed — licence)",
        "state_machine": "run_risk_overlay._compute_states "
                         f"(off {OFF_THRESHOLD}, on {ON_THRESHOLD})",
        "last_bar": str(last_bar),
        "current_state": STATE_LABELS[float(states.iloc[-1])],
        # The CURRENT breadth level, as at last_bar. One scalar, not a
        # series: norgate_prices.py's licence note permits published derived
        # values and names this very percentage as the example. What the
        # licence forbids is committing the daily history, which would
        # republish #SPX%MA50 wholesale — so `series` above stays states-only
        # and this stays a single number.
        #
        # WHY IT IS NEEDED. risk_overlay.json also publishes a
        # current_breadth, but on the norgate-local feed that number is NOT
        # from this feed: run_risk_overlay takes its STATES from the file
        # this script writes and its LEVEL from the CSP1 scrape series
        # (run_risk_overlay.py:714), precisely because this file carried no
        # level to take. Its own NOTE (~line 567) anticipates the result —
        # "a flip date may therefore annotate a scrape level that sits on
        # the other side of the threshold". That is live as at 2026-09-01:
        # scrape 0.5060 against vendor 0.4573, a 4.87pp gap straddling the
        # 0.50 re-engage threshold. So every consumer quoting a breadth
        # level, including the daily research digest and its "buffer to
        # de-risk", has been measuring distance-to-threshold on a series
        # that does not decide the gate. Emitting the level here lets a
        # consumer read the state and the level from ONE feed.
        "current_breadth": float(breadth.iloc[-1]),
        "series": {
            "dates": [str(d.date()) for d in states.index],
            "state": [int(s) for s in states.values],
        },
    }
    if args.commit_path:
        path = DATA / "gate_states_norgate.json"
    else:
        DATA_LOCAL.mkdir(exist_ok=True)
        path = DATA_LOCAL / "gate_states_norgate.preview.json"
    path.write_text(json.dumps(out, indent=1), encoding="utf-8")

    # Stage-1 divergence check vs the deployed scrape-fed state
    try:
        ro = json.loads((DATA / "risk_overlay.json").read_text(
            encoding="utf-8"))
        dep_state = str(ro.get("current_state", "")).upper()
        cand_state = out["current_state"]
        zone = (abs(breadth.iloc[-1] - OFF_THRESHOLD) < 0.02
                or abs(breadth.iloc[-1] - ON_THRESHOLD) < 0.02)
        # Exact string equality — the substring test (`not in`) plus the
        # old "DERISK" label could never match deployed "RISK_OFF".
        flag = (bool(dep_state) and cand_state != dep_state) or zone
        print(f"divergence check: deployed={dep_state or 'n/a'} "
              f"norgate={cand_state} zone={zone} -> "
              f"{'FLAG' if flag else 'ok'}")
    except Exception as exc:
        print(f"divergence check skipped ({type(exc).__name__})")

    print(f"wrote {path.relative_to(ROOT)} (last bar {last_bar}, "
          f"state {out['current_state']})")
    if args.push:
        if not args.commit_path:
            print("--push ignored: only the data/ commit path is ever "
                  "pushed (data_local/ previews are licence-guarded)")
        else:
            _git_publish(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
