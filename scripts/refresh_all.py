"""Full local refresh orchestrator — the canonical "weekly refresh" sequence.

Runs the complete dependency chain in the right order:

    1. Per-ETF refresh (24 ETFs): fetch_constituents + compute_breadth
       (per-constituent price parquets live in data/prices_cache_*.parquet,
        gitignored, so this step is local-only — CI cannot run it.)

    2. Aggregated breadth: run_ma200_sweep
       (produces ma200_sweep.json, which feeds the Live Signal chart
        and several Method-tab tables. Easy to forget — the original
        ma200_sweep silent-staleness bug was a missed manual step here.)

    3. Strategy engines: topk, asset_class, thematic, europe
       (Strategy A/B/C/D headline equity, walk-forward stats, allocations.)

    4. Blend + overlay: multi_strategy, risk_overlay
       (combines sleeves into the deployed 35/35/10/20 + gate + EM tilt.)

    5. Aggregated diagnostics: phase7_bootstrap, phase8_right_tail,
       portfolio_construction
       (these are the three pipeline.py loads that pipeline.py does
        NOT regenerate itself — same silent-staleness class as #2.)

    6. Live / dashboard: mark_to_market_live, export_holdings_prices,
       pipeline (builds docs/index.html + factsheet PDF)

    7. Verification (guard layer, 2026-08-08): the four integrity checks
       run against the state steps 1-6 just wrote, BEFORE the operator
       commits — so a silently-wrong step is caught while the previous
       committed state is still intact. check_capture_integrity (strict
       b,c — matching the weekly factsheet CI semantics, because the
       push this refresh produces IS the factsheet trigger),
       check_pair_integrity, check_refresh_guard (cross-panel coherence
       vs the committed baseline), then check_freshness_headroom
       (informational; exits 0 by design). The first three fail the run;
       a failed run prints in the summary and the operator must not
       commit/push the refreshed state. Then pytest last.

This is the script the user should run weekly (typically Saturday morning,
to catch the Friday close). The CI weekly_factsheet workflow runs a
RESTRICTED subset: only #3 (B + C only — A and D need local price caches),
#4, and the parts of #6 that do not depend on local caches. The committed
outputs of #1, #2, #5 from the most recent local refresh are what CI
operates on.

Usage:
    python scripts/refresh_all.py            # full refresh
    python scripts/refresh_all.py --skip-soxx-fetch
                                              # skip the slow iShares-US
                                              # fetch (Akamai-blocked,
                                              # always carry-forward)
    python scripts/refresh_all.py --no-tests # skip pytest at the end

Each step prints its own wall-clock timing. Failures are recorded but the
script continues — final summary lists any failed steps so the user can
re-run them individually.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

ETFS_NON_SOXX = [
    "CSP1", "CNDX",
    "IUES", "IUFS", "IUIT", "IUHC", "IUIS",
    "IUCS", "IUCD", "IUUS", "IUMS", "IUCM", "IUSP",
    "EXV1", "EXH1", "EXV3", "EXH3", "EXH9",
    "IJPN", "NDIA", "ICHN", "ITWN", "IDP6",
]
ETFS_ALL = ["SOXX", *ETFS_NON_SOXX]


def run_step(label: str, cmd: list[str], cwd: Path = REPO_ROOT) -> tuple[bool, float]:
    """Run a subprocess, stream output, return (ok, elapsed_seconds)."""
    print(f"\n{'='*72}\n{label}\n{'='*72}", flush=True)
    t0 = time.perf_counter()
    try:
        result = subprocess.run(cmd, cwd=cwd, check=False)
        ok = (result.returncode == 0)
    except Exception as e:
        print(f"  EXCEPTION: {e}", flush=True)
        ok = False
    elapsed = time.perf_counter() - t0
    status = "OK" if ok else "FAILED"
    print(f"  [{status}] {elapsed:6.1f}s", flush=True)
    return ok, elapsed


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--skip-soxx-fetch", action="store_true",
                    help="Skip fetch_constituents.py --etf SOXX. Largely "
                         "obsolete since Phase 27 (2026-08-07): SOXX is "
                         "served by the product-data API via "
                         "targetSite=ishares-us, so it no longer depends on "
                         "the Akamai-blocked iShares US route and fetches "
                         "as fast as any other ETF.")
    p.add_argument("--no-tests", action="store_true",
                    help="Skip the final pytest run.")
    args = p.parse_args()

    failures: list[str] = []
    timings: list[tuple[str, float]] = []

    # ----- Step 1: per-ETF constituents + breadth -----
    py = sys.executable
    for i, etf in enumerate(ETFS_ALL, start=1):
        if etf == "SOXX" and args.skip_soxx_fetch:
            print(f"\n[{i}/{len(ETFS_ALL)}] {etf}: SKIPPED (--skip-soxx-fetch)",
                  flush=True)
        else:
            ok, dt = run_step(
                f"[{i}/{len(ETFS_ALL)}] {etf} fetch_constituents",
                [py, "scripts/fetch_constituents.py", "--etf", etf],
            )
            timings.append((f"fetch_constituents {etf}", dt))
            if not ok:
                failures.append(f"fetch_constituents {etf}")
        ok, dt = run_step(
            f"[{i}/{len(ETFS_ALL)}] {etf} compute_breadth",
            [py, "scripts/compute_breadth.py", "--etf", etf],
        )
        timings.append((f"compute_breadth {etf}", dt))
        if not ok:
            failures.append(f"compute_breadth {etf}")

    # ----- Step 2: aggregated breadth sweep -----
    ok, dt = run_step("run_ma200_sweep (aggregates per-ETF breadth)",
                       [py, "scripts/run_ma200_sweep.py"])
    timings.append(("run_ma200_sweep", dt))
    if not ok:
        failures.append("run_ma200_sweep")

    # ----- Step 3: strategy engines -----
    strategy_steps = [
        ("Strategy A (topk_robustness)", "scripts/run_topk_robustness.py"),
        ("Strategy B (asset_class_rotation)", "scripts/run_asset_class_rotation.py"),
        ("Strategy C (thematic_rotation)", "scripts/run_thematic_rotation.py"),
        ("Strategy D (europe_rotation)", "scripts/run_europe_rotation.py"),
    ]
    for label, script in strategy_steps:
        ok, dt = run_step(label, [py, script])
        timings.append((label, dt))
        if not ok:
            failures.append(label)

    # ----- Step 4: blend + overlay -----
    blend_steps = [
        ("multi_strategy blend", "scripts/run_multi_strategy.py"),
        ("risk overlay (regime gate + EM tilt)", "scripts/run_risk_overlay.py"),
    ]
    for label, script in blend_steps:
        ok, dt = run_step(label, [py, script])
        timings.append((label, dt))
        if not ok:
            failures.append(label)

    # ----- Step 5: aggregated diagnostics (the silent-staleness offenders) -----
    diag_steps = [
        ("phase7_bootstrap (CIs on Sharpe)", "scripts/run_phase7_bootstrap.py"),
        ("phase8_right_tail (regime metrics + correlations)",
            "scripts/run_phase8_right_tail.py"),
        ("portfolio_construction (variants table)", "scripts/run_portfolio.py"),
    ]
    for label, script in diag_steps:
        ok, dt = run_step(label, [py, script])
        timings.append((label, dt))
        if not ok:
            failures.append(label)

    # ----- Step 6: live / dashboard -----
    live_steps = [
        ("mark_to_market_live (intra-week NAV)", "scripts/mark_to_market_live.py"),
        ("export_holdings_prices (1Y price series)",
            "scripts/export_holdings_prices.py"),
        ("c_seat_watch (WS7 OOS evidence accumulator)",
            "scripts/run_c_seat_watch.py"),
        ("pipeline (build docs/index.html + factsheet PDF)",
            "scripts/pipeline.py"),
    ]
    for label, script in live_steps:
        ok, dt = run_step(label, [py, script])
        timings.append((label, dt))
        if not ok:
            failures.append(label)

    # ----- Step 7: verification (guard layer) -----
    # These validate the state steps 1-6 just wrote, before the operator
    # commits. Failure semantics:
    #   - capture integrity: FAIL fails the run. --strict b,c mirrors the
    #     weekly factsheet workflow — the push produced by this refresh
    #     triggers that workflow, so a B/C series missing the newest
    #     Friday bar must be caught HERE, not after the email went out
    #     (2026-07-17 fencepost incident).
    #   - pair integrity: FAIL fails the run. A priced fund that does not
    #     move with its own constituent basket invalidates every signal
    #     for that member (EXH3/EXH4 defect class). SKIP on thin data is
    #     not a failure by design.
    #   - refresh guard: FAIL fails the run. Cross-panel coherence — one
    #     shared end_friday, healthy endpoints, no critical staleness,
    #     breadth ending on each ETF's own calendar, and no state lost
    #     versus the committed baseline.
    #   - freshness headroom: informational tripwire, exits 0 by design
    #     (it forecasts the CI hard guard; right after a refresh it
    #     should report lag 0-1). Recorded for timing only.
    verify_steps = [
        ("VERIFY capture integrity (--strict b,c)",
            [py, "scripts/check_capture_integrity.py",
             "--targets", "all", "--strict", "b,c"]),
        ("VERIFY pair integrity (fund vs own constituents)",
            [py, "scripts/check_pair_integrity.py"]),
        ("VERIFY refresh guard (cross-panel coherence)",
            [py, "scripts/check_refresh_guard.py"]),
        ("VERIFY freshness headroom (informational)",
            [py, "scripts/check_freshness_headroom.py"]),
    ]
    for label, cmd in verify_steps:
        ok, dt = run_step(label, cmd)
        timings.append((label, dt))
        if not ok:
            failures.append(label)

    if not args.no_tests:
        ok, dt = run_step("pytest (regression suite)",
                           [py, "-m", "pytest", "tests/", "-q"])
        timings.append(("pytest", dt))
        if not ok:
            failures.append("pytest")

    # ----- Summary -----
    total = sum(t for _, t in timings)
    print(f"\n{'='*72}\nREFRESH SUMMARY (total {total:.0f}s = {total/60:.1f}m)\n{'='*72}")
    for label, dt in timings:
        print(f"  {dt:7.1f}s  {label}")
    if failures:
        print(f"\n{len(failures)} FAILED STEP(S):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nAll steps OK. Review `git status`, commit, push.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
