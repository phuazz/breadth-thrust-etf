"""Incremental-vs-full parity guard for the constituent fetch.

Proves, for the given ETFs, that an incremental fetch_constituents run
starting from the last committed constituents store produces EXACTLY the
same data/constituents_{etf}.json and data/breadth_{etf}.json as a
full-history re-fetch run on the same day. This is the house guard (no
unattended path without a verification layer) that had to pass before
incremental became refresh_all.py's default mode on 2026-07-27, and it
remains the operator tool for re-verifying parity after changes to the
fetch or reuse logic.

Sequence per ETF (both legs run in this working tree):
  1. Snapshot the committed data/constituents_{etf}.json and
     data/breadth_{etf}.json bytes.
  2. FULL leg: fetch_constituents --etf X --full, then
     compute_breadth --etf X (downloads prices, writes the parquet cache).
  3. Restore the committed constituents file — the incremental leg must
     start from the last committed snapshot, exactly like the weekly run.
  4. INCREMENTAL leg: fetch_constituents --etf X --incremental, then
     compute_breadth --etf X --reuse-price-cache (same price panel as the
     full leg, so the comparison isolates the fetch mode).
  5. Compare both legs' outputs, excluding only the wall-clock stamps
     (constituents: fetched_at_utc; breadth: computed_at_utc). Everything
     else must be identical.
  6. Restore the committed files. The negative cache and the price cache
     keep whatever the legs wrote (both are shared, order-independent
     state; the full leg's failure recordings are exactly what a normal
     run would have written).

Convergence note: the full leg runs first and records its outcomes in the
negative cache — a hole that resolves during the full leg is cleared from
the cache, so the incremental leg re-attempts it live and both legs agree;
a hole that stays missing is stamped today, so the incremental leg skips
it and carries forward exactly as the full leg did.

Run with a warm data/raw_ishares cache; without one the full leg
re-downloads ~450 CSVs per ETF from iShares (slow, and exposed to
transient anti-bot blocks that would show up as spurious mismatches).

Usage:
    python scripts/verify_incremental_parity.py CSP1 EXV1
    python scripts/verify_incremental_parity.py ICHN --keep-outputs

Exit codes: 0 all ETFs equal / 1 any mismatch or leg failure.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
GUARD_DIR = REPO_ROOT / "logs" / "parity_guard"  # logs/ is gitignored

# Wall-clock stamp fields excluded from the comparison — the ONLY tolerated
# differences between the two legs.
VOLATILE_FIELDS = {
    "constituents": {"fetched_at_utc"},
    "breadth": {"computed_at_utc"},
}
MAX_REPORTED_DIFFS = 20


def run_leg(label: str, cmd: list[str]) -> float:
    """Run one pipeline command, streaming output. Returns elapsed seconds.
    Raises RuntimeError on non-zero exit."""
    print(f"\n--- {label}: {' '.join(cmd[1:])}", flush=True)
    t0 = time.perf_counter()
    rc = subprocess.run(cmd, cwd=REPO_ROOT).returncode
    elapsed = time.perf_counter() - t0
    if rc != 0:
        raise RuntimeError(f"{label} exited {rc}")
    print(f"--- {label}: OK in {elapsed:.1f}s", flush=True)
    return elapsed


def json_diff(a, b, path: str = "", diffs: list[str] | None = None,
              exclude_top: set[str] = frozenset()) -> list[str]:
    """Recursive structural diff; returns dotted paths of differences
    (capped at MAX_REPORTED_DIFFS)."""
    if diffs is None:
        diffs = []
    if len(diffs) >= MAX_REPORTED_DIFFS:
        return diffs
    if isinstance(a, dict) and isinstance(b, dict):
        keys = set(a) | set(b)
        for k in sorted(keys):
            if path == "" and k in exclude_top:
                continue
            sub = f"{path}.{k}" if path else k
            if k not in a:
                diffs.append(f"{sub}: only in incremental leg")
            elif k not in b:
                diffs.append(f"{sub}: only in full leg")
            else:
                json_diff(a[k], b[k], sub, diffs, frozenset())
            if len(diffs) >= MAX_REPORTED_DIFFS:
                return diffs
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            diffs.append(f"{path}: list length {len(a)} vs {len(b)}")
            return diffs
        for i, (xa, xb) in enumerate(zip(a, b)):
            json_diff(xa, xb, f"{path}[{i}]", diffs, frozenset())
            if len(diffs) >= MAX_REPORTED_DIFFS:
                return diffs
    else:
        if a != b:
            diffs.append(f"{path}: {a!r} vs {b!r}")
    return diffs


def compare_files(full_path: Path, inc_path: Path, kind: str) -> list[str]:
    a = json.loads(full_path.read_text(encoding="utf-8"))
    b = json.loads(inc_path.read_text(encoding="utf-8"))
    return json_diff(a, b, diffs=[], exclude_top=VOLATILE_FIELDS[kind])


def verify_etf(etf: str, keep_outputs: bool) -> tuple[bool, str]:
    """Run both legs for one ETF and compare. Returns (ok, summary_line).
    Always restores the committed constituents + breadth files."""
    e = etf.lower()
    consts_path = DATA_DIR / f"constituents_{e}.json"
    breadth_path = DATA_DIR / f"breadth_{e}.json"
    committed = {
        p: p.read_bytes() for p in (consts_path, breadth_path) if p.exists()
    }
    if consts_path not in committed:
        return False, f"{etf}: no committed constituents file — nothing to guard"

    out_dir = GUARD_DIR / etf
    (out_dir / "full").mkdir(parents=True, exist_ok=True)
    (out_dir / "incremental").mkdir(parents=True, exist_ok=True)
    py = sys.executable
    timings: dict[str, float] = {}

    try:
        # ----- FULL leg (runs first: warms the raw + price caches and
        #       records hole outcomes the incremental leg then relies on)
        timings["full_fetch"] = run_leg(
            f"{etf} full fetch",
            [py, "scripts/fetch_constituents.py", "--etf", etf, "--full"])
        timings["full_breadth"] = run_leg(
            f"{etf} full breadth",
            [py, "scripts/compute_breadth.py", "--etf", etf])
        (out_dir / "full" / consts_path.name).write_bytes(consts_path.read_bytes())
        (out_dir / "full" / breadth_path.name).write_bytes(breadth_path.read_bytes())

        # ----- Restore the committed store; INCREMENTAL leg starts from it
        consts_path.write_bytes(committed[consts_path])
        timings["inc_fetch"] = run_leg(
            f"{etf} incremental fetch",
            [py, "scripts/fetch_constituents.py", "--etf", etf, "--incremental"])
        (out_dir / "incremental" / consts_path.name).write_bytes(
            consts_path.read_bytes())

        consts_diffs = compare_files(
            out_dir / "full" / consts_path.name,
            out_dir / "incremental" / consts_path.name,
            "constituents",
        )
        if consts_diffs:
            lines = "\n    ".join(consts_diffs)
            return False, (
                f"{etf}: CONSTITUENTS MISMATCH "
                f"({len(consts_diffs)}+ differing paths):\n    {lines}"
            )

        timings["inc_breadth"] = run_leg(
            f"{etf} incremental breadth",
            [py, "scripts/compute_breadth.py", "--etf", etf,
             "--reuse-price-cache"])
        (out_dir / "incremental" / breadth_path.name).write_bytes(
            breadth_path.read_bytes())

        breadth_diffs = compare_files(
            out_dir / "full" / breadth_path.name,
            out_dir / "incremental" / breadth_path.name,
            "breadth",
        )
        if breadth_diffs:
            lines = "\n    ".join(breadth_diffs)
            return False, (
                f"{etf}: BREADTH MISMATCH "
                f"({len(breadth_diffs)}+ differing paths):\n    {lines}"
            )

        return True, (
            f"{etf}: EQUAL — constituents (ex fetched_at_utc) and breadth "
            f"(ex computed_at_utc) identical between modes. "
            f"full fetch {timings['full_fetch']:.1f}s / "
            f"incremental fetch {timings['inc_fetch']:.1f}s; "
            f"breadth {timings['full_breadth']:.1f}s / "
            f"{timings['inc_breadth']:.1f}s"
        )
    except Exception as exc:
        return False, f"{etf}: guard errored — {exc}"
    finally:
        for p, blob in committed.items():
            p.write_bytes(blob)
        if not keep_outputs:
            pass  # leg copies under logs/parity_guard are cheap; keep for audit


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("etfs", nargs="+", help="ETF symbols to verify, e.g. CSP1 EXV1")
    p.add_argument("--keep-outputs", action="store_true",
                    help="(default behaviour; flag kept for symmetry) leg "
                         "outputs are retained under logs/parity_guard/ "
                         "for audit either way.")
    args = p.parse_args()

    results: list[tuple[bool, str]] = []
    for etf in args.etfs:
        print(f"\n{'='*72}\nPARITY GUARD: {etf}\n{'='*72}", flush=True)
        ok, summary = verify_etf(etf.upper(), args.keep_outputs)
        results.append((ok, summary))
        print(f"\n{summary}", flush=True)

    print(f"\n{'='*72}\nPARITY GUARD SUMMARY\n{'='*72}")
    for ok, summary in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {summary.splitlines()[0]}")
    all_ok = all(ok for ok, _ in results)
    print(f"\n{'ALL EQUAL' if all_ok else 'MISMATCH — do not trust incremental mode until resolved'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
