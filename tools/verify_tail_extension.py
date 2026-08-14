"""Prove the panel tail extension changes no existing value.

THE CLAIM UNDER TEST.

compute_breadth used to end its daily loop at `end_friday` — the last
PUBLISHED roster Friday. It now runs to the last completed session on the
fund's own venue calendar. The claim is that this is VALUE-PRESERVING: every
date the old bound produced comes out bit-identical under the new one, and the
only difference is extra days on the end.

The claim rests on active_roster_at resolving "most recent snapshot <= T".
Under that rule Thursday 13 Aug uses the 7 Aug roster whether it is computed
today or next week, because 14 Aug is not <= 13 Aug. If that reasoning is
wrong, some existing date moves, and this script finds it.

WHY IT IS NOT A UNIT TEST.

The day loop lives inside main() and is not extractable without a refactor I
am not doing on the day of a live fill. This runs the real thing twice instead.

USE --fixed-prices. IT IS THE ONLY VALID MODE.

Without it this compares two runs that saw DIFFERENT price frames, and the
first IUUS run duly reported 2 discrete breadth values moving in 2022 plus
~1,100 z-scores downstream of them. That was not the bound. Two weaker
controls each failed to prove it:

  - old-bound twice: both runs hit a cache an earlier wide run had left, so
    the download asymmetry under investigation never occurred.
  - old-bound twice with the cache deleted: run 1 then had no `prior` to
    compare against, which disarms _revert_vendor_step_defects — a guard that
    can revert an entire column to its cached values. Whether it fires is
    itself a function of the download, so a control must hold the download
    still, not merely repeat it.

--fixed-prices pins ONE frame for both runs and removes the download from the
experiment. What remains is the schedule bound alone. Under it, IUUS, EXH9 and
CSP1 are bit-identical on every shared date.

Two consequences worth keeping separate. The bound change is value-preserving.
And, independently, recomputing a panel against a re-fetched price frame can
move historical breadth slightly — pre-existing behaviour, not introduced
here, and worth its own investigation.

Usage:
    python tools/verify_tail_extension.py EXH9 IUUS CSP1 --fixed-prices
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import compute_breadth as cb  # noqa: E402


def _panel(etf: str) -> Path:
    return REPO / "data" / f"breadth_{etf.lower()}.json"


def _run(etf: str, extend: bool) -> dict:
    """Compute one panel and return it, with the tail bound on or off."""
    original = cb.last_completed_session_on
    argv = sys.argv[:]
    if not extend:
        cb.last_completed_session_on = lambda *a, **k: None   # old behaviour
    sys.argv = ["compute_breadth.py", "--etf", etf]
    try:
        rc = cb.main()
    finally:
        cb.last_completed_session_on = original
        sys.argv = argv
    if rc != 0:
        raise SystemExit(f"{etf}: compute_breadth exited {rc}")
    return json.loads(_panel(etf).read_text(encoding="utf-8"))


def compare(etf: str, control: bool = False, fresh: bool = False,
            fixed_prices: bool = False) -> bool:
    """`control=True` runs the OLD bound twice.

    A control is not optional here. The first run of any pair re-downloads
    (download_prices asks the cache to cover end_friday+5, which no cache ever
    reaches, so the current window always misses) while the second is served
    from what the first wrote. That asymmetry belongs to the download path,
    not to the bound, and without a control its noise is indistinguishable
    from a real regression — which is exactly the trap the first IUUS run set.
    """
    backup = REPO / "data" / f"_backup_breadth_{etf.lower()}.json"
    had = _panel(etf).exists()
    if had:
        shutil.copy2(_panel(etf), backup)
    if fresh:
        # Force run 1 to re-download so it faces the SAME cache-miss the wide
        # run faces. Without this a control is vacuous: a cache left behind by
        # an earlier wide run lets both control runs hit it, and the asymmetry
        # under investigation never occurs.
        pc = cb.paths_for(etf)["prices_cache"]
        if Path(pc).exists():
            Path(pc).unlink()
            print(f"  [control] deleted {Path(pc).name} to force a download")
    pinned = None
    real_download = cb.download_prices
    if fixed_prices:
        # THE ONLY CLEAN ISOLATION. Two earlier controls were not clean:
        # cache-hit-vs-cache-hit never reproduced the download asymmetry, and
        # delete-the-cache disarmed _revert_vendor_step_defects by removing the
        # `prior` it compares against. That guard can revert a whole column to
        # its cached values, so whether it fires is itself a function of the
        # download — which is precisely what a control must hold still.
        #
        # Pinning one frame for both runs removes the download from the
        # experiment entirely. What remains is the schedule bound alone, which
        # is the change whose value-preservation is being claimed.
        def _pinned(tickers, start, end, **kw):
            nonlocal pinned
            if pinned is None:
                pinned = real_download(tickers, start, end, **kw)
            return pinned
        cb.download_prices = _pinned
    try:
        wide = _run(etf, extend=not control)  # first: writes the price cache
        narrow = _run(etf, extend=False)      # second: served from that cache
    finally:
        cb.download_prices = real_download
        if had:
            shutil.move(backup, _panel(etf))
        else:
            subprocess.run(["git", "checkout", "--", f"data/breadth_{etf.lower()}.json"],
                           cwd=REPO, capture_output=True)

    w, n = wide["series"], narrow["series"]
    key_dates = "dates" if "dates" in w else "date"
    wd, nd = list(w[key_dates]), list(n[key_dates])
    print(f"\n=== {etf} ===")
    print(f"  old bound : {nd[0]} -> {nd[-1]}  ({len(nd)} days)")
    print(f"  new bound : {wd[0]} -> {wd[-1]}  ({len(wd)} days)")
    print(f"  gained    : {len(wd) - len(nd)} day(s): {wd[len(nd):]}")

    if wd[:len(nd)] != nd:
        print("  FAIL: the shared prefix of dates is not identical")
        return False

    ok = True
    for key in sorted(w):
        if key == key_dates:
            continue
        wv, nv = list(w[key])[:len(nd)], list(n[key])
        if len(wv) != len(nv):
            print(f"  FAIL {key}: length {len(wv)} vs {len(nv)}")
            ok = False
            continue
        bad = [(nd[i], a, b) for i, (a, b) in enumerate(zip(wv, nv)) if a != b]
        if bad:
            ok = False
            print(f"  FAIL {key}: {len(bad)} value(s) moved, first 3: {bad[:3]}")
    # Signals are the tradeable output; a silent change here is the worst case.
    def _sig_date(s):
        return s["date"] if isinstance(s, dict) else s

    ws = [s for s in wide.get("signals", []) if _sig_date(s) <= nd[-1]]
    ns = list(narrow.get("signals", []))
    if ws != ns:
        ok = False
        print(f"  FAIL signals: {len(ws)} vs {len(ns)} on the shared window")
    else:
        print(f"  signals on shared window identical ({len(ns)})")
    print(f"  {'PASS — every existing value unchanged' if ok else 'FAIL'}")
    return ok


if __name__ == "__main__":
    argv = sys.argv[1:]
    control = "--control" in argv
    etfs = [a for a in argv if not a.startswith("--")] or ["EXH9", "IUUS"]
    if control:
        print("CONTROL RUN — old bound twice. Any difference here is download "
              "nondeterminism, not the bound.\n")
    fresh = "--fresh" in argv
    fixed = "--fixed-prices" in argv
    results = {e: compare(e, control=control, fresh=fresh,
                          fixed_prices=fixed) for e in etfs}
    print("\n" + "=" * 60)
    for e, ok in results.items():
        print(f"  {e}: {'PASS' if ok else 'FAIL'}")
    raise SystemExit(0 if all(results.values()) else 1)
