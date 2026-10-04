"""Write the freeze header into the PREREG: the content sha256 of every frozen
file, and the statement of what the freeze is. Run once, at the freeze, after
the coverage record is final; it asserts that the coverage record carries the
same hashes as the files the run binds, and refuses if the header is already
written.

    python reviews/2026-10-03_fill-placement-diagnostic/freeze_header.py
"""
import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

STUDY = Path(__file__).resolve().parent
REPO = STUDY.parents[1]
VAULT = REPO.parent
WS = "WS23"
FILES = [("engine", STUDY / "engine/fill_timing.py"), ("spec", STUDY / "engine/prereg_spec.json"),
         ("charts", STUDY / "engine/charts.py"), ("adapter", REPO / "scripts/ws_fill_placement_adapter.py"),
         ("tests", REPO / "tests/test_ws_fill_placement.py"), ("fills", STUDY / "engine/results/fills.json"),
         ("book meta", STUDY / "engine/results/book_meta.json"), ("bars", STUDY / "engine/results/bars_used.json"),
         ("coverage", STUDY / "engine/results/coverage.json"), ("mutation check", STUDY / "mutation_check.py"),
         ("freeze header", STUDY / "freeze_header.py"), ("run script", STUDY / "step8_run.py")]


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def freeze_date_and_free_number() -> str:
    """The freeze date from the clock in Singapore (the fifth spec-freeze pass:
    never hard-coded), and a check that the workstream number is still unused
    in the repository, tracked or untracked (this study's own files aside), and
    in the vault's ledger and register."""
    today = dt.datetime.now(ZoneInfo("Asia/Singapore")).date()      # Python months are 1-indexed
    hits = subprocess.run(["git", "grep", "--untracked", "-l", "-w", WS, "--", ".", ":!reviews/2026-10-03_fill-placement-diagnostic"],
                          cwd=str(REPO), capture_output=True, text=True).stdout.split()
    if hits:
        sys.exit(f"STOP: {WS} is already used in {hits}")
    for f in (VAULT / "STUDIES_LEDGER.md", VAULT / "studies" / "hypotheses.yaml"):
        if re.search(rf"\b{WS}\b", f.read_text(encoding="utf-8")):
            sys.exit(f"STOP: {WS} is already used in {f}")
    return f"{today:%A} {today.isoformat()}"


def main():
    when = freeze_date_and_free_number()
    hashes = {name: sha(p) for name, p in FILES}
    cov = json.loads((STUDY / "engine/results/coverage.json").read_text(encoding="utf-8"))
    prov = cov["provenance"]
    # the coverage record must carry the same hashes for everything the run binds to
    assert prov["engine_sha256"] == hashes["engine"], "engine differs from the coverage record"
    assert prov["spec_sha256"] == hashes["spec"], "spec differs from the coverage record"
    assert prov["history_json_sha256"] == hashes["bars"], "bars differ from the coverage record"
    assert prov["trades_json_sha256"] == hashes["fills"], "fills differ from the coverage record"
    assert prov["book_json_sha256"] == hashes["book meta"], "book meta differs from the coverage record"
    rel = {name: str(p.relative_to(REPO)).replace("\\", "/") for name, p in FILES}
    listing = "; ".join(f"{name} `{rel[name]}` `{hashes[name]}`" for name, _ in FILES)
    header = (f"**Freeze, {when}; workstream {WS}** (the next number unused in the ledger and the repository). The frozen state is "
              "the content sha256 below. Step 8 compares every file listed with them before the run mode starts, and the hash of `coverage.json` "
              "is also quoted in the vault's kickoff row, pushed at the freeze. The build session commits here locally only, as ruled, "
              "but the repository's scheduled capture rebases main and pushes it to the public origin (second spec-freeze review, S2-B), and a "
              "file cannot carry the hash of the commit that contains it. The local hash of the commit that adds this header is therefore "
              "recorded in the build record's step 7 entry and in the ledger row; on origin it is the commit whose subject begins "
              "\"Fill-placement diagnostic, step 7: freeze\". "
              f"Content sha256 at the freeze: {listing}. The run mode refuses to start unless the engine, the spec, the bars, the fills and the "
              f"book meta hash to the values `coverage.json` carries, the complete fill count is {cov['counts']['complete']:,} and the "
              f"confirmatory count {cov['counts']['confirmatory']:,}, and it stops unless the forward null it draws hashes to the per-draw means "
              "`coverage.json` records (without their centre). The engine is "
              "the PCC fill-timing engine (sha256 `e762808c8d9fa251c75bab88dc7ae76383184167894604684e4f074e7bd6249c`) with amendments (a) to (i) "
              "of the build record; the registration carries amendments 1 to 11 (Amendments before the freeze).")
    pre = STUDY / "PREREG.md"
    s = pre.read_text(encoding="utf-8")
    old = "weekdays verified with the Python `datetime` library. NOT FROZEN, NOT RUN. Workstream number assigned at the freeze."
    new = f"weekdays verified with the Python `datetime` library. FROZEN on {when} (below); NOT RUN.\n\n" + header
    assert s.count(old) == 1, "header anchor not found once (the header may already be written)"
    pre.write_text(s.replace(old, new), encoding="utf-8", newline="\n")
    print(json.dumps(hashes, indent=1))


if __name__ == "__main__":
    main()
