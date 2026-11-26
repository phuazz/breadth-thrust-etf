"""Unattended weekly rebuild of the combined-book risk snapshot (2026-11-26).

WHY. ``data/combined_book_risk.json`` is a dated snapshot of the model-held
book. Without a rebuild each week the Monitor tab's risk table shows an older
as-of (owner rulings of 2026-10-28 accept a manual refresh; this makes it
routine). The rebuild is the operator sequence

    python scripts/combined_book_risk.py export-engine-fx   (vendor fetch)
    python scripts/combined_book_risk.py build
    python scripts/combined_book_risk.py check

wrapped in the guards below. Nothing here changes the calculator or the
reconciliation rule (owner ruling F2, 2026-10-28).

THE WINDOW IS READ FROM THE DATA, NEVER FROM THE CLOCK. ``build`` reconciles
the held basis (``component_held_basis.json``) to ``live_track.json`` and
passes only when both are anchored on the same Friday AND live_track still
carries the pre-fill book. Measured on the week of 2026-11-20 (reconcile()
run against each commit's three inputs):

    73018549 Sun 22 Nov 09:48  weekly refresh     live_track 16 Nov   not checked
    03f1c833 Sun 22 Nov 22:25  Europe release     live_track 20 Nov   PASS
    7b3b7e3e Tue 24 Nov 07:15  gate-states        live_track 20 Nov   PASS
    833b77bb Tue 24 Nov 12:41  post-fill refresh  live_track 20 Nov   FAIL 0.30
    f042f521 Tue 24 Nov 17:31  fill recorded      live_track 23 Nov   not checked

The window therefore opens when the weekend refresh's Europe step (or a manual
Europe release, as on 22 Nov) re-anchors live_track, and closes when the
Tuesday post-fill refresh publishes the post-fill weights. It has opened on
Saturday evening (24 Oct), Sunday midday (8 and 15 Nov) and Sunday night
(1 and 22 Nov). So this runs hourly from Saturday 10:00 to Tuesday 10:00 SGT
(``register_cbr_refresh_task.ps1``) and each firing first asks the data:

  probe      read-only. Held basis valid; live_track anchor == held anchor;
             reconcile() passes; live_targets agrees where it applies. Any
             failure is a SKIP, not an error: outside the window is normal.
             A committed snapshot already current for this anchor is a SKIP.
  export     inside the window only. The engine EUR/USD is re-derived; the
             vendor's raw EURUSD=X bars are captured from the same fetch.
  FX guard   every exported rate up to the anchor must be a raw vendor
             observation ON THAT DATE with the same value. The engine
             converter forward-fills up to 10 days onto the Xetra calendar;
             a filled rate (Friday's bar not yet published, a vendor hole)
             fails the run. FX is never forward-filled into a snapshot.
  build      into a staging path outside the repository, so a failed or
             unavailable build never replaces the committed artefact. The
             calculator's own run lock, .pending marker and failure record
             live beside the staging file.
  gate       risk_status == "available" AND capital_status == "available"
             AND ``check`` on the staging artefact returns "current".
  promote    staging replaces data/combined_book_risk.json atomically, and
             ``check`` must return "current" again on the real path.
  publish    only with --push: commit the two owned files, rebase, push.

FAIL CLOSED. Any failure inside the window restores both owned files to the
bytes they had before the run (the export writes its file in place) and
publishes nothing; the previous snapshot stays and displays as historical.
An in-window failure mails the operator (best effort, same channel as the
scheduled refresh). Every firing that reaches a verdict writes
``logs/cbr_refresh_last_run.json`` (liveness, read by fleet_watch); the git
heartbeat is the commit subject "Combined-book risk weekly rebuild".

Python datetime months are 1-indexed (January = 1).

    python scripts/scheduled_cbr_refresh.py --probe   # window verdict only, writes nothing
    python scripts/scheduled_cbr_refresh.py           # soak: rebuild in the window, no commit
    python scripts/scheduled_cbr_refresh.py --push    # armed: commit and push on success
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LOG_DIR = ROOT / "logs"
SENTINEL = LOG_DIR / "cbr_refresh_last_run.json"
ARTEFACT = "combined_book_risk.json"
FX_EXPORT = "combined_book_risk_fx_eurusd.json"
OWNED = (f"data/{ARTEFACT}", f"data/{FX_EXPORT}")
COMMIT_PREFIX = "Combined-book risk weekly rebuild"
# implied = (eur * fx) / eur; anything beyond float noise is a different rate.
FX_REL_TOL = 1e-9
PY = sys.executable

sys.path.insert(0, str(ROOT / "scripts"))


class StepFailed(RuntimeError):
    pass


# --------------------------------------------------------------------------
# probe: is the reconciliation window open? (read-only)
# --------------------------------------------------------------------------

def probe(data_dir: Path = DATA, today: date | None = None,
          artefact: Path | None = None) -> dict:
    """The window verdict from the committed inputs alone. Writes nothing.

    state "open"  - held basis and live_track share an anchor, the
                    reconciliation passes, and no current snapshot exists for
                    that anchor; a rebuild should run.
    state "skip"  - anything else, with the reason. Outside the window is the
                    normal state on most firings."""
    import combined_book_risk as cbr
    today = today or datetime.now(timezone.utc).date()
    artefact = artefact or (data_dir / ARTEFACT)
    basis, reasons = cbr.load_held_book(data_dir, today)
    if basis is None:
        return {"state": "skip", "anchor": None,
                "reason": "held basis does not validate: " + "; ".join(reasons)}
    anchor = basis["anchor"]
    lines = [{"sleeve": r["sleeve"], "etf": r["etf"], "held": float(r["held"])}
             for r in basis["lines"] if float(r["held"]) > 0]
    rec, rec_reasons = cbr.reconcile(lines, data_dir, anchor)
    if rec.get("status") != "pass" or rec_reasons:
        if rec.get("status") == "fail":
            why = (f"live_track carries a different book on the same anchor "
                   f"(max difference {rec.get('max_abs_diff', float('nan')):.4f}); "
                   "the post-fill weights are published, the window has closed")
            extra = [r for r in rec_reasons if not r.startswith("held lines do not reconcile")]
        else:
            why = rec.get("detail") or "reconciliation not verified"
            extra = [r for r in rec_reasons if not r.startswith("reconciliation not verified")]
        if extra:
            why += " | " + "; ".join(extra)
        return {"state": "skip", "anchor": anchor, "reason": why,
                "reconciliation": rec.get("status")}
    if artefact.exists():
        try:
            blob, check = cbr.read_snapshot(artefact, data_dir)
        except cbr.InputError as exc:
            blob, check = None, {"state": "invalid", "reasons": [str(exc)]}
        if (blob is not None and check["state"] == "current"
                and blob.get("risk_status") == "available"
                and (blob.get("fingerprint") or {}).get("anchor") == anchor):
            return {"state": "skip", "anchor": anchor, "reconciliation": "pass",
                    "reason": f"the committed snapshot is already current for {anchor}"}
    return {"state": "open", "anchor": anchor, "reconciliation": "pass",
            "reason": f"held basis and live_track both anchored {anchor}; reconciliation passes"}


# --------------------------------------------------------------------------
# export with the raw vendor bars captured, and the no-forward-fill guard
# --------------------------------------------------------------------------

def export_with_raw(data_dir: Path = DATA):
    """Run the operator export in-process and return the raw EURUSD=X close
    series the engine converter fetched (the same response, not a second
    fetch). The converter does ``import yfinance as yf; yf.download(...)``,
    so wrapping the module attribute captures it."""
    import pandas as pd
    import yfinance
    import combined_book_risk as cbr
    captured: list = []
    real = yfinance.download

    def recording(*a, **kw):
        frame = real(*a, **kw)
        captured.append(((a[0] if a else kw.get("tickers")), frame))
        return frame

    yfinance.download = recording
    try:
        cbr.export_engine_fx(data_dir)
    finally:
        yfinance.download = real
    frames = [f for t, f in captured if str(t) == "EURUSD=X"]
    if len(frames) != 1:
        raise StepFailed(f"expected one EURUSD=X fetch during the export, saw {len(frames)}")
    raw = frames[0]
    if isinstance(raw.columns, pd.MultiIndex):
        raw = raw.copy()
        raw.columns = raw.columns.get_level_values(0)
    close = raw["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    close = close.dropna()
    close.index = pd.to_datetime(close.index).tz_localize(None).normalize()
    return close


def fx_forward_fill_violations(export: dict, raw, anchor: str) -> list[str]:
    """Exported dates up to the anchor whose rate is not the vendor's own
    observation on that date. ``raw`` is a pandas Series of vendor closes
    indexed by normalised date. Returns human-readable violations (empty =
    clean)."""
    raw_by_day = {ts.strftime("%Y-%m-%d"): float(v) for ts, v in raw.items()}
    out: list[str] = []
    for d, v in zip(export["dates"], export["close"]):
        if d > anchor:
            continue
        r = raw_by_day.get(d)
        if r is None:
            out.append(f"{d}: no vendor EURUSD=X bar on this date (the converter filled {v:.6f})")
        elif not math.isclose(float(v), r, rel_tol=FX_REL_TOL, abs_tol=0.0):
            out.append(f"{d}: exported {float(v):.8f} is not the vendor close {r:.8f}")
    if not any(d <= anchor for d in export["dates"]):
        out.append(f"the export carries no rate on or before the anchor {anchor}")
    return out


# --------------------------------------------------------------------------
# git and file helpers
# --------------------------------------------------------------------------

def run(cmd: list[str], label: str, log, cwd: Path | None = None,
        ok_codes: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess:
    log(f"$ {' '.join(str(c) for c in cmd)}")
    p = subprocess.run(cmd, cwd=cwd or ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    for line in ((p.stdout or "") + (p.stderr or "")).splitlines():
        log(f"  | {line}")
    if p.returncode not in ok_codes:
        raise StepFailed(f"{label} exited {p.returncode}")
    return p


def owned_dirt(repo_root: Path = ROOT) -> list[str]:
    p = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all", "--", *OWNED],
                       cwd=repo_root, capture_output=True, text=True)
    if p.returncode != 0:
        raise StepFailed(f"git status exited {p.returncode}: {(p.stderr or p.stdout).strip()}")
    return [ln for ln in p.stdout.splitlines() if ln.strip()]


def snapshot_bytes(data_dir: Path) -> dict[str, bytes | None]:
    out: dict[str, bytes | None] = {}
    for name in (ARTEFACT, FX_EXPORT):
        p = data_dir / name
        out[name] = p.read_bytes() if p.exists() else None
    return out


def restore_bytes(saved: dict[str, bytes | None], data_dir: Path, log) -> None:
    """Put both owned files back exactly as they were before the run."""
    for name, raw in saved.items():
        p = data_dir / name
        if raw is None:
            if p.exists():
                p.unlink()
                log(f"restore: removed {name} (absent before the run)")
            continue
        if p.exists() and p.read_bytes() == raw:
            continue
        tmp = p.with_name(p.name + ".restore.tmp")
        tmp.write_bytes(raw)
        os.replace(tmp, p)
        log(f"restore: {name} put back to its pre-run bytes")


def write_sentinel(outcome: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    rec = {"written_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), **outcome}
    tmp = SENTINEL.with_name(SENTINEL.name + ".tmp")
    tmp.write_text(json.dumps(rec, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, SENTINEL)


def mail_failure(subject: str, body: str, handle) -> None:
    """Best effort; never raises. Same SMTP channel and durable alert record
    as the scheduled refresh."""
    try:
        import scheduled_refresh
        scheduled_refresh._email(subject, body, handle)
    except Exception as exc:  # noqa: BLE001
        handle.write(f"\n[email not attempted: {type(exc).__name__}]\n")


# --------------------------------------------------------------------------
# the rebuild
# --------------------------------------------------------------------------

def rebuild(anchor: str, log, data_dir: Path = DATA, staging_dir: Path | None = None,
            exporter=export_with_raw, py: str = PY) -> dict:
    """Export, guard, build to staging, gate, promote. Raises StepFailed on
    any failure; the caller restores the owned files."""
    import combined_book_risk as cbr
    raw = exporter(data_dir)
    export = json.loads((data_dir / FX_EXPORT).read_text(encoding="utf-8"))
    bad = fx_forward_fill_violations(export, raw, anchor)
    if bad:
        raise StepFailed("EUR/USD export carries rates that are not vendor observations "
                         f"(forward-filled or revised) on or before {anchor}: "
                         + "; ".join(bad[:5]) + (f" (+{len(bad) - 5} more)" if len(bad) > 5 else ""))
    log(f"FX guard: every exported rate to {anchor} is a vendor observation on its date")

    own_tmp = staging_dir is None
    staging_dir = Path(tempfile.mkdtemp(prefix="cbr_stage_")) if own_tmp else staging_dir
    staging_dir.mkdir(parents=True, exist_ok=True)
    try:
        staged = staging_dir / ARTEFACT
        run([py, str(ROOT / "scripts" / "combined_book_risk.py"), "build",
             "--data-dir", str(data_dir), "--out", str(staged)], "build", log)
        blob = json.loads(staged.read_text(encoding="utf-8"))
        if blob.get("run_status") != "ok":
            raise StepFailed("build recorded a failed run: " + "; ".join(blob.get("reasons") or []))
        if blob.get("capital_status") != "available" or blob.get("risk_status") != "available":
            raise StepFailed(f"build produced capital {blob.get('capital_status')}, risk "
                             f"{blob.get('risk_status')}: " + "; ".join(blob.get("reasons") or []))
        if (blob.get("fingerprint") or {}).get("anchor") != anchor:
            raise StepFailed(f"build anchored {(blob.get('fingerprint') or {}).get('anchor')}, "
                             f"not the probed {anchor}")
        run([py, str(ROOT / "scripts" / "combined_book_risk.py"), "check",
             "--data-dir", str(data_dir), "--out", str(staged)], "check (staging)", log)
        target = data_dir / ARTEFACT
        tmp = target.with_name(target.name + ".promote.tmp")
        shutil.copyfile(staged, tmp)
        os.replace(tmp, target)
        log(f"promoted the staged snapshot to {target}")
    finally:
        if own_tmp:
            shutil.rmtree(staging_dir, ignore_errors=True)
    _, check = cbr.read_snapshot(data_dir / ARTEFACT, data_dir)
    if check["state"] != "current":
        raise StepFailed(f"check on the promoted artefact is {check['state']}: "
                         + "; ".join(check["reasons"]))
    return {"anchor": anchor, "risk_status": blob["risk_status"],
            "portfolio_vol": (blob.get("portfolio") or {}).get("vol"),
            "fx_last": export["dates"][-1]}


def publish(anchor: str, log, repo_root: Path = ROOT) -> str:
    run(["git", "add", "--", *OWNED], "git add", log, cwd=repo_root)
    p = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo_root)
    if p.returncode == 0:
        log("nothing to commit — the owned files are unchanged")
        return "unchanged"
    run(["git", "commit", "-m", f"{COMMIT_PREFIX} for {anchor}\n\n"
         "Scheduled: export-engine-fx, build, check; risk available, check current."],
        "git commit", log, cwd=repo_root)
    run(["git", "pull", "--rebase", "origin", "main"], "git pull before push", log, cwd=repo_root)
    run(["git", "push", "origin", "HEAD:main"], "git push", log, cwd=repo_root)
    return "pushed"


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--probe", action="store_true", help="print the window verdict; write nothing")
    ap.add_argument("--push", action="store_true", help="armed: commit and push on success")
    ap.add_argument("--no-pull", action="store_true",
                    help="skip the preflight pull (development only; the scheduled task never passes it)")
    a = ap.parse_args(argv)

    if a.probe:
        v = probe()
        print(json.dumps(v, indent=1))
        return 0

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc)
    handle = (LOG_DIR / f"cbr_refresh_{started:%Y-%m-%d}.log").open("a", encoding="utf-8")

    def log(msg: str) -> None:
        line = f"{datetime.now(timezone.utc):%H:%M:%S} {msg}"
        print(line)
        handle.write(line + "\n")
        handle.flush()

    log(f"=== combined-book risk rebuild {'ARMED' if a.push else 'SOAK'} "
        f"{started.isoformat(timespec='seconds')} ===")
    mode = "armed" if a.push else "soak"
    try:
        dirty = owned_dirt()
        if dirty:
            raise StepFailed("owned paths are dirty before the run (a manual run may be in "
                             "flight); nothing touched:\n  " + "\n  ".join(dirty))
        if not a.no_pull:
            # Fatal: the window is read from origin's files, so a stale clone
            # would probe the wrong week.
            run(["git", "pull", "--rebase", "origin", "main"], "git pull", log)
        v = probe()
        log(f"probe: {v['state']} — {v['reason']}")
        if v["state"] != "open":
            write_sentinel({"mode": mode, "outcome": "skip", **v})
            log("RESULT: SKIP")
            return 0
        saved = snapshot_bytes(DATA)
        try:
            res = rebuild(v["anchor"], log)
        except BaseException:
            restore_bytes(saved, DATA, log)
            raise
        if a.push:
            # A publish failure is left as found: the commit may exist by
            # then, and a rejected commit is a person's to read.
            published = publish(v["anchor"], log)
        else:
            # Soak is a rehearsal: leaving the rebuilt files uncommitted would
            # make the next firing refuse on its own dirt.
            restore_bytes(saved, DATA, log)
            published = "soak: rebuilt, verified, restored; nothing committed"
        write_sentinel({"mode": mode, "outcome": "rebuilt", "published": published, **res})
        log(f"RESULT: REBUILT {res['anchor']} ({published})")
        return 0
    except StepFailed as exc:
        log(f"RESULT: FAILED — {exc}")
        log("The previous snapshot is unchanged and nothing was published.")
        write_sentinel({"mode": mode, "outcome": "failed", "reason": str(exc)})
        if a.push:
            mail_failure("[FAIL] Combined-book risk weekly rebuild",
                         f"{exc}\n\nThe previous snapshot is unchanged; the next hourly "
                         "firing retries while the window is open.", handle)
        return 1
    finally:
        handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
