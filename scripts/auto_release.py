"""Automatic release of the weekly factsheet (2026-09-06, owner decision).

WHY THE RELEASE EXISTED. Since 2026-08-08 a refresh landing on main could
not email the distribution list on its own: the gate wanted a release
marker written by a person after reading the week. That marker earned its
keep twice in four weeks — the 2026-08-30 hollow Friday rows went green and
were held until fixed; the 2026-09-06 09:47 SGT book carried sleeve C on
HOLD for one blank member and was superseded by a corrected refresh before
anything went out. Both times the panel was "current to Friday"; what was
wrong was the content.

WHY IT CAN BE AUTOMATIC NOW. Both of those failures are visible to a
machine today: the hollow-row class fails the refresh outright
(compute_breadth's tail verification, 2026-09-05), and live_targets says
whether EVERY sleeve was ranked on the close its fill will use
(targets_final). So the release becomes a mechanical verdict taken by the
same guarded run that produced the book, and the human step moves after the
send: the workflow mails the operator a notice with the three items no
script judges (roster counts, moves against signals, week-on-week NAV).

THE CONDITIONS, all required — one false holds the week for a person:
  1. weekend cadence: the anchor-producing run, never the post-fill pair;
  2. no operator hold (docs/factsheet_hold.json, release_factsheet.py --hold);
  3. the S&P panel reaches the week-final anchor (the gate's own test);
  4. every sleeve READY and ranked on its fill's decision close
     (live_targets.targets_final) — a held sleeve is a book the reader
     would be told not to trade, and the 09:47 SGT case;
  5. every sleeve's data reaches its venue's last close
     (strategy_freshness.all_current);
  6. the price basis is the one requested — both engine caches record the
     source the run asked for, so a silent fallback to another feed is a
     restatement held for a person (WS19, 2026-09-03);
  7. no staged roster promotion in the run (BTE_APPLY_STAGED_ROSTER);
  8. not already published for this anchor;
  9. the publication-specific readiness checks pass
     (check_publish_readiness.py --skip-slow; the slow sub-checks are the
     VERIFY steps the refresh has just run).

The marker written carries auto:true and every condition with its evidence,
so a reader of docs/factsheet_release.json can see what released the week.
Python datetime months are 1-indexed (January = 1).

Usage:
    python scripts/auto_release.py --dry-run            # print the verdict
    python scripts/auto_release.py --cadence weekend    # write the marker if it passes
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_factsheet_gate import read_hold, read_marker_anchor  # noqa: E402
from nyse_sessions import week_final_anchor  # noqa: E402
from release_factsheet import write_release_marker  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DOCS = ROOT / "docs"
HOLD = DOCS / "factsheet_hold.json"

TRUTHY = {"1", "true", "yes", "on"}

# The report carries an em dash; a console code page must not turn a
# verdict into an exception (see build_commentary).
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass


def _sidecar_source(cache: Path) -> str | None:
    p = cache.with_name(cache.stem + ".source.json")
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("source")
    except Exception:  # noqa: BLE001
        return "unreadable"


def engine_basis(data_dir: Path = DATA) -> dict[str, str | None]:
    """The source each engine cache records it was built from (None = a
    cache written before the sidecar existed, i.e. yfinance)."""
    return {
        "B": _sidecar_source(data_dir / "asset_class_prices_cache.parquet"),
        "C": _sidecar_source(data_dir / "thematic_prices_cache.parquet"),
    }


def _readiness_rc(root: Path) -> tuple[int, str]:
    p = subprocess.run(
        [sys.executable, str(root / "scripts" / "check_publish_readiness.py"),
         "--skip-slow"], cwd=root, capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    # The verdict line, not the last line: the script ends with the release
    # instructions, which read as a failure when quoted as evidence.
    lines = [ln.strip() for ln in (p.stdout or "").splitlines() if ln.strip()]
    verdict = next((ln for ln in lines
                    if ln.startswith(("MECHANICALLY READY", "NOT READY"))
                    or " FAIL, " in ln), lines[-1] if lines else "")
    return p.returncode, verdict[:160]


def evaluate(anchor: date, *, cadence: str, price_source: str,
             data_dir: Path = DATA, docs_dir: Path = DOCS,
             env: dict | None = None, readiness=None) -> dict:
    """The verdict and every condition behind it. Reads artefacts under
    ``data_dir``/``docs_dir`` only; ``readiness`` is the callable that runs
    the publication checks (stubbed in tests)."""
    env = os.environ if env is None else env
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    add("weekend cadence", cadence == "weekend", f"cadence={cadence}")

    hold = read_hold(docs_dir / "factsheet_hold.json")
    add("no operator hold", hold is None,
        "none" if hold is None else
        f"held since {hold.get('held_at_utc', '?')}: {hold.get('note') or 'no note'}")

    try:
        end = json.loads((data_dir / "breadth_csp1.json").read_text(encoding="utf-8"))["end_date"]
        add("panel reaches the anchor", date.fromisoformat(end) >= anchor,
            f"breadth_csp1 end_date {end} vs anchor {anchor.isoformat()}")
    except Exception as exc:  # noqa: BLE001
        add("panel reaches the anchor", False, f"cannot read breadth_csp1.json: {exc!r}")

    try:
        lt = json.loads((data_dir / "live_targets.json").read_text(encoding="utf-8"))
        held = [s["sleeve"] for s in lt.get("sleeves", []) if s.get("status") != "READY"]
        stale = [s["sleeve"] for s in lt.get("sleeves", [])
                 if s.get("decision_session") != s.get("decision_session_for_fill")]
        ok = lt.get("targets_final") is True and not held and not stale
        add("every sleeve ranked on its fill's close", ok,
            f"targets_final={lt.get('targets_final')}"
            + (f"; HOLD: {', '.join(held)}" if held else "")
            + (f"; not ranked on the fill's close: {', '.join(stale)}" if stale else "")
            + (f"; decision {lt.get('as_of')}" if ok else ""))
    except Exception as exc:  # noqa: BLE001
        add("every sleeve ranked on its fill's close", False,
            f"cannot read live_targets.json: {exc!r}")

    try:
        fr = json.loads((data_dir / "strategy_freshness.json").read_text(encoding="utf-8"))
        behind = [f"{s['sleeve']} to {s.get('data_through')}"
                  for s in fr.get("strategies", []) if s.get("status") != "current"]
        add("every sleeve's data reaches its venue's last close",
            fr.get("all_current") is True and not behind,
            "all current" if not behind else "behind: " + ", ".join(behind))
    except Exception as exc:  # noqa: BLE001
        add("every sleeve's data reaches its venue's last close", False,
            f"cannot read strategy_freshness.json: {exc!r}")

    basis = engine_basis(data_dir)
    expected = "norgate" if price_source in ("norgate", "auto") else "yfinance"
    ok_basis = all((v == expected) or (expected == "yfinance" and v is None)
                   for v in basis.values())
    add("price basis as requested", ok_basis,
        f"requested {price_source} -> expected {expected}; caches record "
        + ", ".join(f"{k}={v or 'yfinance (unrecorded)'}" for k, v in basis.items()))

    staged = str(env.get("BTE_APPLY_STAGED_ROSTER", "")).strip().lower() in TRUTHY
    add("no staged roster promotion", not staged,
        "BTE_APPLY_STAGED_ROSTER=" + (str(env.get("BTE_APPLY_STAGED_ROSTER")) if staged else "unset"))

    pub = read_marker_anchor(docs_dir / "factsheet_published.json")
    add("not already published for this anchor", pub != anchor,
        f"last published {pub.isoformat() if pub else 'none'}")

    if readiness is None:
        readiness = lambda: _readiness_rc(ROOT)  # noqa: E731
    try:
        rc, tail = readiness()
        add("publication readiness checks", rc == 0, tail or f"exit {rc}")
    except Exception as exc:  # noqa: BLE001
        add("publication readiness checks", False, f"could not run: {exc!r}")

    release = all(c["ok"] for c in checks)
    failed = [c["check"] for c in checks if not c["ok"]]
    return {"anchor": anchor.isoformat(), "release": release, "checks": checks,
            "summary": (f"auto-release {anchor.isoformat()}: "
                        + ("RELEASE — every condition met" if release
                           else "HOLD — " + "; ".join(failed)))}


def format_report(verdict: dict) -> str:
    lines = [verdict["summary"]]
    for c in verdict["checks"]:
        lines.append(f"  {'ok  ' if c['ok'] else 'FAIL'} {c['check']}: {c['detail']}")
    return "\n".join(lines)


def release_if_ready(anchor: date, *, cadence: str, price_source: str,
                     docs_dir: Path = DOCS, **kw) -> dict:
    """Evaluate and, on RELEASE, write the marker with the evidence."""
    verdict = evaluate(anchor, cadence=cadence, price_source=price_source,
                       docs_dir=docs_dir, **kw)
    if verdict["release"]:
        path = write_release_marker(
            anchor, note="automatic release: every condition met",
            auto=True, conditions=verdict["checks"],
            out=docs_dir / "factsheet_release.json")
        verdict["marker"] = str(path)
    return verdict


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cadence", default="weekend")
    ap.add_argument("--price-source", default=os.environ.get("BTE_PRICE_SOURCE", "norgate"))
    ap.add_argument("--dry-run", action="store_true",
                    help="print the verdict; write nothing")
    args = ap.parse_args(argv)
    anchor = week_final_anchor(datetime.now(timezone.utc))
    if args.dry_run:
        v = evaluate(anchor, cadence=args.cadence, price_source=args.price_source)
    else:
        v = release_if_ready(anchor, cadence=args.cadence, price_source=args.price_source)
    print(format_report(v))
    if v.get("marker"):
        print(f"\nReleased {v['anchor']} -> {v['marker']}")
    return 0 if v["release"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
