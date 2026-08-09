"""Dashboard pipeline — inject result JSONs into template.html, write docs/index.html.

Phase 3 dashboard (5 tabs):
  - Monitor   : per-ETF live state cards (today's allocation under MA200 + 50/150)
  - Strategy  : MA200 verdict, per-ETF comparison table, threshold robustness chart
  - ETF Detail: drill-in — breadth time series, equity curve, long episodes table
  - Portfolio : relative-strength portfolio construction (top-K by breadth)
  - Method    : signal definition, data sources, OOS validation, caveats

Inputs (loaded automatically):
  - data/ma200_sweep.json           (per-ETF sweep + monitor + detail blocks)
  - data/portfolio_construction.json (top-K portfolio variants vs benchmarks)

Output:
  - docs/index.html  (GitHub Pages root)

Per CLAUDE.md:
  - White theme, sans-serif, high contrast.
  - template.html stays under 200 KB. Built artefact will be ~1-3 MB
    due to inlined per-ETF equity / breadth series — NEVER re-read it
    into Claude context.

Run:
    python scripts/pipeline.py
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from etf_registry import ETF_REGISTRY, display_ticker_map  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
TEMPLATE = PROJECT_ROOT / "template.html"
DOCS = PROJECT_ROOT / "docs"
OUT = DOCS / "index.html"


PLACEHOLDER_START = "// __DASHBOARD_DATA_START__"
PLACEHOLDER_END = "// __DASHBOARD_DATA_END__"


def load_ma200() -> dict | None:
    """Load data/ma200_sweep.json. Trims winner_equity_curves to only the
    series the dashboard actually renders (family_b, family_d, buy_and_hold)
    — family_a and family_c equity curves stay in the source JSON for
    reproducibility but are dropped from the inlined dashboard payload to
    keep docs/index.html under ~3 MB.
    """
    path = DATA_DIR / "ma200_sweep.json"
    if not path.exists():
        return None
    blob = json.loads(path.read_text(encoding="utf-8"))
    if "winner_equity_curves" in blob:
        trimmed = {}
        for etf, ws in blob["winner_equity_curves"].items():
            trimmed[etf] = {
                k: v for k, v in ws.items()
                if k in ("family_b", "family_d", "buy_and_hold")
            }
        blob["winner_equity_curves"] = trimmed
    return blob


def load_portfolio() -> dict | None:
    path = DATA_DIR / "portfolio_construction.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_extended_history() -> dict | None:
    path = DATA_DIR / "extended_history.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_topk_robustness() -> dict | None:
    """Load data/topk_robustness.json: rebalance-frequency grid + trade history
    for the top-K rotation headline variant (K=7 weekly Friday)."""
    path = DATA_DIR / "topk_robustness.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_asset_class() -> dict | None:
    """Load data/asset_class_rotation.json: Strategy B (Phase 2) — asset-class
    momentum rotation across 14 broad ETFs (US equity / Intl / EM / RE /
    commodities / bonds)."""
    path = DATA_DIR / "asset_class_rotation.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_thematic() -> dict | None:
    """Load data/thematic_rotation.json: Strategy C (Phase 3) — thematic
    momentum rotation across 16 thematic ETFs (AI / cyber / clean energy /
    biotech / commodity-equity / etc) with signal floor + per-ETF cap."""
    path = DATA_DIR / "thematic_rotation.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_europe() -> dict | None:
    """Load data/europe_rotation.json: Strategy D (Phase 4) — Europe sector
    breadth top-K rotation across 5 Stoxx Europe 600 sector UCITS ETFs
    (EXV1 banks / EXH1 energy / EXV3 tech / EXH3 industrial goods & services,
    traded as EXH4.DE / EXH9 utilities)."""
    path = DATA_DIR / "europe_rotation.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_multi_strategy() -> dict | None:
    """Load data/multi_strategy.json: combinations of Strategy A + Strategy B
    — fixed-weight blends (70/30, 50/50, 30/70) and meta-rotation."""
    path = DATA_DIR / "multi_strategy.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_bootstrap() -> dict | None:
    """Load data/phase7_bootstrap.json: block-bootstrap CIs on per-strategy
    Sharpe and on key paired differentials (deployed vs baseline, etc).
    Block size 60 trading days, 2000 samples."""
    path = DATA_DIR / "phase7_bootstrap.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_right_tail() -> dict | None:
    """Load data/phase8_right_tail.json: Sortino, skewness, rolling 12m
    extremes, regime decomposition, % months as top sleeve. Surfaces the
    optionality-side metrics that Sharpe ratios alone underrate."""
    path = DATA_DIR / "phase8_right_tail.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_risk_overlay() -> dict | None:
    """Load data/risk_overlay.json — Phase 19 breadth regime gate
    diagnostics + gated equity variants. Built by
    scripts/run_risk_overlay.py after multi_strategy.json is built.

    The pipeline merges its ``gated_variants`` into
    window.DATA.multi.strategies (so the existing chart code finds the
    gated key) and its top-level diagnostics into
    window.DATA.multi.regime_gate (for the live regime badge). This
    preserves the user-facing dashboard shape while the architecture
    keeps the overlay as a separate concern from blend construction.
    """
    path = DATA_DIR / "risk_overlay.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_holdings_prices() -> dict | None:
    """Load data/holdings_prices_1y.json — per-ETF 1Y daily close prices
    used by the Monitor tab's holdings click-to-expand mini-chart.
    Built by scripts/export_holdings_prices.py from existing price caches.
    """
    path = DATA_DIR / "holdings_prices_1y.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_live_track() -> dict | None:
    """Load data/live_track.json — daily mark-to-market overlay on the
    deployed blend (built by scripts/mark_to_market_live.py).

    Optional: when present, pipeline.py extends the deployed blend
    equity series in-memory so the dashboard's WTD card and Performance
    chart automatically include intra-week NAV points without any
    extra JS. The Friday-anchor full-backtest series in
    risk_overlay.json is unchanged on disk — the extension is a
    dashboard-render concern only.
    """
    path = DATA_DIR / "live_track.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_robustness() -> dict | None:
    """Load data/robustness.json into a slim payload for the Risk & Validation
    tab.

    Phase 9 cleanup (2026-05-24): keeps only the fields actually rendered by
    the dashboard:
      - test_10_wf_topk_portfolio  (paradigm comparison + WF K refit segments)
      - test_11 lives in topk_robustness.json, not here
      - test_12 lives in ma200_sweep.json's per-ETF breadth, not here

    Legacy fields kept ONLY because renderRobustnessParadigm reads them to
    compute paradigm-1 and paradigm-2 comparison rows (the strategic case
    for top-K rotation over per-ETF L tuning):
      - test_1_walk_forward_l    (paradigm 1: per-ETF tuned L)
      - test_8_fixed_L            (paradigm 2: fixed L = 60)
      - The wf_dates / wf_equity per-ETF sub-arrays inside test_1 are
        stripped — they were never rendered.

    All other test_N_* legacy fields are dropped from the inlined payload
    (still in the source robustness.json on disk for archival
    reproducibility, but not shipped to the browser).
    """
    path = DATA_DIR / "robustness.json"
    if not path.exists():
        return None
    blob = json.loads(path.read_text(encoding="utf-8"))
    # Strip heavy sub-arrays from test_1 (the only legacy field we keep)
    wf = blob.get("test_1_walk_forward_l", {})
    for etf in wf:
        wf[etf].pop("wf_dates", None)
        wf[etf].pop("wf_equity", None)
    # Drop the non-rendered legacy fields entirely
    legacy_drop = [
        "test_2_borrow_cost",
        "test_3_sub_periods",
        "test_4_bootstrap",
        "test_5_ma_period_csp1",
        "test_5b_ma_period_soxx",
        "test_6_rebalance_freq",
        "test_7_wf_x_cadence",
    ]
    for k in legacy_drop:
        blob.pop(k, None)
    return blob


def assert_series_not_frozen(name: str, dates: list, values: list,
                              trailing_n: int = 20) -> None:
    """Hard-fail the pipeline if the trailing N values of a signal series
    are all identical — the canonical 'frozen breadth' bug condition.

    This is the check that would have caught the Phase 4 silent-freeze
    incident, where ``compute_ma200_breadth`` quietly held the last good
    value for non-US ETFs whose constituents had sparse missing days. A
    perfectly-flat trailing window on what should be a daily-updated
    signal is overwhelmingly evidence of a stuck data path, not a real
    market state — flag and abort.

    Args:
        name: human-readable label for error messages.
        dates: parallel date list (used only in the error string).
        values: numeric series. Non-numeric / None entries are ignored.
        trailing_n: window length. 20 trading days is roughly 4 weeks —
            short enough to catch a fresh freeze, long enough that a
            real low-volatility regime does not falsely trigger.

    Raises:
        RuntimeError: when the last ``trailing_n`` non-NaN values are all
            equal to the same number.
    """
    import math
    if not values or len(values) < trailing_n:
        return
    tail = values[-trailing_n:]
    tail_dates = dates[-trailing_n:] if dates and len(dates) >= trailing_n else []
    # Drop None and NaN — they are legitimately missing, not "frozen".
    clean = [v for v in tail if v is not None
              and not (isinstance(v, float) and math.isnan(v))]
    if len(clean) < trailing_n:
        # Some missing values in the window is fine — only check the
        # remaining ones for repetition. But require at least N/2 real
        # points to make the check meaningful.
        if len(clean) < trailing_n // 2:
            return
    if len(set(clean)) == 1:
        first_dt = tail_dates[0] if tail_dates else "?"
        last_dt = tail_dates[-1] if tail_dates else "?"
        raise RuntimeError(
            f"Pipeline aborted: {name} appears FROZEN — "
            f"all {len(clean)} trailing values equal "
            f"{clean[0]!r} from {first_dt} to {last_dt}. This is the "
            f"Phase 4 stuck-breadth bug condition. Re-run the upstream "
            f"data generator and verify constituent prices are being "
            f"refreshed."
        )


def assert_source_panel_fresh_vs_today(
    panel_path: Path,
    today: date,
    max_lag_trading_days: int = 5,
) -> None:
    """Hard-fail when a source breadth panel's ``end_date`` lags today by
    more than ``max_lag_trading_days`` trading days.

    Counterpart to ``assert_derived_not_stale_vs_source``: that function
    only catches the case "I refreshed sources but forgot to regenerate
    derived". This one catches the case the 2026-03-27 -> 2026-06-18
    incident actually was: the SOURCE panel itself stopped advancing
    while the daily-mark-to-market workflow kept extending downstream
    artefacts. Nothing in the chain anchored to ``today`` until now.

    The Phase 28 ``assert_derived_not_stale_vs_source`` check compares
    derived vs source mtimes only; both files can drift together if
    nothing is comparing either to real time. This is what allowed the
    breadth panel to publish a confident regime headline for 11 weeks
    while the actual market reading had silently triggered a de-risk.

    Args:
        panel_path: source JSON whose ``end_date`` is the freshness anchor
            (typically ``data/breadth_csp1.json`` — the panel feeding the
            Phase 19 regime gate).
        today: the build's reference date. Pass ``date.today()`` from
            ``main()``.
        max_lag_trading_days: budget. Default 5 = one full trading week,
            matching ``regime_publish.DEFAULT_BUDGET_TRADING_DAYS``.

    Raises:
        RuntimeError: when the lag exceeds the budget. Message names
            ``refresh_all.py`` as the fix. CI fails the build before
            publish; local dev can override with ``ALLOW_STALE_REGIME=1``
            (checked at the call site).
    """
    if not panel_path.exists():
        return  # Missing-source warning handled elsewhere.
    try:
        blob = json.loads(panel_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise RuntimeError(
            f"Pipeline aborted: could not read {panel_path.name} for "
            f"freshness check: {e}"
        )
    end_iso = blob.get("end_date")
    if not end_iso:
        raise RuntimeError(
            f"Pipeline aborted: {panel_path.name} has no end_date — the "
            "regime publish freshness gate cannot verify the panel. "
            "Re-run scripts/compute_breadth.py for this ETF."
        )
    import numpy as _np
    end_date_obj = date.fromisoformat(end_iso)
    if today <= end_date_obj:
        return
    lag = int(_np.busday_count(end_iso, today.isoformat()))
    if lag > max_lag_trading_days:
        raise RuntimeError(
            f"Pipeline aborted: {panel_path.name} end_date={end_iso} is "
            f"{lag} trading days behind today ({today.isoformat()}). "
            f"Budget is {max_lag_trading_days} trading days. The Phase 19 "
            "regime gate that drives the de-risk decision reads this "
            "panel — publishing a regime headline off a stale panel is "
            "the failure mode the 2026-03-27 -> 2026-06-18 incident "
            "exposed. Run `python scripts/refresh_all.py` (or set "
            "ALLOW_STALE_REGIME=1 for a one-off local dev rebuild)."
        )


def assert_derived_not_stale_vs_source(
    derived: Path, sources: list[Path], max_lag_days: int = 7,
) -> None:
    """Hard-fail when a derived JSON's file mtime trails its sources'
    mtimes by more than ``max_lag_days``.

    Guards against the silent-staleness class that the Live Signal
    chart issue (2026-06-17) exposed: ``ma200_sweep.json``,
    ``phase7_bootstrap.json``, ``phase8_right_tail.json``, and
    ``portfolio_construction.json`` all aggregate or derive from the
    strategy outputs (``multi_strategy.json``, ``breadth_*.json``,
    etc.) but pipeline.py does not regenerate them itself. When the
    user re-ran the strategy engines without also re-running these
    derivations, the dashboard rendered the new strategy lines next
    to old bootstrap CIs / old correlation matrix / old breadth
    sweep — silently mixing data vintages.

    The mtime check is sufficient because the bug is "I refreshed
    sources but forgot to regenerate derived" — in that case the
    derived file's mtime literally predates the source's. We do NOT
    check JSON ``computed_at_utc`` because not every script writes
    one and we want a single uniform check.

    Args:
        derived: path to the aggregated/derived JSON that the
            dashboard renders.
        sources: list of source JSON paths the derived file should
            reflect. Newest source mtime is the reference.
        max_lag_days: tolerance window. Default 7 days — enough for
            a normal weekly cycle (Saturday refresh) where the
            derivation runs hours after sources, but tight enough
            that a missed weekly catches at the next pipeline build.

    Raises:
        RuntimeError: when ``derived`` is older than the newest
            source by more than ``max_lag_days``. Message names the
            fix command (``python scripts/refresh_all.py``).
    """
    if not derived.exists():
        return  # missing file is a separate problem; not our concern here
    existing_sources = [s for s in sources if s.exists()]
    if not existing_sources:
        return
    src_mtime = max(s.stat().st_mtime for s in existing_sources)
    der_mtime = derived.stat().st_mtime
    lag_seconds = src_mtime - der_mtime
    lag_days = lag_seconds / 86400.0
    if lag_days > max_lag_days:
        newest = max(existing_sources, key=lambda s: s.stat().st_mtime).name
        raise RuntimeError(
            f"Pipeline aborted: {derived.name} is {lag_days:.1f} days older "
            f"than its source {newest}. The dashboard would render new "
            f"strategy outputs next to a stale aggregation — silent "
            f"data-vintage mixing. Run `python scripts/refresh_all.py` "
            f"(or just the matching `scripts/run_*.py`) to regenerate."
        )


def assert_no_conflict_markers(path: Path) -> None:
    """Hard-fail if a built artefact contains unresolved git merge
    conflict markers.

    Guards against the dashboard-corruption regression seen on
    2026-05-30 where a ``git stash pop`` after the bot's auto-rebuild
    left ``<<<<<<<`` / ``>>>>>>>`` lines inside docs/index.html. The
    JS payload became a syntax error, init() never ran, the watchdog
    never fired, and the whole dashboard hung on 'Loading…'.

    Checked once per write: docs/index.html, docs/factsheet_meta.json,
    docs/factsheet_latest.pdf header (PDFs do not normally contain
    these strings but we check anyway — costs nothing).

    Only flags markers at the START of a line, which is the true
    conflict-marker convention. A literal ``<<<<<<<`` string inside
    a quoted JS literal would be flagged too only if it began a line —
    in practice that does not happen.
    """
    MARKERS = ("<<<<<<<", ">>>>>>>")
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return  # binary file or unreadable — skip
    for lineno, line in enumerate(text.splitlines(), 1):
        for m in MARKERS:
            if line.startswith(m):
                raise RuntimeError(
                    f"Pipeline aborted: {path.name} contains an "
                    f"unresolved git merge conflict marker at line "
                    f"{lineno}: {line[:80]!r}. The output would be a "
                    "parse error in the browser. Resolve the conflict "
                    "in source (template.html or the upstream JSON) "
                    "and rebuild."
                )


def assert_built_at_valid(ts: str | None) -> None:
    """Hard-fail the pipeline if the 'Last updated:' timestamp is empty
    or malformed.

    This guards against the silent-empty-timestamp bug surfaced in the
    review: prior to this assertion an upstream change that wiped the
    ``built_at`` field would have published a dashboard with a blank
    'Last updated:' footer and no build-time error. The check uses the
    ``datetime`` library (not string parsing) per the date-handling rule
    in CLAUDE.md, and is exercised by ``tests/test_built_at_assertion.py``
    including month- and year-boundary cases.

    Format invariant: 'YYYY-MM-DD HH:MM UTC' (matches the formatter at
    line ~374). If you change one, change the other and update the test.
    """
    fmt = "%Y-%m-%d %H:%M UTC"
    if ts is None or not ts.strip():
        raise RuntimeError(
            "Pipeline aborted: built_at timestamp is empty. "
            "The 'Last updated:' span in docs/index.html would render "
            "blank — fix the upstream timestamp generation before "
            "publishing."
        )
    try:
        parsed = datetime.strptime(ts, fmt)
    except ValueError as exc:
        raise RuntimeError(
            f"Pipeline aborted: built_at value {ts!r} does not match "
            f"required format {fmt!r}. This will break the dashboard "
            "footer date display."
        ) from exc
    # Sanity-check the timestamp is within +/- 24h of now. Anything
    # outside that window suggests a date-library bug (timezone confusion
    # or month-indexing error) rather than a legitimate value.
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    drift_seconds = abs((now - parsed).total_seconds())
    if drift_seconds > 24 * 3600:
        raise RuntimeError(
            f"Pipeline aborted: built_at {ts!r} is "
            f"{drift_seconds/3600:.1f}h off current UTC "
            f"({now.strftime(fmt)}). Likely a date-library bug — "
            "verify month indexing (JS 0-indexed vs Python 1-indexed) "
            "and timezone handling at the call site."
        )


# ============================================================================
# Data Health monitor (Phase 28.1)
# ============================================================================
# Surfaces every monitored data feed's freshness in one place — a Data Health
# tab + persistent header badge — so the user can see at a glance whether the
# signals on the screen reflect today's market or last month's. The pipeline
# computes the report at build time and injects it under window.DATA.data_health;
# the dashboard renders it without further work.
#
# The existing per-sleeve "data as of" prefixes (Phase 2.3) and constituent
# staleness warnings (Phase 26.1) cover sub-cases of this; this report unifies
# them and ADDS coverage for the derived JSONs (ma200_sweep, phase7_bootstrap,
# phase8_right_tail, portfolio_construction) that the silent-staleness bug of
# 2026-06-17 was hiding inside.

DATA_HEALTH_CHECKS: list[dict] = [
    # Live / daily-update feeds — narrow tolerance
    {"key": "live_track", "file": "live_track.json", "group": "live",
     "label": "Live mark-to-market (intra-week NAV splice)",
     "warn": 3, "stale": 7,
     "fix": "GitHub Actions daily_live_track or `python scripts/mark_to_market_live.py`"},
    {"key": "holdings_prices", "file": "holdings_prices_1y.json", "group": "live",
     "label": "Holdings 1Y price series (for click-expand mini-charts)",
     "warn": 3, "stale": 7,
     "fix": "`python scripts/export_holdings_prices.py`"},
    # Strategy-engine outputs — weekly cadence
    {"key": "topk", "file": "topk_robustness.json", "group": "strategy",
     "label": "Strategy A — US Sector Rotation (top-K)",
     "warn": 8, "stale": 14,
     "fix": "`python scripts/run_topk_robustness.py`"},
    {"key": "asset_class", "file": "asset_class_rotation.json", "group": "strategy",
     "label": "Strategy B — Asset Class Rotation",
     "warn": 8, "stale": 14,
     "fix": "`python scripts/run_asset_class_rotation.py`"},
    {"key": "thematic", "file": "thematic_rotation.json", "group": "strategy",
     "label": "Strategy C — Thematic Rotation",
     "warn": 8, "stale": 14,
     "fix": "`python scripts/run_thematic_rotation.py`"},
    {"key": "europe", "file": "europe_rotation.json", "group": "strategy",
     "label": "Strategy D — Europe Sector Rotation",
     "warn": 8, "stale": 14,
     "fix": "`python scripts/run_europe_rotation.py`"},
    {"key": "multi", "file": "multi_strategy.json", "group": "strategy",
     "label": "Multi-strategy blend (deployed 4-way)",
     "warn": 8, "stale": 14,
     "fix": "`python scripts/run_multi_strategy.py`"},
    {"key": "risk_overlay", "file": "risk_overlay.json", "group": "strategy",
     "label": "Risk Overlay (regime gate + EM tilt)",
     "warn": 8, "stale": 14,
     "fix": "`python scripts/run_risk_overlay.py`"},
    # Aggregated derivations — the silent-staleness offenders
    {"key": "ma200_sweep", "file": "ma200_sweep.json", "group": "derived",
     "label": "MA200 sweep (feeds Live Signal breadth chart)",
     "warn": 8, "stale": 14,
     "fix": "`python scripts/run_ma200_sweep.py`"},
    {"key": "phase7_bootstrap", "file": "phase7_bootstrap.json", "group": "derived",
     "label": "Bootstrap CIs on Sharpe (Phase 7)",
     "warn": 8, "stale": 14,
     "fix": "`python scripts/run_phase7_bootstrap.py`"},
    {"key": "phase8_right_tail", "file": "phase8_right_tail.json", "group": "derived",
     "label": "Right-tail / regime metrics + sleeve correlations (Phase 8)",
     "warn": 8, "stale": 14,
     "fix": "`python scripts/run_phase8_right_tail.py`"},
    {"key": "portfolio_construction", "file": "portfolio_construction.json", "group": "derived",
     "label": "Portfolio variants comparison table",
     "warn": 8, "stale": 14,
     "fix": "`python scripts/run_portfolio.py`"},
]
# Per-ETF breadth panels and constituent rosters are added dynamically by
# _compute_data_health below so the registry stays compact.


def _last_data_date_from(blob: dict) -> str | None:
    """Best-effort extract of the LATEST YYYY-MM-DD date anywhere in a
    result JSON. Deep-walks the tree and returns the maximum date string
    encountered, or None if the tree contains no recognisable dates.

    Deliberately does NOT short-circuit on explicit keys like
    ``end_date`` / ``anchor_date`` — live_track.json has both an
    ``anchor_date`` of the most-recent-Friday close AND a ``live_dates``
    array that extends through today's intra-week splice. Stopping at
    ``anchor_date`` would mark a freshly-refreshed file as 14+ days
    stale. Always scan for the true maximum.
    """
    best: list[str] = [""]
    def walk(o):
        if isinstance(o, str):
            # Match strict ISO YYYY-MM-DD prefix
            if len(o) >= 10 and o[4] == "-" and o[7] == "-" and o[:4].isdigit():
                if o[:10] > best[0]:
                    best[0] = o[:10]
        elif isinstance(o, dict):
            for vv in o.values(): walk(vv)
        elif isinstance(o, list):
            for vv in o: walk(vv)
    walk(blob)
    return best[0] or None


def _classify(days_old: int | None, warn: int, stale: int) -> str:
    if days_old is None: return "unknown"
    if days_old >= stale: return "stale"
    if days_old >= warn: return "warn"
    return "ok"


def _compute_data_health(today: date) -> dict:
    """Build the centralised data health report. Walks every monitored
    file, computes status, returns a dict suitable for
    window.DATA.data_health. Never raises — a single file read failure
    becomes a 'broken' row, not a pipeline abort."""
    import os as _os
    from datetime import datetime as _dt
    rows: list[dict] = []

    def _process(check: dict) -> None:
        fp = DATA_DIR / check["file"]
        row = {
            "key": check["key"],
            "file": check["file"],
            "label": check["label"],
            "group": check["group"],
            "fix": check.get("fix", ""),
            "thresholds": {"warn": check["warn"], "stale": check["stale"]},
        }
        if not fp.exists():
            row["status"] = "broken"
            row["last_data_date"] = None
            row["days_old"] = None
            row["mtime"] = None
            row["note"] = "file missing"
            rows.append(row)
            return
        try:
            blob = json.loads(fp.read_text(encoding="utf-8"))
        except Exception as e:
            row["status"] = "broken"
            row["last_data_date"] = None
            row["days_old"] = None
            row["mtime"] = None
            row["note"] = f"unreadable: {e}"
            rows.append(row)
            return
        last = _last_data_date_from(blob)
        mtime_ts = _os.path.getmtime(fp)
        mtime_iso = _dt.utcfromtimestamp(mtime_ts).strftime("%Y-%m-%dT%H:%M:%SZ")
        if last:
            try:
                days = (today - date.fromisoformat(last)).days
            except ValueError:
                days = None
        else:
            # Fallback to file mtime
            days = (today - date.fromtimestamp(mtime_ts)).days
        row["status"] = _classify(days, check["warn"], check["stale"])
        row["last_data_date"] = last
        row["days_old"] = days
        row["mtime"] = mtime_iso
        rows.append(row)

    # Registered explicit checks
    for c in DATA_HEALTH_CHECKS:
        _process(c)

    # Per-ETF threshold overrides for known-blocked endpoints. SOXX is the
    # only Strategy A member sourced from iShares US (Akamai-blocked from
    # CI and increasingly from residential IPs as of 2026-06); roster
    # carry-forward + EDGAR fallback keep it operational for up to ~60
    # days. Without an override SOXX would chronically register as
    # warn/stale and dilute the signal value of the badge.
    THRESHOLD_OVERRIDES = {
        "breadth_soxx": {"warn": 21, "stale": 60},
        "constituents_soxx": {"warn": 30, "stale": 90},
    }

    # Per-ETF breadth panels — single group, weekly cadence
    for fp in sorted(DATA_DIR.glob("breadth_*.json")):
        etf = fp.stem.replace("breadth_", "").upper()
        key = f"breadth_{etf.lower()}"
        warn = THRESHOLD_OVERRIDES.get(key, {}).get("warn", 8)
        stale = THRESHOLD_OVERRIDES.get(key, {}).get("stale", 14)
        _process({
            "key": key,
            "file": fp.name, "group": "breadth",
            "label": f"Breadth panel — {etf}",
            "warn": warn, "stale": stale,
            "fix": f"`python scripts/compute_breadth.py --etf {etf}`",
        })

    # Per-ETF constituent rosters
    for fp in sorted(DATA_DIR.glob("constituents_*.json")):
        etf = fp.stem.replace("constituents_", "").upper()
        key = f"constituents_{etf.lower()}"
        warn = THRESHOLD_OVERRIDES.get(key, {}).get("warn", 14)
        stale = THRESHOLD_OVERRIDES.get(key, {}).get("stale", 60)
        _process({
            "key": key,
            "file": fp.name, "group": "constituents",
            "label": f"Constituent roster — {etf}",
            "warn": warn, "stale": stale,
            "fix": f"`python scripts/fetch_constituents.py --etf {etf}`",
        })

    counts = {"ok": 0, "warn": 0, "stale": 0, "broken": 0, "unknown": 0}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    if counts["broken"] or counts["stale"]:
        overall = "stale"
    elif counts["warn"]:
        overall = "warn"
    else:
        overall = "ok"
    return {
        "computed_at_iso": _dt.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "today": today.isoformat(),
        "overall_status": overall,
        "counts": counts,
        "rows": rows,
    }


def inject(template_text: str, data: dict) -> str:
    payload_json = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    replacement = (
        f"{PLACEHOLDER_START}\n"
        f"const DASHBOARD_DATA_INLINE = {payload_json};\n"
        f"{PLACEHOLDER_END}"
    )
    start_idx = template_text.find(PLACEHOLDER_START)
    end_idx = template_text.find(PLACEHOLDER_END)
    if start_idx == -1 or end_idx == -1:
        raise RuntimeError(
            f"Could not find placeholder markers in {TEMPLATE}. "
            f"Expected '{PLACEHOLDER_START}' and '{PLACEHOLDER_END}'."
        )
    return (
        template_text[:start_idx]
        + replacement
        + template_text[end_idx + len(PLACEHOLDER_END) :]
    )


def main() -> int:
    # Freshness guard: every derived JSON the dashboard renders live must
    # not lag its sources by more than a week. Catches the silent-
    # staleness class that surfaced on 2026-06-17 (Live Signal chart
    # frozen at May 15 because ma200_sweep.json had not been regenerated
    # after the breadth_*.json sources refreshed). See
    # assert_derived_not_stale_vs_source docstring for the full story.
    breadth_sources = sorted((DATA_DIR).glob("breadth_*.json"))
    multi = DATA_DIR / "multi_strategy.json"
    assert_derived_not_stale_vs_source(
        DATA_DIR / "ma200_sweep.json", breadth_sources, max_lag_days=7,
    )
    assert_derived_not_stale_vs_source(
        DATA_DIR / "phase7_bootstrap.json", [multi], max_lag_days=7,
    )
    assert_derived_not_stale_vs_source(
        DATA_DIR / "phase8_right_tail.json", [multi], max_lag_days=7,
    )
    assert_derived_not_stale_vs_source(
        DATA_DIR / "portfolio_construction.json", [multi], max_lag_days=7,
    )

    # Phase 28.5 P3 — source-panel freshness anchor. The Phase 28 derived-
    # vs-source mtime check above only catches "derived forgot to refresh
    # after source moved"; it does NOT catch "source itself stopped
    # advancing" — exactly the 2026-03-27 -> 2026-06-18 incident. This
    # check anchors the breadth_csp1 panel (the one feeding the Phase 19
    # regime gate that decides de-risk) to today's date. ALLOW_STALE_REGIME
    # is the local-dev escape hatch; CI never sets it.
    import os as _os
    if not _os.environ.get("ALLOW_STALE_REGIME"):
        assert_source_panel_fresh_vs_today(
            DATA_DIR / "breadth_csp1.json",
            today=date.today(),
            max_lag_trading_days=5,
        )

    print("Loading MA200 sweep ...", flush=True)
    ma200 = load_ma200()
    if ma200:
        n_etfs = len(ma200.get("monitor", {}))
        print(f"  {n_etfs} ETFs in monitor block")
        in_long = sum(1 for c in ma200.get("monitor", {}).values() if c.get("in_long_state"))
        print(f"  {in_long}/{n_etfs} ETFs currently in LONG-leveraged state")
    else:
        print("  WARNING: no ma200_sweep.json found")

    print("Loading portfolio construction ...", flush=True)
    portfolio = load_portfolio()
    if portfolio:
        print(f"  {len(portfolio.get('results', {}))} portfolio variants")
    else:
        print("  WARNING: no portfolio_construction.json found")

    print("Loading robustness ...", flush=True)
    robustness = load_robustness()
    if robustness:
        wf = robustness.get("test_1_walk_forward_l", {})
        print(f"  walk-forward L for {len(wf)} ETFs")

    # Phase 9 cleanup: extended_history.json was the input to Robustness
    # Test 9 (per-ETF L-threshold 2000-2026 backtest), which is legacy.
    # The renderer was removed in Phase 9 so we no longer inline the data.
    # The file remains on disk for archival reproducibility.
    extended = None

    print("Loading top-K rotation robustness ...", flush=True)
    topk = load_topk_robustness()
    if topk:
        h = topk.get("headline", {})
        print(f"  top-K rotation: K={h.get('K')} {h.get('rebal_freq')}, "
              f"{h.get('n_rebalances')} rebalances, "
              f"Sharpe {h.get('headline_stats', {}).get('sharpe', 0):+.2f}")

    print("Loading asset-class rotation (Strategy B) ...", flush=True)
    asset_class = load_asset_class()
    if asset_class:
        h = asset_class.get("headline", {})
        print(f"  asset-class: K={h.get('K')} {h.get('rebal_freq')}, "
              f"{h.get('n_rebalances')} rebalances, "
              f"Sharpe {h.get('headline_stats', {}).get('sharpe', 0):+.2f}")

    print("Loading thematic rotation (Strategy C) ...", flush=True)
    thematic = load_thematic()
    if thematic:
        h = thematic.get("headline", {})
        print(f"  thematic: K={h.get('K')} {h.get('rebal_freq')}, "
              f"{h.get('n_rebalances')} rebalances, "
              f"Sharpe {h.get('headline_stats', {}).get('sharpe', 0):+.2f}")

    print("Loading Europe sector rotation (Strategy D) ...", flush=True)
    europe = load_europe()
    if europe:
        h = europe.get("headline", {})
        print(f"  europe: K={h.get('K')} {h.get('rebal_freq')}, "
              f"{h.get('n_rebalances')} rebalances, "
              f"Sharpe {h.get('headline_stats', {}).get('sharpe', 0):+.2f}")

    print("Loading multi-strategy combinations (A+B[+C][+D]) ...", flush=True)
    multi = load_multi_strategy()
    if multi:
        strats = multi.get("strategies", {})
        print(f"  multi-strategy: {len(strats)} variants on common window "
              f"{multi.get('common_start')} -> {multi.get('common_end')}")

    print("Loading bootstrap CIs (Phase 7) ...", flush=True)
    bootstrap = load_bootstrap()
    if bootstrap:
        ps = bootstrap.get("per_strategy", {})
        ds = bootstrap.get("differentials", {})
        print(f"  bootstrap: {len(ps)} per-strategy + {len(ds)} paired diffs "
              f"({bootstrap.get('n_bootstrap_samples', 0)} samples, "
              f"block size {bootstrap.get('block_size_days', 0)}d)")

    print("Loading right-tail / regime metrics (Phase 8) ...", flush=True)
    right_tail = load_right_tail()
    if right_tail:
        ps = right_tail.get("per_strategy", {})
        rd = right_tail.get("regime_decomposition", {})
        ts = right_tail.get("top_sleeve_by_month", {})
        print(f"  right-tail: {len(ps)} per-strategy, {len(rd)} regimes, "
              f"{len(ts)} top-sleeve buckets")

    # Phase 28.5 — regime publish freshness verdict, computed once and
    # injected into window.DATA so every dashboard chart that touches the
    # regime state can branch on it without re-implementing the rule.
    # Importing here keeps pipeline import-time light when the helper is
    # absent (older clones); the import is cheap when present.
    from regime_publish import regime_publish_status  # noqa: E402

    print("Loading risk overlay (Phase 19 regime gate) ...", flush=True)
    risk_overlay = load_risk_overlay()
    regime_publish: dict | None = None
    if risk_overlay:
        # Merge the gated variant(s) into the multi-strategy strategies
        # dict so the existing chart-rendering code finds them. Merge
        # the top-level diagnostics into multi.regime_gate for the
        # live regime badge in the hero strip.
        if multi and "strategies" in multi:
            for k, v in (risk_overlay.get("gated_variants") or {}).items():
                multi["strategies"][k] = v
            multi["regime_gate"] = {
                **(risk_overlay.get("gate_parameters") or {}),
                "current_state": risk_overlay.get("current_state"),
                "current_state_since": risk_overlay.get("current_state_since"),
                "current_breadth": risk_overlay.get("current_breadth"),
                "n_switches": risk_overlay.get("n_switches"),
                "days_risk_off": risk_overlay.get("days_risk_off"),
                "pct_days_risk_off": risk_overlay.get("pct_days_risk_off"),
            }
        print(f"  risk_overlay: {len(risk_overlay.get('gated_variants', {}))} "
              f"gated variant(s), current={risk_overlay.get('current_state')} "
              f"since {risk_overlay.get('current_state_since')}")

        # Phase 28.5 — regime publish status. Prefer the panel_end_date
        # field that run_risk_overlay.py now emits (the source-of-truth
        # provenance); fall back to reading breadth_csp1.json directly
        # for older artefacts that pre-date the emission.
        panel_end_iso = risk_overlay.get("panel_end_date")
        if not panel_end_iso:
            try:
                bp = json.loads(
                    (DATA_DIR / "breadth_csp1.json").read_text(encoding="utf-8")
                )
                panel_end_iso = bp.get("end_date")
            except Exception:
                panel_end_iso = None
        cur_breadth = risk_overlay.get("current_breadth")
        gp = risk_overlay.get("gate_parameters") or {}
        if panel_end_iso and cur_breadth is not None:
            try:
                status = regime_publish_status(
                    panel_end_date=date.fromisoformat(panel_end_iso),
                    current_breadth=cur_breadth,
                    off_threshold=gp.get("off_threshold", 0.20),
                    on_threshold=gp.get("on_threshold", 0.50),
                    today=date.today(),
                )
                regime_publish = {
                    **status.as_dict(),
                    "current_state": risk_overlay.get("current_state"),
                    "current_state_since": risk_overlay.get("current_state_since"),
                    "current_breadth": cur_breadth,
                    "historical_revision": risk_overlay.get("historical_revision", []),
                }
                if status.status == "stale":
                    print(f"  REGIME PUBLISH STALE — {status.message}")
                elif status.status == "near":
                    print(f"  REGIME PUBLISH NEAR THRESHOLD — {status.message}")
                else:
                    print(f"  regime publish OK — panel {panel_end_iso}, "
                           f"{status.lag_trading_days} trading day(s) lag")
            except Exception as e:
                print(f"  WARN: could not compute regime publish status: {e}")
        else:
            print("  WARN: regime publish status NOT computed — missing "
                   "panel_end_date or current_breadth")
    else:
        print("  WARNING: data/risk_overlay.json missing — run "
              "scripts/run_risk_overlay.py after run_multi_strategy.py to "
              "enable the Phase 19 breadth regime gate")

    print("Loading live mark-to-market overlay ...", flush=True)
    live_track = load_live_track()
    if live_track:
        n = len(live_track.get("live_dates") or [])
        print(f"  live_track: anchor {live_track.get('anchor_date')} + "
              f"{n} intra-week point(s)")
    else:
        print("  live_track absent — dashboard will show Friday-anchor series only")

    print("Loading holdings 1Y prices ...", flush=True)
    holdings_prices = load_holdings_prices()
    if holdings_prices:
        n = len(holdings_prices.get("prices", {}))
        print(f"  holdings_prices: {n} tickers (built by "
              f"scripts/export_holdings_prices.py)")
    else:
        print("  WARNING: data/holdings_prices_1y.json missing — "
              "run scripts/export_holdings_prices.py to enable the "
              "holdings click-to-expand mini-chart")

    # Use the date library for the build timestamp — never string-concat.
    # CLAUDE.md date-handling rule: "Always use a date library."
    built_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    assert_built_at_valid(built_at)

    # ------------------------------------------------------------------
    # Phase 2.3 — staleness guard on every deployed signal series.
    # If any sleeve's equity curve (the most-aggregated daily signal we
    # publish) is flat for the trailing 20 trading days, abort the
    # publish. This catches the Phase 4 frozen-breadth regression mode
    # and any future stuck-pipeline incident before the dashboard goes
    # out with a silently dead signal.
    # ------------------------------------------------------------------
    def _check_sleeve_equity(label: str, blob: dict | None) -> None:
        if not blob:
            return
        hl = blob.get("headline", {}) or {}
        dates = hl.get("headline_equity_dates") or []
        equity = hl.get("headline_equity") or []
        if dates and equity:
            assert_series_not_frozen(f"{label} headline equity",
                                       dates, equity)

    _check_sleeve_equity("Strategy A (topk_robustness)", topk)
    _check_sleeve_equity("Strategy B (asset_class_rotation)", asset_class)
    _check_sleeve_equity("Strategy C (thematic_rotation)", thematic)
    _check_sleeve_equity("Strategy D (europe_rotation)", europe)

    # The deployed blend itself — last-line check before publish.
    if multi and multi.get("strategies"):
        for key in ("blend_35_35_10_20_gated_eem_tilted",
                     "blend_35_35_10_20_gated", "blend_35_35_10_20"):
            s = multi["strategies"].get(key)
            if s and s.get("dates") and s.get("equity"):
                assert_series_not_frozen(f"deployed blend ({key})",
                                           s["dates"], s["equity"])
                break

    # ------------------------------------------------------------------
    # Live mark-to-market extension — splice the daily intra-week NAV
    # points into the deployed blend equity series so the dashboard's
    # WTD card, hero stats, and Performance chart show data through the
    # latest weekday close rather than stopping on Friday.
    #
    # This is a strictly forward-only extension from the live_track
    # anchor (which must match the Friday-anchored series' last date).
    # If the anchor disagrees, we skip the extension and surface a
    # warning rather than blend mismatched series.
    # ------------------------------------------------------------------
    if live_track and multi and multi.get("strategies"):
        # --- Splice the deployed-blend extension --------------------
        live_dates = live_track.get("live_dates") or []
        live_equity = live_track.get("live_equity") or []
        anchor_date = live_track.get("anchor_date")
        if live_dates and len(live_dates) == len(live_equity):
            key = live_track.get("deployed_key",
                                   "blend_35_35_10_20_gated_eem_tilted")
            target = multi["strategies"].get(key)
            if target and target.get("dates"):
                if target["dates"][-1] != anchor_date:
                    print(f"  WARN: live_track anchor {anchor_date} does not "
                          f"match deployed blend last date "
                          f"{target['dates'][-1]} — skipping extension")
                else:
                    target["dates"] = list(target["dates"]) + list(live_dates)
                    target["equity"] = list(target["equity"]) + list(live_equity)
                    if multi.get("common_end") and live_dates[-1] > multi["common_end"]:
                        multi["common_end"] = live_dates[-1]
                    print(f"  spliced {len(live_dates)} live-track point(s) "
                          f"into {key}; series now ends {live_dates[-1]}")

        # --- Splice the per-sleeve extensions so the Performance chart
        # sleeve lines (A/B/C/D) reach the same end-date as the
        # deployed-blend line. Mismatched anchors are skipped per-sleeve
        # rather than failing the whole splice.
        sleeve_ext = live_track.get("sleeve_extensions") or {}
        for ms_key, ext in sleeve_ext.items():
            target = multi["strategies"].get(ms_key)
            if not (target and target.get("dates") and ext.get("dates")):
                continue
            if target["dates"][-1] != ext.get("anchor_date"):
                print(f"  WARN: live sleeve anchor mismatch for {ms_key} "
                      f"(target {target['dates'][-1]} vs live "
                      f"{ext.get('anchor_date')}) — skipping")
                continue
            target["dates"] = list(target["dates"]) + list(ext["dates"])
            target["equity"] = list(target["equity"]) + list(ext["equity"])
            print(f"  spliced {len(ext['dates'])} live point(s) into "
                  f"{ms_key}; series now ends {ext['dates'][-1]}")

    # Per-panel 'data as of' dates extracted from the sleeve JSONs.
    # The dashboard JS reads window.DATA.signals_asof to render a
    # 'Signals as of YYYY-MM-DD' badge under each strategy panel.
    def _last_date(blob: dict | None) -> str | None:
        if not blob:
            return None
        hl = blob.get("headline", {}) or {}
        dates = hl.get("headline_equity_dates") or []
        return dates[-1] if dates else None

    signals_asof = {
        "a": _last_date(topk),
        "b": _last_date(asset_class),
        "c": _last_date(thematic),
        "d": _last_date(europe),
        "blend": multi.get("common_end") if multi else None,
        "overlay": (risk_overlay or {}).get("current_state_since"),
    }
    print(f"\nSignals as-of: {signals_asof}")

    # Phase 26.1 — data-integrity scan, extended Phase 26.4 (2026-06-01)
    # to handle LEGACY constituent files that were generated before the
    # `staleness` block existed. The scan now:
    #   1. Uses the staleness block when present (the normal path).
    #   2. Falls back to computing staleness from `end_friday` + today
    #      when the block is missing, applying the global default
    #      thresholds (14d warn / 30d critical). This catches files
    #      that haven't been re-fetched since Phase 26.1 added the
    #      block — without this fallback, the alarm framework is
    #      blind to anything generated by an older fetcher run.
    # Both paths feed into window.DATA.data_integrity for the dashboard
    # banner. Critical status STILL aborts publish via SystemExit.
    from datetime import date as _date
    today = _date.today()
    GLOBAL_WARN = 14
    GLOBAL_CRITICAL = 30
    data_integrity = []
    for path in sorted(DATA_DIR.glob("constituents_*.json")):
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        etf_label = blob.get(
            "etf", path.stem.replace("constituents_", "").upper(),
        )
        s = blob.get("staleness")
        if s:
            status = s.get("status")
            if status in (None, "fresh"):
                continue
            data_integrity.append({
                "etf": etf_label,
                "status": status,
                "days_since_last_real_fetch": s.get("days_since_last_real_fetch"),
                "last_real_fetch_date": s.get("last_real_fetch_date"),
                "warn_threshold_days": s.get("warn_threshold_days"),
                "critical_threshold_days": s.get("critical_threshold_days"),
                "source": "staleness_block",
            })
            continue
        # Legacy-file fallback: derive staleness from end_friday.
        end_friday = blob.get("end_friday")
        if not end_friday:
            continue
        try:
            ef_date = _date.fromisoformat(end_friday)
        except ValueError:
            continue
        days = (today - ef_date).days
        if days > GLOBAL_CRITICAL:
            status = "critical"
        elif days > GLOBAL_WARN:
            status = "warning"
        else:
            continue  # fresh — do not list
        data_integrity.append({
            "etf": etf_label,
            "status": status,
            "days_since_last_real_fetch": days,
            "last_real_fetch_date": end_friday,
            "warn_threshold_days": GLOBAL_WARN,
            "critical_threshold_days": GLOBAL_CRITICAL,
            "source": "derived_from_end_friday",
        })
    if data_integrity:
        bar = "-" * 60
        print(f"\n{bar}\nData integrity — non-fresh constituent rosters:")
        for d in data_integrity:
            print(f"  [{d['status'].upper()}] {d['etf']}: "
                  f"{d['days_since_last_real_fetch']} days since last "
                  f"real fetch (last good {d['last_real_fetch_date']})")
        print(bar)
        critical = [d for d in data_integrity if d["status"] == "critical"]
        if critical:
            etfs = ", ".join(c["etf"] for c in critical)
            raise SystemExit(
                f"PUBLISH ABORTED: {len(critical)} roster(s) exceed the "
                f"{critical[0]['critical_threshold_days']}-day critical "
                f"staleness threshold ({etfs}). See "
                f"DATA_INTEGRITY_POLICY.md for remediation."
            )

    # Phase 28.1 — centralised data health for the new Data Health tab
    # + persistent header badge. Computed AFTER every other load so the
    # report reflects what was actually published.
    data_health = _compute_data_health(date.today())
    print(f"\nData health: overall={data_health['overall_status']}  "
           f"OK={data_health['counts']['ok']}  "
           f"WARN={data_health['counts']['warn']}  "
           f"STALE={data_health['counts']['stale']}  "
           f"BROKEN={data_health['counts']['broken']}")

    # Phase 28.5 — fail the build loudly on a stale regime panel (FM-1
    # primary defense, complementing the renderers' STALE banner). The
    # 2026-03-27 -> 2026-06-18 incident published a confident regime
    # headline for 11 weeks because no step in the chain refused to
    # publish on stale breadth. This abort is overridable via the
    # ALLOW_STALE_REGIME env var for local dev — never set this in CI.
    import os as _os
    if (regime_publish
            and regime_publish.get("status") == "stale"
            and not _os.environ.get("ALLOW_STALE_REGIME")):
        raise RuntimeError(
            "Pipeline aborted: the regime publish status is STALE. "
            f"{regime_publish.get('message','')} "
            "Run `python scripts/refresh_all.py` (or set "
            "ALLOW_STALE_REGIME=1 for a one-off local dev rebuild)."
        )

    data = {
        "built_at": built_at,
        "signals_asof": signals_asof,
        # Registry-derived traded symbol per panel, for EVERY sleeve.
        #
        # The dashboard previously resolved a row's traded ticker from
        # ma200.monitor, which only carries the 14 Strategy A members. For
        # any other sleeve it fell back to the panel key, so the Europe rows
        # asked for prices under "EXV1" when the series is keyed "EXV1.DE"
        # and silently rendered no chart. Worse for EXH3, whose panel key is
        # not its traded symbol at all — it trades as EXH4.DE.
        #
        # This is the same defect class the Europe symbol contract exists to
        # prevent: a surface deriving the traded symbol itself rather than
        # reading the registry. Emitting the map means the dashboard reads
        # it like everything else.
        "trading_proxies": {
            etf: (cfg.get("yfinance_trading_proxy") or etf)
            for etf, cfg in ETF_REGISTRY.items()
        },
        # Registry-derived ticker to PRINT per panel — a different question
        # from trading_proxies above, which answers "what do I fetch prices
        # under". The two diverge in both directions: sleeve A prices IUFS off
        # XLF but holds IUFS, so the label must stay IUFS; sleeve D's EXH3 is
        # an internal panel id for a fund that trades as EXH4.DE, so the label
        # must become EXH4. Resolved by etf_registry.display_ticker so the
        # dashboard, the factsheet, the weekly email and the portfolio page
        # all print the same answer.
        "display_tickers": display_ticker_map(),
        "data_integrity": data_integrity,
        "data_health": data_health,
        "regime_publish": regime_publish,
        "live_track": live_track,
        "ma200": ma200,
        "portfolio": portfolio,
        "robustness": robustness,
        "extended": extended,
        "topk": topk,
        "asset_class": asset_class,
        "thematic": thematic,
        "europe": europe,
        "multi": multi,
        "bootstrap": bootstrap,
        "right_tail": right_tail,
        "holdings_prices": holdings_prices,
        "risk_overlay": risk_overlay,
    }

    template_text = TEMPLATE.read_text(encoding="utf-8")
    print(f"\nTemplate size: {len(template_text):,} bytes")
    # Guard against publishing a corrupted template that already has
    # conflict markers — caught at READ time, before injection.
    assert_no_conflict_markers(TEMPLATE)
    built = inject(template_text, data)
    DOCS.mkdir(parents=True, exist_ok=True)
    OUT.write_text(built, encoding="utf-8")
    size_kb = len(built) / 1024
    print(f"Wrote {OUT.relative_to(PROJECT_ROOT)}  ({len(built):,} bytes, {size_kb:.1f} KB)")
    # Guard against publishing the actual built output with markers.
    # This is the post-2026-05-30 hotfix backstop.
    assert_no_conflict_markers(OUT)

    # Data tab payload — built here rather than inlined between the
    # __DASHBOARD_DATA_*__ markers because index.html is already ~7.3MB and
    # this ~1.2MB payload serves a tab most visits never open. Built in the
    # SAME run as the page so the two cannot drift: the Data tab always
    # describes the panels this dashboard was built from.
    print(f"\nBuilding Data tab payload ...")
    try:
        import build_data_audit
        # Per-panel weekly series for the Data tab's expandable charts.
        # Separate files under docs/panel/ so opening one panel does not
        # pull the other 37; weekly rather than daily to keep the committed
        # footprint at ~2.2MB instead of ~9.2MB.
        import build_panel_series
    except Exception as exc:
        print(f"  WARN: data audit build unavailable (non-fatal): {exc}",
              file=sys.stderr)
    else:
        # Run the two independently. They read the same price caches, so a
        # stale cache stops BOTH — but each must still get its own attempt,
        # or one refusing to publish would silently suppress the other.
        stale: list[str] = []
        for label, fn in (("data audit", build_data_audit.write),
                          ("panel series", build_panel_series.write_all)):
            try:
                fn()
            except build_panel_series.StalePriceCacheError as exc:
                stale.append(str(exc))
            except Exception as exc:
                print(f"  WARN: {label} build failed (non-fatal): {exc}",
                      file=sys.stderr)
        if stale:
            # Distinct from a crash: the build worked and deliberately
            # declined to publish. A banner rather than a one-liner because
            # what it protects looks perfectly healthy when wrong — a
            # correct date label sitting over a days-old price.
            print("\n" + "!" * 72, file=sys.stderr)
            print("  STALE PRICE CACHE — Data tab outputs NOT rewritten",
                  file=sys.stderr)
            for message in stale:
                print(f"  {message}", file=sys.stderr)
            print("!" * 72 + "\n", file=sys.stderr)

    # B28 — Auto-generate the monthly factsheet PDF from the same data
    # the dashboard uses. Soft-fail if matplotlib is unavailable so a
    # broken factsheet build does not break the dashboard pipeline.
    print(f"\nBuilding factsheet PDF ...")
    try:
        import build_factsheet
        build_factsheet.build(DOCS / "factsheet_latest.pdf")
    except Exception as exc:
        print(f"  WARN: factsheet build failed (non-fatal): {exc}",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
