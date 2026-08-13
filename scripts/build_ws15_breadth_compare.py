"""WS15 — commit the breadth-level comparison the record's tables cite.

Reads the three breadth legs (Aug survivor via `git show`, the committed
corrected panel, and the WS15 residual-fixed leg from --workdir), and writes
reviews/ws15/ws15_breadth_compare.json: median coverage and mean MA-breadth
deltas by year, plus the signal-set differences. Small and committed, so the
record's tables and charts trace to repository artefacts rather than a
scratch directory.

Run: python scripts/build_ws15_breadth_compare.py --workdir <dir>
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
SURVIVOR_COMMIT = "1ada87b"   # last survivor-panel refresh, end 2026-08-07


def _load(blob: dict) -> tuple[dict, pd.DataFrame]:
    s = blob["series"]
    df = pd.DataFrame(
        {k: s[k] for k in ("n_constituents", "n_with_price", "ma_breadth")},
        index=pd.to_datetime(s["dates"]))
    return blob, df


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workdir", required=True)
    args = ap.parse_args()

    surv_txt = subprocess.run(
        ["git", "show", f"{SURVIVOR_COMMIT}:data/breadth_cndx.json"],
        cwd=ROOT, capture_output=True, text=True, check=True).stdout
    surv_b, surv = _load(json.loads(surv_txt))
    corr_b, corr = _load(json.loads(
        (ROOT / "data" / "breadth_cndx.json").read_text(encoding="utf-8")))
    ws15_b, ws15 = _load(json.loads(
        (Path(args.workdir) / "breadth_ws15.json").read_text(encoding="utf-8")))

    years = {}
    for y in range(2018, 2027):
        idx = surv.index.year == y
        cov = lambda d: round(float(
            (d.loc[idx, "n_with_price"] / d.loc[idx, "n_constituents"])
            .median() * 100), 1)
        years[y] = {
            "coverage_median_pct": {
                "survivor": cov(surv), "corrected": cov(corr),
                "ws15": cov(ws15)},
            "ma_breadth_mean_abs_delta_pp": {
                "surv_to_corr": round(float(
                    (corr.loc[idx, "ma_breadth"] - surv.loc[idx, "ma_breadth"])
                    .abs().mean() * 100), 2),
                "corr_to_ws15": round(float(
                    (ws15.loc[idx, "ma_breadth"] - corr.loc[idx, "ma_breadth"])
                    .abs().mean() * 100), 2),
            },
        }

    sets = {k: {s["date"] for s in b["signals"]}
            for k, b in (("survivor", surv_b), ("corrected", corr_b),
                         ("ws15", ws15_b))}
    out = {
        "survivor_commit": SURVIVOR_COMMIT,
        "n_signals": {k: len(v) for k, v in sets.items()},
        "signals_lost_on_correction": sorted(sets["survivor"] - sets["corrected"]),
        "signals_gained_on_correction": sorted(sets["corrected"] - sets["survivor"]),
        "signals_lost_on_ws15": sorted(sets["corrected"] - sets["ws15"]),
        "signals_gained_on_ws15": sorted(sets["ws15"] - sets["corrected"]),
        "by_year": years,
    }
    dest = ROOT / "reviews" / "ws15" / "ws15_breadth_compare.json"
    dest.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"Wrote {dest.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
