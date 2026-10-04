"""Step 8: execute the registered run mode ONCE from the frozen inputs, then the
charts.

Before the run mode starts, every frozen file is compared with the sha256 the
PREREG header quotes (the third spec-freeze pass). The engine prints a summary
of results to stdout; it goes to the ignored log
data_local/ws_fill_placement/run_stdout.log and is not read here. After the
charts, results.json and the charts are moved to the ignored folder
data_local/ws_fill_placement/held_outputs/ (the second and third passes:
committing them on main would publish them before the verdict read). The
script reports only exit codes, file names, sizes and sha256, including the
log's.

    python reviews/2026-10-03_fill-placement-diagnostic/step8_run.py
"""
import hashlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

STUDY = Path(__file__).resolve().parent
REPO = STUDY.parents[1]
ENGINE_DIR = STUDY / "engine"
LOCAL = REPO / "data_local/ws_fill_placement"
LOG = LOCAL / "run_stdout.log"
MARKER = LOCAL / "run_executed.marker"
HELD = LOCAL / "held_outputs"
FROZEN_FILES = 12


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def header_hashes() -> dict:
    text = (STUDY / "PREREG.md").read_text(encoding="utf-8")
    if "Content sha256 at the freeze: " not in text:
        sys.exit("STOP: the PREREG carries no freeze header")
    seg = text.split("Content sha256 at the freeze: ", 1)[1].split(". The run mode refuses", 1)[0]
    found = re.findall(r"`([^`]+)` `([0-9a-f]{64})`", seg)
    if len(found) != FROZEN_FILES:
        sys.exit(f"STOP: the header quotes {len(found)} frozen files, not {FROZEN_FILES}")
    return dict(found)


def rel(p: Path) -> str:
    return str(p.relative_to(REPO)).replace("\\", "/")


def hold_outputs() -> None:
    """Move whatever the run and the charts wrote into the ignored folder and
    print each file's sha256 and the log's (the fifth spec-freeze pass: on every
    exit path once the marker is written, a late failure included)."""
    HELD.mkdir(parents=True, exist_ok=True)
    res = ENGINE_DIR / "results/results.json"
    charts = ENGINE_DIR / "charts"
    for p in ([res] if res.exists() else []) + (sorted(charts.glob("*")) if charts.exists() else []):
        dest = (HELD / "charts" / p.name) if p.parent == charts else (HELD / p.name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(p), str(dest))
        print(rel(dest), dest.stat().st_size, "bytes", sha(dest))
    if charts.exists() and not any(charts.iterdir()):
        charts.rmdir()
    if LOG.exists():
        print(rel(LOG), LOG.stat().st_size, "bytes", sha(LOG))
    print("outputs held, untracked and ignored, under", rel(HELD))


def main() -> int:
    if MARKER.exists() or (ENGINE_DIR / "results/results.json").exists() or (HELD / "results.json").exists():
        sys.exit("STOP: the run has already been executed once; it is not run again")
    for path, h in header_hashes().items():
        if sha(REPO / path) != h:
            sys.exit(f"STOP: {path} differs from the sha256 the PREREG header quotes")
    print(f"pre-run check: all {FROZEN_FILES} frozen files equal the header's sha256")
    LOCAL.mkdir(parents=True, exist_ok=True)
    MARKER.write_text("run mode invoked\n", encoding="utf-8")
    try:
        with open(LOG, "w", encoding="utf-8") as fh:
            rc = subprocess.run([sys.executable, str(ENGINE_DIR / "fill_timing.py"), "run"], cwd=str(REPO),
                                stdout=fh, stderr=subprocess.STDOUT).returncode
        print("run mode exit code:", rc)
        if rc != 0 or not (ENGINE_DIR / "results/results.json").exists():
            tail = LOG.read_text(encoding="utf-8").strip().splitlines()[-1:] if LOG.exists() else []
            stop_lines = [t for t in tail if t.startswith("STOP:")]
            print("the run did not complete;", stop_lines[0] if stop_lines else "see the ignored log (not read here)")
            return 1
        ch = subprocess.run([sys.executable, str(ENGINE_DIR / "charts.py")], cwd=str(REPO), capture_output=True, text=True)
        print("charts exit code:", ch.returncode)
        # the charts script prints only structural checks (render check, marks, footers)
        for line in (ch.stdout + ch.stderr).splitlines():
            if "render check" in line:
                print(line.split("{")[0].strip())
        return 0 if ch.returncode == 0 else 1
    finally:
        hold_outputs()


if __name__ == "__main__":
    sys.exit(main())
