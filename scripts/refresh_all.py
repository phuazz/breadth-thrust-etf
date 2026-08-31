"""Full local refresh orchestrator — the canonical "weekly refresh" sequence.

Runs the complete dependency chain in the right order:

    1. Per-ETF refresh (38 ETFs = 24 deployed + 14 Europe supersector
       candidates): fetch_constituents + compute_breadth
       (per-constituent price parquets live in data/prices_cache_*.parquet,
        gitignored, so this step is local-only — CI cannot run it.)

       The 14 candidates joined on 2026-08-08. They were previously
       refreshed nowhere, so their price caches existed only on whichever
       machine last captured them by hand; everywhere else their panels
       rebuilt as "skipped — no price cache" and silently kept whatever
       was committed. Including them costs real time on a cold cache
       (full history per constituent, not the incremental top-up the
       deployed 24 get) and is the reason this step now dominates the run.
       They are REFRESHED but not DEPLOYED — see ETFS_ALL vs ETFS_REFRESH.

    2. Aggregated breadth: run_ma200_sweep
       (produces ma200_sweep.json, which feeds the Live Signal chart
        and several Method-tab tables. Easy to forget — the original
        ma200_sweep silent-staleness bug was a missed manual step here.)

    2b. Engine price caches: export_holdings_prices --refresh-caches-only
       (repairs data/{ticker}_ohlc_cache.parquet for every sleeve A and D
        proxy. It MUST precede step 3: on 2026-08-15 Strategy A ran at
        16:17 against a broken SOXX series that this repaired at 16:36,
        publishing sleeve A at 0.76 / 11.2% / +130% against committed
        0.93 / 16.9% / +238% and dragging the blend from 1.24 / +15.0% to
        1.20 / +13.0%. Only the CACHE half moved; the panel export at
        step 6 reads the sleeve JSONs the engines write and stays there.)

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
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(Path(__file__).resolve().parent))
from etf_registry import EUROPE_SUPERSECTORS_CANDIDATE  # noqa: E402

ETFS_NON_SOXX = [
    "CSP1", "CNDX",
    "IUES", "IUFS", "IUIT", "IUHC", "IUIS",
    "IUCS", "IUCD", "IUUS", "IUMS", "IUCM", "IUSP",
    "EXV1", "EXH1", "EXV3", "EXH3", "EXH9",
    "IJPN", "NDIA", "ICHN", "ITWN", "IDP6",
]

# The DEPLOYED set. Not merely a refresh list: check_refresh_guard.py
# imports this as its single source of truth for which panels must share
# an end_friday, and its scope note turns on the 14 candidates being
# outside it. Adding a candidate here would silently put it under those
# alignment checks. Refresh scope is ETFS_REFRESH below — change that.
ETFS_ALL = ["SOXX", *ETFS_NON_SOXX]

# What step 1 actually walks (2026-08-08). The 14 Europe supersector
# candidates were previously refreshed nowhere, so their price caches
# only ever existed on whichever machine last captured them by hand —
# every other machine rebuilt their panels as "skipped, no price cache"
# and silently kept whatever was committed.
#
# They are refreshed but NOT deployed, which is why this is a separate
# name. Candidates are captured for screening; they are not held to the
# shared end_friday, and check_refresh_guard still reads ETFS_ALL.
ETFS_CANDIDATES = list(EUROPE_SUPERSECTORS_CANDIDATE)
ETFS_REFRESH = [*ETFS_ALL, *ETFS_CANDIDATES]

# Pause between ETFs in step 1, to keep the price vendor's rate limiter from
# emptying (2026-08-08).
#
# WHAT HAPPENED. The first 38-ETF run lost EXV5, EXV6, EXV7 and EXV8 in a
# row to YFRateLimitError. Nothing was damaged — compute_breadth now stops
# before writing, so the panels kept their committed contents — but four
# panels went unrefreshed and needed re-running by hand. The scope change
# from 24 to 38 ETFs is what pushed the request volume over the line.
#
# WHY 15 SECONDS. It has to be large enough to matter against a limiter
# measured in requests-per-window and small enough not to dominate a run
# that is otherwise ~25-40 minutes. Measured on the 2026-08-08 run: 37 of
# 38 gaps paused, adding ~9.3 minutes to 36 minutes of step time.
#
# That is the real cost and it is close to the ceiling, not well under it —
# an earlier version of this comment claimed the typical cost would be much
# lower because cache-warm steps would skip. That was wrong on measurement
# (see THROTTLE_SKIP_UNDER_S). The pacing is worth the 9 minutes: the run it
# replaced lost four panels and needed manual repair.
#
# WHAT IT DOES NOT FIX. This paces BETWEEN ETFs; it cannot help WITHIN one.
# A cold-cache panel downloads full history for hundreds of constituents in
# a single step and can exhaust the limiter on its own — ICHN alone carries
# 576 names. If that becomes the failure mode, the answer is retry-with-
# backoff inside the download, not a longer pause out here.
THROTTLE_DEFAULT_S = 15

# Skip the pause after a step this fast, on the theory that it did no
# fetching worth pacing.
#
# MEASURED: IT DOES NOT FIRE, AND SHOULD NOT. Across the 38-ETF run on
# 2026-08-08 the fastest compute_breadth was 10.7s (median 17.1s), so
# nothing fell under 10s — and "Downloading N tickers from yfinance"
# appeared 38 times out of 38. Every step issues a top-up request to bring
# its cache to the latest session, so there is no cache-warm-and-silent
# state for this to detect. Skipping would have been wrong, not merely
# unused.
#
# Kept as a guard rather than deleted: it costs nothing, and it becomes
# live the moment compute_breadth gains a genuine no-fetch path (a
# same-session re-run, or an explicit offline mode). Anyone tuning it should
# know it is currently unreachable, which is why that is written here
# instead of being rediscovered from a timing histogram.
THROTTLE_SKIP_UNDER_S = 10.0


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
    p.add_argument("--throttle", type=float, default=THROTTLE_DEFAULT_S,
                    metavar="SECONDS",
                    help=f"Pause between ETFs in step 1 so the price "
                         f"vendor's rate limiter can refill (default "
                         f"{THROTTLE_DEFAULT_S:g}s). Skipped after a step "
                         f"that finished in under "
                         f"{THROTTLE_SKIP_UNDER_S:g}s, which means it was "
                         f"served from cache. Pass 0 to disable — only "
                         f"sensible on a fully warm cache.")
    args = p.parse_args()

    failures: list[str] = []
    timings: list[tuple[str, float]] = []

    # ----- Step 1: per-ETF constituents + breadth -----
    py = sys.executable
    for i, etf in enumerate(ETFS_REFRESH, start=1):
        # Pace the loop so the price vendor's rate limiter can refill.
        # Skipped before the first ETF, and skipped when the previous
        # compute_breadth was fast: a sub-THROTTLE_SKIP_UNDER_S step served
        # itself from the parquet cache and made no meaningful number of
        # requests, so there is nothing to pace.
        if args.throttle and i > 1 and timings and timings[-1][1] >= THROTTLE_SKIP_UNDER_S:
            print(f"\n  throttle {args.throttle}s before {etf} "
                  f"(previous step took {timings[-1][1]:.0f}s)", flush=True)
            time.sleep(args.throttle)

        if etf == "SOXX" and args.skip_soxx_fetch:
            print(f"\n[{i}/{len(ETFS_REFRESH)}] {etf}: SKIPPED (--skip-soxx-fetch)",
                  flush=True)
        else:
            ok, dt = run_step(
                f"[{i}/{len(ETFS_REFRESH)}] {etf} fetch_constituents",
                [py, "scripts/fetch_constituents.py", "--etf", etf],
            )
            timings.append((f"fetch_constituents {etf}", dt))
            if not ok:
                failures.append(f"fetch_constituents {etf}")
        # BTE_PRICE_SOURCE=norgate is honoured by the sleeve engines directly
        # (they read the env var), but compute_breadth takes it as a flag, so
        # the orchestrator translates. Without this the panels would stay on
        # yfinance while B, C and the A/D proxies moved to Norgate — a book
        # split down the middle by accident rather than by decision, which is
        # worse than either source used consistently.
        _bcmd = [py, "scripts/compute_breadth.py", "--etf", etf]
        if os.environ.get("BTE_PRICE_SOURCE", "").strip().lower() == "norgate":
            _bcmd += ["--price-source", "auto"]
        ok, dt = run_step(
            f"[{i}/{len(ETFS_REFRESH)}] {etf} compute_breadth",
            _bcmd,
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

    # ----- Step 2b: engine-facing price caches (MUST precede step 3) -----
    # Added 2026-08-15. Strategy A ran at 16:17 against a broken SOXX series
    # that export_holdings_prices repaired at 16:36 — three steps too late.
    # The sleeve published Sharpe 0.76 / CAGR 11.2% / +130% against committed
    # 0.93 / 16.9% / +238% and dragged the blend from 1.24 / +15.0% to
    # 1.20 / +13.0%; multi_strategy, portfolio_construction, phase7, phase8,
    # docs/index.html and the factsheet PDF all inherited it silently.
    #
    # This is the half of export_holdings_prices with no dependency on engine
    # output: it takes its symbols from etf_registry and its fetch window from
    # the constituent caches step 1 just wrote. The PANEL half stays at step 6
    # because collect_book_symbols reads the sleeve JSONs the engines write —
    # that is the circular dependency, and splitting the script is what
    # resolves it.
    ok, dt = run_step("engine price caches (repair before the engines read them)",
                       [py, "scripts/export_holdings_prices.py",
                        "--refresh-caches-only"])
    timings.append(("export_holdings_prices --refresh-caches-only", dt))
    if not ok:
        failures.append("export_holdings_prices --refresh-caches-only")

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
        # BEFORE the page builds, because both pages render its output. It
        # reads artefacts off disk and fetches nothing, so it describes the
        # state this refresh just produced. It runs here rather than being an
        # operator step for one reason: live_targets.json is an operator step
        # and sat a week stale on disk, and a freshness widget that is itself
        # stale is worse than none at all.
        # REPORT ONLY, by design. It surfaces isolated vendor holes so the
        # operator sees them; it does NOT fill them, because writing a
        # price the book ranks on is a state-changing action and the vault
        # rule puts a human in front of those. Fill with
        # `python scripts/repair_price_gaps.py --apply` after looking.
        ("price gap probe (isolated vendor holes, report only)",
            "scripts/repair_price_gaps.py"),
        ("strategy_freshness (per-sleeve data reach)",
            "scripts/strategy_freshness.py"),
        # The NEXT fill, ranked on the last close. It was an operator-only
        # step and its JSON sat a WEEK stale on disk, which is exactly the
        # state in which a forward-looking panel misleads worst -- it would
        # show last week's intended trade as this week's. Runs before the
        # page builds so the dashboard renders what this refresh computed.
        ("live_targets (next fill, not yet executed)",
            "scripts/live_targets.py"),
        ("pipeline (build docs/index.html + factsheet PDF)",
            "scripts/pipeline.py"),
        ("simple page (build build/portfolio.html, the reduced public view)",
            "scripts/build_simple_page.py"),
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
    #   - engine price panels: FAIL fails the run. Added 2026-08-15, when
    #     all four checks above passed a sleeve A backtested on a broken
    #     SOXX series. The other four ask whether the panels are fresh,
    #     coherent and correctly paired; this one asks whether the close
    #     series an engine actually priced its universe off exists across
    #     the window it backtested, and it reads the tell directly — a
    #     large days_held beside an annualised return of exactly 0.0.
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
        ("VERIFY engine price panels (sleeve vs the prices it backtested on)",
            [py, "scripts/check_engine_price_panels.py"]),
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
