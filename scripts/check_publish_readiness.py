"""Is this week's data fit to publish? One command, one verdict.

The factsheet email is gated on an explicit release marker, which raises
the obvious question: how do you know when to release? Answering that from
memory means remembering which of six scripts to run and what each one's
silence means. This runs them, adds the checks specific to PUBLICATION as
opposed to refresh correctness, and prints a single verdict.

It is deliberately conservative about what it claims. A PASS here means
every mechanical check the repository knows how to make came back clean —
not that the numbers are economically sensible, which no script can judge.
The review items at the end are the things a person still has to look at.

Run:
    python scripts/check_publish_readiness.py
    python scripts/check_publish_readiness.py --skip-slow   # no subprocesses

Exit 0 = ready to release. Exit 1 = do not release yet.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

DATA = ROOT / "data"
DOCS = ROOT / "docs"

# Checks that already exist and are authoritative in their own domain.
# Re-implementing their logic here would create a second source of truth,
# so they are shelled out to and their exit codes trusted.
SUBCHECKS = [
    ("refresh guard (cross-panel coherence)", ["check_refresh_guard.py"]),
    ("capture integrity (strict b,c)",
        ["check_capture_integrity.py", "--targets", "all", "--strict", "b,c"]),
    ("pair integrity (fund vs own constituents)", ["check_pair_integrity.py"]),
]


class Report:
    def __init__(self) -> None:
        self.fail: list[str] = []
        self.warn: list[str] = []
        self.ok: list[str] = []

    def add(self, level: str, msg: str) -> None:
        getattr(self, level).append(msg)

    def emit(self) -> int:
        for m in self.ok:
            print(f"  OK    {m}")
        for m in self.warn:
            print(f"  WARN  {m}")
        for m in self.fail:
            print(f"  FAIL  {m}")
        print()
        print(f"{len(self.fail)} FAIL, {len(self.warn)} WARN, {len(self.ok)} ok")
        return 1 if self.fail else 0


def _run(args: list[str]) -> tuple[int, str]:
    p = subprocess.run([sys.executable, str(ROOT / "scripts" / args[0]), *args[1:]],
                        cwd=ROOT, capture_output=True, text=True)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--skip-slow", action="store_true",
                     help="Skip the subprocess checks; run only the "
                          "publication-specific assertions.")
    args = ap.parse_args()

    from nyse_sessions import week_final_anchor
    from check_factsheet_gate import (read_release_anchor, read_marker_anchor,
                                       DEFAULT_RELEASE, DEFAULT_MARKER)

    anchor = week_final_anchor(datetime.now(timezone.utc))
    r = Report()
    print(f"publish readiness — anchor {anchor.isoformat()} "
          f"({datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC)\n")

    # ---- existing checks -------------------------------------------------
    if args.skip_slow:
        r.add("warn", "sub-checks skipped (--skip-slow) — run without it before releasing")
    else:
        for label, cmd in SUBCHECKS:
            rc, out = _run(cmd)
            if rc == 0:
                r.add("ok", f"{label}")
            else:
                tail = [ln for ln in out.strip().splitlines() if ln.strip()][-1:] or [""]
                r.add("fail", f"{label} exited {rc}: {tail[0][:110]}")

    # ---- publication-specific --------------------------------------------
    # 1. The factsheet is built from breadth_csp1; its anchor must be the week's.
    try:
        end = json.loads((DATA / "breadth_csp1.json").read_text(encoding="utf-8"))["end_date"]
        if date.fromisoformat(end) >= anchor:
            r.add("ok", f"breadth_csp1 end_date {end} reaches the anchor")
        else:
            r.add("fail", f"breadth_csp1 end_date {end} is behind the anchor {anchor}")
    except Exception as e:
        r.add("fail", f"cannot read breadth_csp1.json: {e}")

    # 2. Thin final bars. The floor suppresses the unusable ones, but a panel
    #    sitting just above it is still a weak reading and the factsheet does
    #    not say so — worth a human glance rather than a silent pass.
    try:
        import compute_breadth as cb
        thin = []
        for p in sorted(DATA.glob("breadth_*.json")):
            ser = json.loads(p.read_text(encoding="utf-8")).get("series") or {}
            n, ma = ser.get("n_with_ma50"), ser.get("ma_breadth")
            if not n or not ma:
                continue
            for i in range(len(ma) - 1, -1, -1):
                if ma[i] is not None:
                    if n[i] < 2 * cb.MIN_BREADTH_NAMES:
                        thin.append(f"{p.stem.replace('breadth_','').upper()}({n[i]})")
                    break
        if thin:
            r.add("warn", f"latest bar rests on a thin roster: {', '.join(thin)}")
        else:
            r.add("ok", "no panel's latest bar rests on a thin roster")
    except Exception as e:
        r.add("warn", f"thin-bar check unavailable: {e}")

    # 3. Roster walkback. Not an error, but the factsheet prints a Friday
    #    date over what may be Thursday's membership, and the reader cannot
    #    tell from the PDF.
    try:
        wb = []
        for p in sorted(DATA.glob("constituents_*.json")):
            d = json.loads(p.read_text(encoding="utf-8"))
            snaps = d.get("snapshots") or {}
            if not snaps:
                continue
            k = max(snaps)
            a = snaps[k].get("actual_date")
            if a and a != k:
                wb.append(d.get("etf", p.stem))
        if len(wb) >= 5:
            r.add("warn", f"{len(wb)} panels carry a walked-back roster "
                          f"(e.g. {', '.join(sorted(wb)[:4])}) — the factsheet "
                          f"dates them to the target Friday")
        elif wb:
            r.add("ok", f"{len(wb)} panel(s) walked back — normal holiday behaviour")
        else:
            r.add("ok", "every panel's roster is dated to its target Friday")
    except Exception as e:
        r.add("warn", f"walkback check unavailable: {e}")

    # 4. Already published / already released.
    pub = read_marker_anchor(DEFAULT_MARKER)
    rel = read_release_anchor(DEFAULT_RELEASE)
    if pub == anchor:
        r.add("warn", f"this anchor was already published on {pub} — "
                      f"releasing again would re-send")
    if rel == anchor:
        r.add("ok", f"already released for {anchor}")
    else:
        r.add("ok", f"not yet released for {anchor} (expected before review)")

    code = r.emit()
    print()
    if code == 0:
        print("MECHANICALLY READY. No script can judge whether the numbers are")
        print("economically sensible — before releasing, look at:")
        print("  - the Data tab: any panel with an unexpected roster count or")
        print("    a coverage drop this week")
        print("  - the rebalance card: do the position changes match what the")
        print("    signals did, or is something moving for no visible reason")
        print("  - week-on-week NAV: a jump with no corresponding market move")
        print("    usually means a pricing or FX problem, not performance")
        print()
        print("Then: python scripts/release_factsheet.py")
        print("      git add docs/factsheet_release.json && git commit && git push")
    else:
        print("NOT READY — fix the FAIL lines above, re-run, and do not release.")
    return code


if __name__ == "__main__":
    sys.exit(main())
